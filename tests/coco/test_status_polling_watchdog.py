"""Tests for watchdog resend behavior in status polling."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest, NetworkError, RetryAfter

import coco.handlers.run_watchdog as run_watchdog
import coco.handlers.status_polling as status_polling
from coco.handlers.run_watchdog import RunWatchCheck


@pytest.fixture
def isolated_run_watchdog(tmp_path, monkeypatch):
    monkeypatch.setattr(
        run_watchdog,
        "_RUN_RETRY_STATE_FILE",
        tmp_path / "run_watchdog_retry_state.json",
    )
    run_watchdog.reset_run_watchdog_for_tests()
    yield
    run_watchdog.reset_run_watchdog_for_tests()


@pytest.mark.asyncio
async def test_probe_stale_nodes_bounds_unreachable_agent_delay(monkeypatch):
    marked: list[float] = []
    stale_nodes = [
        SimpleNamespace(
            machine_id=f"offline-node-{index}",
            display_name=f"Offline Node {index}",
            is_local=False,
            status=status_polling.NODE_STATUS_ONLINE,
            last_seen_ts=1.0,
        )
        for index in range(3)
    ]
    registry = SimpleNamespace(
        offline_timeout_seconds=1.0,
        iter_nodes=lambda: stale_nodes,
        mark_stale_nodes_offline=lambda *, now: marked.append(now),
    )
    monkeypatch.setattr(status_polling, "node_registry", registry)
    monkeypatch.setattr(status_polling, "NODE_PROBE_TIMEOUT", 0.05)
    monkeypatch.setattr(status_polling, "_iter_monitor_workers", lambda **_kwargs: [])
    monkeypatch.setattr(status_polling, "emit_telemetry", lambda *_args, **_kwargs: None)

    async def _never_returns(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(status_polling, "_probe_machine_from_monitor", _never_returns)

    await asyncio.wait_for(status_polling._probe_stale_nodes(None, now=10.0), timeout=0.08)

    assert marked == [10.0]


@pytest.mark.asyncio
async def test_window_health_probe_routes_to_remote_owner(monkeypatch):
    remote_calls: list[tuple[str, float]] = []
    accepted_results: list[tuple[str, dict[str, object]]] = []

    monkeypatch.setattr(
        status_polling.session_manager,
        "get_window_machine_id",
        lambda _window_id: "remote-node",
    )
    monkeypatch.setattr(
        status_polling.session_manager,
        "_local_machine_identity",
        lambda: ("local-node", "Local Node"),
    )
    monkeypatch.setattr(
        status_polling.node_registry,
        "get_node",
        lambda _machine_id: SimpleNamespace(
            runtime={"codex_transport_protocol_version": 1},
        ),
    )

    async def _remote_probe(
        machine_id: str,
        *,
        timeout: float,
    ) -> dict[str, object]:
        remote_calls.append((machine_id, timeout))
        return {
            "healthy": True,
            "transport_epoch": "epoch-1",
            "transport_epoch_started_at": 10.0,
            "transport_generation": 4,
            "transport_reset_sequence": 2,
            "transport_last_reset_generation": 3,
            "transport_last_reset_reason": "request_timeout",
        }

    async def _accept_remote_result(
        *,
        window_id: str,
        result: dict[str, object],
    ) -> bool:
        accepted_results.append((window_id, result))
        return True

    async def _unexpected_local_probe(*, timeout: float) -> bool:
        _ = timeout
        raise AssertionError("remote window probed controller-local app-server")

    monkeypatch.setattr(
        status_polling.agent_rpc_client,
        "probe_codex_health_state",
        _remote_probe,
        raising=False,
    )
    monkeypatch.setattr(
        status_polling.session_manager,
        "_accept_remote_transport_result",
        _accept_remote_result,
    )
    monkeypatch.setattr(
        status_polling.codex_app_server_client,
        "probe_health",
        _unexpected_local_probe,
    )

    healthy = await status_polling._probe_window_codex_transport_health(
        window_id="@remote",
        timeout=5.0,
    )

    assert healthy is True
    assert remote_calls == [("remote-node", 5.0)]
    assert accepted_results == [
        (
            "@remote",
            {
                "healthy": True,
                "transport_epoch": "epoch-1",
                "transport_epoch_started_at": 10.0,
                "transport_generation": 4,
                "transport_reset_sequence": 2,
                "transport_last_reset_generation": 3,
                "transport_last_reset_reason": "request_timeout",
            },
        )
    ]


@pytest.mark.asyncio
async def test_window_health_probe_skips_unsupported_legacy_agent(
    monkeypatch,
):
    monkeypatch.setattr(
        status_polling.session_manager,
        "get_window_machine_id",
        lambda _window_id: "legacy-node",
    )
    monkeypatch.setattr(
        status_polling.session_manager,
        "_local_machine_identity",
        lambda: ("local-node", "Local Node"),
    )
    monkeypatch.setattr(
        status_polling.session_manager,
        "get_window_codex_transport_state",
        lambda _window_id: ("old-modern-epoch", 100.0, 4),
    )
    monkeypatch.setattr(
        status_polling.node_registry,
        "get_node",
        lambda _machine_id: SimpleNamespace(
            runtime={},
            codex_transport_legacy_confirmed=True,
        ),
    )

    async def _unexpected_remote_probe(*_args, **_kwargs):
        raise AssertionError("legacy agent does not expose health-state RPC")

    monkeypatch.setattr(
        status_polling.agent_rpc_client,
        "probe_codex_health_state",
        _unexpected_remote_probe,
    )

    healthy = await status_polling._probe_window_codex_transport_health(
        window_id="@legacy",
        timeout=5.0,
    )

    assert healthy is True


@pytest.mark.asyncio
async def test_window_health_probe_rejects_stale_remote_transport_snapshot(
    monkeypatch,
):
    monkeypatch.setattr(
        status_polling.session_manager,
        "get_window_machine_id",
        lambda _window_id: "remote-node",
    )
    monkeypatch.setattr(
        status_polling.session_manager,
        "_local_machine_identity",
        lambda: ("local-node", "Local Node"),
    )
    monkeypatch.setattr(
        status_polling.node_registry,
        "get_node",
        lambda _machine_id: SimpleNamespace(
            runtime={"codex_transport_protocol_version": 1},
        ),
    )

    async def _remote_probe(
        _machine_id: str,
        *,
        timeout: float,
    ) -> dict[str, object]:
        _ = timeout
        return {
            "healthy": True,
            "transport_epoch": "replacement-epoch",
            "transport_epoch_started_at": 20.0,
            "transport_generation": 1,
            "transport_reset_sequence": 0,
            "transport_last_reset_generation": 0,
            "transport_last_reset_reason": "",
        }

    async def _reject_remote_result(
        *,
        window_id: str,
        result: dict[str, object],
    ) -> bool:
        assert window_id == "@remote"
        assert result["transport_epoch"] == "replacement-epoch"
        return False

    monkeypatch.setattr(
        status_polling.agent_rpc_client,
        "probe_codex_health_state",
        _remote_probe,
        raising=False,
    )
    monkeypatch.setattr(
        status_polling.session_manager,
        "_accept_remote_transport_result",
        _reject_remote_result,
    )

    healthy = await status_polling._probe_window_codex_transport_health(
        window_id="@remote",
        timeout=5.0,
    )

    assert healthy is False


@pytest.mark.asyncio
async def test_emit_due_watchdog_skips_resend_when_active_turn(monkeypatch):
    sent_messages: list[str] = []
    telemetry: list[tuple[str, dict[str, object]]] = []
    health_probe_timeouts: list[float] = []

    check = RunWatchCheck(
        user_id=1,
        thread_id=10,
        window_id="@1",
        checkpoint_seconds=30,
        elapsed_seconds=30.0,
        resend_text="retry me",
        resend_text_len=8,
        pending_fingerprint="abc",
        auto_retry_allowed=True,
        auto_retry_reason="eligible",
        retry_count=0,
        max_auto_retries=2,
    )

    monkeypatch.setattr(status_polling, "get_interactive_window", lambda _u, _t: None)
    monkeypatch.setattr(status_polling, "get_due_run_checks", lambda **_kwargs: [check])
    monkeypatch.setattr(status_polling.session_manager, "resolve_chat_id", lambda _u, _t: -100)
    monkeypatch.setattr(status_polling.session_manager, "get_display_name", lambda _w: "demo")
    monkeypatch.setattr(
        status_polling.session_manager,
        "get_window_codex_thread_id",
        lambda _w: "thread_1",
    )
    monkeypatch.setattr(
        status_polling.session_manager,
        "get_window_codex_active_turn_id",
        lambda _w: "turn_1",
    )
    monkeypatch.setattr(status_polling.codex_app_server_client, "is_turn_in_progress", lambda _t: True)

    async def _probe_health(*, timeout: float) -> bool:
        health_probe_timeouts.append(timeout)
        return True

    monkeypatch.setattr(
        status_polling.codex_app_server_client,
        "probe_health",
        _probe_health,
        raising=False,
    )

    def _unexpected_retry_attempt(**_kwargs):
        raise AssertionError("retry attempt should be skipped while turn is active")

    async def _unexpected_send_to_window(*_args, **_kwargs):
        raise AssertionError(
            "send_topic_text_to_window should not be called while turn is active"
        )

    monkeypatch.setattr(status_polling, "note_auto_retry_attempt", _unexpected_retry_attempt)
    monkeypatch.setattr(
        status_polling.session_manager,
        "send_topic_text_to_window",
        _unexpected_send_to_window,
    )

    async def _safe_send(_bot, _chat_id, text: str, **_kwargs):
        sent_messages.append(text)

    monkeypatch.setattr(status_polling, "safe_send", _safe_send)
    monkeypatch.setattr(status_polling.random, "choice", lambda _items: "👀")
    monkeypatch.setattr(
        status_polling,
        "emit_telemetry",
        lambda event, **fields: telemetry.append((event, fields)),
    )

    await status_polling._emit_due_run_watchdog_checks(
        bot=SimpleNamespace(),
        user_id=1,
        thread_id=10,
        window_id="@1",
    )

    assert len(health_probe_timeouts) == 1
    assert 0.0 < health_probe_timeouts[0] <= 10.0
    assert sent_messages
    assert sent_messages == ["👀"]
    assert telemetry
    assert telemetry[-1][0] == "watchdog.check_fired"
    assert telemetry[-1][1]["auto_retry_reason"] == "active_turn"
    assert telemetry[-1][1]["retry_attempted"] is False


@pytest.mark.asyncio
async def test_emit_due_watchdog_reports_recycled_transport_without_resend(monkeypatch):
    sent_messages: list[str] = []
    telemetry: list[tuple[str, dict[str, object]]] = []
    health_probe_timeouts: list[float] = []
    recovery_uncertainties: list[dict[str, object]] = []

    check = RunWatchCheck(
        user_id=1,
        thread_id=10,
        window_id="@1",
        checkpoint_seconds=30,
        elapsed_seconds=30.0,
        resend_text="do not resend me",
        resend_text_len=16,
        pending_fingerprint="abc",
        auto_retry_allowed=True,
        auto_retry_reason="eligible",
        retry_count=0,
        max_auto_retries=2,
    )

    monkeypatch.setattr(status_polling, "get_interactive_window", lambda _u, _t: None)
    monkeypatch.setattr(status_polling, "get_due_run_checks", lambda **_kwargs: [check])
    monkeypatch.setattr(status_polling.session_manager, "resolve_chat_id", lambda _u, _t: -100)
    monkeypatch.setattr(status_polling.session_manager, "get_display_name", lambda _w: "demo")
    monkeypatch.setattr(
        status_polling.session_manager,
        "get_window_codex_thread_id",
        lambda _w: "thread_1",
    )
    monkeypatch.setattr(
        status_polling.session_manager,
        "get_window_codex_active_turn_id",
        lambda _w: "turn_1",
    )
    monkeypatch.setattr(status_polling.codex_app_server_client, "is_turn_in_progress", lambda _t: True)

    async def _probe_health(*, timeout: float) -> bool:
        health_probe_timeouts.append(timeout)
        return False

    monkeypatch.setattr(
        status_polling.codex_app_server_client,
        "probe_health",
        _probe_health,
        raising=False,
    )
    monkeypatch.setattr(
        status_polling,
        "note_transport_reset_uncertainty",
        lambda **kwargs: recovery_uncertainties.append(kwargs),
        raising=False,
    )

    def _unexpected_retry_attempt(**_kwargs):
        raise AssertionError("transport recovery must not replay the user's prompt")

    async def _unexpected_send_to_window(*_args, **_kwargs):
        raise AssertionError("transport recovery must not replay the user's prompt")

    monkeypatch.setattr(status_polling, "note_auto_retry_attempt", _unexpected_retry_attempt)
    monkeypatch.setattr(
        status_polling.session_manager,
        "send_topic_text_to_window",
        _unexpected_send_to_window,
    )

    async def _safe_send(_bot, _chat_id, text: str, **_kwargs):
        sent_messages.append(text)

    monkeypatch.setattr(status_polling, "safe_send", _safe_send)
    monkeypatch.setattr(
        status_polling,
        "emit_telemetry",
        lambda event, **fields: telemetry.append((event, fields)),
    )

    await status_polling._emit_due_run_watchdog_checks(
        bot=SimpleNamespace(),
        user_id=1,
        thread_id=10,
        window_id="@1",
    )

    assert len(health_probe_timeouts) == 1
    assert 0.0 < health_probe_timeouts[0] <= 10.0
    assert len(recovery_uncertainties) == 1
    assert recovery_uncertainties[0]["reason"] == "health_probe_recycled"
    assert len(sent_messages) == 1
    report = sent_messages[0].lower()
    assert "run watchdog check" in report
    assert "recycled" in report
    assert "interrupted" in report
    assert telemetry
    assert telemetry[-1][0] == "watchdog.check_fired"
    assert telemetry[-1][1]["auto_retry_reason"] == "transport_recycled"
    assert telemetry[-1][1]["retry_attempted"] is False
    assert telemetry[-1][1]["notification_kind"] == "transport_recycled"


@pytest.mark.asyncio
async def test_emit_due_watchdog_does_not_label_protocol_error_as_recycle(
    monkeypatch,
):
    sent_messages: list[str] = []
    telemetry: list[tuple[str, dict[str, object]]] = []
    recovery_uncertainties: list[dict[str, object]] = []
    check = RunWatchCheck(
        user_id=1,
        thread_id=10,
        window_id="@1",
        checkpoint_seconds=30,
        elapsed_seconds=30.0,
        resend_text="do not resend me",
        resend_text_len=16,
        pending_fingerprint="abc",
        auto_retry_allowed=True,
        auto_retry_reason="eligible",
        retry_count=0,
        max_auto_retries=2,
    )

    monkeypatch.setattr(status_polling, "get_interactive_window", lambda _u, _t: None)
    monkeypatch.setattr(status_polling, "get_due_run_checks", lambda **_kwargs: [check])
    monkeypatch.setattr(
        status_polling.session_manager,
        "resolve_chat_id",
        lambda _u, _t: -100,
    )
    monkeypatch.setattr(
        status_polling.session_manager,
        "get_display_name",
        lambda _w: "demo",
    )
    monkeypatch.setattr(
        status_polling.session_manager,
        "get_window_codex_thread_id",
        lambda _w: "thread_1",
    )
    monkeypatch.setattr(
        status_polling.session_manager,
        "get_window_codex_active_turn_id",
        lambda _w: "turn_1",
    )

    async def _probe_health(*, timeout: float) -> bool:
        _ = timeout
        raise RuntimeError("authentication required")

    monkeypatch.setattr(
        status_polling.codex_app_server_client,
        "probe_health",
        _probe_health,
    )
    monkeypatch.setattr(
        status_polling,
        "note_transport_reset_uncertainty",
        lambda **kwargs: recovery_uncertainties.append(kwargs),
    )
    monkeypatch.setattr(
        status_polling,
        "note_auto_retry_attempt",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("active uncertain turn must not be resent")
        ),
    )

    async def _safe_send(_bot, _chat_id, text: str, **_kwargs):
        sent_messages.append(text)

    monkeypatch.setattr(status_polling, "safe_send", _safe_send)
    monkeypatch.setattr(
        status_polling,
        "emit_telemetry",
        lambda event, **fields: telemetry.append((event, fields)),
    )

    await status_polling._emit_due_run_watchdog_checks(
        bot=SimpleNamespace(),
        user_id=1,
        thread_id=10,
        window_id="@1",
    )

    assert recovery_uncertainties == []
    assert "health probe failed" in sent_messages[0].lower()
    assert telemetry[-1][1]["auto_retry_reason"] == "health_probe_failed"
    assert telemetry[-1][1]["notification_kind"] == "watchdog_report"


@pytest.mark.asyncio
async def test_transport_uncertainty_notice_is_one_shot_and_clears_watch(
    monkeypatch,
    isolated_run_watchdog,
):
    sent_messages: list[str] = []
    now = [605.0]

    run_watchdog.note_run_started(
        user_id=1,
        thread_id=12,
        window_id="@reset",
        source="user_input",
        expect_response=True,
        pending_text="run once",
        now=0.0,
    )
    run_watchdog.note_transport_reset_uncertainty(
        window_ids={"@reset"},
        reason="request_timeout:turn/start",
        now=5.0,
    )

    monkeypatch.setattr(run_watchdog.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(status_polling, "get_interactive_window", lambda _u, _t: None)
    monkeypatch.setattr(
        status_polling.session_manager,
        "resolve_chat_id",
        lambda _u, _t: -100,
    )
    monkeypatch.setattr(
        status_polling.session_manager,
        "get_display_name",
        lambda _w: "demo",
    )
    monkeypatch.setattr(
        status_polling.session_manager,
        "get_window_codex_thread_id",
        lambda _w: "",
    )
    monkeypatch.setattr(
        status_polling.session_manager,
        "get_window_codex_active_turn_id",
        lambda _w: "",
    )

    async def _safe_send(_bot, _chat_id, text: str, **_kwargs):
        sent_messages.append(text)
        return SimpleNamespace(message_id=1)

    monkeypatch.setattr(status_polling, "safe_send", _safe_send)

    await status_polling._emit_due_run_watchdog_checks(
        bot=SimpleNamespace(),
        user_id=1,
        thread_id=12,
        window_id="@reset",
    )
    now[0] = 1205.0
    await status_polling._emit_due_run_watchdog_checks(
        bot=SimpleNamespace(),
        user_id=1,
        thread_id=12,
        window_id="@reset",
    )

    assert len(sent_messages) == 1
    assert "interrupted" in sent_messages[0].lower()
    assert "send a new message to resume" in sent_messages[0].lower()
    assert run_watchdog.get_immediate_auto_retry_candidate(
        user_id=1,
        thread_id=12,
        window_id="@reset",
        now=1205.0,
    ) is None


@pytest.mark.asyncio
async def test_transport_uncertainty_notice_does_not_clear_replacement_watch(
    monkeypatch,
    isolated_run_watchdog,
):
    run_watchdog.note_run_started(
        user_id=1,
        thread_id=12,
        window_id="@reset",
        source="user_input",
        expect_response=True,
        pending_text="run once",
        now=0.0,
    )
    run_watchdog.note_transport_reset_uncertainty(
        window_ids={"@reset"},
        reason="request_timeout:turn/start",
        now=5.0,
    )

    monkeypatch.setattr(run_watchdog.time, "monotonic", lambda: 35.0)
    monkeypatch.setattr(status_polling, "get_interactive_window", lambda _u, _t: None)
    monkeypatch.setattr(
        status_polling.session_manager,
        "resolve_chat_id",
        lambda _u, _t: -100,
    )
    monkeypatch.setattr(
        status_polling.session_manager,
        "get_display_name",
        lambda _w: "demo",
    )
    monkeypatch.setattr(
        status_polling.session_manager,
        "get_window_codex_thread_id",
        lambda _w: "",
    )
    monkeypatch.setattr(
        status_polling.session_manager,
        "get_window_codex_active_turn_id",
        lambda _w: "",
    )

    async def _safe_send(*_args, **_kwargs):
        # Simulate the user sending the same text again while Telegram delivery
        # of the prior interruption notice is still in flight.
        run_watchdog.note_run_started(
            user_id=1,
            thread_id=12,
            window_id="@reset",
            source="user_input",
            expect_response=True,
            pending_text="run once",
            now=36.0,
        )
        return SimpleNamespace(message_id=2)

    monkeypatch.setattr(status_polling, "safe_send", _safe_send)

    await status_polling._emit_due_run_watchdog_checks(
        bot=SimpleNamespace(),
        user_id=1,
        thread_id=12,
        window_id="@reset",
    )

    candidate = run_watchdog.get_immediate_auto_retry_candidate(
        user_id=1,
        thread_id=12,
        window_id="@reset",
        now=36.0,
    )
    assert candidate is not None
    assert candidate.auto_retry_reason == "eligible"


@pytest.mark.asyncio
async def test_transport_uncertainty_safe_send_none_retries_on_next_poll(
    monkeypatch,
    isolated_run_watchdog,
):
    run_watchdog.note_run_started(
        user_id=1,
        thread_id=12,
        window_id="@reset",
        source="user_input",
        expect_response=True,
        pending_text="run once",
        now=0.0,
    )
    run_watchdog.note_transport_reset_uncertainty(
        window_ids={"@reset"},
        reason="request_timeout:turn/start",
        now=5.0,
    )

    monkeypatch.setattr(run_watchdog.time, "monotonic", lambda: 1805.0)
    monkeypatch.setattr(status_polling, "get_interactive_window", lambda _u, _t: None)
    monkeypatch.setattr(
        status_polling.session_manager,
        "resolve_chat_id",
        lambda _u, _t: -100,
    )
    monkeypatch.setattr(
        status_polling.session_manager,
        "get_display_name",
        lambda _w: "demo",
    )
    monkeypatch.setattr(
        status_polling.session_manager,
        "get_window_codex_thread_id",
        lambda _w: "",
    )
    monkeypatch.setattr(
        status_polling.session_manager,
        "get_window_codex_active_turn_id",
        lambda _w: "",
    )

    send_attempts = 0

    async def _safe_send(*_args, **_kwargs):
        nonlocal send_attempts
        send_attempts += 1
        if send_attempts == 1:
            return None
        return SimpleNamespace(message_id=3)

    monkeypatch.setattr(status_polling, "safe_send", _safe_send)

    await status_polling._emit_due_run_watchdog_checks(
        bot=SimpleNamespace(),
        user_id=1,
        thread_id=12,
        window_id="@reset",
    )
    await status_polling._emit_due_run_watchdog_checks(
        bot=SimpleNamespace(),
        user_id=1,
        thread_id=12,
        window_id="@reset",
    )

    assert send_attempts == 2
    assert run_watchdog.get_immediate_auto_retry_candidate(
        user_id=1,
        thread_id=12,
        window_id="@reset",
        now=1805.0,
    ) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "send_error",
    [
        pytest.param(NetworkError("telegram unavailable"), id="network-error"),
        pytest.param(RetryAfter(timedelta(seconds=1)), id="retry-after"),
    ],
)
async def test_transport_uncertainty_send_failure_retries_on_next_poll(
    monkeypatch,
    isolated_run_watchdog,
    send_error,
):
    run_watchdog.note_run_started(
        user_id=1,
        thread_id=12,
        window_id="@reset",
        source="user_input",
        expect_response=True,
        pending_text="run once",
        now=0.0,
    )
    run_watchdog.note_transport_reset_uncertainty(
        window_ids={"@reset"},
        reason="request_timeout:turn/start",
        now=5.0,
    )

    monkeypatch.setattr(run_watchdog.time, "monotonic", lambda: 1805.0)
    monkeypatch.setattr(status_polling, "get_interactive_window", lambda _u, _t: None)
    monkeypatch.setattr(
        status_polling.session_manager,
        "resolve_chat_id",
        lambda _u, _t: -100,
    )
    monkeypatch.setattr(
        status_polling.session_manager,
        "get_display_name",
        lambda _w: "demo",
    )
    monkeypatch.setattr(
        status_polling.session_manager,
        "get_window_codex_thread_id",
        lambda _w: "",
    )
    monkeypatch.setattr(
        status_polling.session_manager,
        "get_window_codex_active_turn_id",
        lambda _w: "",
    )

    send_attempts = 0

    async def _safe_send(*_args, **_kwargs):
        nonlocal send_attempts
        send_attempts += 1
        if send_attempts == 1:
            raise send_error
        return SimpleNamespace(message_id=4)

    monkeypatch.setattr(status_polling, "safe_send", _safe_send)

    with pytest.raises(type(send_error)):
        await status_polling._emit_due_run_watchdog_checks(
            bot=SimpleNamespace(),
            user_id=1,
            thread_id=12,
            window_id="@reset",
        )
    await status_polling._emit_due_run_watchdog_checks(
        bot=SimpleNamespace(),
        user_id=1,
        thread_id=12,
        window_id="@reset",
    )

    assert send_attempts == 2
    assert run_watchdog.get_immediate_auto_retry_candidate(
        user_id=1,
        thread_id=12,
        window_id="@reset",
        now=1805.0,
    ) is None


@pytest.mark.asyncio
async def test_emit_due_watchdog_records_retry_result_on_success(monkeypatch):
    sent_messages: list[str] = []
    recorded_results: list[bool] = []
    telemetry: list[tuple[str, dict[str, object]]] = []

    check = RunWatchCheck(
        user_id=1,
        thread_id=11,
        window_id="@2",
        checkpoint_seconds=30,
        elapsed_seconds=30.0,
        resend_text="retry me",
        resend_text_len=8,
        pending_fingerprint="abc",
        auto_retry_allowed=True,
        auto_retry_reason="eligible",
        retry_count=0,
        max_auto_retries=2,
    )

    monkeypatch.setattr(status_polling, "get_interactive_window", lambda _u, _t: None)
    monkeypatch.setattr(status_polling, "get_due_run_checks", lambda **_kwargs: [check])
    monkeypatch.setattr(status_polling.session_manager, "resolve_chat_id", lambda _u, _t: -100)
    monkeypatch.setattr(status_polling.session_manager, "get_display_name", lambda _w: "demo")
    monkeypatch.setattr(
        status_polling.session_manager,
        "get_window_codex_thread_id",
        lambda _w: "",
    )
    monkeypatch.setattr(
        status_polling.session_manager,
        "get_window_codex_active_turn_id",
        lambda _w: "",
    )
    monkeypatch.setattr(status_polling, "note_auto_retry_attempt", lambda **_kwargs: (1, 2))

    async def _send_to_window(
        *,
        user_id: int,
        thread_id: int | None,
        window_id: str,
        text: str,
        steer: bool = False,
    ):
        _ = user_id, thread_id, window_id, text, steer
        return True, ""

    monkeypatch.setattr(
        status_polling.session_manager,
        "send_topic_text_to_window",
        _send_to_window,
    )

    def _note_retry_result(**kwargs):
        recorded_results.append(bool(kwargs.get("send_success")))

    monkeypatch.setattr(status_polling, "note_auto_retry_result", _note_retry_result)

    async def _safe_send(_bot, _chat_id, text: str, **_kwargs):
        sent_messages.append(text)

    monkeypatch.setattr(status_polling, "safe_send", _safe_send)
    monkeypatch.setattr(
        status_polling,
        "emit_telemetry",
        lambda event, **fields: telemetry.append((event, fields)),
    )

    await status_polling._emit_due_run_watchdog_checks(
        bot=SimpleNamespace(),
        user_id=1,
        thread_id=11,
        window_id="@2",
    )

    assert recorded_results == [True]
    assert sent_messages
    assert "auto-retry sent (1/2)" in sent_messages[0]
    assert telemetry
    assert telemetry[-1][0] == "watchdog.check_fired"
    assert telemetry[-1][1]["retry_attempted"] is True
    assert telemetry[-1][1]["resend_ok"] is True
    assert telemetry[-1][1]["resend_err"] == ""


@pytest.mark.asyncio
async def test_update_status_message_app_server_mode_skips_legacy_polling(monkeypatch):
    cleared: list[tuple[int, int | None]] = []

    monkeypatch.setattr(status_polling.config, "session_provider", "codex")
    monkeypatch.setattr(status_polling.config, "codex_transport", "app_server")
    monkeypatch.setattr(status_polling, "get_interactive_window", lambda _u, _t: "@1")

    async def _clear_interactive_msg(user_id: int, _bot, thread_id: int | None = None):
        cleared.append((user_id, thread_id))

    monkeypatch.setattr(status_polling, "clear_interactive_msg", _clear_interactive_msg)

    await status_polling.update_status_message(
        bot=SimpleNamespace(),
        user_id=1,
        window_id="@1",
        thread_id=10,
    )

    assert cleared == [(1, 10)]


@pytest.mark.asyncio
async def test_status_poll_loop_app_server_only_runs_watchdog_without_legacy(monkeypatch):
    events: list[str] = []

    monkeypatch.setattr(status_polling.config, "session_provider", "codex")
    monkeypatch.setattr(status_polling.config, "runtime_mode", "app_server_only")
    monkeypatch.setattr(
        status_polling.session_manager,
        "iter_topic_window_bindings",
        lambda: [(1, 10, "@900000")],
    )
    monkeypatch.setattr(status_polling, "prune_run_watch_topics", lambda _topics: None)
    monkeypatch.setattr(status_polling, "prune_looper_topics", lambda _topics: None)
    async def _no_pending(_uid: int) -> set[int]:
        return set()
    monkeypatch.setattr(status_polling, "get_pending_delivery_topics", _no_pending)
    monkeypatch.setattr(
        status_polling.time,
        "monotonic",
        lambda: 0.0,
    )
    monkeypatch.setattr(status_polling, "TOPIC_CHECK_INTERVAL", 60.0)

    async def _unexpected_find_window_by_id(_wid: str):
        raise AssertionError("legacy window lookup should not run in app_server_only poll loop")


    async def _unexpected_update_status(*_args, **_kwargs):
        raise AssertionError("status pane polling should not run in app_server_only")

    monkeypatch.setattr(status_polling, "update_status_message", _unexpected_update_status)

    async def _emit_watchdog(_bot, *, user_id: int, thread_id: int | None, window_id: str):
        _ = user_id, thread_id
        events.append(f"watchdog:{window_id}")

    async def _emit_looper(_bot, *, user_id: int, thread_id: int | None, window_id: str):
        _ = user_id, thread_id
        events.append(f"looper:{window_id}")

    async def _emit_personality(_bot, *, user_id: int, thread_id: int | None, window_id: str):
        _ = user_id, thread_id
        events.append(f"personality:{window_id}")

    async def _emit_autoresearch(_bot, *, user_id: int, thread_id: int | None, window_id: str):
        _ = user_id, thread_id
        events.append(f"autoresearch:{window_id}")

    monkeypatch.setattr(status_polling, "_emit_due_run_watchdog_checks", _emit_watchdog)
    monkeypatch.setattr(status_polling, "_emit_due_looper_prompt", _emit_looper)
    monkeypatch.setattr(status_polling, "_emit_due_personality_delivery", _emit_personality)
    monkeypatch.setattr(status_polling, "_emit_due_autoresearch_delivery", _emit_autoresearch)

    async def _cancel_sleep(_seconds: float):
        raise asyncio.CancelledError

    monkeypatch.setattr(status_polling.asyncio, "sleep", _cancel_sleep)

    class _Bot:
        async def unpin_all_forum_topic_messages(self, **_kwargs):
            events.append("probe")

    with pytest.raises(asyncio.CancelledError):
        await status_polling.status_poll_loop(_Bot())

    assert "probe" not in events
    assert "watchdog:@900000" in events
    assert "looper:@900000" in events
    assert "personality:@900000" in events
    assert "autoresearch:@900000" in events


@pytest.mark.asyncio
async def test_status_poll_loop_topic_deleted_in_app_server_only_skips_legacy_kill(monkeypatch):
    unbound: list[tuple[int, int | None]] = []
    cleared: list[tuple[int, int | None]] = []

    monkeypatch.setattr(status_polling.config, "session_provider", "codex")
    monkeypatch.setattr(status_polling.config, "runtime_mode", "app_server_only")
    monkeypatch.setattr(
        status_polling.session_manager,
        "iter_topic_window_bindings",
        lambda: [(1, 10, "@900000")],
    )
    monkeypatch.setattr(status_polling, "prune_run_watch_topics", lambda _topics: None)
    monkeypatch.setattr(status_polling, "prune_looper_topics", lambda _topics: None)
    async def _no_pending(_uid: int) -> set[int]:
        return set()
    monkeypatch.setattr(status_polling, "get_pending_delivery_topics", _no_pending)
    monkeypatch.setattr(status_polling, "TOPIC_CHECK_INTERVAL", 0.0)
    monkeypatch.setattr(status_polling.time, "monotonic", lambda: 1.0)
    monkeypatch.setattr(status_polling.session_manager, "resolve_chat_id", lambda _u, _t: -100)
    monkeypatch.setattr(
        status_polling.session_manager,
        "unbind_thread",
        lambda user_id, thread_id: unbound.append((user_id, thread_id)),
    )
    monkeypatch.setattr(
        status_polling,
        "clear_topic_state",
        lambda user_id, thread_id, _bot: cleared.append((user_id, thread_id)),
    )

    async def _unexpected_find_window_by_id(_wid: str):
        raise AssertionError("legacy lookup should not run for topic cleanup in app_server_only")

    async def _unexpected_kill_window(_wid: str):
        raise AssertionError("legacy kill should not run for topic cleanup in app_server_only")

    monkeypatch.setattr(status_polling, "update_status_message", lambda *_args, **_kwargs: None)

    async def _emit_watchdog(*_args, **_kwargs):
        return None

    async def _emit_looper(*_args, **_kwargs):
        return None

    async def _emit_personality(*_args, **_kwargs):
        return None

    async def _emit_autoresearch(*_args, **_kwargs):
        return None

    monkeypatch.setattr(status_polling, "_emit_due_run_watchdog_checks", _emit_watchdog)
    monkeypatch.setattr(status_polling, "_emit_due_looper_prompt", _emit_looper)
    monkeypatch.setattr(status_polling, "_emit_due_personality_delivery", _emit_personality)
    monkeypatch.setattr(status_polling, "_emit_due_autoresearch_delivery", _emit_autoresearch)

    async def _cancel_sleep(_seconds: float):
        raise asyncio.CancelledError

    monkeypatch.setattr(status_polling.asyncio, "sleep", _cancel_sleep)

    class _Bot:
        async def unpin_all_forum_topic_messages(self, **_kwargs):
            raise BadRequest("Topic_id_invalid")

    with pytest.raises(asyncio.CancelledError):
        await status_polling.status_poll_loop(_Bot())

    assert unbound == [(1, 10)]
    assert cleared == [(1, 10)]


@pytest.mark.asyncio
async def test_topic_probe_dedupes_permission_failures_and_backs_off(monkeypatch):
    calls: list[tuple[int, int | None]] = []

    monkeypatch.setattr(status_polling.session_manager, "resolve_chat_id", lambda *_args, **_kwargs: -100)
    monkeypatch.setattr(status_polling, "TOPIC_PROBE_PERMISSION_BACKOFF", 30.0, raising=False)
    if hasattr(status_polling, "_topic_probe_retry_after"):
        status_polling._topic_probe_retry_after.clear()

    class _Bot:
        async def unpin_all_forum_topic_messages(self, *, chat_id: int, message_thread_id: int | None):
            calls.append((chat_id, message_thread_id))
            raise BadRequest("Not enough rights to manage pinned messages in the chat")

    bindings = [
        (1, None, 10, "@same-a"),
        (1, -100, 10, "@same-b"),
    ]

    await status_polling._probe_topic_bindings(_Bot(), bindings, now=100.0)
    await status_polling._probe_topic_bindings(_Bot(), bindings, now=120.0)
    await status_polling._probe_topic_bindings(_Bot(), bindings, now=131.0)

    assert calls == [(-100, 10), (-100, 10)]


@pytest.mark.asyncio
async def test_topic_probe_prunes_retry_backoff_for_inactive_topics(monkeypatch):
    monkeypatch.setattr(status_polling.session_manager, "resolve_chat_id", lambda *_args, **_kwargs: -100)
    status_polling._topic_probe_retry_after.clear()
    status_polling._topic_probe_retry_after[(-100, 99)] = 999.0

    class _Bot:
        async def unpin_all_forum_topic_messages(self, *, chat_id: int, message_thread_id: int | None):
            _ = chat_id, message_thread_id

    await status_polling._probe_topic_bindings(
        _Bot(),
        [(1, -100, 10, "@active")],
        now=100.0,
    )

    assert (-100, 99) not in status_polling._topic_probe_retry_after


@pytest.mark.asyncio
async def test_topic_probe_skips_binding_when_chat_resolution_fails(monkeypatch):
    calls: list[tuple[int, int | None]] = []
    status_polling._topic_probe_retry_after.clear()

    def _resolve_chat_id(_user_id: int, thread_id: int | None, **_kwargs):
        if thread_id == 10:
            raise RuntimeError("bad binding")
        return -100

    class _Bot:
        async def unpin_all_forum_topic_messages(self, *, chat_id: int, message_thread_id: int | None):
            calls.append((chat_id, message_thread_id))

    monkeypatch.setattr(status_polling.session_manager, "resolve_chat_id", _resolve_chat_id)

    await status_polling._probe_topic_bindings(
        _Bot(),
        [
            (1, None, 10, "@bad"),
            (1, None, 11, "@valid"),
        ],
        now=100.0,
    )

    assert calls == [(-100, 11)]


@pytest.mark.asyncio
async def test_status_poll_loop_skips_only_busy_topic_not_all_user_topics(monkeypatch):
    events: list[str] = []

    monkeypatch.setattr(status_polling.config, "session_provider", "codex")
    monkeypatch.setattr(status_polling.config, "runtime_mode", "app_server_only")
    monkeypatch.setattr(
        status_polling.session_manager,
        "iter_topic_window_bindings",
        lambda: [(1, 10, "@busy"), (1, 11, "@idle")],
    )
    monkeypatch.setattr(status_polling, "prune_run_watch_topics", lambda _topics: None)
    monkeypatch.setattr(status_polling, "prune_looper_topics", lambda _topics: None)
    monkeypatch.setattr(status_polling, "TOPIC_CHECK_INTERVAL", 60.0)
    monkeypatch.setattr(status_polling.time, "monotonic", lambda: 0.0)

    pending_calls: list[int] = []
    async def _get_pending_delivery_topics(uid: int) -> set[int]:
        pending_calls.append(uid)
        return {10}

    monkeypatch.setattr(
        status_polling,
        "get_pending_delivery_topics",
        _get_pending_delivery_topics,
    )

    async def _emit_watchdog(_bot, *, user_id: int, thread_id: int | None, window_id: str):
        _ = user_id, thread_id
        events.append(f"watchdog:{window_id}")

    async def _emit_looper(_bot, *, user_id: int, thread_id: int | None, window_id: str):
        _ = user_id, thread_id
        events.append(f"looper:{window_id}")

    async def _emit_personality(_bot, *, user_id: int, thread_id: int | None, window_id: str):
        _ = user_id, thread_id
        events.append(f"personality:{window_id}")

    async def _emit_autoresearch(_bot, *, user_id: int, thread_id: int | None, window_id: str):
        _ = user_id, thread_id
        events.append(f"autoresearch:{window_id}")

    monkeypatch.setattr(status_polling, "_emit_due_run_watchdog_checks", _emit_watchdog)
    monkeypatch.setattr(status_polling, "_emit_due_looper_prompt", _emit_looper)
    monkeypatch.setattr(status_polling, "_emit_due_personality_delivery", _emit_personality)
    monkeypatch.setattr(status_polling, "_emit_due_autoresearch_delivery", _emit_autoresearch)

    async def _cancel_sleep(_seconds: float):
        raise asyncio.CancelledError

    monkeypatch.setattr(status_polling.asyncio, "sleep", _cancel_sleep)

    class _Bot:
        async def unpin_all_forum_topic_messages(self, **_kwargs):
            raise AssertionError("topic probe should not run")

    with pytest.raises(asyncio.CancelledError):
        await status_polling.status_poll_loop(_Bot())

    assert all("@busy" not in event for event in events)
    assert "watchdog:@idle" in events
    assert "looper:@idle" in events
    assert "personality:@idle" in events
    assert "autoresearch:@idle" in events
    assert pending_calls == [1]


@pytest.mark.asyncio
async def test_status_poll_loop_checks_transport_interruption_for_busy_topic(
    monkeypatch,
    isolated_run_watchdog,
):
    events: list[str] = []

    run_watchdog.note_run_started(
        user_id=1,
        thread_id=10,
        window_id="@reset",
        source="user_input",
        expect_response=True,
        pending_text="run once",
        now=0.0,
    )
    run_watchdog.note_transport_reset_uncertainty(
        window_ids={"@reset"},
        reason="request_timeout:turn/start",
        now=5.0,
    )

    monkeypatch.setattr(status_polling.config, "session_provider", "codex")
    monkeypatch.setattr(status_polling.config, "runtime_mode", "app_server_only")
    monkeypatch.setattr(
        status_polling.session_manager,
        "iter_topic_window_bindings",
        lambda: [(1, 10, "@reset")],
    )
    monkeypatch.setattr(status_polling, "prune_run_watch_topics", lambda _topics: None)
    monkeypatch.setattr(status_polling, "prune_looper_topics", lambda _topics: None)
    monkeypatch.setattr(status_polling, "TOPIC_CHECK_INTERVAL", 60.0)
    monkeypatch.setattr(status_polling.time, "monotonic", lambda: 35.0)

    async def _no_op_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(status_polling, "_probe_stale_nodes", _no_op_async)
    monkeypatch.setattr(status_polling, "_emit_node_status_notifications", _no_op_async)
    monkeypatch.setattr(
        status_polling,
        "_emit_due_resource_monitor_notifications",
        _no_op_async,
    )
    monkeypatch.setattr(
        status_polling,
        "_emit_due_codex_account_notifications",
        _no_op_async,
    )

    async def _get_pending_delivery_topics(_uid: int) -> set[int]:
        return {10}

    monkeypatch.setattr(
        status_polling,
        "get_pending_delivery_topics",
        _get_pending_delivery_topics,
    )

    async def _emit_watchdog(
        _bot,
        *,
        user_id: int,
        thread_id: int | None,
        window_id: str,
    ):
        _ = user_id, thread_id
        events.append(f"watchdog:{window_id}")

    async def _emit_background(*_args, **_kwargs):
        events.append("background")

    monkeypatch.setattr(status_polling, "_emit_due_run_watchdog_checks", _emit_watchdog)
    monkeypatch.setattr(status_polling, "_emit_due_looper_prompt", _emit_background)
    monkeypatch.setattr(status_polling, "_emit_due_personality_delivery", _emit_background)
    monkeypatch.setattr(status_polling, "_emit_due_autoresearch_delivery", _emit_background)

    async def _cancel_sleep(_seconds: float):
        raise asyncio.CancelledError

    monkeypatch.setattr(status_polling.asyncio, "sleep", _cancel_sleep)

    class _Bot:
        async def unpin_all_forum_topic_messages(self, **_kwargs):
            raise AssertionError("topic probe should not run")

    with pytest.raises(asyncio.CancelledError):
        await status_polling.status_poll_loop(_Bot())

    assert events == ["watchdog:@reset"]
