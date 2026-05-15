"""Tests for /goal command behavior."""

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
async def test_goal_command_reports_active_goal(monkeypatch):
    update = _make_update("/goal")
    replies: list[str] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )

    async def _get_topic_goal(*_args, **_kwargs):
        return True, {"goal": {"objective": "Ship the goal feature", "status": "active"}}, ""

    monkeypatch.setattr(bot.session_manager, "get_topic_goal", _get_topic_goal)

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.goal_command(update, SimpleNamespace(user_data={}))

    assert replies
    assert "Goal: `active`" in replies[0]
    assert "Ship the goal feature" in replies[0]


@pytest.mark.asyncio
async def test_goal_command_set_updates_topic_goal(monkeypatch):
    update = _make_update("/goal set Ship the goal feature")
    replies: list[str] = []
    calls: list[tuple[int, int, int | None, str]] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )

    async def _set_topic_goal(user_id: int, thread_id: int, *, chat_id=None, goal_text: str):
        calls.append((user_id, thread_id, chat_id, goal_text))
        return True, {"goal": {"objective": goal_text, "status": "active"}}, ""

    monkeypatch.setattr(bot.session_manager, "set_topic_goal", _set_topic_goal)

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.goal_command(update, SimpleNamespace(user_data={}))

    assert calls == [(1147817421, 77, -100123, "Ship the goal feature")]
    assert replies
    assert "Goal: `active`" in replies[0]
    assert "Ship the goal feature" in replies[0]
