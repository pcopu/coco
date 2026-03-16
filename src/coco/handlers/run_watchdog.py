"""Run watchdog tracking for long in-progress assistant turns.

Phase A behavior:
  - Track user turns awaiting the first assistant signal per (user, topic).
  - Trigger checks at 30s, 1m, 3m, 5m, 10m, 20m, 30m, then every 30m.
  - Optional auto-resend uses the original pending user text.
  - Auto-resend is capped (max 2), skips oversized payloads, and is persisted
    by message fingerprint.
  - After one successful auto-resend, duplicate follow-up resends are blocked
    for the same pending message.
"""

from dataclasses import dataclass, field
import hashlib
import json
import logging
import time

from ..telemetry import emit_telemetry
from ..utils import atomic_write_json, coco_dir

logger = logging.getLogger(__name__)

# No-response checkpoints (seconds): 30s, 1m, 3m, 5m, 10m, 20m, 30m.
RUN_CHECKPOINTS_SECONDS: tuple[int, ...] = (
    30,
    60,
    3 * 60,
    5 * 60,
    10 * 60,
    20 * 60,
    30 * 60,
)
# After the last explicit checkpoint, keep pinging every 30 minutes.
RUN_REPEAT_CHECKPOINT_INTERVAL_SECONDS = 30 * 60
# Only these checkpoints may trigger an auto-resend.
RUN_AUTO_RESEND_CHECKPOINTS_SECONDS: tuple[int, ...] = (30, 60)
# Hard cap per pending message fingerprint.
RUN_MAX_AUTO_RETRIES = 2
# Guardrail: skip automatic resend for very large payloads.
RUN_AUTO_RESEND_MAX_TEXT_CHARS = 3000
# Persist fingerprint retry counters for one day to survive process restarts.
RUN_RETRY_STATE_TTL_SECONDS = 24 * 60 * 60

_RUN_RETRY_STATE_FILE = coco_dir() / "run_watchdog_retry_state.json"


@dataclass
class RunWatchState:
    """Tracking state for one topic-bound pending user turn."""

    window_id: str
    started_at: float
    pending_text: str
    pending_fingerprint: str
    retry_count: int = 0
    auto_retry_succeeded: bool = False
    fired_checkpoints: set[int] = field(default_factory=set)


@dataclass(frozen=True)
class RunWatchCheck:
    """One due no-response checkpoint."""

    user_id: int
    thread_id: int | None
    window_id: str
    checkpoint_seconds: int
    elapsed_seconds: float
    resend_text: str
    resend_text_len: int
    pending_fingerprint: str
    auto_retry_allowed: bool
    auto_retry_reason: str
    retry_count: int
    max_auto_retries: int


@dataclass(frozen=True)
class RunWatchRetryCandidate:
    """Immediate auto-retry payload for a still-pending topic turn."""

    user_id: int
    thread_id: int | None
    window_id: str
    elapsed_seconds: float
    resend_text: str
    resend_text_len: int
    pending_fingerprint: str
    auto_retry_allowed: bool
    auto_retry_reason: str
    retry_count: int
    max_auto_retries: int


# (user_id, thread_id_or_0) -> RunWatchState
_run_watch_state: dict[tuple[int, int], RunWatchState] = {}
# "<user_id>:<thread_id_or_0>:<fingerprint>" -> {"count": int, "updated_at": float}
_run_watch_retry_state: dict[str, dict[str, float | int]] = {}
_run_watch_retry_state_loaded = False


def _topic_key(user_id: int, thread_id: int | None) -> tuple[int, int]:
    return user_id, thread_id or 0


def _fingerprint_text(text: str) -> str:
    normalized = " ".join(text.split())
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def _retry_key(topic_key: tuple[int, int], fingerprint: str) -> str:
    return f"{topic_key[0]}:{topic_key[1]}:{fingerprint}"


def _load_retry_state(now: float | None = None) -> None:
    global _run_watch_retry_state_loaded
    if _run_watch_retry_state_loaded:
        return

    _run_watch_retry_state_loaded = True
    _run_watch_retry_state.clear()

    path = _RUN_RETRY_STATE_FILE
    if not path.is_file():
        return

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("Failed loading run watchdog retry state (%s): %s", path, e)
        return

    if not isinstance(payload, dict):
        return

    ts = now if now is not None else time.monotonic()
    for key, raw in payload.items():
        if not isinstance(key, str) or not isinstance(raw, dict):
            continue
        count_raw = raw.get("count")
        updated_raw = raw.get("updated_at")
        try:
            count = int(count_raw)
            updated_at = float(updated_raw)
        except (TypeError, ValueError):
            continue
        if count <= 0:
            continue
        if updated_at <= 0:
            continue
        if ts - updated_at > RUN_RETRY_STATE_TTL_SECONDS:
            continue
        _run_watch_retry_state[key] = {
            "count": min(count, RUN_MAX_AUTO_RETRIES),
            "updated_at": updated_at,
        }


