from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from coco import mobile_access
from coco.session import TopicSendDispatchState


REQUEST_ID = "123e4567-e89b-12d3-a456-426614174000"
OWNER_ID = 1147817421
CHAT_ID = -1003841129251
THREAD_ID = 3413


def _write_target(
    tmp_path: Path,
    *,
    workspace: str | None = None,
    db_path: Path | None = None,
    user_id: object = OWNER_ID,
    chat_id: object = CHAT_ID,
    thread_id: object = THREAD_ID,
    raw: object | None = None,
    mode: int = 0o600,
) -> Path:
    target_path = tmp_path / "approval-target.json"
    if raw is None:
        raw = {
            "user_id": user_id,
            "chat_id": chat_id,
            "thread_id": thread_id,
            "workspace": workspace or str(tmp_path / "workspace"),
            "db_path": str(db_path or tmp_path / "access.sqlite3"),
        }
        target_path.write_text(json.dumps(raw), encoding="utf-8")
    else:
        target_path.write_text(str(raw), encoding="utf-8")
    target_path.chmod(mode)
    return target_path


def _write_db(
    tmp_path: Path,
    *,
    request_id: str = REQUEST_ID,
    status: str = "pending",
    expires_at: float = 9999999999.0,
) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "access.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE pending_requests (
                request_id TEXT PRIMARY KEY,
                agent TEXT NOT NULL,
                purpose TEXT NOT NULL,
                created_at REAL NOT NULL,
                status TEXT NOT NULL,
                decided_at REAL,
                expires_at REAL NOT NULL
            )
            """
        )
        if request_id:
            connection.execute(
                "INSERT INTO pending_requests VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    request_id,
                    "deployment-check-agent <do-not-copy>",
                    "read-only ZIP; <untrusted-purpose>",
                    1.0,
                    status,
                    None,
                    expires_at,
                ),
            )
    return db_path


def _runtime(
    *,
    workspace: str,
    db_path: Path,
    owner_allowed: bool = True,
    busy: bool = False,
    send_result: tuple[bool, str] = (True, ""),
    ownership_cwd: str | None = None,
    dispatch_started: bool = False,
) -> tuple[SimpleNamespace, dict[str, object], list[dict[str, object]]]:
    ownership = SimpleNamespace(
        window_id="@mobile",
        codex_thread_id="codex-thread",
        machine_id="local",
        cwd=ownership_cwd or workspace,
    )
    session = SimpleNamespace()
    session.get_window_for_thread = lambda *_args, **_kwargs: "@mobile"
    session.is_codex_uncertain_send_result = lambda value: "uncertain" in str(value)
    session.is_codex_active_writer_error = lambda value: "active writer" in str(value)

    async def send_topic_text_to_window(**kwargs):
        if dispatch_started:
            kwargs["dispatch_state"].transport_dispatch_started = True
        sent.append(kwargs)
        return send_result

    sent: list[dict[str, object]] = []
    session.send_topic_text_to_window = send_topic_text_to_window
    started: list[dict[str, object]] = []
    telemetry: list[dict[str, object]] = []
    runtime = SimpleNamespace(
        config=SimpleNamespace(allowed_users={OWNER_ID} if owner_allowed else set()),
        session_manager=session,
        capture_topic_ownership=lambda *_args, **_kwargs: ownership,
        _is_window_in_progress=lambda *_args, **_kwargs: _async_value(busy),
        TopicSendDispatchState=TopicSendDispatchState,
        note_run_started=lambda **kwargs: started.append(kwargs),
        emit_telemetry=lambda event, **kwargs: telemetry.append(
            {"event": event, **kwargs}
        ),
    )
    runtime._started = started
    runtime._telemetry = telemetry
    runtime._sent = sent
    return runtime, {"workspace": workspace, "db_path": db_path}, {"sent": sent}


async def _async_value(value: object) -> object:
    return value


async def _enqueue(runtime: SimpleNamespace, request_id: object = REQUEST_ID, **extra):
    params = {"request_id": request_id, **extra}
    return await mobile_access.handle_mobile_access_enqueue(params, runtime=runtime)


@pytest.mark.asyncio
async def test_mobile_access_is_disabled_when_target_config_is_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("COCO_MOBILE_ACCESS_APPROVAL_CONFIG", raising=False)
    db_path = _write_db(tmp_path)
    runtime, _target, _state = _runtime(workspace=str(tmp_path / "workspace"), db_path=db_path)

    result = await _enqueue(runtime)

    assert result == {"accepted": False, "status": "disabled"}
    assert runtime._sent == []


@pytest.mark.asyncio
async def test_mobile_access_rejects_malformed_target_config(tmp_path, monkeypatch):
    config_path = _write_target(tmp_path, raw='{"user_id": 1}')
    monkeypatch.setenv("COCO_MOBILE_ACCESS_APPROVAL_CONFIG", str(config_path))
    db_path = _write_db(tmp_path)
    runtime, _target, _state = _runtime(workspace=str(tmp_path / "workspace"), db_path=db_path)

    result = await _enqueue(runtime)

    assert result == {"accepted": False, "status": "invalid_config"}
    assert runtime._sent == []


@pytest.mark.asyncio
async def test_mobile_access_rejects_noncanonical_uuid_and_caller_target_fields(
    tmp_path, monkeypatch
):
    db_path = _write_db(tmp_path)
    workspace = str(tmp_path / "workspace")
    config_path = _write_target(tmp_path, workspace=workspace, db_path=db_path)
    monkeypatch.setenv("COCO_MOBILE_ACCESS_APPROVAL_CONFIG", str(config_path))
    runtime, _target, _state = _runtime(workspace=workspace, db_path=db_path)

    result = await _enqueue(
        runtime,
        REQUEST_ID.upper(),
        user_id=999,
        chat_id=123,
        thread_id=456,
        workspace="/caller/chosen/workspace",
    )

    assert result == {"accepted": False, "status": "invalid_request"}
    assert runtime._sent == []
    assert str(UUID(REQUEST_ID)) == REQUEST_ID


@pytest.mark.asyncio
async def test_mobile_access_rejects_owner_not_allowed_or_live_binding_mismatch(
    tmp_path, monkeypatch
):
    db_path = _write_db(tmp_path)
    workspace = str(tmp_path / "workspace")
    config_path = _write_target(tmp_path, workspace=workspace, db_path=db_path)
    monkeypatch.setenv("COCO_MOBILE_ACCESS_APPROVAL_CONFIG", str(config_path))

    runtime, _target, _state = _runtime(
        workspace=workspace,
        db_path=db_path,
        owner_allowed=False,
    )
    assert await _enqueue(runtime) == {
        "accepted": False,
        "status": "target_unavailable",
    }

    runtime, _target, _state = _runtime(
        workspace=workspace,
        db_path=db_path,
        ownership_cwd=str(tmp_path / "different-workspace"),
    )
    assert await _enqueue(runtime) == {
        "accepted": False,
        "status": "target_unavailable",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["approved", "denied", "expired"])
async def test_mobile_access_requires_a_pending_row(tmp_path, monkeypatch, status):
    workspace = str(tmp_path / "workspace")
    db_path = _write_db(tmp_path, status=status)
    config_path = _write_target(tmp_path, workspace=workspace, db_path=db_path)
    monkeypatch.setenv("COCO_MOBILE_ACCESS_APPROVAL_CONFIG", str(config_path))
    runtime, _target, _state = _runtime(workspace=workspace, db_path=db_path)

    assert await _enqueue(runtime) == {
        "accepted": False,
        "status": "not_pending",
    }

    expired_db = _write_db(tmp_path / "expired", expires_at=1.0)
    expired_config = _write_target(
        tmp_path / "expired", workspace=workspace, db_path=expired_db
    )
    monkeypatch.setenv("COCO_MOBILE_ACCESS_APPROVAL_CONFIG", str(expired_config))
    assert await _enqueue(runtime) == {
        "accepted": False,
        "status": "not_pending",
    }

    missing_db = _write_db(tmp_path / "missing", request_id="")
    missing_config = _write_target(
        tmp_path / "missing", workspace=workspace, db_path=missing_db
    )
    monkeypatch.setenv("COCO_MOBILE_ACCESS_APPROVAL_CONFIG", str(missing_config))
    assert await _enqueue(runtime) == {
        "accepted": False,
        "status": "not_pending",
    }


@pytest.mark.asyncio
async def test_mobile_access_reports_busy_without_dispatch(tmp_path, monkeypatch):
    workspace = str(tmp_path / "workspace")
    db_path = _write_db(tmp_path)
    config_path = _write_target(tmp_path, workspace=workspace, db_path=db_path)
    monkeypatch.setenv("COCO_MOBILE_ACCESS_APPROVAL_CONFIG", str(config_path))
    runtime, _target, _state = _runtime(workspace=workspace, db_path=db_path, busy=True)

    assert await _enqueue(runtime) == {
        "accepted": False,
        "status": "busy",
        "retry_after": 30,
    }
    assert runtime._sent == []


@pytest.mark.asyncio
async def test_mobile_access_starts_browser_link_prompt_without_untrusted_metadata(
    tmp_path, monkeypatch
):
    workspace = str(tmp_path / "workspace")
    db_path = _write_db(tmp_path)
    config_path = _write_target(tmp_path, workspace=workspace, db_path=db_path)
    monkeypatch.setenv("COCO_MOBILE_ACCESS_APPROVAL_CONFIG", str(config_path))
    runtime, _target, _state = _runtime(workspace=workspace, db_path=db_path)

    result = await _enqueue(runtime)

    assert result == {"accepted": True, "status": "started"}
    prompt = str(runtime._sent[0]["text"])
    assert REQUEST_ID in prompt
    assert "python3 /home/pcopu/.local/share/sabbath-mobile-downloads/api/service.py" in prompt
    assert "--db /home/pcopu/.local/share/sabbath-mobile-downloads/state/access.sqlite3 --pending" in prompt
    assert "--approval-link " + REQUEST_ID in prompt
    assert "--approve " + REQUEST_ID not in prompt
    assert "--deny " + REQUEST_ID in prompt
    assert "approval_url" in prompt
    assert "one-time" in prompt.lower()
    assert "60 minutes" in prompt
    assert "15 minutes" in prompt
    assert "GET preview" in prompt
    assert "submit the approval form" in prompt
    assert "grant the session locally" in prompt
    assert "paste the resulting" not in prompt
    assert "deployment" in prompt.lower()
    assert "deployment-check-agent" not in prompt
    assert "<untrusted-purpose>" not in prompt
    assert runtime._sent[0]["force_new_turn"] is True
    assert runtime._sent[0]["topic_ownership"] is not None
    assert runtime._started[0]["source"] == "mobile_access"
    assert runtime._telemetry[0]["source"] == "mobile_access"


@pytest.mark.asyncio
async def test_mobile_access_reports_uncertain_after_dispatch(tmp_path, monkeypatch):
    workspace = str(tmp_path / "workspace")
    db_path = _write_db(tmp_path)
    config_path = _write_target(tmp_path, workspace=workspace, db_path=db_path)
    monkeypatch.setenv("COCO_MOBILE_ACCESS_APPROVAL_CONFIG", str(config_path))
    runtime, _target, _state = _runtime(
        workspace=workspace,
        db_path=db_path,
        send_result=(False, "transport failed"),
        dispatch_started=True,
    )

    assert await _enqueue(runtime) == {
        "accepted": True,
        "status": "uncertain",
    }
    assert runtime._started == []


@pytest.mark.asyncio
async def test_mobile_access_reports_definite_send_failure_for_retry(tmp_path, monkeypatch):
    workspace = str(tmp_path / "workspace")
    db_path = _write_db(tmp_path)
    config_path = _write_target(tmp_path, workspace=workspace, db_path=db_path)
    monkeypatch.setenv("COCO_MOBILE_ACCESS_APPROVAL_CONFIG", str(config_path))
    runtime, _target, _state = _runtime(
        workspace=workspace,
        db_path=db_path,
        send_result=(False, "definite failure"),
    )

    result = await _enqueue(runtime)

    assert result["accepted"] is False
    assert result["status"] == "retry"
    assert result["retry_after"] == 30


@pytest.mark.asyncio
async def test_controller_rpc_routes_mobile_access_without_transport_metadata(monkeypatch):
    import coco.bot as bot

    calls: list[tuple[dict[str, object], object]] = []

    async def _handle(params: dict[str, object], *, runtime):
        calls.append((params, runtime))
        return {"accepted": True, "status": "started"}

    monkeypatch.setattr(bot, "_handle_mobile_access_enqueue", _handle)
    result = await bot._handle_controller_rpc_request(
        {
            "method": "mobile_access/enqueue",
            "params": {"request_id": REQUEST_ID},
        },
        bot=SimpleNamespace(),
    )

    assert result == {"accepted": True, "status": "started"}
    assert calls[0][0] == {"request_id": REQUEST_ID}
    assert calls[0][1] is bot
