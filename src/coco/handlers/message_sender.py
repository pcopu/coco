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
from typing import Any

from telegram import Bot, InputMediaPhoto, LinkPreviewOptions, Message
from telegram.error import RetryAfter

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


def _thread_id_from_kwargs(kwargs: dict[str, Any]) -> int | None:
    tid = kwargs.get("message_thread_id")
    return tid if isinstance(tid, int) else None


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
) -> Message | None:
    """Send message with MarkdownV2, falling back to plain text on failure.

    Returns the sent Message on success, None on failure.
    RetryAfter is re-raised for caller handling.
    """
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    thread_id = _thread_id_from_kwargs(kwargs)
    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=convert_markdown(text),
            parse_mode="MarkdownV2",
            **kwargs,
        )
        log_outgoing_send(
            text=text,
            chat_id=chat_id,
            thread_id=thread_id,
            message_id=sent.message_id,
            source="message_sender.send_with_fallback",
        )
        return sent
    except RetryAfter:
        raise
    except Exception:
        try:
            sent = await bot.send_message(
                chat_id=chat_id, text=_strip_sentinels(text), **kwargs
            )
            log_outgoing_send(
                text=text,
                chat_id=chat_id,
                thread_id=thread_id,
                message_id=sent.message_id,
                source="message_sender.send_with_fallback",
            )
            return sent
        except RetryAfter:
            raise
        except Exception as e:
            logger.error(f"Failed to send message to {chat_id}: {e}")
            return None


async def send_photo(
    bot: Bot,
    chat_id: int,
    image_data: list[tuple[str, bytes]],
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
        logger.warning("Photo send failed for %d; falling back to documents: %s", chat_id, e)
        try:
            for index, (media_type, raw_bytes) in enumerate(image_data, start=1):
                extension = _IMAGE_EXTENSION_BY_MEDIA_TYPE.get(media_type.lower(), ".bin")
                await bot.send_document(
                    chat_id=chat_id,
                    document=io.BytesIO(raw_bytes),
                    filename=f"image-{index}{extension}",
                    **kwargs,
                )
        except RetryAfter:
            raise
        except Exception as doc_exc:
            logger.error("Failed to send image fallback document to %d: %s", chat_id, doc_exc)


async def send_documents(
    bot: Bot,
    chat_id: int,
    document_data: list[tuple[str, bytes]],
    **kwargs: Any,
) -> None:
    """Send one or more documents to chat."""
    if not document_data:
        return
    try:
        for filename, raw_bytes in document_data:
            await bot.send_document(
                chat_id=chat_id,
                document=io.BytesIO(raw_bytes),
                filename=filename,
                **kwargs,
            )
    except RetryAfter:
        raise
    except Exception as e:
        logger.error("Failed to send document to %d: %s", chat_id, e)


async def safe_reply(message: Message, text: str, **kwargs: Any) -> Message:
    """Reply with MarkdownV2, falling back to plain text on failure."""
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    thread_id = getattr(message, "message_thread_id", None)
    try:
        sent = await message.reply_text(
            convert_markdown(text),
            parse_mode="MarkdownV2",
            **kwargs,
        )
        log_outgoing_send(
            text=text,
            chat_id=message.chat_id,
            thread_id=thread_id if isinstance(thread_id, int) else None,
            message_id=sent.message_id,
            source="message_sender.safe_reply",
        )
        return sent
    except RetryAfter:
        raise
    except Exception:
        try:
            sent = await message.reply_text(_strip_sentinels(text), **kwargs)
            log_outgoing_send(
                text=text,
                chat_id=message.chat_id,
                thread_id=thread_id if isinstance(thread_id, int) else None,
                message_id=sent.message_id,
                source="message_sender.safe_reply",
            )
            return sent
        except RetryAfter:
            raise
        except Exception as e:
            logger.error(f"Failed to reply: {e}")
            raise


async def safe_edit(target: Any, text: str, **kwargs: Any) -> None:
    """Edit message with MarkdownV2, falling back to plain text on failure."""
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    chat_id, message_id, thread_id = _target_message_meta(target)
    try:
        await target.edit_message_text(
            convert_markdown(text),
            parse_mode="MarkdownV2",
            **kwargs,
        )
        if chat_id is not None and message_id is not None:
            log_outgoing_edit(
                text=text,
                chat_id=chat_id,
                thread_id=thread_id,
                message_id=message_id,
                source="message_sender.safe_edit",
            )
    except RetryAfter:
        raise
    except Exception:
        try:
            await target.edit_message_text(_strip_sentinels(text), **kwargs)
            if chat_id is not None and message_id is not None:
                log_outgoing_edit(
                    text=text,
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
    **kwargs: Any,
) -> None:
    """Send message with MarkdownV2, falling back to plain text on failure."""
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    if message_thread_id is not None:
        kwargs.setdefault("message_thread_id", message_thread_id)
    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=convert_markdown(text),
            parse_mode="MarkdownV2",
            **kwargs,
        )
        log_outgoing_send(
            text=text,
            chat_id=chat_id,
            thread_id=message_thread_id,
            message_id=sent.message_id,
            source="message_sender.safe_send",
        )
    except RetryAfter:
        raise
    except Exception:
        try:
            sent = await bot.send_message(
                chat_id=chat_id, text=_strip_sentinels(text), **kwargs
            )
            log_outgoing_send(
                text=text,
                chat_id=chat_id,
                thread_id=message_thread_id,
                message_id=sent.message_id,
                source="message_sender.safe_send",
            )
        except RetryAfter:
            raise
        except Exception as e:
            logger.error(f"Failed to send message to {chat_id}: {e}")
