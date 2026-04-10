"""Tests for immediate looper trigger on normal user messages."""

from types import SimpleNamespace

import pytest

import coco.bot as bot


def _make_update(text: str, *, thread_id: int = 77, user_id: int = 1147817421):
    chat = SimpleNamespace(type="supergroup", id=-100123)
    message = SimpleNamespace(
        text=text,
        message_thread_id=thread_id,
        chat=chat,
        chat_id=chat.id,
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_message=message,
        effective_chat=chat,
        message=message,
    )


@pytest.mark.asyncio
async def test_text_handler_triggers_immediate_looper_tick_after_forward(monkeypatch):
    update = _make_update("next one")
    context = SimpleNamespace(bot=object(), user_data={})
    events: list[str] = []

    monkeypatch.setattr(bot, "_is_chat_allowed", lambda _chat: True)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot,
        "get_looper_state",
        lambda **_kwargs: SimpleNamespace(trigger_on_user_message=True, window_id="@77"),
    )

    async def _forward_topic_text_message(**_kwargs):
        events.append("forward")

    async def _emit_looper_tick(_bot, **kwargs):
        assert kwargs["force"] is True
        assert kwargs["window_id"] == "@77"
        events.append("looper")

    monkeypatch.setattr(bot, "_forward_topic_text_message", _forward_topic_text_message)
    monkeypatch.setattr(bot, "emit_looper_tick", _emit_looper_tick)

    await bot.text_handler(update, context)

    assert events == ["forward", "looper"]
