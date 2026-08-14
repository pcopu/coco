"""Memory-log hooks in safe Telegram send/edit helpers."""

from types import SimpleNamespace

import pytest
from telegram.error import BadRequest, NetworkError

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
async def test_safe_send_omits_general_topic_id_but_logs_internal_topic(monkeypatch):
    sent_kwargs: list[dict[str, object]] = []
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(
        message_sender,
        "log_outgoing_send",
        lambda **kwargs: captured.append(kwargs),
    )

    class _Bot:
        async def send_message(self, **kwargs):
            sent_kwargs.append(kwargs)
            return SimpleNamespace(message_id=322)

    await message_sender.safe_send(
        _Bot(),
        chat_id=-1009,
        text="general status",
        message_thread_id=1,
    )

    assert "message_thread_id" not in sent_kwargs[0]
    assert captured[0]["thread_id"] == 1


@pytest.mark.asyncio
async def test_send_photo_omits_general_topic_id():
    sent_kwargs: list[dict[str, object]] = []

    class _Bot:
        async def send_photo(self, **kwargs):
            sent_kwargs.append(kwargs)

    await message_sender.send_photo(
        _Bot(),
        chat_id=-1009,
        image_data=[("image/png", b"PNG")],
        message_thread_id=1,
    )

    assert "message_thread_id" not in sent_kwargs[0]


@pytest.mark.asyncio
async def test_send_with_fallback_raises_when_markdown_and_plain_send_fail():
    attempts = 0

    class _Bot:
        async def send_message(self, **_kwargs):
            nonlocal attempts
            attempts += 1
            raise RuntimeError(f"send failed {attempts}")

    with pytest.raises(RuntimeError, match="send failed 2"):
        await message_sender.send_with_fallback(
            _Bot(),
            chat_id=-1009,
            text="hello world",
        )


@pytest.mark.asyncio
async def test_send_with_fallback_does_not_duplicate_after_network_error():
    attempts = 0

    class _Bot:
        async def send_message(self, **_kwargs):
            nonlocal attempts
            attempts += 1
            raise NetworkError("connection lost")

    with pytest.raises(NetworkError, match="connection lost"):
        await message_sender.send_with_fallback(_Bot(), chat_id=-1009, text="hello")

    assert attempts == 1


