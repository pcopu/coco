"""Structured telemetry helpers for diagnostic event logging."""

from __future__ import annotations

import json
import logging
import math
import time
from collections import deque
from collections.abc import Mapping
from copy import deepcopy
from threading import Lock

logger = logging.getLogger("coco.telemetry")

_MAX_STRING_CHARS = 512
_MAX_COLLECTION_ITEMS = 64
_MAX_RECENT_FAILURES = 64
_FAILURE_EVENT_MARKERS = ("fail", "error", "timeout", "uncertain")

_recent_failures: deque[dict[str, object]] = deque(maxlen=_MAX_RECENT_FAILURES)
_recent_failures_lock = Lock()


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
            sanitized_key = str(_sanitize_value(str(key)))
            sanitized[sanitized_key] = _sanitize_value(inner)
        return sanitized
    if isinstance(value, (list, tuple, set, frozenset)):
        raw_items = list(value)
        limited = raw_items[:_MAX_COLLECTION_ITEMS]
        sanitized_list = [_sanitize_value(item) for item in limited]
        if len(raw_items) > _MAX_COLLECTION_ITEMS:
            sanitized_list.append(f"...[{len(raw_items) - _MAX_COLLECTION_ITEMS} more]")
        return sanitized_list
    return _sanitize_value(str(value))


def _is_failure_event_name(name: str) -> bool:
    """Return whether a case-insensitive event name contains a failure marker."""
    normalized_name = name.casefold()
    return any(marker in normalized_name for marker in _FAILURE_EVENT_MARKERS)


def get_recent_failures(limit: int = 5) -> list[dict[str, object]]:
    """Return up to ``limit`` recent failure events in FIFO order.

    Event names containing ``fail``, ``error``, ``timeout``, or ``uncertain``
    (case-insensitively) are retained. Returned payloads are deep copies so
    callers cannot mutate the in-memory ring.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        return []
    bounded_limit = min(limit, _MAX_RECENT_FAILURES)
    with _recent_failures_lock:
        events = list(_recent_failures)[-bounded_limit:]
    return deepcopy(events)


def emit_telemetry(event: str, **fields: object) -> None:
    """Emit one structured telemetry event on the dedicated logger."""
    name = event.strip()
    if not name:
        return

    payload: dict[str, object] = {
        "event": _sanitize_value(name),
        "ts": round(time.time(), 3),
    }
    for key, value in fields.items():
        if not key:
            continue
        payload[str(_sanitize_value(str(key)))] = _sanitize_value(value)

    if _is_failure_event_name(name):
        with _recent_failures_lock:
            _recent_failures.append(payload)

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
