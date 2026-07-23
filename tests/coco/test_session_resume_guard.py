"""Regression tests for guarding oversized Codex rollout resumes."""

import json
import os
from pathlib import Path

import pytest

import coco.session as session_mod
from coco.codex_app_server import CodexAppServerError
from coco.session import SessionManager


@pytest.fixture
def mgr(monkeypatch) -> SessionManager:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    return SessionManager()


def _write_rollout(
    sessions_root: Path,
    *,
    thread_id: str,
    cwd: Path,
    padding_bytes: int = 0,
) -> Path:
    session_dir = sessions_root / "2026" / "07"
    session_dir.mkdir(parents=True, exist_ok=True)
    rollout = session_dir / f"rollout-2026-07-23T12-00-00-{thread_id}.jsonl"
    metadata = {
        "timestamp": "2026-07-23T12:00:00Z",
        "type": "session_meta",
        "payload": {
            "id": thread_id,
            "cwd": str(cwd.resolve()),
            "source": "vscode",
        },
    }
    rollout.write_text(
        json.dumps(metadata) + "\n" + ("x" * padding_bytes),
        encoding="utf-8",
    )
    return rollout


def test_transport_snapshot_requires_string_epoch(mgr: SessionManager) -> None:
    assert mgr._normalize_codex_transport_snapshot(
        {
            "epoch": None,
            "epoch_started_at": 100.0,
            "generation": 7,
        }
    ) == ("", 0.0, 0)


@pytest.mark.asyncio
async def test_explicit_resume_size_checks_rollout_older_than_discovery_window(
    mgr: SessionManager,
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions_root = tmp_path / "sessions"
    rollout = _write_rollout(
        sessions_root,
        thread_id="thread-old-large",
        cwd=workspace,
        padding_bytes=64,
    )
    os.utime(rollout, (1, 1))
    for index in range(300):
        newer_rollout = _write_rollout(
            sessions_root,
            thread_id=f"thread-newer-{index}",
            cwd=workspace,
        )
        newer_timestamp = 1_000 + index
        os.utime(newer_rollout, (newer_timestamp, newer_timestamp))

    monkeypatch.setattr(session_mod.config, "session_provider", "codex")
    monkeypatch.setattr(session_mod.config, "sessions_path", sessions_root)
    monkeypatch.setattr(
        session_mod.config,
        "codex_max_resume_bytes",
        rollout.stat().st_size - 1,
        raising=False,
    )

    async def _unexpected_thread_resume(*, thread_id: str):
        raise AssertionError(f"oversized thread reached app-server: {thread_id}")

    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "thread_resume",
        _unexpected_thread_resume,
    )

    with pytest.raises(CodexAppServerError, match="resume limit"):
        await mgr.resume_codex_session_for_window(
            window_id="@1",
            cwd=str(workspace),
            thread_id="thread-old-large",
        )


@pytest.mark.asyncio
async def test_explicit_resume_under_size_limit_calls_app_server(
    mgr: SessionManager,
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions_root = tmp_path / "sessions"
    rollout = _write_rollout(
        sessions_root,
        thread_id="thread-small",
        cwd=workspace,
    )
    monkeypatch.setattr(session_mod.config, "sessions_path", sessions_root)
    monkeypatch.setattr(
        session_mod.config,
        "codex_max_resume_bytes",
        rollout.stat().st_size + 1,
        raising=False,
    )

    resume_calls: list[str] = []

    async def _thread_resume(*, thread_id: str):
        resume_calls.append(thread_id)
        return {
            "thread": {"id": thread_id},
            "turn": {"id": "turn-small"},
        }

    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "thread_resume",
        _thread_resume,
    )

    resumed = await mgr.resume_codex_session_for_window(
        window_id="@1",
        cwd=str(workspace),
        thread_id="thread-small",
    )

    assert resumed == "thread-small"
    assert resume_calls == ["thread-small"]
    assert mgr.get_window_codex_thread_id("@1") == "thread-small"
    assert mgr.get_window_codex_active_turn_id("@1") == "turn-small"


