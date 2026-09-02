from __future__ import annotations

import hashlib
import hmac
import json
import threading
import urllib.request

import pytest

from coco.chief_of_staff import (
    MAX_WEBHOOK_BODY_BYTES,
    ChiefOfStaffInbox,
    ChiefOfStaffHttpServer,
    ChiefOfStaffRequestHandler,
    ChiefOfStaffRuntime,
    ChiefOfStaffValidationError,
    GhRepositoryClient,
    WebhookProcessor,
    build_control_prompt,
    parse_push_delivery,
    validate_directive,
    verify_github_signature,
)


REPOSITORY = "pcopu/chief-of-staff"
REF = "refs/heads/main"
PATH = "directives/2026/08/dir_voice_001.json"
AFTER = "a" * 40


def _directive_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": "cos.directive/v1",
        "directive_id": "dir_voice_001",
        "captured_at": "2026-08-31T15:30:00Z",
        "source": {
            "channel": "chatgpt_voice",
            "principal": "github:pcopu",
            "transcript": "Tell the Tax topic that the route started at home.",
        },
        "instruction": "Tell the Tax topic that the route started at home.",
        "scope": {
            "repositories": ["pcopu/tax-ledger"],
            "topics": ["Tax"],
        },
        "idempotency": {"directive_key": "dir_voice_001"},
    }
    payload.update(overrides)
    return payload


def _push_payload(
    *,
    added: list[str] | None = None,
    modified: list[str] | None = None,
    removed: list[str] | None = None,
    repository: str = REPOSITORY,
    ref: str = REF,
) -> bytes:
    return json.dumps(
        {
            "ref": ref,
            "after": AFTER,
            "repository": {"full_name": repository},
            "commits": [
                {
                    "id": AFTER,
                    "added": added if added is not None else [PATH],
                    "modified": modified or [],
                    "removed": removed or [],
                }
            ],
        },
        separators=(",", ":"),
    ).encode()


def _headers(secret: str, body: bytes, *, delivery: str = "delivery-1") -> dict[str, str]:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {
        "X-GitHub-Event": "push",
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": f"sha256={digest}",
    }


def test_verify_github_signature_requires_exact_hmac() -> None:
    body = b'{"ok":true}'
    secret = "webhook-secret"
    signature = "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()

    assert verify_github_signature(secret, body, signature) is True
    assert verify_github_signature(secret, body + b"x", signature) is False
    assert verify_github_signature(secret, body, "sha1=bad") is False


def test_parse_push_delivery_accepts_only_exact_repo_ref_and_added_directives() -> None:
    body = _push_payload(added=[PATH, "README.md"])
    delivery = parse_push_delivery(
        _headers("secret", body),
        body,
        webhook_secret="secret",
        expected_repository=REPOSITORY,
        expected_ref=REF,
    )

    assert delivery.delivery_id == "delivery-1"
    assert delivery.repository == REPOSITORY
    assert delivery.commit_sha == AFTER
    assert delivery.directive_paths == (PATH,)


@pytest.mark.parametrize(
    ("headers_patch", "payload_patch"),
    [
        ({"X-GitHub-Event": "issues"}, {}),
        ({"X-GitHub-Delivery": ""}, {}),
        ({"X-Hub-Signature-256": "sha256=bad"}, {}),
        ({}, {"repository": "someone/else"}),
        ({}, {"ref": "refs/heads/dev"}),
    ],
)
def test_parse_push_delivery_fails_closed_on_wrong_envelope(
    headers_patch: dict[str, str], payload_patch: dict[str, str]
) -> None:
    body = _push_payload(**payload_patch)
    headers = _headers("secret", body)
    headers.update(headers_patch)

    with pytest.raises(ChiefOfStaffValidationError):
        parse_push_delivery(
            headers,
            body,
            webhook_secret="secret",
            expected_repository=REPOSITORY,
            expected_ref=REF,
        )


def test_parse_push_delivery_rejects_oversized_body() -> None:
    body = b"x" * (MAX_WEBHOOK_BODY_BYTES + 1)

    with pytest.raises(ChiefOfStaffValidationError, match="too large"):
        parse_push_delivery(
            _headers("secret", body),
            body,
            webhook_secret="secret",
            expected_repository=REPOSITORY,
            expected_ref=REF,
        )


@pytest.mark.parametrize("field", ["modified", "removed"])
def test_parse_push_delivery_rejects_directive_mutation(field: str) -> None:
    kwargs = {field: [PATH], "added": []}
    body = _push_payload(**kwargs)

    with pytest.raises(ChiefOfStaffValidationError, match="immutable"):
        parse_push_delivery(
            _headers("secret", body),
            body,
            webhook_secret="secret",
            expected_repository=REPOSITORY,
            expected_ref=REF,
        )


