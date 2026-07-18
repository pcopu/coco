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
        chat_id=chat.id,
        message_id=555,
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


@pytest.mark.asyncio
async def test_goal_command_treats_direct_goal_text_as_set(monkeypatch):
    update = _make_update("/goal implement /docs/MASTER_GOAL.md into completion")
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

    assert calls == [
        (1147817421, 77, -100123, "implement /docs/MASTER_GOAL.md into completion")
    ]
    assert replies
    assert "Goal: `active`" in replies[0]
    assert "implement /docs/MASTER_GOAL.md into completion" in replies[0]


@pytest.mark.asyncio
async def test_forward_topic_text_message_sets_goal_directly_for_plain_language_request(
    monkeypatch,
):
    update = _make_update("set the goal to implement /docs/MASTER_GOAL.md into completion")
    replies: list[str] = []
    goal_calls: list[tuple[int, int, int | None, str]] = []
    send_calls: list[dict[str, object]] = []

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    async def _set_topic_goal(user_id: int, thread_id: int, *, chat_id=None, goal_text: str):
        goal_calls.append((user_id, thread_id, chat_id, goal_text))
        return True, {"goal": {"objective": goal_text, "status": "active"}}, ""

    async def _send_topic_text_to_window(**kwargs):
        send_calls.append(kwargs)
        return True, "ok"

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)
    monkeypatch.setattr(bot.session_manager, "get_window_for_thread", lambda *_args, **_kwargs: "@77")
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda *_args, **_kwargs: SimpleNamespace(codex_thread_id="thread-77", cwd="/tmp/proj"),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_topic_response_mode",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "is_coco_control_topic",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_mention_only",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "is_window_external_turn_active",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(bot, "_is_window_in_progress", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(bot.session_manager, "set_topic_goal", _set_topic_goal)
    monkeypatch.setattr(bot.session_manager, "send_topic_text_to_window", _send_topic_text_to_window)

    async def _send_action(_action):
        return None

    update.message.chat.send_action = _send_action

    await bot._forward_topic_text_message(
        message=update.message,
        context=SimpleNamespace(bot=object(), user_data={}),
        user_id=1147817421,
        thread_id=77,
        chat_id=-100123,
        text=update.message.text,
    )

    assert goal_calls == [
        (1147817421, 77, -100123, "implement /docs/MASTER_GOAL.md into completion")
    ]
    assert not send_calls
    assert replies
    assert "Goal: `active`" in replies[0]
    assert "implement /docs/MASTER_GOAL.md into completion" in replies[0]


@pytest.mark.parametrize(
    "text",
    [
        "set the goal to ship the docs and then start implementing it",
        "set the goal to deploy if the tests pass",
    ],
)
def test_direct_goal_parser_leaves_compound_requests_for_the_model(text):
    assert bot._parse_direct_goal_request(text) == ("", "")
