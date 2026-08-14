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
        return SimpleNamespace(
            accepted=True,
            trigger_looper=True,
            user_id=1147817421,
            thread_id=77,
            chat_id=-100123,
            window_id="@77",
        )

    async def _emit_looper_tick(_bot, **kwargs):
        assert kwargs["force"] is True
        assert kwargs["window_id"] == "@77"
        events.append("looper")

    monkeypatch.setattr(bot, "_forward_topic_text_message", _forward_topic_text_message)
    monkeypatch.setattr(bot, "emit_looper_tick", _emit_looper_tick)

    await bot.text_handler(update, context)

    assert events == ["forward", "looper"]


@pytest.mark.asyncio
async def test_text_handler_looper_uses_forwarded_general_owner_scope(monkeypatch):
    update = _make_update("admin prompt", thread_id=1, user_id=999)
    context = SimpleNamespace(bot=object(), user_data={})
    events: list[str] = []
    lookups: list[dict[str, object]] = []

    monkeypatch.setattr(bot, "_is_chat_allowed", lambda _chat: True)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda uid: uid == 999)
    monkeypatch.setattr(
        bot,
        "_ensure_default_coco_general_control",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(100, 1, -100123),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )

    def _get_looper_state(**kwargs):
        lookups.append(kwargs)
        if kwargs["user_id"] != 100:
            return None
        return SimpleNamespace(trigger_on_user_message=True, window_id="@control")

    monkeypatch.setattr(bot, "get_looper_state", _get_looper_state)

    async def _forward_topic_text_message(**_kwargs):
        events.append("forward")
        return SimpleNamespace(
            accepted=True,
            trigger_looper=True,
            user_id=100,
            thread_id=1,
            chat_id=-100123,
            window_id="@control",
        )

    async def _emit_looper_tick(_bot, **kwargs):
        assert kwargs["user_id"] == 100
        assert kwargs["thread_id"] == 1
        assert kwargs["chat_id"] == -100123
        assert kwargs["window_id"] == "@control"
        events.append("looper")

    monkeypatch.setattr(bot, "_forward_topic_text_message", _forward_topic_text_message)
    monkeypatch.setattr(bot, "emit_looper_tick", _emit_looper_tick)

    await bot.text_handler(update, context)

    assert events == ["forward", "looper"]
    assert lookups == [
        {
            "user_id": 100,
            "chat_id": -100123,
            "thread_id": 1,
        }
    ]


@pytest.mark.asyncio
async def test_text_handler_does_not_trigger_looper_after_rejected_forward(monkeypatch):
    update = _make_update("rejected", user_id=1147817421)
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
        return SimpleNamespace(
            accepted=False,
            trigger_looper=False,
            user_id=1147817421,
            thread_id=77,
            chat_id=-100123,
            window_id="@77",
        )

    async def _emit_looper_tick(_bot, **_kwargs):
        events.append("looper")

    monkeypatch.setattr(bot, "_forward_topic_text_message", _forward_topic_text_message)
    monkeypatch.setattr(bot, "emit_looper_tick", _emit_looper_tick)

    await bot.text_handler(update, context)

    assert events == ["forward"]
