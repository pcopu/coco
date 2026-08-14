"""Tests for inbound Telegram document handling."""

import io
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram.constants import ChatAction

import coco.bot as bot

_REMOTE_ATTACHMENT_REJECTION_TEXT = (
    "❌ Attachments to remote sessions are not supported yet. "
    "Send this attachment in a local topic."
)


class _FakeChat:
    type = "supergroup"
    id = -100123

    def __init__(self) -> None:
        self.actions: list[str] = []

    async def send_action(self, action: str) -> None:
        self.actions.append(action)


class _FakeTelegramFile:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.paths: list[Path] = []

    async def download_to_drive(self, path: Path) -> None:
        path.write_bytes(self.payload)
        self.paths.append(path)


class _FakeDocument:
    file_unique_id = "doc-123"

    def __init__(self, tg_file: _FakeTelegramFile, *, file_name: str, mime_type: str) -> None:
        self._tg_file = tg_file
        self.file_name = file_name
        self.mime_type = mime_type

    async def get_file(self):
        return self._tg_file


class _FakeVideo:
    file_unique_id = "video-123"

    def __init__(self, tg_file: _FakeTelegramFile, *, file_name: str, mime_type: str) -> None:
        self._tg_file = tg_file
        self.file_name = file_name
        self.mime_type = mime_type

    async def get_file(self):
        return self._tg_file


def _make_document_update(*, file_name: str, mime_type: str, caption: str | None = None):
    chat = _FakeChat()
    tg_file = _FakeTelegramFile(b"%PDF-1.7 test")
    message = SimpleNamespace(
        text=None,
        caption=caption,
        document=_FakeDocument(tg_file, file_name=file_name, mime_type=mime_type),
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


def _make_video_update(*, file_name: str, mime_type: str, caption: str | None = None):
    chat = _FakeChat()
    tg_file = _FakeTelegramFile(b"MP4 test")
    message = SimpleNamespace(
        text=None,
        caption=caption,
        video=_FakeVideo(tg_file, file_name=file_name, mime_type=mime_type),
        message_thread_id=77,
        chat=chat,
        chat_id=chat.id,
        message_id=998,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1147817421),
        effective_message=message,
        effective_chat=chat,
        message=message,
    )
    return update, tg_file


@pytest.mark.asyncio
async def test_document_handler_downloads_pdf_and_forwards_topic_text(monkeypatch, tmp_path):
    update, tg_file = _make_document_update(
        file_name="brochure.pdf",
        mime_type="application/pdf",
        caption="Use this",
    )
    context = SimpleNamespace(bot=object(), user_data={})
    forwarded: list[dict[str, object]] = []
    replies: list[str] = []

    monkeypatch.setattr(bot, "_DOCUMENTS_DIR", tmp_path, raising=False)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)

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

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)
    monkeypatch.setattr(
        bot,
        "_forward_topic_text_message",
        _forward_topic_text_message,
        raising=False,
    )

    await bot.document_handler(update, context)

    assert update.message.chat.actions == [ChatAction.TYPING]
    assert len(tg_file.paths) == 1
    saved_path = tg_file.paths[0]
    assert saved_path.exists()
    assert replies == []
    assert len(forwarded) == 1
    assert forwarded[0]["thread_id"] == 77
    assert forwarded[0]["chat_id"] == -100123
    assert "Use this" in forwarded[0]["text"]
    assert str(saved_path) in forwarded[0]["text"]


