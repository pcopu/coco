"""Tests for fixed /transcription command behavior."""

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
async def test_transcription_command_reports_current_mode(monkeypatch):
    update = _make_update("/transcription")
    replies: list[str] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(
        bot,
        "resolve_transcription_runtime",
        lambda profile="": SimpleNamespace(
            profile="compatible",
            device="cpu",
            compute_type="int8",
            model_name="base",
            gpu_available=False,
        ),
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.transcription_command(update, SimpleNamespace(user_data={}))

    assert replies
    assert "Server transcription mode: `COMPATIBLE`" in replies[0]
    assert "Audio transcription always uses the portable local CPU path." in replies[0]
    assert "Resolved here: `cpu / int8 / base`" in replies[0]


@pytest.mark.asyncio
async def test_transcription_command_rejects_change_attempts(monkeypatch):
    update = _make_update("/transcription compatible")
    replies: list[str] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.transcription_command(update, SimpleNamespace(user_data={}))

    assert replies == [
        "Local transcription is fixed to `COMPATIBLE` on this server. No other modes are available."
    ]