@pytest.mark.asyncio
async def test_metadata_only_resume_preserves_known_active_turn(
    mgr: SessionManager,
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions_root = tmp_path / "sessions"
    rollout = _write_rollout(
        sessions_root,
        thread_id="thread-known",
        cwd=workspace,
    )
    monkeypatch.setattr(session_mod.config, "sessions_path", sessions_root)
    monkeypatch.setattr(
        session_mod.config,
        "codex_max_resume_bytes",
        rollout.stat().st_size + 1,
        raising=False,
    )
    mgr.set_window_codex_thread_id("@1", "thread-known")
    mgr.set_window_codex_active_turn_id("@1", "turn-known")
    mgr.set_window_codex_transport_state(
        "@1",
        epoch="agent-epoch-live",
        epoch_started_at=100.0,
        generation=7,
    )

    async def _thread_resume(*, thread_id: str):
        return {"thread": {"id": thread_id}}

    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "thread_resume",
        _thread_resume,
    )
    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "get_active_turn_id",
        lambda _thread_id: None,
    )
    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "transport_state_snapshot",
        lambda: {
            "epoch": "agent-epoch-live",
            "epoch_started_at": 100.0,
            "generation": 7,
        },
    )

    resumed = await mgr.resume_codex_session_for_window(
        window_id="@1",
        cwd=str(workspace),
        thread_id="thread-known",
    )

    assert resumed == "thread-known"
    assert mgr.get_window_codex_active_turn_id("@1") == "turn-known"


@pytest.mark.asyncio
async def test_metadata_only_resume_drops_known_turn_after_server_restart(
    mgr: SessionManager,
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions_root = tmp_path / "sessions"
    rollout = _write_rollout(
        sessions_root,
        thread_id="thread-restarted",
        cwd=workspace,
    )
    monkeypatch.setattr(session_mod.config, "sessions_path", sessions_root)
    monkeypatch.setattr(
        session_mod.config,
        "codex_max_resume_bytes",
        rollout.stat().st_size + 1,
        raising=False,
    )
    mgr.set_window_codex_thread_id("@1", "thread-restarted")
    mgr.set_window_codex_active_turn_id("@1", "turn-from-old-server")
    mgr.set_window_codex_transport_state(
        "@1",
        epoch="agent-epoch-live",
        epoch_started_at=100.0,
        generation=7,
    )

    async def _thread_resume(*, thread_id: str):
        return {"thread": {"id": thread_id}}

    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "thread_resume",
        _thread_resume,
    )
    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "get_active_turn_id",
        lambda _thread_id: None,
    )
    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "transport_state_snapshot",
        lambda: {
            "epoch": "agent-epoch-live",
            "epoch_started_at": 100.0,
            "generation": 8,
        },
    )

    resumed = await mgr.resume_codex_session_for_window(
        window_id="@1",
        cwd=str(workspace),
        thread_id="thread-restarted",
    )

    assert resumed == "thread-restarted"
    assert mgr.get_window_codex_active_turn_id("@1") == ""
    assert mgr.get_window_codex_transport_state("@1") == (
        "agent-epoch-live",
        100.0,
        8,
    )


@pytest.mark.asyncio
async def test_metadata_only_resume_drops_known_turn_from_old_agent_epoch(
    mgr: SessionManager,
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions_root = tmp_path / "sessions"
    rollout = _write_rollout(
        sessions_root,
        thread_id="thread-old-agent",
        cwd=workspace,
    )
    monkeypatch.setattr(session_mod.config, "sessions_path", sessions_root)
    monkeypatch.setattr(
        session_mod.config,
        "codex_max_resume_bytes",
        rollout.stat().st_size + 1,
        raising=False,
    )
    mgr.set_window_codex_thread_id("@1", "thread-old-agent")
    mgr.set_window_codex_active_turn_id("@1", "turn-from-old-agent")
    mgr.set_window_codex_transport_state(
        "@1",
        epoch="agent-epoch-before-restart",
        epoch_started_at=100.0,
        generation=1,
    )

    async def _thread_resume(*, thread_id: str):
        return {"thread": {"id": thread_id}}

    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "thread_resume",
        _thread_resume,
    )
    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "get_active_turn_id",
        lambda _thread_id: None,
    )
    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "transport_state_snapshot",
        lambda: {
            "epoch": "agent-epoch-after-restart",
            "epoch_started_at": 200.0,
            "generation": 1,
        },
    )

    resumed = await mgr.resume_codex_session_for_window(
        window_id="@1",
        cwd=str(workspace),
        thread_id="thread-old-agent",
    )

    assert resumed == "thread-old-agent"
    assert mgr.get_window_codex_active_turn_id("@1") == ""
    assert mgr.get_window_codex_transport_state("@1") == (
        "agent-epoch-after-restart",
        200.0,
        1,
    )


