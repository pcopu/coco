"""Authorization tests for anonymous Telegram topic messages."""

from types import SimpleNamespace

import pytest

import coco.bot as bot


def _make_update(
    text: str,
    *,
    thread_id: int,
    effective_user=None,
    sender_chat=None,
):
    chat = SimpleNamespace(type="supergroup", id=-100123)
    message = SimpleNamespace(
        text=text,
        message_thread_id=thread_id,
        chat=chat,
        chat_id=chat.id,
        message_id=55,
        sender_chat=sender_chat,
    )
    return SimpleNamespace(
        effective_user=effective_user,
        effective_message=message,
        effective_chat=chat,
        message=message,
    )


def _install_anonymous_topic_fallback(
    monkeypatch,
    *,
    owner_user_id: int = 100,
    thread_id: int = 1,
):
    monkeypatch.setattr(bot, "_is_chat_allowed", lambda _chat: True)
    monkeypatch.setattr(bot, "is_user_allowed", lambda user_id: user_id == owner_user_id)
    monkeypatch.setattr(
        bot.session_manager,
        "iter_topic_window_bindings",
        lambda: iter([(owner_user_id, -100123, thread_id, "@control")]),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )


@pytest.mark.asyncio
async def test_anonymous_general_tell_is_denied_before_cross_topic_dispatch(monkeypatch):
    update = _make_update(
        "tell worker to inspect the logs",
        thread_id=1,
    )
    context = SimpleNamespace(bot=object(), user_data={})
    replies: list[str] = []
    forwarded: list[dict[str, object]] = []

    _install_anonymous_topic_fallback(monkeypatch)

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    async def _forward(**kwargs):
        forwarded.append(kwargs)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)
    monkeypatch.setattr(bot, "_forward_topic_text_message", _forward)

    await bot.text_handler(update, context)

    assert replies == ["You are not authorized to use this bot."]
    assert forwarded == []


@pytest.mark.asyncio
async def test_anonymous_general_prompt_is_denied_without_sending(monkeypatch):
    update = _make_update("what is the deployment status?", thread_id=1)
    context = SimpleNamespace(bot=object(), user_data={})
    replies: list[str] = []
    sends: list[dict[str, object]] = []

    _install_anonymous_topic_fallback(monkeypatch)

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    async def _forward(**kwargs):
        sends.append(kwargs)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)
    monkeypatch.setattr(bot, "_forward_topic_text_message", _forward)

    await bot.text_handler(update, context)

    assert replies == ["You are not authorized to use this bot."]
    assert sends == []


@pytest.mark.asyncio
async def test_nonallowlisted_anonymous_named_topic_is_denied_without_routing(monkeypatch):
    update = _make_update(
        "continue the current task",
        thread_id=77,
        effective_user=SimpleNamespace(id=999),
    )
    context = SimpleNamespace(bot=object(), user_data={})
    replies: list[str] = []
    forwarded: list[dict[str, object]] = []
    routed: list[tuple[object, ...]] = []

    _install_anonymous_topic_fallback(monkeypatch, thread_id=77)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *args, **_kwargs: routed.append(args),
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    async def _forward(**kwargs):
        forwarded.append(kwargs)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)
    monkeypatch.setattr(bot, "_forward_topic_text_message", _forward)

    await bot.text_handler(update, context)

    assert replies == ["You are not authorized to use this bot."]
    assert forwarded == []
    assert routed == []


@pytest.mark.asyncio
async def test_sender_chat_named_topic_is_denied_without_forward_or_routing(monkeypatch):
    update = _make_update(
        "continue the current task",
        thread_id=77,
        sender_chat=SimpleNamespace(id=-100999, type="channel"),
    )
    context = SimpleNamespace(bot=object(), user_data={})
    replies: list[str] = []
    forwarded: list[dict[str, object]] = []
    routed: list[tuple[object, ...]] = []

    _install_anonymous_topic_fallback(monkeypatch, thread_id=77)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *args, **_kwargs: routed.append(args),
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    async def _forward(**kwargs):
        forwarded.append(kwargs)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)
    monkeypatch.setattr(bot, "_forward_topic_text_message", _forward)

    await bot.text_handler(update, context)

    assert replies == ["You are not authorized to use this bot."]
    assert forwarded == []
    assert routed == []
