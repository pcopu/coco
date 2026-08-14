"""Safe message sending helpers with MarkdownV2 fallback.

Provides utility functions for sending Telegram messages with automatic
conversion to MarkdownV2 format and fallback to plain text on failure.

Functions:
  - send_with_fallback: Send with MarkdownV2 → plain text fallback
  - send_photo: Photo sending (single or media group)
  - send_documents: Document sending for explicit Telegram attachments
  - safe_reply: Reply with MarkdownV2, fallback to plain text
  - safe_edit: Edit message with MarkdownV2, fallback to plain text
  - safe_send: Send message with MarkdownV2, fallback to plain text

Rate limiting is handled globally by AIORateLimiter on the Application.
RetryAfter exceptions are re-raised so callers (queue worker) can handle them.
"""

import io
import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from telegram import Bot, InputMediaPhoto, LinkPreviewOptions, Message
from telegram.error import BadRequest, NetworkError, RetryAfter

from ..markdown_v2 import convert_markdown
from ..telegram_memory import log_outgoing_edit, log_outgoing_send
from ..transcript_parser import TranscriptParser

logger = logging.getLogger(__name__)

# Sentinel characters to strip from plain text fallback
_SENTINELS = (
    TranscriptParser.EXPANDABLE_QUOTE_START,
    TranscriptParser.EXPANDABLE_QUOTE_END,
)


def _strip_sentinels(text: str) -> str:
    """Strip expandable quote sentinel markers for plain text fallback."""
    for s in _SENTINELS:
        text = text.replace(s, "")
    return text


# Disable link previews in all messages to reduce visual noise
NO_LINK_PREVIEW = LinkPreviewOptions(is_disabled=True)
_IMAGE_EXTENSION_BY_MEDIA_TYPE = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
}
_VIDEO_EXTENSION_BY_MEDIA_TYPE = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/x-msvideo": ".avi",
    "video/mpeg": ".mpeg",
}
_VOICE_EXTENSION_BY_MEDIA_TYPE = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/ogg": ".ogg",
}
_RICH_TEXT_FORMATS = {"html", "markdown"}
_GENERAL_TOPIC_THREAD_ID = 1


def _thread_id_from_kwargs(kwargs: dict[str, Any]) -> int | None:
    tid = kwargs.get("message_thread_id")
    return tid if isinstance(tid, int) else None


def _omit_general_topic_for_telegram(kwargs: dict[str, Any]) -> None:
    """Send forum General as topic-less while retaining internal topic key 1."""
    if kwargs.get("message_thread_id") == _GENERAL_TOPIC_THREAD_ID:
        kwargs.pop("message_thread_id", None)


def _build_rich_message(
    *,
    rich_text: str,
    rich_format: str,
    rich_is_rtl: bool = False,
    rich_skip_entity_detection: bool = False,
) -> dict[str, Any]:
    rich_format = rich_format.strip().lower()
    if rich_format not in _RICH_TEXT_FORMATS:
        raise ValueError(f"Unsupported rich text format: {rich_format}")
    payload: dict[str, Any] = {rich_format: rich_text}
    if rich_is_rtl:
        payload["is_rtl"] = True
    if rich_skip_entity_detection:
        payload["skip_entity_detection"] = True
    return payload


async def _send_rich_message(
    bot: Bot,
    *,
    chat_id: int,
    rich_message: dict[str, Any],
    **kwargs: Any,
) -> Message | SimpleNamespace:
    # sendRichMessage does not accept the regular text method's preview option.
    kwargs.pop("link_preview_options", None)
    result = await bot._post(
        "sendRichMessage",
        {
            "chat_id": chat_id,
            "rich_message": rich_message,
            **kwargs,
        },
    )
    if isinstance(result, Message):
        return result
    if isinstance(result, dict):
        return SimpleNamespace(message_id=result.get("message_id"))
    return SimpleNamespace(message_id=None)


async def _edit_rich_message(
    target: Any,
    *,
    rich_message: dict[str, Any],
    **kwargs: Any,
) -> Any:
    """Edit rich content without PTB's currently required text argument."""
    chat_id, message_id, _thread_id = _target_message_meta(target)
    data: dict[str, Any] = {"rich_message": rich_message, **kwargs}
    if chat_id is not None and message_id is not None:
        data.update(chat_id=chat_id, message_id=message_id)
    else:
        inline_message_id = getattr(target, "inline_message_id", None)
        if not isinstance(inline_message_id, str) or not inline_message_id:
            raise ValueError("Rich edit target has no Telegram message identifier")
        data["inline_message_id"] = inline_message_id
    return await target.get_bot()._post("editMessageText", data)


