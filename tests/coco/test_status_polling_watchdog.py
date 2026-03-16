"""Tests for watchdog resend behavior in status polling."""

import asyncio
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest

import coco.handlers.status_polling as status_polling
from coco.handlers.run_watchdog import RunWatchCheck


@pytest.mark.asyncio
async def test_emit_due_watchdog_skips_resend_when_active_turn(monkeypatch):
    sent_messages: list[str] = []
    telemetry: list[tuple[str, dict[str, object]]] = []

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

    assert sent_messages
    assert sent_messages == ["👀"]
    assert telemetry
    assert telemetry[-1][0] == "watchdog.check_fired"
    assert telemetry[-1][1]["auto_retry_reason"] == "active_turn"
    assert telemetry[-1][1]["retry_attempted"] is False


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
    monkeypatch.setattr(status_polling, "get_message_queue", lambda _uid: None)
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

    monkeypatch.setattr(status_polling, "_emit_due_run_watchdog_checks", _emit_watchdog)
    monkeypatch.setattr(status_polling, "_emit_due_looper_prompt", _emit_looper)

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
    monkeypatch.setattr(status_polling, "get_message_queue", lambda _uid: None)
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

    monkeypatch.setattr(status_polling, "_emit_due_run_watchdog_checks", _emit_watchdog)
    monkeypatch.setattr(status_polling, "_emit_due_looper_prompt", _emit_looper)

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
