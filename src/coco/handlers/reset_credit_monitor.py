"""Discover Codex reset credits and emit durable expiry reminders."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time as datetime_time, timedelta, tzinfo
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..utils import atomic_write_json, coco_dir

logger = logging.getLogger(__name__)

STATE_FILE_NAME = "reset_credit_monitor_state.json"
FETCH_INTERVAL_SECONDS = 24 * 60 * 60
FETCH_RETRY_SECONDS = 15 * 60
MORNING_HOUR = 9
RESET_CREDITS_URL = (
    "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"
)


class ResetCreditFetchError(RuntimeError):
    """Raised when reset-credit inventory cannot be refreshed."""


@dataclass(frozen=True)
class ResetCredit:
    credit_id: str
    title: str
    expires_at: datetime


class ResetCreditNotification(str):
    """Reminder text carrying the durable marker acknowledged after delivery."""

    credit_id: str
    reminder: str
    reminders: tuple[str, ...]

    def __new__(
        cls,
        text: str,
        *,
        credit_id: str,
        reminder: str,
        reminders: tuple[str, ...] | None = None,
    ) -> ResetCreditNotification:
        instance = super().__new__(cls, text)
        instance.credit_id = credit_id
        instance.reminder = reminder
        instance.reminders = reminders or (reminder,)
        return instance


def _state_path() -> Path:
    return coco_dir() / STATE_FILE_NAME


def _default_state() -> dict[str, Any]:
    return {
        "credits": {},
        "sent": {},
        "last_fetch_ts": 0.0,
        "last_fetch_attempt_ts": 0.0,
    }


def _load_state() -> dict[str, Any]:
    path = _state_path()
    if not path.is_file():
        return _default_state()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Reset-credit state is unreadable; rebuilding it: %s", exc)
        return _default_state()
    if not isinstance(payload, dict):
        return _default_state()
    state = _default_state()
    state.update(payload)
    if not isinstance(state.get("credits"), dict):
        state["credits"] = {}
    if not isinstance(state.get("sent"), dict):
        state["sent"] = {}
    return state


def _as_timestamp(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_utc(parsed)


def _local_timezone(timezone_name: str | None) -> tzinfo:
    raw = timezone_name
    if raw is None:
        raw = os.environ.get("COCO_RESET_CREDIT_TIMEZONE", "").strip()
    if raw:
        try:
            return ZoneInfo(raw)
        except ZoneInfoNotFoundError:
            logger.warning("Unknown reset-credit timezone %r; using host timezone", raw)
    return datetime.now().astimezone().tzinfo or UTC


def _auth_file_path() -> Path:
    configured = os.environ.get("COCO_RESET_CREDIT_AUTH_FILE", "").strip()
    if configured:
        return Path(configured).expanduser()
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    if codex_home:
        return Path(codex_home).expanduser() / "auth.json"
    return Path.home() / ".codex" / "auth.json"


def fetch_reset_credits(*, timeout: float = 10.0) -> list[ResetCredit]:
    """Fetch available reset credits using the existing local Codex login."""
    auth_path = _auth_file_path()
    try:
        auth_payload = json.loads(auth_path.read_text(encoding="utf-8"))
        tokens = auth_payload.get("tokens", {})
        access_token = str(tokens.get("access_token", "")).strip()
        account_id = str(tokens.get("account_id", "")).strip()
    except Exception as exc:
        raise ResetCreditFetchError(f"unable to read Codex login from {auth_path}") from exc
    if not access_token:
        raise ResetCreditFetchError(f"Codex login has no access token: {auth_path}")

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "OpenAI-Beta": "codex-1",
        "originator": "Codex Desktop",
        "User-Agent": "CoCo",
    }
    if account_id:
        headers["ChatGPT-Account-ID"] = account_id
    request = Request(RESET_CREDITS_URL, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed HTTPS endpoint
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise ResetCreditFetchError(f"reset-credit API returned HTTP {exc.code}") from exc
    except (OSError, URLError, ValueError, json.JSONDecodeError) as exc:
        raise ResetCreditFetchError("reset-credit API request failed") from exc

    raw_credits = payload.get("credits", []) if isinstance(payload, dict) else []
    credits: list[ResetCredit] = []
    for item in raw_credits:
        if not isinstance(item, dict) or item.get("status") != "available":
            continue
        credit_id = str(item.get("id", "")).strip()
        expires_at = _parse_datetime(item.get("expires_at"))
        if not credit_id or expires_at is None:
            continue
        title = str(item.get("title", "")).strip() or "Full reset"
        credits.append(
            ResetCredit(
                credit_id=credit_id,
                title=title,
                expires_at=expires_at,
            )
        )
    return credits


def _refresh_due(state: dict[str, Any], *, now_ts: float, force: bool) -> bool:
    if force:
        return True
    last_fetch = _as_timestamp(state.get("last_fetch_ts"))
    if last_fetch and now_ts - last_fetch < FETCH_INTERVAL_SECONDS:
        return False
    last_attempt = _as_timestamp(state.get("last_fetch_attempt_ts"))
    return not last_attempt or now_ts - last_attempt >= FETCH_RETRY_SECONDS


def _replace_available_credits(
    state: dict[str, Any],
    credits: list[ResetCredit],
) -> None:
    stored: dict[str, dict[str, str]] = {}
    for credit in credits:
        stored[credit.credit_id] = {
            "title": credit.title,
            "expires_at": _as_utc(credit.expires_at).isoformat(),
        }
    state["credits"] = stored
    state["sent"] = {
        credit_id: markers
        for credit_id, markers in state["sent"].items()
        if credit_id in stored
    }


def _reminder_schedule(expiry: datetime, timezone: tzinfo) -> list[tuple[str, datetime]]:
    local_expiry = expiry.astimezone(timezone)
    morning_local = datetime.combine(
        local_expiry.date(),
        datetime_time(hour=MORNING_HOUR),
        tzinfo=timezone,
    )
    if morning_local >= local_expiry:
        morning_local -= timedelta(days=1)
    schedule = [
        ("24h", expiry - timedelta(hours=24)),
        ("morning", morning_local.astimezone(UTC)),
        ("1h", expiry - timedelta(hours=1)),
    ]
    return sorted(schedule, key=lambda item: item[1])


def _format_notification(*, reminder: str, title: str, expiry: datetime) -> str:
    expiry_text = expiry.strftime("%Y-%m-%d %H:%M UTC")
    lead = {
        "24h": "This reset credit expires in 24 hours.",
        "morning": "This reset credit expires today.",
        "1h": "This reset credit expires in 1 hour.",
    }[reminder]
    return "\n".join(
        [
            "⏰ *Codex reset credit reminder*",
            "",
            lead,
            f"Credit: `{title}`",
            f"Expires: `{expiry_text}`",
        ]
    )


def _collect_reminders(
    state: dict[str, Any],
    *,
    now: datetime,
    timezone: tzinfo,
) -> list[str]:
    notifications: list[str] = []
    credits = state["credits"]
    sent = state["sent"]
    stale_ids: list[str] = []
    for credit_id, raw_credit in credits.items():
        if not isinstance(raw_credit, dict):
            stale_ids.append(credit_id)
            continue
        expiry = _parse_datetime(raw_credit.get("expires_at"))
        if expiry is None:
            stale_ids.append(credit_id)
            continue
        if now >= expiry:
            if now - expiry >= timedelta(days=7):
                stale_ids.append(credit_id)
            continue
        title = str(raw_credit.get("title", "")).strip() or "Full reset"
        raw_sent = sent.get(credit_id)
        sent_markers = raw_sent if isinstance(raw_sent, list) else []
        due_markers = [
            marker
            for marker, due_at in _reminder_schedule(expiry, timezone)
            if due_at < expiry and marker not in sent_markers and now >= due_at
        ]
        if due_markers:
            marker = due_markers[-1]
            notifications.append(
                ResetCreditNotification(
                    _format_notification(
                        reminder=marker,
                        title=title,
                        expiry=expiry,
                    ),
                    credit_id=credit_id,
                    reminder=marker,
                    reminders=tuple(due_markers),
                )
            )
    for credit_id in stale_ids:
        credits.pop(credit_id, None)
        sent.pop(credit_id, None)
    return notifications


def acknowledge_notifications(notifications: list[str]) -> None:
    """Persist successful reminder deliveries so they are not sent twice."""
    markers = [
        (notice.credit_id, marker)
        for notice in notifications
        if isinstance(notice, ResetCreditNotification)
        for marker in notice.reminders
    ]
    if not markers:
        return
    state = _load_state()
    sent = state["sent"]
    changed = False
    for credit_id, marker in markers:
        if credit_id not in state["credits"]:
            continue
        raw_markers = sent.get(credit_id)
        sent_markers = raw_markers if isinstance(raw_markers, list) else []
        if marker in sent_markers:
            continue
        sent_markers.append(marker)
        sent[credit_id] = sent_markers
        changed = True
    if changed:
        atomic_write_json(_state_path(), state, indent=2)


def collect_due_notifications(
    *,
    now: datetime | None = None,
    fetch_credits: Callable[[], list[ResetCredit]] = fetch_reset_credits,
    force_refresh: bool = False,
    require_fresh: bool = False,
    timezone_name: str | None = None,
) -> list[str]:
    """Refresh inventory when due and return newly due reminder messages."""
    current = _as_utc(now or datetime.now(UTC))
    now_ts = current.timestamp()
    state = _load_state()
    refresh_failed = False
    force_refresh = force_refresh or require_fresh
    if _refresh_due(state, now_ts=now_ts, force=force_refresh):
        state["last_fetch_attempt_ts"] = now_ts
        try:
            discovered = fetch_credits()
        except ResetCreditFetchError as exc:
            logger.warning("Reset-credit refresh failed: %s", exc)
            refresh_failed = True
        except Exception:
            logger.exception("Unexpected reset-credit refresh failure")
            refresh_failed = True
        else:
            _replace_available_credits(state, discovered)
            state["last_fetch_ts"] = now_ts

    if require_fresh and refresh_failed:
        atomic_write_json(_state_path(), state, indent=2)
        return []

    notifications = _collect_reminders(
        state,
        now=current,
        timezone=_local_timezone(timezone_name),
    )
    atomic_write_json(_state_path(), state, indent=2)
    return notifications
