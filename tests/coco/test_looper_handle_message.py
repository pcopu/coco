"""Tests looper keyword shutdown integration in handle_new_message."""

from types import SimpleNamespace

import pytest

import coco.bot as bot
from coco.session_monitor import NewMessage


@pytest.mark.asyncio
async def test_handle_new_message_stops_looper_on_completion_keyword(monkeypatch):
    events: list[str] = []

    msg = NewMessage(
        session_id="session-1",
        text="done",
        is_complete=True,
        content_type="text",
        role="assistant",
    )

    monkeypatch.setattr(bot, "_codex_app_server_enabled", lambda: False)

    async def _find_users_for_session(_session_id: str):
        return [(1, None, "@1", 10)]

    monkeypatch.setattr(bot.session_manager, "find_users_for_session", _find_users_for_session)
    monkeypatch.setattr(bot, "note_run_activity", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "build_response_parts", lambda *_args, **_kwargs: ["done"])

    async def _enqueue_progress_finalize(*_args, **_kwargs):
        events.append("progress_finalize")

    monkeypatch.setattr(bot, "enqueue_progress_finalize", _enqueue_progress_finalize)
    monkeypatch.setattr(bot, "note_run_completed", lambda **_kwargs: events.append("run_completed"))

    monkeypatch.setattr(
        bot,
        "consume_looper_completion_keyword",
        lambda **_kwargs: SimpleNamespace(plan_path="plans/demo.md", keyword="done"),
    )

    async def _enqueue_content_message(*_args, **_kwargs):
        events.append("content_message")

    monkeypatch.setattr(bot, "enqueue_content_message", _enqueue_content_message)
    monkeypatch.setattr(bot, "queued_topic_input_count", lambda *_args, **_kwargs: 0)

    monkeypatch.setattr(
        bot.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, **_kwargs: -100,
    )

    async def _safe_send(_bot, _chat_id: int, text: str, **_kwargs):
        events.append(f"notify:{text}")

    monkeypatch.setattr(bot, "safe_send", _safe_send)

    async def _update_user_read_offset_for_window(**_kwargs):
        events.append("offset_updated")

    monkeypatch.setattr(
        bot,
        "_update_user_read_offset_for_window",
        _update_user_read_offset_for_window,
    )

    await bot.handle_new_message(msg, SimpleNamespace())

    assert "progress_finalize" in events
    assert "run_completed" in events
    assert "content_message" in events
    assert any("Looper stopped" in item for item in events if item.startswith("notify:"))


@pytest.mark.asyncio
async def test_handle_new_message_progress_skips_read_offset_updates(monkeypatch):
    events: list[str] = []

    msg = NewMessage(
        session_id="session-2",
        text="thinking chunk",
        is_complete=True,
        content_type="progress",
        role="assistant",
    )

    monkeypatch.setattr(bot, "_codex_app_server_enabled", lambda: False)

    async def _find_users_for_session(_session_id: str):
        return [(1, None, "@2", 20)]

    monkeypatch.setattr(
        bot.session_manager,
        "find_users_for_session",
        _find_users_for_session,
    )
    monkeypatch.setattr(bot, "note_run_activity", lambda **_kwargs: None)
    monkeypatch.setattr(
        bot,
        "build_response_parts",
        lambda *_args, **_kwargs: ["thinking chunk"],
    )

    async def _enqueue_progress_update(*_args, **_kwargs):
        events.append("progress_update")

    monkeypatch.setattr(bot, "enqueue_progress_update", _enqueue_progress_update)

    async def _unexpected_enqueue_content_message(*_args, **_kwargs):
        events.append("content_message")

    monkeypatch.setattr(
        bot,
        "enqueue_content_message",
        _unexpected_enqueue_content_message,
    )

    async def _update_user_read_offset_for_window(**_kwargs):
        events.append("offset_updated")

    monkeypatch.setattr(
        bot,
        "_update_user_read_offset_for_window",
        _update_user_read_offset_for_window,
    )

    await bot.handle_new_message(msg, SimpleNamespace())

    assert "progress_update" in events
    assert "content_message" not in events
    assert "offset_updated" not in events
