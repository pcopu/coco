"""Tests for inbound Telegram document/PDF handling."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram.constants import ChatAction

import coco.bot as bot


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
async def test_document_handler_rejects_non_pdf_documents(monkeypatch, tmp_path):
    update, tg_file = _make_document_update(
        file_name="notes.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    context = SimpleNamespace(bot=object(), user_data={})
    replies: list[str] = []

    monkeypatch.setattr(bot, "_DOCUMENTS_DIR", tmp_path, raising=False)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    async def _unexpected_forward(**_kwargs):
        raise AssertionError("non-pdf document should not be forwarded")

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)
    monkeypatch.setattr(bot, "_forward_topic_text_message", _unexpected_forward, raising=False)

    await bot.document_handler(update, context)

    assert tg_file.paths == []
    assert replies == [
        "⚠ This media type is not supported yet. Send text, photos, voice notes, audio files, or PDF documents."
    ]