def _save_retry_state() -> None:
    if not _run_watch_retry_state_loaded:
        return

    path = _RUN_RETRY_STATE_FILE
    try:
        if not _run_watch_retry_state:
            if path.exists():
                path.unlink()
            return
        atomic_write_json(path, _run_watch_retry_state, indent=2)
    except OSError as e:
        logger.debug("Failed saving run watchdog retry state (%s): %s", path, e)


def _prune_retry_state(now: float | None = None) -> None:
    _load_retry_state(now)
    ts = now if now is not None else time.monotonic()
    stale_keys = []
    for key, raw in _run_watch_retry_state.items():
        try:
            updated_at = float(raw.get("updated_at", 0.0))
        except (TypeError, ValueError):
            updated_at = 0.0
        if updated_at <= 0 or ts - updated_at > RUN_RETRY_STATE_TTL_SECONDS:
            stale_keys.append(key)
    if not stale_keys:
        return
    for key in stale_keys:
        _run_watch_retry_state.pop(key, None)
    _save_retry_state()


def _get_persisted_retry_count(
    topic_key: tuple[int, int],
    fingerprint: str,
    now: float | None = None,
) -> int:
    _prune_retry_state(now)
    raw = _run_watch_retry_state.get(_retry_key(topic_key, fingerprint))
    if not raw:
        return 0
    try:
        count = int(raw.get("count", 0))
    except (TypeError, ValueError):
        return 0
    return min(max(0, count), RUN_MAX_AUTO_RETRIES)


def _set_persisted_retry_count(
    topic_key: tuple[int, int],
    fingerprint: str,
    count: int,
    now: float | None = None,
) -> None:
    _load_retry_state(now)
    key = _retry_key(topic_key, fingerprint)
    normalized = min(max(0, int(count)), RUN_MAX_AUTO_RETRIES)
    if normalized <= 0:
        if _run_watch_retry_state.pop(key, None) is not None:
            _save_retry_state()
        return

    ts = now if now is not None else time.monotonic()
    _run_watch_retry_state[key] = {
        "count": normalized,
        "updated_at": ts,
    }
    _save_retry_state()


def _clear_persisted_retry_count(topic_key: tuple[int, int], fingerprint: str) -> None:
    _set_persisted_retry_count(topic_key, fingerprint, 0)


def _clear_topic_state(topic_key: tuple[int, int]) -> None:
    state = _run_watch_state.pop(topic_key, None)
    if not state:
        return
    _clear_persisted_retry_count(topic_key, state.pending_fingerprint)


def note_run_started(
    *,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    source: str = "",
    pending_text: str = "",
    expect_response: bool = False,
    now: float | None = None,
) -> None:
    """Start/reset pending-response tracking for a topic turn."""
    ts = now if now is not None else time.monotonic()
    skey = _topic_key(user_id, thread_id)

    text = pending_text.strip()
    if not expect_response or not text:
        _clear_topic_state(skey)
        return

    pending_fingerprint = _fingerprint_text(text)

    old_state = _run_watch_state.get(skey)
    if old_state and old_state.pending_fingerprint != pending_fingerprint:
        _clear_persisted_retry_count(skey, old_state.pending_fingerprint)

    persisted_retries = _get_persisted_retry_count(
        skey,
        pending_fingerprint,
        now=ts,
    )
    _run_watch_state[skey] = RunWatchState(
        window_id=window_id,
        started_at=ts,
        pending_text=text,
        pending_fingerprint=pending_fingerprint,
        retry_count=persisted_retries,
    )
    logger.info(
        "Run watchdog pending-start (user=%d thread=%s window=%s source=%s retries=%d)",
        user_id,
        thread_id,
        window_id,
        source or "unknown",
        persisted_retries,
    )
    emit_telemetry(
        "watchdog.pending_start",
        user_id=user_id,
        thread_id=thread_id,
        window_id=window_id,
        source=source or "unknown",
        retry_count=persisted_retries,
        pending_text_len=len(text),
        pending_fingerprint=pending_fingerprint,
    )


def note_run_activity(
    *,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    source: str = "",
    now: float | None = None,
) -> None:
    """Record assistant activity by clearing pending-response tracking."""
    _ = now if now is not None else time.monotonic()
    skey = _topic_key(user_id, thread_id)
    state = _run_watch_state.get(skey)
    if not state:
        return
    if state.window_id != window_id:
        return

    _clear_topic_state(skey)
    logger.info(
        "Run watchdog pending-cleared by activity (user=%d thread=%s window=%s source=%s)",
        user_id,
        thread_id,
        window_id,
        source or "unknown",
    )
    emit_telemetry(
        "watchdog.pending_cleared_activity",
        user_id=user_id,
        thread_id=thread_id,
        window_id=window_id,
        source=source or "unknown",
    )


