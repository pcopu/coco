"""Memory-log hooks in safe Telegram send/edit helpers."""

from types import SimpleNamespace

import pytest

import coco.handlers.message_sender as message_sender


@pytest.mark.asyncio
async def test_send_with_fallback_logs_outgoing_send(monkeypatch):
    captured: list[dict[str, object]] = []

    def _capture(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(message_sender, "log_outgoing_send", _capture)

    class _Bot:
        async def send_message(self, **_kwargs):
            return SimpleNamespace(message_id=321)

    sent = await message_sender.send_with_fallback(
        _Bot(),
        chat_id=-1009,
        text="hello world",
        message_thread_id=77,
    )

    assert sent is not None
    assert sent.message_id == 321
    assert len(captured) == 1
    assert captured[0]["chat_id"] == -1009
    assert captured[0]["thread_id"] == 77
    assert captured[0]["text"] == "hello world"


@pytest.mark.asyncio
async def test_safe_edit_logs_outgoing_edit(monkeypatch):
    captured: list[dict[str, object]] = []

    def _capture(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(message_sender, "log_outgoing_edit", _capture)

    class _Target:
        def __init__(self) -> None:
            self.message = SimpleNamespace(
                chat_id=-10042,
                message_id=808,
                message_thread_id=11,
            )

        async def edit_message_text(self, *_args, **_kwargs):
            return True

    await message_sender.safe_edit(_Target(), "updated output")

    assert len(captured) == 1
    assert captured[0]["chat_id"] == -10042
    assert captured[0]["thread_id"] == 11
    assert captured[0]["message_id"] == 808
    assert captured[0]["text"] == "updated output"


@pytest.mark.asyncio
async def test_send_photo_falls_back_to_document_for_webp(monkeypatch):
    photo_attempts: list[dict[str, object]] = []
    document_sends: list[dict[str, object]] = []

    class _Bot:
        async def send_photo(self, **kwargs):
            photo_attempts.append(kwargs)
            raise RuntimeError("unsupported photo format")

        async def send_document(self, **kwargs):
            document_sends.append(kwargs)
            return SimpleNamespace(message_id=444)

    await message_sender.send_photo(
        _Bot(),
        chat_id=-1009,
        image_data=[("image/webp", b"WEBPDATA")],
        message_thread_id=77,
    )

    assert len(photo_attempts) == 1
    assert len(document_sends) == 1
    assert document_sends[0]["chat_id"] == -1009
    assert document_sends[0]["message_thread_id"] == 77
    assert document_sends[0]["filename"] == "image-1.webp"
    assert document_sends[0]["document"].getvalue() == b"WEBPDATA"