@pytest.mark.asyncio
async def test_send_with_fallback_uses_plain_text_after_bad_request():
    attempts = 0

    class _Bot:
        async def send_message(self, **_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise BadRequest("can't parse entities")
            return SimpleNamespace(message_id=777)

    sent = await message_sender.send_with_fallback(_Bot(), chat_id=-1009, text="hello")

    assert sent.message_id == 777
    assert attempts == 2


@pytest.mark.asyncio
async def test_send_photo_does_not_fallback_after_network_error():
    document_attempts = 0

    class _Bot:
        async def send_photo(self, **_kwargs):
            raise NetworkError("connection lost")

        async def send_document(self, **_kwargs):
            nonlocal document_attempts
            document_attempts += 1

    with pytest.raises(NetworkError, match="connection lost"):
        await message_sender.send_photo(
            _Bot(), chat_id=-1009, image_data=[("image/png", b"PNG")]
        )

    assert document_attempts == 0


@pytest.mark.asyncio
async def test_safe_send_does_not_duplicate_after_network_error():
    attempts = 0

    class _Bot:
        async def send_message(self, **_kwargs):
            nonlocal attempts
            attempts += 1
            raise NetworkError("connection lost")

    with pytest.raises(NetworkError, match="connection lost"):
        await message_sender.safe_send(_Bot(), chat_id=-1009, text="hello")

    assert attempts == 1


@pytest.mark.asyncio
async def test_safe_send_can_surface_a_terminal_bad_request():
    attempts = 0

    class _Bot:
        async def send_message(self, **kwargs):
            nonlocal attempts
            assert "raise_on_failure" not in kwargs
            attempts += 1
            raise BadRequest("Topic_id_invalid")

    with pytest.raises(BadRequest, match="Topic_id_invalid"):
        await message_sender.safe_send(
            _Bot(),
            chat_id=-1009,
            text="migration notice",
            message_thread_id=77,
            raise_on_failure=True,
        )

    assert attempts == 2


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
            raise BadRequest("unsupported photo format")

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


@pytest.mark.asyncio
async def test_send_photo_stops_document_fallback_after_cancellation():
    document_sends: list[dict[str, object]] = []
    current = True

    class _Bot:
        async def send_media_group(self, **_kwargs):
            raise BadRequest("media group rejected")

        async def send_document(self, **kwargs):
            nonlocal current
            document_sends.append(kwargs)
            current = False

    await message_sender.send_photo(
        _Bot(),
        chat_id=-1009,
        image_data=[("image/png", b"ONE"), ("image/png", b"TWO")],
        delivery_is_current=lambda: current,
    )

    assert len(document_sends) == 1


@pytest.mark.asyncio
async def test_send_photo_raises_when_document_fallback_fails():
    class _Bot:
        async def send_photo(self, **_kwargs):
            raise RuntimeError("photo failed")

        async def send_document(self, **_kwargs):
            raise RuntimeError("document failed")

    with pytest.raises(RuntimeError, match="document failed"):
        await message_sender.send_photo(
            _Bot(),
            chat_id=-1009,
            image_data=[("image/webp", b"WEBPDATA")],
        )


@pytest.mark.asyncio
async def test_send_video_raises_when_document_fallback_fails():
    class _Bot:
        async def send_video(self, **_kwargs):
            raise BadRequest("video rejected")

        async def send_document(self, **_kwargs):
            raise RuntimeError("document failed")

    with pytest.raises(RuntimeError, match="document failed"):
        await message_sender.send_video(
            _Bot(),
            chat_id=-1009,
            media_type="video/mp4",
            raw_bytes=b"VIDEO",
        )


@pytest.mark.asyncio
async def test_send_documents_raises_when_delivery_fails():
    class _Bot:
        async def send_document(self, **_kwargs):
            raise RuntimeError("document failed")

    with pytest.raises(RuntimeError, match="document failed"):
        await message_sender.send_documents(
            _Bot(),
            chat_id=-1009,
            document_data=[("report.txt", b"REPORT")],
        )


@pytest.mark.asyncio
async def test_send_voice_raises_when_all_fallbacks_fail():
    class _Bot:
        async def send_voice(self, **_kwargs):
            raise RuntimeError("voice failed")

        async def send_audio(self, **_kwargs):
            raise RuntimeError("audio failed")

        async def send_document(self, **_kwargs):
            raise RuntimeError("document failed")

    with pytest.raises(RuntimeError, match="document failed"):
        await message_sender.send_voice(
            _Bot(),
            chat_id=-1009,
            media_type="audio/ogg",
            raw_bytes=b"VOICE",
        )


@pytest.mark.asyncio
async def test_safe_send_uses_rich_message_markdown_when_requested(monkeypatch):
    captured: list[dict[str, object]] = []

    def _capture(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(message_sender, "log_outgoing_send", _capture)

    class _Bot:
        async def _post(self, endpoint, data=None, **_kwargs):
            assert endpoint == "sendRichMessage"
            assert data == {
                "chat_id": -1009,
                "message_thread_id": 77,
                "rich_message": {"markdown": "*hi* ==mark=="},
            }
            return {"message_id": 654}

    sent = await message_sender.safe_send(
        _Bot(),
        -1009,
        "ignored plain text",
        message_thread_id=77,
        rich_text="*hi* ==mark==",
        rich_format="markdown",
    )

    assert sent.message_id == 654
    assert len(captured) == 1
    assert captured[0]["chat_id"] == -1009
    assert captured[0]["thread_id"] == 77
    assert captured[0]["message_id"] == 654
    assert captured[0]["text"] == "*hi* ==mark=="


@pytest.mark.asyncio
async def test_safe_send_rich_only_fallback_preserves_source_text(monkeypatch):
    calls: list[tuple[str, str]] = []
    logged: list[dict[str, object]] = []
    monkeypatch.setattr(message_sender, "log_outgoing_send", lambda **kwargs: logged.append(kwargs))

    class _Bot:
        async def _post(self, _endpoint, data=None, **_kwargs):
            raise BadRequest("rich markup rejected")

        async def send_message(self, *, text, **_kwargs):
            calls.append(("plain", text))
            return SimpleNamespace(message_id=655)

    sent = await message_sender.safe_send(
        _Bot(),
        -1009,
        "",
        rich_text="*important rich content*",
        rich_format="markdown",
    )

    assert sent.message_id == 655
    assert calls == [("plain", "*important rich content*")]
    assert logged[0]["text"] == "*important rich content*"


@pytest.mark.asyncio
async def test_safe_edit_uses_rich_message_html_when_requested(monkeypatch):
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
            self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def get_bot(self):
            target = self

            class _Bot:
                async def _post(self, endpoint, data=None, **_kwargs):
                    target.calls.append(((endpoint,), data or {}))
                    return True

            return _Bot()

        async def edit_message_text(self, *_args, **_kwargs):
            raise AssertionError("rich edits must bypass PTB's required text argument")

    target = _Target()

    await message_sender.safe_edit(
        target,
        "ignored plain text",
        rich_text="<b>updated</b><tg-spoiler>hidden</tg-spoiler>",
        rich_format="html",
    )

    assert target.calls == [
        (
            ("editMessageText",),
            {
                "chat_id": -10042,
                "message_id": 808,
                "rich_message": {"html": "<b>updated</b><tg-spoiler>hidden</tg-spoiler>"},
                "link_preview_options": message_sender.NO_LINK_PREVIEW,
            },
        )
    ]
    assert len(captured) == 1
    assert captured[0]["chat_id"] == -10042
    assert captured[0]["thread_id"] == 11
    assert captured[0]["message_id"] == 808
    assert captured[0]["text"] == "<b>updated</b><tg-spoiler>hidden</tg-spoiler>"


@pytest.mark.asyncio
async def test_safe_reply_rejects_rich_text_that_cannot_preserve_reply_semantics(monkeypatch):
    monkeypatch.setattr(message_sender, "log_outgoing_send", lambda **_kwargs: None)

    class _Message:
        chat_id = -10042
        message_id = 809
        message_thread_id = 12

        def get_bot(self):
            raise AssertionError("sendRichMessage cannot preserve reply semantics")

        async def reply_text(self, _text, **_kwargs):
            raise AssertionError("unsupported rich reply must be rejected explicitly")

    with pytest.raises(ValueError, match="Rich Telegram replies are not supported"):
        await message_sender.safe_reply(
            _Message(),
            "plain fallback",
            rich_text="*rich source*",
        )