@pytest.mark.asyncio
async def test_document_handler_extracts_zip_and_forwards_directory(monkeypatch, tmp_path):
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as archive:
        archive.writestr("report/data.txt", "hello")

    update, tg_file = _make_document_update(
        file_name="bundle.zip",
        mime_type="application/zip",
        caption="Analyze this bundle",
    )
    tg_file.payload = zip_buffer.getvalue()
    context = SimpleNamespace(bot=object(), user_data={})
    forwarded: list[dict[str, object]] = []
    replies: list[str] = []

    monkeypatch.setattr(bot, "_DOCUMENTS_DIR", tmp_path, raising=False)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)

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

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)
    monkeypatch.setattr(bot, "_forward_topic_text_message", _forward_topic_text_message, raising=False)

    await bot.document_handler(update, context)

    assert update.message.chat.actions == [ChatAction.TYPING]
    assert len(tg_file.paths) == 1
    saved_zip = tg_file.paths[0]
    extracted_dir = tmp_path / f"{saved_zip.stem}_unpacked"
    assert saved_zip.exists()
    assert (extracted_dir / "report" / "data.txt").read_text(encoding="utf-8") == "hello"
    assert replies == []
    assert len(forwarded) == 1
    assert "Analyze this bundle" in forwarded[0]["text"]
    assert str(extracted_dir) in forwarded[0]["text"]
    assert str(saved_zip) in forwarded[0]["text"]


@pytest.mark.asyncio
async def test_document_handler_forwards_text_like_document(monkeypatch, tmp_path):
    update, tg_file = _make_document_update(
        file_name="brief.md",
        mime_type="text/markdown",
        caption="Use this note",
    )
    context = SimpleNamespace(bot=object(), user_data={})
    forwarded: list[dict[str, object]] = []
    replies: list[str] = []

    monkeypatch.setattr(bot, "_DOCUMENTS_DIR", tmp_path, raising=False)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)

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
        forwarded.append({"text": text, "thread_id": thread_id, "chat_id": chat_id})

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)
    monkeypatch.setattr(bot, "_forward_topic_text_message", _forward_topic_text_message, raising=False)

    await bot.document_handler(update, context)

    assert replies == []
    assert len(tg_file.paths) == 1
    assert len(forwarded) == 1
    assert "Use this note" in forwarded[0]["text"]
    assert str(tg_file.paths[0]) in forwarded[0]["text"]


@pytest.mark.asyncio
async def test_document_handler_forwards_office_document(monkeypatch, tmp_path):
    update, tg_file = _make_document_update(
        file_name="notes.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        caption="Review this",
    )
    context = SimpleNamespace(bot=object(), user_data={})
    forwarded: list[str] = []
    replies: list[str] = []

    monkeypatch.setattr(bot, "_DOCUMENTS_DIR", tmp_path, raising=False)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    async def _forward_topic_text_message(**kwargs):
        forwarded.append(kwargs["text"])

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)
    monkeypatch.setattr(bot, "_forward_topic_text_message", _forward_topic_text_message, raising=False)

    await bot.document_handler(update, context)

    assert replies == []
    assert len(tg_file.paths) == 1
    assert len(forwarded) == 1
    assert "Review this" in forwarded[0]
    assert str(tg_file.paths[0]) in forwarded[0]


@pytest.mark.asyncio
async def test_document_handler_forwards_image_document(monkeypatch, tmp_path):
    update, tg_file = _make_document_update(
        file_name="image.png",
        mime_type="image/png",
        caption="Look at this image",
    )
    context = SimpleNamespace(bot=object(), user_data={})
    forwarded: list[str] = []
    replies: list[str] = []

    monkeypatch.setattr(bot, "_DOCUMENTS_DIR", tmp_path, raising=False)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    async def _forward_topic_text_message(**kwargs):
        forwarded.append(kwargs["text"])

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)
    monkeypatch.setattr(bot, "_forward_topic_text_message", _forward_topic_text_message, raising=False)

    await bot.document_handler(update, context)

    assert replies == []
    assert len(tg_file.paths) == 1
    assert len(forwarded) == 1
    assert "Look at this image" in forwarded[0]
    assert str(tg_file.paths[0]) in forwarded[0]
    assert "(image attached:" in forwarded[0]


