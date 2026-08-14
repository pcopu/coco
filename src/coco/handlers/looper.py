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
import random
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
    interval_max_seconds: int = 0
    runner_command: str = ""
    trigger_on_user_message: bool = False
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
            "interval_max_seconds": self.interval_max_seconds,
            "runner_command": self.runner_command,
            "trigger_on_user_message": self.trigger_on_user_message,
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
    runner_command: str = ""
    chat_id: int = 0


# (user_id, chat_id, thread_id) -> LooperState
_looper_state: dict[tuple[int, int, int], LooperState] = {}
_looper_state_loaded = False
_legacy_looper_state_keys: set[tuple[int, int, int]] = set()


def _topic_key(
    user_id: int,
    thread_id: int,
    chat_id: int | None = None,
) -> tuple[int, int, int]:
    return int(user_id), int(chat_id or 0), int(thread_id)


def _key_to_string(key: tuple[int, int, int]) -> str:
    return f"{key[0]}:{key[1]}:{key[2]}"


def _persisted_key_to_string(key: tuple[int, int, int]) -> str:
    if key in _legacy_looper_state_keys:
        return f"{key[0]}:{key[2]}"
    return _key_to_string(key)


def _parse_key(raw_key: str) -> tuple[int, int, int] | None:
    parts = raw_key.split(":")
    if len(parts) == 2:
        uid_s, tid_s = parts
        chat_s = "0"
    elif len(parts) == 3:
        uid_s, chat_s, tid_s = parts
    else:
        return None
    try:
        return int(uid_s), int(chat_s), int(tid_s)
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


def _sample_interval_seconds(*, interval_seconds: int, interval_max_seconds: int = 0) -> int:
    low = _clamp_int(
        int(interval_seconds),
        low=LOOPER_MIN_INTERVAL_SECONDS,
        high=LOOPER_MAX_INTERVAL_SECONDS,
    )
    high_raw = int(interval_max_seconds) if int(interval_max_seconds) > 0 else low
    high = _clamp_int(
        high_raw,
        low=LOOPER_MIN_INTERVAL_SECONDS,
        high=LOOPER_MAX_INTERVAL_SECONDS,
    )
    if high < low:
        low, high = high, low
    if high == low:
        return low
    return random.randint(low, high)


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
        raw_interval_max_seconds = int(raw.get("interval_max_seconds", 0) or 0)
        interval_max_seconds = (
            _clamp_int(
                raw_interval_max_seconds,
                low=LOOPER_MIN_INTERVAL_SECONDS,
                high=LOOPER_MAX_INTERVAL_SECONDS,
            )
            if raw_interval_max_seconds > 0
            else 0
        )
        runner_command = str(raw.get("runner_command", "")).strip()
        trigger_on_user_message = bool(raw.get("trigger_on_user_message", False))
        started_at = float(raw.get("started_at", 0.0))
        next_prompt_at = float(raw.get("next_prompt_at", 0.0))
        deadline_at = float(raw.get("deadline_at", 0.0))
        prompt_count = max(0, int(raw.get("prompt_count", 0)))
        last_prompt_at = float(raw.get("last_prompt_at", 0.0))
    except (TypeError, ValueError):
        return None

    if not window_id or (not plan_path and not runner_command) or not keyword:
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
        interval_max_seconds=interval_max_seconds,
        runner_command=runner_command,
        trigger_on_user_message=trigger_on_user_message,
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
    _legacy_looper_state_keys.clear()

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
        if len(raw_key.split(":")) == 2:
            _legacy_looper_state_keys.add(parsed_key)
        else:
            _legacy_looper_state_keys.discard(parsed_key)


