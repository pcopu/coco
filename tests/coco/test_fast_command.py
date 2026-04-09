"""Tests for /fast command behavior."""

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
async def test_fast_command_reports_current_mode(monkeypatch):
    update = _make_update("/fast")
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
        "ensure_topic_binding",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_topic_service_tier_selection",
        lambda *_args, **_kwargs: "fast",
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.fast_command(update, SimpleNamespace(user_data={}))

    assert replies
    assert "Fast mode: `ON`" in replies[0]
    assert "Future turns in this topic use the `fast` service tier." in replies[0]
    assert "Usage: `/fast` or `/fast on|off|toggle`" in replies[0]


@pytest.mark.asyncio
async def test_fast_command_sets_topic_service_tier_without_reset(monkeypatch):
    update = _make_update("/fast off")
    replies: list[str] = []
    set_calls: list[tuple[int, int, int | None, str]] = []
    reset_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "ensure_topic_binding",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_topic_service_tier_selection",
        lambda *_args, **_kwargs: "fast",
    )

    def _set_topic_service_tier_selection(
        user_id: int,
        thread_id: int,
        *,
        chat_id: int | None = None,
        service_tier: str = "",
    ) -> bool:
        set_calls.append((user_id, thread_id, chat_id, service_tier))
        return True

    monkeypatch.setattr(
        bot.session_manager,
        "set_topic_service_tier_selection",
        _set_topic_service_tier_selection,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_window_codex_thread_id",
        lambda wid, thread_id: reset_calls.append((wid, thread_id)),
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.fast_command(update, SimpleNamespace(user_data={}))

    assert set_calls == [(1147817421, 77, -100123, "flex")]
    assert reset_calls == []
    assert replies
    assert "Fast mode is now `OFF`" in replies[0]
    assert "Future turns in this topic will use the `flex` service tier." in replies[0]
