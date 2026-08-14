"""Tests for local Telegram audio transcription ingress."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram.constants import ChatAction
from telegram.error import NetworkError

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


def _pending_dashboard_steer_context(
    *,
    owner_user_id: int = 1147817421,
    thread_id: int = 77,
    created_at: float | None = None,
):
    return SimpleNamespace(
        bot=object(),
        user_data={
            "_coco_dashboard_steer": {
                "owner_user_id": owner_user_id,
                "chat_id": -100123,
                "thread_id": thread_id,
                "created_at": (
                    bot.time.monotonic() if created_at is None else created_at
                ),
                "ownership": {
                    "window_id": "@target",
                    "codex_thread_id": "codex-target",
                    "machine_id": "controller-node",
                    "cwd": "/target/workspace",
                },
            }
        },
    )


def _install_pending_dashboard_voice_routing(monkeypatch):
    """Keep General owned by another user while allowing the pending target."""
    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(bot.session_manager, "set_group_chat_id", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(9001, bot.GENERAL_TOPIC_THREAD_ID, -100123),
    )
    monkeypatch.setattr(
        bot,
        "_can_coco_control_target",
        lambda *, caller_user_id, target_user_id, **_kwargs: int(caller_user_id)
        == int(target_user_id),
    )
    monkeypatch.setattr(bot, "is_topic_ownership_current", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        bot.session_manager,
        "is_coco_control_topic",
        lambda *_args, **_kwargs: False,
    )


@pytest.mark.asyncio
async def test_audio_handler_pending_steer_resolves_self_target_before_general_owner(
    monkeypatch,
    tmp_path,
):
    """A caller-owned pending voice steer bypasses another user's General owner gate."""
    update, tg_file = _make_voice_update()
    update.message.message_thread_id = bot.GENERAL_TOPIC_THREAD_ID
    context = _pending_dashboard_steer_context()
    _install_pending_dashboard_voice_routing(monkeypatch)
    monkeypatch.setattr(bot, "_AUDIO_DIR", tmp_path, raising=False)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(bot, "begin_transcription_bootstrap", lambda profile="": None)
    monkeypatch.setattr(
        bot,
        "transcribe_audio_file",
        lambda _path, *, profile="": "pending voice steer",
    )
    replies: list[str] = []
    forwarded: list[dict[str, object]] = []

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    async def _forward_topic_text_message(**kwargs):
        forwarded.append(kwargs)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)
    monkeypatch.setattr(bot, "_forward_topic_text_message", _forward_topic_text_message)

    await bot.audio_handler(update, context)

    assert "_coco_dashboard_steer" not in context.user_data
    assert replies == []
    assert len(forwarded) == 1
    assert forwarded[0]["user_id"] == 1147817421
    assert forwarded[0]["thread_id"] == 77
    assert forwarded[0]["pending_steer_target"].thread_id == 77
    assert forwarded[0]["response_mode"] == "voice"
    assert "pending voice steer" in forwarded[0]["text"]
    assert len(tg_file.paths) == 1