@pytest.mark.asyncio
async def test_document_handler_forwards_audio_document_as_file_path(monkeypatch, tmp_path):
    update, tg_file = _make_document_update(
        file_name="meditation.wav",
        mime_type="audio/wav",
        caption="Use this audio",
    )
    context = SimpleNamespace(bot=object(), user_data={})
    forwarded: list[str] = []
    replies: list[str] = []

    monkeypatch.setattr(bot, "_DOCUMENTS_DIR", tmp_path, raising=False)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    async def _forward_topic_text_message(**kwargs):
        forwarded.append(kwargs["text"])

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)
    monkeypatch.setattr(bot, "_forward_topic_text_message", _forward_topic_text_message, raising=False)

    await bot.document_handler(update, context)

    assert tg_file.paths[0].exists() is True
    assert replies == []
    assert len(forwarded) == 1
    assert "Use this audio" in forwarded[0]
    assert str(tg_file.paths[0]) in forwarded[0]


@pytest.mark.asyncio
async def test_document_handler_extracts_tgz_and_forwards_directory(monkeypatch, tmp_path):
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w:gz") as archive:
        payload = b"hello tar"
        info = tarfile.TarInfo("bundle/data.txt")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))

    update, tg_file = _make_document_update(
        file_name="bundle.tgz",
        mime_type="application/gzip",
        caption="Use tgz",
    )
    tg_file.payload = tar_buffer.getvalue()
    context = SimpleNamespace(bot=object(), user_data={})
    forwarded: list[str] = []
    replies: list[str] = []

    monkeypatch.setattr(bot, "_DOCUMENTS_DIR", tmp_path, raising=False)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    async def _forward_topic_text_message(**kwargs):
        forwarded.append(kwargs["text"])

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)
    monkeypatch.setattr(bot, "_forward_topic_text_message", _forward_topic_text_message, raising=False)

    await bot.document_handler(update, context)

    saved_archive = tg_file.paths[0]
    extracted_dir = tmp_path / f"{saved_archive.stem}_unpacked"
    assert replies == []
    assert (extracted_dir / "bundle" / "data.txt").read_text(encoding="utf-8") == "hello tar"
    assert len(forwarded) == 1
    assert str(extracted_dir) in forwarded[0]
    assert str(saved_archive) in forwarded[0]


@pytest.mark.asyncio
async def test_document_handler_rejects_unsupported_document(monkeypatch, tmp_path):
    update, tg_file = _make_document_update(
        file_name="malware.exe",
        mime_type="application/octet-stream",
    )
    context = SimpleNamespace(bot=object(), user_data={})
    replies: list[str] = []

    monkeypatch.setattr(bot, "_DOCUMENTS_DIR", tmp_path, raising=False)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    async def _unexpected_forward(**_kwargs):
        raise AssertionError("unsupported document should not be forwarded")

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)
    monkeypatch.setattr(bot, "_forward_topic_text_message", _unexpected_forward, raising=False)

    await bot.document_handler(update, context)

    assert tg_file.paths == []
    assert replies == [
        "⚠ This file type is not supported yet. Send text, photos, voice notes, audio files, supported documents, or supported archives."
    ]


@pytest.mark.asyncio
async def test_document_handler_rejects_unconfigured_general_before_download(
    monkeypatch,
    tmp_path,
):
    update, tg_file = _make_document_update(
        file_name="brief.md",
        mime_type="text/markdown",
    )
    update.message.message_thread_id = 1
    context = SimpleNamespace(bot=object(), user_data={})
    replies: list[str] = []
    routing_users: list[int] = []

    monkeypatch.setattr(bot, "_DOCUMENTS_DIR", tmp_path, raising=False)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda uid, *_args, **_kwargs: routing_users.append(uid),
    )

    async def _reply(_message, text, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _reply)

    await bot.document_handler(update, context)

    assert tg_file.paths == []
    assert replies == [bot._COCO_CONTROL_UNCONFIGURED_TEXT]
    assert routing_users == []


