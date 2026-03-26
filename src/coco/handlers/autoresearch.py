"""Generic daily auto research app backed by Telegram-visible memory."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import json
import logging
from pathlib import Path

from ..config import config
from ..utils import atomic_write_json, coco_dir
from . import personality as _personality, research_backend

logger = logging.getLogger(__name__)

AUTORESEARCH_RESEARCH_HOUR_LOCAL = _personality.PERSONALITY_RESEARCH_HOUR_LOCAL
AUTORESEARCH_DELIVERY_HOUR_LOCAL = _personality.PERSONALITY_DELIVERY_HOUR_LOCAL
AUTORESEARCH_RESEARCH_BACKEND_HEURISTIC = research_backend.BACKEND_HEURISTIC

_AUTORESEARCH_STATE_FILE = coco_dir() / "autoresearch_state.json"


@dataclass(frozen=True)
class AutoResearchDigest:
    """One generated daily digest for a topic and desired outcome."""

    target_date: str
    outcome: str
    session_count: int
    success_count: int
    failure_count: int
    focus_terms: tuple[str, ...]
    positive_terms: tuple[str, ...]
    negative_terms: tuple[str, ...]
    message_text: str


@dataclass
class AutoResearchState:
    """Persisted once-per-topic autoresearch state."""

    outcome: str = ""
    last_researched_for_date: str = ""
    last_delivered_for_date: str = ""
    last_digest_text: str = ""
    last_digest_generated_at: float = 0.0
    last_session_count: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome,
            "last_researched_for_date": self.last_researched_for_date,
            "last_delivered_for_date": self.last_delivered_for_date,
            "last_digest_text": self.last_digest_text,
            "last_digest_generated_at": self.last_digest_generated_at,
            "last_session_count": self.last_session_count,
        }


_autoresearch_state: dict[tuple[int, int], AutoResearchState] = {}
_autoresearch_state_loaded = False


def _topic_key(user_id: int, thread_id: int) -> tuple[int, int]:
    return user_id, thread_id


def _key_to_string(key: tuple[int, int]) -> str:
    return f"{key[0]}:{key[1]}"


def _parse_key(raw_key: str) -> tuple[int, int] | None:
    user_s, sep, thread_s = raw_key.partition(":")
    if not sep:
        return None
    try:
        return int(user_s), int(thread_s)
    except (TypeError, ValueError):
        return None


def _parse_state(raw: dict[str, object]) -> AutoResearchState | None:
    try:
        outcome = str(raw.get("outcome", "")).strip()
        last_researched_for_date = str(raw.get("last_researched_for_date", "")).strip()
        last_delivered_for_date = str(raw.get("last_delivered_for_date", "")).strip()
        last_digest_text = str(raw.get("last_digest_text", "")).strip()
        last_digest_generated_at = float(raw.get("last_digest_generated_at", 0.0))
        last_session_count = max(0, int(raw.get("last_session_count", 0)))
    except (TypeError, ValueError):
        return None
    return AutoResearchState(
        outcome=outcome,
        last_researched_for_date=last_researched_for_date,
        last_delivered_for_date=last_delivered_for_date,
        last_digest_text=last_digest_text,
        last_digest_generated_at=last_digest_generated_at,
        last_session_count=last_session_count,
    )


def _load_state() -> None:
    global _autoresearch_state_loaded
    if _autoresearch_state_loaded:
        return
    _autoresearch_state_loaded = True
    _autoresearch_state.clear()

    if not _AUTORESEARCH_STATE_FILE.is_file():
        return
    try:
        payload = json.loads(_AUTORESEARCH_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Failed loading autoresearch state (%s): %s", _AUTORESEARCH_STATE_FILE, exc)
        return
    if not isinstance(payload, dict):
        return
    for raw_key, raw_value in payload.items():
        if not isinstance(raw_key, str) or not isinstance(raw_value, dict):
            continue
        key = _parse_key(raw_key)
        if not key:
            continue
        state = _parse_state(raw_value)
        if state is None:
            continue
        _autoresearch_state[key] = state


def _save_state() -> None:
    if not _autoresearch_state_loaded:
        return
    try:
        if not _autoresearch_state:
            if _AUTORESEARCH_STATE_FILE.exists():
                _AUTORESEARCH_STATE_FILE.unlink()
            return
        payload = {
            _key_to_string(key): state.to_dict()
            for key, state in sorted(_autoresearch_state.items())
        }
        atomic_write_json(_AUTORESEARCH_STATE_FILE, payload, indent=2)
    except OSError as exc:
        logger.debug("Failed saving autoresearch state (%s): %s", _AUTORESEARCH_STATE_FILE, exc)


def reset_autoresearch_state_for_tests(*, clear_persisted: bool = True) -> None:
    """Test helper to clear in-memory autoresearch state."""
    global _autoresearch_state_loaded
    _autoresearch_state.clear()
    _autoresearch_state_loaded = False
    if clear_persisted:
        try:
            _AUTORESEARCH_STATE_FILE.unlink(missing_ok=True)
        except OSError:
            pass


def get_autoresearch_state(user_id: int, thread_id: int | None) -> AutoResearchState | None:
    """Return current autoresearch state for a topic."""
    if thread_id is None:
        return None
    _load_state()
    return _autoresearch_state.get(_topic_key(user_id, thread_id))


def set_autoresearch_outcome(
    *,
    user_id: int,
    thread_id: int,
    outcome: str,
) -> AutoResearchState:
    """Create or update the desired outcome for one topic."""
    _load_state()
    key = _topic_key(user_id, thread_id)
    current = _autoresearch_state.get(key) or AutoResearchState()
    updated = AutoResearchState(
        outcome=outcome.strip(),
        last_researched_for_date=current.last_researched_for_date,
        last_delivered_for_date=current.last_delivered_for_date,
        last_digest_text=current.last_digest_text,
        last_digest_generated_at=current.last_digest_generated_at,
        last_session_count=current.last_session_count,
    )
    _autoresearch_state[key] = updated
    _save_state()
    return updated


def clear_autoresearch_state(user_id: int, thread_id: int | None = None) -> None:
    """Clear autoresearch state for one topic."""
    if thread_id is None:
        return
    _load_state()
    key = _topic_key(user_id, thread_id)
    if key not in _autoresearch_state:
        return
    del _autoresearch_state[key]
    _save_state()


def prune_autoresearch_topics(active_topic_keys: set[tuple[int, int]]) -> None:
    """Drop autoresearch state for topics that are no longer active."""
    _load_state()
    stale = [key for key in _autoresearch_state if key not in active_topic_keys]
    if not stale:
        return
    for key in stale:
        _autoresearch_state.pop(key, None)
    _save_state()


def _build_autoresearch_text(
    *,
    name: str,
    outcome: str,
    session_count: int,
    success_count: int,
    failure_count: int,
    focus_terms: tuple[str, ...],
    positive_terms: tuple[str, ...],
    negative_terms: tuple[str, ...],
) -> str:
    salutation = f"Hey {name}," if name else "Hey,"
    lines = [
        (
            f"{salutation} yesterday I reviewed Coco against your goal: "
            f"`{outcome}`."
        )
    ]
    details: list[str] = [f"We had {session_count} session{'s' if session_count != 1 else ''}."]
    details.append(
        f"Bot fit looked like {success_count} success{'es' if success_count != 1 else ''} "
        f"vs {failure_count} rough spot{'s' if failure_count != 1 else ''}."
    )
    if focus_terms:
        details.append(f"The strongest themes were {_personality._human_terms(focus_terms)}.")
    if positive_terms:
        details.append(f"What helped most looked like {_personality._human_terms(positive_terms)}.")
    if negative_terms:
        details.append(f"Friction showed up around {_personality._human_terms(negative_terms)}.")
    lines.append(" ".join(details))
    return "\n\n".join(lines)


def _external_program_markdown(outcome: str) -> str:
    return "\n".join(
        [
            "# AutoResearch Run",
            "",
            "Read `sessions.json` and write a concise daily research result.",
            "",
            f"Desired outcome: {outcome}",
            "",
            "Goals:",
            "- assess how yesterday's visible Coco sessions tracked against the desired outcome",
            "- estimate bot success vs failure for the day",
            "- surface the strongest themes, what helped, and where friction showed up",
            "- keep the final note short and direct for Telegram delivery",
            "",
            "Output JSON schema:",
            '  {"message_text": str, "session_count": int, "success_count": int, "failure_count": int,',
            '   "focus_terms": [str], "positive_terms": [str], "negative_terms": [str]}',
            "",
            "Constraints:",
            "- Ground claims in the visible session text only.",
            "- Do not mention internal tools or hidden runtime details.",
            "- Keep `message_text` to 2 short paragraphs max.",
            "- Make `message_text` suitable for sending directly in Telegram at 9am.",
        ]
    )


def _build_external_digest(
    *,
    payload: dict[str, object],
    target_date: str,
    outcome: str,
    default_session_count: int,
    default_success_count: int,
    default_failure_count: int,
    default_focus_terms: tuple[str, ...],
    default_positive_terms: tuple[str, ...],
    default_negative_terms: tuple[str, ...],
) -> AutoResearchDigest | None:
    message_text = str(payload.get("message_text", "")).strip()
    if not message_text:
        return None
    return AutoResearchDigest(
        target_date=target_date,
        outcome=outcome,
        session_count=_personality._coerce_count(
            payload.get("session_count"),
            default=default_session_count,
        ),
        success_count=_personality._coerce_count(
            payload.get("success_count"),
            default=default_success_count,
        ),
        failure_count=_personality._coerce_count(
            payload.get("failure_count"),
            default=default_failure_count,
        ),
        focus_terms=_personality._normalize_term_list(payload.get("focus_terms")) or default_focus_terms,
        positive_terms=(
            _personality._normalize_term_list(payload.get("positive_terms"))
            or default_positive_terms
        ),
        negative_terms=(
            _personality._normalize_term_list(payload.get("negative_terms"))
            or default_negative_terms
        ),
        message_text=message_text,
    )


def generate_autoresearch_digest(
    *,
    user_id: int,
    chat_id: int,
    thread_id: int,
    target_date: str,
    outcome: str,
) -> AutoResearchDigest | None:
    """Generate one daily digest from Telegram-visible topic memory."""
    try:
        target = date.fromisoformat(target_date)
    except ValueError:
        return None
    desired_outcome = outcome.strip()
    if not desired_outcome:
        return None

    tzinfo = _personality._local_timezone()
    entries = _personality._parse_memory_entries(
        user_id=user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        target_date=target,
        tzinfo=tzinfo,
    )
    if not entries:
        return None

    inbound_texts = [entry.text for entry in entries if entry.direction == "in"]
    if not inbound_texts:
        return None

    outbound_texts = [entry.text for entry in entries if entry.direction != "in"]
    sessions = _personality._split_sessions(entries)
    success_count = _personality._count_pattern_hits(
        outbound_texts,
        _personality._SUCCESS_PATTERNS,
    )
    failure_count = (
        _personality._count_pattern_hits(inbound_texts, _personality._FAILURE_PATTERNS)
        + _personality._count_pattern_hits(outbound_texts, _personality._FAILURE_PATTERNS)
    )
    positive_terms = _personality._extract_terms(
        [
            text
            for text in inbound_texts
            if _personality._count_pattern_hits([text], _personality._POSITIVE_PATTERNS) > 0
        ]
    )
    negative_terms = _personality._extract_terms(
        [
            text
            for text in inbound_texts
            if _personality._count_pattern_hits([text], _personality._FAILURE_PATTERNS) > 0
        ]
    )
    focus_terms = _personality._extract_terms(inbound_texts)
    if research_backend.research_backend_mode("COCO_AUTORESEARCH") != AUTORESEARCH_RESEARCH_BACKEND_HEURISTIC:
        external_payload = research_backend.run_external_research(
            app_slug="autoresearch",
            env_prefix="COCO_AUTORESEARCH",
            target_date=target_date,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            bundle_payload={
                "user_id": user_id,
                "chat_id": chat_id,
                "thread_id": thread_id,
                "target_date": target_date,
                "outcome": desired_outcome,
                "sessions": _personality._serialize_sessions(sessions),
            },
            program_markdown=_external_program_markdown(desired_outcome),
        )
        if external_payload is not None:
            external_digest = _build_external_digest(
                payload=external_payload,
                target_date=target_date,
                outcome=desired_outcome,
                default_session_count=len(sessions),
                default_success_count=success_count,
                default_failure_count=failure_count,
                default_focus_terms=focus_terms,
                default_positive_terms=positive_terms,
                default_negative_terms=negative_terms,
            )
            if external_digest is not None:
                return external_digest

    name = _personality._load_allowed_user_name(user_id)

    message_text = _build_autoresearch_text(
        name=name,
        outcome=desired_outcome,
        session_count=len(sessions),
        success_count=success_count,
        failure_count=failure_count,
        focus_terms=focus_terms,
        positive_terms=positive_terms,
        negative_terms=negative_terms,
    )
    return AutoResearchDigest(
        target_date=target_date,
        outcome=desired_outcome,
        session_count=len(sessions),
        success_count=success_count,
        failure_count=failure_count,
        focus_terms=focus_terms,
        positive_terms=positive_terms,
        negative_terms=negative_terms,
        message_text=message_text,
    )


def claim_due_autoresearch_delivery(
    *,
    user_id: int,
    chat_id: int,
    thread_id: int,
    now: datetime | None = None,
) -> str | None:
    """Generate and claim a once-per-day morning auto research digest."""
    _load_state()
    ts = _personality._normalize_now(now)
    local_now = ts.astimezone(_personality._local_timezone(ts))
    target_date = (local_now.date() - timedelta(days=1)).isoformat()
    key = _topic_key(user_id, thread_id)
    state = _autoresearch_state.get(key) or AutoResearchState()

    if not state.outcome:
        return None

    if local_now.hour >= AUTORESEARCH_RESEARCH_HOUR_LOCAL:
        if state.last_researched_for_date != target_date:
            digest = generate_autoresearch_digest(
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                target_date=target_date,
                outcome=state.outcome,
            )
            state.last_researched_for_date = target_date
            state.last_digest_text = digest.message_text if digest is not None else ""
            state.last_digest_generated_at = ts.timestamp()
            state.last_session_count = digest.session_count if digest is not None else 0
            _autoresearch_state[key] = state
            _save_state()

    if local_now.hour < AUTORESEARCH_DELIVERY_HOUR_LOCAL:
        return None
    if state.last_researched_for_date != target_date:
        return None
    if not state.last_digest_text:
        return None
    if state.last_delivered_for_date == target_date:
        return None

    state.last_delivered_for_date = target_date
    _autoresearch_state[key] = state
    _save_state()
    return state.last_digest_text
