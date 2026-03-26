"""Daily topic personality research backed by Telegram-visible memory."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import json
import logging
import re
from pathlib import Path

from ..config import config
from ..utils import atomic_write_json, coco_dir, env_alias
from . import research_backend

logger = logging.getLogger(__name__)

PERSONALITY_SESSION_GAP_SECONDS = 30 * 60
PERSONALITY_RESEARCH_HOUR_LOCAL = 2
PERSONALITY_DELIVERY_HOUR_LOCAL = 9
PERSONALITY_RESEARCH_BACKEND_HEURISTIC = research_backend.BACKEND_HEURISTIC
PERSONALITY_RESEARCH_BACKEND_EXTERNAL = research_backend.BACKEND_EXTERNAL

_PERSONALITY_STATE_FILE = coco_dir() / "personality_state.json"

_SUCCESS_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bworked\b",
        r"\busable\b",
        r"\bup\b",
        r"\bfixed\b",
        r"\bfound\b",
        r"\bverified\b",
        r"\bstarted\b",
        r"\bconnected\b",
        r"\bready\b",
        r"\bcomplete(?:d)?\b",
        r"\bsuccess(?:ful|fully)?\b",
    )
)
_FAILURE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bbroken\b",
        r"\bannoying\b",
        r"\bfrustrat(?:ed|ing)\b",
        r"\bhate\b",
        r"\bfail(?:ed|ure)?\b",
        r"\berror\b",
        r"\bstuck\b",
        r"\bnot working\b",
        r"\bcould not\b",
        r"\bcouldn't\b",
        r"\bunavailable\b",
        r"\brejected\b",
        r"\bwrong\b",
    )
)
_POSITIVE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\blove\b",
        r"\bgreat\b",
        r"\bperfect\b",
        r"\bgood\b",
        r"\bawesome\b",
        r"\bthanks?\b",
        r"\bnice\b",
        r"\bfast\b",
    )
)
_PROGRESS_TEXT_PREFIXES = (
    "⏳ working",
    "✅ process complete",
    "⎋ interrupted active turn",
)
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")
_STOPWORDS = {
    "about",
    "again",
    "around",
    "back",
    "been",
    "bot",
    "broke",
    "broken",
    "check",
    "checking",
    "coco",
    "does",
    "dont",
    "fix",
    "fixed",
    "flow",
    "from",
    "hand",
    "handoff",
    "have",
    "help",
    "how",
    "into",
    "its",
    "just",
    "keep",
    "login",
    "need",
    "please",
    "still",
    "that",
    "the",
    "them",
    "then",
    "there",
    "they",
    "this",
    "today",
    "use",
    "usable",
    "want",
    "more",
    "was",
    "reply",
    "draft",
    "great",
    "when",
    "with",
    "worked",
    "working",
    "would",
    "yesterday",
}
_UPPER_TOKENS = {"api", "gpu", "cpu", "ssh", "vnc"}


@dataclass(frozen=True)
class PersonalityDigest:
    """One generated daily digest for a topic."""

    target_date: str
    session_count: int
    success_count: int
    failure_count: int
    focus_terms: tuple[str, ...]
    positive_terms: tuple[str, ...]
    negative_terms: tuple[str, ...]
    message_text: str


@dataclass
class PersonalityTopicState:
    """Persisted once-per-topic personality state."""

    last_researched_for_date: str = ""
    last_delivered_for_date: str = ""
    last_digest_text: str = ""
    last_digest_generated_at: float = 0.0
    last_session_count: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "last_researched_for_date": self.last_researched_for_date,
            "last_delivered_for_date": self.last_delivered_for_date,
            "last_digest_text": self.last_digest_text,
            "last_digest_generated_at": self.last_digest_generated_at,
            "last_session_count": self.last_session_count,
        }


@dataclass(frozen=True)
class _MemoryEntry:
    ts: datetime
    direction: str
    text: str


_personality_state: dict[tuple[int, int], PersonalityTopicState] = {}
_personality_state_loaded = False


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


def _parse_state(raw: dict[str, object]) -> PersonalityTopicState | None:
    try:
        last_researched_for_date = str(raw.get("last_researched_for_date", "")).strip()
        last_delivered_for_date = str(raw.get("last_delivered_for_date", "")).strip()
        last_digest_text = str(raw.get("last_digest_text", "")).strip()
        last_digest_generated_at = float(raw.get("last_digest_generated_at", 0.0))
        last_session_count = max(0, int(raw.get("last_session_count", 0)))
    except (TypeError, ValueError):
        return None
    return PersonalityTopicState(
        last_researched_for_date=last_researched_for_date,
        last_delivered_for_date=last_delivered_for_date,
        last_digest_text=last_digest_text,
        last_digest_generated_at=last_digest_generated_at,
        last_session_count=last_session_count,
    )


def _load_state() -> None:
    global _personality_state_loaded
    if _personality_state_loaded:
        return
    _personality_state_loaded = True
    _personality_state.clear()

    if not _PERSONALITY_STATE_FILE.is_file():
        return
    try:
        payload = json.loads(_PERSONALITY_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Failed loading personality state (%s): %s", _PERSONALITY_STATE_FILE, exc)
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
        _personality_state[key] = state


def _save_state() -> None:
    if not _personality_state_loaded:
        return
    try:
        if not _personality_state:
            if _PERSONALITY_STATE_FILE.exists():
                _PERSONALITY_STATE_FILE.unlink()
            return
        payload = {
            _key_to_string(key): state.to_dict()
            for key, state in sorted(_personality_state.items())
        }
        atomic_write_json(_PERSONALITY_STATE_FILE, payload, indent=2)
    except OSError as exc:
        logger.debug("Failed saving personality state (%s): %s", _PERSONALITY_STATE_FILE, exc)


def reset_personality_state_for_tests(*, clear_persisted: bool = True) -> None:
    """Test helper to clear in-memory personality state."""
    global _personality_state_loaded
    _personality_state.clear()
    _personality_state_loaded = False
    if clear_persisted:
        try:
            _PERSONALITY_STATE_FILE.unlink(missing_ok=True)
        except OSError:
            pass


def prune_personality_topics(active_topic_keys: set[tuple[int, int]]) -> None:
    """Drop personality state for topics that are no longer active."""
    _load_state()
    stale = [key for key in _personality_state if key not in active_topic_keys]
    if not stale:
        return
    for key in stale:
        _personality_state.pop(key, None)
    _save_state()


def clear_personality_state(user_id: int, thread_id: int | None = None) -> None:
    """Clear personality state for one topic."""
    if thread_id is None:
        return
    _load_state()
    key = _topic_key(user_id, thread_id)
    if key not in _personality_state:
        return
    del _personality_state[key]
    _save_state()


def _memory_log_path() -> Path:
    raw = env_alias("COCO_TELEGRAM_MEMORY_LOG_PATH")
    if raw:
        return Path(raw).expanduser()
    return Path(__file__).resolve().parents[3] / "TELEGRAM_CHAT_MEMORY.jsonl"


def _local_timezone(now: datetime | None = None):
    reference = now if now is not None else datetime.now(UTC)
    return reference.astimezone().tzinfo or UTC


def _normalize_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC)
    return now


def _is_progress_text(text: str) -> bool:
    lowered = " ".join(text.strip().lower().split())
    return any(lowered.startswith(prefix) for prefix in _PROGRESS_TEXT_PREFIXES)


def _is_substantive_text(text: str) -> bool:
    value = text.strip()
    if not value:
        return False
    if _is_progress_text(value):
        return False
    return True


def _parse_memory_entries(
    *,
    user_id: int,
    chat_id: int,
    thread_id: int,
    target_date: date,
    tzinfo,
) -> list[_MemoryEntry]:
    path = _memory_log_path()
    if not path.is_file():
        return []

    relevant: list[_MemoryEntry] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict):
                    continue
                if int(data.get("chat_id", 0) or 0) != chat_id:
                    continue
                if int(data.get("thread_id", 0) or 0) != thread_id:
                    continue
                direction = str(data.get("direction", "")).strip()
                if direction == "in":
                    if int(data.get("from_user_id", 0) or 0) != user_id:
                        continue
                elif direction not in {"out_send", "out_edit"}:
                    continue
                text = str(data.get("text", "")).strip()
                if not _is_substantive_text(text):
                    continue
                raw_ts = str(data.get("ts_utc", "")).strip()
                if not raw_ts:
                    continue
                try:
                    ts = datetime.fromisoformat(raw_ts)
                except ValueError:
                    continue
                local_ts = ts.astimezone(tzinfo)
                if local_ts.date() != target_date:
                    continue
                relevant.append(
                    _MemoryEntry(
                        ts=local_ts,
                        direction=direction,
                        text=text,
                    )
                )
    except OSError as exc:
        logger.debug("Failed reading Telegram memory log (%s): %s", path, exc)
        return []

    relevant.sort(key=lambda entry: entry.ts)
    return relevant


def _split_sessions(entries: list[_MemoryEntry]) -> list[list[_MemoryEntry]]:
    sessions: list[list[_MemoryEntry]] = []
    current: list[_MemoryEntry] = []
    previous_ts: datetime | None = None
    for entry in entries:
        if (
            previous_ts is not None
            and (entry.ts - previous_ts).total_seconds() > PERSONALITY_SESSION_GAP_SECONDS
            and current
        ):
            sessions.append(current)
            current = []
        current.append(entry)
        previous_ts = entry.ts
    if current:
        sessions.append(current)
    return sessions


def _count_pattern_hits(texts: list[str], patterns: tuple[re.Pattern[str], ...]) -> int:
    hits = 0
    for text in texts:
        for pattern in patterns:
            if pattern.search(text):
                hits += 1
    return hits


def _extract_terms(texts: list[str], *, limit: int = 3) -> tuple[str, ...]:
    counts: Counter[str] = Counter()
    for text in texts:
        for raw in _TOKEN_RE.findall(text):
            token = raw.lower()
            if token in _STOPWORDS:
                continue
            if token.startswith("http"):
                continue
            counts[token] += 1
    ordered = [token for token, _count in counts.most_common(limit)]
    return tuple(_format_term(token) for token in ordered)


def _format_term(token: str) -> str:
    if token in _UPPER_TOKENS:
        return token.upper()
    if token.startswith("instagram"):
        return "Instagram"
    if token.startswith("telegram"):
        return "Telegram"
    return token.capitalize()


def _human_terms(terms: tuple[str, ...]) -> str:
    if not terms:
        return ""
    if len(terms) == 1:
        return terms[0]
    if len(terms) == 2:
        return f"{terms[0]} and {terms[1]}"
    return f"{terms[0]}, {terms[1]}, and {terms[2]}"


def _load_allowed_user_name(user_id: int) -> str:
    path = config.auth_meta_file
    try:
        if not path or not path.is_file():
            return ""
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    names = payload.get("names")
    if not isinstance(names, dict):
        return ""
    value = names.get(str(user_id), "")
    return str(value).strip()


def _research_backend() -> str:
    return research_backend.research_backend_mode("COCO_PERSONALITY")


def _serialize_sessions(sessions: list[list[_MemoryEntry]]) -> list[dict[str, object]]:
    payload: list[dict[str, object]] = []
    for index, session in enumerate(sessions, start=1):
        if not session:
            continue
        payload.append(
            {
                "session_index": index,
                "started_at": session[0].ts.isoformat(),
                "ended_at": session[-1].ts.isoformat(),
                "messages": [
                    {
                        "timestamp": entry.ts.isoformat(),
                        "direction": entry.direction,
                        "text": entry.text,
                    }
                    for entry in session
                ],
            }
        )
    return payload


def _external_program_markdown() -> str:
    return "\n".join(
        [
            "# Personality Research Run",
            "",
            "Read `sessions.json` and write `output.json`.",
            "",
            "Goals:",
            "- infer what the user seems to like or dislike about Coco",
            "- estimate bot success vs failure for the day",
            "- note where frustration showed up",
            "- keep the final morning note short and direct",
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


def _normalize_term_list(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    values: list[str] = []
    for item in raw:
        term = str(item).strip()
        if not term:
            continue
        values.append(term)
    return tuple(values[:3])


def _coerce_count(raw: object, *, default: int = 0) -> int:
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return default


def _run_external_personality_research(
    *,
    user_id: int,
    chat_id: int,
    thread_id: int,
    target_date: str,
    sessions: list[list[_MemoryEntry]],
) -> dict[str, object] | None:
    return research_backend.run_external_research(
        app_slug="personality",
        env_prefix="COCO_PERSONALITY",
        target_date=target_date,
        user_id=user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        bundle_payload={
            "user_id": user_id,
            "chat_id": chat_id,
            "thread_id": thread_id,
            "target_date": target_date,
            "sessions": _serialize_sessions(sessions),
        },
        program_markdown=_external_program_markdown(),
    )


def _build_external_digest(
    *,
    payload: dict[str, object],
    target_date: str,
    default_session_count: int,
    default_success_count: int,
    default_failure_count: int,
    default_focus_terms: tuple[str, ...],
    default_positive_terms: tuple[str, ...],
    default_negative_terms: tuple[str, ...],
) -> PersonalityDigest | None:
    message_text = str(payload.get("message_text", "")).strip()
    if not message_text:
        return None
    return PersonalityDigest(
        target_date=target_date,
        session_count=_coerce_count(
            payload.get("session_count"),
            default=default_session_count,
        ),
        success_count=_coerce_count(
            payload.get("success_count"),
            default=default_success_count,
        ),
        failure_count=_coerce_count(
            payload.get("failure_count"),
            default=default_failure_count,
        ),
        focus_terms=_normalize_term_list(payload.get("focus_terms")) or default_focus_terms,
        positive_terms=(
            _normalize_term_list(payload.get("positive_terms")) or default_positive_terms
        ),
        negative_terms=(
            _normalize_term_list(payload.get("negative_terms")) or default_negative_terms
        ),
        message_text=message_text,
    )


def _build_digest_text(
    *,
    name: str,
    session_count: int,
    success_count: int,
    failure_count: int,
    focus_terms: tuple[str, ...],
    positive_terms: tuple[str, ...],
    negative_terms: tuple[str, ...],
) -> str:
    salutation = f"Hey {name}," if name else "Hey,"
    lines = [f"{salutation} yesterday I learned a bit more about how you use Coco."]
    details: list[str] = [f"We had {session_count} session{'s' if session_count != 1 else ''}."]
    if focus_terms:
        details.append(f"You kept coming back to {_human_terms(focus_terms)}.")
    details.append(
        f"Bot fit looked like {success_count} success{'es' if success_count != 1 else ''} "
        f"vs {failure_count} rough spot{'s' if failure_count != 1 else ''}."
    )
    if positive_terms:
        details.append(f"You seemed happiest around {_human_terms(positive_terms)}.")
    if negative_terms:
        details.append(f"Friction showed up around {_human_terms(negative_terms)}.")
    lines.append(" ".join(details))
    return "\n\n".join(lines)


def generate_personality_digest(
    *,
    user_id: int,
    chat_id: int,
    thread_id: int,
    target_date: str,
) -> PersonalityDigest | None:
    """Generate one daily digest from Telegram-visible topic memory."""
    try:
        target = date.fromisoformat(target_date)
    except ValueError:
        return None

    tzinfo = _local_timezone()
    entries = _parse_memory_entries(
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
    sessions = _split_sessions(entries)
    success_count = _count_pattern_hits(outbound_texts, _SUCCESS_PATTERNS)
    failure_count = (
        _count_pattern_hits(inbound_texts, _FAILURE_PATTERNS)
        + _count_pattern_hits(outbound_texts, _FAILURE_PATTERNS)
    )
    positive_terms = _extract_terms(
        [text for text in inbound_texts if _count_pattern_hits([text], _POSITIVE_PATTERNS) > 0]
    )
    negative_terms = _extract_terms(
        [text for text in inbound_texts if _count_pattern_hits([text], _FAILURE_PATTERNS) > 0]
    )
    focus_terms = _extract_terms(inbound_texts)

    if _research_backend() != PERSONALITY_RESEARCH_BACKEND_HEURISTIC:
        external_payload = _run_external_personality_research(
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            target_date=target_date,
            sessions=sessions,
        )
        if external_payload is not None:
            external_digest = _build_external_digest(
                payload=external_payload,
                target_date=target_date,
                default_session_count=len(sessions),
                default_success_count=success_count,
                default_failure_count=failure_count,
                default_focus_terms=focus_terms,
                default_positive_terms=positive_terms,
                default_negative_terms=negative_terms,
            )
            if external_digest is not None:
                return external_digest

    name = _load_allowed_user_name(user_id)
    message_text = _build_digest_text(
        name=name,
        session_count=len(sessions),
        success_count=success_count,
        failure_count=failure_count,
        focus_terms=focus_terms,
        positive_terms=positive_terms,
        negative_terms=negative_terms,
    )
    return PersonalityDigest(
        target_date=target_date,
        session_count=len(sessions),
        success_count=success_count,
        failure_count=failure_count,
        focus_terms=focus_terms,
        positive_terms=positive_terms,
        negative_terms=negative_terms,
        message_text=message_text,
    )


def claim_due_personality_delivery(
    *,
    user_id: int,
    chat_id: int,
    thread_id: int,
    now: datetime | None = None,
) -> str | None:
    """Generate and claim a once-per-day morning personality digest."""
    _load_state()
    ts = _normalize_now(now)
    local_now = ts.astimezone(_local_timezone(ts))
    target_date = (local_now.date() - timedelta(days=1)).isoformat()
    key = _topic_key(user_id, thread_id)
    state = _personality_state.get(key) or PersonalityTopicState()

    if local_now.hour >= PERSONALITY_RESEARCH_HOUR_LOCAL:
        if state.last_researched_for_date != target_date:
            digest = generate_personality_digest(
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                target_date=target_date,
            )
            state.last_researched_for_date = target_date
            state.last_digest_text = digest.message_text if digest is not None else ""
            state.last_digest_generated_at = ts.timestamp()
            state.last_session_count = digest.session_count if digest is not None else 0
            _personality_state[key] = state
            _save_state()

    if local_now.hour < PERSONALITY_DELIVERY_HOUR_LOCAL:
        return None
    if state.last_researched_for_date != target_date:
        return None
    if not state.last_digest_text:
        return None
    if state.last_delivered_for_date == target_date:
        return None

    state.last_delivered_for_date = target_date
    _personality_state[key] = state
    _save_state()
    return state.last_digest_text
