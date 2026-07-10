"""Regression tests for topic-wide app-server interruption."""

from types import SimpleNamespace

import pytest
from telegram.error import NetworkError

import coco.bot as bot
import coco.handlers.commands as commands


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_status_reply", [False, True])
@pytest.mark.parametrize("failure_mode", ["none", "interrupt", "read"])
async def test_esc_recovers_active_turn_from_thread_read_and_fences_before_interrupt(
    monkeypatch,
    fail_status_reply,
    failure_mode,
):
    events: list[str] = []
    message = SimpleNamespace()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=-10042, type="supergroup"),
        effective_message=message,
        message=message,
    )
    context = SimpleNamespace(bot=object())

    commands._sync_bot_globals()
    monkeypatch.setattr(commands, "_sync_bot_globals", lambda: None)
    monkeypatch.setattr(commands, "is_user_allowed", lambda _uid: True, raising=False)

    async def _allowed(_update):
        return True

    monkeypatch.setattr(commands, "_ensure_chat_allowed", _allowed)
    monkeypatch.setattr(commands, "_get_thread_id", lambda _update: 777)
    monkeypatch.setattr(commands, "_codex_app_server_enabled", lambda: True)
    monkeypatch.setattr(
        commands.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        commands.session_manager,
        "resolve_window_for_thread",
        lambda *_args, **_kwargs: "@7",
    )
    monkeypatch.setattr(
        commands.session_manager,
        "resolve_topic_binding",
        lambda *_args, **_kwargs: SimpleNamespace(codex_thread_id="th-7"),
    )
    monkeypatch.setattr(
        commands.session_manager,
        "get_window_codex_active_turn_id",
        lambda _wid: "",
    )
    monkeypatch.setattr(
        commands.codex_app_server_client,
        "get_active_turn_id",
        lambda _thread_id: None,
    )

    async def _thread_read(*, thread_id: str, timeout: float):
        assert timeout == 5.0
        events.append(f"read:{thread_id}")
        if failure_mode == "read":
            raise RuntimeError("thread read failed")
        return {
            "thread": {
                "id": thread_id,
                "turns": [
                    {"id": "turn-old", "status": "completed"},
                    {"id": "turn-live", "status": "inProgress"},
                ],
            }
        }

    async def _interrupt(*, thread_id: str, turn_id: str):
        assert thread_id in bot._interrupted_codex_threads
        events.append(f"interrupt:{thread_id}:{turn_id}")
        if failure_mode == "interrupt":
            raise RuntimeError("interrupt transport failed")

    async def _cancel_delivery(
        user_id: int, thread_id: int | None, *, chat_id: int | None = None
    ):
        assert chat_id == -10042
        events.append(f"purge:{user_id}:{thread_id}")
        return 3

    async def _clear_dock(*_args, **_kwargs):
        events.append("dock-cleared")

    async def _clear_progress(*_args, **_kwargs):
        events.append("progress-cleared")

    replies: list[str] = []
    reply_attempts = 0

    async def _reply(_message, text: str, **_kwargs):
        nonlocal reply_attempts
        reply_attempts += 1
        if fail_status_reply and reply_attempts == 1:
            raise NetworkError("telegram unavailable")
        replies.append(text)

    monkeypatch.setattr(commands.codex_app_server_client, "thread_read", _thread_read)
    monkeypatch.setattr(commands.codex_app_server_client, "turn_interrupt", _interrupt)
    monkeypatch.setattr(commands, "cancel_topic_delivery", _cancel_delivery, raising=False)
    monkeypatch.setattr(commands, "clear_queued_topic_inputs", lambda *_args: None)
    monkeypatch.setattr(commands, "clear_queued_topic_dock", _clear_dock)
    monkeypatch.setattr(commands, "enqueue_progress_clear", _clear_progress, raising=False)
    monkeypatch.setattr(commands, "safe_reply", _reply)
    monkeypatch.setattr(commands.session_manager, "clear_window_codex_turn", lambda _wid: None)
    monkeypatch.setattr(
        commands.codex_app_server_client,
        "clear_active_turn",
        lambda _thread_id: None,
    )

    bot._interrupted_codex_threads.discard("th-7")
    try:
        await commands.esc_command(update, context)
        fence_after_command = "th-7" in bot._interrupted_codex_threads
    finally:
        bot._interrupted_codex_threads.discard("th-7")
        bot._interrupted_codex_turns.pop("th-7", None)

    expected_events = [
        "purge:42:777",
        "read:th-7",
    ]
    if failure_mode != "read":
        expected_events.append("interrupt:th-7:turn-live")
    expected_events.extend(["dock-cleared", "progress-cleared"])
    assert events == expected_events
    if fail_status_reply:
        assert replies == []
        return
    assert replies
    if failure_mode == "interrupt":
        assert fence_after_command is False
        assert replies[-1].startswith("❌ App-server interrupt failed")
    elif failure_mode == "read":
        assert fence_after_command is False
        assert replies[-1].startswith("⎋ No foreground turn")
    else:
        assert fence_after_command is True
        assert replies[-1].startswith("⎋ Interrupted")