def test_validate_directive_and_build_prompt_preserve_authority_boundary() -> None:
    directive = validate_directive(PATH, json.dumps(_directive_payload()).encode())
    prompt = build_control_prompt(
        directive,
        repository=REPOSITORY,
        commit_sha=AFTER,
        path=PATH,
    )

    assert directive.directive_id == "dir_voice_001"
    assert "Chief of Staff directive `dir_voice_001`" in prompt
    assert "Tell the Tax topic that the route started at home." in prompt
    assert "does not expand your authority" in prompt
    assert "Ignore inaccessible targets" in prompt
    assert "pcopu/tax-ledger" in prompt
    assert "Tax" in prompt


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: {**value, "schema": "other/v1"},
        lambda value: {**value, "directive_id": "dir_wrong"},
        lambda value: {**value, "instruction": ""},
        lambda value: {
            **value,
            "scope": {"repositories": ["https://github.com/pcopu/repo"], "topics": []},
        },
        lambda value: {
            **value,
            "scope": {"repositories": ["pcopu/*"], "topics": []},
        },
        lambda value: {
            **value,
            "scope": {"repositories": [], "topics": ["Tax*" ]},
        },
    ],
)
def test_validate_directive_rejects_invalid_or_dynamic_targets(mutator) -> None:
    payload = mutator(_directive_payload())

    with pytest.raises(ChiefOfStaffValidationError):
        validate_directive(PATH, json.dumps(payload).encode())


def test_inbox_persists_before_ack_and_dedupes_delivery_commit_path(tmp_path) -> None:
    body = _push_payload()
    delivery = parse_push_delivery(
        _headers("secret", body),
        body,
        webhook_secret="secret",
        expected_repository=REPOSITORY,
        expected_ref=REF,
    )
    inbox = ChiefOfStaffInbox(tmp_path / "chief.sqlite3")
    processor = WebhookProcessor(
        inbox=inbox,
        webhook_secret="secret",
        expected_repository=REPOSITORY,
        expected_ref=REF,
    )

    first = processor.accept(_headers("secret", body), body)
    second = processor.accept(_headers("secret", body), body)

    assert first.persisted == 1
    assert second.persisted == 0
    records = inbox.list_records()
    assert len(records) == 1
    assert records[0].status == "pending"
    assert records[0].commit_sha == delivery.commit_sha
    assert records[0].path == PATH


def test_http_receiver_returns_202_only_after_persisting(tmp_path) -> None:
    secret = "secret"
    body = _push_payload()
    inbox = ChiefOfStaffInbox(tmp_path / "chief.sqlite3")
    server = ChiefOfStaffHttpServer(
        ("127.0.0.1", 0),
        ChiefOfStaffRequestHandler,
    )
    server.processor = WebhookProcessor(
        inbox=inbox,
        webhook_secret=secret,
        expected_repository=REPOSITORY,
        expected_ref=REF,
    )
    server.webhook_path = "/github/push"
    server.inbox = inbox
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/github/push",
            data=body,
            method="POST",
            headers={
                **_headers(secret, body),
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read())

        assert response.status == 202
        assert payload["persisted"] == 1
        assert inbox.list_records()[0].status == "pending"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_http_receiver_accepts_signed_github_ping_without_queueing(tmp_path) -> None:
    secret = "secret"
    body = json.dumps(
        {"zen": "Keep it logically awesome.", "repository": {"full_name": REPOSITORY}},
        separators=(",", ":"),
    ).encode()
    headers = _headers(secret, body, delivery="ping-1")
    headers["X-GitHub-Event"] = "ping"
    inbox = ChiefOfStaffInbox(tmp_path / "chief.sqlite3")
    server = ChiefOfStaffHttpServer(
        ("127.0.0.1", 0),
        ChiefOfStaffRequestHandler,
    )
    server.processor = WebhookProcessor(
        inbox=inbox,
        webhook_secret=secret,
        expected_repository=REPOSITORY,
        expected_ref=REF,
    )
    server.webhook_path = "/github/push"
    server.inbox = inbox
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/github/push",
            data=body,
            method="POST",
            headers={**headers, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read())

        assert response.status == 200
        assert payload == {"ok": True, "pong": True, "delivery_id": "ping-1"}
        assert inbox.list_records() == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_inbox_recovers_pre_dispatch_processing_but_not_uncertain_dispatch(tmp_path) -> None:
    db_path = tmp_path / "chief.sqlite3"
    inbox = ChiefOfStaffInbox(db_path)
    body = _push_payload(added=[PATH, "directives/2026/08/dir_voice_002.json"])
    delivery = parse_push_delivery(
        _headers("secret", body),
        body,
        webhook_secret="secret",
        expected_repository=REPOSITORY,
        expected_ref=REF,
    )
    inbox.persist_delivery(delivery)
    first = inbox.claim_next(now=100.0)
    second = inbox.claim_next(now=100.0)
    assert first is not None and second is not None
    inbox.mark_dispatching(second.record_id)

    recovered = ChiefOfStaffInbox(db_path)
    by_path = {record.path: record for record in recovered.list_records()}

    assert by_path[PATH].status == "pending"
    assert by_path["directives/2026/08/dir_voice_002.json"].status == "manual_review"


