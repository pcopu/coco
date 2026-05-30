"""Tests for /voice command behavior."""

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
async def test_voice_command_reports_current_mode(monkeypatch):
    update = _make_update("/voice")
    replies: list[str] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(bot.session_manager, "get_topic_response_mode", lambda *_args, **_kwargs: "text")
    monkeypatch.setattr(bot, "get_default_tts_voice", lambda: "M1")
    monkeypatch.setattr(bot, "get_default_tts_speed", lambda: 1.4)

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.voice_command(update, SimpleNamespace(user_data={}))

    assert replies == [
        "Voice replies for this topic are currently `OFF`.\nDefault voice: `M1`\nDefault speed: `1.4`"
    ]


@pytest.mark.asyncio
async def test_voice_command_turns_voice_on(monkeypatch):
    update = _make_update("/voice on")
    replies: list[str] = []
    set_calls: list[tuple[int, int, int, str]] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(bot, "get_default_tts_voice", lambda: "F2")
    monkeypatch.setattr(bot, "get_default_tts_speed", lambda: 1.4)

    def _set_topic_response_mode(user_id, thread_id, *, chat_id=None, response_mode=""):
        set_calls.append((user_id, thread_id, chat_id, response_mode))
        return True

    monkeypatch.setattr(bot.session_manager, "set_topic_response_mode", _set_topic_response_mode)

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.voice_command(update, SimpleNamespace(user_data={}))

    assert set_calls == [(1147817421, 77, -100123, "voice")]
    assert replies == [
        "✅ Voice replies are now `ON` for this topic.\nCurrent default voice: `F2`\nCurrent default speed: `1.4`"
    ]


@pytest.mark.asyncio
async def test_voice_command_turns_voice_off(monkeypatch):
    update = _make_update("/voice off")
    replies: list[str] = []
    set_calls: list[tuple[int, int, int, str]] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)

    def _set_topic_response_mode(user_id, thread_id, *, chat_id=None, response_mode=""):
        set_calls.append((user_id, thread_id, chat_id, response_mode))
        return True

    monkeypatch.setattr(bot.session_manager, "set_topic_response_mode", _set_topic_response_mode)

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.voice_command(update, SimpleNamespace(user_data={}))

    assert set_calls == [(1147817421, 77, -100123, "text")]
    assert replies == ["✅ Voice replies are now `OFF` for this topic."]
