"""Session management — the core state hub.

Manages the key mappings:
  Window→Session (window_states): which session_id a window holds (keyed by window_id).
  User→TopicScope→Binding (topic_bindings_v2): canonical transport-neutral topic metadata.

Responsibilities:
  - Persist/load state to the configured CoCo state root.
  - Resolve window IDs to AssistantSession objects (JSONL file reading).
  - Track per-user read offsets for unread-message detection.
  - Manage chat+thread scoped bindings for Telegram topic routing.
  - Maintain window_id→display name mapping for UI display.
  - Re-resolve stale window IDs on startup.

Key class: SessionManager (singleton instantiated as `session_manager`).
Key methods for thread binding access:
  - resolve_window_for_thread: Get window_id for a user's topic
  - iter_topic_window_bindings: Iterate all (user_id, chat_id, thread_id, window_id)
  - find_users_for_session: Find all users bound to a session_id
"""

import asyncio
import json
import logging
import os
import re
import shlex
import time
from contextlib import asynccontextmanager
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any

import aiofiles

from .codex_app_server import CodexAppServerError, codex_app_server_client
from .config import config
from .node_registry import node_registry
from .runtime_capabilities import (
    get_transcription_runtime_summary,
    get_tts_runtime_summary,
)
from .skills import SkillDefinition, discover_skills, resolve_skill_identifier
from .telemetry import emit_telemetry
from .transcript_parser import TranscriptParser
from .utils import atomic_write_json, env_alias

logger = logging.getLogger(__name__)

GENERAL_TOPIC_THREAD_ID = 1


class _CodexAggregateResumeLimitError(CodexAppServerError):
    """Raised when another history would exceed the shared transport budget."""


class _CodexTurnTimeoutError(CodexAppServerError):
    """Turn timeout carrying the immutable thread/turn selected at dispatch."""

    def __init__(
        self,
        error: CodexAppServerError,
        *,
        method: str,
        thread_id: str,
        turn_id: str = "",
    ) -> None:
        super().__init__(str(error))
        self.method = method
        self.thread_id = thread_id.strip()
        self.turn_id = turn_id.strip()


@dataclass
class TopicSendDispatchState:
    """Mutable stage marker used to make queued-input replay decisions.

    Once transport dispatch begins, a caller must assume the request may have
    crossed the write boundary even when the eventual error text is generic.
    """

    transport_dispatch_started: bool = False

    def mark_transport_dispatch_started(self) -> None:
        self.transport_dispatch_started = True


@dataclass(frozen=True)
class TopicOwnership:
    """Immutable identity of one persisted Telegram topic binding."""

    window_id: str
    codex_thread_id: str
    machine_id: str
    cwd: str


APP_SERVER_MAX_TEXT_CHARS_PER_INPUT = 3000
APP_SERVER_TURN_START_TIMEOUT_SECONDS = 20.0
APP_SERVER_ACTIVE_WRITER_RE = re.compile(
    r"\b(?:already )?has an active writer\b",
    re.IGNORECASE,
)
APP_SERVER_UNCERTAIN_SEND_RE = re.compile(
    r"(?:request will not be replayed automatically|"
    r"uncertain request was not replayed|outcome is uncertain)",
    re.IGNORECASE,
)
APP_SERVER_RESUME_LIMIT_RE = re.compile(
    r"\btranscripts? exceeds? (?:aggregate )?resume limit\b",
    re.IGNORECASE,
)
APP_SERVER_THREAD_NOT_FOUND_RE = re.compile(r"\bthread not found\b", re.IGNORECASE)
APP_SERVER_TURN_STEER_TIMEOUT_RE = re.compile(
    r"Timed out waiting for app-server response:\s*turn/steer",
    re.IGNORECASE,
)
APP_SERVER_NO_ACTIVE_TURN_RE = re.compile(
    r"\bno active turn to steer\b",
    re.IGNORECASE,
)
APP_SERVER_NO_GOAL_EXISTS_RE = re.compile(
    r"\bno goal exists\b",
    re.IGNORECASE,
)
GOAL_CONTEXT_TRIGGER_RE = re.compile(
    r"(^|\s|[`'\"(])(?:/goal(?![\w-])|goal(?![\w-])|objective(?![\w-]))",
    re.IGNORECASE,
)
STATE_SCHEMA_VERSION = 6
TOPIC_BINDING_TRANSPORT_WINDOW = "window"
TOPIC_BINDING_TRANSPORT_CODEX_THREAD = "codex_thread"
TOPIC_SYNC_MODE_TELEGRAM_LIVE = "telegram_live"
TOPIC_SYNC_MODE_HOST_FOLLOW_FINAL = "host_follow_final"
EXPECTED_TRANSCRIPT_USER_ECHO_MAX_AGE_SECONDS = 120.0
CODEX_SERVICE_TIERS = frozenset({"fast", "flex"})
TRANSCRIPTION_PROFILES = frozenset({"compatible", "auto"})
SESSION_START_REASONS = frozenset(
    {"fresh_start", "resume", "after_clear", "oversized_rollover"}
)


@dataclass
class ExpectedTranscriptUserEcho:
    """One pending transcript echo expected from a Telegram-origin turn."""

    text: str
    created_at: float


@dataclass(frozen=True)
class CodexSessionSummary:
    """One Codex transcript discovered for a workspace path."""

    thread_id: str
    file_path: Path
    created_at: float
    last_active_at: float


@dataclass
class WindowState:
    """Persistent state for one session window.

    Attributes:
        session_id: Associated session ID (empty if not yet detected)
        cwd: Working directory for direct file path construction
        window_name: Display name of the window
        last_input_ts: Epoch timestamp of the last message sent to this window
        approval_mode: Per-window Codex approval mode override
        mention_only: Whether this window should only accept @mentions as input
        codex_thread_id: Codex app-server thread ID (Codex app-server transport)
        codex_active_turn_id: In-progress turn ID for codex_thread_id
        codex_transport_epoch: Process incarnation for the bound app-server state
        codex_transport_epoch_started_at: Wall-clock start of that incarnation
        codex_transport_generation: App-server generation within that incarnation
    """

    session_id: str = ""
    cwd: str = ""
    window_name: str = ""
    last_input_ts: float = 0.0
    approval_mode: str = ""
    mention_only: bool = False
    codex_thread_id: str = ""
    codex_active_turn_id: str = ""
    codex_transport_epoch: str = ""
    codex_transport_epoch_started_at: float = 0.0
    codex_transport_generation: int = 0

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "session_id": self.session_id,
            "cwd": self.cwd,
        }
        if self.window_name:
            d["window_name"] = self.window_name
        if self.last_input_ts > 0:
            d["last_input_ts"] = self.last_input_ts
        if self.approval_mode:
            d["approval_mode"] = self.approval_mode
        if self.mention_only:
            d["mention_only"] = True
        if self.codex_thread_id:
            d["codex_thread_id"] = self.codex_thread_id
        if self.codex_active_turn_id:
            d["codex_active_turn_id"] = self.codex_active_turn_id
        if self.codex_transport_epoch:
            d["codex_transport_epoch"] = self.codex_transport_epoch
        if self.codex_transport_epoch_started_at > 0:
            d["codex_transport_epoch_started_at"] = (
                self.codex_transport_epoch_started_at
            )
        if self.codex_transport_generation > 0:
            d["codex_transport_generation"] = self.codex_transport_generation
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WindowState":
        try:
            last_input_ts = float(data.get("last_input_ts", 0.0))
        except (TypeError, ValueError):
            last_input_ts = 0.0
        raw_mention_only = data.get("mention_only", False)
        if isinstance(raw_mention_only, bool):
            mention_only = raw_mention_only
        elif isinstance(raw_mention_only, (int, float)):
            mention_only = bool(raw_mention_only)
        elif isinstance(raw_mention_only, str):
            mention_only = raw_mention_only.strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
                "mention",
                "mentions",
                "mention_only",
            }
        else:
            mention_only = False

        def _text(key: str) -> str:
            value = data.get(key, "")
            return value if isinstance(value, str) else ""

        try:
            codex_transport_epoch_started_at = max(
                0.0,
                float(data.get("codex_transport_epoch_started_at", 0.0) or 0.0),
            )
        except (TypeError, ValueError):
            codex_transport_epoch_started_at = 0.0
        try:
            codex_transport_generation = max(
                0,
                int(data.get("codex_transport_generation", 0) or 0),
            )
        except (TypeError, ValueError):
            codex_transport_generation = 0

        return cls(
            session_id=_text("session_id"),
            cwd=_text("cwd"),
            window_name=_text("window_name"),
            last_input_ts=last_input_ts,
            approval_mode=_text("approval_mode"),
            mention_only=mention_only,
            codex_thread_id=_text("codex_thread_id"),
            codex_active_turn_id=_text("codex_active_turn_id"),
            codex_transport_epoch=_text("codex_transport_epoch"),
            codex_transport_epoch_started_at=codex_transport_epoch_started_at,
            codex_transport_generation=codex_transport_generation,
        )


@dataclass
class TopicBinding:
    """Transport-neutral topic binding metadata."""

    transport: str = TOPIC_BINDING_TRANSPORT_WINDOW
    chat_id: int = 0
    thread_id: int = 0
    window_id: str = ""
    codex_thread_id: str = ""
    cwd: str = ""
    display_name: str = ""
    sync_mode: str = TOPIC_SYNC_MODE_TELEGRAM_LIVE
    machine_id: str = ""
    machine_display_name: str = ""
    model_slug: str = ""
    reasoning_effort: str = ""
    model_selection_explicit: bool = False
    service_tier: str = ""
    response_mode: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "transport": self.transport,
        }
        if self.chat_id:
            d["chat_id"] = self.chat_id
        if self.thread_id:
            d["thread_id"] = self.thread_id
        if self.window_id:
            d["window_id"] = self.window_id
        if self.codex_thread_id:
            d["codex_thread_id"] = self.codex_thread_id
        if self.cwd:
            d["cwd"] = self.cwd
        if self.display_name:
            d["display_name"] = self.display_name
        if self.sync_mode != TOPIC_SYNC_MODE_TELEGRAM_LIVE:
            d["sync_mode"] = self.sync_mode
        if self.machine_id:
            d["machine_id"] = self.machine_id
        if self.machine_display_name:
            d["machine_display_name"] = self.machine_display_name
        if self.model_slug:
            d["model_slug"] = self.model_slug
        if self.reasoning_effort:
            d["reasoning_effort"] = self.reasoning_effort
        if self.model_slug or self.reasoning_effort:
            d["model_selection_explicit"] = self.model_selection_explicit
        if self.service_tier:
            d["service_tier"] = self.service_tier
        if self.response_mode:
            d["response_mode"] = self.response_mode
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TopicBinding":
        def _text(key: str) -> str:
            value = data.get(key, "")
            return value.strip() if isinstance(value, str) else ""

        raw_transport = data.get("transport", "")
        transport = str(raw_transport).strip().lower() if isinstance(raw_transport, str) else ""
        try:
            chat_id = int(data.get("chat_id", 0) or 0)
        except (TypeError, ValueError):
            chat_id = 0
        try:
            thread_id = int(data.get("thread_id", 0) or 0)
        except (TypeError, ValueError):
            thread_id = 0
        window_id = _text("window_id")
        codex_thread_id = _text("codex_thread_id")
        cwd = _text("cwd")
        display_name = _text("display_name")
        raw_sync_mode = data.get("sync_mode", TOPIC_SYNC_MODE_TELEGRAM_LIVE)
        sync_mode = SessionManager._normalize_topic_sync_mode(raw_sync_mode)
        machine_id = _text("machine_id")
        machine_display_name = _text("machine_display_name")
        model_slug = _text("model_slug")
        reasoning_effort = _text("reasoning_effort")
        if "model_selection_explicit" in data:
            model_selection_explicit = data.get("model_selection_explicit") is True
        else:
            model_selection_explicit = bool(model_slug or reasoning_effort)
        raw_service_tier = data.get("service_tier", "")
        service_tier = (
            str(raw_service_tier).strip().lower()
            if isinstance(raw_service_tier, str)
            else ""
        )
        if service_tier not in CODEX_SERVICE_TIERS:
            service_tier = ""
        raw_response_mode = data.get("response_mode", "")
        response_mode = (
            str(raw_response_mode).strip().lower()
            if isinstance(raw_response_mode, str)
            else ""
        )
        if response_mode not in {"text", "voice"}:
            response_mode = ""
        if transport not in {
            TOPIC_BINDING_TRANSPORT_WINDOW,
            TOPIC_BINDING_TRANSPORT_CODEX_THREAD,
        }:
            transport = (
                TOPIC_BINDING_TRANSPORT_CODEX_THREAD
                if codex_thread_id
                else TOPIC_BINDING_TRANSPORT_WINDOW
            )
        return cls(
            transport=transport,
            chat_id=chat_id,
            thread_id=thread_id,
            window_id=window_id,
            codex_thread_id=codex_thread_id,
            cwd=cwd,
            display_name=display_name,
            sync_mode=sync_mode,
            machine_id=machine_id,
            machine_display_name=machine_display_name,
            model_slug=model_slug,
            reasoning_effort=reasoning_effort,
            model_selection_explicit=model_selection_explicit,
            service_tier=service_tier,
            response_mode=response_mode,
        )


@dataclass
class CocoControlTopic:
    """Singleton CoCo control-topic assignment."""

    user_id: int
    thread_id: int
    chat_id: int = 0

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "user_id": self.user_id,
            "thread_id": self.thread_id,
        }
        if self.chat_id:
            payload["chat_id"] = self.chat_id
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CocoControlTopic | None":
        try:
            user_id = int(data.get("user_id", 0) or 0)
            thread_id = int(data.get("thread_id", 0) or 0)
            chat_id = int(data.get("chat_id", 0) or 0)
        except (TypeError, ValueError):
            return None
        if user_id <= 0 or thread_id <= 0:
            return None
        return cls(user_id=user_id, thread_id=thread_id, chat_id=chat_id)


@dataclass(frozen=True)
class CocoControlMigration:
    """Persisted result of moving the singleton control topic to General."""

    user_id: int
    chat_id: int
    previous_thread_id: int
    general_thread_id: int
    moved_history: bool


@dataclass
class SessionTranscript:
    """Information about a session transcript."""

    session_id: str
    summary: str
    message_count: int
    file_path: str


