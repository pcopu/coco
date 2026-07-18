"""Persist and report Codex quota windows crossing 10% remaining marks."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from ..utils import atomic_write_json, coco_dir

logger = logging.getLogger(__name__)

STATE_FILE_NAME = "quota_monitor_state.json"
REMAINING_THRESHOLDS = tuple(range(90, 0, -10))
WINDOW_LABELS = {
    "primary": "Primary",
    "secondary": "Secondary",
}


class QuotaNotification(str):
    """Alert text carrying the markers persisted after successful delivery."""

    window_key: str
    cycle_id: str
    thresholds: tuple[int, ...]

    def __new__(
        cls,
        text: str,
        *,
        window_key: str,
        cycle_id: str,
        thresholds: tuple[int, ...],
    ) -> QuotaNotification:
        instance = super().__new__(cls, text)
        instance.window_key = window_key
        instance.cycle_id = cycle_id
        instance.thresholds = thresholds
        return instance


def _state_path() -> Path:
    return coco_dir() / STATE_FILE_NAME


def _default_state() -> dict[str, Any]:
    return {"windows": {}}


def _load_state() -> dict[str, Any]:
    path = _state_path()
    if not path.is_file():
        return _default_state()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Quota monitor state is unreadable; rebuilding it: %s", exc)
        return _default_state()
    if not isinstance(payload, dict) or not isinstance(payload.get("windows"), dict):
        return _default_state()
    return payload


def _as_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _format_percent(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:.1f}"


def _duration_label(raw_minutes: object) -> str:
    minutes = _as_number(raw_minutes)
    if minutes is None or minutes <= 0:
        return "unknown window"
    if minutes % (7 * 24 * 60) == 0:
        days = int(minutes / (24 * 60))
        return f"{days} day" if days == 1 else f"{days} days"
    if minutes % 60 == 0:
        hours = int(minutes / 60)
        return f"{hours} hour" if hours == 1 else f"{hours} hours"
    return f"{int(minutes)} minutes"


def _cycle_id(window: dict[str, Any]) -> str:
    reset = _as_number(window.get("resetsAt"))
    duration = _as_number(window.get("windowDurationMins"))
    return f"{reset or 0:g}:{duration or 0:g}"


def _format_reset(raw_reset: object) -> str:
    reset = _as_number(raw_reset)
    if reset is None or reset <= 0:
        return "unknown"
    return datetime.fromtimestamp(reset).astimezone().strftime("%b %d, %I:%M %p %Z")


def _human_join(values: list[str]) -> str:
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return f"{values[0]} and {values[1]}"
    return f"{', '.join(values[:-1])}, and {values[-1]}"


def _format_notification(
    *,
    window_key: str,
    window: dict[str, Any],
    remaining: float,
    used: float,
    thresholds: tuple[int, ...],
) -> str:
    label = WINDOW_LABELS.get(window_key, window_key.title())
    duration = _duration_label(window.get("windowDurationMins"))
    crossed = _human_join([f"{threshold}%" for threshold in thresholds])
    return "\n".join(
        [
            "📉 *Codex quota alert*",
            "",
            f"{label} ({duration}): `{_format_percent(remaining)}% remaining`",
            f"Used: `{_format_percent(used)}%`",
            f"Crossed: `{crossed}` remaining",
            f"Resets: `{_format_reset(window.get('resetsAt'))}`",
        ]
    )


def collect_due_notifications(rate_limits: dict[str, Any]) -> list[str]:
    """Update quota state and return unacknowledged threshold alerts."""
    if not isinstance(rate_limits, dict):
        return []
    state = _load_state()
    windows = state["windows"]
    notifications: list[str] = []

    for window_key in ("primary", "secondary"):
        raw_window = rate_limits.get(window_key)
        if not isinstance(raw_window, dict):
            continue
        used = _as_number(raw_window.get("usedPercent"))
        if used is None:
            continue
        used = max(0.0, min(100.0, used))
        remaining = 100.0 - used
        cycle_id = _cycle_id(raw_window)
        previous = windows.get(window_key)
        if not isinstance(previous, dict):
            previous = {}
        previous_remaining = _as_number(previous.get("last_remaining"))
        new_cycle = previous.get("cycle_id") != cycle_id
        replenished = (
            previous_remaining is not None and remaining > previous_remaining
        )
        if new_cycle or replenished:
            sent_thresholds: list[int] = []
        else:
            raw_sent = previous.get("sent_thresholds")
            sent_thresholds = (
                [int(value) for value in raw_sent if _as_number(value) is not None]
                if isinstance(raw_sent, list)
                else []
            )

        due = tuple(
            threshold
            for threshold in REMAINING_THRESHOLDS
            if remaining <= threshold and threshold not in sent_thresholds
        )
        windows[window_key] = {
            "cycle_id": cycle_id,
            "last_remaining": remaining,
            "sent_thresholds": sent_thresholds,
        }
        if due:
            notifications.append(
                QuotaNotification(
                    _format_notification(
                        window_key=window_key,
                        window=raw_window,
                        remaining=remaining,
                        used=used,
                        thresholds=due,
                    ),
                    window_key=window_key,
                    cycle_id=cycle_id,
                    thresholds=due,
                )
            )

    atomic_write_json(_state_path(), state, indent=2)
    return notifications


def acknowledge_notifications(notifications: list[str]) -> None:
    """Persist successfully delivered thresholds so they are not repeated."""
    markers = [
        notice
        for notice in notifications
        if isinstance(notice, QuotaNotification)
    ]
    if not markers:
        return
    state = _load_state()
    windows = state["windows"]
    changed = False
    for notice in markers:
        window = windows.get(notice.window_key)
        if not isinstance(window, dict) or window.get("cycle_id") != notice.cycle_id:
            continue
        raw_sent = window.get("sent_thresholds")
        sent = raw_sent if isinstance(raw_sent, list) else []
        for threshold in notice.thresholds:
            if threshold not in sent:
                sent.append(threshold)
                changed = True
        sent.sort(reverse=True)
        window["sent_thresholds"] = sent
    if changed:
        atomic_write_json(_state_path(), state, indent=2)