@pytest.mark.asyncio
async def test_audio_handler_pending_steer_consumes_when_transcription_fails(
    monkeypatch,
    tmp_path,
):
    """A failed pending voice transcription cannot capture a later text message."""
    update, tg_file = _make_voice_update()
    update.message.message_thread_id = bot.GENERAL_TOPIC_THREAD_ID
    context = _pending_dashboard_steer_context()
    _install_pending_dashboard_voice_routing(monkeypatch)
    monkeypatch.setattr(bot, "_AUDIO_DIR", tmp_path, raising=False)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(bot, "begin_transcription_bootstrap", lambda profile="": None)

    def _fail_transcription(_path, *, profile=""):
        raise RuntimeError("transcription failed")

    monkeypatch.setattr(bot, "transcribe_audio_file", _fail_transcription)
    replies: list[str] = []

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.audio_handler(update, context)

    assert len(tg_file.paths) == 1
    assert "_coco_dashboard_steer" not in context.user_data
    assert replies == ["❌ Audio transcription failed: transcription failed"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("created_at", "ownership_current", "expected_reply"),
    [
        (
            bot.time.monotonic() - bot._COCO_DASHBOARD_STEER_TTL_SECONDS - 1,
            True,
            bot._COCO_DASHBOARD_STEER_EXPIRED_TEXT,
        ),
        (
            bot.time.monotonic(),
            False,
            "❌ That dashboard target changed. Refresh /coco and try again.",
        ),
    ],
)
async def test_audio_handler_pending_steer_stale_or_expired_skips_transcription(
    monkeypatch,
    tmp_path,
    created_at,
    ownership_current,
    expected_reply,
):
    """Stale/expired pending voice steers are consumed before media work."""
    update, tg_file = _make_voice_update()
    update.message.message_thread_id = bot.GENERAL_TOPIC_THREAD_ID
    context = _pending_dashboard_steer_context(created_at=created_at)
    _install_pending_dashboard_voice_routing(monkeypatch)
    monkeypatch.setattr(bot, "is_topic_ownership_current", lambda *_args, **_kwargs: ownership_current)
    monkeypatch.setattr(bot, "_AUDIO_DIR", tmp_path, raising=False)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(
        bot,
        "get_default_transcription_profile",
        lambda: pytest.fail("stale/expired voice must not transcribe"),
    )
    replies: list[str] = []

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.audio_handler(update, context)

    assert tg_file.paths == []
    assert "_coco_dashboard_steer" not in context.user_data
    assert replies == [expected_reply]


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_echo", [False, True])
async def test_audio_handler_transcribes_voice_and_forwards_topic_text(
    monkeypatch, tmp_path, fail_echo
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
        if fail_echo:
            raise NetworkError("telegram echo failed")
        replies.append(text)

    async def _forward_topic_text_message(
        *,
        message,
        context,
        user_id: int,
        thread_id: int | None,
        chat_id: int | None,
        text: str,
        response_mode: str = "",
        persist_response_mode: bool = True,
    ) -> None:
        assert replies == ([] if fail_echo else ["voice transcript"])
        assert response_mode == "voice"
        assert persist_response_mode is True
        forwarded.append(
            {
                "message": message,
                "context": context,
                "user_id": user_id,
                "thread_id": thread_id,
                "chat_id": chat_id,
                "text": text,
                "response_mode": response_mode,
                "persist_response_mode": persist_response_mode,
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
    assert replies == ([] if fail_echo else ["voice transcript"])
    assert forwarded == [
        {
            "message": update.message,
            "context": context,
            "user_id": 1147817421,
            "thread_id": 77,
            "chat_id": -100123,
            "text": "voice transcript",
            "response_mode": "voice",
            "persist_response_mode": True,
        }
    ]


@pytest.mark.asyncio
async def test_audio_handler_coco_control_voice_skips_transcript_echo_and_injects_control_prompt(
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
    monkeypatch.setattr(
        bot,
        "transcribe_audio_file",
        lambda _path, *, profile="": "what is happening in bottleshot",
        raising=False,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "is_coco_control_topic",
        lambda _uid, tid, *, chat_id=None: tid == 77 and chat_id == -100123,
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    async def _forward_topic_text_message(**kwargs):
        forwarded.append(kwargs)

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
    assert replies == []
    assert len(forwarded) == 1
    assert forwarded[0]["response_mode"] == "voice"
    assert forwarded[0]["persist_response_mode"] is False
    forwarded_text = str(forwarded[0]["text"])
    assert "[coco voice note]" in forwarded_text
    assert "Prefer a concise spoken control-room reply." in forwarded_text
    assert forwarded_text.endswith("what is happening in bottleshot")


@pytest.mark.asyncio
async def test_audio_handler_activates_general_control_before_classifying_voice(
    monkeypatch,
    tmp_path,
):
    update, tg_file = _make_voice_update()
    update.message.message_thread_id = None
    update.effective_chat.is_forum = True
    context = SimpleNamespace(bot=object(), user_data={})
    activated = False
    replies: list[str] = []
    forwarded: list[dict[str, object]] = []

    monkeypatch.setattr(bot, "_AUDIO_DIR", tmp_path, raising=False)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(
        bot,
        "begin_transcription_bootstrap",
        lambda profile="": None,
        raising=False,
    )
    monkeypatch.setattr(
        bot,
        "transcribe_audio_file",
        lambda _path, *, profile="": "control status",
        raising=False,
    )

    def _activate(**_kwargs):
        nonlocal activated
        activated = True

    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", _activate)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(1147817421, 1, -100123),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "is_coco_control_topic",
        lambda _uid, tid, *, chat_id=None: (
            activated and tid == 1 and chat_id == -100123
        ),
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    async def _forward(**kwargs):
        forwarded.append(kwargs)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)
    monkeypatch.setattr(bot, "_forward_topic_text_message", _forward)

    await bot.audio_handler(update, context)

    assert len(tg_file.paths) == 1
    assert activated is True
    assert replies == []
    assert forwarded[0]["thread_id"] == 1
    assert forwarded[0]["persist_response_mode"] is False
    assert "[coco voice note]" in str(forwarded[0]["text"])


@pytest.mark.asyncio
async def test_audio_handler_rejects_unconfigured_general_before_download(
    monkeypatch,
    tmp_path,
):
    update, tg_file = _make_voice_update()
    update.message.message_thread_id = 1
    update.effective_chat.is_forum = True
    context = SimpleNamespace(bot=object(), user_data={})
    replies: list[str] = []
    routing_users: list[int] = []

    monkeypatch.setattr(bot, "_AUDIO_DIR", tmp_path, raising=False)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda uid, *_args, **_kwargs: routing_users.append(uid),
    )
    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: None,
    )
    monkeypatch.setattr(
        bot,
        "get_default_transcription_profile",
        lambda: pytest.fail("unconfigured General audio must not transcribe"),
    )

    async def _reply(_message, text, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _reply)

    await bot.audio_handler(update, context)

    assert tg_file.paths == []
    assert replies == [bot._COCO_CONTROL_UNCONFIGURED_TEXT]
    assert routing_users == []


@pytest.mark.asyncio
async def test_authorized_admin_general_voice_uses_canonical_control_owner(
    monkeypatch,
    tmp_path,
):
    update, tg_file = _make_voice_update()
    owner_user_id = 100
    admin_user_id = 200
    update.effective_user.id = admin_user_id
    update.message.message_thread_id = None
    update.effective_chat.is_forum = True
    context = SimpleNamespace(bot=object(), user_data={})
    replies: list[str] = []
    forwarded: list[dict[str, object]] = []

    monkeypatch.setattr(bot, "_AUDIO_DIR", tmp_path, raising=False)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda uid: uid == admin_user_id)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(
        bot,
        "_ensure_default_coco_general_control",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(owner_user_id, 1, -100123),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "is_coco_control_topic",
        lambda uid, _thread_id, **_kwargs: uid == owner_user_id,
    )
    monkeypatch.setattr(
        bot,
        "transcribe_audio_file",
        lambda _path, *, profile="": "canonical control transcript",
        raising=False,
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    async def _forward_topic_text_message(**kwargs):
        forwarded.append(kwargs)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)
    monkeypatch.setattr(bot, "_forward_topic_text_message", _forward_topic_text_message)

    await bot.audio_handler(update, context)

    assert len(tg_file.paths) == 1
    assert replies == []
    assert forwarded
    assert forwarded[0]["persist_response_mode"] is False
    assert "[coco voice note]" in str(forwarded[0]["text"])


@pytest.mark.asyncio
async def test_single_session_user_cannot_upload_audio_to_general_control(
    monkeypatch,
    tmp_path,
):
    update, tg_file = _make_voice_update()
    update.effective_user.id = 999
    update.message.message_thread_id = None
    update.effective_chat.is_forum = True
    context = SimpleNamespace(bot=object(), user_data={})
    replies: list[str] = []

    monkeypatch.setattr(bot, "_AUDIO_DIR", tmp_path, raising=False)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: False)
    monkeypatch.setattr(bot, "_get_user_scope", lambda _uid: bot.SCOPE_SINGLE_SESSION)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(bot.session_manager, "set_group_chat_id", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(100, 1, -100123),
    )

    async def _reply(_message, text, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _reply)

    await bot.audio_handler(update, context)

    assert tg_file.paths == []
    assert replies == [
        "❌ Only the CoCo control owner or an admin can control another user's topic."
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


@pytest.mark.asyncio
async def test_audio_handler_splits_long_transcript_and_still_forwards(
    monkeypatch, tmp_path
):
    update, tg_file = _make_voice_update()
    context = SimpleNamespace(bot=object(), user_data={})
    forwarded: list[str] = []
    replies: list[str] = []
    long_transcript = ("x" * 2500) + "\n" + ("y" * 2500)

    monkeypatch.setattr(bot, "_AUDIO_DIR", tmp_path, raising=False)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(
        bot,
        "begin_transcription_bootstrap",
        lambda profile="": None,
        raising=False,
    )
    monkeypatch.setattr(
        bot,
        "transcribe_audio_file",
        lambda _path, *, profile="": long_transcript,
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
    assert replies == [("x" * 2500), ("y" * 2500)]
    assert forwarded == [long_transcript]
