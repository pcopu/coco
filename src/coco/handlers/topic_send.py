"""Helpers for sending direct messages into a Telegram topic."""

from __future__ import annotations

import mimetypes
from pathlib import Path

import httpx
from telegram import Bot

from ..session import session_manager
from .message_sender import safe_send, send_photo, send_video


def _normalize_content_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


def _media_type_for_file(file_path: str, *, expected_prefix: str, label: str) -> str:
    guessed_type, _encoding = mimetypes.guess_type(file_path)
    media_type = _normalize_content_type(guessed_type or "")
    if not media_type.startswith(expected_prefix):
        raise ValueError(f"Could not infer a {label} media type from the file path.")
    return media_type


async def _download_media_data(
    media_url: str,
    *,
    expected_prefix: str,
    label: str,
) -> tuple[str, bytes]:
    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.get(media_url)
        response.raise_for_status()
    media_type = _normalize_content_type(response.headers.get("Content-Type", ""))
    if not media_type.startswith(expected_prefix):
        raise ValueError(f"Downloaded URL did not return a {label} content type.")
    return media_type, response.content


def _read_media_file(
    file_path: str,
    *,
    expected_prefix: str,
    label: str,
) -> tuple[str, bytes]:
    media_type = _media_type_for_file(file_path, expected_prefix=expected_prefix, label=label)
    return media_type, Path(file_path).read_bytes()


async def send_message_to_topic(
    bot: Bot,
    *,
    user_id: int,
    thread_id: int,
    chat_id: int | None = None,
    text: str = "",
    image_url: str = "",
    image_file: str = "",
    video_url: str = "",
    video_file: str = "",
) -> tuple[bool, str]:
    """Send one text or text+media message to a bound Telegram topic."""
    media_sources = [image_url, image_file, video_url, video_file]
    if sum(1 for value in media_sources if value) > 1:
        return False, "Provide at most one media source."

    resolved_chat_id = session_manager.resolve_chat_id(
        user_id,
        thread_id,
        chat_id=chat_id,
    )
    if resolved_chat_id is None:
        return False, "No chat binding for this topic."

    if not any(media_sources):
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
            media_type, raw_bytes = _read_media_file(
                image_file,
                expected_prefix="image/",
                label="image",
            )
            await send_photo(
                bot,
                resolved_chat_id,
                [(media_type, raw_bytes)],
                caption=text,
                message_thread_id=thread_id,
            )
        elif image_url:
            media_type, raw_bytes = await _download_media_data(
                image_url,
                expected_prefix="image/",
                label="image",
            )
            await send_photo(
                bot,
                resolved_chat_id,
                [(media_type, raw_bytes)],
                caption=text,
                message_thread_id=thread_id,
            )
        elif video_file:
            media_type, raw_bytes = _read_media_file(
                video_file,
                expected_prefix="video/",
                label="video",
            )
            await send_video(
                bot,
                resolved_chat_id,
                media_type,
                raw_bytes,
                caption=text,
                message_thread_id=thread_id,
            )
        else:
            media_type, raw_bytes = await _download_media_data(
                video_url,
                expected_prefix="video/",
                label="video",
            )
            await send_video(
                bot,
                resolved_chat_id,
                media_type,
                raw_bytes,
                caption=text,
                message_thread_id=thread_id,
            )
    except FileNotFoundError as exc:
        file_label = "image" if image_file else "video"
        return False, f"Failed reading {file_label} file: {exc}"
    except OSError as exc:
        file_label = "image" if image_file else "video"
        return False, f"Failed reading {file_label} file: {exc}"
    except httpx.HTTPError as exc:
        url_label = "image" if image_url else "video"
        return False, f"Failed downloading {url_label} URL: {exc}"
    except ValueError as exc:
        return False, str(exc)
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
