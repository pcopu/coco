"""Tests for topic cleanup across chat-scoped automation state."""

from types import SimpleNamespace

import pytest

from coco.handlers import autoresearch, looper, message_queue, personality
from coco.handlers.cleanup import clear_topic_state


@pytest.fixture(autouse=True)
def _isolated_automation_state(tmp_path, monkeypatch):
    monkeypatch.setattr(looper, "_LOOPER_STATE_FILE", tmp_path / "looper.json")
    monkeypatch.setattr(
        autoresearch,
        "_AUTORESEARCH_STATE_FILE",
        tmp_path / "autoresearch.json",
    )
    monkeypatch.setattr(
        personality,
        "_PERSONALITY_STATE_FILE",
        tmp_path / "personality.json",
    )
    looper.reset_looper_state_for_tests()
    autoresearch.reset_autoresearch_state_for_tests()
    personality.reset_personality_state_for_tests()
    yield
    looper.reset_looper_state_for_tests()
    autoresearch.reset_autoresearch_state_for_tests()
    personality.reset_personality_state_for_tests()


@pytest.mark.asyncio
async def test_clear_topic_state_only_clears_requested_chat_for_all_automation_apps():
    user_id = 77
    thread_id = 88
    chat_a = -100701
    chat_b = -100702

    looper.start_looper(
        user_id=user_id,
        chat_id=chat_a,
        thread_id=thread_id,
        window_id="@a",
        plan_path="plans/a.md",
        keyword="done",
        now=100.0,
    )
    looper.start_looper(
        user_id=user_id,
        chat_id=chat_b,
        thread_id=thread_id,
        window_id="@b",
        plan_path="plans/b.md",
        keyword="ship",
        now=100.0,
    )
    autoresearch.set_autoresearch_outcome(
        user_id=user_id,
        chat_id=chat_a,
        thread_id=thread_id,
        outcome="goal a",
    )
    autoresearch.set_autoresearch_outcome(
        user_id=user_id,
        chat_id=chat_b,
        thread_id=thread_id,
        outcome="goal b",
    )
    personality._load_state()
    personality._personality_state[(user_id, chat_a, thread_id)] = (
        personality.PersonalityTopicState(last_session_count=1)
    )
    personality._personality_state[(user_id, chat_b, thread_id)] = (
        personality.PersonalityTopicState(last_session_count=2)
    )

    await clear_topic_state(user_id, thread_id, SimpleNamespace(), chat_id=chat_a)

    assert looper.get_looper_state(
        user_id=user_id, chat_id=chat_a, thread_id=thread_id
    ) is None
    assert looper.get_looper_state(
        user_id=user_id, chat_id=chat_b, thread_id=thread_id
    ) is not None
    assert autoresearch.get_autoresearch_state(
        user_id=user_id, chat_id=chat_a, thread_id=thread_id
    ) is None
    assert autoresearch.get_autoresearch_state(
        user_id=user_id, chat_id=chat_b, thread_id=thread_id
    ).outcome == "goal b"
    assert (user_id, chat_a, thread_id) not in personality._personality_state
    assert personality._personality_state[(user_id, chat_b, thread_id)].last_session_count == 2


@pytest.mark.asyncio
async def test_clear_topic_state_only_clears_tool_message_ids_in_requested_chat(monkeypatch):
    user_id = 77
    thread_id = 88
    chat_a = -100701
    chat_b = -100702
    message_queue._tool_msg_ids.clear()
    message_queue._tool_msg_ids[("tool-a", user_id, chat_a, thread_id)] = 7001
    message_queue._tool_msg_ids[("tool-b", user_id, chat_b, thread_id)] = 7002
    async def _clear_interactive_msg(*_args, **_kwargs):
        return None

    monkeypatch.setattr("coco.handlers.cleanup.clear_interactive_msg", _clear_interactive_msg)

    await clear_topic_state(user_id, thread_id, SimpleNamespace(), chat_id=chat_a)

    assert ("tool-a", user_id, chat_a, thread_id) not in message_queue._tool_msg_ids
    assert message_queue._tool_msg_ids[("tool-b", user_id, chat_b, thread_id)] == 7002
    message_queue._tool_msg_ids.clear()