def _install_remote_general_attachment_routing(monkeypatch, *, owner_user_id: int):
    """Route one attachment update to a remote General control binding."""
    remote_ownership = bot.TopicOwnership(
        window_id="@remote-general",
        codex_thread_id="codex-remote-general",
        machine_id="remote-node",
        cwd="/remote/workspace",
    )
    chat_id = -100123
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: True)
    monkeypatch.setattr(bot, "_can_coco_control_target", lambda **_kwargs: True)
    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(bot.session_manager, "set_group_chat_id", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(owner_user_id, 1, chat_id),
    )
    monkeypatch.setattr(bot, "capture_topic_ownership", lambda *_args, **_kwargs: remote_ownership)
    monkeypatch.setattr(bot, "_local_machine_identity", lambda: ("controller-node", "Controller"))
    return remote_ownership


@pytest.mark.asyncio
async def test_document_handler_rejects_remote_general_before_download(monkeypatch, tmp_path):
    update, _tg_file = _make_document_update(
        file_name="brief.pdf",
        mime_type="application/pdf",
    )
    update.message.message_thread_id = 1
    context = SimpleNamespace(bot=object(), user_data={})
    replies: list[str] = []

    _install_remote_general_attachment_routing(monkeypatch, owner_user_id=1147817421)
    monkeypatch.setattr(bot, "_DOCUMENTS_DIR", tmp_path, raising=False)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.document_handler(update, context)

    assert _tg_file.paths == []
    assert replies == [_REMOTE_ATTACHMENT_REJECTION_TEXT]


@pytest.mark.asyncio
async def test_archive_handler_rejects_remote_general_before_download(monkeypatch, tmp_path):
    update, _tg_file = _make_document_update(
        file_name="bundle.zip",
        mime_type="application/zip",
    )
    update.message.message_thread_id = 1
    context = SimpleNamespace(bot=object(), user_data={})
    replies: list[str] = []

    _install_remote_general_attachment_routing(monkeypatch, owner_user_id=1147817421)
    monkeypatch.setattr(bot, "_DOCUMENTS_DIR", tmp_path, raising=False)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.document_handler(update, context)

    assert _tg_file.paths == []
    assert replies == [_REMOTE_ATTACHMENT_REJECTION_TEXT]


@pytest.mark.asyncio
async def test_video_handler_downloads_video_and_forwards_topic_text(monkeypatch, tmp_path):
    update, tg_file = _make_video_update(
        file_name="clip.mp4",
        mime_type="video/mp4",
        caption="Please review this",
    )
    context = SimpleNamespace(bot=object(), user_data={})
    forwarded: list[dict[str, object]] = []
    replies: list[str] = []

    monkeypatch.setattr(bot, "_VIDEOS_DIR", tmp_path, raising=False)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)

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

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)
    monkeypatch.setattr(
        bot,
        "_forward_topic_text_message",
        _forward_topic_text_message,
        raising=False,
    )

    await bot.video_handler(update, context)

    assert update.message.chat.actions == [ChatAction.TYPING]
    assert len(tg_file.paths) == 1
    saved_path = tg_file.paths[0]
    assert saved_path.exists()
    assert replies == []
    assert len(forwarded) == 1
    assert forwarded[0]["thread_id"] == 77
    assert forwarded[0]["chat_id"] == -100123
    assert "Please review this" in forwarded[0]["text"]
    assert str(saved_path) in forwarded[0]["text"]


@pytest.mark.asyncio
async def test_video_handler_rejects_unconfigured_general_before_download(
    monkeypatch,
    tmp_path,
):
    update, tg_file = _make_video_update(
        file_name="clip.mp4",
        mime_type="video/mp4",
    )
    update.message.message_thread_id = 1
    context = SimpleNamespace(bot=object(), user_data={})
    replies: list[str] = []
    routing_users: list[int] = []

    monkeypatch.setattr(bot, "_VIDEOS_DIR", tmp_path, raising=False)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda uid, *_args, **_kwargs: routing_users.append(uid),
    )

    async def _reply(_message, text, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _reply)

    await bot.video_handler(update, context)

    assert tg_file.paths == []
    assert replies == [bot._COCO_CONTROL_UNCONFIGURED_TEXT]
    assert routing_users == []


