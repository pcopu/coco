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
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Iterator
from typing import Any

import aiofiles

from .codex_app_server import CodexAppServerError, codex_app_server_client
from .config import config
from .node_registry import node_registry
from .skills import SkillDefinition, discover_skills, resolve_skill_identifier
from .telemetry import emit_telemetry
from .transcript_parser import TranscriptParser
from .utils import atomic_write_json

logger = logging.getLogger(__name__)

APP_SERVER_MAX_TEXT_CHARS_PER_INPUT = 3000
APP_SERVER_TURN_START_TIMEOUT_SECONDS = 75.0
APP_SERVER_TURN_START_MAX_ATTEMPTS = 2
APP_SERVER_TURN_START_RETRY_DELAY_SECONDS = 0.4
APP_SERVER_THREAD_NOT_FOUND_RE = re.compile(r"\bthread not found\b", re.IGNORECASE)
APP_SERVER_TURN_STEER_TIMEOUT_RE = re.compile(
    r"Timed out waiting for app-server response:\s*turn/steer",
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
    """

    session_id: str = ""
    cwd: str = ""
    window_name: str = ""
    last_input_ts: float = 0.0
    approval_mode: str = ""
    mention_only: bool = False
    codex_thread_id: str = ""
    codex_active_turn_id: str = ""

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
        return cls(
            session_id=data.get("session_id", ""),
            cwd=data.get("cwd", ""),
            window_name=data.get("window_name", ""),
            last_input_ts=last_input_ts,
            approval_mode=data.get("approval_mode", ""),
            mention_only=mention_only,
            codex_thread_id=data.get("codex_thread_id", ""),
            codex_active_turn_id=data.get("codex_active_turn_id", ""),
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
    service_tier: str = ""

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
        if self.service_tier:
            d["service_tier"] = self.service_tier
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TopicBinding":
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
        window_id = str(data.get("window_id", "")).strip()
        codex_thread_id = str(data.get("codex_thread_id", "")).strip()
        cwd = str(data.get("cwd", "")).strip()
        display_name = str(data.get("display_name", "")).strip()
        raw_sync_mode = data.get("sync_mode", TOPIC_SYNC_MODE_TELEGRAM_LIVE)
        sync_mode = SessionManager._normalize_topic_sync_mode(raw_sync_mode)
        machine_id = str(data.get("machine_id", "")).strip()
        machine_display_name = str(data.get("machine_display_name", "")).strip()
        model_slug = str(data.get("model_slug", "")).strip()
        reasoning_effort = str(data.get("reasoning_effort", "")).strip()
        raw_service_tier = data.get("service_tier", "")
        service_tier = (
            str(raw_service_tier).strip().lower()
            if isinstance(raw_service_tier, str)
            else ""
        )
        if service_tier not in CODEX_SERVICE_TIERS:
            service_tier = ""
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
            service_tier=service_tier,
        )


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

    def __post_init__(self) -> None:
        self._load_state()

    def _get_window_send_lock(self, window_id: str) -> asyncio.Lock:
        """Get or create a per-window lock for send/steer operations."""
        lock = self._window_send_locks.get(window_id)
        if lock is None:
            lock = asyncio.Lock()
            self._window_send_locks[window_id] = lock
        return lock

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
                    service_tier=binding.service_tier,
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
                raw_schema_version = state.get("state_schema_version", 1)
                try:
                    self.state_schema_version = int(raw_schema_version)
                except (TypeError, ValueError):
                    self.state_schema_version = 1
                if self.state_schema_version < 1:
                    self.state_schema_version = 1
                self.window_states = {
                    k: WindowState.from_dict(v)
                    for k, v in state.get("window_states", {}).items()
                }
                self.user_window_offsets = {
                    int(uid): offsets
                    for uid, offsets in state.get("user_window_offsets", {}).items()
                }
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
                self.window_display_names = state.get("window_display_names", {})
                self.group_chat_ids = {
                    k: int(v) for k, v in state.get("group_chat_ids", {}).items()
                }
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

            except (json.JSONDecodeError, ValueError) as e:
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
        bound_window_ids = {
            window_id for _, _, _, window_id in self.iter_topic_window_bindings()
        }
        return {
            window_id: state.session_id
            for window_id, state in self.window_states.items()
            if self._is_window_id(window_id)
            and state.session_id
            and window_id in bound_window_ids
        }

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
        )[:limit]
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
        changed = self._sync_topic_bindings_for_window_model_selection(
            window_id=window_id,
            model_slug=model_slug,
            reasoning_effort=reasoning_effort,
        )
        if changed:
            self._save_state()
        return changed, model_slug, reasoning_effort

    def _codex_cwd_matches(self, target_cwd: str, file_cwd: str) -> bool:
        """Return True when the Codex transcript cwd exactly matches window cwd."""
        return file_cwd == target_cwd

    def _find_latest_session_for_cwd(
        self, cwd: str, *, prefer_recent_since: float = 0.0
    ) -> tuple[str, Path] | None:
        """Find the most recent session transcript that matches cwd.

        For Codex, when ``prefer_recent_since`` is set, only sessions updated
        after that timestamp are considered. This avoids binding to stale
        transcripts when a new window is created in a directory with old history.
        """
        try:
            target_cwd = str(Path(cwd).resolve())
        except (OSError, ValueError):
            target_cwd = cwd

        # Codex sessions are sharded by date under ~/.codex/sessions/YYYY/MM/...
        matching = [
            (summary.last_active_at, summary.thread_id, summary.file_path)
            for summary in self.list_codex_session_summaries_for_cwd(cwd)
        ]

        if not matching:
            return None

        if prefer_recent_since > 0:
            cutoff = prefer_recent_since - 2.0
            recent = [item for item in matching if item[0] >= cutoff]
            if not recent:
                return None
            _mtime, sid, path = max(recent, key=lambda item: item[0])
            return sid, path

        _mtime, sid, path = max(matching, key=lambda item: item[0])
        return sid, path

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
            extracted = self._extract_codex_session_summary(file_path)
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

    async def autodiscover_session_for_window(self, window_id: str) -> bool:
        """Auto-associate transcript session metadata for one window binding."""
        state = self.get_window_state(window_id)
        cwd = state.cwd.strip()
        if not cwd:
            return False

        if not state.session_id and state.last_input_ts <= 0:
            return False

        discovered = self._find_latest_session_for_cwd(
            cwd,
            prefer_recent_since=state.last_input_ts,
        )
        if not discovered:
            return False

        session_id, _ = discovered
        changed = False

        if state.session_id != session_id:
            state.session_id = session_id
            changed = True
        if changed:
            self._save_state()
            logger.info(
                "Auto-associated window %s -> session %s",
                window_id,
                session_id,
            )
        return True

    async def autodiscover_sessions_for_bound_windows(self) -> None:
        """Auto-associate sessions for all currently bound windows."""
        bound_window_ids = {
            window_id for _, _, _, window_id in self.iter_topic_window_bindings()
        }
        for window_id in bound_window_ids:
            try:
                await self.autodiscover_session_for_window(window_id)
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
        self._save_state()
        logger.info("Cleared session for window_id %s", window_id)

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
        """Keep topic bindings in sync when a window inherits a session model."""
        normalized_model = model_slug.strip()
        normalized_effort = reasoning_effort.strip()
        changed = False
        for bindings in self.topic_bindings_v2.values():
            for binding in bindings.values():
                if binding.window_id.strip() != window_id:
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
        file_path = self._build_session_file_path(session_id, cwd)

        # Fallback: glob search if direct path doesn't exist
        if not file_path or not file_path.exists():
            matches = list(config.sessions_path.glob(f"**/*-{session_id}.jsonl"))
            if matches:
                file_path = matches[0]
                logger.debug("Found session via glob: %s", file_path)
            else:
                return None

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

    async def send_topic_text_to_window(
        self,
        *,
        user_id: int,
        thread_id: int | None,
        chat_id: int | None = None,
        window_id: str,
        text: str,
        steer: bool = False,
    ) -> tuple[bool, str]:
        """Send user/topic text with app/skill context applied."""
        machine_id = self.get_window_machine_id(window_id)
        local_machine_id, _local_machine_name = self._local_machine_identity()
        if (
            thread_id is not None
            and self._codex_app_server_mode_enabled()
            and self.get_topic_sync_mode(user_id, thread_id, chat_id=chat_id)
            == TOPIC_SYNC_MODE_HOST_FOLLOW_FINAL
        ):
            state = self.get_window_state(window_id)
            cwd = state.cwd.strip()
            if not cwd:
                return False, "No workspace bound to this topic. Run /folder first."
            if machine_id and machine_id != local_machine_id:
                from .agent_rpc import agent_rpc_client

                resume_result = await agent_rpc_client.resume_latest(
                    machine_id,
                    window_id=window_id,
                    cwd=cwd,
                    window_name=state.window_name or self.get_display_name(window_id),
                    approval_mode=state.approval_mode.strip(),
                )
                resumed_thread_id = str(resume_result.get("thread_id", "")).strip()
                if resumed_thread_id:
                    self.set_window_codex_thread_id(window_id, resumed_thread_id)
                    self.set_window_codex_active_turn_id(
                        window_id,
                        str(resume_result.get("turn_id", "")).strip(),
                    )
            else:
                resumed_thread_id = await self.resume_latest_codex_session_for_window(
                    window_id=window_id,
                    cwd=cwd,
                )
            if not resumed_thread_id:
                return False, "Failed to resume the latest Codex session for this folder."

        apps = self.resolve_thread_skills(user_id, thread_id, chat_id=chat_id)
        codex_skills = self.resolve_thread_codex_skills(
            user_id, thread_id, chat_id=chat_id
        )
        if not apps and not codex_skills:
            if machine_id and machine_id != local_machine_id:
                from .agent_rpc import agent_rpc_client

                state = self.get_window_state(window_id)
                cwd = state.cwd.strip()
                if not cwd:
                    return False, "No workspace bound to this topic. Run /folder first."
                node = node_registry.get_node(machine_id)
                if node is not None and node.status == "offline":
                    return False, f"Machine offline: {node.display_name}"
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
                remote_result = await agent_rpc_client.send_inputs(
                    machine_id,
                    window_id=window_id,
                    cwd=cwd,
                    window_name=state.window_name or self.get_display_name(window_id),
                    inputs=[{"type": "text", "text": text}],
                    steer=steer,
                    thread_id=state.codex_thread_id.strip(),
                    approval_mode=state.approval_mode.strip(),
                    model_slug=model_slug,
                    reasoning_effort=reasoning_effort,
                    service_tier=service_tier,
                )
                resolved_thread_id = str(remote_result.get("thread_id", "")).strip()
                resolved_turn_id = str(remote_result.get("turn_id", "")).strip()
                if resolved_thread_id:
                    self.set_window_codex_thread_id(window_id, resolved_thread_id)
                if resolved_turn_id or self.get_window_codex_active_turn_id(window_id):
                    self.set_window_codex_active_turn_id(window_id, resolved_turn_id)
                ok = bool(remote_result.get("ok", False))
                msg = str(remote_result.get("message", "")).strip() or "Remote send complete."
            else:
                ok, msg = await self.send_to_window(window_id, text, steer=steer)
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
            if apps:
                app_context = self._inject_skill_context(
                    "",
                    apps=apps,
                    codex_skills=[],
                ).strip()
                inputs.insert(0, {"type": "text", "text": app_context})
            inputs.append({"type": "text", "text": text})
            if machine_id and machine_id != local_machine_id:
                from .agent_rpc import agent_rpc_client

                state = self.get_window_state(window_id)
                cwd = state.cwd.strip()
                if not cwd:
                    return False, "No workspace bound to this topic. Run /folder first."
                node = node_registry.get_node(machine_id)
                if node is not None and node.status == "offline":
                    return False, f"Machine offline: {node.display_name}"
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
                remote_result = await agent_rpc_client.send_inputs(
                    machine_id,
                    window_id=window_id,
                    cwd=cwd,
                    window_name=state.window_name or self.get_display_name(window_id),
                    inputs=inputs,
                    steer=steer,
                    thread_id=state.codex_thread_id.strip(),
                    approval_mode=state.approval_mode.strip(),
                    model_slug=model_slug,
                    reasoning_effort=reasoning_effort,
                    service_tier=service_tier,
                )
                resolved_thread_id = str(remote_result.get("thread_id", "")).strip()
                resolved_turn_id = str(remote_result.get("turn_id", "")).strip()
                if resolved_thread_id:
                    self.set_window_codex_thread_id(window_id, resolved_thread_id)
                if resolved_turn_id or self.get_window_codex_active_turn_id(window_id):
                    self.set_window_codex_active_turn_id(window_id, resolved_turn_id)
                ok = bool(remote_result.get("ok", False))
                msg = str(remote_result.get("message", "")).strip() or "Remote send complete."
            else:
                ok, msg = await self.send_inputs_to_window(window_id, inputs, steer=steer)
            if ok:
                self.mark_topic_telegram_live(
                    user_id=user_id,
                    thread_id=thread_id,
                    window_id=window_id,
                    chat_id=chat_id,
                )
            return ok, msg

        injected = self._inject_skill_context(
            text,
            apps=apps,
            codex_skills=codex_skills,
        )
        ok, msg = await self.send_to_window(window_id, injected, steer=steer)
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
        ):
            return False
        binding.model_slug = normalized_model
        binding.reasoning_effort = normalized_effort
        self._save_state()
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
        resolved_service_tier = existing.service_tier if existing else ""
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
            service_tier=resolved_service_tier,
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
            service_tier=binding.service_tier,
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
            if state.session_id == session_id:
                result.append((user_id, chat_id, window_id, thread_id))
                continue

            # Known non-empty session IDs are authoritative enough to skip
            # expensive transcript re-resolution on every streamed chunk.
            if state.session_id:
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
            resolved_codex_thread_id = binding.codex_thread_id
            window_id = binding.window_id.strip()
            if not resolved_codex_thread_id and window_id:
                resolved_codex_thread_id = self.get_window_state(window_id).codex_thread_id
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

    def set_codex_turn_for_thread(self, codex_thread_id: str, turn_id: str) -> None:
        """Update active Codex turn id across all windows bound to a thread id."""
        if not codex_thread_id:
            return
        changed = False
        normalized = turn_id.strip()
        for state in self.window_states.values():
            if state.codex_thread_id != codex_thread_id:
                continue
            if state.codex_active_turn_id == normalized:
                continue
            state.codex_active_turn_id = normalized
            changed = True
        if changed:
            self._save_state()

    async def validate_codex_topic_bindings(self) -> dict[str, int]:
        """Validate persisted Codex thread bindings and clear stale thread ids."""
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
                payload = await codex_app_server_client.thread_read(
                    thread_id=codex_thread_id
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

        repaired = 0
        changed = False
        for _user_id, bindings in self.topic_bindings_v2.items():
            for _thread_id, binding in bindings.items():
                codex_thread_id = binding.codex_thread_id.strip()
                if not codex_thread_id or codex_thread_id not in invalid_thread_ids:
                    continue
                binding.codex_thread_id = ""
                repaired += 1
                changed = True
                window_id = binding.window_id.strip()
                if not window_id:
                    continue
                state = self.get_window_state(window_id)
                if state.codex_thread_id == codex_thread_id:
                    state.codex_thread_id = ""
                    state.codex_active_turn_id = ""

        for state in self.window_states.values():
            codex_thread_id = state.codex_thread_id.strip()
            if codex_thread_id and codex_thread_id in invalid_thread_ids:
                state.codex_thread_id = ""
                state.codex_active_turn_id = ""
                changed = True

        if changed:
            self._save_state()

        return {
            "checked": checked,
            "invalid": len(invalid_thread_ids),
            "repaired": repaired,
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
    ) -> str:
        """Build one short runtime context note to avoid stale read-only assumptions."""
        write_state = "enabled" if can_write else "disabled"
        return (
            "[coco runtime context]\n"
            f"Workspace: {workspace_path}\n"
            f"Filesystem write access: {write_state}\n"
            f"Approval policy: {approval_policy}\n"
            "Telegram attachments: to upload a workspace file for the user, "
            'append a standalone line exactly like '
            '<telegram-attachment path="relative/path.pdf" /> '
            "after your normal answer. Supported types: .pdf, .txt, .md, "
            ".png, .jpg, .jpeg, .webp, .gif, .bmp, .tif, .tiff. "
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
        service_tier: str = "",
    ) -> dict[str, Any]:
        """Start a turn with one guarded retry for transient timeout cases."""
        attempts = APP_SERVER_TURN_START_MAX_ATTEMPTS
        last_err: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return await codex_app_server_client.turn_start(
                    thread_id=thread_id,
                    inputs=inputs,
                    approval_policy=approval_policy,
                    service_tier=service_tier.strip() or None,
                    timeout=APP_SERVER_TURN_START_TIMEOUT_SECONDS,
                )
            except Exception as e:
                last_err = e
                if not self._is_turn_start_timeout(e) or attempt >= attempts:
                    raise
                # If the server started a turn but the response frame was delayed/lost,
                # use tracked active turn and avoid duplicate turn/start submission.
                existing_turn = codex_app_server_client.get_active_turn_id(thread_id)
                if existing_turn:
                    logger.warning(
                        "turn/start timed out but active turn already exists (thread=%s turn=%s); treating as success",
                        thread_id,
                        existing_turn,
                    )
                    return {"turn": {"id": existing_turn}}
                logger.warning(
                    "turn/start timeout (thread=%s attempt=%d/%d), retrying once",
                    thread_id,
                    attempt,
                    attempts,
                )
                await asyncio.sleep(APP_SERVER_TURN_START_RETRY_DELAY_SECONDS)
        if last_err:
            raise last_err
        raise CodexAppServerError("turn/start failed without an explicit error")

    async def _ensure_codex_thread_for_window(
        self,
        *,
        window_id: str,
        cwd: str,
        model: str = "",
        effort: str = "",
        service_tier: str = "",
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

        changed = False
        if state.codex_thread_id != new_thread_id:
            state.codex_thread_id = new_thread_id
            state.codex_active_turn_id = ""
            changed = True
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
        latest_thread_id = self.get_latest_codex_session_id_for_cwd(cwd)
        if not latest_thread_id:
            return ""

        result = await codex_app_server_client.thread_resume(thread_id=latest_thread_id)
        resumed_thread_id = self._extract_lifecycle_thread_id(
            result,
            fallback=latest_thread_id,
        )
        if not resumed_thread_id:
            return ""
        resumed_turn_id = self._extract_lifecycle_turn_id(result)
        self.set_window_codex_thread_id(window_id, resumed_thread_id)
        self.set_window_codex_active_turn_id(window_id, resumed_turn_id)
        state = self.get_window_state(window_id)
        if cwd and state.cwd != cwd:
            state.cwd = cwd
            self._save_state()
        self.sync_window_topic_model_selection_from_codex_session(
            window_id=window_id,
            codex_thread_id=resumed_thread_id,
            cwd=cwd,
        )
        logger.info(
            "Resumed latest Codex thread for window %s (cwd=%s): %s",
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
    ) -> tuple[bool, str]:
        """Clear stale thread id and retry one send using a fresh thread."""
        if stale_thread_id:
            self.set_window_codex_thread_id(window_id, "")
        else:
            self.clear_window_codex_turn(window_id)

        logger.warning(
            "App-server thread missing for %s (%s), retrying with a resumed/latest or fresh thread id",
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

        try:
            resumed_thread_id = await self.resume_latest_codex_session_for_window(
                window_id=window_id,
                cwd=cwd,
            )
            if resumed_thread_id:
                emit_telemetry(
                    "transport.app_server.thread_missing_resumed_latest",
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
                "transport.app_server.thread_missing_resume_latest_failed",
                runtime_mode=config.runtime_mode,
                codex_transport=config.codex_transport,
                window_id=window_id,
                display=self.get_display_name(window_id),
                stale_thread_id=stale_thread_id,
                cwd=cwd,
                error=str(resume_error),
            )

        send_kwargs: dict[str, str] = {}
        if model_slug:
            send_kwargs["model_slug"] = model_slug
        if reasoning_effort:
            send_kwargs["reasoning_effort"] = reasoning_effort
        if service_tier:
            send_kwargs["service_tier"] = service_tier
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

    async def _retry_send_after_steer_timeout(
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
    ) -> tuple[bool, str]:
        """Clear stale active turn and retry once via turn/start."""
        if thread_id:
            codex_app_server_client.clear_active_turn(thread_id)
        self.clear_window_codex_turn(window_id)

        logger.warning(
            "App-server turn/steer timed out for %s (%s), retrying with turn/start",
            window_id,
            self.get_display_name(window_id),
        )
        emit_telemetry(
            "transport.app_server.steer_timeout_retry",
            runtime_mode=config.runtime_mode,
            codex_transport=config.codex_transport,
            window_id=window_id,
            display=self.get_display_name(window_id),
            steer=steer,
            stale_turn_id=stale_turn_id,
            thread_id=thread_id,
        )

        send_kwargs: dict[str, str] = {}
        if model_slug:
            send_kwargs["model_slug"] = model_slug
        if reasoning_effort:
            send_kwargs["reasoning_effort"] = reasoning_effort
        if service_tier:
            send_kwargs["service_tier"] = service_tier
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
                "transport.app_server.steer_timeout_recovered",
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
                "transport.app_server.steer_timeout_recovery_failed",
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
        window_name: str,
        cwd: str,
        model_slug: str = "",
        reasoning_effort: str = "",
        service_tier: str = "",
    ) -> tuple[bool, str]:
        if not model_slug and not reasoning_effort:
            model_slug, reasoning_effort = self.get_window_topic_model_selection(window_id)
        if not service_tier:
            service_tier = self.get_window_topic_service_tier_selection(window_id)
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
            **ensure_kwargs,
        )
        workspace_path, can_write = self._runtime_write_state(cwd)
        runtime_hint = self._build_runtime_capability_hint(
            workspace_path=workspace_path,
            can_write=can_write,
            approval_policy=approval_policy,
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

        if steer or active_turn:
            if not active_turn:
                return False, "No active turn to steer."
            result = await codex_app_server_client.turn_steer(
                thread_id=thread_id,
                expected_turn_id=active_turn,
                inputs=turn_inputs,
            )
            new_turn_id = result.get("turnId") if isinstance(result, dict) else None
            state.codex_active_turn_id = (
                new_turn_id
                if isinstance(new_turn_id, str) and new_turn_id
                else active_turn
            )
        else:
            result = await self._turn_start_with_retry(
                thread_id=thread_id,
                inputs=turn_inputs,
                approval_policy=approval_policy,
                service_tier=service_tier,
            )
            turn = result.get("turn") if isinstance(result, dict) else None
            turn_id = turn.get("id") if isinstance(turn, dict) else None
            state.codex_active_turn_id = turn_id if isinstance(turn_id, str) else ""

        if cwd and state.cwd != cwd:
            state.cwd = cwd
        if window_name and state.window_name != window_name:
            state.window_name = window_name
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
        model_slug: str = "",
        reasoning_effort: str = "",
        service_tier: str = "",
    ) -> tuple[bool, str]:
        """Send structured user inputs to a window via Codex app-server."""
        display = self.get_display_name(window_id)
        lock = self._get_window_send_lock(window_id)
        lock_wait_started = time.monotonic()
        async with lock:
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
            cwd = fallback_state.cwd

            if codex_app_server_mode:
                if not cwd:
                    return False, "No workspace bound to this topic. Run /start first."
                try:
                    send_kwargs: dict[str, Any] = {}
                    if model_slug:
                        send_kwargs["model_slug"] = model_slug
                    if reasoning_effort:
                        send_kwargs["reasoning_effort"] = reasoning_effort
                    if service_tier:
                        send_kwargs["service_tier"] = service_tier
                    return await self._send_inputs_via_codex_app_server(
                        window_id=window_id,
                        inputs=inputs,
                        steer=steer,
                        window_name=window_name,
                        cwd=cwd,
                        **send_kwargs,
                    )
                except Exception as e:
                    stale_thread_id = fallback_state.codex_thread_id.strip()
                    stale_turn_id = fallback_state.codex_active_turn_id.strip()
                    error_text = str(e)
                    if self._is_missing_codex_thread_error(e):
                        try:
                            return await self._retry_send_after_missing_codex_thread(
                                window_id=window_id,
                                inputs=inputs,
                                window_name=window_name,
                                cwd=cwd,
                                steer=steer,
                                stale_thread_id=stale_thread_id,
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
                    elif self._is_turn_steer_timeout_error(e):
                        try:
                            return await self._retry_send_after_steer_timeout(
                                window_id=window_id,
                                inputs=inputs,
                                window_name=window_name,
                                cwd=cwd,
                                steer=steer,
                                stale_turn_id=stale_turn_id,
                                thread_id=stale_thread_id,
                                **send_kwargs,
                            )
                        except Exception as retry_error:
                            emit_telemetry(
                                "transport.app_server.steer_timeout_recovery_failed",
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
        return await self.send_inputs_to_window(window_id, payload, steer=steer)

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
