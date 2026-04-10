"""Helpers for sending direct messages into a Telegram topic."""

from __future__ import annotations

import mimetypes
from pathlib import Path

import httpx
from telegram import Bot

from ..session import session_manager
from .message_sender import safe_send, send_photo


def _normalize_content_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


def _image_media_type_for_file(image_file: str) -> str:
    guessed_type, _encoding = mimetypes.guess_type(image_file)
    media_type = _normalize_content_type(guessed_type or "")
    if not media_type.startswith("image/"):
        raise ValueError("Could not infer an image media type from the file path.")
    return media_type


async def _download_image_data(image_url: str) -> tuple[str, bytes]:
    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.get(image_url)
        response.raise_for_status()
    media_type = _normalize_content_type(response.headers.get("Content-Type", ""))
    if not media_type.startswith("image/"):
        raise ValueError("Downloaded URL did not return an image content type.")
    return media_type, response.content


def _read_image_file(image_file: str) -> tuple[str, bytes]:
    media_type = _image_media_type_for_file(image_file)
    return media_type, Path(image_file).read_bytes()


async def send_message_to_topic(
    bot: Bot,
    *,
    user_id: int,
    thread_id: int,
    chat_id: int | None = None,
    text: str = "",
    image_url: str = "",
    image_file: str = "",
) -> tuple[bool, str]:
    """Send one text or text+image message to a bound Telegram topic."""
    if image_url and image_file:
        return False, "Provide at most one of image_url or image_file."

    resolved_chat_id = session_manager.resolve_chat_id(
        user_id,
        thread_id,
        chat_id=chat_id,
    )
    if resolved_chat_id is None:
        return False, "No chat binding for this topic."

    if not image_url and not image_file:
        try:
            sent = await safe_send(
                bot,
                resolved_chat_id,
                text,
                message_thread_id=thread_id,
            )
        except Exception as exc:
            return False, str(exc)
        if sent is None:
            return False, "Failed to send message to Telegram."
        return True, ""

    try:
        if image_file:
            media_type, raw_bytes = _read_image_file(image_file)
        else:
            media_type, raw_bytes = await _download_image_data(image_url)
    except FileNotFoundError as exc:
        return False, f"Failed reading image file: {exc}"
    except OSError as exc:
        return False, f"Failed reading image file: {exc}"
    except httpx.HTTPError as exc:
        return False, f"Failed downloading image URL: {exc}"
    except ValueError as exc:
        return False, str(exc)

    try:
        await send_photo(
            bot,
            resolved_chat_id,
            [(media_type, raw_bytes)],
            caption=text,
            message_thread_id=thread_id,
        )
    except Exception as exc:
        return False, str(exc)
    return True, ""


async def send_text_to_topic(
    bot: Bot,
    *,
    user_id: int,
    thread_id: int,
    chat_id: int | None = None,
    text: str,
) -> tuple[bool, str]:
    """Send one text message to a bound Telegram topic."""
    return await send_message_to_topic(
        bot,
        user_id=user_id,
        thread_id=thread_id,
        chat_id=chat_id,
        text=text,
    )