@pytest.mark.asyncio
async def test_video_handler_rejects_remote_general_before_download(monkeypatch, tmp_path):
    update, _tg_file = _make_video_update(
        file_name="clip.mp4",
        mime_type="video/mp4",
    )
    update.message.message_thread_id = 1
    context = SimpleNamespace(bot=object(), user_data={})
    replies: list[str] = []

    _install_remote_general_attachment_routing(monkeypatch, owner_user_id=1147817421)
    monkeypatch.setattr(bot, "_VIDEOS_DIR", tmp_path, raising=False)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.video_handler(update, context)

    assert _tg_file.paths == []
    assert replies == [_REMOTE_ATTACHMENT_REJECTION_TEXT]


_PENDING_STEER_CALLER_ID = 1147817421
_PENDING_STEER_CONTROL_OWNER_ID = 9001


def _pending_dashboard_steer_context(
    *,
    owner_user_id: int = _PENDING_STEER_CALLER_ID,
    thread_id: int = 77,
    machine_id: str = "controller-node",
    window_id: str = "@target",
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
                    "window_id": window_id,
                    "codex_thread_id": "codex-target",
                    "machine_id": machine_id,
                    "cwd": "/target/workspace",
                },
            }
        },
    )


def _install_pending_dashboard_attachment_routing(
    monkeypatch,
    *,
    context,
    control_owner_user_id: int,
    canonical_ownership: bot.TopicOwnership,
):
    """Install General control state while retaining a pending target steer."""
    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(bot.session_manager, "set_group_chat_id", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(
            control_owner_user_id,
            bot.GENERAL_TOPIC_THREAD_ID,
            -100123,
        ),
    )
    monkeypatch.setattr(
        bot,
        "_can_coco_control_target",
        lambda *, caller_user_id, target_user_id, **_kwargs: int(caller_user_id)
        == int(target_user_id),
    )
    monkeypatch.setattr(
        bot,
        "capture_topic_ownership",
        lambda *_args, **_kwargs: canonical_ownership,
    )
    monkeypatch.setattr(
        bot,
        "is_topic_ownership_current",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        bot,
        "_local_machine_identity",
        lambda: ("controller-node", "Controller"),
    )
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    return context


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("file_name", "mime_type"),
    [
        ("brief.pdf", "application/pdf"),
        ("bundle.zip", "application/zip"),
        ("image.png", "image/png"),
        ("meditation.wav", "audio/wav"),
        ("clip.mp4", "video/mp4"),
    ],
)
async def test_document_handler_pending_steer_uses_caller_owned_local_target(
    monkeypatch,
    tmp_path,
    file_name,
    mime_type,
):
    """A General attachment can steer the caller's topic despite another owner."""
    update, tg_file = _make_document_update(
        file_name=file_name,
        mime_type=mime_type,
        caption="Use this target",
    )
    update.message.message_thread_id = bot.GENERAL_TOPIC_THREAD_ID
    if file_name.endswith(".zip"):
        archive_buffer = io.BytesIO()
        with zipfile.ZipFile(archive_buffer, "w") as archive:
            archive.writestr("report/data.txt", "hello")
        tg_file.payload = archive_buffer.getvalue()
    context = _pending_dashboard_steer_context()
    canonical_ownership = bot.TopicOwnership(
        window_id="@remote-general",
        codex_thread_id="codex-general",
        machine_id="remote-node",
        cwd="/remote/workspace",
    )
    _install_pending_dashboard_attachment_routing(
        monkeypatch,
        context=context,
        control_owner_user_id=_PENDING_STEER_CONTROL_OWNER_ID,
        canonical_ownership=canonical_ownership,
    )
    forwarded: list[dict[str, object]] = []
    replies: list[str] = []
    monkeypatch.setattr(bot, "_DOCUMENTS_DIR", tmp_path, raising=False)

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    async def _forward_topic_text_message(**kwargs):
        forwarded.append(kwargs)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)
    monkeypatch.setattr(bot, "_forward_topic_text_message", _forward_topic_text_message)

    await bot.document_handler(update, context)

    assert tg_file.paths
    assert replies == []
    assert len(forwarded) == 1
    assert forwarded[0]["user_id"] == _PENDING_STEER_CALLER_ID
    assert forwarded[0]["thread_id"] == 77
    assert forwarded[0]["pending_steer_target"].thread_id == 77
    assert forwarded[0]["pending_steer_target"].owner_user_id == _PENDING_STEER_CALLER_ID


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("file_name", "mime_type"),
    [
        ("brief.pdf", "application/pdf"),
        ("bundle.zip", "application/zip"),
        ("image.png", "image/png"),
        ("meditation.wav", "audio/wav"),
        ("clip.mp4", "video/mp4"),
    ],
)
async def test_document_handler_pending_steer_rejects_remote_target_before_download(
    monkeypatch,
    tmp_path,
    file_name,
    mime_type,
):
    """A pending steer must reject a caller-owned target on another machine."""
    update, tg_file = _make_document_update(
        file_name=file_name,
        mime_type=mime_type,
    )
    update.message.message_thread_id = bot.GENERAL_TOPIC_THREAD_ID
    context = _pending_dashboard_steer_context(machine_id="remote-node")
    canonical_ownership = bot.TopicOwnership(
        window_id="@general",
        codex_thread_id="codex-general",
        machine_id="controller-node",
        cwd="/controller/workspace",
    )
    _install_pending_dashboard_attachment_routing(
        monkeypatch,
        context=context,
        control_owner_user_id=_PENDING_STEER_CALLER_ID,
        canonical_ownership=canonical_ownership,
    )
    replies: list[str] = []
    forwarded: list[dict[str, object]] = []
    monkeypatch.setattr(bot, "_DOCUMENTS_DIR", tmp_path, raising=False)

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    async def _forward_topic_text_message(**kwargs):
        forwarded.append(kwargs)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)
    monkeypatch.setattr(bot, "_forward_topic_text_message", _forward_topic_text_message)

    await bot.document_handler(update, context)

    assert tg_file.paths == []
    assert forwarded == []
    assert replies == [_REMOTE_ATTACHMENT_REJECTION_TEXT]
    assert "_coco_dashboard_steer" not in context.user_data