def _save_state() -> None:
    if not _looper_state_loaded:
        return
    _legacy_looper_state_keys.intersection_update(_looper_state)
    try:
        if not _looper_state:
            _legacy_looper_state_keys.clear()
            if _LOOPER_STATE_FILE.exists():
                _LOOPER_STATE_FILE.unlink()
            return
        payload = {
            _persisted_key_to_string(key): state.to_dict()
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
    chat_id: int | None = None,
    thread_id: int,
    window_id: str,
    plan_path: str,
    keyword: str,
    interval_seconds: int = LOOPER_DEFAULT_INTERVAL_SECONDS,
    interval_max_seconds: int = 0,
    limit_seconds: int = 0,
    instructions: str = "",
    runner_command: str = "",
    trigger_on_user_message: bool = False,
    now: float | None = None,
) -> LooperState:
    """Create/replace looper config for a topic."""
    _load_state()

    plan = plan_path.strip()
    normalized_runner_command = runner_command.strip()
    if not plan and not normalized_runner_command:
        raise ValueError("plan_path is required")

    normalized_keyword = normalize_looper_keyword(keyword)
    if not _is_single_word(normalized_keyword):
        raise ValueError("keyword must be a single word")

    interval = _clamp_int(
        int(interval_seconds),
        low=LOOPER_MIN_INTERVAL_SECONDS,
        high=LOOPER_MAX_INTERVAL_SECONDS,
    )
    interval_high = 0
    if int(interval_max_seconds) > 0:
        interval_high = _clamp_int(
            int(interval_max_seconds),
            low=LOOPER_MIN_INTERVAL_SECONDS,
            high=LOOPER_MAX_INTERVAL_SECONDS,
        )
        if interval_high < interval:
            interval, interval_high = interval_high, interval
    limit = 0
    if int(limit_seconds) > 0:
        limit = _clamp_int(
            int(limit_seconds),
            low=LOOPER_MIN_LIMIT_SECONDS,
            high=LOOPER_MAX_LIMIT_SECONDS,
        )

    ts = now if now is not None else time.time()
    deadline_at = ts + limit if limit > 0 else 0.0
    next_interval = _sample_interval_seconds(
        interval_seconds=interval,
        interval_max_seconds=interval_high,
    )
    state = LooperState(
        window_id=window_id,
        plan_path=plan,
        keyword=normalized_keyword,
        instructions=instructions.strip(),
        interval_seconds=interval,
        interval_max_seconds=interval_high,
        runner_command=normalized_runner_command,
        trigger_on_user_message=bool(trigger_on_user_message),
        started_at=ts,
        next_prompt_at=ts + next_interval,
        deadline_at=deadline_at,
    )
    normalized_chat_id = int(chat_id or 0)
    _looper_state[_topic_key(user_id, thread_id, normalized_chat_id)] = state
    _save_state()

    emit_telemetry(
        "looper.started",
        user_id=user_id,
        chat_id=normalized_chat_id,
        thread_id=thread_id,
        window_id=window_id,
        plan_path=plan,
        keyword=normalized_keyword,
        interval_seconds=interval,
        interval_max_seconds=interval_high,
        limit_seconds=limit,
        has_instructions=bool(state.instructions),
        runner_mode=bool(state.runner_command),
        trigger_on_user_message=state.trigger_on_user_message,
    )
    return state


def stop_looper(
    *,
    user_id: int,
    chat_id: int | None = None,
    thread_id: int,
    reason: str = "manual",
) -> LooperState | None:
    """Stop looper for one topic."""
    _load_state()
    normalized_chat_id = int(chat_id or 0)
    key = _topic_key(user_id, thread_id, normalized_chat_id)
    state = _looper_state.pop(key, None)
    if state:
        _save_state()
        emit_telemetry(
            "looper.stopped",
            user_id=user_id,
            chat_id=normalized_chat_id,
            thread_id=thread_id,
            window_id=state.window_id,
            reason=reason,
            prompt_count=state.prompt_count,
        )
    return state


def get_looper_state(
    *,
    user_id: int,
    chat_id: int | None = None,
    thread_id: int,
) -> LooperState | None:
    """Return current looper state for a topic."""
    _load_state()
    return _looper_state.get(_topic_key(user_id, thread_id, chat_id))


def clear_looper_state(
    user_id: int,
    thread_id: int | None = None,
    chat_id: int | None = None,
) -> None:
    """Clear looper state for one topic."""
    if thread_id is None:
        return
    stop_looper(
        user_id=user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        reason="cleared",
    )