def note_run_completed(
    *,
    user_id: int,
    thread_id: int | None,
    reason: str = "",
    now: float | None = None,
) -> None:
    """Stop watchdog tracking for a completed run."""
    ts = now if now is not None else time.monotonic()
    skey = _topic_key(user_id, thread_id)
    state = _run_watch_state.get(skey)
    if not state:
        return
    _clear_topic_state(skey)
    elapsed = max(0, ts - state.started_at)
    logger.info(
        "Run watchdog pending-cleared by completion (user=%d thread=%s window=%s elapsed=%.1fs reason=%s)",
        user_id,
        thread_id,
        state.window_id,
        elapsed,
        reason or "unknown",
    )
    emit_telemetry(
        "watchdog.pending_cleared_completion",
        user_id=user_id,
        thread_id=thread_id,
        window_id=state.window_id,
        elapsed_seconds=round(elapsed, 3),
        reason=reason or "unknown",
    )


def clear_run_watch_state(user_id: int, thread_id: int | None = None) -> None:
    """Clear watchdog state for one topic."""
    _clear_topic_state(_topic_key(user_id, thread_id))


def prune_run_watch_topics(active_topic_keys: set[tuple[int, int]]) -> None:
    """Drop watchdog state for topics that are no longer bound."""
    stale = [key for key in _run_watch_state if key not in active_topic_keys]
    for key in stale:
        _clear_topic_state(key)


def note_auto_retry_attempt(
    *,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    now: float | None = None,
) -> tuple[int, int]:
    """Increment and persist auto-retry attempt count for the active topic."""
    ts = now if now is not None else time.monotonic()
    skey = _topic_key(user_id, thread_id)
    state = _run_watch_state.get(skey)
    if not state:
        emit_telemetry(
            "watchdog.retry_attempt_skipped",
            user_id=user_id,
            thread_id=thread_id,
            window_id=window_id,
            reason="no_state",
        )
        return 0, RUN_MAX_AUTO_RETRIES
    if state.window_id != window_id:
        emit_telemetry(
            "watchdog.retry_attempt_skipped",
            user_id=user_id,
            thread_id=thread_id,
            window_id=window_id,
            reason="window_mismatch",
            state_window_id=state.window_id,
            retry_count=state.retry_count,
        )
        return state.retry_count, RUN_MAX_AUTO_RETRIES
    if state.retry_count >= RUN_MAX_AUTO_RETRIES:
        emit_telemetry(
            "watchdog.retry_attempt_skipped",
            user_id=user_id,
            thread_id=thread_id,
            window_id=window_id,
            reason="retry_cap",
            retry_count=state.retry_count,
            retry_limit=RUN_MAX_AUTO_RETRIES,
        )
        return state.retry_count, RUN_MAX_AUTO_RETRIES

    state.retry_count += 1
    _set_persisted_retry_count(
        skey,
        state.pending_fingerprint,
        state.retry_count,
        now=ts,
    )
    emit_telemetry(
        "watchdog.retry_attempt",
        user_id=user_id,
        thread_id=thread_id,
        window_id=window_id,
        retry_count=state.retry_count,
        retry_limit=RUN_MAX_AUTO_RETRIES,
        pending_fingerprint=state.pending_fingerprint,
    )
    return state.retry_count, RUN_MAX_AUTO_RETRIES


def note_auto_retry_result(
    *,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    send_success: bool,
) -> None:
    """Record the outcome of one auto-retry send attempt.

    On success, further auto-resends for the same pending message are blocked
    to avoid duplicate long-paste submissions.
    """
    skey = _topic_key(user_id, thread_id)
    state = _run_watch_state.get(skey)
    if not state:
        return
    if state.window_id != window_id:
        return
    if send_success:
        state.auto_retry_succeeded = True
    emit_telemetry(
        "watchdog.retry_result",
        user_id=user_id,
        thread_id=thread_id,
        window_id=window_id,
        send_success=send_success,
        retry_count=state.retry_count,
        pending_fingerprint=state.pending_fingerprint,
    )