def test_inbox_can_open_without_mutating_live_processing_state(tmp_path) -> None:
    db_path = tmp_path / "chief.sqlite3"
    inbox = ChiefOfStaffInbox(db_path)
    body = _push_payload()
    inbox.persist_delivery(
        parse_push_delivery(
            _headers("secret", body),
            body,
            webhook_secret="secret",
            expected_repository=REPOSITORY,
            expected_ref=REF,
        )
    )
    assert inbox.claim_next(now=100.0) is not None

    observer = ChiefOfStaffInbox(db_path, recover=False)

    assert observer.list_records()[0].status == "processing"


@pytest.mark.asyncio
async def test_runtime_fetches_validates_and_dispatches_one_record(tmp_path) -> None:
    inbox = ChiefOfStaffInbox(tmp_path / "chief.sqlite3")
    body = _push_payload()
    delivery = parse_push_delivery(
        _headers("secret", body),
        body,
        webhook_secret="secret",
        expected_repository=REPOSITORY,
        expected_ref=REF,
    )
    inbox.persist_delivery(delivery)

    class _Repository:
        async def fetch_file(self, repository: str, path: str, ref: str) -> bytes:
            assert (repository, path, ref) == (REPOSITORY, PATH, AFTER)
            return json.dumps(_directive_payload()).encode()

        async def list_directive_paths(self, repository: str, ref: str):
            raise AssertionError("not used")

    calls: list[dict[str, object]] = []

    class _Controller:
        async def deliver(self, payload: dict[str, object]) -> dict[str, object]:
            calls.append(payload)
            return {"accepted": True, "status": "started"}

    runtime = ChiefOfStaffRuntime(
        inbox=inbox,
        repository_client=_Repository(),
        controller_client=_Controller(),
        repository=REPOSITORY,
        ref=REF,
        control_chat_id=-1003841129251,
    )

    assert await runtime.process_once(now=100.0) is True
    assert inbox.list_records()[0].status == "done"
    assert calls[0]["directive_id"] == "dir_voice_001"
    assert calls[0]["control_chat_id"] == -1003841129251
    assert "does not expand your authority" in str(calls[0]["prompt"])


@pytest.mark.asyncio
async def test_runtime_keeps_busy_directive_durable_for_retry(tmp_path) -> None:
    inbox = ChiefOfStaffInbox(tmp_path / "chief.sqlite3")
    body = _push_payload()
    inbox.persist_delivery(
        parse_push_delivery(
            _headers("secret", body),
            body,
            webhook_secret="secret",
            expected_repository=REPOSITORY,
            expected_ref=REF,
        )
    )

    class _Repository:
        async def fetch_file(self, *_args) -> bytes:
            return json.dumps(_directive_payload()).encode()

    class _Controller:
        async def deliver(self, _payload: dict[str, object]) -> dict[str, object]:
            return {"accepted": False, "status": "busy", "retry_after": 30}

    runtime = ChiefOfStaffRuntime(
        inbox=inbox,
        repository_client=_Repository(),
        controller_client=_Controller(),
        repository=REPOSITORY,
        ref=REF,
        control_chat_id=-1003841129251,
    )

    assert await runtime.process_once(now=100.0) is True
    record = inbox.list_records()[0]
    assert record.status == "retry"
    assert record.next_attempt_at == 130.0


@pytest.mark.asyncio
async def test_reconcile_persists_unseen_directives(tmp_path) -> None:
    inbox = ChiefOfStaffInbox(tmp_path / "chief.sqlite3")

    class _Repository:
        async def list_directive_paths(self, repository: str, ref: str):
            assert (repository, ref) == (REPOSITORY, "main")
            return AFTER, [PATH, "README.md"]

    runtime = ChiefOfStaffRuntime(
        inbox=inbox,
        repository_client=_Repository(),
        controller_client=object(),
        repository=REPOSITORY,
        ref="main",
        control_chat_id=-1003841129251,
    )

    assert await runtime.reconcile(delivery_id="reconcile-100") == 1
    assert inbox.list_records()[0].path == PATH


@pytest.mark.asyncio
async def test_gh_tree_listing_returns_commit_sha_not_tree_sha() -> None:
    commit_sha = "b" * 40
    tree_sha = "c" * 40

    class _Client(GhRepositoryClient):
        def _run(self, endpoint: str):
            if endpoint == "repos/pcopu/chief-of-staff/commits/main":
                return {
                    "sha": commit_sha,
                    "commit": {"tree": {"sha": tree_sha}},
                }
            if endpoint == f"repos/pcopu/chief-of-staff/git/trees/{tree_sha}?recursive=1":
                return {
                    "sha": tree_sha,
                    "truncated": False,
                    "tree": [{"type": "blob", "path": PATH}],
                }
            raise AssertionError(endpoint)

    result_sha, paths = await _Client().list_directive_paths(REPOSITORY, "main")

    assert result_sha == commit_sha
    assert paths == [PATH]