@pytest.mark.asyncio
async def test_metadata_only_resume_uses_client_live_active_turn(
    mgr: SessionManager,
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions_root = tmp_path / "sessions"
    rollout = _write_rollout(
        sessions_root,
        thread_id="thread-live",
        cwd=workspace,
    )
    monkeypatch.setattr(session_mod.config, "sessions_path", sessions_root)
    monkeypatch.setattr(
        session_mod.config,
        "codex_max_resume_bytes",
        rollout.stat().st_size + 1,
        raising=False,
    )

    async def _thread_resume(*, thread_id: str):
        return {"thread": {"id": thread_id}}

    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "thread_resume",
        _thread_resume,
    )
    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "get_active_turn_id",
        lambda thread_id: "turn-live" if thread_id == "thread-live" else None,
    )

    resumed = await mgr.resume_codex_session_for_window(
        window_id="@1",
        cwd=str(workspace),
        thread_id="thread-live",
    )

    assert resumed == "thread-live"
    assert mgr.get_window_codex_active_turn_id("@1") == "turn-live"


@pytest.mark.asyncio
async def test_explicit_resume_over_size_limit_fails_before_app_server(
    mgr: SessionManager,
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions_root = tmp_path / "sessions"
    rollout = _write_rollout(
        sessions_root,
        thread_id="thread-large",
        cwd=workspace,
        padding_bytes=64,
    )
    monkeypatch.setattr(session_mod.config, "sessions_path", sessions_root)
    monkeypatch.setattr(
        session_mod.config,
        "codex_max_resume_bytes",
        rollout.stat().st_size - 1,
        raising=False,
    )

    async def _unexpected_thread_resume(*, thread_id: str):
        raise AssertionError(f"oversized thread reached app-server: {thread_id}")

    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "thread_resume",
        _unexpected_thread_resume,
    )

    with pytest.raises(CodexAppServerError, match="resume limit"):
        await mgr.resume_codex_session_for_window(
            window_id="@1",
            cwd=str(workspace),
            thread_id="thread-large",
        )


@pytest.mark.asyncio
async def test_automatic_latest_resume_skips_oversized_rollout_and_marks_rollover(
    mgr: SessionManager,
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions_root = tmp_path / "sessions"
    rollout = _write_rollout(
        sessions_root,
        thread_id="thread-large",
        cwd=workspace,
        padding_bytes=64,
    )
    monkeypatch.setattr(session_mod.config, "session_provider", "codex")
    monkeypatch.setattr(session_mod.config, "sessions_path", sessions_root)
    monkeypatch.setattr(
        session_mod.config,
        "codex_max_resume_bytes",
        rollout.stat().st_size - 1,
        raising=False,
    )

    async def _unexpected_thread_resume(*, thread_id: str):
        raise AssertionError(f"oversized thread reached app-server: {thread_id}")

    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "thread_resume",
        _unexpected_thread_resume,
    )

    resumed = await mgr.resume_latest_codex_session_for_window(
        window_id="@1",
        cwd=str(workspace),
    )

    assert resumed == ""
    assert (
        mgr.consume_window_pending_session_start_reason("@1")
        == "oversized_rollover"
    )


@pytest.mark.asyncio
async def test_binding_validation_rolls_over_oversized_thread_without_server_read(
    mgr: SessionManager,
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions_root = tmp_path / "sessions"
    rollout = _write_rollout(
        sessions_root,
        thread_id="thread-large",
        cwd=workspace,
        padding_bytes=64,
    )
    monkeypatch.setattr(session_mod.config, "session_provider", "codex")
    monkeypatch.setattr(session_mod.config, "sessions_path", sessions_root)
    monkeypatch.setattr(
        session_mod.config,
        "codex_max_resume_bytes",
        rollout.stat().st_size - 1,
        raising=False,
    )
    mgr.bind_topic_to_codex_thread(
        user_id=10,
        thread_id=7,
        codex_thread_id="thread-large",
        cwd=str(workspace),
        display_name="large",
        window_id="@1",
    )

    async def _unexpected_thread_read(**_kwargs):
        raise AssertionError("oversized thread reached app-server validation")

    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "thread_read",
        _unexpected_thread_read,
    )

    summary = await mgr.validate_codex_topic_bindings()

    assert summary == {"checked": 1, "invalid": 1, "repaired": 1}
    assert mgr.get_window_codex_thread_id("@1") == ""
    assert (
        mgr.consume_window_pending_session_start_reason("@1")
        == "oversized_rollover"
    )
