"""Tests for /mentions command behavior."""

from types import SimpleNamespace

import pytest

import coco.bot as bot


def _make_update(text: str, *, thread_id: int = 77, user_id: int = 1147817421):
    chat = SimpleNamespace(type="supergroup", id=-100123)
    message = SimpleNamespace(
        text=text,
        message_thread_id=thread_id,
        chat=chat,
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_message=message,
        effective_chat=chat,
        message=message,
    )


@pytest.mark.asyncio
async def test_mentions_command_reports_current_mode(monkeypatch):
    update = _make_update("/mentions")
    replies: list[str] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_window_for_thread",
        lambda _uid, _tid, **_kwargs: "@42",
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_mention_only",
        lambda _wid: False,
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.mentions_command(update, SimpleNamespace(bot=SimpleNamespace(username="Terminex_bot"), user_data={}))

    assert replies
    assert "Mention-only mode: `OFF`" in replies[0]
    assert "Usage: `/mentions` or `/mentions on|off|toggle`" in replies[0]


@pytest.mark.asyncio
async def test_mentions_command_sets_mode(monkeypatch):
    update = _make_update("/mentions on")
    replies: list[str] = []
    set_calls: list[tuple[str, bool]] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_window_for_thread",
        lambda _uid, _tid, **_kwargs: "@42",
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_mention_only",
        lambda _wid: False,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_window_mention_only",
        lambda wid, mention_only: set_calls.append((wid, mention_only)),
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.mentions_command(update, SimpleNamespace(bot=SimpleNamespace(username="Terminex_bot"), user_data={}))

    assert set_calls == [("@42", True)]
    assert replies
    assert "Mention-only mode is now `ON`" in replies[0]


@pytest.mark.asyncio
async def test_mentions_command_requires_bound_topic(monkeypatch):
    update = _make_update("/mentions off")
    replies: list[str] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_window_for_thread",
        lambda _uid, _tid, **_kwargs: None,
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.mentions_command(update, SimpleNamespace(bot=SimpleNamespace(username="Terminex_bot"), user_data={}))

    assert replies == ["❌ No session bound to this topic."]