@pytest.mark.asyncio
async def test_video_handler_pending_steer_uses_caller_owned_local_target(
    monkeypatch,
    tmp_path,
):
    """A General video can steer the caller's local topic despite another owner."""
    update, tg_file = _make_video_update(
        file_name="clip.mp4",
        mime_type="video/mp4",
        caption="Use this target",
    )
    update.message.message_thread_id = bot.GENERAL_TOPIC_THREAD_ID
    context = _pending_dashboard_steer_context()
    canonical_ownership = bot.TopicOwnership(
        window_id="@remote-general",
        codex_thread_id="codex-general",
        machine_id="remote-node",
        cwd="/remote/workspace",
    )
    _install_pending_dashboard_attachment_routing(
        monkeypatch,
        context=context,
        control_owner_user_id=_PENDING_STEER_CONTROL_OWNER_ID,
        canonical_ownership=canonical_ownership,
    )
    forwarded: list[dict[str, object]] = []
    replies: list[str] = []
    monkeypatch.setattr(bot, "_VIDEOS_DIR", tmp_path, raising=False)

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    async def _forward_topic_text_message(**kwargs):
        forwarded.append(kwargs)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)
    monkeypatch.setattr(bot, "_forward_topic_text_message", _forward_topic_text_message)

    await bot.video_handler(update, context)

    assert tg_file.paths
    assert replies == []
    assert len(forwarded) == 1
    assert forwarded[0]["user_id"] == _PENDING_STEER_CALLER_ID
    assert forwarded[0]["thread_id"] == 77
    assert forwarded[0]["pending_steer_target"].thread_id == 77
    assert forwarded[0]["pending_steer_target"].owner_user_id == _PENDING_STEER_CALLER_ID