def prune_looper_topics(
    active_topic_keys: set[tuple[int, int] | tuple[int, int, int]],
) -> None:
    """Drop looper state for topics that are no longer bound."""
    _load_state()
    normalized_active_keys = {
        (int(key[0]), 0, int(key[1]))
        if len(key) == 2
        else (int(key[0]), int(key[1]), int(key[2]))
        for key in active_topic_keys
    }
    changed = False
    for key in list(_legacy_looper_state_keys):
        if key not in _looper_state or key[1] != 0:
            continue
        matches = {
            active_key
            for active_key in normalized_active_keys
            if active_key[0] == key[0]
            and active_key[2] == key[2]
            and active_key[1] != 0
        }
        if len(matches) != 1:
            continue
        target_key = next(iter(matches))
        if target_key in _looper_state:
            # A concrete scoped state is authoritative. Drop the superseded
            # legacy alias so a later prune can never revive it after clear.
            _looper_state.pop(key, None)
            _legacy_looper_state_keys.discard(key)
            changed = True
            continue
        _looper_state[target_key] = _looper_state.pop(key)
        _legacy_looper_state_keys.discard(key)
        changed = True

    stale = [
        key
        for key in _looper_state
        if key not in normalized_active_keys
        and not (key in _legacy_looper_state_keys and key[1] == 0)
    ]
    if not stale and not changed:
        return
    for key in stale:
        state = _looper_state.pop(key, None)
        _legacy_looper_state_keys.discard(key)
        if state:
            emit_telemetry(
                "looper.stopped",
                user_id=key[0],
                chat_id=key[1],
                thread_id=key[2],
                window_id=state.window_id,
                reason="stale_topic",
                prompt_count=state.prompt_count,
            )
    _save_state()


def stop_looper_if_expired(
    *,
    user_id: int,
    chat_id: int | None = None,
    thread_id: int,
    window_id: str,
    now: float | None = None,
) -> LooperState | None:
    """Stop and return looper state when its time limit has elapsed."""
    _load_state()
    normalized_chat_id = int(chat_id or 0)
    key = _topic_key(user_id, thread_id, normalized_chat_id)
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
        chat_id=normalized_chat_id,
        thread_id=thread_id,
        window_id=window_id,
        reason="time_limit_reached",
        prompt_count=state.prompt_count,
    )
    return state


def claim_due_looper_prompt(
    *,
    user_id: int,
    chat_id: int | None = None,
    thread_id: int,
    window_id: str,
    force: bool = False,
    now: float | None = None,
) -> DueLooperPrompt | None:
    """Claim one due prompt and schedule the next interval."""
    _load_state()
    normalized_chat_id = int(chat_id or 0)
    key = _topic_key(user_id, thread_id, normalized_chat_id)
    state = _looper_state.get(key)
    if not state:
        return None
    if state.window_id != window_id:
        return None

    ts = now if now is not None else time.time()
    if state.deadline_at > 0 and ts >= state.deadline_at:
        return None
    if not force and ts < state.next_prompt_at:
        return None

    state.prompt_count += 1
    state.last_prompt_at = ts
    scheduled_interval = _sample_interval_seconds(
        interval_seconds=state.interval_seconds,
        interval_max_seconds=state.interval_max_seconds,
    )
    state.next_prompt_at = ts + scheduled_interval
    _save_state()

    prompt_text = ""
    if not state.runner_command:
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
        chat_id=normalized_chat_id,
        thread_id=thread_id,
        window_id=window_id,
        prompt_count=state.prompt_count,
        interval_seconds=scheduled_interval,
        runner_mode=bool(state.runner_command),
        forced=force,
    )
    return DueLooperPrompt(
        user_id=user_id,
        thread_id=thread_id,
        window_id=window_id,
        prompt_text=prompt_text,
        plan_path=state.plan_path,
        keyword=state.keyword,
        instructions=state.instructions,
        interval_seconds=scheduled_interval,
        prompt_count=state.prompt_count,
        deadline_at=state.deadline_at,
        runner_command=state.runner_command,
        chat_id=normalized_chat_id,
    )


def delay_looper_next_prompt(
    *,
    user_id: int,
    chat_id: int | None = None,
    thread_id: int,
    delay_seconds: int = 60,
    now: float | None = None,
) -> None:
    """Bring next prompt closer when a claimed send failed."""
    _load_state()
    state = _looper_state.get(_topic_key(user_id, thread_id, chat_id))
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
    chat_id: int | None = None,
    thread_id: int,
    window_id: str,
    assistant_text: str,
) -> LooperState | None:
    """Stop looper when assistant response matches configured keyword."""
    _load_state()
    normalized_chat_id = int(chat_id or 0)
    key = _topic_key(user_id, thread_id, normalized_chat_id)
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
        chat_id=normalized_chat_id,
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
    _legacy_looper_state_keys.clear()
    _looper_state_loaded = False
    if not clear_persisted:
        return
    try:
        if _LOOPER_STATE_FILE.exists():
            _LOOPER_STATE_FILE.unlink()
    except OSError:
        pass
