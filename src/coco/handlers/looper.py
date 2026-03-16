"""Per-topic looper state for periodic plan nudges.

Looper sends a recurring instruction into a topic-bound session until one of:
  - Assistant replies with the configured completion keyword (single word).
  - Configured time limit expires.
  - User stops it manually.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import re
import time

from ..telemetry import emit_telemetry
from ..utils import atomic_write_json, coco_dir

logger = logging.getLogger(__name__)

LOOPER_DEFAULT_INTERVAL_SECONDS = 10 * 60
LOOPER_MIN_INTERVAL_SECONDS = 60
LOOPER_MAX_INTERVAL_SECONDS = 24 * 60 * 60

LOOPER_MIN_LIMIT_SECONDS = 60
LOOPER_MAX_LIMIT_SECONDS = 30 * 24 * 60 * 60

_LOOPER_STATE_FILE = coco_dir() / "looper_state.json"


@dataclass
class LooperState:
    """One active looper config for a topic."""

    window_id: str
    plan_path: str
    keyword: str
    instructions: str
    interval_seconds: int
    started_at: float
    next_prompt_at: float
    deadline_at: float = 0.0
    prompt_count: int = 0
    last_prompt_at: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "window_id": self.window_id,
            "plan_path": self.plan_path,
            "keyword": self.keyword,
            "instructions": self.instructions,
            "interval_seconds": self.interval_seconds,
            "started_at": self.started_at,
            "next_prompt_at": self.next_prompt_at,
            "deadline_at": self.deadline_at,
            "prompt_count": self.prompt_count,
            "last_prompt_at": self.last_prompt_at,
        }


@dataclass(frozen=True)
class DueLooperPrompt:
    """One claimed due looper prompt for dispatch."""

    user_id: int
    thread_id: int
    window_id: str
    prompt_text: str
    plan_path: str
    keyword: str
    instructions: str
    interval_seconds: int
    prompt_count: int
    deadline_at: float


# (user_id, thread_id) -> LooperState
_looper_state: dict[tuple[int, int], LooperState] = {}
_looper_state_loaded = False


def _topic_key(user_id: int, thread_id: int) -> tuple[int, int]:
    return user_id, thread_id


def _key_to_string(key: tuple[int, int]) -> str:
    return f"{key[0]}:{key[1]}"


def _parse_key(raw_key: str) -> tuple[int, int] | None:
    uid_s, sep, tid_s = raw_key.partition(":")
    if not sep:
        return None
    try:
        return int(uid_s), int(tid_s)
    except (TypeError, ValueError):
        return None


def _clamp_int(value: int, *, low: int, high: int) -> int:
    return max(low, min(high, value))


def normalize_looper_keyword(raw: str) -> str:
    """Normalize keyword/candidate for strict single-word comparison."""
    value = raw.strip()
    value = value.strip("`")
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1].strip()
    value = re.sub(r"\s+", " ", value)
    return value.lower()


def _is_single_word(value: str) -> bool:
    return bool(value) and " " not in value


def _parse_state(raw: dict[str, object]) -> LooperState | None:
    try:
        window_id = str(raw.get("window_id", "")).strip()
        plan_path = str(raw.get("plan_path", "")).strip()
        keyword = normalize_looper_keyword(str(raw.get("keyword", "")))
        instructions = str(raw.get("instructions", "")).strip()
        interval_seconds = _clamp_int(
            int(raw.get("interval_seconds", LOOPER_DEFAULT_INTERVAL_SECONDS)),
            low=LOOPER_MIN_INTERVAL_SECONDS,
            high=LOOPER_MAX_INTERVAL_SECONDS,
        )
        started_at = float(raw.get("started_at", 0.0))
        next_prompt_at = float(raw.get("next_prompt_at", 0.0))
        deadline_at = float(raw.get("deadline_at", 0.0))
        prompt_count = max(0, int(raw.get("prompt_count", 0)))
        last_prompt_at = float(raw.get("last_prompt_at", 0.0))
    except (TypeError, ValueError):
        return None

    if not window_id or not plan_path or not keyword:
        return None
    if not _is_single_word(keyword):
        return None
    if started_at <= 0:
        return None
    if next_prompt_at <= 0:
        next_prompt_at = started_at + interval_seconds
    if deadline_at < 0:
        deadline_at = 0.0

    return LooperState(
        window_id=window_id,
        plan_path=plan_path,
        keyword=keyword,
        instructions=instructions,
        interval_seconds=interval_seconds,
        started_at=started_at,
        next_prompt_at=next_prompt_at,
        deadline_at=deadline_at,
        prompt_count=prompt_count,
        last_prompt_at=last_prompt_at,
    )


def _load_state() -> None:
    global _looper_state_loaded
    if _looper_state_loaded:
        return
    _looper_state_loaded = True
    _looper_state.clear()

    if not _LOOPER_STATE_FILE.is_file():
        return
    try:
        payload = json.loads(_LOOPER_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("Failed loading looper state (%s): %s", _LOOPER_STATE_FILE, e)
        return
    if not isinstance(payload, dict):
        return

    for raw_key, raw_state in payload.items():
        if not isinstance(raw_key, str) or not isinstance(raw_state, dict):
            continue
        parsed_key = _parse_key(raw_key)
        if not parsed_key:
            continue
        parsed_state = _parse_state(raw_state)
        if not parsed_state:
            continue
        _looper_state[parsed_key] = parsed_state


def _save_state() -> None:
    if not _looper_state_loaded:
        return
    try:
        if not _looper_state:
            if _LOOPER_STATE_FILE.exists():
                _LOOPER_STATE_FILE.unlink()
            return
        payload = {
            _key_to_string(key): state.to_dict()
            for key, state in sorted(_looper_state.items())
        }
        atomic_write_json(_LOOPER_STATE_FILE, payload, indent=2)
    except OSError as e:
        logger.debug("Failed saving looper state (%s): %s", _LOOPER_STATE_FILE, e)


def _format_deadline_hint(deadline_at: float, *, now: float) -> str:
    if deadline_at <= 0:
        return ""
    remaining = int(max(0.0, deadline_at - now))
    mins, secs = divmod(remaining, 60)
    hrs, mins = divmod(mins, 60)
    if hrs > 0:
        return f"{hrs}h {mins:02d}m {secs:02d}s remaining"
    return f"{mins}m {secs:02d}s remaining"


def build_looper_prompt(
    *,
    plan_path: str,
    keyword: str,
    instructions: str = "",
    deadline_at: float = 0.0,
    now: float | None = None,
) -> str:
    """Build the recurring assistant nudge for one loop tick."""
    ts = now if now is not None else time.time()
    lines = [
        (
            f"Continue working on the `{plan_path}` plan until it is completely finished. "
            f'When finished, reply with exactly one word: "{keyword}".'
        ),
    ]
    if instructions.strip():
        lines.append(f"Additional instructions: {instructions.strip()}")
    deadline_hint = _format_deadline_hint(deadline_at, now=ts)
    if deadline_hint:
        lines.append(f"Time limit: {deadline_hint}.")
    return "\n".join(lines)


def start_looper(
    *,
    user_id: int,
    thread_id: int,
    window_id: str,
    plan_path: str,
    keyword: str,
    interval_seconds: int = LOOPER_DEFAULT_INTERVAL_SECONDS,
    limit_seconds: int = 0,
    instructions: str = "",
    now: float | None = None,
) -> LooperState:
    """Create/replace looper config for a topic."""
    _load_state()

    plan = plan_path.strip()
    if not plan:
        raise ValueError("plan_path is required")

    normalized_keyword = normalize_looper_keyword(keyword)
    if not _is_single_word(normalized_keyword):
        raise ValueError("keyword must be a single word")

    interval = _clamp_int(
        int(interval_seconds),
        low=LOOPER_MIN_INTERVAL_SECONDS,
        high=LOOPER_MAX_INTERVAL_SECONDS,
    )
    limit = 0
    if int(limit_seconds) > 0:
        limit = _clamp_int(
            int(limit_seconds),
            low=LOOPER_MIN_LIMIT_SECONDS,
            high=LOOPER_MAX_LIMIT_SECONDS,
        )

    ts = now if now is not None else time.time()
    deadline_at = ts + limit if limit > 0 else 0.0
    state = LooperState(
        window_id=window_id,
        plan_path=plan,
        keyword=normalized_keyword,
        instructions=instructions.strip(),
        interval_seconds=interval,
        started_at=ts,
        next_prompt_at=ts + interval,
        deadline_at=deadline_at,
    )
    _looper_state[_topic_key(user_id, thread_id)] = state
    _save_state()

    emit_telemetry(
        "looper.started",
        user_id=user_id,
        thread_id=thread_id,
        window_id=window_id,
        plan_path=plan,
        keyword=normalized_keyword,
        interval_seconds=interval,
        limit_seconds=limit,
        has_instructions=bool(state.instructions),
    )
    return state


def stop_looper(
    *,
    user_id: int,
    thread_id: int,
    reason: str = "manual",
) -> LooperState | None:
    """Stop looper for one topic."""
    _load_state()
    key = _topic_key(user_id, thread_id)
    state = _looper_state.pop(key, None)
    if state:
        _save_state()
        emit_telemetry(
            "looper.stopped",
            user_id=user_id,
            thread_id=thread_id,
            window_id=state.window_id,
            reason=reason,
            prompt_count=state.prompt_count,
        )
    return state


def get_looper_state(
    *,
    user_id: int,
    thread_id: int,
) -> LooperState | None:
    """Return current looper state for a topic."""
    _load_state()
    return _looper_state.get(_topic_key(user_id, thread_id))


def clear_looper_state(user_id: int, thread_id: int | None = None) -> None:
    """Clear looper state for one topic."""
    if thread_id is None:
        return
    stop_looper(user_id=user_id, thread_id=thread_id, reason="cleared")


def prune_looper_topics(active_topic_keys: set[tuple[int, int]]) -> None:
    """Drop looper state for topics that are no longer bound."""
    _load_state()
    stale = [key for key in _looper_state if key not in active_topic_keys]
    if not stale:
        return
    for key in stale:
        state = _looper_state.pop(key, None)
        if state:
            emit_telemetry(
                "looper.stopped",
                user_id=key[0],
                thread_id=key[1],
                window_id=state.window_id,
                reason="stale_topic",
                prompt_count=state.prompt_count,
            )
    _save_state()


def stop_looper_if_expired(
    *,
    user_id: int,
    thread_id: int,
    window_id: str,
    now: float | None = None,
) -> LooperState | None:
    """Stop and return looper state when its time limit has elapsed."""
    _load_state()
    key = _topic_key(user_id, thread_id)
    state = _looper_state.get(key)
    if not state:
        return None
    if state.window_id != window_id:
        return None
    if state.deadline_at <= 0:
        return None
    ts = now if now is not None else time.time()
    if ts < state.deadline_at:
        return None
    del _looper_state[key]
    _save_state()
    emit_telemetry(
        "looper.stopped",
        user_id=user_id,
        thread_id=thread_id,
        window_id=window_id,
        reason="time_limit_reached",
        prompt_count=state.prompt_count,
    )
    return state


def claim_due_looper_prompt(
    *,
    user_id: int,
    thread_id: int,
    window_id: str,
    now: float | None = None,
) -> DueLooperPrompt | None:
    """Claim one due prompt and schedule the next interval."""
    _load_state()
    key = _topic_key(user_id, thread_id)
    state = _looper_state.get(key)
    if not state:
        return None
    if state.window_id != window_id:
        return None

    ts = now if now is not None else time.time()
    if state.deadline_at > 0 and ts >= state.deadline_at:
        return None
    if ts < state.next_prompt_at:
        return None

    state.prompt_count += 1
    state.last_prompt_at = ts
    state.next_prompt_at = ts + state.interval_seconds
    _save_state()

    prompt_text = build_looper_prompt(
        plan_path=state.plan_path,
        keyword=state.keyword,
        instructions=state.instructions,
        deadline_at=state.deadline_at,
        now=ts,
    )
    emit_telemetry(
        "looper.prompt_claimed",
        user_id=user_id,
        thread_id=thread_id,
        window_id=window_id,
        prompt_count=state.prompt_count,
        interval_seconds=state.interval_seconds,
    )
    return DueLooperPrompt(
        user_id=user_id,
        thread_id=thread_id,
        window_id=window_id,
        prompt_text=prompt_text,
        plan_path=state.plan_path,
        keyword=state.keyword,
        instructions=state.instructions,
        interval_seconds=state.interval_seconds,
        prompt_count=state.prompt_count,
        deadline_at=state.deadline_at,
    )


def delay_looper_next_prompt(
    *,
    user_id: int,
    thread_id: int,
    delay_seconds: int = 60,
    now: float | None = None,
) -> None:
    """Bring next prompt closer when a claimed send failed."""
    _load_state()
    state = _looper_state.get(_topic_key(user_id, thread_id))
    if not state:
        return
    ts = now if now is not None else time.time()
    retry_at = ts + max(15, int(delay_seconds))
    if state.next_prompt_at > retry_at:
        state.next_prompt_at = retry_at
        _save_state()


def consume_looper_completion_keyword(
    *,
    user_id: int,
    thread_id: int,
    window_id: str,
    assistant_text: str,
) -> LooperState | None:
    """Stop looper when assistant response matches configured keyword."""
    _load_state()
    key = _topic_key(user_id, thread_id)
    state = _looper_state.get(key)
    if not state:
        return None
    if state.window_id != window_id:
        return None

    candidate = normalize_looper_keyword(assistant_text)
    if not _is_single_word(candidate):
        return None
    if candidate != state.keyword:
        return None

    del _looper_state[key]
    _save_state()
    emit_telemetry(
        "looper.stopped",
        user_id=user_id,
        thread_id=thread_id,
        window_id=window_id,
        reason="keyword_match",
        prompt_count=state.prompt_count,
        keyword=state.keyword,
    )
    return state


def reset_looper_state_for_tests(*, clear_persisted: bool = True) -> None:
    """Test helper to clear looper in-memory state."""
    global _looper_state_loaded
    _looper_state.clear()
    _looper_state_loaded = False
    if not clear_persisted:
        return
    try:
        if _LOOPER_STATE_FILE.exists():
            _LOOPER_STATE_FILE.unlink()
    except OSError:
        pass