@pytest.mark.asyncio
async def test_video_handler_pending_steer_rejects_remote_target_before_download(
    monkeypatch,
    tmp_path,
):
    """A pending steer must reject a remote video before downloading it."""
    update, tg_file = _make_video_update(
        file_name="clip.mp4",
        mime_type="video/mp4",
    )
    update.message.message_thread_id = bot.GENERAL_TOPIC_THREAD_ID
    context = _pending_dashboard_steer_context(machine_id="remote-node")
    canonical_ownership = bot.TopicOwnership(
        window_id="@general",
        codex_thread_id="codex-general",
        machine_id="controller-node",
        cwd="/controller/workspace",
    )
    _install_pending_dashboard_attachment_routing(
        monkeypatch,
        context=context,
        control_owner_user_id=_PENDING_STEER_CALLER_ID,
        canonical_ownership=canonical_ownership,
    )
    replies: list[str] = []
    forwarded: list[dict[str, object]] = []
    monkeypatch.setattr(bot, "_VIDEOS_DIR", tmp_path, raising=False)

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    async def _forward_topic_text_message(**kwargs):
        forwarded.append(kwargs)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)
    monkeypatch.setattr(bot, "_forward_topic_text_message", _forward_topic_text_message)

    await bot.video_handler(update, context)

    assert tg_file.paths == []
    assert forwarded == []
    assert replies == [_REMOTE_ATTACHMENT_REJECTION_TEXT]
    assert "_coco_dashboard_steer" not in context.user_data


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
async def test_document_handler_pending_steer_stale_or_expired_fails_closed_before_download(
    monkeypatch,
    tmp_path,
    created_at,
    ownership_current,
    expected_reply,
):
    """Matching stale/expired document steers are consumed before download."""
    update, tg_file = _make_document_update(
        file_name="brief.pdf",
        mime_type="application/pdf",
    )
    update.message.message_thread_id = bot.GENERAL_TOPIC_THREAD_ID
    context = _pending_dashboard_steer_context(created_at=created_at)
    canonical_ownership = bot.TopicOwnership(
        window_id="@general",
        codex_thread_id="codex-general",
        machine_id="controller-node",
        cwd="/controller/workspace",
    )
    _install_pending_dashboard_attachment_routing(
        monkeypatch,
        context=context,
        control_owner_user_id=_PENDING_STEER_CALLER_ID,
        canonical_ownership=canonical_ownership,
    )
    monkeypatch.setattr(
        bot,
        "is_topic_ownership_current",
        lambda *_args, **_kwargs: ownership_current,
    )
    monkeypatch.setattr(bot, "_DOCUMENTS_DIR", tmp_path, raising=False)
    replies: list[str] = []
    forwarded: list[dict[str, object]] = []

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    async def _forward_topic_text_message(**kwargs):
        forwarded.append(kwargs)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)
    monkeypatch.setattr(bot, "_forward_topic_text_message", _forward_topic_text_message)

    await bot.document_handler(update, context)

    assert tg_file.paths == []
    assert forwarded == []
    assert replies == [expected_reply]
    assert "_coco_dashboard_steer" not in context.user_data


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["get_file", "download"])
async def test_document_handler_pending_steer_consumes_before_download_failure(
    monkeypatch,
    tmp_path,
    failure_stage,
):
    """A failed Telegram document attempt cannot arm the next General text."""
    update, tg_file = _make_document_update(
        file_name="brief.pdf",
        mime_type="application/pdf",
    )
    update.message.message_thread_id = bot.GENERAL_TOPIC_THREAD_ID
    context = _pending_dashboard_steer_context()
    _install_pending_dashboard_attachment_routing(
        monkeypatch,
        context=context,
        control_owner_user_id=_PENDING_STEER_CONTROL_OWNER_ID,
        canonical_ownership=bot.TopicOwnership(
            window_id="@general",
            codex_thread_id="codex-general",
            machine_id="controller-node",
            cwd="/controller/workspace",
        ),
    )
    monkeypatch.setattr(bot, "_DOCUMENTS_DIR", tmp_path, raising=False)
    replies: list[str] = []

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)
    if failure_stage == "get_file":
        async def _fail_get_file():
            raise RuntimeError("Telegram get_file failed")

        update.message.document.get_file = _fail_get_file
    else:
        async def _fail_download(_path):
            raise RuntimeError("Telegram download failed")

        tg_file.download_to_drive = _fail_download

    with pytest.raises(RuntimeError, match="Telegram"):
        await bot.document_handler(update, context)

    assert "_coco_dashboard_steer" not in context.user_data
    assert replies == []