def get_immediate_auto_retry_candidate(
    *,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    now: float | None = None,
) -> RunWatchRetryCandidate | None:
    """Return the current resend candidate without waiting for a checkpoint."""
    ts = now if now is not None else time.monotonic()
    skey = _topic_key(user_id, thread_id)
    state = _run_watch_state.get(skey)
    if not state:
        return None

    if state.window_id != window_id:
        _clear_topic_state(skey)
        return None

    auto_retry_allowed = False
    auto_retry_reason = "checkpoint"
    resend_text = state.pending_text
    resend_text_len = len(resend_text)
    if state.auto_retry_succeeded:
        auto_retry_reason = "already_sent"
    elif state.retry_count >= RUN_MAX_AUTO_RETRIES:
        auto_retry_reason = "retry_cap"
    elif not resend_text.strip():
        auto_retry_reason = "no_payload"
    elif resend_text_len > RUN_AUTO_RESEND_MAX_TEXT_CHARS:
        auto_retry_reason = "payload_too_large"
    else:
        auto_retry_allowed = True
        auto_retry_reason = "eligible"

    return RunWatchRetryCandidate(
        user_id=user_id,
        thread_id=thread_id,
        window_id=window_id,
        elapsed_seconds=max(0.0, ts - state.started_at),
        resend_text=resend_text,
        resend_text_len=resend_text_len,
        pending_fingerprint=state.pending_fingerprint,
        auto_retry_allowed=auto_retry_allowed,
        auto_retry_reason=auto_retry_reason,
        retry_count=state.retry_count,
        max_auto_retries=RUN_MAX_AUTO_RETRIES,
    )


def get_due_run_checks(
    *,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    now: float | None = None,
) -> list[RunWatchCheck]:
    """Return newly-due no-response checkpoints for a pending user turn."""
    ts = now if now is not None else time.monotonic()
    skey = _topic_key(user_id, thread_id)
    state = _run_watch_state.get(skey)
    if not state:
        return []

    if state.window_id != window_id:
        # Binding moved to a different window: clear stale pending state.
        _clear_topic_state(skey)
        return []

    elapsed = max(0.0, ts - state.started_at)
    due: list[RunWatchCheck] = []

    checkpoints = list(RUN_CHECKPOINTS_SECONDS)
    if checkpoints:
        last_checkpoint = checkpoints[-1]
        repeat_checkpoint = last_checkpoint + RUN_REPEAT_CHECKPOINT_INTERVAL_SECONDS
        while repeat_checkpoint <= elapsed:
            checkpoints.append(repeat_checkpoint)
            repeat_checkpoint += RUN_REPEAT_CHECKPOINT_INTERVAL_SECONDS

    for checkpoint in checkpoints:
        if checkpoint in state.fired_checkpoints:
            continue
        if elapsed >= checkpoint:
            state.fired_checkpoints.add(checkpoint)
            auto_retry_allowed = False
            auto_retry_reason = "checkpoint"
            if checkpoint in RUN_AUTO_RESEND_CHECKPOINTS_SECONDS:
                if state.auto_retry_succeeded:
                    auto_retry_reason = "already_sent"
                elif state.retry_count >= RUN_MAX_AUTO_RETRIES:
                    auto_retry_reason = "retry_cap"
                elif state.pending_text.strip():
                    if len(state.pending_text) > RUN_AUTO_RESEND_MAX_TEXT_CHARS:
                        auto_retry_reason = "payload_too_large"
                    else:
                        auto_retry_allowed = True
                        auto_retry_reason = "eligible"
                else:
                    auto_retry_reason = "no_payload"
            due.append(
                RunWatchCheck(
                    user_id=user_id,
                    thread_id=thread_id,
                    window_id=window_id,
                    checkpoint_seconds=checkpoint,
                    elapsed_seconds=elapsed,
                    resend_text=state.pending_text,
                    resend_text_len=len(state.pending_text),
                    pending_fingerprint=state.pending_fingerprint,
                    auto_retry_allowed=auto_retry_allowed,
                    auto_retry_reason=auto_retry_reason,
                    retry_count=state.retry_count,
                    max_auto_retries=RUN_MAX_AUTO_RETRIES,
                )
            )
            emit_telemetry(
                "watchdog.check_due",
                user_id=user_id,
                thread_id=thread_id,
                window_id=window_id,
                checkpoint_seconds=checkpoint,
                elapsed_seconds=round(elapsed, 3),
                retry_count=state.retry_count,
                retry_limit=RUN_MAX_AUTO_RETRIES,
                auto_retry_allowed=auto_retry_allowed,
                auto_retry_reason=auto_retry_reason,
                resend_text_len=len(state.pending_text),
                pending_fingerprint=state.pending_fingerprint,
            )

    return due


def reset_run_watchdog_for_tests(*, clear_persisted: bool = True) -> None:
    """Test helper to clear watchdog in-memory state."""
    global _run_watch_retry_state_loaded
    _run_watch_state.clear()
    _run_watch_retry_state.clear()
    _run_watch_retry_state_loaded = False
    if not clear_persisted:
        return
    try:
        if _RUN_RETRY_STATE_FILE.exists():
            _RUN_RETRY_STATE_FILE.unlink()
    except OSError:
        pass