def _target_message_meta(target: Any) -> tuple[int | None, int | None, int | None]:
    """Extract chat_id/message_id/thread_id from Message or CallbackQuery-like targets."""
    msg = getattr(target, "message", None)
    if msg is None:
        msg = target
    chat_id = getattr(msg, "chat_id", None)
    msg_id = getattr(msg, "message_id", None)
    thread_id = getattr(msg, "message_thread_id", None)
    return (
        chat_id if isinstance(chat_id, int) else None,
        msg_id if isinstance(msg_id, int) else None,
        thread_id if isinstance(thread_id, int) else None,
    )


async def send_with_fallback(
    bot: Bot,
    chat_id: int,
    text: str,
    **kwargs: Any,
) -> Message:
    """Send message with MarkdownV2, falling back to plain text on failure.

    Returns the sent Message on success. Terminal delivery errors and
    RetryAfter are re-raised for caller handling.
    """
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    thread_id = _thread_id_from_kwargs(kwargs)
    _omit_general_topic_for_telegram(kwargs)
    rich_text = kwargs.pop("rich_text", None)
    fallback_text = rich_text if isinstance(rich_text, str) else text
    rich_format = kwargs.pop("rich_format", "markdown")
    rich_is_rtl = bool(kwargs.pop("rich_is_rtl", False))
    rich_skip_entity_detection = bool(kwargs.pop("rich_skip_entity_detection", False))
    try:
        if isinstance(rich_text, str):
            sent = await _send_rich_message(
                bot,
                chat_id=chat_id,
                rich_message=_build_rich_message(
                    rich_text=rich_text,
                    rich_format=rich_format,
                    rich_is_rtl=rich_is_rtl,
                    rich_skip_entity_detection=rich_skip_entity_detection,
                ),
                **kwargs,
            )
            logged_text = rich_text
        else:
            sent = await bot.send_message(
                chat_id=chat_id,
                text=convert_markdown(text),
                parse_mode="MarkdownV2",
                **kwargs,
            )
            logged_text = text
        log_outgoing_send(
            text=logged_text,
            chat_id=chat_id,
            thread_id=thread_id,
            message_id=sent.message_id,
            source="message_sender.send_with_fallback",
        )
        return sent
    except RetryAfter:
        raise
    except Exception as exc:
        if isinstance(exc, NetworkError) and not isinstance(exc, BadRequest):
            raise
        try:
            sent = await bot.send_message(
                chat_id=chat_id, text=_strip_sentinels(fallback_text), **kwargs
            )
            log_outgoing_send(
                text=fallback_text,
                chat_id=chat_id,
                thread_id=thread_id,
                message_id=sent.message_id,
                source="message_sender.send_with_fallback",
            )
            return sent
        except (RetryAfter, NetworkError):
            raise
        except Exception as e:
            logger.error(f"Failed to send message to {chat_id}: {e}")
            raise


async def send_photo(
    bot: Bot,
    chat_id: int,
    image_data: list[tuple[str, bytes]],
    delivery_is_current: Callable[[], bool] | None = None,
    **kwargs: Any,
) -> None:
    """Send photo(s) to chat. Sends as media group if multiple images.

    Rate limiting is handled globally by AIORateLimiter on the Application.

    Args:
        bot: Telegram Bot instance
        chat_id: Target chat ID
        image_data: List of (media_type, raw_bytes) tuples
        **kwargs: Extra kwargs passed to send_photo/send_media_group
    """
    if not image_data:
        return
    _omit_general_topic_for_telegram(kwargs)
    try:
        if len(image_data) == 1:
            _media_type, raw_bytes = image_data[0]
            await bot.send_photo(
                chat_id=chat_id,
                photo=io.BytesIO(raw_bytes),
                **kwargs,
            )
        else:
            media = [
                InputMediaPhoto(media=io.BytesIO(raw_bytes))
                for _media_type, raw_bytes in image_data
            ]
            await bot.send_media_group(
                chat_id=chat_id,
                media=media,
                **kwargs,
            )
    except RetryAfter:
        raise
    except Exception as e:
        if isinstance(e, NetworkError) and not isinstance(e, BadRequest):
            raise
        logger.warning("Photo send failed for %d; falling back to documents: %s", chat_id, e)
        try:
            for index, (media_type, raw_bytes) in enumerate(image_data, start=1):
                if delivery_is_current is not None and not delivery_is_current():
                    return
                extension = _IMAGE_EXTENSION_BY_MEDIA_TYPE.get(media_type.lower(), ".bin")
                await bot.send_document(
                    chat_id=chat_id,
                    document=io.BytesIO(raw_bytes),
                    filename=f"image-{index}{extension}",
                    **kwargs,
                )
        except (RetryAfter, NetworkError):
            raise
        except Exception as doc_exc:
            logger.error("Failed to send image fallback document to %d: %s", chat_id, doc_exc)
            raise