@dataclass
class SessionManager:
    """Manages session state for assistant transcripts.

    All internal keys use window_id (e.g. '@0', '@12') for uniqueness.
    Display names (window_name) are stored separately for UI presentation.

    window_states: window_id -> WindowState (session_id, cwd, window_name)
    user_window_offsets: user_id -> {window_id -> byte_offset}
    topic_bindings_v2: user_id -> {topic_slot_key -> TopicBinding}
    window_display_names: window_id -> window_name (for display)
    group_chat_ids: "user_id:thread_id" -> group chat_id (for supergroup routing)
    """

    window_states: dict[str, WindowState] = field(default_factory=dict)
    user_window_offsets: dict[int, dict[str, int]] = field(default_factory=dict)
    state_schema_version: int = STATE_SCHEMA_VERSION
    # user_id -> {thread_id -> TopicBinding}
    topic_bindings_v2: dict[int, dict[str, TopicBinding]] = field(default_factory=dict)
    # user_id -> {topic_slot_key -> [app_name, ...]} (legacy key name kept for compatibility)
    thread_skills: dict[int, dict[str, list[str]]] = field(default_factory=dict)
    # user_id -> {topic_slot_key -> [codex_skill_name, ...]}
    thread_codex_skills: dict[int, dict[str, list[str]]] = field(default_factory=dict)
    # window_id -> display name (window_name)
    window_display_names: dict[str, str] = field(default_factory=dict)
    # "user_id:thread_id" or "user_id:chat_id:thread_id" -> group chat_id
    # (for supergroup forum topic routing)
    # IMPORTANT: This mapping is essential for supergroup/forum topic support.
    # Telegram Bot API requires group chat_id (negative number like -100xxx)
    # as the chat_id parameter when sending messages to forum topics.
    # Using user_id as chat_id will fail with "Message thread not found".
    # See: https://core.telegram.org/bots/api#sendmessage
    # History: originally added in 5afc111, erroneously removed in 26cb81f,
    # restored in PR #23.
    group_chat_ids: dict[str, int] = field(default_factory=dict)
    # App-wide approval mode default used when window override is unset.
    default_approval_mode: str = ""
    # machine_id -> server-wide transcription profile selection
    machine_transcription_profiles: dict[str, str] = field(default_factory=dict)
    # Singleton control-topic assignment for `/coco`.
    coco_control_topic: CocoControlTopic | None = None
    # Per-window send/steer lock. Prevents concurrent turn mutations in one window.
    _window_send_locks: dict[str, asyncio.Lock] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _expected_transcript_user_echoes: dict[str, list[ExpectedTranscriptUserEcho]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _external_turn_active_by_window: dict[str, bool] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _pending_session_start_reason_by_window: dict[str, str] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _next_topic_response_mode: dict[tuple[int, int, int], str] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _session_file_path_cache: dict[str, Path] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _codex_resume_admission_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        init=False,
        repr=False,
    )
    _codex_resume_admission_transport_state: tuple[str, float, int] = field(
        default=("", 0.0, 0),
        init=False,
        repr=False,
    )
    _codex_resume_bytes_by_thread: dict[str, int] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _codex_resume_paths_by_thread: dict[str, Path] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _transport_uncertainty_handler: (
        Callable[[set[str], str], None] | None
    ) = field(
        default=None,
        init=False,
        repr=False,
    )
    _remote_transport_result_handler: (
        Callable[[str, dict[str, Any]], Awaitable[bool]] | None
    ) = field(
        default=None,
        init=False,
        repr=False,
    )

    @staticmethod
    def is_codex_active_writer_error(error: object) -> bool:
        """Return whether an app-server failure is a pre-dispatch writer conflict."""
        return APP_SERVER_ACTIVE_WRITER_RE.search(str(error)) is not None

    @staticmethod
    def is_codex_uncertain_send_result(error: object) -> bool:
        """Return whether replay could duplicate a request that may have dispatched."""
        return APP_SERVER_UNCERTAIN_SEND_RE.search(str(error)) is not None

    @staticmethod
    def is_codex_resume_limit_error(error: object) -> bool:
        """Return whether an exact resume was rejected before dispatch for size."""
        return APP_SERVER_RESUME_LIMIT_RE.search(str(error)) is not None

    def __post_init__(self) -> None:
        self._load_state()

    def _get_window_send_lock(self, window_id: str) -> asyncio.Lock:
        """Get or create a per-window lock for send/steer operations."""
        lock = self._window_send_locks.get(window_id)
        if lock is None:
            lock = asyncio.Lock()
            self._window_send_locks[window_id] = lock
        return lock

    @asynccontextmanager
    async def _window_send_context(
        self,
        window_id: str,
        *,
        remote_thread_id: str | None = None,
        remote_cwd: str = "",
        remote_window_name: str = "",
        remote_approval_mode: str = "",
        result_snapshot: dict[str, str] | None = None,
    ) -> AsyncIterator[None]:
        """Hold the send lock while applying an optional remote dispatch config."""
        lock = self._get_window_send_lock(window_id)
        async with lock:
            if remote_thread_id is not None:
                state = self.get_window_state(window_id)
                changed = False
                normalized_cwd = (
                    str(Path(remote_cwd).expanduser().resolve())
                    if remote_cwd
                    else ""
                )
                if normalized_cwd and state.cwd != normalized_cwd:
                    state.cwd = normalized_cwd
                    changed = True
                if remote_window_name and state.window_name != remote_window_name:
                    state.window_name = remote_window_name
                    changed = True
                if remote_approval_mode and state.approval_mode != remote_approval_mode:
                    state.approval_mode = remote_approval_mode
                    changed = True
                normalized_thread_id = remote_thread_id.strip()
                if state.codex_thread_id != normalized_thread_id:
                    state.codex_thread_id = normalized_thread_id
                    state.codex_active_turn_id = ""
                    changed = True
                if changed:
                    self._save_state()
            try:
                yield
            finally:
                if result_snapshot is not None:
                    state = self.get_window_state(window_id)
                    result_snapshot["thread_id"] = state.codex_thread_id.strip()
                    result_snapshot["turn_id"] = state.codex_active_turn_id.strip()

    def set_transport_uncertainty_handler(
        self,
        handler: Callable[[set[str], str], None] | None,
    ) -> None:
        """Register the controller callback for uncertain remote mutations."""
        self._transport_uncertainty_handler = handler

    def _note_transport_uncertainty(
        self,
        *,
        window_ids: set[str],
        reason: str,
    ) -> None:
        handler = self._transport_uncertainty_handler
        if handler is not None:
            handler(window_ids, reason)

    def set_remote_transport_result_handler(
        self,
        handler: Callable[[str, dict[str, Any]], Awaitable[bool]] | None,
    ) -> None:
        """Register the controller validator for remote transport snapshots."""
        self._remote_transport_result_handler = handler

    async def _accept_remote_transport_result(
        self,
        *,
        window_id: str,
        result: dict[str, Any],
    ) -> bool:
        handler = self._remote_transport_result_handler
        if handler is not None and not await handler(window_id, result):
            self.clear_window_codex_turn(window_id)
            return False

        epoch = str(result.get("transport_epoch", "")).strip()
        try:
            epoch_started_at = max(
                0.0,
                float(result.get("transport_epoch_started_at", 0.0) or 0.0),
            )
        except (TypeError, ValueError):
            epoch_started_at = 0.0
        try:
            generation = max(
                0,
                int(result.get("transport_generation", 0) or 0),
            )
        except (TypeError, ValueError):
            generation = 0
        if epoch and epoch_started_at > 0 and generation > 0:
            self.set_window_codex_transport_state(
                window_id,
                epoch=epoch,
                epoch_started_at=epoch_started_at,
                generation=generation,
            )
        return True

    def _save_state(self) -> None:
        topic_bindings = self._collect_topic_bindings()
        self.topic_bindings_v2 = topic_bindings
        state: dict[str, Any] = {
            "state_schema_version": STATE_SCHEMA_VERSION,
            "window_states": {k: v.to_dict() for k, v in self.window_states.items()},
            "user_window_offsets": {
                str(uid): offsets for uid, offsets in self.user_window_offsets.items()
            },
            "topic_bindings_v2": {
                str(uid): {
                    slot_key: binding.to_dict()
                    for slot_key, binding in bindings.items()
                }
                for uid, bindings in topic_bindings.items()
            },
            "thread_skills": {
                str(uid): {
                    slot_key: [str(name) for name in names if isinstance(name, str) and name]
                    for slot_key, names in bindings.items()
                }
                for uid, bindings in self.thread_skills.items()
            },
            "thread_codex_skills": {
                str(uid): {
                    slot_key: [str(name) for name in names if isinstance(name, str) and name]
                    for slot_key, names in bindings.items()
                }
                for uid, bindings in self.thread_codex_skills.items()
            },
            "window_display_names": self.window_display_names,
            "group_chat_ids": self.group_chat_ids,
        }
        if self.default_approval_mode:
            state["default_approval_mode"] = self.default_approval_mode
        if self.machine_transcription_profiles:
            state["machine_transcription_profiles"] = self.machine_transcription_profiles
        if self.coco_control_topic is not None:
            state["coco_control_topic"] = self.coco_control_topic.to_dict()
        atomic_write_json(config.state_file, state)
        logger.debug("State saved to %s", config.state_file)

    def _topic_binding_from_window(self, window_id: str) -> TopicBinding:
        state = self.window_states.get(window_id)
        display_name = self.window_display_names.get(window_id, "")
        if not display_name and state and state.window_name:
            display_name = state.window_name
        machine_id, machine_display_name = self._local_machine_identity()
        return TopicBinding(
            transport=TOPIC_BINDING_TRANSPORT_WINDOW,
            chat_id=0,
            thread_id=0,
            window_id=window_id,
            codex_thread_id=state.codex_thread_id.strip() if state else "",
            cwd=state.cwd.strip() if state else "",
            display_name=display_name.strip(),
            sync_mode=TOPIC_SYNC_MODE_TELEGRAM_LIVE,
            machine_id=machine_id,
            machine_display_name=machine_display_name,
        )

    @staticmethod
    def _normalize_topic_sync_mode(raw_mode: object) -> str:
        if isinstance(raw_mode, str):
            mode = raw_mode.strip().lower()
            if mode == TOPIC_SYNC_MODE_HOST_FOLLOW_FINAL:
                return TOPIC_SYNC_MODE_HOST_FOLLOW_FINAL
        return TOPIC_SYNC_MODE_TELEGRAM_LIVE

    @staticmethod
    def _topic_slot_key(*, thread_id: int, chat_id: int | None = None) -> str:
        if chat_id is None:
            return str(thread_id)
        return f"{chat_id}:{thread_id}"

    @staticmethod
    def _local_machine_identity() -> tuple[str, str]:
        node = node_registry.get_node(node_registry.local_machine_id)
        if node is not None:
            return node.machine_id, node.display_name
        machine_id = config.machine_id.strip()
        machine_name = config.machine_name.strip() or machine_id
        return machine_id, machine_name

    @classmethod
    def _parse_topic_slot_key(cls, raw_key: str) -> tuple[int | None, int]:
        try:
            if ":" not in raw_key:
                return None, int(raw_key)
            left, right = raw_key.split(":", 1)
            return int(left), int(right)
        except (TypeError, ValueError):
            return None, 0

    def _collect_topic_bindings(self) -> dict[int, dict[str, TopicBinding]]:
        combined: dict[int, dict[str, TopicBinding]] = {}

        for user_id, bindings in self.topic_bindings_v2.items():
            per_user: dict[str, TopicBinding] = {}
            for slot_key, binding in bindings.items():
                per_user[slot_key] = TopicBinding(
                    transport=binding.transport,
                    chat_id=binding.chat_id,
                    thread_id=binding.thread_id,
                    window_id=binding.window_id,
                    codex_thread_id=binding.codex_thread_id,
                    cwd=binding.cwd,
                    display_name=binding.display_name,
                    sync_mode=binding.sync_mode,
                    machine_id=binding.machine_id,
                    machine_display_name=binding.machine_display_name,
                    model_slug=binding.model_slug,
                    reasoning_effort=binding.reasoning_effort,
                    model_selection_explicit=binding.model_selection_explicit,
                    service_tier=binding.service_tier,
                    response_mode=binding.response_mode,
                )
            if per_user:
                combined[user_id] = per_user

        return combined

    def _find_topic_slot_key(
        self,
        user_id: int,
        thread_id: int,
        *,
        chat_id: int | None = None,
    ) -> str | None:
        per_user = self.topic_bindings_v2.get(user_id, {})
        scoped_chat_id = chat_id
        if scoped_chat_id is None:
            # Recover chat scope from persisted group routing map when available.
            resolved_chat_id = self.resolve_chat_id(user_id, thread_id)
            if resolved_chat_id != user_id:
                scoped_chat_id = resolved_chat_id
        if scoped_chat_id is not None:
            scoped = self._topic_slot_key(thread_id=thread_id, chat_id=scoped_chat_id)
            if scoped in per_user:
                return scoped
        legacy = self._topic_slot_key(thread_id=thread_id, chat_id=None)
        if legacy in per_user:
            return legacy
        matches: list[str] = []
        for slot_key in per_user:
            parsed_chat_id, parsed_thread_id = self._parse_topic_slot_key(slot_key)
            if parsed_thread_id != thread_id:
                continue
            if (
                scoped_chat_id is not None
                and parsed_chat_id is not None
                and parsed_chat_id != scoped_chat_id
            ):
                continue
            matches.append(slot_key)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1 and scoped_chat_id is None:
            logger.debug(
                "Ambiguous topic binding for user=%d thread=%d; provide chat_id",
                user_id,
                thread_id,
            )
        return None

    def _get_persisted_topic_binding(
        self,
        user_id: int,
        thread_id: int,
        *,
        chat_id: int | None = None,
    ) -> TopicBinding | None:
        """Return the raw persisted binding without window-state fallbacks."""
        slot_key = self._find_topic_slot_key(
            user_id,
            thread_id,
            chat_id=chat_id,
        )
        if slot_key is None:
            return None
        return self.topic_bindings_v2.get(user_id, {}).get(slot_key)

    def _topic_binding_ownership_matches(
        self,
        user_id: int,
        thread_id: int,
        *,
        chat_id: int | None,
        window_id: str,
        codex_thread_id: str,
        machine_id: str,
        cwd: str,
    ) -> bool:
        """Return whether a topic still has the ownership captured before an await."""
        binding = self._get_persisted_topic_binding(
            user_id,
            thread_id,
            chat_id=chat_id,
        )
        return bool(
            binding is not None
            and binding.window_id.strip() == window_id.strip()
            and binding.codex_thread_id.strip() == codex_thread_id.strip()
            and binding.machine_id.strip() == machine_id.strip()
            and binding.cwd.strip() == cwd.strip()
        )

    def _window_topic_ownership_snapshot(
        self,
        window_id: str,
    ) -> tuple[tuple[int, str, str, str, str], ...]:
        """Capture raw canonical topic ownership for one window.

        Window state is only a cache.  Recovery paths use this snapshot to
        prove that no persisted topic moved to another window/thread while an
        app-server await was in flight.
        """
        normalized_window_id = window_id.strip()
        return tuple(
            sorted(
                (
                    user_id,
                    slot_key,
                    binding.codex_thread_id.strip(),
                    binding.machine_id.strip(),
                    binding.cwd.strip(),
                )
                for user_id, bindings in self.topic_bindings_v2.items()
                for slot_key, binding in bindings.items()
                if binding.window_id.strip() == normalized_window_id
                and binding.codex_thread_id.strip()
            )
        )

    def _get_persisted_window_codex_thread_id(self, window_id: str) -> str:
        """Return one raw canonical thread ID persisted for a window."""
        thread_ids = {
            binding.codex_thread_id.strip()
            for bindings in self.topic_bindings_v2.values()
            for binding in bindings.values()
            if binding.window_id.strip() == window_id
            and binding.codex_thread_id.strip()
        }
        if len(thread_ids) == 1:
            return next(iter(thread_ids))
        if len(thread_ids) > 1:
            logger.error(
                "Conflicting persisted Codex topic bindings for window %s: %s",
                window_id,
                sorted(thread_ids),
            )
        return ""

    def _is_window_id(self, key: str) -> bool:
        """Check if a key looks like a window ID (e.g. '@0', '@12')."""
        return key.startswith("@") and len(key) > 1 and key[1:].isdigit()

    @staticmethod
    def _is_recoverable_window_state(state: WindowState | None) -> bool:
        """Return whether stale state has enough metadata for lazy recovery.

        A non-empty cwd lets bot handlers recreate a missing session on the
        next user message instead of forcing an immediate unbind at startup.
        """
        if not isinstance(state, WindowState):
            return False
        return bool(state.cwd.strip())

    def _load_state(self) -> None:
        """Load state synchronously during initialization.

        Detects old-format state (window_name keys without '@' prefix) and
        marks for migration on next startup re-resolution.
        """
        if config.state_file.exists():
            try:
                state = json.loads(config.state_file.read_text())
                if not isinstance(state, dict):
                    raise ValueError("state root must be a JSON object")
                raw_schema_version = state.get("state_schema_version", 1)
                try:
                    self.state_schema_version = int(raw_schema_version)
                except (TypeError, ValueError):
                    self.state_schema_version = 1
                if self.state_schema_version < 1:
                    self.state_schema_version = 1
                raw_window_states = state.get("window_states", {})
                if not isinstance(raw_window_states, dict):
                    raw_window_states = {}
                self.window_states = {
                    str(key): WindowState.from_dict(value)
                    for key, value in raw_window_states.items()
                    if isinstance(key, str) and isinstance(value, dict)
                }
                raw_offsets = state.get("user_window_offsets", {})
                if not isinstance(raw_offsets, dict):
                    raw_offsets = {}
                self.user_window_offsets = {}
                for raw_uid, raw_window_offsets in raw_offsets.items():
                    if not isinstance(raw_window_offsets, dict):
                        continue
                    try:
                        uid = int(raw_uid)
                    except (TypeError, ValueError):
                        continue
                    offsets: dict[str, int] = {}
                    for raw_window_id, raw_offset in raw_window_offsets.items():
                        if not isinstance(raw_window_id, str):
                            continue
                        try:
                            offset = int(raw_offset)
                        except (TypeError, ValueError, OverflowError):
                            continue
                        if offset >= 0:
                            offsets[raw_window_id] = offset
                    if offsets:
                        self.user_window_offsets[uid] = offsets
                raw_topic_bindings = state.get("topic_bindings_v2", {})
                if not isinstance(raw_topic_bindings, dict):
                    raw_topic_bindings = {}
                parsed_topic_bindings: dict[int, dict[str, TopicBinding]] = {}
                bindings_changed = False
                local_machine_id, local_machine_name = self._local_machine_identity()
                for uid, bindings in raw_topic_bindings.items():
                    if not isinstance(bindings, dict):
                        continue
                    try:
                        user_id = int(uid)
                    except (TypeError, ValueError):
                        continue
                    per_user: dict[str, TopicBinding] = {}
                    for raw_slot_key, raw_binding in bindings.items():
                        if not isinstance(raw_binding, dict):
                            continue
                        slot_key = str(raw_slot_key)
                        parsed_chat_id, parsed_thread_id = self._parse_topic_slot_key(slot_key)
                        if parsed_thread_id <= 0:
                            continue
                        binding = TopicBinding.from_dict(raw_binding)
                        if binding.thread_id <= 0:
                            binding.thread_id = parsed_thread_id
                        if binding.chat_id == 0 and parsed_chat_id is not None:
                            binding.chat_id = parsed_chat_id
                        if not binding.machine_id:
                            binding.machine_id = local_machine_id
                            bindings_changed = True
                        if not binding.machine_display_name:
                            if binding.machine_id == local_machine_id:
                                binding.machine_display_name = local_machine_name
                                bindings_changed = True
                            else:
                                node = node_registry.get_node(binding.machine_id)
                                if node is not None and node.display_name:
                                    binding.machine_display_name = node.display_name
                                    bindings_changed = True
                        normalized_slot = self._topic_slot_key(
                            thread_id=binding.thread_id,
                            chat_id=binding.chat_id or None,
                        )
                        per_user[normalized_slot] = binding
                    if per_user:
                        parsed_topic_bindings[user_id] = per_user
                self.topic_bindings_v2 = parsed_topic_bindings
                raw_thread_apps = state.get("thread_apps")
                if not isinstance(raw_thread_apps, dict):
                    raw_thread_apps = state.get("thread_skills", {})
                    if not isinstance(raw_thread_apps, dict):
                        raw_thread_apps = {}
                raw_thread_codex_skills = state.get("thread_codex_skills", {})
                if not isinstance(raw_thread_codex_skills, dict):
                    raw_thread_codex_skills = {}
                self.thread_skills = {}
                for uid, bindings in raw_thread_apps.items():
                    if not isinstance(bindings, dict):
                        continue
                    try:
                        user_id = int(uid)
                    except (TypeError, ValueError):
                        continue
                    per_user: dict[str, list[str]] = {}
                    for raw_slot_key, names in bindings.items():
                        if not isinstance(names, list):
                            continue
                        per_user[str(raw_slot_key)] = [
                            str(name)
                            for name in names
                            if isinstance(name, str) and str(name).strip()
                        ]
                    if per_user:
                        self.thread_skills[user_id] = per_user
                self.thread_codex_skills = {}
                for uid, bindings in raw_thread_codex_skills.items():
                    if not isinstance(bindings, dict):
                        continue
                    try:
                        user_id = int(uid)
                    except (TypeError, ValueError):
                        continue
                    per_user: dict[str, list[str]] = {}
                    for raw_slot_key, names in bindings.items():
                        if not isinstance(names, list):
                            continue
                        per_user[str(raw_slot_key)] = [
                            str(name)
                            for name in names
                            if isinstance(name, str) and str(name).strip()
                        ]
                    if per_user:
                        self.thread_codex_skills[user_id] = per_user
                raw_display_names = state.get("window_display_names", {})
                self.window_display_names = (
                    {
                        str(key): value
                        for key, value in raw_display_names.items()
                        if isinstance(key, str) and isinstance(value, str)
                    }
                    if isinstance(raw_display_names, dict)
                    else {}
                )
                raw_group_chat_ids = state.get("group_chat_ids", {})
                self.group_chat_ids = {}
                if isinstance(raw_group_chat_ids, dict):
                    for key, raw_chat_id in raw_group_chat_ids.items():
                        try:
                            self.group_chat_ids[str(key)] = int(raw_chat_id)
                        except (TypeError, ValueError):
                            continue
                raw_default_mode = state.get("default_approval_mode", "")
                self.default_approval_mode = (
                    raw_default_mode.strip()
                    if isinstance(raw_default_mode, str)
                    else ""
                )
                raw_machine_transcription_profiles = state.get(
                    "machine_transcription_profiles",
                    {},
                )
                if not isinstance(raw_machine_transcription_profiles, dict):
                    raw_machine_transcription_profiles = {}
                self.machine_transcription_profiles = {}
                for raw_machine_id, raw_profile in raw_machine_transcription_profiles.items():
                    machine_id = str(raw_machine_id).strip()
                    if not machine_id or not isinstance(raw_profile, str):
                        continue
                    normalized_profile = raw_profile.strip().lower()
                    if normalized_profile not in TRANSCRIPTION_PROFILES:
                        continue
                    self.machine_transcription_profiles[machine_id] = normalized_profile
                raw_coco_control_topic = state.get("coco_control_topic")
                if isinstance(raw_coco_control_topic, dict):
                    self.coco_control_topic = CocoControlTopic.from_dict(
                        raw_coco_control_topic
                    )
                else:
                    self.coco_control_topic = None

                # Detect old format: keys that don't look like window IDs
                needs_migration = False
                for k in self.window_states:
                    if not self._is_window_id(k):
                        needs_migration = True
                        break
                if not needs_migration:
                    for bindings in self.topic_bindings_v2.values():
                        for binding in bindings.values():
                            wid = binding.window_id.strip()
                            if not wid:
                                continue
                            if not self._is_window_id(wid):
                                needs_migration = True
                                break
                        if needs_migration:
                            break

                if needs_migration:
                    logger.info(
                        "Detected old-format state (window_name keys), "
                        "will re-resolve on startup"
                    )
                    pass

                if bindings_changed or self.state_schema_version < STATE_SCHEMA_VERSION:
                    self.state_schema_version = STATE_SCHEMA_VERSION
                    self._save_state()

            except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
                logger.warning("Failed to load state: %s", e)
                self.window_states = {}
                self.user_window_offsets = {}
                self.state_schema_version = STATE_SCHEMA_VERSION
                self.topic_bindings_v2 = {}
                self.thread_skills = {}
                self.thread_codex_skills = {}
                self.window_display_names = {}
                self.group_chat_ids = {}
                self.default_approval_mode = ""
                self.machine_transcription_profiles = {}
                self.coco_control_topic = None
                pass

    async def resolve_stale_ids(self) -> None:
        """Remove legacy non-window-id keys from persisted state."""
        changed = False

        window_states: dict[str, WindowState] = {}
        for key, value in self.window_states.items():
            if self._is_window_id(key):
                window_states[key] = value
            else:
                changed = True
        self.window_states = window_states

        window_display_names: dict[str, str] = {}
        for key, value in self.window_display_names.items():
            if self._is_window_id(key):
                window_display_names[key] = value
            else:
                changed = True
        self.window_display_names = window_display_names

        for user_id, bindings in list(self.topic_bindings_v2.items()):
            cleaned: dict[str, TopicBinding] = {}
            for slot_key, binding in bindings.items():
                wid = binding.window_id.strip()
                if wid and not self._is_window_id(wid):
                    changed = True
                    continue
                cleaned[slot_key] = binding
            self.topic_bindings_v2[user_id] = cleaned

        for user_id, offsets in list(self.user_window_offsets.items()):
            cleaned_offsets = {
                window_id: offset
                for window_id, offset in offsets.items()
                if self._is_window_id(window_id)
            }
            if len(cleaned_offsets) != len(offsets):
                changed = True
            self.user_window_offsets[user_id] = cleaned_offsets

        if changed:
            self._save_state()
            logger.info("Removed legacy stale window-id state entries")

    def current_window_session_map(self) -> dict[str, str]:
        """Return in-memory window_id -> session_id map for active windows."""
        result: dict[str, str] = {}
        for user_id, chat_id, thread_id, window_id in self.iter_topic_window_bindings():
            state = self.window_states.get(window_id)
            if state is None or not self._is_window_id(window_id) or not state.session_id:
                continue
            persisted = self._get_persisted_topic_binding(
                user_id,
                thread_id,
                chat_id=chat_id,
            )
            canonical_thread_id = (
                persisted.codex_thread_id.strip() if persisted is not None else ""
            )
            if not canonical_thread_id or state.session_id != canonical_thread_id:
                continue
            result[window_id] = state.session_id
        return result

    def _extract_codex_session_meta(self, file_path: Path) -> tuple[str, str] | None:
        """Read Codex session meta (session id + cwd) from a JSONL file."""
        try:
            with file_path.open("r", encoding="utf-8") as f:
                for _ in range(25):
                    line = f.readline()
                    if not line:
                        break
                    data = TranscriptParser.parse_line(line)
                    if not data or data.get("type") != "session_meta":
                        continue
                    payload = data.get("payload", {})
                    if not isinstance(payload, dict):
                        continue
                    session_id = payload.get("id", "")
                    cwd = payload.get("cwd", "")
                    if isinstance(session_id, str) and isinstance(cwd, str):
                        if session_id and cwd:
                            return session_id, cwd
                    break
        except OSError:
            return None
        return None

    @staticmethod
    def _parse_transcript_timestamp(raw_timestamp: object) -> float:
        """Parse a transcript ISO timestamp into epoch seconds."""
        if not isinstance(raw_timestamp, str):
            return 0.0
        value = raw_timestamp.strip()
        if not value:
            return 0.0
        if value.endswith("Z"):
            value = f"{value[:-1]}+00:00"
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return 0.0

    def _extract_codex_session_summary(
        self,
        file_path: Path,
        *,
        resumable_only: bool = False,
    ) -> tuple[CodexSessionSummary, str] | None:
        """Read Codex session summary + cwd from a transcript JSONL file."""
        try:
            stat = file_path.stat()
        except OSError:
            return None

        thread_id = ""
        file_cwd = ""
        created_at = 0.0
        try:
            with file_path.open("r", encoding="utf-8") as f:
                for _ in range(25):
                    line = f.readline()
                    if not line:
                        break
                    data = TranscriptParser.parse_line(line)
                    if not data:
                        continue
                    if created_at <= 0:
                        created_at = self._parse_transcript_timestamp(
                            TranscriptParser.get_timestamp(data)
                        )
                    if data.get("type") != "session_meta":
                        continue
                    payload = data.get("payload", {})
                    if not isinstance(payload, dict):
                        continue
                    if resumable_only and self._is_codex_subagent_session_meta(payload):
                        return None
                    raw_thread_id = payload.get("id", "")
                    raw_cwd = payload.get("cwd", "")
                    if isinstance(raw_thread_id, str) and raw_thread_id.strip():
                        thread_id = raw_thread_id.strip()
                    if isinstance(raw_cwd, str) and raw_cwd.strip():
                        file_cwd = raw_cwd.strip()
                    if thread_id and file_cwd:
                        break
        except OSError:
            return None

        if not thread_id or not file_cwd:
            return None
        if created_at <= 0:
            created_at = stat.st_mtime
        return (
            CodexSessionSummary(
                thread_id=thread_id,
                file_path=file_path,
                created_at=created_at,
                last_active_at=stat.st_mtime,
            ),
            file_cwd,
        )

    @staticmethod
    def _is_codex_subagent_session_meta(payload: dict[str, Any]) -> bool:
        """Return whether session metadata identifies a non-resumable sub-agent."""
        thread_source = payload.get("thread_source")
        if isinstance(thread_source, str) and thread_source.strip().lower() == "subagent":
            return True
        source = payload.get("source")
        if isinstance(source, str):
            return source.strip().lower() == "subagent"
        return isinstance(source, dict) and "subagent" in source

    @staticmethod
    def _extract_codex_session_model_selection(file_path: Path) -> tuple[str, str]:
        """Read the last observed model/effort from a Codex transcript JSONL file."""
        model_slug = ""
        reasoning_effort = ""
        try:
            with file_path.open("r", encoding="utf-8") as f:
                for line in f:
                    data = TranscriptParser.parse_line(line)
                    if not data or data.get("type") != "turn_context":
                        continue
                    payload = data.get("payload", {})
                    if not isinstance(payload, dict):
                        continue
                    raw_model = payload.get("model")
                    if isinstance(raw_model, str) and raw_model.strip():
                        model_slug = raw_model.strip()
                    raw_effort = payload.get("effort")
                    if isinstance(raw_effort, str) and raw_effort.strip():
                        reasoning_effort = raw_effort.strip()
                        continue
                    raw_reasoning_effort = payload.get("reasoning_effort")
                    if isinstance(raw_reasoning_effort, str) and raw_reasoning_effort.strip():
                        reasoning_effort = raw_reasoning_effort.strip()
                        continue
                    collaboration_mode = payload.get("collaboration_mode")
                    if not isinstance(collaboration_mode, dict):
                        continue
                    settings = collaboration_mode.get("settings")
                    if not isinstance(settings, dict):
                        continue
                    raw_collab_effort = settings.get("reasoning_effort")
                    if isinstance(raw_collab_effort, str) and raw_collab_effort.strip():
                        reasoning_effort = raw_collab_effort.strip()
        except OSError:
            return "", ""
        return model_slug, reasoning_effort

    def _find_codex_session_file_for_thread(
        self,
        thread_id: str,
        *,
        cwd: str = "",
        limit: int = 300,
    ) -> Path | None:
        """Locate the transcript file for one Codex thread id."""
        normalized_thread_id = thread_id.strip()
        if not normalized_thread_id or config.session_provider != "codex":
            return None
        if cwd:
            for summary in self.list_codex_session_summaries_for_cwd(cwd, limit=limit):
                if summary.thread_id == normalized_thread_id:
                    return summary.file_path
        if not config.sessions_path.exists():
            return None
        candidates = sorted(
            config.sessions_path.glob("**/*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for file_path in candidates:
            extracted = self._extract_codex_session_summary(file_path)
            if not extracted:
                continue
            summary, _file_cwd = extracted
            if summary.thread_id == normalized_thread_id:
                return file_path
        return None

    def get_codex_session_model_selection_for_thread(
        self,
        thread_id: str,
        *,
        cwd: str = "",
    ) -> tuple[str, str]:
        """Return the persisted model/effort for a Codex thread transcript."""
        file_path = self._find_codex_session_file_for_thread(thread_id, cwd=cwd)
        if file_path is None:
            return "", ""
        return self._extract_codex_session_model_selection(file_path)

    def sync_window_topic_model_selection_from_codex_session(
        self,
        *,
        window_id: str,
        codex_thread_id: str,
        cwd: str = "",
    ) -> tuple[bool, str, str]:
        """Sync a window's bound topic model selection from a resumed Codex session."""
        model_slug, reasoning_effort = self.get_codex_session_model_selection_for_thread(
            codex_thread_id,
            cwd=cwd,
        )
        if not model_slug and not reasoning_effort:
            return False, "", ""
        changed = self.inherit_window_topic_model_selection(
            window_id=window_id,
            model_slug=model_slug,
            reasoning_effort=reasoning_effort,
        )
        return changed, model_slug, reasoning_effort

    def inherit_window_topic_model_selection(
        self,
        *,
        window_id: str,
        model_slug: str,
        reasoning_effort: str,
    ) -> bool:
        """Fill unset topic selections from resumed local or remote session metadata."""
        changed = self._sync_topic_bindings_for_window_model_selection(
            window_id=window_id,
            model_slug=model_slug,
            reasoning_effort=reasoning_effort,
        )
        if changed:
            self._save_state()
        return changed

    def _codex_cwd_matches(self, target_cwd: str, file_cwd: str) -> bool:
        """Return True when the Codex transcript cwd exactly matches window cwd."""
        return file_cwd == target_cwd

    @staticmethod
    def _normalized_cwd_key(cwd: str) -> str:
        try:
            return str(Path(cwd).resolve())
        except (OSError, ValueError):
            return cwd

    @staticmethod
    def _select_latest_session_summary(
        summaries: list[CodexSessionSummary], *, prefer_recent_since: float = 0.0
    ) -> tuple[str, Path] | None:
        matching = [
            (summary.last_active_at, summary.thread_id, summary.file_path)
            for summary in summaries
        ]
        if not matching:
            return None

        if prefer_recent_since > 0:
            cutoff = prefer_recent_since - 2.0
            matching = [item for item in matching if item[0] >= cutoff]
            if not matching:
                return None

        _mtime, sid, path = max(matching, key=lambda item: item[0])
        return sid, path

    def _find_latest_session_for_cwd(
        self, cwd: str, *, prefer_recent_since: float = 0.0
    ) -> tuple[str, Path] | None:
        """Find the most recent session transcript that matches cwd.

        For Codex, when ``prefer_recent_since`` is set, only sessions updated
        after that timestamp are considered. This avoids binding to stale
        transcripts when a new window is created in a directory with old history.
        """
        summaries = self.list_codex_session_summaries_for_cwd(cwd)
        return self._select_latest_session_summary(
            summaries,
            prefer_recent_since=prefer_recent_since,
        )

    def get_latest_codex_session_id_for_cwd(self, cwd: str) -> str:
        """Return latest Codex session/thread id for an exact workspace cwd."""
        discovered = self._find_latest_session_for_cwd(cwd)
        if not discovered:
            return ""
        session_id, _path = discovered
        return session_id

    def list_codex_session_summaries_for_cwd(
        self,
        cwd: str,
        *,
        limit: int = 100,
    ) -> list[CodexSessionSummary]:
        """Return resumable Codex sessions for an exact workspace cwd."""
        try:
            target_cwd = str(Path(cwd).resolve())
        except (OSError, ValueError):
            target_cwd = cwd
        if not config.sessions_path.exists():
            return []

        summaries: list[CodexSessionSummary] = []
        seen_thread_ids: set[str] = set()
        candidates = sorted(
            config.sessions_path.glob("**/*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:300]
        for file_path in candidates:
            extracted = self._extract_codex_session_summary(
                file_path,
                resumable_only=True,
            )
            if not extracted:
                continue
            summary, file_cwd = extracted
            if summary.thread_id in seen_thread_ids:
                continue
            try:
                normalized_file_cwd = str(Path(file_cwd).resolve())
            except (OSError, ValueError):
                normalized_file_cwd = file_cwd
            if not self._codex_cwd_matches(target_cwd, normalized_file_cwd):
                continue
            summaries.append(summary)
            seen_thread_ids.add(summary.thread_id)
            if len(summaries) >= limit:
                break
        return summaries

    def _autodiscover_session_for_window_from_summaries(
        self, window_id: str, summaries: list[CodexSessionSummary]
    ) -> bool:
        state = self.get_window_state(window_id)
        if not state.cwd.strip():
            return False
        if not state.session_id and state.last_input_ts <= 0:
            return False

        canonical_thread_id = self._get_persisted_window_codex_thread_id(window_id)
        if not canonical_thread_id:
            if state.session_id:
                state.session_id = ""
                self._save_state()
            return False
        if canonical_thread_id:
            canonical_summary = next(
                (
                    summary
                    for summary in summaries
                    if summary.thread_id == canonical_thread_id
                ),
                None,
            )
            if canonical_summary is None:
                canonical_path = self._find_codex_session_file_for_thread(
                    canonical_thread_id,
                    cwd=state.cwd.strip(),
                )
                if canonical_path is None:
                    if state.session_id and state.session_id != canonical_thread_id:
                        rejected_session_id = state.session_id
                        state.session_id = ""
                        self._save_state()
                        logger.warning(
                            "Cleared noncanonical session association for %s "
                            "(bound=%s rejected=%s)",
                            window_id,
                            canonical_thread_id,
                            rejected_session_id,
                        )
                        emit_telemetry(
                            "session.autodiscovery.noncanonical_cleared",
                            window_id=window_id,
                            bound_thread_id=canonical_thread_id,
                            rejected_thread_id=rejected_session_id,
                        )
                    return False
                discovered = (canonical_thread_id, canonical_path)
            else:
                discovered = (
                    canonical_summary.thread_id,
                    canonical_summary.file_path,
                )
        else:
            discovered = self._select_latest_session_summary(
                summaries,
                prefer_recent_since=state.last_input_ts,
            )
        if not discovered:
            return False

        session_id, _ = discovered
        if state.session_id == session_id:
            return True

        state.session_id = session_id
        self._save_state()
        logger.info(
            "Auto-associated window %s -> session %s",
            window_id,
            session_id,
        )
        return True

    async def autodiscover_session_for_window(self, window_id: str) -> bool:
        """Auto-associate transcript session metadata for one window binding."""
        state = self.get_window_state(window_id)
        cwd = state.cwd.strip()
        if not cwd:
            return False
        summaries = self.list_codex_session_summaries_for_cwd(cwd)
        return self._autodiscover_session_for_window_from_summaries(
            window_id,
            summaries,
        )

    async def autodiscover_sessions_for_bound_windows(self) -> None:
        """Auto-associate sessions for all currently bound windows."""
        bound_window_ids = {
            window_id for _, _, _, window_id in self.iter_topic_window_bindings()
        }
        summaries_by_cwd: dict[str, list[CodexSessionSummary]] = {}
        for window_id in bound_window_ids:
            try:
                state = self.get_window_state(window_id)
                cwd = state.cwd.strip()
                if not cwd:
                    continue
                cwd_key = self._normalized_cwd_key(cwd)
                summaries = summaries_by_cwd.get(cwd_key)
                if summaries is None:
                    summaries = self.list_codex_session_summaries_for_cwd(cwd)
                    summaries_by_cwd[cwd_key] = summaries
                self._autodiscover_session_for_window_from_summaries(
                    window_id,
                    summaries,
                )
            except Exception as e:
                logger.debug("Autodiscovery failed for window %s: %s", window_id, e)

    # --- Display name management ---

    def get_display_name(self, window_id: str) -> str:
        """Get display name for a window_id, fallback to window_id itself."""
        return self.window_display_names.get(window_id, window_id)

    # --- Group chat ID management (supergroup forum topic routing) ---

    def set_group_chat_id(
        self, user_id: int, thread_id: int | None, chat_id: int
    ) -> None:
        """Store the group chat_id for a user+topic combination.

        In supergroups with forum topics, messages must be sent to the group's
        chat_id (negative number like -100xxx) rather than the user's personal ID.
        Telegram's Bot API rejects message_thread_id when chat_id is a private
        user ID — the thread only exists within the group context.

        DO NOT REMOVE this method or the group_chat_ids mapping.
        Without it, all outbound messages in forum topics fail with
        "Message thread not found". See commit history: 5afc111 → 26cb81f → PR #23.
        """
        tid = thread_id or 0
        slot_key = self._topic_slot_key(
            thread_id=tid,
            chat_id=chat_id if thread_id is not None else None,
        )
        key = f"{user_id}:{slot_key}"
        legacy_key = f"{user_id}:{tid}"
        changed = False
        if self.group_chat_ids.get(key) != chat_id:
            self.group_chat_ids[key] = chat_id
            changed = True
        # Keep legacy key updated so callers that only know (user, thread)
        # still resolve to the most recently seen chat scope.
        if self.group_chat_ids.get(legacy_key) != chat_id:
            self.group_chat_ids[legacy_key] = chat_id
            changed = True
        if changed:
            self._save_state()
            logger.debug(
                "Stored group chat_id: user=%d, thread=%s, chat_id=%d",
                user_id,
                thread_id,
                chat_id,
            )

    def resolve_chat_id(
        self,
        user_id: int,
        thread_id: int | None = None,
        *,
        chat_id: int | None = None,
    ) -> int:
        """Resolve the correct chat_id for sending messages.

        Returns the stored group chat_id when a thread_id is present and a
        mapping exists, otherwise falls back to user_id (for private chats).

        Every outbound Telegram API call (send_message, edit_message_text,
        delete_message, send_chat_action, edit_forum_topic, etc.) MUST use
        this method instead of raw user_id. Using user_id directly breaks
        supergroup forum topic routing.
        """
        if thread_id is not None:
            if chat_id is not None:
                scoped_key = (
                    f"{user_id}:"
                    f"{self._topic_slot_key(thread_id=thread_id, chat_id=chat_id)}"
                )
                group_id = self.group_chat_ids.get(scoped_key)
                if group_id is not None:
                    return group_id
            legacy_key = f"{user_id}:{thread_id}"
            group_id = self.group_chat_ids.get(legacy_key)
            if group_id is not None:
                return group_id
            if chat_id is None:
                suffix = f":{thread_id}"
                matches = [
                    gid
                    for key, gid in self.group_chat_ids.items()
                    if key.startswith(f"{user_id}:") and key.endswith(suffix)
                ]
                if len(matches) == 1:
                    return matches[0]
        return user_id

    async def wait_for_session_map_entry(
        self, window_id: str, timeout: float = 5.0, interval: float = 0.5
    ) -> bool:
        _ = window_id, timeout, interval
        return False

    async def load_session_map(self) -> None:
        return None

    # --- Window state management ---

    def get_window_state(self, window_id: str) -> WindowState:
        """Get or create window state."""
        if window_id not in self.window_states:
            self.window_states[window_id] = WindowState()
        return self.window_states[window_id]

    def clear_window_session(self, window_id: str) -> None:
        """Clear session association for a window (e.g., after /clear command)."""
        state = self.get_window_state(window_id)
        state.session_id = ""
        self.mark_window_pending_session_start_reason(window_id, "after_clear")
        self._save_state()
        logger.info("Cleared session for window_id %s", window_id)

    @staticmethod
    def _normalize_session_start_reason(reason: str) -> str:
        """Normalize one-shot session-start reason labels."""
        normalized = reason.strip().lower()
        return normalized if normalized in SESSION_START_REASONS else ""

    def mark_window_pending_session_start_reason(self, window_id: str, reason: str) -> None:
        """Remember a one-shot session-start reason for the next Codex turn."""
        normalized_window_id = window_id.strip()
        normalized_reason = self._normalize_session_start_reason(reason)
        if not normalized_window_id:
            return
        if not normalized_reason:
            self._pending_session_start_reason_by_window.pop(normalized_window_id, None)
            return
        self._pending_session_start_reason_by_window[normalized_window_id] = normalized_reason

    def peek_window_pending_session_start_reason(self, window_id: str) -> str:
        """Return the pending one-shot session-start reason for a window, if any."""
        return self._pending_session_start_reason_by_window.get(window_id.strip(), "")

    def consume_window_pending_session_start_reason(self, window_id: str) -> str:
        """Consume and return the pending one-shot session-start reason for a window."""
        return self._pending_session_start_reason_by_window.pop(window_id.strip(), "")

    def get_window_approval_mode(self, window_id: str) -> str:
        """Get the per-window approval mode override (empty means inherit)."""
        state = self.get_window_state(window_id)
        return state.approval_mode if isinstance(state.approval_mode, str) else ""

    def set_window_approval_mode(self, window_id: str, mode: str) -> None:
        """Set per-window approval mode override.

        Args:
            window_id: Tmux window id (e.g. "@12")
            mode: Approval mode override, or empty string to inherit default
        """
        state = self.get_window_state(window_id)
        normalized = mode.strip()
        if state.approval_mode == normalized:
            return
        state.approval_mode = normalized
        self._save_state()

    def get_window_mention_only(self, window_id: str) -> bool:
        """Return whether this window only accepts explicit @mentions."""
        state = self.get_window_state(window_id)
        return bool(state.mention_only)

    def set_window_mention_only(self, window_id: str, mention_only: bool) -> None:
        """Persist mention-only mode for a window."""
        state = self.get_window_state(window_id)
        normalized = bool(mention_only)
        if state.mention_only == normalized:
            return
        state.mention_only = normalized
        self._save_state()

    def get_default_approval_mode(self) -> str:
        """Get app-wide approval mode default (empty means inherit command default)."""
        return self.default_approval_mode.strip()

    def set_default_approval_mode(self, mode: str) -> None:
        """Persist app-wide approval mode default."""
        normalized = mode.strip()
        if self.default_approval_mode == normalized:
            return
        self.default_approval_mode = normalized
        self._save_state()

    def get_window_codex_thread_id(self, window_id: str) -> str:
        """Get Codex app-server thread id for a window (empty if unset)."""
        state = self.get_window_state(window_id)
        value = state.codex_thread_id.strip()
        return value

    def register_expected_transcript_user_echo(self, window_id: str, text: str) -> None:
        """Record one transcript user_message expected from a Telegram-origin turn."""
        normalized_window_id = window_id.strip()
        normalized_text = text.strip()
        if not normalized_window_id or not normalized_text:
            return
        pending = self._expected_transcript_user_echoes.setdefault(
            normalized_window_id,
            [],
        )
        pending.append(
            ExpectedTranscriptUserEcho(
                text=normalized_text,
                created_at=time.monotonic(),
            )
        )
        if len(pending) > 12:
            del pending[:-12]

    def consume_expected_transcript_user_echo(
        self,
        window_id: str,
        text: str,
        *,
        max_age_seconds: float = EXPECTED_TRANSCRIPT_USER_ECHO_MAX_AGE_SECONDS,
    ) -> bool:
        """Return True when transcript text matches a recent Telegram-origin send."""
        normalized_window_id = window_id.strip()
        normalized_text = text.strip()
        if not normalized_window_id or not normalized_text:
            return False
        pending = self._expected_transcript_user_echoes.get(normalized_window_id)
        if not pending:
            return False

        now = time.monotonic()
        keep: list[ExpectedTranscriptUserEcho] = []
        matched = False
        for echo in pending:
            if now - echo.created_at > max_age_seconds:
                continue
            if not matched and echo.text == normalized_text:
                matched = True
                continue
            keep.append(echo)
        if keep:
            self._expected_transcript_user_echoes[normalized_window_id] = keep
        else:
            self._expected_transcript_user_echoes.pop(normalized_window_id, None)
        return matched

    def is_window_external_turn_active(self, window_id: str) -> bool:
        """Return whether this window is currently controlled by an external host turn."""
        return bool(self._external_turn_active_by_window.get(window_id.strip(), False))

    def set_window_external_turn_active(self, window_id: str, active: bool) -> None:
        """Mark whether a host-driven external turn is active for this window."""
        normalized_window_id = window_id.strip()
        if not normalized_window_id:
            return
        if active:
            self._external_turn_active_by_window[normalized_window_id] = True
            return
        self._external_turn_active_by_window.pop(normalized_window_id, None)

    def get_topic_sync_mode(
        self,
        user_id: int,
        thread_id: int | None,
        *,
        chat_id: int | None = None,
    ) -> str:
        """Return the sync mode for one Telegram topic."""
        binding = self.resolve_topic_binding(user_id, thread_id, chat_id=chat_id)
        if binding is None:
            return TOPIC_SYNC_MODE_TELEGRAM_LIVE
        return self._normalize_topic_sync_mode(binding.sync_mode)

    def set_topic_sync_mode(
        self,
        user_id: int,
        thread_id: int | None,
        mode: str,
        *,
        chat_id: int | None = None,
    ) -> bool:
        """Persist the sync mode for one Telegram topic."""
        if thread_id is None:
            return False
        slot_key = self._find_topic_slot_key(user_id, thread_id, chat_id=chat_id)
        if slot_key is None:
            return False
        binding = self.topic_bindings_v2.get(user_id, {}).get(slot_key)
        if binding is None:
            return False
        normalized_mode = self._normalize_topic_sync_mode(mode)
        if binding.sync_mode == normalized_mode:
            return False
        binding.sync_mode = normalized_mode
        self._save_state()
        return True

    def mark_topic_telegram_live(
        self,
        *,
        user_id: int,
        thread_id: int | None,
        window_id: str,
        chat_id: int | None = None,
    ) -> None:
        """Restore a topic to Telegram live control after a successful Telegram send."""
        if thread_id is not None:
            self.set_topic_sync_mode(
                user_id,
                thread_id,
                TOPIC_SYNC_MODE_TELEGRAM_LIVE,
                chat_id=chat_id,
            )
        self.set_window_external_turn_active(window_id, False)

    def _sync_topic_bindings_for_window_codex_thread(
        self,
        *,
        window_id: str,
        thread_id: str,
    ) -> bool:
        """Keep topic bindings in sync when a window's Codex thread id changes."""
        normalized = thread_id.strip()
        changed = False
        for bindings in self.topic_bindings_v2.values():
            for binding in bindings.values():
                if binding.window_id.strip() != window_id:
                    continue
                if binding.codex_thread_id == normalized:
                    continue
                binding.codex_thread_id = normalized
                changed = True
        return changed

    def _sync_topic_bindings_for_window_model_selection(
        self,
        *,
        window_id: str,
        model_slug: str,
        reasoning_effort: str,
    ) -> bool:
        """Fill unset topic selections when a window inherits a session model."""
        normalized_model = model_slug.strip()
        normalized_effort = reasoning_effort.strip()
        changed = False
        for bindings in self.topic_bindings_v2.values():
            for binding in bindings.values():
                if binding.window_id.strip() != window_id:
                    continue
                if binding.model_selection_explicit:
                    continue
                if normalized_model and binding.model_slug != normalized_model:
                    binding.model_slug = normalized_model
                    changed = True
                if normalized_effort and binding.reasoning_effort != normalized_effort:
                    binding.reasoning_effort = normalized_effort
                    changed = True
        return changed

    def set_window_codex_thread_id(self, window_id: str, thread_id: str) -> None:
        """Persist Codex app-server thread id for a window."""
        state = self.get_window_state(window_id)
        normalized = thread_id.strip()
        changed = False
        if state.codex_thread_id != normalized:
            state.codex_thread_id = normalized
            changed = True
        if not normalized and state.codex_active_turn_id:
            state.codex_active_turn_id = ""
            changed = True
        if self._sync_topic_bindings_for_window_codex_thread(
            window_id=window_id,
            thread_id=normalized,
        ):
            changed = True
        if changed:
            self._save_state()

    def _set_window_codex_thread_cache(self, window_id: str, thread_id: str) -> None:
        """Update only window cache state, preserving raw topic ownership."""
        state = self.get_window_state(window_id)
        normalized = thread_id.strip()
        changed = False
        if state.codex_thread_id != normalized:
            state.codex_thread_id = normalized
            changed = True
        if not normalized and state.codex_active_turn_id:
            state.codex_active_turn_id = ""
            changed = True
        if changed:
            self._save_state()

    def get_window_codex_active_turn_id(self, window_id: str) -> str:
        """Get active Codex turn id for a window (empty if none)."""
        state = self.get_window_state(window_id)
        value = state.codex_active_turn_id.strip()
        return value

    def set_window_codex_active_turn_id(self, window_id: str, turn_id: str) -> None:
        """Persist active Codex turn id for a window."""
        state = self.get_window_state(window_id)
        normalized = turn_id.strip()
        if state.codex_active_turn_id == normalized:
            return
        state.codex_active_turn_id = normalized
        self._save_state()

    def clear_window_codex_turn(self, window_id: str) -> None:
        """Clear active Codex turn id for a window."""
        self.set_window_codex_active_turn_id(window_id, "")

    def get_window_codex_transport_generation(self, window_id: str) -> int:
        """Return the app-server generation associated with current remote state."""
        return max(
            0,
            int(self.get_window_state(window_id).codex_transport_generation),
        )

    def get_window_codex_transport_state(
        self,
        window_id: str,
    ) -> tuple[str, float, int]:
        """Return the app-server epoch, epoch start time, and generation."""
        state = self.get_window_state(window_id)
        return (
            state.codex_transport_epoch.strip(),
            max(0.0, float(state.codex_transport_epoch_started_at)),
            max(0, int(state.codex_transport_generation)),
        )

    @staticmethod
    def _normalize_codex_transport_snapshot(
        snapshot: dict[str, Any],
    ) -> tuple[str, float, int]:
        """Return a validated app-server process identity from a snapshot."""
        raw_epoch = snapshot.get("epoch")
        epoch = raw_epoch.strip() if isinstance(raw_epoch, str) else ""
        try:
            epoch_started_at = max(
                0.0,
                float(snapshot.get("epoch_started_at", 0.0) or 0.0),
            )
        except (TypeError, ValueError):
            epoch_started_at = 0.0
        try:
            generation = max(
                0,
                int(snapshot.get("generation", 0) or 0),
            )
        except (TypeError, ValueError):
            generation = 0
        if not epoch or epoch_started_at <= 0 or generation <= 0:
            return "", 0.0, 0
        return epoch, epoch_started_at, generation

    def set_window_codex_transport_state(
        self,
        window_id: str,
        *,
        epoch: str,
        epoch_started_at: float,
        generation: int,
    ) -> None:
        """Persist the complete remote app-server transport identity."""
        state = self.get_window_state(window_id)
        normalized_epoch = epoch.strip()
        normalized_started_at = max(0.0, float(epoch_started_at))
        normalized_generation = max(0, int(generation))
        if (
            state.codex_transport_epoch == normalized_epoch
            and state.codex_transport_epoch_started_at == normalized_started_at
            and state.codex_transport_generation == normalized_generation
        ):
            return
        state.codex_transport_epoch = normalized_epoch
        state.codex_transport_epoch_started_at = normalized_started_at
        state.codex_transport_generation = normalized_generation
        self._save_state()

    def set_window_codex_transport_generation(
        self,
        window_id: str,
        generation: int,
    ) -> None:
        """Persist the app-server generation associated with a remote window."""
        state = self.get_window_state(window_id)
        normalized = max(0, int(generation))
        if state.codex_transport_generation == normalized:
            return
        state.codex_transport_generation = normalized
        self._save_state()

    def clear_window_codex_turns(self, window_ids: set[str]) -> int:
        """Clear active Codex turn ids for an explicit set of windows."""
        cleared = 0
        for window_id in window_ids:
            state = self.window_states.get(window_id)
            if state is None or not state.codex_active_turn_id:
                continue
            state.codex_active_turn_id = ""
            cleared += 1
        if cleared:
            self._save_state()
        return cleared

    def clear_window_codex_turns_for_machine(self, machine_id: str) -> int:
        """Clear active Codex turn ids for windows bound to one machine."""
        window_ids = self.get_window_ids_for_machine(machine_id)
        return self.clear_window_codex_turns(window_ids)

    def get_window_ids_for_machine(self, machine_id: str) -> set[str]:
        """Return window ids bound to one machine, including local defaults."""
        target_machine_id = machine_id.strip()
        local_machine_id, _local_machine_name = self._local_machine_identity()
        if not target_machine_id:
            target_machine_id = local_machine_id

        window_ids: set[str] = set()
        for window_id in self.window_states:
            bound_machine_id = self.get_window_machine_id(window_id)
            if bound_machine_id:
                if bound_machine_id != target_machine_id:
                    continue
            elif target_machine_id != local_machine_id:
                continue
            window_ids.add(window_id)
        return window_ids

    def _build_session_file_path(self, session_id: str, cwd: str) -> Path | None:
        """Return direct transcript path when it can be derived cheaply.

        Codex session logs are date-sharded and filename-prefixed, so lookups
        normally fall back to a glob search.
        """
        _ = session_id, cwd
        return None

    async def _get_session_direct(
        self, session_id: str, cwd: str
    ) -> SessionTranscript | None:
        """Get a session directly from session_id and cwd (no full scan)."""
        file_path = self._session_file_path_cache.get(session_id)
        if file_path is not None and not file_path.exists():
            self._session_file_path_cache.pop(session_id, None)
            file_path = None

        if file_path is None:
            file_path = self._build_session_file_path(session_id, cwd)

        # Fallback: glob search if direct path doesn't exist
        if not file_path or not file_path.exists():
            matches = list(config.sessions_path.glob(f"**/*-{session_id}.jsonl"))
            if matches:
                file_path = matches[0]
                self._session_file_path_cache[session_id] = file_path
                logger.debug("Found session via glob: %s", file_path)
            else:
                self._session_file_path_cache.pop(session_id, None)
                return None
        else:
            self._session_file_path_cache[session_id] = file_path

        # Single pass: read file once, extract summary + count messages
        summary = ""
        last_user_msg = ""
        message_count = 0
        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                async for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        if not isinstance(data, dict):
                            continue
                        parsed = TranscriptParser.parse_message(data)
                        if parsed:
                            message_count += 1
                        # Check for summary
                        if data.get("type") == "summary":
                            s = data.get("summary", "")
                            if s:
                                summary = s
                        # Track last user message as fallback
                        elif TranscriptParser.is_user_message(data):
                            if parsed and parsed.text.strip():
                                last_user_msg = parsed.text.strip()
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return None

        if not summary:
            summary = last_user_msg[:50] if last_user_msg else "Untitled"

        return SessionTranscript(
            session_id=session_id,
            summary=summary,
            message_count=message_count,
            file_path=str(file_path),
        )

    # --- Window → Session resolution ---

    async def resolve_session_for_window(self, window_id: str) -> SessionTranscript | None:
        """Resolve a window to the best matching session.

        Uses persisted session_id + cwd to construct file path directly.
        Returns None if no session is associated with this window.
        """
        state = self.get_window_state(window_id)

        if not state.session_id or not state.cwd:
            await self.autodiscover_session_for_window(window_id)
            state = self.get_window_state(window_id)
            if not state.session_id or not state.cwd:
                return None

        session = await self._get_session_direct(state.session_id, state.cwd)
        if session:
            return session

        # File no longer exists, clear state
        logger.warning(
            "Session file no longer exists for window_id %s (sid=%s, cwd=%s)",
            window_id,
            state.session_id,
            state.cwd,
        )
        state.session_id = ""
        state.cwd = ""
        self._save_state()
        return None

    # --- User window offset management ---

    def update_user_window_offset(
        self, user_id: int, window_id: str, offset: int
    ) -> None:
        """Update the user's last read offset for a window."""
        if user_id not in self.user_window_offsets:
            self.user_window_offsets[user_id] = {}
        self.user_window_offsets[user_id][window_id] = offset
        self._save_state()

    # --- Thread binding management ---

    def get_thread_skills(
        self,
        user_id: int,
        thread_id: int | None,
        *,
        chat_id: int | None = None,
    ) -> list[str]:
        """Get enabled skill names for a topic thread."""
        if thread_id is None:
            return []
        per_user = self.thread_skills.get(user_id)
        if not per_user:
            return []
        slot_key = self._find_topic_slot_key(user_id, thread_id, chat_id=chat_id)
        if slot_key is None:
            return []
        names = per_user.get(slot_key, [])
        return [str(name) for name in names if isinstance(name, str) and name.strip()]

    def set_thread_skills(
        self,
        user_id: int,
        thread_id: int | None,
        skill_names: list[str],
        *,
        chat_id: int | None = None,
    ) -> None:
        """Set enabled skill names for one topic thread."""
        if thread_id is None:
            return
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in skill_names:
            name = str(raw).strip().lower()
            if not name or name in seen:
                continue
            seen.add(name)
            normalized.append(name)

        if not normalized:
            self.clear_thread_skills(user_id, thread_id, chat_id=chat_id)
            return

        if user_id not in self.thread_skills:
            self.thread_skills[user_id] = {}
        slot_key = self._find_topic_slot_key(user_id, thread_id, chat_id=chat_id)
        if slot_key is None:
            slot_key = self._topic_slot_key(thread_id=thread_id, chat_id=chat_id)
        existing = self.thread_skills[user_id].get(slot_key, [])
        if existing == normalized:
            return
        self.thread_skills[user_id][slot_key] = normalized
        self._save_state()

    def clear_thread_skills(
        self,
        user_id: int,
        thread_id: int | None,
        *,
        chat_id: int | None = None,
    ) -> None:
        """Clear enabled skills for one topic thread."""
        if thread_id is None:
            return
        per_user = self.thread_skills.get(user_id)
        slot_key = self._find_topic_slot_key(user_id, thread_id, chat_id=chat_id)
        if not per_user or slot_key is None or slot_key not in per_user:
            return
        del per_user[slot_key]
        if not per_user:
            del self.thread_skills[user_id]
        self._save_state()

    def get_thread_codex_skills(
        self,
        user_id: int,
        thread_id: int | None,
        *,
        chat_id: int | None = None,
    ) -> list[str]:
        """Get enabled Codex skill names for a topic thread."""
        if thread_id is None:
            return []
        per_user = self.thread_codex_skills.get(user_id)
        if not per_user:
            return []
        slot_key = self._find_topic_slot_key(user_id, thread_id, chat_id=chat_id)
        if slot_key is None:
            return []
        names = per_user.get(slot_key, [])
        return [str(name) for name in names if isinstance(name, str) and name.strip()]

    def set_thread_codex_skills(
        self,
        user_id: int,
        thread_id: int | None,
        skill_names: list[str],
        *,
        chat_id: int | None = None,
    ) -> None:
        """Set enabled Codex skill names for one topic thread."""
        if thread_id is None:
            return
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in skill_names:
            name = str(raw).strip().lower()
            if not name or name in seen:
                continue
            seen.add(name)
            normalized.append(name)

        if not normalized:
            self.clear_thread_codex_skills(user_id, thread_id, chat_id=chat_id)
            return

        if user_id not in self.thread_codex_skills:
            self.thread_codex_skills[user_id] = {}
        slot_key = self._find_topic_slot_key(user_id, thread_id, chat_id=chat_id)
        if slot_key is None:
            slot_key = self._topic_slot_key(thread_id=thread_id, chat_id=chat_id)
        existing = self.thread_codex_skills[user_id].get(slot_key, [])
        if existing == normalized:
            return
        self.thread_codex_skills[user_id][slot_key] = normalized
        self._save_state()

    def clear_thread_codex_skills(
        self,
        user_id: int,
        thread_id: int | None,
        *,
        chat_id: int | None = None,
    ) -> None:
        """Clear enabled Codex skills for one topic thread."""
        if thread_id is None:
            return
        per_user = self.thread_codex_skills.get(user_id)
        slot_key = self._find_topic_slot_key(user_id, thread_id, chat_id=chat_id)
        if not per_user or slot_key is None or slot_key not in per_user:
            return
        del per_user[slot_key]
        if not per_user:
            del self.thread_codex_skills[user_id]
        self._save_state()

    def discover_skill_catalog(self) -> dict[str, SkillDefinition]:
        """Discover available app-style skills from configured app roots."""
        return discover_skills(config.apps_paths)

    def discover_codex_skill_catalog(self) -> dict[str, SkillDefinition]:
        """Discover available Codex skills from configured Codex roots."""
        return discover_skills(config.codex_skills_paths)

    def resolve_thread_skills(
        self,
        user_id: int,
        thread_id: int | None,
        *,
        chat_id: int | None = None,
        catalog: dict[str, SkillDefinition] | None = None,
    ) -> list[SkillDefinition]:
        """Resolve enabled topic apps to active skill definitions."""
        if thread_id is None:
            return []
        current_names = self.get_thread_skills(user_id, thread_id, chat_id=chat_id)
        if not current_names:
            return []
        skill_catalog = catalog if catalog is not None else self.discover_skill_catalog()
        resolved: list[SkillDefinition] = []
        normalized_names: list[str] = []
        for raw_name in current_names:
            canonical = resolve_skill_identifier(raw_name, skill_catalog)
            if not canonical:
                continue
            skill = skill_catalog.get(canonical)
            if not skill:
                continue
            resolved.append(skill)
            normalized_names.append(skill.name)
        if normalized_names != current_names:
            self.set_thread_skills(user_id, thread_id, normalized_names, chat_id=chat_id)
        return resolved

    def resolve_thread_codex_skills(
        self,
        user_id: int,
        thread_id: int | None,
        *,
        chat_id: int | None = None,
        catalog: dict[str, SkillDefinition] | None = None,
    ) -> list[SkillDefinition]:
        """Resolve enabled topic Codex skills to active skill definitions."""
        if thread_id is None:
            return []
        current_names = self.get_thread_codex_skills(user_id, thread_id, chat_id=chat_id)
        if not current_names:
            return []
        skill_catalog = (
            catalog if catalog is not None else self.discover_codex_skill_catalog()
        )
        resolved: list[SkillDefinition] = []
        normalized_names: list[str] = []
        for raw_name in current_names:
            canonical = resolve_skill_identifier(raw_name, skill_catalog)
            if not canonical:
                continue
            skill = skill_catalog.get(canonical)
            if not skill:
                continue
            resolved.append(skill)
            normalized_names.append(skill.name)
        if normalized_names != current_names:
            self.set_thread_codex_skills(
                user_id, thread_id, normalized_names, chat_id=chat_id
            )
        return resolved

    @staticmethod
    def _inject_skill_context(
        text: str,
        *,
        apps: list[SkillDefinition],
        codex_skills: list[SkillDefinition],
    ) -> str:
        """Inject concise app/skill context before message delivery."""
        if not apps and not codex_skills:
            return text
        lines = [
            "[coco guidance]",
        ]
        if apps:
            lines.append("Enabled apps for this topic:")
            for app in apps:
                lines.append(f"- app `{app.name}`: {app.skill_md_path}")
        if codex_skills:
            lines.append("Enabled Codex skills for this topic:")
            for skill in codex_skills:
                lines.append(f"- skill `{skill.name}`: {skill.skill_md_path}")
        lines.append("Read each SKILL.md and apply relevant guidance for this request.")
        lines.append("")
        lines.append(text)
        return "\n".join(lines)

    def _build_coco_operator_context(
        self,
        *,
        user_id: int,
        thread_id: int | None,
        chat_id: int | None = None,
    ) -> str:
        """Build a concise operator brief for the singleton `/coco` control topic."""
        if not self.is_coco_control_topic(user_id, thread_id, chat_id=chat_id):
            return ""

        normalized_chat_id = int(chat_id or 0)
        inventory: list[str] = []
        for entry_user_id, entry_chat_id, entry_thread_id, binding in self.iter_topic_bindings():
            if entry_user_id != user_id:
                continue
            if int(entry_chat_id or 0) != normalized_chat_id:
                continue
            if thread_id is not None and entry_thread_id == thread_id:
                continue
            label = binding.display_name.strip() or f"thread-{entry_thread_id}"
            cwd = binding.cwd.strip() or "(no workspace)"
            machine_name = (
                binding.machine_display_name.strip()
                or binding.machine_id.strip()
                or "unknown"
            )
            sync_mode = self.get_topic_sync_mode(
                entry_user_id,
                entry_thread_id,
                chat_id=entry_chat_id,
            )
            response_mode = self.get_topic_response_mode(
                entry_user_id,
                entry_thread_id,
                chat_id=entry_chat_id,
            )
            turn_status = "idle"
            window_id = binding.window_id.strip()
            if window_id and self.get_window_codex_active_turn_id(window_id):
                turn_status = "active"
            inventory.append(
                f"- thread `{entry_thread_id}`: `{label}` — "
                f"machine `{machine_name}`, "
                f"sync `{sync_mode}`, "
                f"response `{response_mode}`, "
                f"turn `{turn_status}`, "
                f"workspace `{cwd}`"
            )

        lines = [
            "[coco operator]",
            "This topic is the singleton CoCo control topic.",
            "You can inspect, summarize, and steer other topics in this chat.",
            "Answer from the control-plane perspective by default.",
            "If the user clearly names another topic, explicit phrases like `tell <topic> to ...`, `ask <topic> to ...`, or `queue for <topic>: ...` are routed as cross-topic actions.",
            "Prefer safe, reversible orchestration and explain cross-topic actions before or while taking them.",
        ]
        if inventory:
            lines.append("Current topic inventory:")
            lines.extend(inventory)
        else:
            lines.append("Current topic inventory: no other bound topics yet.")
        recent_activity = self._build_coco_recent_activity_summary(
            user_id=user_id,
            chat_id=normalized_chat_id,
            current_thread_id=thread_id,
        )
        if recent_activity:
            lines.append("Recent visible activity:")
            lines.extend(recent_activity)
        return "\n".join(lines)

    @staticmethod
    def _message_requests_live_goal_context(text: str) -> bool:
        """Return whether one user message likely needs fresh native goal state."""
        if not isinstance(text, str):
            return False
        normalized = text.strip()
        if not normalized:
            return False
        return bool(GOAL_CONTEXT_TRIGGER_RE.search(normalized))

    @staticmethod
    def _extract_goal_status_and_text(payload: object) -> tuple[str, str]:
        """Normalize one native goal payload into (status, objective text)."""
        if not isinstance(payload, dict):
            return "", ""

        status = ""
        text = ""
        goal_block = payload.get("goal")
        if isinstance(goal_block, dict):
            for key in ("objective", "text", "goal"):
                value = goal_block.get(key)
                if isinstance(value, str) and value.strip():
                    text = value.strip()
                    break
            raw_status = goal_block.get("status")
            if isinstance(raw_status, str) and raw_status.strip():
                status = raw_status.strip().lower()
        elif isinstance(goal_block, str) and goal_block.strip():
            text = goal_block.strip()

        if not text:
            for key in ("objective", "text"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    text = value.strip()
                    break
        if not status:
            raw_status = payload.get("status")
            if isinstance(raw_status, str) and raw_status.strip():
                status = raw_status.strip().lower()
        return status, text

    @staticmethod
    def _goal_error_means_no_goal(message: str) -> bool:
        """Return whether one goal read failure clearly means no goal is set."""
        normalized = message.strip().lower()
        if not normalized:
            return False
        return "no goal" in normalized or "no persisted codex thread is bound yet" in normalized

    async def _build_live_goal_context(
        self,
        *,
        user_id: int,
        thread_id: int | None,
        chat_id: int | None = None,
        user_text: str,
    ) -> str:
        """Build one fresh goal-state note for goal-sensitive user messages."""
        if thread_id is None or not self._message_requests_live_goal_context(user_text):
            return ""

        lines = [
            "[coco goal context]",
            "Trust this live goal state over stale session memory.",
        ]
        ok, payload, message = await self.get_topic_goal(
            user_id=user_id,
            thread_id=thread_id,
            chat_id=chat_id,
        )
        if ok:
            status, goal_text = self._extract_goal_status_and_text(payload)
            if status:
                lines.append(f"Current native goal status: {status}.")
            if goal_text:
                lines.append(f"Current native goal objective: {goal_text}")
            if not status and not goal_text:
                lines.append("Live native goal state for this topic: no goal is currently set.")
            lines.append(
                "If the user wants to change the goal, re-check native goal tools from current state before deciding between create and update."
            )
            return "\n".join(lines)

        if self._goal_error_means_no_goal(message):
            lines.append("Live native goal state for this topic: no goal is currently set.")
            lines.append(
                "If the user wants to set a goal, do not claim an older completed goal is still attached unless the live tool confirms it."
            )
            return "\n".join(lines)

        lines.append(f"Live native goal refresh failed: {message}")
        lines.append(
            "Do not assume earlier goal state is still correct; verify with native goal tools before describing goal constraints."
        )
        return "\n".join(lines)

    @staticmethod
    def _telegram_memory_log_path() -> Path:
        raw = env_alias("COCO_TELEGRAM_MEMORY_LOG_PATH")
        if raw:
            return Path(raw).expanduser()
        return Path(__file__).resolve().parents[2] / "TELEGRAM_CHAT_MEMORY.jsonl"

    @staticmethod
    def _is_substantive_telegram_memory_text(text: str) -> bool:
        value = " ".join(text.strip().split())
        if not value:
            return False
        lowered = value.lower()
        if lowered.startswith("⏳ working"):
            return False
        if lowered == "✅ process complete":
            return False
        return True

    def _build_coco_recent_activity_summary(
        self,
        *,
        user_id: int,
        chat_id: int,
        current_thread_id: int | None,
    ) -> list[str]:
        path = self._telegram_memory_log_path()
        if not path.is_file():
            return []

        topic_labels: dict[int, str] = {}
        for entry_user_id, entry_chat_id, entry_thread_id, binding in self.iter_topic_bindings():
            if entry_user_id != user_id:
                continue
            if int(entry_chat_id or 0) != chat_id:
                continue
            if current_thread_id is not None and entry_thread_id == current_thread_id:
                continue
            topic_labels[entry_thread_id] = binding.display_name.strip() or f"thread-{entry_thread_id}"
        if not topic_labels:
            return []

        recent_by_thread: dict[int, list[str]] = {tid: [] for tid in topic_labels}
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []

        for raw_line in reversed(lines):
            try:
                data = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            try:
                entry_chat_id = int(data.get("chat_id", 0) or 0)
                raw_thread_id = int(data.get("thread_id", 0) or 0)
            except (TypeError, ValueError):
                continue
            if entry_chat_id != chat_id:
                continue
            if raw_thread_id not in recent_by_thread:
                continue
            direction = str(data.get("direction", "")).strip()
            if direction == "in":
                try:
                    from_user_id = int(data.get("from_user_id", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if from_user_id != user_id:
                    continue
                speaker = "User"
            elif direction in {"out_send", "out_edit"}:
                speaker = "CoCo"
            else:
                continue
            text = str(data.get("text", "")).strip()
            if not self._is_substantive_telegram_memory_text(text):
                continue
            if text.startswith("I’m ") and "Working" not in text and speaker == "CoCo":
                text = text
            bucket = recent_by_thread[raw_thread_id]
            if len(bucket) >= 2:
                continue
            bucket.append(f"{speaker}: {text}")

        summary_lines: list[str] = []
        for thread_key in sorted(topic_labels):
            entries = list(reversed(recent_by_thread.get(thread_key, [])))
            if not entries:
                continue
            summary_lines.append(f"{topic_labels[thread_key]}: {' | '.join(entries)}")
        return summary_lines

    def _inject_topic_context(
        self,
        text: str,
        *,
        user_id: int,
        thread_id: int | None,
        chat_id: int | None = None,
        apps: list[SkillDefinition],
        codex_skills: list[SkillDefinition],
    ) -> str:
        """Inject all topic-scoped guidance before message delivery."""
        enriched = self._inject_skill_context(
            text,
            apps=apps,
            codex_skills=codex_skills,
        )
        operator_context = self._build_coco_operator_context(
            user_id=user_id,
            thread_id=thread_id,
            chat_id=chat_id,
        ).strip()
        if not operator_context:
            return enriched
        return f"{operator_context}\n\n{enriched}"

    async def send_topic_text_to_window(
        self,
        *,
        user_id: int,
        thread_id: int | None,
        chat_id: int | None = None,
        window_id: str,
        text: str,
        steer: bool = False,
        force_new_turn: bool = False,
        dispatch_state: TopicSendDispatchState | None = None,
        topic_ownership: TopicOwnership | None = None,
    ) -> tuple[bool, str]:
        """Send user/topic text with app/skill context applied."""
        if topic_ownership is not None and (
            thread_id is None
            or not self._topic_binding_ownership_matches(
                user_id,
                thread_id,
                chat_id=chat_id,
                window_id=topic_ownership.window_id,
                codex_thread_id=topic_ownership.codex_thread_id,
                machine_id=topic_ownership.machine_id,
                cwd=topic_ownership.cwd,
            )
        ):
            return (
                False,
                "The topic's canonical Codex binding changed after this request "
                "was accepted. The request was not sent; retry it explicitly.",
            )
        persisted_topic_binding = (
            self._get_persisted_topic_binding(
                user_id,
                thread_id,
                chat_id=chat_id,
            )
            if thread_id is not None
            else None
        )
        canonical_topic_thread_id = (
            persisted_topic_binding.codex_thread_id.strip()
            if persisted_topic_binding is not None
            else ""
        )
        canonical_topic_cwd = (
            persisted_topic_binding.cwd.strip()
            if persisted_topic_binding is not None
            else ""
        )
        if thread_id is not None and persisted_topic_binding is None:
            return (
                False,
                "No persisted topic binding exists for this topic. The request "
                "was not sent; run /start or use /resume explicitly.",
            )
        cached_window_thread_id = self.get_window_codex_thread_id(window_id)
        if (
            persisted_topic_binding is not None
            and not canonical_topic_thread_id
            and cached_window_thread_id
        ):
            emit_telemetry(
                "session.implicit_rebind_blocked",
                window_id=window_id,
                bound_thread_id="",
                rejected_thread_id=cached_window_thread_id,
                source="missing_topic_authority",
            )
            return (
                False,
                "No canonical Codex thread is persisted for this topic, but "
                "the window cache contains a thread ID. The request was not "
                "sent; use /resume to select the intended thread explicitly.",
            )
        if (
            canonical_topic_thread_id
            and cached_window_thread_id
            and cached_window_thread_id != canonical_topic_thread_id
        ):
            emit_telemetry(
                "session.implicit_rebind_blocked",
                window_id=window_id,
                bound_thread_id=canonical_topic_thread_id,
                rejected_thread_id=cached_window_thread_id,
                source="window_cache_disagreement",
            )
            return (
                False,
                "The topic binding and window cache disagree about the Codex "
                "thread. The request was not sent; use /resume to select the "
                "intended thread explicitly.",
            )
        if canonical_topic_thread_id and not cached_window_thread_id:
            # The persisted topic binding is authoritative. Repair only the
            # empty window cache; never overwrite a conflicting nonempty ID.
            self._set_window_codex_thread_cache(
                window_id,
                canonical_topic_thread_id,
            )
        machine_id = (
            persisted_topic_binding.machine_id.strip()
            if persisted_topic_binding is not None
            else self.get_window_machine_id(window_id)
        )

        def _topic_ownership_is_current() -> bool:
            if thread_id is None:
                return True
            return self._topic_binding_ownership_matches(
                user_id,
                thread_id,
                chat_id=chat_id,
                window_id=window_id,
                codex_thread_id=canonical_topic_thread_id,
                machine_id=machine_id,
                cwd=canonical_topic_cwd,
            )

        def _commit_fresh_local_topic_thread() -> tuple[bool, str]:
            """Commit a first local thread only after its turn was accepted."""
            if thread_id is None or canonical_topic_thread_id:
                return True, ""
            if not _topic_ownership_is_current():
                return (
                    False,
                    "The topic's canonical Codex binding changed before its "
                    "new thread could be committed. The request was not sent.",
                )
            new_thread_id = self.get_window_codex_thread_id(window_id).strip()
            if not new_thread_id:
                return False, "Codex did not return a thread for this topic."
            self.bind_topic_to_codex_thread(
                user_id=user_id,
                thread_id=thread_id,
                chat_id=chat_id,
                codex_thread_id=new_thread_id,
                cwd=canonical_topic_cwd,
                display_name=(
                    persisted_topic_binding.display_name.strip()
                    if persisted_topic_binding is not None
                    else self.get_display_name(window_id)
                ),
                window_id=window_id,
                machine_id=machine_id,
                machine_display_name=(
                    persisted_topic_binding.machine_display_name.strip()
                    if persisted_topic_binding is not None
                    else ""
                ),
            )
            return True, ""

        local_machine_id, _local_machine_name = self._local_machine_identity()
        operator_context = self._build_coco_operator_context(
            user_id=user_id,
            thread_id=thread_id,
            chat_id=chat_id,
        ).strip()
        model_slug, reasoning_effort = self.get_topic_model_selection(
            user_id,
            thread_id,
            chat_id=chat_id,
        )
        service_tier = self.get_topic_service_tier_selection(
            user_id,
            thread_id,
            chat_id=chat_id,
        )
        topic_send_kwargs: dict[str, str] = {}
        if model_slug:
            topic_send_kwargs["model_slug"] = model_slug
        if reasoning_effort:
            topic_send_kwargs["reasoning_effort"] = reasoning_effort
        if service_tier:
            topic_send_kwargs["service_tier"] = service_tier
        if (
            thread_id is not None
            and self._codex_app_server_mode_enabled()
            and self.get_topic_sync_mode(user_id, thread_id, chat_id=chat_id)
            == TOPIC_SYNC_MODE_HOST_FOLLOW_FINAL
        ):
            state = self.get_window_state(window_id)
            binding = persisted_topic_binding
            binding_cwd = binding.cwd.strip() if binding is not None else ""
            cwd = binding_cwd
            oversized_rollover = False
            if not cwd:
                return False, "No workspace bound to this topic. Run /folder first."
            bound_thread_id = (
                binding.codex_thread_id.strip()
                if binding is not None
                else state.codex_thread_id.strip()
            )
            if not bound_thread_id:
                return (
                    False,
                    "No canonical Codex thread is bound to this topic. "
                    "Use /resume to choose one explicitly.",
                )
            if machine_id and machine_id != local_machine_id:
                from .agent_rpc import (
                    RemoteCodexMutationDeferredError,
                    agent_rpc_client,
                )

                try:
                    resume_result = await agent_rpc_client.resume_thread(
                        machine_id,
                        window_id=window_id,
                        cwd=cwd,
                        thread_id=bound_thread_id,
                        window_name=state.window_name
                        or self.get_display_name(window_id),
                        approval_mode=state.approval_mode.strip(),
                    )
                except RemoteCodexMutationDeferredError as exc:
                    return False, str(exc)
                except Exception as exc:
                    if self.is_codex_resume_limit_error(exc):
                        return (
                            False,
                            "This topic's bound Codex transcript is too large "
                            "to resume automatically. Use an explicit /resume "
                            "or /new selection; the topic binding was preserved.",
                        )
                    if not self.is_codex_active_writer_error(exc):
                        raise
                    error_text = str(exc)
                    logger.warning(
                        "Host-follow remote resume found an active writer for %s (%s): %s",
                        window_id,
                        self.get_display_name(window_id),
                        error_text,
                    )
                    emit_telemetry(
                        "transport.app_server.host_follow_resume_failed",
                        runtime_mode=config.runtime_mode,
                        codex_transport=config.codex_transport,
                        window_id=window_id,
                        display=self.get_display_name(window_id),
                        active_writer=True,
                        error=error_text,
                    )
                    return (
                        False,
                        "This topic's Codex run already has an active writer. "
                        "The request was not sent.",
                    )
                resumed_thread_id = str(resume_result.get("thread_id", "")).strip()
                oversized_rollover = (
                    str(resume_result.get("session_start_reason", "")).strip()
                    == "oversized_rollover"
                )
                if not self._topic_binding_ownership_matches(
                    user_id,
                    thread_id,
                    chat_id=chat_id,
                    window_id=window_id,
                    codex_thread_id=bound_thread_id,
                    machine_id=machine_id,
                    cwd=binding_cwd,
                ):
                    return (
                        False,
                        "The topic's canonical Codex binding changed while its "
                        "remote thread was resuming. The request was not sent.",
                    )
                if resumed_thread_id:
                    if resumed_thread_id != bound_thread_id:
                        emit_telemetry(
                            "session.implicit_rebind_blocked",
                            window_id=window_id,
                            bound_thread_id=bound_thread_id,
                            rejected_thread_id=resumed_thread_id,
                            source="host_follow_remote",
                        )
                        return (
                            False,
                            "Refused to replace this topic's bound Codex thread "
                            "without an explicit /resume selection.",
                        )
                    self._set_window_codex_thread_cache(
                        window_id,
                        resumed_thread_id,
                    )
                    self.set_window_codex_active_turn_id(
                        window_id,
                        str(resume_result.get("turn_id", "")).strip(),
                    )
                    self.inherit_window_topic_model_selection(
                        window_id=window_id,
                        model_slug=str(resume_result.get("model_slug", "")).strip(),
                        reasoning_effort=str(
                            resume_result.get("reasoning_effort", "")
                        ).strip(),
                    )
                    model_slug, reasoning_effort = self.get_topic_model_selection(
                        user_id,
                        thread_id,
                        chat_id=chat_id,
                    )
                    topic_send_kwargs = {
                        key: value
                        for key, value in {
                            "model_slug": model_slug,
                            "reasoning_effort": reasoning_effort,
                            "service_tier": service_tier,
                        }.items()
                        if value
                    }
            else:
                try:
                    resumed_thread_id = (
                        await self.resume_codex_session_for_window(
                            window_id=window_id,
                            cwd=cwd,
                            thread_id=bound_thread_id,
                        )
                    )
                except CodexAppServerError as exc:
                    error_text = str(exc)
                    if self.is_codex_resume_limit_error(exc):
                        return (
                            False,
                            "This topic's bound Codex transcript is too large "
                            "to resume automatically. Use an explicit /resume "
                            "or /new selection; the topic binding was preserved.",
                        )
                    active_writer = self.is_codex_active_writer_error(exc)
                    if not active_writer:
                        raise
                    logger.warning(
                        "Host-follow resume found an active writer for %s (%s): %s",
                        window_id,
                        self.get_display_name(window_id),
                        error_text,
                    )
                    emit_telemetry(
                        "transport.app_server.host_follow_resume_failed",
                        runtime_mode=config.runtime_mode,
                        codex_transport=config.codex_transport,
                        window_id=window_id,
                        display=self.get_display_name(window_id),
                        active_writer=active_writer,
                        error=error_text,
                    )
                    return (
                        False,
                        "This topic's Codex run already has an active writer. "
                        "The request was not sent.",
                    )
                if resumed_thread_id and resumed_thread_id != bound_thread_id:
                    emit_telemetry(
                        "session.implicit_rebind_blocked",
                        window_id=window_id,
                        bound_thread_id=bound_thread_id,
                        rejected_thread_id=resumed_thread_id,
                        source="host_follow_local",
                    )
                    return (
                        False,
                        "Refused to replace this topic's bound Codex thread "
                        "without an explicit /resume selection.",
                    )
                if not _topic_ownership_is_current():
                    return (
                        False,
                        "The topic's canonical Codex binding changed while its "
                        "local thread was resuming. The request was not sent.",
                    )
                oversized_rollover = (
                    self.peek_window_pending_session_start_reason(window_id)
                    == "oversized_rollover"
                )
            if oversized_rollover:
                emit_telemetry(
                    "session.implicit_rebind_blocked",
                    window_id=window_id,
                    bound_thread_id=bound_thread_id,
                    rejected_thread_id="",
                    source="host_follow_oversized_rollover",
                )
                return (
                    False,
                    "This topic's bound Codex transcript is too large to resume "
                    "automatically. Use an explicit /resume or /new selection; "
                    "the topic binding was preserved.",
                )
            if not resumed_thread_id:
                return False, "Failed to resume the Codex session bound to this topic."

        goal_context = await self._build_live_goal_context(
            user_id=user_id,
            thread_id=thread_id,
            chat_id=chat_id,
            user_text=text,
        )

        apps = self.resolve_thread_skills(user_id, thread_id, chat_id=chat_id)
        codex_skills = self.resolve_thread_codex_skills(
            user_id, thread_id, chat_id=chat_id
        )
        if not apps and not codex_skills and not operator_context and not goal_context:
            if machine_id and machine_id != local_machine_id:
                from .agent_rpc import (
                    RemoteCodexMutationDeferredError,
                    agent_rpc_client,
                )

                state = self.get_window_state(window_id)
                cwd = canonical_topic_cwd
                if not cwd:
                    return False, "No workspace bound to this topic. Run /folder first."
                node = node_registry.get_node(machine_id)
                if node is not None and node.status == "offline":
                    return False, f"Machine offline: {node.display_name}"
                dispatched_thread_id = (
                    canonical_topic_thread_id
                    or state.codex_thread_id.strip()
                )
                if thread_id is not None and not self._topic_binding_ownership_matches(
                    user_id,
                    thread_id,
                    chat_id=chat_id,
                    window_id=window_id,
                    codex_thread_id=canonical_topic_thread_id,
                    machine_id=machine_id,
                    cwd=canonical_topic_cwd,
                ):
                    return (
                        False,
                        "The topic's canonical Codex binding changed before "
                        "remote dispatch. The request was not sent.",
                    )
                try:
                    remote_result = await agent_rpc_client.send_inputs(
                        machine_id,
                        window_id=window_id,
                        cwd=cwd,
                        window_name=state.window_name
                        or self.get_display_name(window_id),
                        inputs=[{"type": "text", "text": text}],
                        steer=steer,
                        force_new_turn=force_new_turn,
                        thread_id=dispatched_thread_id,
                        approval_mode=state.approval_mode.strip(),
                        model_slug=model_slug,
                        reasoning_effort=reasoning_effort,
                        service_tier=service_tier,
                        **(
                            {
                                "on_dispatch": dispatch_state.mark_transport_dispatch_started
                            }
                            if dispatch_state is not None
                            else {}
                        ),
                    )
                except RemoteCodexMutationDeferredError as exc:
                    return False, str(exc)
                except Exception:
                    self._note_transport_uncertainty(
                        window_ids={window_id},
                        reason="remote_send_rpc_failed",
                    )
                    self.clear_window_codex_turn(window_id)
                    raise
                if not _topic_ownership_is_current():
                    return (
                        False,
                        "The topic's canonical Codex binding changed while the "
                        "request was in flight; the remote outcome is uncertain "
                        "and the request will not be replayed automatically.",
                    )
                if not await self._accept_remote_transport_result(
                    window_id=window_id,
                    result=remote_result,
                ):
                    return (
                        False,
                        "Remote Codex transport changed before acknowledgement; "
                        "the request will not be replayed automatically.",
                    )
                if not _topic_ownership_is_current():
                    return (
                        False,
                        "The topic's canonical Codex binding changed while the "
                        "request was in flight; the remote outcome is uncertain "
                        "and the request will not be replayed automatically.",
                    )
                resolved_thread_id = str(remote_result.get("thread_id", "")).strip()
                resolved_turn_id = str(remote_result.get("turn_id", "")).strip()
                ok = bool(remote_result.get("ok", False))
                if ok and (
                    not resolved_thread_id
                    or (
                        dispatched_thread_id
                        and resolved_thread_id != dispatched_thread_id
                    )
                ):
                    emit_telemetry(
                        "session.implicit_rebind_blocked",
                        window_id=window_id,
                        bound_thread_id=dispatched_thread_id,
                        rejected_thread_id=resolved_thread_id or "<missing>",
                        source="remote_send_response",
                    )
                    return (
                        False,
                        "Remote Codex did not acknowledge the request on the "
                        "exact expected thread; the outcome is uncertain and "
                        "the request will not be replayed automatically. The "
                        "topic binding was preserved.",
                    )
                current_persisted_binding = self._get_persisted_topic_binding(
                    user_id,
                    thread_id,
                    chat_id=chat_id,
                )
                current_canonical_thread_id = (
                    current_persisted_binding.codex_thread_id.strip()
                    if current_persisted_binding is not None
                    else ""
                )
                if ok and (
                    current_persisted_binding is None
                    or current_canonical_thread_id != canonical_topic_thread_id
                    or self.get_window_codex_thread_id(window_id)
                    != dispatched_thread_id
                ):
                    return (
                        False,
                        "The topic's canonical Codex thread changed while the "
                        "request was in flight; the response was not applied "
                        "and the request will not be replayed automatically.",
                    )
                if ok and resolved_thread_id and not canonical_topic_thread_id:
                    self.bind_topic_to_codex_thread(
                        user_id=user_id,
                        thread_id=thread_id,
                        chat_id=chat_id,
                        codex_thread_id=resolved_thread_id,
                        cwd=cwd,
                        display_name=state.window_name or self.get_display_name(window_id),
                        window_id=window_id,
                        machine_id=machine_id,
                    )
                if ok and (
                    resolved_turn_id
                    or self.get_window_codex_active_turn_id(window_id)
                ):
                    self.set_window_codex_active_turn_id(window_id, resolved_turn_id)
                msg = str(remote_result.get("message", "")).strip() or "Remote send complete."
            else:
                ok, msg = await self.send_to_window(
                    window_id,
                    text,
                    steer=steer,
                    force_new_turn=force_new_turn,
                    dispatch_cwd=canonical_topic_cwd,
                    ownership_validator=_topic_ownership_is_current,
                    dispatch_state=dispatch_state,
                    **topic_send_kwargs,
                )
                if ok:
                    committed, commit_error = _commit_fresh_local_topic_thread()
                    if not committed:
                        return False, commit_error
            if ok:
                self.mark_topic_telegram_live(
                    user_id=user_id,
                    thread_id=thread_id,
                    window_id=window_id,
                    chat_id=chat_id,
                )
            return ok, msg

        if self._codex_app_server_mode_enabled():
            inputs: list[dict[str, Any]] = [
                {
                    "type": "skill",
                    "name": skill.name,
                    "path": str(skill.folder_path),
                }
                for skill in codex_skills
            ]
            if operator_context:
                inputs.insert(0, {"type": "text", "text": operator_context})
            if apps:
                app_context = self._inject_skill_context(
                    "",
                    apps=apps,
                    codex_skills=[],
                ).strip()
                inputs.insert(0, {"type": "text", "text": app_context})
            if goal_context:
                inputs.append({"type": "text", "text": goal_context})
            inputs.append({"type": "text", "text": text})
            if machine_id and machine_id != local_machine_id:
                from .agent_rpc import (
                    RemoteCodexMutationDeferredError,
                    agent_rpc_client,
                )

                state = self.get_window_state(window_id)
                cwd = canonical_topic_cwd
                if not cwd:
                    return False, "No workspace bound to this topic. Run /folder first."
                node = node_registry.get_node(machine_id)
                if node is not None and node.status == "offline":
                    return False, f"Machine offline: {node.display_name}"
                dispatched_thread_id = (
                    canonical_topic_thread_id
                    or state.codex_thread_id.strip()
                )
                if thread_id is not None and not self._topic_binding_ownership_matches(
                    user_id,
                    thread_id,
                    chat_id=chat_id,
                    window_id=window_id,
                    codex_thread_id=canonical_topic_thread_id,
                    machine_id=machine_id,
                    cwd=canonical_topic_cwd,
                ):
                    return (
                        False,
                        "The topic's canonical Codex binding changed before "
                        "remote dispatch. The request was not sent.",
                    )
                try:
                    remote_result = await agent_rpc_client.send_inputs(
                        machine_id,
                        window_id=window_id,
                        cwd=cwd,
                        window_name=state.window_name
                        or self.get_display_name(window_id),
                        inputs=inputs,
                        steer=steer,
                        force_new_turn=force_new_turn,
                        thread_id=dispatched_thread_id,
                        approval_mode=state.approval_mode.strip(),
                        model_slug=model_slug,
                        reasoning_effort=reasoning_effort,
                        service_tier=service_tier,
                        **(
                            {
                                "on_dispatch": dispatch_state.mark_transport_dispatch_started
                            }
                            if dispatch_state is not None
                            else {}
                        ),
                    )
                except RemoteCodexMutationDeferredError as exc:
                    return False, str(exc)
                except Exception:
                    self._note_transport_uncertainty(
                        window_ids={window_id},
                        reason="remote_send_rpc_failed",
                    )
                    self.clear_window_codex_turn(window_id)
                    raise
                if not _topic_ownership_is_current():
                    return (
                        False,
                        "The topic's canonical Codex binding changed while the "
                        "request was in flight; the remote outcome is uncertain "
                        "and the request will not be replayed automatically.",
                    )
                if not await self._accept_remote_transport_result(
                    window_id=window_id,
                    result=remote_result,
                ):
                    return (
                        False,
                        "Remote Codex transport changed before acknowledgement; "
                        "the request will not be replayed automatically.",
                    )
                if not _topic_ownership_is_current():
                    return (
                        False,
                        "The topic's canonical Codex binding changed while the "
                        "request was in flight; the remote outcome is uncertain "
                        "and the request will not be replayed automatically.",
                    )
                resolved_thread_id = str(remote_result.get("thread_id", "")).strip()
                resolved_turn_id = str(remote_result.get("turn_id", "")).strip()
                ok = bool(remote_result.get("ok", False))
                if ok and (
                    not resolved_thread_id
                    or (
                        dispatched_thread_id
                        and resolved_thread_id != dispatched_thread_id
                    )
                ):
                    emit_telemetry(
                        "session.implicit_rebind_blocked",
                        window_id=window_id,
                        bound_thread_id=dispatched_thread_id,
                        rejected_thread_id=resolved_thread_id or "<missing>",
                        source="remote_send_response_with_context",
                    )
                    return (
                        False,
                        "Remote Codex did not acknowledge the request on the "
                        "exact expected thread; the outcome is uncertain and "
                        "the request will not be replayed automatically. The "
                        "topic binding was preserved.",
                    )
                current_persisted_binding = self._get_persisted_topic_binding(
                    user_id,
                    thread_id,
                    chat_id=chat_id,
                )
                current_canonical_thread_id = (
                    current_persisted_binding.codex_thread_id.strip()
                    if current_persisted_binding is not None
                    else ""
                )
                if ok and (
                    current_persisted_binding is None
                    or current_canonical_thread_id != canonical_topic_thread_id
                    or self.get_window_codex_thread_id(window_id)
                    != dispatched_thread_id
                ):
                    return (
                        False,
                        "The topic's canonical Codex thread changed while the "
                        "request was in flight; the response was not applied "
                        "and the request will not be replayed automatically.",
                    )
                if ok and resolved_thread_id and not canonical_topic_thread_id:
                    self.bind_topic_to_codex_thread(
                        user_id=user_id,
                        thread_id=thread_id,
                        chat_id=chat_id,
                        codex_thread_id=resolved_thread_id,
                        cwd=cwd,
                        display_name=state.window_name or self.get_display_name(window_id),
                        window_id=window_id,
                        machine_id=machine_id,
                    )
                if ok and (
                    resolved_turn_id
                    or self.get_window_codex_active_turn_id(window_id)
                ):
                    self.set_window_codex_active_turn_id(window_id, resolved_turn_id)
                msg = str(remote_result.get("message", "")).strip() or "Remote send complete."
            else:
                ok, msg = await self.send_inputs_to_window(
                    window_id,
                    inputs,
                    steer=steer,
                    force_new_turn=force_new_turn,
                    dispatch_cwd=canonical_topic_cwd,
                    ownership_validator=_topic_ownership_is_current,
                    dispatch_state=dispatch_state,
                    **topic_send_kwargs,
                )
                if ok:
                    committed, commit_error = _commit_fresh_local_topic_thread()
                    if not committed:
                        return False, commit_error
            if ok:
                self.mark_topic_telegram_live(
                    user_id=user_id,
                    thread_id=thread_id,
                    window_id=window_id,
                    chat_id=chat_id,
                )
            return ok, msg

        injected_text = text
        if goal_context:
            injected_text = f"{goal_context}\n\n{injected_text}"
        injected = self._inject_topic_context(
            injected_text,
            user_id=user_id,
            thread_id=thread_id,
            chat_id=chat_id,
            apps=apps,
            codex_skills=codex_skills,
        )
        ok, msg = await self.send_to_window(
            window_id,
            injected,
            steer=steer,
            force_new_turn=force_new_turn,
            dispatch_cwd=canonical_topic_cwd,
            ownership_validator=_topic_ownership_is_current,
            dispatch_state=dispatch_state,
        )
        if ok:
            committed, commit_error = _commit_fresh_local_topic_thread()
            if not committed:
                return False, commit_error
        if ok:
            self.mark_topic_telegram_live(
                user_id=user_id,
                thread_id=thread_id,
                window_id=window_id,
                chat_id=chat_id,
            )
        return ok, msg

    def _set_topic_binding(
        self,
        *,
        user_id: int,
        thread_id: int,
        chat_id: int | None,
        binding: TopicBinding,
    ) -> None:
        if not binding.machine_id:
            binding.machine_id, binding.machine_display_name = self._local_machine_identity()
        elif not binding.machine_display_name:
            node = node_registry.get_node(binding.machine_id)
            if node is not None and node.display_name:
                binding.machine_display_name = node.display_name
        if user_id not in self.topic_bindings_v2:
            self.topic_bindings_v2[user_id] = {}
        slot_key = self._topic_slot_key(thread_id=thread_id, chat_id=chat_id)
        self.topic_bindings_v2[user_id][slot_key] = binding

        window_id = binding.window_id.strip()
        if window_id and binding.display_name:
            self.window_display_names[window_id] = binding.display_name

    def ensure_topic_binding(
        self,
        user_id: int,
        thread_id: int | None,
        *,
        chat_id: int | None = None,
    ) -> TopicBinding | None:
        """Ensure a topic has a persisted binding record, even before folder bind."""
        if thread_id is None:
            return None
        slot_key = self._find_topic_slot_key(user_id, thread_id, chat_id=chat_id)
        if slot_key is not None:
            existing = self.topic_bindings_v2.get(user_id, {}).get(slot_key)
            if existing is not None:
                return existing
        machine_id, machine_display_name = self._local_machine_identity()
        binding = TopicBinding(
            transport=TOPIC_BINDING_TRANSPORT_WINDOW,
            chat_id=chat_id or 0,
            thread_id=thread_id,
            machine_id=machine_id,
            machine_display_name=machine_display_name,
        )
        self._set_topic_binding(
            user_id=user_id,
            thread_id=thread_id,
            chat_id=chat_id,
            binding=binding,
        )
        self._save_state()
        slot_key = self._find_topic_slot_key(user_id, thread_id, chat_id=chat_id)
        if slot_key is None:
            return None
        return self.topic_bindings_v2.get(user_id, {}).get(slot_key)

    def get_coco_control_topic(self) -> CocoControlTopic | None:
        """Return the singleton `/coco` control-topic assignment."""
        return self.coco_control_topic

    def is_coco_control_topic(
        self,
        user_id: int,
        thread_id: int | None,
        *,
        chat_id: int | None = None,
    ) -> bool:
        """Return whether one topic is the active `/coco` control topic."""
        if thread_id is None or self.coco_control_topic is None:
            return False
        if self.coco_control_topic.user_id != user_id:
            return False
        if self.coco_control_topic.thread_id != thread_id:
            return False
        normalized_chat_id = int(chat_id or 0)
        return self.coco_control_topic.chat_id == normalized_chat_id

    def set_coco_control_topic(
        self,
        user_id: int,
        thread_id: int | None,
        *,
        chat_id: int | None = None,
    ) -> TopicBinding | None:
        """Persist General as the singleton `/coco` control topic."""
        if thread_id != GENERAL_TOPIC_THREAD_ID or not int(chat_id or 0):
            return None
        binding = self.ensure_topic_binding(user_id, thread_id, chat_id=chat_id)
        if binding is None:
            return None
        self.coco_control_topic = CocoControlTopic(
            user_id=user_id,
            thread_id=thread_id,
            chat_id=int(chat_id or 0),
        )
        self._save_state()
        return binding

    def migrate_coco_control_to_general(
        self,
        *,
        workspace_dir: str,
        general_thread_id: int = GENERAL_TOPIC_THREAD_ID,
    ) -> CocoControlMigration | None:
        """Move the existing control binding and its history to General once."""
        current = self.coco_control_topic
        raw_workspace = workspace_dir.strip()
        if (
            current is None
            or current.user_id <= 0
            or not current.chat_id
            or general_thread_id <= 0
            or not raw_workspace
            or current.thread_id == general_thread_id
        ):
            return None
        workspace = os.path.abspath(os.path.expanduser(raw_workspace))

        user_id = current.user_id
        chat_id = current.chat_id
        old_thread_id = current.thread_id
        old_slot = self._find_topic_slot_key(
            user_id,
            old_thread_id,
            chat_id=chat_id,
        )
        new_slot = self._topic_slot_key(
            thread_id=general_thread_id,
            chat_id=chat_id,
        )
        per_user = self.topic_bindings_v2.setdefault(user_id, {})
        source = per_user.get(old_slot) if old_slot is not None else None
        if source is None:
            source = per_user.get(new_slot)
        if source is None:
            machine_id, machine_display_name = self._local_machine_identity()
            source = TopicBinding(
                window_id=self.allocate_virtual_window_id(),
                machine_id=machine_id,
                machine_display_name=machine_display_name,
            )

        binding = TopicBinding(
            transport=source.transport,
            chat_id=chat_id,
            thread_id=general_thread_id,
            window_id=source.window_id or self.allocate_virtual_window_id(),
            codex_thread_id=source.codex_thread_id,
            cwd=workspace,
            display_name="coco-control",
            sync_mode=source.sync_mode,
            machine_id=source.machine_id,
            machine_display_name=source.machine_display_name,
            model_slug=source.model_slug,
            reasoning_effort=source.reasoning_effort,
            model_selection_explicit=source.model_selection_explicit,
            service_tier=source.service_tier,
            response_mode=source.response_mode,
        )
        moved_history = bool(binding.codex_thread_id.strip())
        state = self.get_window_state(binding.window_id)
        if not binding.codex_thread_id and state.codex_thread_id.strip():
            binding.codex_thread_id = state.codex_thread_id.strip()
            moved_history = True
        state.cwd = workspace
        state.window_name = "coco-control"
        state.codex_thread_id = binding.codex_thread_id

        if old_slot is not None and old_slot != new_slot:
            per_user.pop(old_slot, None)
        self._set_topic_binding(
            user_id=user_id,
            thread_id=general_thread_id,
            chat_id=chat_id,
            binding=binding,
        )

        for settings in (self.thread_skills, self.thread_codex_skills):
            per_user_settings = settings.get(user_id)
            if not per_user_settings or old_slot is None:
                continue
            if old_slot in per_user_settings:
                per_user_settings[new_slot] = per_user_settings.pop(old_slot)

        self.group_chat_ids[f"{user_id}:{new_slot}"] = chat_id
        self.group_chat_ids[f"{user_id}:{general_thread_id}"] = chat_id
        self.coco_control_topic = CocoControlTopic(
            user_id=user_id,
            thread_id=general_thread_id,
            chat_id=chat_id,
        )
        self._save_state()
        return CocoControlMigration(
            user_id=user_id,
            chat_id=chat_id,
            previous_thread_id=old_thread_id,
            general_thread_id=general_thread_id,
            moved_history=moved_history,
        )

    def get_topic_model_selection(
        self,
        user_id: int,
        thread_id: int | None,
        *,
        chat_id: int | None = None,
    ) -> tuple[str, str]:
        """Return the persisted per-topic model selection."""
        binding = self.resolve_topic_binding(user_id, thread_id, chat_id=chat_id)
        if binding is None:
            return "", ""
        raw_model = getattr(binding, "model_slug", "")
        raw_effort = getattr(binding, "reasoning_effort", "")
        model_slug = raw_model.strip() if isinstance(raw_model, str) else ""
        reasoning_effort = raw_effort.strip() if isinstance(raw_effort, str) else ""
        return model_slug, reasoning_effort

    def get_topic_service_tier_selection(
        self,
        user_id: int,
        thread_id: int | None,
        *,
        chat_id: int | None = None,
    ) -> str:
        """Return the persisted per-topic service tier selection."""
        binding = self.resolve_topic_binding(user_id, thread_id, chat_id=chat_id)
        if binding is None:
            return ""
        raw_service_tier = getattr(binding, "service_tier", "")
        if not isinstance(raw_service_tier, str):
            return ""
        normalized = raw_service_tier.strip().lower()
        return normalized if normalized in CODEX_SERVICE_TIERS else ""

    def get_window_topic_model_selection(self, window_id: str) -> tuple[str, str]:
        """Return one persisted model selection for a window-bound topic."""
        normalized_window_id = window_id.strip()
        if not normalized_window_id:
            return "", ""
        for _user_id, _chat_id, _thread_id, binding in self.iter_topic_bindings():
            if binding.window_id.strip() != normalized_window_id:
                continue
            return binding.model_slug.strip(), binding.reasoning_effort.strip()
        return "", ""

    def get_window_topic_service_tier_selection(self, window_id: str) -> str:
        """Return one persisted service tier selection for a window-bound topic."""
        normalized_window_id = window_id.strip()
        if not normalized_window_id:
            return ""
        for _user_id, _chat_id, _thread_id, binding in self.iter_topic_bindings():
            if binding.window_id.strip() != normalized_window_id:
                continue
            raw_service_tier = binding.service_tier.strip().lower()
            return raw_service_tier if raw_service_tier in CODEX_SERVICE_TIERS else ""
        return ""

    def get_window_topic_response_mode(self, window_id: str) -> str:
        """Return one persisted response mode for a window-bound topic."""
        normalized_window_id = window_id.strip()
        if not normalized_window_id:
            return "text"
        for _user_id, _chat_id, _thread_id, binding in self.iter_topic_bindings():
            if binding.window_id.strip() != normalized_window_id:
                continue
            raw_response_mode = binding.response_mode.strip().lower()
            return raw_response_mode if raw_response_mode in {"text", "voice"} else "text"
        return "text"

    def get_topic_response_mode(
        self,
        user_id: int,
        thread_id: int | None,
        *,
        chat_id: int | None = None,
    ) -> str:
        """Return one persisted response mode for a specific topic binding."""
        binding = self.resolve_topic_binding(user_id, thread_id, chat_id=chat_id)
        if binding is None:
            return "text"
        raw_response_mode = binding.response_mode.strip().lower()
        return raw_response_mode if raw_response_mode in {"text", "voice"} else "text"

    def get_window_machine_id(self, window_id: str) -> str:
        """Return the bound machine id for a window-bound topic."""
        normalized_window_id = window_id.strip()
        if not normalized_window_id:
            return ""
        for _user_id, _chat_id, _thread_id, binding in self.iter_topic_bindings():
            if binding.window_id.strip() != normalized_window_id:
                continue
            return binding.machine_id.strip()
        return ""

    @staticmethod
    def _format_goal_transport_error(err: Exception) -> str:
        """Render one user-facing error string for native goal transport failures."""
        text = str(err).strip() or err.__class__.__name__
        if "goals feature is disabled" in text.lower():
            return "Codex goals feature is disabled on this machine."
        return text

    async def resolve_goal_thread_for_topic(
        self,
        *,
        user_id: int,
        thread_id: int | None,
        chat_id: int | None = None,
        create: bool = False,
        force_refresh: bool = False,
    ) -> tuple[str, str]:
        """Resolve the native Codex thread id for one topic goal operation."""
        if thread_id is None:
            return "", "Use `/goal` inside a named topic."

        binding = self.resolve_topic_binding(user_id, thread_id, chat_id=chat_id)
        if binding is None:
            return "", "No session is bound to this topic. Run `/start` or `/folder` first."
        persisted_binding = self._get_persisted_topic_binding(
            user_id,
            thread_id,
            chat_id=chat_id,
        )
        if persisted_binding is None:
            return "", "No persisted topic binding exists for this topic."

        # Goal recovery is an implicit operation, so only the raw persisted
        # binding may authorize the thread, machine, and workspace it touches.
        binding = persisted_binding

        local_machine_id, _local_machine_name = self._local_machine_identity()
        machine_id = binding.machine_id.strip()
        is_remote = bool(machine_id and machine_id != local_machine_id)

        window_id = binding.window_id.strip()
        binding_cwd = binding.cwd.strip()
        codex_thread_id = (
            persisted_binding.codex_thread_id.strip()
            if persisted_binding is not None
            else ""
        )
        cached_thread_id = (
            self.get_window_codex_thread_id(window_id) if window_id else ""
        )
        if persisted_binding is not None and not codex_thread_id and cached_thread_id:
            return (
                "",
                "No canonical Codex thread is persisted for this topic, but "
                "the window cache contains a thread ID. Use `/resume` to select "
                "the intended thread explicitly.",
            )
        if codex_thread_id and cached_thread_id and cached_thread_id != codex_thread_id:
            return (
                "",
                "The topic binding and window cache disagree about the Codex "
                "thread. Use `/resume` to select the intended thread explicitly.",
            )
        if codex_thread_id and window_id and not cached_thread_id:
            self._set_window_codex_thread_cache(window_id, codex_thread_id)
        if codex_thread_id and not force_refresh:
            return codex_thread_id, ""

        if not create:
            return "", "No persisted Codex thread is bound yet; no goal is set."

        if not window_id:
            return "", "No session is bound to this topic. Run `/start` or `/folder` first."

        state = self.get_window_state(window_id)
        # Goal recovery is implicit.  A cached window cwd is not authority to
        # repair an incomplete raw topic binding because that cache can belong
        # to a previously rebound session.  Only an explicit lifecycle action
        # may establish the missing workspace in the persisted binding.
        cwd = binding_cwd
        if not cwd:
            return "", "No workspace is bound to this topic yet. Run `/start` or `/folder` first."

        if is_remote:
            from .agent_rpc import agent_rpc_client

            machine_name = binding.machine_display_name.strip() or machine_id
            if codex_thread_id:
                try:
                    resumed = await agent_rpc_client.resume_thread(
                        machine_id,
                        window_id=window_id,
                        cwd=cwd,
                        thread_id=codex_thread_id,
                        window_name=(
                            binding.display_name.strip()
                            or state.window_name.strip()
                        ),
                        approval_mode=state.approval_mode.strip(),
                    )
                except Exception as e:
                    return "", self._format_goal_transport_error(e)

                resumed_thread_id = (
                    str(resumed.get("thread_id", "")).strip()
                    if isinstance(resumed, dict)
                    else ""
                )
                if resumed_thread_id != codex_thread_id:
                    emit_telemetry(
                        "session.implicit_rebind_blocked",
                        window_id=window_id,
                        bound_thread_id=codex_thread_id,
                        rejected_thread_id=resumed_thread_id,
                        source="goal_refresh_remote",
                    )
                    return (
                        "",
                        "Refused to replace this topic's bound Codex thread "
                        "during goal recovery.",
                    )
                if not self._topic_binding_ownership_matches(
                    user_id,
                    thread_id,
                    chat_id=chat_id,
                    window_id=window_id,
                    codex_thread_id=codex_thread_id,
                    machine_id=machine_id,
                    cwd=binding_cwd,
                ):
                    return (
                        "",
                        "The topic's canonical Codex binding changed while goal "
                        "recovery was in flight.",
                    )
                self.inherit_window_topic_model_selection(
                    window_id=window_id,
                    model_slug=str(resumed.get("model_slug", "")).strip(),
                    reasoning_effort=str(resumed.get("reasoning_effort", "")).strip(),
                )
                return codex_thread_id, ""

            try:
                ensured = await agent_rpc_client.ensure_thread(
                    machine_id,
                    window_id=window_id,
                    cwd=cwd,
                    window_name=binding.display_name.strip() or state.window_name.strip(),
                    approval_mode=state.approval_mode.strip(),
                    model_slug=binding.model_slug.strip(),
                    reasoning_effort=binding.reasoning_effort.strip(),
                    service_tier=binding.service_tier.strip(),
                )
            except Exception as e:
                return "", self._format_goal_transport_error(e)

            ensured_thread_id = str(ensured.get("thread_id", "")).strip() if isinstance(ensured, dict) else ""
            if not ensured_thread_id:
                return "", f"Failed to create a Codex thread on remote machine `{machine_name}`."
            if not self._topic_binding_ownership_matches(
                user_id,
                thread_id,
                chat_id=chat_id,
                window_id=window_id,
                codex_thread_id="",
                machine_id=machine_id,
                cwd=binding_cwd,
            ):
                return (
                    "",
                    "The topic's canonical Codex binding changed while a remote "
                    "goal thread was being created.",
                )
            self.bind_topic_to_codex_thread(
                user_id=user_id,
                thread_id=thread_id,
                chat_id=chat_id,
                codex_thread_id=ensured_thread_id,
                cwd=cwd,
                display_name=binding.display_name.strip() or self.get_display_name(window_id),
                window_id=window_id,
                machine_id=machine_id,
                machine_display_name=machine_name,
            )
            return ensured_thread_id, ""

        lock = self._get_window_send_lock(window_id)
        async with lock:
            existing_thread_id = self.get_window_codex_thread_id(window_id)
            if existing_thread_id and not force_refresh:
                return existing_thread_id, ""
            bound_thread_id = codex_thread_id or existing_thread_id
            if bound_thread_id:
                try:
                    resumed_thread_id = await self.resume_codex_session_for_window(
                        window_id=window_id,
                        cwd=cwd,
                        thread_id=bound_thread_id,
                    )
                except Exception as e:
                    return "", self._format_goal_transport_error(e)
                if resumed_thread_id != bound_thread_id:
                    emit_telemetry(
                        "session.implicit_rebind_blocked",
                        window_id=window_id,
                        bound_thread_id=bound_thread_id,
                        rejected_thread_id=resumed_thread_id,
                        source="goal_refresh_local",
                    )
                    return (
                        "",
                        "Refused to replace this topic's bound Codex thread "
                        "during goal recovery.",
                    )
                if not self._topic_binding_ownership_matches(
                    user_id,
                    thread_id,
                    chat_id=chat_id,
                    window_id=window_id,
                    codex_thread_id=codex_thread_id,
                    machine_id=machine_id,
                    cwd=binding_cwd,
                ):
                    return (
                        "",
                        "The topic's canonical Codex binding changed while local "
                        "goal recovery was in flight.",
                    )
                return bound_thread_id, ""

            try:
                created_thread_id, _approval_policy = (
                    await self._ensure_codex_thread_for_window(
                        window_id=window_id,
                        cwd=cwd,
                        sync_topic_bindings=False,
                    )
                )
            except Exception as e:
                return "", self._format_goal_transport_error(e)
            if not self._topic_binding_ownership_matches(
                user_id,
                thread_id,
                chat_id=chat_id,
                window_id=window_id,
                codex_thread_id="",
                machine_id=machine_id,
                cwd=binding_cwd,
            ):
                return (
                    "",
                    "The topic's canonical Codex binding changed while a local "
                    "goal thread was being created.",
                )
            self.bind_topic_to_codex_thread(
                user_id=user_id,
                thread_id=thread_id,
                chat_id=chat_id,
                codex_thread_id=created_thread_id,
                cwd=cwd,
                display_name=(
                    binding.display_name.strip()
                    or self.get_display_name(window_id)
                ),
                window_id=window_id,
                machine_id=machine_id,
                machine_display_name=binding.machine_display_name.strip(),
            )
            return created_thread_id, ""

    async def get_topic_goal(
        self,
        *,
        user_id: int,
        thread_id: int | None,
        chat_id: int | None = None,
    ) -> tuple[bool, dict[str, Any] | None, str]:
        """Read the native Codex goal for one topic."""
        codex_thread_id, error = await self.resolve_goal_thread_for_topic(
            user_id=user_id,
            thread_id=thread_id,
            chat_id=chat_id,
            create=False,
        )
        if not codex_thread_id:
            return False, None, error
        try:
            binding = self.resolve_topic_binding(user_id, thread_id, chat_id=chat_id)
            machine_id = binding.machine_id.strip() if binding else ""
            local_machine_id, _local_machine_name = self._local_machine_identity()
            if machine_id and machine_id != local_machine_id:
                from .agent_rpc import agent_rpc_client

                payload = await agent_rpc_client.thread_goal_get(
                    machine_id,
                    thread_id=codex_thread_id,
                )
            else:
                payload = await codex_app_server_client.thread_goal_get(
                    thread_id=codex_thread_id
                )
        except Exception as e:
            return False, None, self._format_goal_transport_error(e)
        return True, payload, ""

    async def set_topic_goal(
        self,
        *,
        user_id: int,
        thread_id: int | None,
        goal_text: str,
        chat_id: int | None = None,
    ) -> tuple[bool, dict[str, Any] | None, str]:
        """Create/update the native Codex goal for one topic."""
        normalized_goal_text = goal_text.strip()
        if not normalized_goal_text:
            return False, None, "Goal text is required."
        codex_thread_id, error = await self.resolve_goal_thread_for_topic(
            user_id=user_id,
            thread_id=thread_id,
            chat_id=chat_id,
            create=True,
        )
        if not codex_thread_id:
            return False, None, error
        local_machine_id, _local_machine_name = self._local_machine_identity()

        async def _send_goal(target_thread_id: str) -> dict[str, Any]:
            binding = (
                self._get_persisted_topic_binding(
                    user_id,
                    thread_id,
                    chat_id=chat_id,
                )
                if thread_id is not None
                else None
            )
            if (
                binding is None
                or binding.codex_thread_id.strip() != target_thread_id.strip()
            ):
                raise CodexAppServerError(
                    "The topic's canonical Codex binding changed before the goal "
                    "mutation. The request was not sent."
                )
            captured_window_id = binding.window_id.strip()
            captured_machine_id = binding.machine_id.strip()
            captured_cwd = binding.cwd.strip()
            if captured_machine_id and captured_machine_id != local_machine_id:
                from .agent_rpc import agent_rpc_client

                payload = await agent_rpc_client.thread_goal_set(
                    captured_machine_id,
                    thread_id=target_thread_id,
                    goal=normalized_goal_text,
                )
            else:
                payload = await codex_app_server_client.thread_goal_set(
                    thread_id=target_thread_id,
                    goal=normalized_goal_text,
                )
            if not self._topic_binding_ownership_matches(
                user_id,
                thread_id,
                chat_id=chat_id,
                window_id=captured_window_id,
                codex_thread_id=target_thread_id,
                machine_id=captured_machine_id,
                cwd=captured_cwd,
            ):
                raise CodexAppServerError(
                    "The topic's canonical Codex binding changed while the goal "
                    "mutation was in flight; the outcome is uncertain and the "
                    "request will not be replayed automatically."
                )
            return payload

        try:
            payload = await _send_goal(codex_thread_id)
        except Exception as e:
            if self._is_missing_goal_error(e):
                logger.warning(
                    "Goal update hit missing goal state for thread %s; refreshing topic thread and retrying once",
                    codex_thread_id,
                )
                refreshed_thread_id, refresh_error = await self.resolve_goal_thread_for_topic(
                    user_id=user_id,
                    thread_id=thread_id,
                    chat_id=chat_id,
                    create=True,
                    force_refresh=True,
                )
                if refreshed_thread_id:
                    try:
                        payload = await _send_goal(refreshed_thread_id)
                    except Exception as retry_err:
                        return False, None, self._format_goal_transport_error(retry_err)
                    return True, payload, ""
                if refresh_error:
                    return False, None, refresh_error
            return False, None, self._format_goal_transport_error(e)
        return True, payload, ""

    async def clear_topic_goal(
        self,
        *,
        user_id: int,
        thread_id: int | None,
        chat_id: int | None = None,
    ) -> tuple[bool, dict[str, Any] | None, str]:
        """Clear the native Codex goal for one topic."""
        codex_thread_id, error = await self.resolve_goal_thread_for_topic(
            user_id=user_id,
            thread_id=thread_id,
            chat_id=chat_id,
            create=False,
        )
        if not codex_thread_id:
            return False, None, error
        try:
            binding = self.resolve_topic_binding(user_id, thread_id, chat_id=chat_id)
            machine_id = binding.machine_id.strip() if binding else ""
            local_machine_id, _local_machine_name = self._local_machine_identity()
            if machine_id and machine_id != local_machine_id:
                from .agent_rpc import agent_rpc_client

                payload = await agent_rpc_client.thread_goal_clear(
                    machine_id,
                    thread_id=codex_thread_id,
                )
            else:
                payload = await codex_app_server_client.thread_goal_clear(
                    thread_id=codex_thread_id
                )
        except Exception as e:
            return False, None, self._format_goal_transport_error(e)
        return True, payload, ""

    def get_machine_transcription_profile_selection(self, machine_id: str = "") -> str:
        """Return the server-wide transcription profile for one machine."""
        normalized_machine_id = machine_id.strip()
        if not normalized_machine_id:
            return ""
        raw_profile = self.machine_transcription_profiles.get(normalized_machine_id, "")
        if not isinstance(raw_profile, str):
            return ""
        normalized_profile = raw_profile.strip().lower()
        return normalized_profile if normalized_profile in TRANSCRIPTION_PROFILES else ""

    def iter_topics_for_machine(
        self,
        machine_id: str,
    ) -> Iterator[tuple[int, int | None, int, TopicBinding]]:
        """Iterate all topic bindings bound to one machine id."""
        normalized_machine_id = machine_id.strip()
        if not normalized_machine_id:
            return
        for user_id, chat_id, thread_id, binding in self.iter_topic_bindings():
            if binding.machine_id.strip() != normalized_machine_id:
                continue
            yield user_id, chat_id, thread_id, binding

    def set_topic_model_selection(
        self,
        user_id: int,
        thread_id: int | None,
        *,
        chat_id: int | None = None,
        model_slug: str = "",
        reasoning_effort: str = "",
    ) -> bool:
        """Persist the per-topic model selection."""
        binding = self.ensure_topic_binding(user_id, thread_id, chat_id=chat_id)
        if binding is None:
            return False
        normalized_model = model_slug.strip()
        normalized_effort = reasoning_effort.strip()
        if (
            binding.model_slug == normalized_model
            and binding.reasoning_effort == normalized_effort
            and binding.model_selection_explicit
        ):
            return False
        binding.model_slug = normalized_model
        binding.reasoning_effort = normalized_effort
        binding.model_selection_explicit = bool(normalized_model or normalized_effort)
        self._save_state()
        return True

    def invalidate_topic_codex_thread(
        self,
        user_id: int,
        thread_id: int | None,
        *,
        chat_id: int | None = None,
    ) -> bool:
        """Clear the active Codex thread binding for one topic.

        This forces the next turn to create a fresh thread so updated
        topic-scoped model or reasoning settings actually take effect.
        """
        if thread_id is None:
            return False
        window_id = self.get_window_for_thread(user_id, thread_id, chat_id=chat_id)
        if not window_id:
            return False
        if not self.get_window_codex_thread_id(window_id) and not self.get_window_codex_active_turn_id(window_id):
            return False
        self.set_window_codex_thread_id(window_id, "")
        return True

    def set_topic_service_tier_selection(
        self,
        user_id: int,
        thread_id: int | None,
        *,
        chat_id: int | None = None,
        service_tier: str = "",
    ) -> bool:
        """Persist the per-topic service tier selection."""
        binding = self.ensure_topic_binding(user_id, thread_id, chat_id=chat_id)
        if binding is None:
            return False
        normalized_service_tier = service_tier.strip().lower()
        if normalized_service_tier not in CODEX_SERVICE_TIERS:
            normalized_service_tier = ""
        if binding.service_tier == normalized_service_tier:
            return False
        binding.service_tier = normalized_service_tier
        self._save_state()
        return True

    def set_topic_response_mode(
        self,
        user_id: int,
        thread_id: int | None,
        *,
        chat_id: int | None = None,
        response_mode: str = "",
    ) -> bool:
        """Persist the preferred reply modality for one topic."""
        binding = self.ensure_topic_binding(user_id, thread_id, chat_id=chat_id)
        if binding is None:
            return False
        normalized = response_mode.strip().lower()
        if normalized not in {"text", "voice"}:
            normalized = "text"
        if binding.response_mode == normalized:
            return False
        binding.response_mode = normalized
        self._save_state()
        return True

    def set_next_topic_response_mode(
        self,
        user_id: int,
        thread_id: int | None,
        *,
        chat_id: int | None = None,
        response_mode: str = "",
    ) -> bool:
        """Set one non-persistent reply mode override for the next topic response."""
        try:
            normalized_thread_id = int(thread_id or 0)
        except (TypeError, ValueError):
            normalized_thread_id = 0
        if normalized_thread_id <= 0:
            return False
        normalized_chat_id = int(chat_id or 0)
        normalized = response_mode.strip().lower()
        if normalized not in {"text", "voice"}:
            self._next_topic_response_mode.pop(
                (int(user_id), normalized_chat_id, normalized_thread_id),
                None,
            )
            return False
        key = (int(user_id), normalized_chat_id, normalized_thread_id)
        if self._next_topic_response_mode.get(key) == normalized:
            return False
        self._next_topic_response_mode[key] = normalized
        return True

    def peek_next_topic_response_mode(
        self,
        user_id: int,
        thread_id: int | None,
        *,
        chat_id: int | None = None,
    ) -> str:
        """Return one pending non-persistent reply mode override, if any."""
        try:
            normalized_thread_id = int(thread_id or 0)
        except (TypeError, ValueError):
            return ""
        if normalized_thread_id <= 0:
            return ""
        key = (int(user_id), int(chat_id or 0), normalized_thread_id)
        raw = self._next_topic_response_mode.get(key, "").strip().lower()
        return raw if raw in {"text", "voice"} else ""

    def consume_next_topic_response_mode(
        self,
        user_id: int,
        thread_id: int | None,
        *,
        chat_id: int | None = None,
    ) -> str:
        """Consume and return one pending non-persistent reply mode override."""
        try:
            normalized_thread_id = int(thread_id or 0)
        except (TypeError, ValueError):
            return ""
        if normalized_thread_id <= 0:
            return ""
        key = (int(user_id), int(chat_id or 0), normalized_thread_id)
        raw = self._next_topic_response_mode.pop(key, "").strip().lower()
        return raw if raw in {"text", "voice"} else ""

    def set_machine_transcription_profile_selection(
        self,
        machine_id: str,
        *,
        transcription_profile: str = "",
    ) -> bool:
        """Persist the server-wide transcription profile for one machine."""
        normalized_machine_id = machine_id.strip()
        if not normalized_machine_id:
            return False
        normalized_profile = transcription_profile.strip().lower()
        if normalized_profile not in TRANSCRIPTION_PROFILES:
            normalized_profile = ""
        current_profile = self.get_machine_transcription_profile_selection(
            normalized_machine_id
        )
        if current_profile == normalized_profile:
            return False
        if normalized_profile:
            self.machine_transcription_profiles[normalized_machine_id] = normalized_profile
        else:
            self.machine_transcription_profiles.pop(normalized_machine_id, None)
        self._save_state()
        return True

    def bind_topic_to_codex_thread(
        self,
        *,
        user_id: int,
        thread_id: int,
        chat_id: int | None = None,
        codex_thread_id: str,
        cwd: str = "",
        display_name: str = "",
        window_id: str = "",
        machine_id: str = "",
        machine_display_name: str = "",
    ) -> None:
        """Bind a topic directly to a Codex thread (transport-neutral API)."""
        normalized_codex_thread_id = codex_thread_id.strip()
        if not normalized_codex_thread_id:
            raise ValueError("codex_thread_id is required")

        existing = self.resolve_topic_binding(user_id, thread_id, chat_id=chat_id)
        resolved_window_id = window_id.strip() or (existing.window_id if existing else "")
        resolved_cwd = cwd.strip() or (existing.cwd if existing else "")
        resolved_display_name = (
            display_name.strip() or (existing.display_name if existing else "")
        )
        resolved_chat_id = chat_id if chat_id is not None else (existing.chat_id if existing else 0)
        resolved_sync_mode = (
            existing.sync_mode if existing else TOPIC_SYNC_MODE_TELEGRAM_LIVE
        )
        local_machine_id, local_machine_name = self._local_machine_identity()
        resolved_machine_id = (
            machine_id.strip()
            or (existing.machine_id if existing and existing.machine_id else local_machine_id)
        )
        resolved_machine_display_name = (
            machine_display_name.strip()
            or (
                existing.machine_display_name
                if existing and existing.machine_display_name
                else local_machine_name
            )
        )
        resolved_model_slug = existing.model_slug if existing else ""
        resolved_reasoning_effort = existing.reasoning_effort if existing else ""
        resolved_model_selection_explicit = (
            existing.model_selection_explicit if existing else False
        )
        resolved_service_tier = existing.service_tier if existing else ""
        resolved_response_mode = existing.response_mode if existing else ""
        binding = TopicBinding(
            transport=TOPIC_BINDING_TRANSPORT_CODEX_THREAD,
            chat_id=resolved_chat_id,
            thread_id=thread_id,
            window_id=resolved_window_id,
            codex_thread_id=normalized_codex_thread_id,
            cwd=resolved_cwd,
            display_name=resolved_display_name,
            sync_mode=resolved_sync_mode,
            machine_id=resolved_machine_id,
            machine_display_name=resolved_machine_display_name,
            model_slug=resolved_model_slug,
            reasoning_effort=resolved_reasoning_effort,
            model_selection_explicit=resolved_model_selection_explicit,
            service_tier=resolved_service_tier,
            response_mode=resolved_response_mode,
        )
        self._set_topic_binding(
            user_id=user_id,
            thread_id=thread_id,
            chat_id=resolved_chat_id or None,
            binding=binding,
        )

        if resolved_window_id:
            state = self.get_window_state(resolved_window_id)
            state.codex_thread_id = normalized_codex_thread_id
            if resolved_cwd and state.cwd != resolved_cwd:
                state.cwd = resolved_cwd
            if resolved_display_name and state.window_name != resolved_display_name:
                state.window_name = resolved_display_name

        self._save_state()
        logger.info(
            "Bound thread %d -> codex_thread_id %s (window=%s) for user %d",
            thread_id,
            normalized_codex_thread_id,
            resolved_window_id or "<none>",
            user_id,
        )

    def resolve_topic_binding(
        self,
        user_id: int,
        thread_id: int | None,
        *,
        chat_id: int | None = None,
    ) -> TopicBinding | None:
        """Resolve a transport-neutral binding for one topic."""
        if thread_id is None:
            return None

        slot_key = self._find_topic_slot_key(user_id, thread_id, chat_id=chat_id)
        if slot_key is None:
            return None
        binding = self.topic_bindings_v2.get(user_id, {}).get(slot_key)
        if binding is None:
            return None

        resolved_chat_id, resolved_thread_id = self._parse_topic_slot_key(slot_key)
        resolved = TopicBinding(
            transport=binding.transport,
            chat_id=binding.chat_id or (resolved_chat_id or 0),
            thread_id=binding.thread_id or resolved_thread_id,
            window_id=binding.window_id,
            codex_thread_id=binding.codex_thread_id,
            cwd=binding.cwd,
            display_name=binding.display_name,
            sync_mode=self._normalize_topic_sync_mode(binding.sync_mode),
            machine_id=binding.machine_id,
            machine_display_name=binding.machine_display_name,
            model_slug=binding.model_slug,
            reasoning_effort=binding.reasoning_effort,
            model_selection_explicit=binding.model_selection_explicit,
            service_tier=binding.service_tier,
            response_mode=binding.response_mode,
        )
        if resolved.window_id:
            fallback = self._topic_binding_from_window(resolved.window_id)
            if not resolved.codex_thread_id:
                resolved.codex_thread_id = fallback.codex_thread_id
            if not resolved.cwd:
                resolved.cwd = fallback.cwd
            if not resolved.display_name:
                resolved.display_name = fallback.display_name
        return resolved

    def resolve_topic_target(
        self,
        user_id: int,
        thread_id: int | None,
        *,
        chat_id: int | None = None,
    ) -> tuple[str, str] | None:
        """Resolve transport target for one topic."""
        binding = self.resolve_topic_binding(user_id, thread_id, chat_id=chat_id)
        if binding is None:
            return None
        if (
            binding.transport == TOPIC_BINDING_TRANSPORT_CODEX_THREAD
            and binding.codex_thread_id
        ):
            return TOPIC_BINDING_TRANSPORT_CODEX_THREAD, binding.codex_thread_id
        if binding.window_id:
            return TOPIC_BINDING_TRANSPORT_WINDOW, binding.window_id
        if binding.codex_thread_id:
            return TOPIC_BINDING_TRANSPORT_CODEX_THREAD, binding.codex_thread_id
        return None

    def iter_topic_bindings(self) -> Iterator[tuple[int, int | None, int, TopicBinding]]:
        """Iterate all topic bindings as (user_id, chat_id, thread_id, binding)."""
        for user_id, bindings in self._collect_topic_bindings().items():
            for slot_key, binding in bindings.items():
                parsed_chat_id, parsed_thread_id = self._parse_topic_slot_key(slot_key)
                thread_id = binding.thread_id or parsed_thread_id
                if thread_id <= 0:
                    continue
                chat_id = binding.chat_id or (parsed_chat_id or 0)
                yield user_id, (chat_id or None), thread_id, binding

    def unbind_topic(
        self,
        user_id: int,
        thread_id: int,
        *,
        chat_id: int | None = None,
    ) -> TopicBinding | None:
        """Remove a transport-neutral topic binding."""
        per_user_bindings = self.topic_bindings_v2.get(user_id)
        slot_key = self._find_topic_slot_key(user_id, thread_id, chat_id=chat_id)
        if not per_user_bindings or slot_key is None or slot_key not in per_user_bindings:
            return None
        removed = per_user_bindings.pop(slot_key)
        if not per_user_bindings:
            del self.topic_bindings_v2[user_id]
        return removed

    def allocate_virtual_window_id(self) -> str:
        """Allocate a synthetic window id for app-server-only topic bindings."""
        used_ids: set[str] = set(self.window_states.keys())
        for _user_id, _chat_id, _thread_id, window_id in self.iter_topic_window_bindings():
            used_ids.add(window_id)
        next_id = 900000
        while True:
            candidate = f"@{next_id}"
            if candidate not in used_ids:
                return candidate
            next_id += 1

    def bind_thread(
        self,
        user_id: int,
        thread_id: int,
        window_id: str,
        window_name: str = "",
        *,
        chat_id: int | None = None,
    ) -> None:
        """Bind a Telegram topic thread to a session window.

        Args:
            user_id: Telegram user ID
            thread_id: Telegram topic thread ID
            window_id: Tmux window ID (e.g. '@0')
            window_name: Display name for the window (optional)
        """
        fallback = self._topic_binding_from_window(window_id)
        existing = self.resolve_topic_binding(user_id, thread_id, chat_id=chat_id)
        display = window_name.strip() or fallback.display_name
        binding = TopicBinding(
            transport=TOPIC_BINDING_TRANSPORT_WINDOW,
            chat_id=chat_id or 0,
            thread_id=thread_id,
            window_id=window_id,
            codex_thread_id=fallback.codex_thread_id,
            cwd=fallback.cwd,
            display_name=display,
            sync_mode=existing.sync_mode if existing else TOPIC_SYNC_MODE_TELEGRAM_LIVE,
            machine_id=existing.machine_id if existing else fallback.machine_id,
            machine_display_name=(
                existing.machine_display_name if existing else fallback.machine_display_name
            ),
            model_slug=existing.model_slug if existing else "",
            reasoning_effort=existing.reasoning_effort if existing else "",
            model_selection_explicit=(
                existing.model_selection_explicit if existing else False
            ),
            service_tier=existing.service_tier if existing else "",
        )
        self._set_topic_binding(
            user_id=user_id,
            thread_id=thread_id,
            chat_id=chat_id,
            binding=binding,
        )
        self._save_state()
        logger.info(
            "Bound thread %d -> window_id %s (%s) for user %d",
            thread_id,
            window_id,
            display,
            user_id,
        )

    def unbind_thread(
        self,
        user_id: int,
        thread_id: int,
        *,
        chat_id: int | None = None,
    ) -> str | None:
        """Remove a thread binding. Returns the previously bound window_id, or None."""
        slot_key = self._find_topic_slot_key(user_id, thread_id, chat_id=chat_id)
        removed = self.unbind_topic(user_id, thread_id, chat_id=chat_id)
        if removed is None:
            return None
        window_id = removed.window_id or None
        per_user_skills = self.thread_skills.get(user_id)
        if per_user_skills and slot_key and slot_key in per_user_skills:
            del per_user_skills[slot_key]
            if not per_user_skills:
                del self.thread_skills[user_id]
        per_user_codex_skills = self.thread_codex_skills.get(user_id)
        if per_user_codex_skills and slot_key and slot_key in per_user_codex_skills:
            del per_user_codex_skills[slot_key]
            if not per_user_codex_skills:
                del self.thread_codex_skills[user_id]
        self._save_state()
        logger.info(
            "Unbound thread %d (was %s) for user %d",
            thread_id,
            window_id or "<none>",
            user_id,
        )
        return window_id

    def get_window_for_thread(
        self,
        user_id: int,
        thread_id: int,
        *,
        chat_id: int | None = None,
    ) -> str | None:
        """Look up the window_id bound to a thread."""
        binding = self.resolve_topic_binding(user_id, thread_id, chat_id=chat_id)
        if not binding:
            return None
        window_id = getattr(binding, "window_id", "")
        if not isinstance(window_id, str):
            return None
        window_id = window_id.strip()
        return window_id or None

    def resolve_window_for_thread(
        self,
        user_id: int,
        thread_id: int | None,
        *,
        chat_id: int | None = None,
    ) -> str | None:
        """Resolve the window_id for a user's thread.

        Returns None if thread_id is None or the thread is not bound.
        """
        if thread_id is None:
            return None
        return self.get_window_for_thread(user_id, thread_id, chat_id=chat_id)

    def iter_topic_window_bindings(self) -> Iterator[tuple[int, int | None, int, str]]:
        """Iterate all thread bindings as (user_id, chat_id, thread_id, window_id).

        Provides a window-id view derived from transport-neutral topic bindings.
        """
        for user_id, chat_id, thread_id, binding in self.iter_topic_bindings():
            window_id = binding.window_id.strip()
            if window_id:
                yield user_id, chat_id, thread_id, window_id

    async def find_users_for_session(
        self,
        session_id: str,
    ) -> list[tuple[int, int | None, str, int]]:
        """Find all users whose thread-bound window maps to the given session_id.

        Returns list of (user_id, chat_id, window_id, thread_id) tuples.
        """
        result: list[tuple[int, int | None, str, int]] = []
        for user_id, chat_id, thread_id, window_id in self.iter_topic_window_bindings():
            state = self.get_window_state(window_id)
            persisted = self._get_persisted_topic_binding(
                user_id,
                thread_id,
                chat_id=chat_id,
            )
            canonical_thread_id = (
                persisted.codex_thread_id.strip() if persisted is not None else ""
            )
            if not canonical_thread_id:
                continue
            if canonical_thread_id and session_id != canonical_thread_id:
                continue
            if state.session_id == session_id:
                result.append((user_id, chat_id, window_id, thread_id))
                continue

            # Known non-empty session IDs are authoritative enough to skip
            # expensive transcript re-resolution on every streamed chunk.
            if state.session_id and (
                not canonical_thread_id
                or state.session_id == canonical_thread_id
            ):
                continue

            # Session ID can be briefly empty right after sending input in Codex
            # mode; attempt one lightweight autodiscovery before giving up.
            try:
                await self.autodiscover_session_for_window(window_id)
            except Exception as e:
                logger.debug("Autodiscovery failed for window %s: %s", window_id, e)
                continue

            refreshed_state = self.get_window_state(window_id)
            if refreshed_state.session_id == session_id:
                result.append((user_id, chat_id, window_id, thread_id))
        return result

    def find_users_for_codex_thread(
        self,
        codex_thread_id: str,
    ) -> list[tuple[int, int | None, str, int]]:
        """Find all users whose bound window maps to a Codex app-server thread id.

        Returns list of (user_id, chat_id, window_id, thread_id) tuples.
        """
        if not codex_thread_id:
            return []
        result: list[tuple[int, int | None, str, int]] = []
        for user_id, chat_id, thread_id, binding in self.iter_topic_bindings():
            resolved_codex_thread_id = binding.codex_thread_id.strip()
            window_id = binding.window_id.strip()
            if resolved_codex_thread_id != codex_thread_id:
                continue
            if not window_id:
                # Keep tuple shape stable for callers that currently expect window_id.
                if chat_id is not None:
                    window_id = f"topic:{user_id}:{chat_id}:{thread_id}"
                else:
                    window_id = f"topic:{user_id}:{thread_id}"
            result.append((user_id, chat_id, window_id, thread_id))
        return result

    def set_codex_turn_for_thread(
        self,
        codex_thread_id: str,
        turn_id: str,
        *,
        machine_id: str = "",
    ) -> None:
        """Update active turn ids for one thread, optionally on one machine."""
        if not codex_thread_id:
            return
        changed = False
        normalized = turn_id.strip()
        normalized_machine_id = machine_id.strip()
        for window_id, state in self.window_states.items():
            if state.codex_thread_id != codex_thread_id:
                continue
            if (
                normalized_machine_id
                and self.get_window_machine_id(window_id).strip()
                != normalized_machine_id
            ):
                continue
            if state.codex_active_turn_id == normalized:
                continue
            state.codex_active_turn_id = normalized
            changed = True
        if changed:
            self._save_state()

    async def validate_codex_topic_bindings(self) -> dict[str, int]:
        """Validate persisted Codex thread bindings without changing ownership."""
        thread_ids: set[str] = set()
        for _user_id, bindings in self.topic_bindings_v2.items():
            for _slot_key, binding in bindings.items():
                codex_thread_id = binding.codex_thread_id.strip()
                if codex_thread_id:
                    thread_ids.add(codex_thread_id)

        if not thread_ids:
            return {"checked": 0, "invalid": 0, "repaired": 0}

        checked = 0
        invalid_thread_ids: set[str] = set()
        for codex_thread_id in sorted(thread_ids):
            checked += 1
            try:
                rollout_path = self._find_codex_session_file_for_thread(
                    codex_thread_id
                )
                rollout_size = (
                    rollout_path.stat().st_size
                    if rollout_path is not None
                    else 0
                )
            except OSError as exc:
                logger.warning(
                    "Stored Codex thread transcript check failed "
                    "(thread=%s): %s",
                    codex_thread_id,
                    exc,
                )
                invalid_thread_ids.add(codex_thread_id)
                continue
            resume_limit = int(config.codex_max_resume_bytes)
            if rollout_size > resume_limit:
                emit_telemetry(
                    "transport.app_server.oversized_binding_preserved",
                    runtime_mode=config.runtime_mode,
                    codex_transport=config.codex_transport,
                    thread_id=codex_thread_id,
                    rollout_path=str(rollout_path),
                    rollout_size_bytes=rollout_size,
                    resume_limit_bytes=resume_limit,
                )
                logger.warning(
                    "Preserving oversized Codex binding without app-server "
                    "validation for thread %s (size=%d limit=%d)",
                    codex_thread_id,
                    rollout_size,
                    resume_limit,
                )
                continue
            try:
                payload = await codex_app_server_client.thread_read(
                    thread_id=codex_thread_id,
                    timeout=10.0,
                    include_turns=False,
                )
            except Exception as e:
                logger.warning(
                    "Stored Codex thread validation failed (thread=%s): %s",
                    codex_thread_id,
                    e,
                )
                invalid_thread_ids.add(codex_thread_id)
                continue

            thread_obj = payload.get("thread") if isinstance(payload, dict) else None
            resolved_thread_id = ""
            if isinstance(thread_obj, dict):
                raw_id = thread_obj.get("id")
                if isinstance(raw_id, str):
                    resolved_thread_id = raw_id.strip()

            if resolved_thread_id == codex_thread_id:
                continue

            if resolved_thread_id:
                logger.warning(
                    "Stored Codex thread id mismatch (stored=%s returned=%s)",
                    codex_thread_id,
                    resolved_thread_id,
                )
            else:
                logger.warning(
                    "Stored Codex thread validation returned no thread id (thread=%s)",
                    codex_thread_id,
                )
            invalid_thread_ids.add(codex_thread_id)

        if not invalid_thread_ids:
            return {"checked": checked, "invalid": 0, "repaired": 0}
        return {
            "checked": checked,
            "invalid": len(invalid_thread_ids),
            "repaired": 0,
        }

    # --- Tmux helpers ---

    def note_window_input(
        self,
        window_id: str,
        *,
        window_name: str = "",
        cwd: str = "",
    ) -> None:
        """Record that user input was sent to a window.

        Updates last_input_ts and refresh-related state used by session
        autodiscovery. This clears the cached session_id so the next transcript
        lookup re-resolves the active session.
        """
        state = self.get_window_state(window_id)
        changed = False
        now = time.time()

        if state.last_input_ts != now:
            state.last_input_ts = now
            changed = True
        if state.session_id:
            # Force re-discovery after each user input; Codex may switch/fork sessions.
            state.session_id = ""
            changed = True
        if cwd and state.cwd != cwd:
            state.cwd = cwd
            changed = True
        if window_name and state.window_name != window_name:
            state.window_name = window_name
            changed = True
        if window_name and self.window_display_names.get(window_id) != window_name:
            self.window_display_names[window_id] = window_name
            changed = True

        if changed:
            self._save_state()

    @staticmethod
    def _codex_app_server_mode_enabled() -> bool:
        return True

    @staticmethod
    def _normalize_approval_policy(raw_mode: str) -> str:
        mode = raw_mode.strip().lower()
        if mode in {"", "default", "inherit", "inherited"}:
            return ""
        if mode in {"untrusted", "on-request", "never"}:
            return mode
        if mode == "on-failure":
            return "on-failure"
        # Map richer bot-level modes to closest app-server policy.
        if mode in {
            "full-auto",
            "full_auto",
            "agent",
            "dangerous",
            "dangerously-bypass-approvals-and-sandbox",
        }:
            return "never"
        return ""

    @classmethod
    def _infer_default_approval_policy_from_command(cls) -> str:
        """Infer app-server approval policy from configured assistant command."""
        try:
            parts = shlex.split(config.assistant_command)
        except ValueError:
            return ""

        for idx, token in enumerate(parts):
            if token in {"--full-auto", "--dangerously-bypass-approvals-and-sandbox"}:
                return "never"
            if token in {"-a", "--ask-for-approval"} and idx + 1 < len(parts):
                inferred = cls._normalize_approval_policy(parts[idx + 1])
                if inferred:
                    return inferred
            if token.startswith("--ask-for-approval="):
                _left, _sep, value = token.partition("=")
                inferred = cls._normalize_approval_policy(value)
                if inferred:
                    return inferred
        return ""

    @staticmethod
    def _runtime_write_state(cwd: str) -> tuple[str, bool]:
        """Return normalized workspace path and writeability for runtime hinting."""
        raw_path = cwd.strip() if isinstance(cwd, str) else ""
        path = Path(raw_path).expanduser() if raw_path else Path.cwd()
        if path.exists() and path.is_file():
            path = path.parent
        resolved = path.resolve()
        can_write = resolved.exists() and os.access(resolved, os.W_OK)
        return str(resolved), can_write

    @staticmethod
    def _build_runtime_capability_hint(
        *,
        workspace_path: str,
        can_write: bool,
        approval_policy: str,
        session_start_reason: str = "",
        tts_available: bool = False,
        tts_default_voice: str = "",
        tts_default_speed: float = 1.0,
        transcription_runtime_label: str = "",
    ) -> str:
        """Build one short runtime context note to avoid stale read-only assumptions."""
        write_state = "enabled" if can_write else "disabled"
        session_reason_line = ""
        if session_start_reason:
            session_reason_line = f"Session start reason: {session_start_reason}\n"
        transcription_line = ""
        if transcription_runtime_label:
            transcription_line = f"Speech-to-text: {transcription_runtime_label}\n"
        tts_line = (
            f"Text-to-speech: available (voice `{tts_default_voice}`, speed `{tts_default_speed:.1f}`)\n"
            if tts_available
            else "Text-to-speech: unavailable\n"
        )
        return (
            "[coco runtime context]\n"
            f"Workspace: {workspace_path}\n"
            f"Filesystem write access: {write_state}\n"
            f"Approval policy: {approval_policy}\n"
            f"{session_reason_line}"
            f"{transcription_line}"
            f"{tts_line}"
            "Telegram attachments: to upload a workspace file for the user, "
            'append a standalone line exactly like '
            '<telegram-attachment path="relative/path.pdf" /> '
            "after your normal answer. Supported types: .pdf, .txt, .md, "
            ".png, .jpg, .jpeg, .webp, .gif, .bmp, .tif, .tiff, "
            ".mp4, .mov, .webm, .mkv, .avi, .mpeg, .mpg. "
            "Use only files inside the workspace. The tag line is hidden from the user.\n"
            "Treat this as the current runtime capability for this turn, "
            "not as a user request."
        )

    @staticmethod
    def _chunk_text_for_app_server(
        text: str,
        *,
        max_chars: int = APP_SERVER_MAX_TEXT_CHARS_PER_INPUT,
    ) -> list[str]:
        """Split very large text payloads to avoid oversized single input items."""
        if not text or len(text) <= max_chars:
            return [text]

        chunks: list[str] = []
        current = ""
        for line in text.splitlines(keepends=True):
            if len(line) > max_chars:
                if current:
                    chunks.append(current)
                    current = ""
                for start in range(0, len(line), max_chars):
                    chunks.append(line[start : start + max_chars])
                continue

            if len(current) + len(line) > max_chars:
                if current:
                    chunks.append(current)
                current = line
            else:
                current += line

        if current:
            chunks.append(current)
        return chunks or [text]

    @classmethod
    def _normalize_app_server_inputs(
        cls,
        inputs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Normalize/split user inputs before app-server turn submission."""
        normalized: list[dict[str, Any]] = []
        for item in inputs:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "text":
                normalized.append(dict(item))
                continue
            text = item.get("text")
            if not isinstance(text, str):
                normalized.append(dict(item))
                continue
            parts = cls._chunk_text_for_app_server(text)
            if len(parts) <= 1:
                normalized.append(dict(item))
                continue
            for part in parts:
                normalized.append({"type": "text", "text": part})
        return normalized

    @staticmethod
    def _build_expected_transcript_user_text(inputs: list[dict[str, Any]]) -> str:
        """Rebuild the Codex transcript user_message text from text inputs."""
        parts: list[str] = []
        for item in inputs:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "text":
                continue
            text = item.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        return "".join(parts).strip()

    @staticmethod
    def _is_turn_start_timeout(err: Exception) -> bool:
        """Return whether an app-server exception is a turn/start timeout."""
        if not isinstance(err, CodexAppServerError):
            return False
        return "Timed out waiting for app-server response: turn/start" in str(err)

    async def _turn_start_with_retry(
        self,
        *,
        thread_id: str,
        inputs: list[dict[str, Any]],
        approval_policy: str,
        model_slug: str = "",
        reasoning_effort: str = "",
        service_tier: str = "",
        on_dispatch: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Start a turn once; timeout outcomes are uncertain and never replayed."""
        turn_kwargs: dict[str, Any] = {}
        if model_slug:
            turn_kwargs["model"] = model_slug
        if reasoning_effort:
            turn_kwargs["effort"] = reasoning_effort
        dispatch_kwargs = (
            {"on_dispatch": on_dispatch} if on_dispatch is not None else {}
        )
        try:
            return await codex_app_server_client.turn_start(
                thread_id=thread_id,
                inputs=inputs,
                approval_policy=approval_policy,
                service_tier=service_tier.strip() or None,
                timeout=APP_SERVER_TURN_START_TIMEOUT_SECONDS,
                **dispatch_kwargs,
                **turn_kwargs,
            )
        except Exception as error:
            if self._is_turn_start_timeout(error):
                # A notification can prove that the server accepted the turn
                # even when its response frame was lost. Otherwise the outcome
                # remains uncertain and must be surfaced without replay.
                existing_turn = codex_app_server_client.get_active_turn_id(
                    thread_id
                )
                if existing_turn:
                    logger.warning(
                        "turn/start timed out but active turn already exists "
                        "(thread=%s turn=%s); treating as success",
                        thread_id,
                        existing_turn,
                    )
                    return {"turn": {"id": existing_turn}}
            raise

    async def _ensure_codex_thread_for_window(
        self,
        *,
        window_id: str,
        cwd: str,
        model: str = "",
        effort: str = "",
        service_tier: str = "",
        sync_topic_bindings: bool = True,
        ownership_validator: Callable[[], bool] | None = None,
    ) -> tuple[str, str]:
        """Ensure a window has a Codex app-server thread id.

        Returns:
            (thread_id, approval_policy)
        """
        state = self.get_window_state(window_id)
        thread_id = state.codex_thread_id.strip()

        raw_mode = state.approval_mode.strip() or self.default_approval_mode.strip()
        approval_policy = self._normalize_approval_policy(raw_mode)
        if not approval_policy:
            approval_policy = self._infer_default_approval_policy_from_command() or "on-request"
        normalized_service_tier = service_tier.strip().lower()
        if normalized_service_tier not in CODEX_SERVICE_TIERS:
            normalized_service_tier = self.get_window_topic_service_tier_selection(window_id)

        if thread_id:
            return thread_id, approval_policy

        started = await codex_app_server_client.thread_start(
            cwd=cwd,
            approval_policy=approval_policy,
            model=model.strip() or None,
            effort=effort.strip() or None,
            service_tier=normalized_service_tier or None,
        )
        thread = started.get("thread") if isinstance(started, dict) else None
        new_thread_id = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(new_thread_id, str) or not new_thread_id:
            raise CodexAppServerError("thread/start did not return a thread id")
        if state.codex_thread_id.strip() != thread_id:
            raise CodexAppServerError(
                "window thread binding changed while thread/start was in flight"
            )
        if ownership_validator is not None and not ownership_validator():
            raise CodexAppServerError(
                "topic binding changed while thread/start was in flight"
            )

        changed = False
        if state.codex_thread_id != new_thread_id:
            state.codex_thread_id = new_thread_id
            state.codex_active_turn_id = ""
            changed = True
        if sync_topic_bindings:
            if self._sync_topic_bindings_for_window_codex_thread(
                window_id=window_id,
                thread_id=new_thread_id,
            ):
                changed = True
        if cwd and state.cwd != cwd:
            state.cwd = cwd
            changed = True
        if changed:
            self._save_state()
        return new_thread_id, approval_policy

    @staticmethod
    def _extract_lifecycle_thread_id(
        payload: dict[str, Any] | None,
        *,
        fallback: str = "",
    ) -> str:
        """Extract thread id from lifecycle response payload."""
        if not isinstance(payload, dict):
            return fallback
        direct = payload.get("threadId")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        thread = payload.get("thread")
        if isinstance(thread, dict):
            thread_id = thread.get("id")
            if isinstance(thread_id, str) and thread_id.strip():
                return thread_id.strip()
        forked = payload.get("forkedThread")
        if isinstance(forked, dict):
            thread_id = forked.get("id")
            if isinstance(thread_id, str) and thread_id.strip():
                return thread_id.strip()
        resumed = payload.get("resumedThread")
        if isinstance(resumed, dict):
            thread_id = resumed.get("id")
            if isinstance(thread_id, str) and thread_id.strip():
                return thread_id.strip()
        return fallback

    @staticmethod
    def _extract_lifecycle_turn_id(payload: dict[str, Any] | None) -> str:
        """Extract active turn id from lifecycle response payload."""
        if not isinstance(payload, dict):
            return ""
        direct = payload.get("turnId")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        turn = payload.get("turn")
        if isinstance(turn, dict):
            turn_id = turn.get("id")
            if isinstance(turn_id, str) and turn_id.strip():
                return turn_id.strip()
        return ""

    async def resume_latest_codex_session_for_window(
        self,
        *,
        window_id: str,
        cwd: str,
    ) -> str:
        """Resume latest Codex session for cwd and bind it to a window."""
        discovered = self._find_latest_session_for_cwd(cwd)
        if not discovered:
            return ""
        latest_thread_id, rollout_path = discovered

        try:
            rollout_size = rollout_path.stat().st_size
        except OSError as exc:
            raise CodexAppServerError(
                f"Cannot safely resume Codex thread {latest_thread_id}: "
                f"failed to stat transcript {rollout_path}"
            ) from exc
        resume_limit = int(config.codex_max_resume_bytes)
        if rollout_size > resume_limit:
            self.set_window_codex_thread_id(window_id, "")
            self.mark_window_pending_session_start_reason(
                window_id,
                "oversized_rollover",
            )
            emit_telemetry(
                "transport.app_server.oversized_resume_rollover",
                runtime_mode=config.runtime_mode,
                codex_transport=config.codex_transport,
                window_id=window_id,
                cwd=cwd,
                thread_id=latest_thread_id,
                rollout_path=str(rollout_path),
                rollout_size_bytes=rollout_size,
                resume_limit_bytes=resume_limit,
            )
            logger.warning(
                "Skipping oversized Codex resume for %s "
                "(thread=%s size=%d limit=%d); starting a fresh thread",
                window_id,
                latest_thread_id,
                rollout_size,
                resume_limit,
            )
            return ""

        try:
            resumed_thread_id = await self.resume_codex_session_for_window(
                window_id=window_id,
                cwd=cwd,
                thread_id=latest_thread_id,
            )
            # Choosing "Resume latest" is an explicit lifecycle action. The
            # exact-resume primitive intentionally repairs only the window
            # cache so implicit recovery cannot rebind topics; perform the
            # authorized topic rebind here at the explicit boundary.
            self.set_window_codex_thread_id(window_id, resumed_thread_id)
            return resumed_thread_id
        except _CodexAggregateResumeLimitError:
            self.set_window_codex_thread_id(window_id, "")
            self.mark_window_pending_session_start_reason(
                window_id,
                "oversized_rollover",
            )
            logger.warning(
                "Skipping Codex resume for %s because the shared transport "
                "history budget is full (thread=%s); starting a fresh thread",
                window_id,
                latest_thread_id,
            )
            return ""

    async def resume_codex_session_for_window(
        self,
        *,
        window_id: str,
        cwd: str,
        thread_id: str,
    ) -> str:
        """Safely resume one Codex rollout and bind it to a window."""
        normalized_thread_id = thread_id.strip()
        if not normalized_thread_id:
            raise CodexAppServerError("Codex thread id is required for resume")

        rollout_path = self._find_codex_session_file_for_thread(
            normalized_thread_id,
            cwd=cwd,
        )
        if rollout_path is None:
            raise CodexAppServerError(
                f"Cannot safely resume Codex thread {normalized_thread_id}: "
                "local transcript was not found"
            )
        try:
            rollout_size = rollout_path.stat().st_size
        except OSError as exc:
            raise CodexAppServerError(
                f"Cannot safely resume Codex thread {normalized_thread_id}: "
                f"failed to stat transcript {rollout_path}"
            ) from exc
        resume_limit = int(config.codex_max_resume_bytes)
        if rollout_size > resume_limit:
            raise CodexAppServerError(
                f"Codex transcript exceeds resume limit "
                f"({rollout_size} > {resume_limit} bytes): {normalized_thread_id}"
            )

        state = self.get_window_state(window_id)
        starting_window_thread_id = state.codex_thread_id.strip()
        known_turn_id = (
            state.codex_active_turn_id.strip()
            if state.codex_thread_id.strip() == normalized_thread_id
            else ""
        )
        known_transport_state = self.get_window_codex_transport_state(window_id)
        async with self._codex_resume_admission_lock:
            admission_transport_state = self._normalize_codex_transport_snapshot(
                codex_app_server_client.transport_state_snapshot()
            )
            if (
                admission_transport_state
                != self._codex_resume_admission_transport_state
                or not codex_app_server_client.is_running()
            ):
                self._codex_resume_admission_transport_state = (
                    admission_transport_state
                )
                self._codex_resume_bytes_by_thread.clear()
                self._codex_resume_paths_by_thread.clear()

            for admitted_thread_id, admitted_path in (
                self._codex_resume_paths_by_thread.items()
            ):
                try:
                    current_size = admitted_path.stat().st_size
                except OSError:
                    continue
                self._codex_resume_bytes_by_thread[admitted_thread_id] = max(
                    self._codex_resume_bytes_by_thread.get(admitted_thread_id, 0),
                    current_size,
                )

            previous_size = self._codex_resume_bytes_by_thread.get(
                normalized_thread_id,
                0,
            )
            admitted_rollout_size = max(previous_size, rollout_size)
            aggregate_size = (
                sum(self._codex_resume_bytes_by_thread.values())
                - previous_size
                + admitted_rollout_size
            )
            if aggregate_size > resume_limit:
                emit_telemetry(
                    "transport.app_server.aggregate_resume_rejected",
                    runtime_mode=config.runtime_mode,
                    codex_transport=config.codex_transport,
                    window_id=window_id,
                    cwd=cwd,
                    thread_id=normalized_thread_id,
                    rollout_path=str(rollout_path),
                    rollout_size_bytes=rollout_size,
                    aggregate_resume_size_bytes=aggregate_size,
                    resume_limit_bytes=resume_limit,
                )
                raise _CodexAggregateResumeLimitError(
                    "Codex transcripts exceed aggregate resume limit "
                    f"({aggregate_size} > {resume_limit} bytes): "
                    f"{normalized_thread_id}"
                )

            self._codex_resume_bytes_by_thread[normalized_thread_id] = (
                admitted_rollout_size
            )
            self._codex_resume_paths_by_thread[normalized_thread_id] = rollout_path
            resume_succeeded = False
            try:
                result = await codex_app_server_client.thread_resume(
                    thread_id=normalized_thread_id
                )
                resume_succeeded = True
            finally:
                current_admission_transport_state = (
                    self._normalize_codex_transport_snapshot(
                        codex_app_server_client.transport_state_snapshot()
                    )
                )
                if current_admission_transport_state != admission_transport_state:
                    self._codex_resume_admission_transport_state = (
                        current_admission_transport_state
                    )
                    self._codex_resume_bytes_by_thread.clear()
                    self._codex_resume_paths_by_thread.clear()
                    if resume_succeeded or not admission_transport_state[0]:
                        self._codex_resume_bytes_by_thread[
                            normalized_thread_id
                        ] = admitted_rollout_size
                        self._codex_resume_paths_by_thread[
                            normalized_thread_id
                        ] = rollout_path
        resumed_thread_id = self._extract_lifecycle_thread_id(
            result,
        )
        if not resumed_thread_id:
            raise CodexAppServerError(
                "thread/resume did not return a thread id for exact resume"
            )
        if resumed_thread_id != normalized_thread_id:
            emit_telemetry(
                "session.implicit_rebind_blocked",
                window_id=window_id,
                bound_thread_id=normalized_thread_id,
                rejected_thread_id=resumed_thread_id,
                source="thread_resume_response",
            )
            raise CodexAppServerError(
                "thread/resume returned a different thread id "
                f"({resumed_thread_id}) than requested ({normalized_thread_id})"
            )
        current_window_thread_id = state.codex_thread_id.strip()
        if (
            current_window_thread_id != starting_window_thread_id
            and current_window_thread_id != normalized_thread_id
        ):
            raise CodexAppServerError(
                "window thread binding changed while exact resume was in flight"
            )
        current_transport_state = self._normalize_codex_transport_snapshot(
            codex_app_server_client.transport_state_snapshot()
        )
        known_turn_is_current = bool(
            known_turn_id
            and state.codex_thread_id.strip() == resumed_thread_id
            and state.codex_active_turn_id.strip() == known_turn_id
            and current_transport_state[0]
            and known_transport_state == current_transport_state
        )
        resumed_turn_id = (
            self._extract_lifecycle_turn_id(result)
            or (
                codex_app_server_client.get_active_turn_id(resumed_thread_id)
                or ""
            ).strip()
            or (known_turn_id if known_turn_is_current else "")
        )
        if known_turn_id and not resumed_turn_id:
            emit_telemetry(
                "transport.app_server.resume_stale_turn_dropped",
                runtime_mode=config.runtime_mode,
                codex_transport=config.codex_transport,
                window_id=window_id,
                thread_id=resumed_thread_id,
                known_transport_epoch=known_transport_state[0],
                known_transport_generation=known_transport_state[2],
                current_transport_epoch=current_transport_state[0],
                current_transport_generation=current_transport_state[2],
            )
        self._set_window_codex_thread_cache(window_id, resumed_thread_id)
        self.set_window_codex_active_turn_id(window_id, resumed_turn_id)
        if current_transport_state[0]:
            self.set_window_codex_transport_state(
                window_id,
                epoch=current_transport_state[0],
                epoch_started_at=current_transport_state[1],
                generation=current_transport_state[2],
            )
        self.mark_window_pending_session_start_reason(window_id, "resume")
        if cwd and state.cwd != cwd:
            state.cwd = cwd
            self._save_state()
        self.sync_window_topic_model_selection_from_codex_session(
            window_id=window_id,
            codex_thread_id=resumed_thread_id,
            cwd=cwd,
        )
        logger.info(
            "Resumed Codex thread for window %s (cwd=%s): %s",
            window_id,
            cwd,
            resumed_thread_id,
        )
        return resumed_thread_id

    @staticmethod
    def _is_missing_codex_thread_error(err: Exception) -> bool:
        """Return whether an app-server exception indicates a missing thread."""
        return bool(APP_SERVER_THREAD_NOT_FOUND_RE.search(str(err)))

    @staticmethod
    def _is_turn_steer_timeout_error(err: Exception) -> bool:
        """Return whether an app-server exception is a turn/steer timeout."""
        return bool(APP_SERVER_TURN_STEER_TIMEOUT_RE.search(str(err)))

    @staticmethod
    def _is_no_active_turn_error(err: Exception) -> bool:
        """Return whether app-server rejected a stale turn/steer request."""
        return bool(APP_SERVER_NO_ACTIVE_TURN_RE.search(str(err)))

    @staticmethod
    def _is_missing_goal_error(err: Exception) -> bool:
        """Return whether app-server rejected a goal update because no goal exists."""
        return bool(APP_SERVER_NO_GOAL_EXISTS_RE.search(str(err)))

    async def _retry_send_after_missing_codex_thread(
        self,
        *,
        window_id: str,
        inputs: list[dict[str, Any]],
        window_name: str,
        cwd: str,
        steer: bool,
        stale_thread_id: str,
        model_slug: str = "",
        reasoning_effort: str = "",
        service_tier: str = "",
        ownership_validator: Callable[[], bool] | None = None,
        dispatch_state: TopicSendDispatchState | None = None,
    ) -> tuple[bool, str]:
        """Retry a missing app-server thread without changing topic ownership."""
        if ownership_validator is not None and not ownership_validator():
            return (
                False,
                "The topic's canonical Codex binding changed during recovery. "
                "The request was not sent.",
            )
        if not stale_thread_id:
            self.clear_window_codex_turn(window_id)

        logger.warning(
            "App-server thread missing for %s (%s), retrying the bound thread",
            window_id,
            self.get_display_name(window_id),
        )
        emit_telemetry(
            "transport.app_server.thread_missing_retry",
            runtime_mode=config.runtime_mode,
            codex_transport=config.codex_transport,
            window_id=window_id,
            display=self.get_display_name(window_id),
            steer=steer,
            stale_thread_id=stale_thread_id,
        )

        if stale_thread_id:
            try:
                resumed_thread_id = await self.resume_codex_session_for_window(
                    window_id=window_id,
                    cwd=cwd,
                    thread_id=stale_thread_id,
                )
                emit_telemetry(
                    "transport.app_server.thread_missing_resumed_bound",
                    runtime_mode=config.runtime_mode,
                    codex_transport=config.codex_transport,
                    window_id=window_id,
                    display=self.get_display_name(window_id),
                    stale_thread_id=stale_thread_id,
                    resumed_thread_id=resumed_thread_id,
                    cwd=cwd,
                )
            except Exception as resume_error:
                emit_telemetry(
                    "transport.app_server.thread_missing_resume_bound_failed",
                    runtime_mode=config.runtime_mode,
                    codex_transport=config.codex_transport,
                    window_id=window_id,
                    display=self.get_display_name(window_id),
                    stale_thread_id=stale_thread_id,
                    cwd=cwd,
                    error=str(resume_error),
                )
                raise
        else:
            resumed_thread_id = ""

        if resumed_thread_id and resumed_thread_id != stale_thread_id:
            emit_telemetry(
                "session.implicit_rebind_blocked",
                window_id=window_id,
                bound_thread_id=stale_thread_id,
                rejected_thread_id=resumed_thread_id,
                source="missing_thread_recovery",
            )
            raise CodexAppServerError(
                "Refused to replace the topic's bound Codex thread during recovery"
            )

        if ownership_validator is not None and not ownership_validator():
            return (
                False,
                "The topic's canonical Codex binding changed during recovery. "
                "The request was not sent.",
            )

        send_kwargs: dict[str, Any] = {}
        if model_slug:
            send_kwargs["model_slug"] = model_slug
        if reasoning_effort:
            send_kwargs["reasoning_effort"] = reasoning_effort
        if service_tier:
            send_kwargs["service_tier"] = service_tier
        if ownership_validator is not None:
            send_kwargs["ownership_validator"] = ownership_validator
        if dispatch_state is not None:
            send_kwargs["dispatch_state"] = dispatch_state
        ok, msg = await self._send_inputs_via_codex_app_server(
            window_id=window_id,
            inputs=inputs,
            steer=False,
            window_name=window_name,
            cwd=cwd,
            **send_kwargs,
        )
        if ok:
            emit_telemetry(
                "transport.app_server.thread_missing_recovered",
                runtime_mode=config.runtime_mode,
                codex_transport=config.codex_transport,
                window_id=window_id,
                display=self.get_display_name(window_id),
                stale_thread_id=stale_thread_id,
                new_thread_id=self.get_window_codex_thread_id(window_id),
            )
        else:
            emit_telemetry(
                "transport.app_server.thread_missing_recovery_failed",
                runtime_mode=config.runtime_mode,
                codex_transport=config.codex_transport,
                window_id=window_id,
                display=self.get_display_name(window_id),
                stale_thread_id=stale_thread_id,
                error=msg,
            )
        return ok, msg

    async def _retry_send_after_no_active_turn(
        self,
        *,
        window_id: str,
        inputs: list[dict[str, Any]],
        window_name: str,
        cwd: str,
        steer: bool,
        stale_turn_id: str,
        thread_id: str,
        model_slug: str = "",
        reasoning_effort: str = "",
        service_tier: str = "",
        ownership_validator: Callable[[], bool] | None = None,
        dispatch_state: TopicSendDispatchState | None = None,
    ) -> tuple[bool, str]:
        """Clear a definitively stale active turn and retry once via turn/start."""
        if ownership_validator is not None and not ownership_validator():
            return (
                False,
                "The topic's canonical Codex binding changed during recovery. "
                "The request was not sent.",
            )
        if thread_id:
            codex_app_server_client.clear_active_turn(thread_id)
        self.clear_window_codex_turn(window_id)

        logger.warning(
            "App-server rejected stale active turn for %s (%s); "
            "retrying with turn/start",
            window_id,
            self.get_display_name(window_id),
        )
        emit_telemetry(
            "transport.app_server.no_active_turn_retry",
            runtime_mode=config.runtime_mode,
            codex_transport=config.codex_transport,
            window_id=window_id,
            display=self.get_display_name(window_id),
            steer=steer,
            stale_turn_id=stale_turn_id,
            thread_id=thread_id,
        )

        send_kwargs: dict[str, Any] = {}
        if model_slug:
            send_kwargs["model_slug"] = model_slug
        if reasoning_effort:
            send_kwargs["reasoning_effort"] = reasoning_effort
        if service_tier:
            send_kwargs["service_tier"] = service_tier
        if ownership_validator is not None:
            send_kwargs["ownership_validator"] = ownership_validator
        if dispatch_state is not None:
            send_kwargs["dispatch_state"] = dispatch_state
        ok, msg = await self._send_inputs_via_codex_app_server(
            window_id=window_id,
            inputs=inputs,
            steer=False,
            window_name=window_name,
            cwd=cwd,
            **send_kwargs,
        )
        if ok:
            emit_telemetry(
                "transport.app_server.no_active_turn_recovered",
                runtime_mode=config.runtime_mode,
                codex_transport=config.codex_transport,
                window_id=window_id,
                display=self.get_display_name(window_id),
                stale_turn_id=stale_turn_id,
                thread_id=thread_id,
                new_turn_id=self.get_window_codex_active_turn_id(window_id),
            )
        else:
            emit_telemetry(
                "transport.app_server.no_active_turn_recovery_failed",
                runtime_mode=config.runtime_mode,
                codex_transport=config.codex_transport,
                window_id=window_id,
                display=self.get_display_name(window_id),
                stale_turn_id=stale_turn_id,
                thread_id=thread_id,
                error=msg,
            )
        return ok, msg

    async def _send_inputs_via_codex_app_server(
        self,
        *,
        window_id: str,
        inputs: list[dict[str, Any]],
        steer: bool,
        force_new_turn: bool = False,
        window_name: str,
        cwd: str,
        model_slug: str = "",
        reasoning_effort: str = "",
        service_tier: str = "",
        ownership_validator: Callable[[], bool] | None = None,
        dispatch_state: TopicSendDispatchState | None = None,
    ) -> tuple[bool, str]:
        if not model_slug and not reasoning_effort:
            model_slug, reasoning_effort = self.get_window_topic_model_selection(window_id)
        if not service_tier:
            service_tier = self.get_window_topic_service_tier_selection(window_id)
        had_thread_before = bool(self.get_window_codex_thread_id(window_id))
        pending_session_start_reason = self.peek_window_pending_session_start_reason(
            window_id
        )
        ensure_kwargs: dict[str, str] = {}
        if model_slug:
            ensure_kwargs["model"] = model_slug
        if reasoning_effort:
            ensure_kwargs["effort"] = reasoning_effort
        if service_tier:
            ensure_kwargs["service_tier"] = service_tier
        thread_id, approval_policy = await self._ensure_codex_thread_for_window(
            window_id=window_id,
            cwd=cwd,
            sync_topic_bindings=ownership_validator is None,
            ownership_validator=ownership_validator,
            **ensure_kwargs,
        )
        if ownership_validator is not None and not ownership_validator():
            return (
                False,
                "The topic's canonical Codex binding changed before turn dispatch. "
                "The request was not sent.",
            )
        workspace_path, can_write = self._runtime_write_state(cwd)
        session_start_reason = pending_session_start_reason
        if not session_start_reason and not had_thread_before and thread_id:
            session_start_reason = "fresh_start"
        transcription_runtime = get_transcription_runtime_summary("compatible")
        tts_runtime = get_tts_runtime_summary()
        runtime_hint = self._build_runtime_capability_hint(
            workspace_path=workspace_path,
            can_write=can_write,
            approval_policy=approval_policy,
            session_start_reason=session_start_reason,
            tts_available=bool(tts_runtime.get("available")),
            tts_default_voice=str(tts_runtime.get("default_voice", "")).strip(),
            tts_default_speed=float(tts_runtime.get("default_speed", 1.0) or 1.0),
            transcription_runtime_label=(
                f"{transcription_runtime['mode']} -> "
                f"{transcription_runtime['device']} / "
                f"{transcription_runtime['compute_type']} / "
                f"{transcription_runtime['model_name']}"
            ),
        )
        normalized_inputs = self._normalize_app_server_inputs(inputs)
        turn_inputs = [{"type": "text", "text": runtime_hint}, *normalized_inputs]
        logger.info(
            "App-server turn payload prepared (window=%s thread=%s items=%d user_items=%d)",
            window_id,
            thread_id,
            len(turn_inputs),
            len(normalized_inputs),
        )
        state = self.get_window_state(window_id)
        active_turn = (
            state.codex_active_turn_id.strip()
            or codex_app_server_client.get_active_turn_id(thread_id)
            or ""
        )
        if force_new_turn:
            active_turn = ""

        on_dispatch = (
            dispatch_state.mark_transport_dispatch_started
            if dispatch_state is not None
            else None
        )

        if steer and not active_turn:
            logger.info(
                "No active app-server turn for %s (%s); starting a new turn instead of steering",
                window_id,
                self.get_display_name(window_id),
            )
            steer = False

        if steer or active_turn:
            dispatch_kwargs = (
                {"on_dispatch": on_dispatch} if on_dispatch is not None else {}
            )
            try:
                result = await codex_app_server_client.turn_steer(
                    thread_id=thread_id,
                    expected_turn_id=active_turn,
                    inputs=turn_inputs,
                    **dispatch_kwargs,
                )
            except CodexAppServerError as error:
                if self._is_turn_steer_timeout_error(error):
                    raise _CodexTurnTimeoutError(
                        error,
                        method="turn/steer",
                        thread_id=thread_id,
                        turn_id=active_turn,
                    ) from error
                raise
            if ownership_validator is not None and not ownership_validator():
                return (
                    False,
                    "The topic's canonical Codex binding changed while turn/steer "
                    "was in flight; the outcome is uncertain and the request will "
                    "not be replayed automatically.",
                )
            new_turn_id = result.get("turnId") if isinstance(result, dict) else None
            state.codex_active_turn_id = (
                new_turn_id
                if isinstance(new_turn_id, str) and new_turn_id
                else active_turn
            )
        else:
            turn_start_kwargs: dict[str, Any] = {}
            if model_slug:
                turn_start_kwargs["model_slug"] = model_slug
            if reasoning_effort:
                turn_start_kwargs["reasoning_effort"] = reasoning_effort
            if on_dispatch is not None:
                turn_start_kwargs["on_dispatch"] = on_dispatch
            try:
                result = await self._turn_start_with_retry(
                    thread_id=thread_id,
                    inputs=turn_inputs,
                    approval_policy=approval_policy,
                    service_tier=service_tier,
                    **turn_start_kwargs,
                )
            except CodexAppServerError as error:
                if self._is_turn_start_timeout(error):
                    raise _CodexTurnTimeoutError(
                        error,
                        method="turn/start",
                        thread_id=thread_id,
                    ) from error
                raise
            if ownership_validator is not None and not ownership_validator():
                return (
                    False,
                    "The topic's canonical Codex binding changed while turn/start "
                    "was in flight; the outcome is uncertain and the request will "
                    "not be replayed automatically.",
                )
            turn = result.get("turn") if isinstance(result, dict) else None
            turn_id = turn.get("id") if isinstance(turn, dict) else None
            state.codex_active_turn_id = turn_id if isinstance(turn_id, str) else ""

        if cwd and state.cwd != cwd:
            state.cwd = cwd
        if window_name and state.window_name != window_name:
            state.window_name = window_name
        if pending_session_start_reason:
            self.consume_window_pending_session_start_reason(window_id)
        self._save_state()
        self.note_window_input(window_id, window_name=window_name, cwd=cwd)
        expected_transcript_text = self._build_expected_transcript_user_text(turn_inputs)
        if expected_transcript_text:
            self.register_expected_transcript_user_echo(
                window_id,
                expected_transcript_text,
            )
        return True, f"Sent via app-server to {self.get_display_name(window_id)}"

    async def send_inputs_to_window(
        self,
        window_id: str,
        inputs: list[dict[str, Any]],
        *,
        steer: bool = False,
        force_new_turn: bool = False,
        model_slug: str = "",
        reasoning_effort: str = "",
        service_tier: str = "",
        remote_thread_id: str | None = None,
        remote_cwd: str = "",
        remote_window_name: str = "",
        remote_approval_mode: str = "",
        result_snapshot: dict[str, str] | None = None,
        dispatch_cwd: str | None = None,
        ownership_validator: Callable[[], bool] | None = None,
        dispatch_state: TopicSendDispatchState | None = None,
    ) -> tuple[bool, str]:
        """Send structured user inputs to a window via Codex app-server."""
        display = self.get_display_name(window_id)
        lock_wait_started = time.monotonic()
        async with self._window_send_context(
            window_id,
            remote_thread_id=remote_thread_id,
            remote_cwd=remote_cwd,
            remote_window_name=remote_window_name,
            remote_approval_mode=remote_approval_mode,
            result_snapshot=result_snapshot,
        ):
            lock_wait_elapsed = time.monotonic() - lock_wait_started
            if lock_wait_elapsed >= 0.01:
                logger.debug(
                    "Send lock wait: window_id=%s (%s) waited=%.3fs steer=%s",
                    window_id,
                    display,
                    lock_wait_elapsed,
                    steer,
                )

            codex_app_server_mode = self._codex_app_server_mode_enabled()
            fallback_state = self.get_window_state(window_id)
            window_name = fallback_state.window_name or display
            cwd = (
                fallback_state.cwd
                if dispatch_cwd is None
                else dispatch_cwd.strip()
            )
            initial_topic_ownership = self._window_topic_ownership_snapshot(window_id)

            def _send_ownership_is_current() -> bool:
                if (
                    initial_topic_ownership
                    and self._window_topic_ownership_snapshot(window_id)
                    != initial_topic_ownership
                ):
                    return False
                return ownership_validator is None or ownership_validator()

            if codex_app_server_mode:
                if not cwd:
                    return False, "No workspace bound to this topic. Run /start first."
                if not _send_ownership_is_current():
                    return (
                        False,
                        "The topic's canonical Codex binding changed before "
                        "dispatch. The request was not sent.",
                    )
                try:
                    send_kwargs: dict[str, Any] = {}
                    if model_slug:
                        send_kwargs["model_slug"] = model_slug
                    if reasoning_effort:
                        send_kwargs["reasoning_effort"] = reasoning_effort
                    if service_tier:
                        send_kwargs["service_tier"] = service_tier
                    dispatched_thread_id = fallback_state.codex_thread_id.strip()
                    dispatched_turn_id = fallback_state.codex_active_turn_id.strip()
                    strict_ownership_validator = (
                        _send_ownership_is_current
                        if initial_topic_ownership
                        or ownership_validator is not None
                        else None
                    )
                    initial_send_kwargs = dict(send_kwargs)
                    if strict_ownership_validator is not None:
                        initial_send_kwargs["ownership_validator"] = (
                            strict_ownership_validator
                        )
                    if dispatch_state is not None:
                        initial_send_kwargs["dispatch_state"] = dispatch_state
                    send_result = await self._send_inputs_via_codex_app_server(
                        window_id=window_id,
                        inputs=inputs,
                        steer=steer,
                        force_new_turn=force_new_turn,
                        window_name=window_name,
                        cwd=cwd,
                        **initial_send_kwargs,
                    )
                    if not _send_ownership_is_current():
                        return (
                            False,
                            "The topic's canonical Codex binding changed while the "
                            "request was in flight; the outcome is uncertain and "
                            "the request will not be replayed automatically.",
                        )
                    return send_result
                except Exception as e:
                    stale_thread_id = dispatched_thread_id
                    stale_turn_id = dispatched_turn_id
                    error_text = str(e)
                    if not _send_ownership_is_current():
                        return (
                            False,
                            "The topic's canonical Codex binding changed while the "
                            "request was in flight. Recovery was not attempted and "
                            "the request will not be replayed automatically.",
                        )
                    turn_timeout_method = (
                        "turn/start"
                        if self._is_turn_start_timeout(e)
                        else (
                            "turn/steer"
                            if self._is_turn_steer_timeout_error(e)
                            else ""
                        )
                    )
                    timeout_thread_id = stale_thread_id
                    timeout_turn_id = stale_turn_id
                    if isinstance(e, _CodexTurnTimeoutError):
                        timeout_thread_id = e.thread_id
                        timeout_turn_id = e.turn_id
                    if self._is_missing_codex_thread_error(e):
                        try:
                            return await self._retry_send_after_missing_codex_thread(
                                window_id=window_id,
                                inputs=inputs,
                                window_name=window_name,
                                cwd=cwd,
                                steer=steer,
                                stale_thread_id=stale_thread_id,
                                ownership_validator=_send_ownership_is_current,
                                dispatch_state=dispatch_state,
                                **send_kwargs,
                            )
                        except Exception as retry_error:
                            emit_telemetry(
                                "transport.app_server.thread_missing_recovery_failed",
                                runtime_mode=config.runtime_mode,
                                codex_transport=config.codex_transport,
                                window_id=window_id,
                                display=display,
                                steer=steer,
                                stale_thread_id=stale_thread_id,
                                error=str(retry_error),
                            )
                            error_text = (
                                f"{error_text}; retry with new thread failed: {retry_error}"
                            )
                            retry_timeout_method = (
                                retry_error.method
                                if isinstance(retry_error, _CodexTurnTimeoutError)
                                else (
                                    "turn/start"
                                    if self._is_turn_start_timeout(retry_error)
                                    else (
                                        "turn/steer"
                                        if self._is_turn_steer_timeout_error(
                                            retry_error
                                        )
                                        else ""
                                    )
                                )
                            )
                            if retry_timeout_method:
                                turn_timeout_method = retry_timeout_method
                                if isinstance(
                                    retry_error,
                                    _CodexTurnTimeoutError,
                                ):
                                    timeout_thread_id = retry_error.thread_id
                                    timeout_turn_id = retry_error.turn_id
                                else:
                                    timeout_thread_id = (
                                        fallback_state.codex_thread_id.strip()
                                    )
                                    timeout_turn_id = (
                                        fallback_state.codex_active_turn_id.strip()
                                    )
                    elif self._is_no_active_turn_error(e):
                        try:
                            return await self._retry_send_after_no_active_turn(
                                window_id=window_id,
                                inputs=inputs,
                                window_name=window_name,
                                cwd=cwd,
                                steer=steer,
                                stale_turn_id=stale_turn_id,
                                thread_id=stale_thread_id,
                                ownership_validator=_send_ownership_is_current,
                                dispatch_state=dispatch_state,
                                **send_kwargs,
                            )
                        except Exception as retry_error:
                            emit_telemetry(
                                "transport.app_server.no_active_turn_recovery_failed",
                                runtime_mode=config.runtime_mode,
                                codex_transport=config.codex_transport,
                                window_id=window_id,
                                display=display,
                                steer=steer,
                                stale_turn_id=stale_turn_id,
                                thread_id=stale_thread_id,
                                error=str(retry_error),
                            )
                            error_text = (
                                f"{error_text}; retry with turn/start failed: {retry_error}"
                            )
                            retry_timeout_method = (
                                retry_error.method
                                if isinstance(retry_error, _CodexTurnTimeoutError)
                                else (
                                    "turn/start"
                                    if self._is_turn_start_timeout(retry_error)
                                    else (
                                        "turn/steer"
                                        if self._is_turn_steer_timeout_error(
                                            retry_error
                                        )
                                        else ""
                                    )
                                )
                            )
                            if retry_timeout_method:
                                turn_timeout_method = retry_timeout_method
                                if isinstance(
                                    retry_error,
                                    _CodexTurnTimeoutError,
                                ):
                                    timeout_thread_id = retry_error.thread_id
                                    timeout_turn_id = retry_error.turn_id
                                else:
                                    timeout_thread_id = (
                                        fallback_state.codex_thread_id.strip()
                                    )
                                    timeout_turn_id = (
                                        fallback_state.codex_active_turn_id.strip()
                                    )
                    if turn_timeout_method:
                        if turn_timeout_method == "turn/steer":
                            emit_telemetry(
                                "transport.app_server.steer_timeout_uncertain",
                                runtime_mode=config.runtime_mode,
                                codex_transport=config.codex_transport,
                                window_id=window_id,
                                display=display,
                                steer=steer,
                                stale_turn_id=timeout_turn_id,
                                thread_id=timeout_thread_id,
                            )
                        recovery_error = ""
                        try:
                            recover_timeout = (
                                codex_app_server_client.recover_uncertain_turn_timeout
                            )
                            recovery_kwargs = {"method": turn_timeout_method}
                            if turn_timeout_method == "turn/steer":
                                recovery_kwargs.update(
                                    {
                                        "thread_id": timeout_thread_id,
                                        "turn_id": timeout_turn_id,
                                    }
                                )
                            transport_recovered = await recover_timeout(**recovery_kwargs)
                        except Exception as recovery_exception:
                            transport_recovered = False
                            recovery_error = str(recovery_exception)
                            logger.exception(
                                "Failed recovering app-server after uncertain %s "
                                "timeout",
                                turn_timeout_method,
                            )
                        if (
                            fallback_state.codex_thread_id.strip()
                            == timeout_thread_id
                        ):
                            self.set_window_codex_active_turn_id(
                                window_id,
                                codex_app_server_client.get_active_turn_id(
                                    timeout_thread_id
                                )
                                or "",
                            )
                        recovery_event = (
                            "transport.app_server.uncertain_turn_timeout_recovered"
                            if transport_recovered
                            else "transport.app_server.uncertain_turn_timeout_recovery_failed"
                        )
                        emit_telemetry(
                            recovery_event,
                            runtime_mode=config.runtime_mode,
                            codex_transport=config.codex_transport,
                            window_id=window_id,
                            display=display,
                            steer=steer,
                            method=turn_timeout_method,
                            stale_turn_id=timeout_turn_id,
                            thread_id=timeout_thread_id,
                            error=recovery_error,
                        )
                        recovery_status = (
                            "transport recovered"
                            if transport_recovered
                            else "transport recovery failed"
                        )
                        error_text = (
                            f"{error_text}; {recovery_status}; the uncertain "
                            "request was not replayed"
                        )
                    logger.warning(
                        "App-server send failed for %s (%s): %s",
                        window_id,
                        display,
                        error_text,
                    )
                    fallback_allowed = False
                    emit_telemetry(
                        "transport.app_server.send_failed",
                        runtime_mode=config.runtime_mode,
                        codex_transport=config.codex_transport,
                        window_id=window_id,
                        display=display,
                        steer=steer,
                        fallback_allowed=fallback_allowed,
                        error=error_text,
                    )
                    if not fallback_allowed:
                        return False, f"App-server send failed: {error_text}"

            return False, "Codex app-server transport is unavailable."

    async def send_to_window(
        self,
        window_id: str,
        text: str,
        *,
        steer: bool = False,
        force_new_turn: bool = False,
        model_slug: str = "",
        reasoning_effort: str = "",
        service_tier: str = "",
        dispatch_cwd: str | None = None,
        ownership_validator: Callable[[], bool] | None = None,
        dispatch_state: TopicSendDispatchState | None = None,
    ) -> tuple[bool, str]:
        """Send plain text input to a window.

        When Codex app-server transport is enabled, text is sent via turn APIs.
        """
        display = self.get_display_name(window_id)
        logger.debug(
            "send_to_window: window_id=%s (%s), text_len=%d, steer=%s",
            window_id,
            display,
            len(text),
            steer,
        )
        payload = [{"type": "text", "text": text}]
        return await self.send_inputs_to_window(
            window_id,
            payload,
            steer=steer,
            force_new_turn=force_new_turn,
            model_slug=model_slug,
            reasoning_effort=reasoning_effort,
            service_tier=service_tier,
            dispatch_cwd=dispatch_cwd,
            ownership_validator=ownership_validator,
            dispatch_state=dispatch_state,
        )

    # --- Message history ---

    async def get_recent_messages(
        self,
        window_id: str,
        *,
        start_byte: int = 0,
        end_byte: int | None = None,
    ) -> tuple[list[dict], int]:
        """Get user/assistant messages for a window's session.

        Resolves window → session, then reads the JSONL.
        Supports byte range filtering via start_byte/end_byte.
        Returns (messages, total_count).
        """
        session = await self.resolve_session_for_window(window_id)
        if not session or not session.file_path:
            return [], 0

        file_path = Path(session.file_path)
        if not file_path.exists():
            return [], 0

        # Read JSONL entries (optionally filtered by byte range)
        entries: list[dict] = []
        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                if start_byte > 0:
                    await f.seek(start_byte)

                while True:
                    # Check byte limit before reading
                    if end_byte is not None:
                        current_pos = await f.tell()
                        if current_pos >= end_byte:
                            break

                    line = await f.readline()
                    if not line:
                        break

                    data = TranscriptParser.parse_line(line)
                    if data:
                        entries.append(data)
        except OSError as e:
            logger.error("Error reading session file %s: %s", file_path, e)
            return [], 0

        parsed_entries, _ = TranscriptParser.parse_entries(entries)
        all_messages = [
            {
                "role": e.role,
                "text": e.text,
                "content_type": e.content_type,
                "timestamp": e.timestamp,
            }
            for e in parsed_entries
        ]

        return all_messages, len(all_messages)


session_manager = SessionManager()
