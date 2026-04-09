"""Tests for local Telegram audio transcription ingress."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram.constants import ChatAction

import coco.bot as bot
from coco.transcription import TranscriptionBootstrapHandle


class _FakeChat:
    type = "supergroup"
    id = -100123

    def __init__(self) -> None:
        self.actions: list[str] = []

    async def send_action(self, action: str) -> None:
        self.actions.append(action)


class _FakeTelegramFile:
    def __init__(self) -> None:
        self.paths: list[Path] = []

    async def download_to_drive(self, path: Path) -> None:
        path.write_bytes(b"audio-bytes")
        self.paths.append(path)


class _FakeVoice:
    file_unique_id = "voice-123"
    duration = 7
    mime_type = "audio/ogg"

    def __init__(self, tg_file: _FakeTelegramFile) -> None:
        self._tg_file = tg_file

    async def get_file(self):
        return self._tg_file


def _make_voice_update():
    chat = _FakeChat()
    tg_file = _FakeTelegramFile()
    message = SimpleNamespace(
        text=None,
        caption=None,
        voice=_FakeVoice(tg_file),
        audio=None,
        message_thread_id=77,
        chat=chat,
        chat_id=chat.id,
        message_id=999,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1147817421),
        effective_message=message,
        effective_chat=chat,
        message=message,
    )
    return update, tg_file


@pytest.mark.asyncio
async def test_audio_handler_transcribes_voice_and_forwards_topic_text(
    monkeypatch, tmp_path
):
    update, tg_file = _make_voice_update()
    context = SimpleNamespace(bot=object(), user_data={})
    forwarded: list[dict[str, object]] = []
    replies: list[str] = []

    monkeypatch.setattr(bot, "_AUDIO_DIR", tmp_path, raising=False)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(
        bot,
        "begin_transcription_bootstrap",
        lambda profile="": None,
        raising=False,
    )

    def _transcribe_audio_file(path: Path, *, profile: str = ""):
        assert path.exists()
        assert profile == "compatible"
        return "voice transcript"

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    async def _forward_topic_text_message(
        *,
        message,
        context,
        user_id: int,
        thread_id: int | None,
        chat_id: int | None,
        text: str,
    ) -> None:
        assert replies == ["voice transcript"]
        forwarded.append(
            {
                "message": message,
                "context": context,
                "user_id": user_id,
                "thread_id": thread_id,
                "chat_id": chat_id,
                "text": text,
            }
        )

    monkeypatch.setattr(bot, "transcribe_audio_file", _transcribe_audio_file, raising=False)
    monkeypatch.setattr(bot, "safe_reply", _safe_reply)
    monkeypatch.setattr(
        bot,
        "_forward_topic_text_message",
        _forward_topic_text_message,
        raising=False,
    )

    await bot.audio_handler(update, context)

    assert update.message.chat.actions == [ChatAction.TYPING]
    assert len(tg_file.paths) == 1
    assert not tg_file.paths[0].exists()
    assert replies == ["voice transcript"]
    assert forwarded == [
        {
            "message": update.message,
            "context": context,
            "user_id": 1147817421,
            "thread_id": 77,
            "chat_id": -100123,
            "text": "voice transcript",
        }
    ]


@pytest.mark.asyncio
async def test_audio_handler_replies_when_transcription_fails(monkeypatch, tmp_path):
    update, tg_file = _make_voice_update()
    context = SimpleNamespace(bot=object(), user_data={})
    replies: list[str] = []

    monkeypatch.setattr(bot, "_AUDIO_DIR", tmp_path, raising=False)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(
        bot,
        "begin_transcription_bootstrap",
        lambda profile="": None,
        raising=False,
    )

    def _transcribe_audio_file(_path: Path, *, profile: str = ""):
        assert profile == "compatible"
        raise RuntimeError("faster-whisper unavailable")

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    async def _unexpected_forward(**_kwargs):
        raise AssertionError("audio should not forward text when transcription fails")

    monkeypatch.setattr(bot, "transcribe_audio_file", _transcribe_audio_file, raising=False)
    monkeypatch.setattr(bot, "safe_reply", _safe_reply)
    monkeypatch.setattr(bot, "_forward_topic_text_message", _unexpected_forward, raising=False)

    await bot.audio_handler(update, context)

    assert len(tg_file.paths) == 1
    assert not tg_file.paths[0].exists()
    assert replies == ["❌ Audio transcription failed: faster-whisper unavailable"]


@pytest.mark.asyncio
async def test_audio_handler_announces_first_model_download_and_ready(
    monkeypatch, tmp_path
):
    update, tg_file = _make_voice_update()
    context = SimpleNamespace(bot=object(), user_data={})
    replies: list[str] = []
    forwarded: list[str] = []
    complete_calls: list[bool] = []

    monkeypatch.setattr(bot, "_AUDIO_DIR", tmp_path, raising=False)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(
        bot,
        "begin_transcription_bootstrap",
        lambda profile="": TranscriptionBootstrapHandle(("base", "cpu", "int8", "")),
        raising=False,
    )
    monkeypatch.setattr(
        bot,
        "complete_transcription_bootstrap",
        lambda _handle, *, success: complete_calls.append(success) or success,
        raising=False,
    )
    monkeypatch.setattr(
        bot,
        "transcribe_audio_file",
        lambda _path, *, profile="": "voice transcript",
        raising=False,
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    async def _forward_topic_text_message(**kwargs):
        forwarded.append(kwargs["text"])

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)
    monkeypatch.setattr(
        bot,
        "_forward_topic_text_message",
        _forward_topic_text_message,
        raising=False,
    )

    await bot.audio_handler(update, context)

    assert len(tg_file.paths) == 1
    assert not tg_file.paths[0].exists()
    assert complete_calls == [True]
    assert forwarded == ["voice transcript"]
    assert replies == [
        "⏳ Downloading the local transcription model for first use. This can take a minute.",
        "voice transcript",
        "✅ Local transcription is ready. The model finished downloading and the first transcription is complete.",
    ]


@pytest.mark.asyncio
async def test_audio_handler_clears_bootstrap_without_ready_notice_on_failure(
    monkeypatch, tmp_path
):
    update, tg_file = _make_voice_update()
    context = SimpleNamespace(bot=object(), user_data={})
    replies: list[str] = []
    complete_calls: list[bool] = []

    monkeypatch.setattr(bot, "_AUDIO_DIR", tmp_path, raising=False)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(
        bot,
        "begin_transcription_bootstrap",
        lambda profile="": TranscriptionBootstrapHandle(("base", "cpu", "int8", "")),
        raising=False,
    )
    monkeypatch.setattr(
        bot,
        "complete_transcription_bootstrap",
        lambda _handle, *, success: complete_calls.append(success) or False,
        raising=False,
    )

    def _transcribe_audio_file(_path: Path, *, profile: str = ""):
        assert profile == "compatible"
        raise RuntimeError("download interrupted")

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    async def _unexpected_forward(**_kwargs):
        raise AssertionError("audio should not forward text when transcription fails")

    monkeypatch.setattr(bot, "transcribe_audio_file", _transcribe_audio_file, raising=False)
    monkeypatch.setattr(bot, "safe_reply", _safe_reply)
    monkeypatch.setattr(bot, "_forward_topic_text_message", _unexpected_forward, raising=False)

    await bot.audio_handler(update, context)

    assert len(tg_file.paths) == 1
    assert not tg_file.paths[0].exists()
    assert complete_calls == [False]
    assert replies == [
        "⏳ Downloading the local transcription model for first use. This can take a minute.",
        "❌ Audio transcription failed: download interrupted",
    ]


@pytest.mark.asyncio
async def test_audio_handler_uses_fixed_compatible_profile(monkeypatch, tmp_path):
    update, tg_file = _make_voice_update()
    context = SimpleNamespace(bot=object(), user_data={})
    profiles: list[str] = []
    forwarded: list[str] = []
    replies: list[str] = []

    monkeypatch.setattr(bot, "_AUDIO_DIR", tmp_path, raising=False)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(
        bot.session_manager,
        "get_machine_transcription_profile_selection",
        lambda machine_id="": (_ for _ in ()).throw(
            AssertionError("audio transcription should not consult machine profile state")
        ),
    )
    monkeypatch.setattr(bot, "begin_transcription_bootstrap", lambda profile="": None, raising=False)

    def _transcribe_audio_file(path: Path, *, profile: str = ""):
        assert path.exists()
        profiles.append(profile)
        return "voice transcript"

    async def _forward_topic_text_message(**kwargs):
        forwarded.append(kwargs["text"])

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "transcribe_audio_file", _transcribe_audio_file, raising=False)
    monkeypatch.setattr(bot, "safe_reply", _safe_reply)
    monkeypatch.setattr(
        bot,
        "_forward_topic_text_message",
        _forward_topic_text_message,
        raising=False,
    )

    await bot.audio_handler(update, context)

    assert len(tg_file.paths) == 1
    assert not tg_file.paths[0].exists()
    assert profiles == ["compatible"]
    assert replies == ["voice transcript"]
    assert forwarded == ["voice transcript"]
