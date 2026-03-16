"""Structured telemetry helpers for diagnostic event logging."""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Mapping

logger = logging.getLogger("coco.telemetry")

_MAX_STRING_CHARS = 512
_MAX_COLLECTION_ITEMS = 64


def _sanitize_value(value: object) -> object:
    """Convert arbitrary telemetry fields to JSON-safe bounded values."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        if len(value) <= _MAX_STRING_CHARS:
            return value
        return f"{value[:_MAX_STRING_CHARS]}...[{len(value)} chars]"
    if isinstance(value, bytes):
        return _sanitize_value(value.decode("utf-8", errors="replace"))
    if isinstance(value, Mapping):
        sanitized: dict[str, object] = {}
        for idx, (key, inner) in enumerate(value.items()):
            if idx >= _MAX_COLLECTION_ITEMS:
                sanitized["_truncated"] = True
                break
            sanitized[str(key)] = _sanitize_value(inner)
        return sanitized
    if isinstance(value, (list, tuple, set, frozenset)):
        raw_items = list(value)
        limited = raw_items[:_MAX_COLLECTION_ITEMS]
        sanitized_list = [_sanitize_value(item) for item in limited]
        if len(raw_items) > _MAX_COLLECTION_ITEMS:
            sanitized_list.append(f"...[{len(raw_items) - _MAX_COLLECTION_ITEMS} more]")
        return sanitized_list
    return str(value)


def emit_telemetry(event: str, **fields: object) -> None:
    """Emit one structured telemetry event on the dedicated logger."""
    name = event.strip()
    if not name:
        return

    payload: dict[str, object] = {
        "event": name,
        "ts": round(time.time(), 3),
    }
    for key, value in fields.items():
        if not key:
            continue
        payload[str(key)] = _sanitize_value(value)

    try:
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    except Exception:
        encoded = json.dumps(
            {
                "event": name,
                "ts": round(time.time(), 3),
                "encode_error": "failed_to_encode_payload",
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    logger.info("%s", encoded)