async def send_video(
    bot: Bot,
    chat_id: int,
    media_type: str,
    raw_bytes: bytes,
    **kwargs: Any,
) -> None:
    """Send one video to chat with document fallback."""
    _omit_general_topic_for_telegram(kwargs)
    try:
        await bot.send_video(
            chat_id=chat_id,
            video=io.BytesIO(raw_bytes),
            **kwargs,
        )
    except RetryAfter:
        raise
    except Exception as exc:
        if isinstance(exc, NetworkError) and not isinstance(exc, BadRequest):
            raise
        logger.warning("Video send failed for %d; falling back to document: %s", chat_id, exc)
        extension = _VIDEO_EXTENSION_BY_MEDIA_TYPE.get(media_type.lower(), ".bin")
        try:
            await bot.send_document(
                chat_id=chat_id,
                document=io.BytesIO(raw_bytes),
                filename=f"video{extension}",
                **kwargs,
            )
        except (RetryAfter, NetworkError):
            raise
        except Exception as doc_exc:
            logger.error("Failed to send video fallback document to %d: %s", chat_id, doc_exc)
            raise doc_exc


async def send_documents(
    bot: Bot,
    chat_id: int,
    document_data: list[tuple[str, bytes]],
    **kwargs: Any,
) -> None:
    """Send one or more documents to chat."""
    if not document_data:
        return
    _omit_general_topic_for_telegram(kwargs)
    try:
        for filename, raw_bytes in document_data:
            await bot.send_document(
                chat_id=chat_id,
                document=io.BytesIO(raw_bytes),
                filename=filename,
                **kwargs,
            )
    except (RetryAfter, NetworkError):
        raise
    except Exception as e:
        logger.error("Failed to send document to %d: %s", chat_id, e)
        raise


async def send_voice(
    bot: Bot,
    chat_id: int,
    media_type: str,
    raw_bytes: bytes,
    **kwargs: Any,
) -> None:
    """Send one voice note to chat with audio/document fallback."""
    _omit_general_topic_for_telegram(kwargs)
    extension = _VOICE_EXTENSION_BY_MEDIA_TYPE.get(media_type.lower(), ".bin")
    try:
        await bot.send_voice(
            chat_id=chat_id,
            voice=io.BytesIO(raw_bytes),
            filename=f"voice{extension}",
            **kwargs,
        )
    except RetryAfter:
        raise
    except Exception as exc:
        if isinstance(exc, NetworkError) and not isinstance(exc, BadRequest):
            raise
        logger.warning("Voice send failed for %d; falling back to audio/document: %s", chat_id, exc)
        try:
            await bot.send_audio(
                chat_id=chat_id,
                audio=io.BytesIO(raw_bytes),
                filename=f"voice{extension}",
                **kwargs,
            )
        except (RetryAfter, NetworkError):
            raise
        except Exception as audio_exc:
            logger.warning("Audio fallback failed for %d; falling back to document: %s", chat_id, audio_exc)
            try:
                await bot.send_document(
                    chat_id=chat_id,
                    document=io.BytesIO(raw_bytes),
                    filename=f"voice{extension}",
                    **kwargs,
                )
            except (RetryAfter, NetworkError):
                raise
            except Exception as doc_exc:
                logger.error("Failed to send voice fallback document to %d: %s", chat_id, doc_exc)
                raise


async def safe_reply(message: Message, text: str, **kwargs: Any) -> Message:
    """Reply with MarkdownV2, falling back to plain text on failure."""
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    thread_id = getattr(message, "message_thread_id", None)
    rich_text = kwargs.pop("rich_text", None)
    if isinstance(rich_text, str):
        raise ValueError(
            "Rich Telegram replies are not supported; use a standalone rich send or a plain reply"
        )
    reply_source = text
    fallback_text = reply_source
    kwargs.pop("rich_format", None)
    kwargs.pop("rich_is_rtl", None)
    kwargs.pop("rich_skip_entity_detection", None)
    try:
        sent = await message.reply_text(
            convert_markdown(reply_source),
            parse_mode="MarkdownV2",
            **kwargs,
        )
        logged_text = reply_source
        log_outgoing_send(
            text=logged_text,
            chat_id=message.chat_id,
            thread_id=thread_id if isinstance(thread_id, int) else None,
            message_id=sent.message_id,
            source="message_sender.safe_reply",
        )
        return sent
    except RetryAfter:
        raise
    except Exception as exc:
        if isinstance(exc, NetworkError) and not isinstance(exc, BadRequest):
            raise
        try:
            sent = await message.reply_text(_strip_sentinels(fallback_text), **kwargs)
            log_outgoing_send(
                text=fallback_text,
                chat_id=message.chat_id,
                thread_id=thread_id if isinstance(thread_id, int) else None,
                message_id=sent.message_id,
                source="message_sender.safe_reply",
            )
            return sent
        except (RetryAfter, NetworkError):
            raise
        except Exception as e:
            logger.error(f"Failed to reply: {e}")
            raise


