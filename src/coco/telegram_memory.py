"""Append-only memory log for Telegram-visible chat text.

This module records only text that is visible in Telegram chat:
  - inbound user text/captions
  - outbound bot sends
  - outbound bot edits

It does not attempt to capture internal tool output or hidden runtime state.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from .transcript_parser import TranscriptParser
from .utils import env_alias

logger = logging.getLogger(__name__)

_START = TranscriptParser.EXPANDABLE_QUOTE_START
_END = TranscriptParser.EXPANDABLE_QUOTE_END


def _default_log_path() -> Path:
    # src/coco/telegram_memory.py -> repo root is parents[2]
    return Path(__file__).resolve().parents[2] / "TELEGRAM_CHAT_MEMORY.jsonl"


def _resolve_log_path() -> Path:
    raw = env_alias("COCO_TELEGRAM_MEMORY_LOG_PATH")
    if raw:
        return Path(raw).expanduser()
    return _default_log_path()


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_visible_text(text: str | None) -> str:
    if not text:
        return ""
    return text.replace(_START, "").replace(_END, "")


def _append(entry: dict[str, object]) -> None:
    path = _resolve_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False))
            f.write("\n")
    except Exception as e:
        logger.debug("Failed to append Telegram memory log (%s): %s", path, e)


def log_incoming_message(
    *,
    kind: str,
    text: str | None,
    chat_id: int | None,
    thread_id: int | None,
    message_id: int | None,
    from_user_id: int | None,
    sender_chat_id: int | None,
    chat_type: str | None,
) -> None:
    visible_text = _normalize_visible_text(text).strip()
    if not visible_text:
        return
    _append(
        {
            "ts_utc": _now_utc_iso(),
            "direction": "in",
            "kind": kind,
            "chat_id": chat_id,
            "thread_id": thread_id,
            "message_id": message_id,
            "from_user_id": from_user_id,
            "sender_chat_id": sender_chat_id,
            "chat_type": chat_type,
            "text": visible_text,
        }
    )


def log_outgoing_send(
    *,
    text: str | None,
    chat_id: int,
    thread_id: int | None,
    message_id: int | None,
    source: str,
) -> None:
    visible_text = _normalize_visible_text(text).strip()
    if not visible_text:
        return
    _append(
        {
            "ts_utc": _now_utc_iso(),
            "direction": "out_send",
            "source": source,
            "chat_id": chat_id,
            "thread_id": thread_id,
            "message_id": message_id,
            "text": visible_text,
        }
    )


def log_outgoing_edit(
    *,
    text: str | None,
    chat_id: int,
    thread_id: int | None,
    message_id: int,
    source: str,
) -> None:
    visible_text = _normalize_visible_text(text).strip()
    if not visible_text:
        return
    _append(
        {
            "ts_utc": _now_utc_iso(),
            "direction": "out_edit",
            "source": source,
            "chat_id": chat_id,
            "thread_id": thread_id,
            "message_id": message_id,
            "text": visible_text,
        }
    )
