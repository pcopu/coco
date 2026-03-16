"""Tests for queued /q dock message syncing."""

from types import SimpleNamespace

import pytest

import coco.handlers.message_queue as mq


@pytest.mark.asyncio
async def test_sync_queued_topic_dock_send_replace_delete(monkeypatch):
    user_id = 17
    thread_id = 71

    mq.clear_queued_topic_inputs(user_id, thread_id)
    mq._queue_dock_msg_info.clear()

    class _FakeBot:
        def __init__(self) -> None:
            self.sent: list[tuple[int, str]] = []
            self.edited: list[tuple[int, int, str]] = []
            self.deleted: list[tuple[int, int]] = []

        async def send_message(self, *, chat_id: int, text: str, **_kwargs):
            self.sent.append((chat_id, text))
            return SimpleNamespace(message_id=900 + len(self.sent))

        async def edit_message_text(
            self,
            *,
            chat_id: int,
            message_id: int,
            text: str,
            **_kwargs,
        ):
            self.edited.append((chat_id, message_id, text))
            return True

        async def delete_message(self, *, chat_id: int, message_id: int):
            self.deleted.append((chat_id, message_id))
            return True

    fake_bot = _FakeBot()

    monkeypatch.setattr(mq.session_manager, "resolve_chat_id", lambda _uid, _tid: -100123)
    monkeypatch.setattr(mq.session_manager, "get_display_name", lambda _wid: "demo")

    mq.enqueue_queued_topic_input(user_id, thread_id, "first queued item", -100123, 1)
    await mq.sync_queued_topic_dock(fake_bot, user_id, thread_id, window_id="@1")
    assert len(fake_bot.sent) == 1
    assert fake_bot.sent[0][1].startswith("⏳ Queue")
    assert not fake_bot.edited
    assert not fake_bot.deleted

    mq.enqueue_queued_topic_input(user_id, thread_id, "second queued item", -100123, 2)
    await mq.sync_queued_topic_dock(fake_bot, user_id, thread_id, window_id="@1")
    assert len(fake_bot.sent) == 2
    assert not fake_bot.edited
    assert len(fake_bot.deleted) == 1
    assert fake_bot.sent[1][1].startswith("⏳ Queue")

    mq.clear_queued_topic_inputs(user_id, thread_id)
    await mq.sync_queued_topic_dock(fake_bot, user_id, thread_id, window_id="@1")
    assert len(fake_bot.deleted) == 2

    mq._queue_dock_msg_info.clear()