async def safe_edit(target: Any, text: str, **kwargs: Any) -> None:
    """Edit message with MarkdownV2, falling back to plain text on failure."""
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    chat_id, message_id, thread_id = _target_message_meta(target)
    rich_text = kwargs.pop("rich_text", None)
    fallback_text = rich_text if isinstance(rich_text, str) else text
    rich_format = kwargs.pop("rich_format", "markdown")
    rich_is_rtl = bool(kwargs.pop("rich_is_rtl", False))
    rich_skip_entity_detection = bool(kwargs.pop("rich_skip_entity_detection", False))
    try:
        logged_text = text
        if isinstance(rich_text, str):
            await _edit_rich_message(
                target,
                rich_message=_build_rich_message(
                    rich_text=rich_text,
                    rich_format=rich_format,
                    rich_is_rtl=rich_is_rtl,
                    rich_skip_entity_detection=rich_skip_entity_detection,
                ),
                **kwargs,
            )
            logged_text = rich_text
        else:
            await target.edit_message_text(
                convert_markdown(text),
                parse_mode="MarkdownV2",
                **kwargs,
            )
        if chat_id is not None and message_id is not None:
            log_outgoing_edit(
                text=logged_text,
                chat_id=chat_id,
                thread_id=thread_id,
                message_id=message_id,
                source="message_sender.safe_edit",
            )
    except RetryAfter:
        raise
    except Exception as exc:
        if isinstance(exc, NetworkError) and not isinstance(exc, BadRequest):
            raise
        try:
            await target.edit_message_text(_strip_sentinels(fallback_text), **kwargs)
            if chat_id is not None and message_id is not None:
                log_outgoing_edit(
                    text=fallback_text,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    message_id=message_id,
                    source="message_sender.safe_edit",
                )
        except RetryAfter:
            raise
        except Exception as e:
            logger.error("Failed to edit message: %s", e)


async def safe_send(
    bot: Bot,
    chat_id: int,
    text: str,
    message_thread_id: int | None = None,
    raise_on_failure: bool = False,
    **kwargs: Any,
) -> Message | SimpleNamespace | None:
    """Send message with MarkdownV2, falling back to plain text on failure."""
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    if message_thread_id is not None:
        kwargs.setdefault("message_thread_id", message_thread_id)
    _omit_general_topic_for_telegram(kwargs)
    rich_text = kwargs.pop("rich_text", None)
    fallback_text = rich_text if isinstance(rich_text, str) else text
    rich_format = kwargs.pop("rich_format", "markdown")
    rich_is_rtl = bool(kwargs.pop("rich_is_rtl", False))
    rich_skip_entity_detection = bool(kwargs.pop("rich_skip_entity_detection", False))
    try:
        if isinstance(rich_text, str):
            sent = await _send_rich_message(
                bot,
                chat_id=chat_id,
                rich_message=_build_rich_message(
                    rich_text=rich_text,
                    rich_format=rich_format,
                    rich_is_rtl=rich_is_rtl,
                    rich_skip_entity_detection=rich_skip_entity_detection,
                ),
                **kwargs,
            )
            logged_text = rich_text
        else:
            sent = await bot.send_message(
                chat_id=chat_id,
                text=convert_markdown(text),
                parse_mode="MarkdownV2",
                **kwargs,
            )
            logged_text = text
        log_outgoing_send(
            text=logged_text,
            chat_id=chat_id,
            thread_id=message_thread_id,
            message_id=sent.message_id,
            source="message_sender.safe_send",
        )
        return sent
    except RetryAfter:
        raise
    except Exception as exc:
        if isinstance(exc, NetworkError) and not isinstance(exc, BadRequest):
            raise
        try:
            sent = await bot.send_message(
                chat_id=chat_id, text=_strip_sentinels(fallback_text), **kwargs
            )
            log_outgoing_send(
                text=fallback_text,
                chat_id=chat_id,
                thread_id=message_thread_id,
                message_id=sent.message_id,
                source="message_sender.safe_send",
            )
            return sent
        except (RetryAfter, NetworkError):
            raise
        except Exception as e:
            logger.error(f"Failed to send message to {chat_id}: {e}")
            if raise_on_failure:
                raise
            return None