@pytest.mark.asyncio
async def test_document_handler_pending_steer_consumes_before_archive_extraction_failure(
    monkeypatch,
    tmp_path,
):
    """A valid archive steer is consumed before fallible extraction."""
    update, tg_file = _make_document_update(
        file_name="bundle.zip",
        mime_type="application/zip",
    )
    update.message.message_thread_id = bot.GENERAL_TOPIC_THREAD_ID
    tg_file.payload = b"not a zip archive"
    context = _pending_dashboard_steer_context()
    _install_pending_dashboard_attachment_routing(
        monkeypatch,
        context=context,
        control_owner_user_id=_PENDING_STEER_CONTROL_OWNER_ID,
        canonical_ownership=bot.TopicOwnership(
            window_id="@general",
            codex_thread_id="codex-general",
            machine_id="controller-node",
            cwd="/controller/workspace",
        ),
    )
    monkeypatch.setattr(bot, "_DOCUMENTS_DIR", tmp_path, raising=False)
    replies: list[str] = []
    forwarded: list[dict[str, object]] = []

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    async def _forward_topic_text_message(**kwargs):
        forwarded.append(kwargs)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)
    monkeypatch.setattr(bot, "_forward_topic_text_message", _forward_topic_text_message)

    await bot.document_handler(update, context)

    assert "_coco_dashboard_steer" not in context.user_data
    assert forwarded == []
    assert replies and replies[0].startswith("❌ Archive extraction failed:")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["get_file", "download"])
async def test_video_handler_pending_steer_consumes_before_download_failure(
    monkeypatch,
    tmp_path,
    failure_stage,
):
    """A failed Telegram video attempt cannot arm the next General text."""
    update, tg_file = _make_video_update(
        file_name="clip.mp4",
        mime_type="video/mp4",
    )
    update.message.message_thread_id = bot.GENERAL_TOPIC_THREAD_ID
    context = _pending_dashboard_steer_context()
    _install_pending_dashboard_attachment_routing(
        monkeypatch,
        context=context,
        control_owner_user_id=_PENDING_STEER_CONTROL_OWNER_ID,
        canonical_ownership=bot.TopicOwnership(
            window_id="@general",
            codex_thread_id="codex-general",
            machine_id="controller-node",
            cwd="/controller/workspace",
        ),
    )
    monkeypatch.setattr(bot, "_VIDEOS_DIR", tmp_path, raising=False)
    replies: list[str] = []

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)
    if failure_stage == "get_file":
        async def _fail_get_file():
            raise RuntimeError("Telegram get_file failed")

        update.message.video.get_file = _fail_get_file
    else:
        async def _fail_download(_path):
            raise RuntimeError("Telegram download failed")

        tg_file.download_to_drive = _fail_download

    with pytest.raises(RuntimeError, match="Telegram"):
        await bot.video_handler(update, context)

    assert "_coco_dashboard_steer" not in context.user_data
    assert replies == []
