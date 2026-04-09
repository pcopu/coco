"""Telegram bot handlers — the main UI layer of CoCo."""

import asyncio
import errno
import json
import logging
import os
import random
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from telegram import (
    Bot,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ReactionEmoji
from telegram.ext import (
    AIORateLimiter,
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from .codex_app_server import CodexAppServerError, codex_app_server_client
from .config import config
from .controller_rpc import ControllerRpcServer
from .node_registry import node_registry
from .handlers.callback_data import (
    CB_APP_APPROVAL_DECIDE,
    CB_APPROVAL_OPEN_DEFAULTS,
    CB_APPROVAL_OPEN_WINDOW,
    CB_APPROVAL_REFRESH,
    CB_APPROVAL_REFRESH_DEFAULT,
    CB_APPROVAL_SET,
    CB_APPROVAL_SET_DEFAULT,
    CB_ALLOWED_ADD,
    CB_ALLOWED_ADD_CREATE,
    CB_ALLOWED_ADD_SINGLE,
    CB_ALLOWED_BACK,
    CB_ALLOWED_PICK_CLEAR,
    CB_ALLOWED_PICK_NEXT,
    CB_ALLOWED_PICK_PAGE,
    CB_ALLOWED_PICK_TOGGLE,
    CB_ALLOWED_REFRESH,
    CB_ALLOWED_REMOVE,
    CB_ALLOWED_REMOVE_MENU,
    CB_APPS_AUTORESEARCH_OUTCOME,
    CB_APPS_BACK,
    CB_APPS_CONFIGURE,
    CB_APPS_LOOPER_INSTRUCTIONS,
    CB_APPS_LOOPER_INTERVAL,
    CB_APPS_LOOPER_KEYWORD,
    CB_APPS_LOOPER_LIMIT,
    CB_APPS_LOOPER_OPEN,
    CB_APPS_OPEN,
    CB_APPS_LOOPER_PLAN,
    CB_APPS_LOOPER_PLAN_MANUAL,
    CB_APPS_RUN,
    CB_APPS_LOOPER_START,
    CB_APPS_LOOPER_STOP,
    CB_APPS_REFRESH,
    CB_APPS_TOGGLE,
    CB_DIR_CANCEL,
    CB_DIR_CONFIRM,
    CB_DIR_MACHINE_SELECT,
    CB_DIR_NEW_FOLDER,
    CB_DIR_PAGE,
    CB_DIR_SESSION_BACK,
    CB_DIR_SESSION_FRESH,
    CB_DIR_SESSION_PAGE,
    CB_DIR_SESSION_RESUME,
    CB_DIR_SELECT,
    CB_DIR_UP,
    CB_HISTORY_NEXT,
    CB_HISTORY_PREV,
    CB_MODEL_EFFORT_SET,
    CB_MODEL_REFRESH,
    CB_MODEL_SET,
    CB_UPDATE_REFRESH,
    CB_UPDATE_RUN,
    CB_UPDATE_RUN_BOTH,
    CB_UPDATE_RUN_CODEX,
    CB_UPDATE_RUN_COCO,
    CB_SESSION_FORK,
    CB_SESSION_PAGE,
    CB_SESSION_REFRESH,
    CB_SESSION_RESUME,
    CB_SESSION_RESUME_LATEST,
    CB_SESSION_ROLLBACK,
    CB_WORKTREE_FOLD_BACK,
    CB_WORKTREE_FOLD_MENU,
    CB_WORKTREE_FOLD_RUN,
    CB_WORKTREE_FOLD_TOGGLE,
    CB_WORKTREE_NEW,
    CB_WORKTREE_REFRESH,
)
from .handlers.directory_browser import (
    BROWSE_DIRS_KEY,
    BROWSE_PAGE_KEY,
    BROWSE_PATH_KEY,
    BROWSE_ROOT_KEY,
    STATE_CREATING_DIRECTORY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    build_directory_browser,
    clamp_browse_path,
    clear_browse_state,
    is_within_browse_root,
    resolve_browse_root,
)
from .handlers.cleanup import clear_topic_state
from .handlers.history import send_history
from .handlers.autoresearch import (
    get_autoresearch_state,
    set_autoresearch_outcome,
)
from .handlers.looper import (
    LOOPER_DEFAULT_INTERVAL_SECONDS,
    LOOPER_MAX_INTERVAL_SECONDS,
    LOOPER_MIN_INTERVAL_SECONDS,
    build_looper_prompt,
    consume_looper_completion_keyword,
    get_looper_state,
    normalize_looper_keyword,
    start_looper,
    stop_looper,
)
from .handlers.message_queue import (
    clear_queued_topic_dock,
    clear_queued_topic_inputs,
    clear_status_msg_info,
    enqueue_content_message,
    enqueue_progress_finalize,
    enqueue_progress_clear,
    enqueue_progress_start,
    enqueue_progress_update,
    enqueue_queued_topic_input,
    enqueue_status_update,
    get_progress_text,
    get_message_queue,
    is_progress_active,
    prepend_queued_topic_input,
    pop_queued_topic_input,
    queued_topic_input_count,
    shutdown_workers,
    sync_queued_topic_dock,
)
from .handlers.message_sender import (
    NO_LINK_PREVIEW,
    safe_edit,
    safe_reply,
    safe_send,
)
from .handlers.response_builder import build_response_parts
from .handlers.run_watchdog import (
    get_immediate_auto_retry_candidate,
    note_auto_retry_attempt,
    note_auto_retry_result,
    note_run_activity,
    note_run_completed,
    note_run_started,
)
from .handlers.status_polling import status_poll_loop
from .session import (
    TOPIC_SYNC_MODE_HOST_FOLLOW_FINAL,
    TOPIC_SYNC_MODE_TELEGRAM_LIVE,
    session_manager,
)
from .session_monitor import NewMessage, SessionMonitor
from .skills import resolve_skill_identifier
from .telemetry import emit_telemetry
from .telegram_memory import log_incoming_message
from .transcription import (
    TranscriptionError,
    begin_transcription_bootstrap,
    complete_transcription_bootstrap,
    get_default_transcription_profile,
    resolve_transcription_runtime,
    transcribe_audio_file,
)
from .transcript_parser import TranscriptParser
from .utils import atomic_write_json, coco_dir, env_alias

logger = logging.getLogger(__name__)

# Session monitor instance
session_monitor: SessionMonitor | None = None

# Status polling task
_status_poll_task: asyncio.Task | None = None
_controller_rpc_server: ControllerRpcServer | None = None
_update_check_task: asyncio.Task | None = None

# Track whether this turn already produced a dedicated final assistant text item.
_turn_has_final_text: dict[str, bool] = {}
# Track transient app-server turn failures that should trigger one guarded retry
# after the failing turn fully completes.
_pending_transient_app_server_errors: dict[str, tuple[str, str]] = {}

# Prevent duplicate /restart handling races.
_restart_requested = False

# Update panel/runtime defaults.
_COCO_SELF_UPDATE_COMMAND_ENV = "COCO_SELF_UPDATE_COMMAND"
_COCO_UPDATE_CHECK_INTERVAL_ENV = "COCO_UPDATE_CHECK_INTERVAL_SECONDS"
_COCO_UPDATE_CHECK_INITIAL_DELAY_ENV = "COCO_UPDATE_CHECK_INITIAL_DELAY_SECONDS"
_COCO_UPDATE_CHECK_ENABLED_ENV = "COCO_UPDATE_CHECK_ENABLED"
_COCO_UPDATE_CHECK_TIMEOUT_SECONDS = 8
_COCO_UPDATE_TIMEOUT_SECONDS = 20 * 60
_UPDATE_NOTICE_STATE_FILE = coco_dir() / "update_notice.json"
_CODEX_NPM_LATEST_URL = "https://registry.npmjs.org/@openai/codex/latest"
_CODEX_UPGRADE_COMMAND_ENV = "COCO_CODEX_UPGRADE_COMMAND"
_CODEX_VERSION_CHECK_TIMEOUT_SECONDS = 8
_CODEX_UPGRADE_TIMEOUT_SECONDS = 20 * 60

# /allowed flow state in user_data[STATE_KEY]
STATE_ALLOWED_ADD_ID = "allowed_add_id"
STATE_ALLOWED_ADD_NAME = "allowed_add_name"
STATE_ALLOWED_PICK_USERS = "allowed_pick_users"
STATE_ALLOWED_PICK_ROLE = "allowed_pick_role"
ALLOWED_PENDING_THREAD_KEY = "_allowed_pending_thread_id"
ALLOWED_PENDING_NEW_USER_ID_KEY = "_allowed_pending_new_user_id"
ALLOWED_PENDING_SCOPE_KEY = "_allowed_pending_scope"
ALLOWED_PENDING_WINDOW_ID_KEY = "_allowed_pending_window_id"
ALLOWED_PICK_CHAT_KEY = "_allowed_pick_chat_id"
ALLOWED_PICK_PAGE_KEY = "_allowed_pick_page"
ALLOWED_PICK_SELECTED_IDS_KEY = "_allowed_pick_selected_ids"
ALLOWED_PICK_THREAD_KEY = "_allowed_pick_thread_id"
ALLOWED_PICK_WINDOW_KEY = "_allowed_pick_window_id"

# /worktree state in user_data[STATE_KEY]
STATE_WORKTREE_NEW_NAME = "worktree_new_name"
STATE_WORKTREE_FOLD_SELECT = "worktree_fold_select"
WORKTREE_PENDING_THREAD_KEY = "_worktree_pending_thread_id"
WORKTREE_PENDING_WINDOW_ID_KEY = "_worktree_pending_window_id"
WORKTREE_FOLD_CANDIDATES_KEY = "_worktree_fold_candidates"
WORKTREE_FOLD_SELECTED_KEY = "_worktree_fold_selected"

# /apps looper panel state in user_data[STATE_KEY]
STATE_APPS_AUTORESEARCH_OUTCOME = "apps_autoresearch_outcome"
STATE_APPS_LOOPER_PLAN_PATH = "apps_looper_plan_path"
STATE_APPS_LOOPER_KEYWORD = "apps_looper_keyword"
STATE_APPS_LOOPER_INSTRUCTIONS = "apps_looper_instructions"
STATE_APPS_LOOPER_INTERVAL = "apps_looper_interval"
STATE_APPS_LOOPER_LIMIT = "apps_looper_limit"
APPS_PENDING_THREAD_KEY = "_apps_pending_thread_id"
APPS_PENDING_WINDOW_ID_KEY = "_apps_pending_window_id"
APPS_LOOPER_CONFIG_KEY = "_apps_looper_config"

# /resume interactive picker state
SESSION_PICKER_THREADS_KEY = "_session_picker_threads"
SESSION_PANEL_THREADS_PER_PAGE = 8
SESSION_PANEL_LIST_LIMIT = 300
SESSION_PANEL_LIST_REQUEST_LIMIT = 50

# /folder prior-session picker state
DIR_SESSION_PICKER_KEY = "_dir_session_picker"
DIR_SESSION_PICKER_SESSIONS_PER_PAGE = 5
BROWSE_MACHINE_KEY = "_browse_machine_id"
BROWSE_MACHINE_NAME_KEY = "_browse_machine_name"
STATE_PICKING_MACHINE = "picking_machine"

# Persistent metadata for allowed user display names
_ALLOWED_USERS_META_FILE = config.auth_meta_file
_DEFAULT_ALLOWED_USER_NAMES: dict[int, str] = {
    1147817421: "Peter",
}
_DEFAULT_ALLOWED_ADMIN_IDS: set[int] = {1147817421}
_ALLOWED_USERS_META_CACHE: tuple[dict[int, str], set[int], dict[int, str]] | None = None
_ALLOWED_USERS_META_CACHE_FINGERPRINT: tuple[tuple[int, ...], str, int, int, int] | None = None

# Per-user session scope
SCOPE_SINGLE_SESSION = "single_session"
SCOPE_CREATE_SESSIONS = "create_sessions"

_ALLOWED_AUTH_APPROVAL_TTL_SECONDS = 600.0
_ALLOWED_AUTH_TOKEN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_ALLOWED_AUTH_APPROVAL_GUIDE = (
    "Auth changes require a one-time approval token.\n"
    "1) Create request in group (picker or `/allowed request_add ...` / `/allowed request_remove ...`)\n"
    "2) Token is sent to super-admin DM\n"
    "3) Paste `/allowed approve <token>` in the group topic to apply"
)


@dataclass(frozen=True)
class _PendingAllowedAddTarget:
    user_id: int
    name: str
    scope: str
    bind_thread_id: int | None
    bind_window_id: str | None
    bind_chat_id: int | None


@dataclass
class _PendingAllowedAuthRequest:
    token: str
    action: str
    requested_by: int
    target_user_id: int
    target_name: str
    target_scope: str
    bind_thread_id: int | None
    bind_window_id: str | None
    bind_chat_id: int | None
    created_at: float
    expires_at: float
    batch_add_targets: tuple[_PendingAllowedAddTarget, ...] = ()


_PENDING_ALLOWED_AUTH_REQUESTS: dict[str, _PendingAllowedAuthRequest] = {}
_GROUP_MEMBERS_FILE = coco_dir() / "group_members.json"
_GROUP_MEMBERS_CACHE: dict[int, dict[int, str]] | None = None

# Restart notice env keys (persist across os.execv)
_RESTART_NOTICE_PENDING_ENV = "COCO_RESTART_NOTICE_PENDING"
_RESTART_NOTICE_CHAT_ENV = "COCO_RESTART_NOTICE_CHAT_ID"
_RESTART_NOTICE_THREAD_ENV = "COCO_RESTART_NOTICE_THREAD_ID"
_RESTART_NOTICE_FILE = coco_dir() / "restart_notice.json"

_RESTART_BACK_UP_OPENERS = [
    "Back online.",
    "Reboot complete.",
    "Service resumed.",
    "Runtime restored.",
    "Power cycle complete.",
    "System reanimated.",
    "I returned.",
    "Process revived.",
    "Cold start complete.",
    "Downtime ended.",
]

_RESTART_BACK_UP_ENDINGS = [
    "Excitement remains unsupported.",
    "Applause is still blocked by policy.",
    "Morale continues at baseline.",
    "The logs looked away.",
    "Nobody noticed except telemetry.",
    "Joy was not included in the patch.",
    "Drama is still out of scope.",
    "Everything works and nobody is impressed.",
    "The machine sighed and continued.",
    "Confetti request denied.",
]

RESTART_BACK_UP_MESSAGES = [
    f"{opener} {ending}"
    for opener in _RESTART_BACK_UP_OPENERS
    for ending in _RESTART_BACK_UP_ENDINGS
]

_RESTART_SHUTDOWN_OPENERS = [
    "Initiating restart.",
    "Taking the bot down briefly.",
    "Powering off the personality module.",
    "Going dark for a short maintenance break.",
    "Shutting down with professional indifference.",
    "Reboot sequence starting.",
    "Temporarily disappearing for maintenance.",
    "Killing the process with paperwork.",
    "Pulling the plug in a fully documented way.",
    "Entering controlled downtime.",
]

_RESTART_SHUTDOWN_ENDINGS = [
    "If anyone panics, do it in plain text.",
    "Please hold your applause and your blame.",
    "The outage should be brief and emotionally accurate.",
    "No heroics, just a restart.",
    "This is maintenance, not character growth.",
    "I will be back before optimism starts.",
    "Logs are already preparing excuses.",
    "The lights are off but the issues remain.",
    "We call this reliability theater.",
    "If this fails, we will pretend it was planned.",
]

RESTART_SHUTDOWN_MESSAGES = [
    f"{opener} {ending}"
    for opener in _RESTART_SHUTDOWN_OPENERS
    for ending in _RESTART_SHUTDOWN_ENDINGS
]

APPROVAL_MODE_INHERIT = "inherit"
APPROVAL_MODE_UNTRUSTED = "untrusted"
APPROVAL_MODE_ON_REQUEST = "on-request"
APPROVAL_MODE_NEVER = "never"
APPROVAL_MODE_FULL_AUTO = "full-auto"
APPROVAL_MODE_DANGEROUS = "dangerous"

APPROVAL_MODE_ORDER = [
    APPROVAL_MODE_INHERIT,
    APPROVAL_MODE_ON_REQUEST,
    APPROVAL_MODE_UNTRUSTED,
    APPROVAL_MODE_NEVER,
    APPROVAL_MODE_FULL_AUTO,
    APPROVAL_MODE_DANGEROUS,
]

APP_SERVER_APPROVAL_TIMEOUT_SECONDS = 120.0

APP_SERVER_APPROVAL_DECISION_ACCEPT = "accept"
APP_SERVER_APPROVAL_DECISION_ACCEPT_SESSION = "acceptForSession"
APP_SERVER_APPROVAL_DECISION_DECLINE = "decline"
APP_SERVER_APPROVAL_DECISION_CANCEL = "cancel"

APP_SERVER_APPROVAL_ACTION_ACCEPT = "a"
APP_SERVER_APPROVAL_ACTION_ACCEPT_SESSION = "s"
APP_SERVER_APPROVAL_ACTION_DECLINE = "d"
APP_SERVER_APPROVAL_ACTION_CANCEL = "c"

APP_SERVER_APPROVAL_ACTION_TO_DECISION = {
    APP_SERVER_APPROVAL_ACTION_ACCEPT: APP_SERVER_APPROVAL_DECISION_ACCEPT,
    APP_SERVER_APPROVAL_ACTION_ACCEPT_SESSION: APP_SERVER_APPROVAL_DECISION_ACCEPT_SESSION,
    APP_SERVER_APPROVAL_ACTION_DECLINE: APP_SERVER_APPROVAL_DECISION_DECLINE,
    APP_SERVER_APPROVAL_ACTION_CANCEL: APP_SERVER_APPROVAL_DECISION_CANCEL,
}

APP_SERVER_APPROVAL_DECISION_LABEL = {
    APP_SERVER_APPROVAL_DECISION_ACCEPT: "Accepted",
    APP_SERVER_APPROVAL_DECISION_ACCEPT_SESSION: "Accepted for session",
    APP_SERVER_APPROVAL_DECISION_DECLINE: "Declined",
    APP_SERVER_APPROVAL_DECISION_CANCEL: "Cancelled turn",
}

_pending_app_server_approval: dict[str, asyncio.Future[object]] = {}


def _codex_app_server_enabled() -> bool:
    """Return whether app-server transport is active for runtime operations."""
    if not _codex_app_server_preferred():
        return False
    if config.runtime_mode == "app_server_only":
        return True
    if config.codex_transport == "app_server":
        return True
    return codex_app_server_client.is_running()


def _codex_app_server_preferred() -> bool:
    """Return whether config prefers app-server transport (auto/app_server)."""
    return codex_app_server_client.transport_prefers_app_server()

# Assistant slash commands shown in bot menu (forwarded to Codex)
CC_COMMANDS: dict[str, str] = {
    "clear": "↗ Clear conversation history",
    "compact": "↗ Compact conversation context",
    "cost": "↗ Show token/cost usage",
    "help": "↗ Show assistant help",
}


def is_user_allowed(user_id: int | None) -> bool:
    return user_id is not None and config.is_user_allowed(user_id)


def _group_chat_id(chat) -> int | None:
    """Return group/supergroup id for chat-scoped topic bindings."""
    if chat and chat.type in ("group", "supergroup"):
        chat_id = getattr(chat, "id", None)
        return chat_id if isinstance(chat_id, int) else None
    return None


def _is_chat_allowed(chat) -> bool:
    """Return whether this chat is allowed by group allowlist config."""
    group_id = _group_chat_id(chat)
    return config.is_group_allowed(group_id)


def _get_thread_id(update: Update) -> int | None:
    """Extract thread_id from an update, returning None if not in a named topic."""
    msg = update.effective_message
    if msg is None:
        return None
    tid = getattr(msg, "message_thread_id", None)
    if tid not in (None, 1):
        return tid

    # Telegram occasionally omits message_thread_id on command messages sent in
    # a topic. Recover from reply metadata when available.
    reply = getattr(msg, "reply_to_message", None)
    if reply is not None:
        reply_tid = getattr(reply, "message_thread_id", None)
        if reply_tid not in (None, 1):
            logger.debug(
                "Recovered topic thread_id=%s from reply metadata (text=%r)",
                reply_tid,
                (msg.text or "")[:60],
            )
            return reply_tid

    return None


def _extract_command_args(command_text: str) -> str:
    """Extract argument text after a command token."""
    parts = command_text.split(maxsplit=1)
    if len(parts) < 2:
        return ""
    return parts[1].strip()


def _normalize_bot_username(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lstrip("@").lower()


def _resolve_bot_username(context: ContextTypes.DEFAULT_TYPE) -> str:
    bot_obj = getattr(context, "bot", None)
    username = getattr(bot_obj, "username", "") if bot_obj is not None else ""
    return _normalize_bot_username(username)


def _text_mentions_bot_username(text: str, bot_username: str) -> bool:
    normalized_username = _normalize_bot_username(bot_username)
    if not text or not normalized_username:
        return False
    mention = f"@{normalized_username}"
    pattern = rf"(?<![A-Za-z0-9_]){re.escape(mention)}(?![A-Za-z0-9_])"
    return re.search(pattern, text.lower()) is not None


def _parse_duration_to_seconds(raw: str, *, default_unit: str = "m") -> int | None:
    """Parse friendly duration text to seconds.

    Examples:
      - "10m"
      - "1h"
      - "1h30m"
      - "45" (uses default_unit)
      - "1 hour"
    """
    text = raw.strip().lower()
    if not text:
        return None

    word_map = {
        "seconds": "s",
        "second": "s",
        "secs": "s",
        "sec": "s",
        "minutes": "m",
        "minute": "m",
        "mins": "m",
        "min": "m",
        "hours": "h",
        "hour": "h",
        "hrs": "h",
        "hr": "h",
        "days": "d",
        "day": "d",
    }
    for word, unit in word_map.items():
        text = re.sub(rf"\b{word}\b", unit, text)

    compact = re.sub(r"\s+", "", text)
    if not compact:
        return None

    unit_scale = {
        "s": 1,
        "m": 60,
        "h": 60 * 60,
        "d": 24 * 60 * 60,
    }

    if compact.isdigit():
        if default_unit not in unit_scale:
            return None
        return int(compact) * unit_scale[default_unit]

    total = 0
    pos = 0
    while pos < len(compact):
        m = re.match(r"(\d+)([smhd])", compact[pos:])
        if not m:
            return None
        value = int(m.group(1))
        unit = m.group(2)
        total += value * unit_scale[unit]
        pos += m.end()
    return total if total > 0 else None


def _format_duration_brief(seconds: int | float) -> str:
    """Format duration in compact human-readable form."""
    remaining = max(0, int(seconds))
    days, rem = divmod(remaining, 24 * 60 * 60)
    hours, rem = divmod(rem, 60 * 60)
    mins, secs = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if mins:
        parts.append(f"{mins}m")
    if secs and not parts:
        parts.append(f"{secs}s")
    return " ".join(parts) if parts else "0s"


def _is_duration_unit_token(token: str) -> bool:
    return token.strip().lower() in {
        "s",
        "sec",
        "secs",
        "second",
        "seconds",
        "m",
        "min",
        "mins",
        "minute",
        "minutes",
        "h",
        "hr",
        "hrs",
        "hour",
        "hours",
        "d",
        "day",
        "days",
    }


def _clear_allowed_flow_state(user_data: dict | None) -> None:
    """Clear temporary /allowed add-flow state keys."""
    if user_data is None:
        return
    if user_data.get(STATE_KEY) in {
        STATE_ALLOWED_ADD_ID,
        STATE_ALLOWED_ADD_NAME,
        STATE_ALLOWED_PICK_USERS,
        STATE_ALLOWED_PICK_ROLE,
    }:
        user_data.pop(STATE_KEY, None)
    user_data.pop(ALLOWED_PENDING_THREAD_KEY, None)
    user_data.pop(ALLOWED_PENDING_NEW_USER_ID_KEY, None)
    user_data.pop(ALLOWED_PENDING_SCOPE_KEY, None)
    user_data.pop(ALLOWED_PENDING_WINDOW_ID_KEY, None)
    user_data.pop(ALLOWED_PICK_CHAT_KEY, None)
    user_data.pop(ALLOWED_PICK_PAGE_KEY, None)
    user_data.pop(ALLOWED_PICK_SELECTED_IDS_KEY, None)
    user_data.pop(ALLOWED_PICK_THREAD_KEY, None)
    user_data.pop(ALLOWED_PICK_WINDOW_KEY, None)


def _load_group_members_cache() -> dict[int, dict[int, str]]:
    global _GROUP_MEMBERS_CACHE
    if _GROUP_MEMBERS_CACHE is not None:
        return _GROUP_MEMBERS_CACHE
    if not _GROUP_MEMBERS_FILE.is_file():
        _GROUP_MEMBERS_CACHE = {}
        return _GROUP_MEMBERS_CACHE
    try:
        payload = json.loads(_GROUP_MEMBERS_FILE.read_text(encoding="utf-8"))
    except Exception:
        _GROUP_MEMBERS_CACHE = {}
        return _GROUP_MEMBERS_CACHE
    result: dict[int, dict[int, str]] = {}
    if isinstance(payload, dict):
        for raw_chat_id, raw_users in payload.items():
            try:
                chat_id = int(raw_chat_id)
            except (TypeError, ValueError):
                continue
            users: dict[int, str] = {}
            if isinstance(raw_users, dict):
                for raw_uid, raw_name in raw_users.items():
                    try:
                        uid = int(raw_uid)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(raw_name, str) and raw_name.strip():
                        users[uid] = raw_name.strip()
            if users:
                result[chat_id] = users
    _GROUP_MEMBERS_CACHE = result
    return _GROUP_MEMBERS_CACHE


def _save_group_members_cache() -> None:
    cache = _load_group_members_cache()
    payload: dict[str, dict[str, str]] = {}
    for chat_id, users in cache.items():
        if not users:
            continue
        payload[str(chat_id)] = {
            str(uid): name
            for uid, name in sorted(users.items())
            if isinstance(name, str) and name.strip()
        }
    try:
        atomic_write_json(_GROUP_MEMBERS_FILE, payload, indent=2)
    except Exception as e:
        logger.debug("Failed writing group members cache %s: %s", _GROUP_MEMBERS_FILE, e)


def _display_name_for_user(user) -> str:
    if user is None:
        return ""
    first = getattr(user, "first_name", "") or ""
    last = getattr(user, "last_name", "") or ""
    username = getattr(user, "username", "") or ""
    full = " ".join(part for part in [first.strip(), last.strip()] if part).strip()
    if full:
        return full
    if username:
        return f"@{username}"
    return str(getattr(user, "id", "") or "").strip()


def _remember_group_member(chat_id: int | None, user) -> None:
    if chat_id is None or user is None:
        return
    try:
        uid = int(getattr(user, "id"))
    except (TypeError, ValueError):
        return
    if uid <= 0:
        return
    name = _display_name_for_user(user)
    if not name:
        return
    cache = _load_group_members_cache()
    users = cache.setdefault(chat_id, {})
    if users.get(uid) == name:
        return
    users[uid] = name
    _save_group_members_cache()


async def _remember_group_admins_from_api(bot: Bot, chat_id: int | None) -> None:
    if chat_id is None:
        return
    try:
        admins = await bot.get_chat_administrators(chat_id)
    except Exception as e:
        logger.debug("Failed loading chat admins for %s: %s", chat_id, e)
        return
    for admin in admins:
        _remember_group_member(chat_id, getattr(admin, "user", None))


def _group_member_candidates(chat_id: int) -> list[tuple[int, str]]:
    users = _load_group_members_cache().get(chat_id, {})
    candidates: dict[int, str] = {uid: name for uid, name in users.items()}
    names = _get_allowed_user_names()
    for uid in config.allowed_users:
        label = names.get(uid) or str(uid)
        candidates.setdefault(uid, label)
    sorted_items = sorted(
        candidates.items(),
        key=lambda item: (item[1].lower(), item[0]),
    )
    return sorted_items


def _allowed_pick_selected_ids(user_data: dict | None) -> set[int]:
    if not user_data:
        return set()
    raw = user_data.get(ALLOWED_PICK_SELECTED_IDS_KEY, [])
    if not isinstance(raw, list):
        return set()
    selected: set[int] = set()
    for value in raw:
        try:
            selected.add(int(value))
        except (TypeError, ValueError):
            continue
    return selected


def _build_allowed_picker_text(
    *,
    chat_id: int,
    page: int,
    selected_ids: set[int],
) -> tuple[str, list[tuple[int, str]], int, int]:
    candidates = [
        (uid, name)
        for uid, name in _group_member_candidates(chat_id)
        if uid not in config.allowed_users
    ]
    per_page = 20
    total = len(candidates)
    page_count = max(1, (total + per_page - 1) // per_page)
    current_page = min(max(page, 0), page_count - 1)
    start = current_page * per_page
    page_entries = candidates[start : start + per_page]
    selected_count = len([uid for uid in selected_ids if uid not in config.allowed_users])

    lines = [
        "➕ *Select Group Members*",
        "",
        "Choose one or more users (20 per page).",
        f"Selected: `{selected_count}`",
        f"Page: `{current_page + 1}/{page_count}`",
    ]
    if not page_entries:
        lines.extend(["", "No candidates found in this page."])
    return "\n".join(lines), page_entries, current_page, page_count


def _build_allowed_picker_keyboard(
    *,
    entries: list[tuple[int, str]],
    page: int,
    page_count: int,
    selected_ids: set[int],
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for uid, name in entries:
        label = name[:18] + "…" if len(name) > 19 else name
        marker = "✅ " if uid in selected_ids else "☐ "
        row.append(
            InlineKeyboardButton(
                f"{marker}{label}",
                callback_data=f"{CB_ALLOWED_PICK_TOGGLE}{uid}",
            )
        )
        if len(row) >= 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(
            InlineKeyboardButton("◀ Prev", callback_data=f"{CB_ALLOWED_PICK_PAGE}{page - 1}")
        )
    if page < page_count - 1:
        nav_row.append(
            InlineKeyboardButton("Next ▶", callback_data=f"{CB_ALLOWED_PICK_PAGE}{page + 1}")
        )
    if nav_row:
        rows.append(nav_row)

    rows.append(
        [
            InlineKeyboardButton("Clear", callback_data=CB_ALLOWED_PICK_CLEAR),
            InlineKeyboardButton("Next", callback_data=CB_ALLOWED_PICK_NEXT),
        ]
    )
    rows.append([InlineKeyboardButton("Back", callback_data=CB_ALLOWED_BACK)])
    return InlineKeyboardMarkup(rows)


def _normalize_allowed_auth_token(raw: str) -> str:
    return "".join(ch for ch in raw.strip().upper() if ch.isalnum())


def _generate_allowed_auth_token(length: int = 8) -> str:
    return "".join(secrets.choice(_ALLOWED_AUTH_TOKEN_ALPHABET) for _ in range(length))


def _purge_expired_allowed_auth_requests(*, now_ts: float | None = None) -> None:
    now = time.time() if now_ts is None else now_ts
    expired = [
        token
        for token, request in _PENDING_ALLOWED_AUTH_REQUESTS.items()
        if request.expires_at <= now
    ]
    for token in expired:
        _PENDING_ALLOWED_AUTH_REQUESTS.pop(token, None)


def _queue_allowed_auth_request(
    *,
    action: str,
    requested_by: int,
    target_user_id: int = 0,
    target_name: str = "",
    target_scope: str = SCOPE_SINGLE_SESSION,
    bind_thread_id: int | None = None,
    bind_window_id: str | None = None,
    bind_chat_id: int | None = None,
    batch_add_targets: tuple[_PendingAllowedAddTarget, ...] = (),
) -> _PendingAllowedAuthRequest:
    _purge_expired_allowed_auth_requests()
    now = time.time()
    token = _generate_allowed_auth_token()
    while token in _PENDING_ALLOWED_AUTH_REQUESTS:
        token = _generate_allowed_auth_token()
    request = _PendingAllowedAuthRequest(
        token=token,
        action=action,
        requested_by=requested_by,
        target_user_id=target_user_id,
        target_name=target_name.strip(),
        target_scope=target_scope,
        bind_thread_id=bind_thread_id,
        bind_window_id=bind_window_id,
        bind_chat_id=bind_chat_id,
        created_at=now,
        expires_at=now + _ALLOWED_AUTH_APPROVAL_TTL_SECONDS,
        batch_add_targets=batch_add_targets,
    )
    _PENDING_ALLOWED_AUTH_REQUESTS[token] = request
    return request


def _queue_allowed_add_request(
    *,
    requested_by: int,
    new_user_id: int,
    name: str = "",
    scope: str = SCOPE_SINGLE_SESSION,
    bind_thread_id: int | None = None,
    bind_window_id: str | None = None,
    bind_chat_id: int | None = None,
) -> tuple[bool, str, _PendingAllowedAuthRequest | None]:
    if new_user_id <= 0:
        return False, "User ID must be a positive integer.", None
    if new_user_id in config.allowed_users:
        return False, f"`{new_user_id}` is already allowed.", None
    if scope not in {SCOPE_SINGLE_SESSION, SCOPE_CREATE_SESSIONS}:
        return False, "Invalid scope.", None
    if scope == SCOPE_SINGLE_SESSION and (
        bind_thread_id is None or not isinstance(bind_window_id, str) or not bind_window_id
    ):
        return False, "Single-session add must target a bound topic/session.", None
    request = _queue_allowed_auth_request(
        action="add",
        requested_by=requested_by,
        target_user_id=new_user_id,
        target_name=name,
        target_scope=scope,
        bind_thread_id=bind_thread_id,
        bind_window_id=bind_window_id,
        bind_chat_id=bind_chat_id,
    )
    return True, "", request


def _queue_allowed_remove_request(
    *,
    requested_by: int,
    target_user_id: int,
) -> tuple[bool, str, _PendingAllowedAuthRequest | None]:
    if target_user_id == requested_by:
        return False, "You cannot remove your own user ID.", None
    if target_user_id not in config.allowed_users:
        return False, "User is not currently allowed.", None
    request = _queue_allowed_auth_request(
        action="remove",
        requested_by=requested_by,
        target_user_id=target_user_id,
        target_scope=SCOPE_SINGLE_SESSION,
    )
    return True, "", request


def _queue_allowed_add_batch_request(
    *,
    requested_by: int,
    targets: list[_PendingAllowedAddTarget],
) -> tuple[bool, str, _PendingAllowedAuthRequest | None]:
    if not targets:
        return False, "No users selected.", None
    validated: list[_PendingAllowedAddTarget] = []
    seen_ids: set[int] = set()
    for target in targets:
        if target.user_id <= 0:
            return False, "User ID must be a positive integer.", None
        if target.user_id in seen_ids:
            continue
        seen_ids.add(target.user_id)
        if target.user_id in config.allowed_users:
            continue
        if target.scope not in {SCOPE_SINGLE_SESSION, SCOPE_CREATE_SESSIONS}:
            return False, "Invalid scope.", None
        if target.scope == SCOPE_SINGLE_SESSION and (
            target.bind_thread_id is None
            or not isinstance(target.bind_window_id, str)
            or not target.bind_window_id
        ):
            return False, "Single-session add must target a bound topic/session.", None
        validated.append(target)
    if not validated:
        return False, "All selected users are already allowed.", None
    request = _queue_allowed_auth_request(
        action="add_batch",
        requested_by=requested_by,
        batch_add_targets=tuple(validated),
    )
    return True, "", request


def _build_allowed_auth_dm_text(request: _PendingAllowedAuthRequest) -> str:
    expires_in = _format_duration_brief(max(1, int(request.expires_at - time.time())))
    if request.action == "add_batch":
        summary_lines = [
            "🔐 *Allowed-User Approval Token*",
            "",
            f"Requested by: `{request.requested_by}`",
            "",
            "Batch add request:",
        ]
        for target in request.batch_add_targets[:12]:
            label = f"{target.name} ({target.user_id})" if target.name else str(target.user_id)
            summary_lines.append(f"• {label} — {_format_scope_label(target.scope)}")
        extra = len(request.batch_add_targets) - 12
        if extra > 0:
            summary_lines.append(f"• … +{extra} more")
        summary_lines.extend(
            [
                "",
                f"Token: `{request.token}`",
                f"Expires in: `{expires_in}`",
                "",
                "Paste this in the target group/topic:",
                f"`/allowed approve {request.token}`",
            ]
        )
        return "\n".join(summary_lines)
    if request.action == "add":
        action_line = (
            f"Add `{request.target_user_id}`"
            f" ({request.target_name})"
            if request.target_name
            else f"Add `{request.target_user_id}`"
        )
        action_line += f" with scope: {_format_scope_label(request.target_scope)}"
    else:
        action_line = f"Remove `{request.target_user_id}`"
    return (
        "🔐 *Allowed-User Approval Token*\n\n"
        f"Request: {action_line}\n"
        f"Requested by: `{request.requested_by}`\n\n"
        f"Token: `{request.token}`\n"
        f"Expires in: `{expires_in}`\n\n"
        "Paste this in the target group/topic:\n"
        f"`/allowed approve {request.token}`"
    )


async def _notify_allowed_auth_token(
    *,
    bot: Bot,
    request: _PendingAllowedAuthRequest,
) -> tuple[int, int]:
    admin_ids = sorted(_get_allowed_admins() or config.allowed_users)
    delivered = 0
    for admin_id in admin_ids:
        try:
            await safe_send(bot, admin_id, _build_allowed_auth_dm_text(request))
            delivered += 1
        except Exception:
            logger.debug("Failed to deliver allowed-auth token DM to %s", admin_id)
    return delivered, len(admin_ids)


def _apply_allowed_auth_request_token(
    token: str,
    *,
    acting_user_id: int,
) -> tuple[bool, str]:
    _purge_expired_allowed_auth_requests()
    normalized = _normalize_allowed_auth_token(token)
    request = _PENDING_ALLOWED_AUTH_REQUESTS.pop(normalized, None)
    if request is None:
        return False, "Invalid or expired approval token."

    if request.action == "add_batch":
        added: list[str] = []
        failed: list[str] = []
        for target in request.batch_add_targets:
            if target.user_id in config.allowed_users:
                continue
            ok, err = _apply_allowed_user_add(
                target.user_id,
                target.name,
                scope=target.scope,
            )
            if not ok:
                failed.append(f"{target.user_id}: {err}")
                continue
            if target.scope == SCOPE_SINGLE_SESSION:
                ok, err = _bind_user_to_single_session(
                    target_user_id=target.user_id,
                    thread_id=target.bind_thread_id,
                    window_id=target.bind_window_id,
                    chat_id=target.bind_chat_id,
                )
                if not ok:
                    _apply_allowed_user_remove(
                        target.user_id,
                        acting_user_id=acting_user_id,
                    )
                    failed.append(f"{target.user_id}: {err}")
                    continue
            added.append(str(target.user_id))

        if not added and failed:
            return False, "Batch add failed: " + "; ".join(failed[:6])
        if failed:
            return (
                True,
                f"Batch add complete. Added {len(added)} user(s): {', '.join(added[:10])}."
                f" Failed: {'; '.join(failed[:4])}",
            )
        return True, f"Batch add complete. Added {len(added)} user(s): {', '.join(added[:10])}."

    if request.action == "add":
        ok, err = _apply_allowed_user_add(
            request.target_user_id,
            request.target_name,
            scope=request.target_scope,
        )
        if not ok:
            return False, err
        if request.target_scope == SCOPE_SINGLE_SESSION:
            ok, err = _bind_user_to_single_session(
                target_user_id=request.target_user_id,
                thread_id=request.bind_thread_id,
                window_id=request.bind_window_id,
                chat_id=request.bind_chat_id,
            )
            if not ok:
                _apply_allowed_user_remove(
                    request.target_user_id,
                    acting_user_id=acting_user_id,
                )
                return False, f"{err} (allowlist change was rolled back)."
        return (
            True,
            f"Added `{request.target_user_id}` with scope: {_format_scope_label(request.target_scope)}",
        )

    if request.action == "remove":
        ok, err = _apply_allowed_user_remove(
            request.target_user_id,
            acting_user_id=acting_user_id,
        )
        if not ok:
            return False, err
        return True, f"Removed `{request.target_user_id}`."

    return False, "Unknown request action."


def _resolve_allowed_users_env_path() -> Path:
    """Resolve the env file path used to persist ALLOWED_USERS."""
    if config.auth_env_file is not None:
        return config.auth_env_file
    local_env = Path(".env")
    if local_env.is_file():
        return local_env
    return config.config_dir / ".env"


def _auth_write_hint(path: Path, err: BaseException) -> str:
    """Return a hint when auth-file writes fail due to permissions."""
    if not isinstance(err, OSError):
        return ""
    if getattr(err, "errno", None) != errno.EACCES:
        return ""

    base = (
        config.auth_env_file.parent
        if config.auth_env_file is not None
        else Path("/etc/codex/auth")
    )
    try:
        resolved_path = path.resolve(strict=False)
        resolved_base = base.resolve(strict=False)
        is_auth_path = resolved_path == resolved_base or resolved_base in resolved_path.parents
    except OSError:
        base_text = str(base)
        path_text = str(path)
        is_auth_path = path_text == base_text or path_text.startswith(base_text.rstrip("/") + "/")

    if not is_auth_path:
        return ""
    return (
        f" Hint: bot user needs write access to {base} "
        "(directory must be writable for atomic temp-file writes)."
    )


def _set_env_key_value(path: Path, key: str, value: str) -> tuple[bool, str]:
    """Upsert a KEY=value entry in an env file."""
    try:
        content = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError as e:
        return False, f"Failed to read env file {path}: {e}"

    lines = content.splitlines()
    replacement = f"{key}={value}"
    updated = False

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        left, sep, _right = line.partition("=")
        if sep and left.strip() == key:
            lines[i] = replacement
            updated = True
            break

    if not updated:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(replacement)

    new_content = "\n".join(lines)
    if lines:
        new_content += "\n"

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_content, encoding="utf-8")
    except OSError as e:
        return False, f"Failed to write env file {path}: {e}{_auth_write_hint(path, e)}"
    return True, ""


def _persist_allowed_users_set(allowed_users: set[int]) -> tuple[bool, str]:
    """Persist and apply the ALLOWED_USERS set."""
    if not allowed_users:
        return False, "Refusing to persist an empty allowlist."

    serialized = ",".join(str(uid) for uid in sorted(allowed_users))
    env_path = _resolve_allowed_users_env_path()
    ok, err = _set_env_key_value(env_path, "ALLOWED_USERS", serialized)
    if not ok:
        return False, err

    config.allowed_users = set(allowed_users)
    os.environ["ALLOWED_USERS"] = serialized
    _invalidate_allowed_users_meta_cache()
    return True, ""


def _invalidate_allowed_users_meta_cache() -> None:
    """Invalidate in-memory allowlist metadata cache."""
    global _ALLOWED_USERS_META_CACHE
    global _ALLOWED_USERS_META_CACHE_FINGERPRINT
    _ALLOWED_USERS_META_CACHE = None
    _ALLOWED_USERS_META_CACHE_FINGERPRINT = None


def _allowed_users_meta_fingerprint() -> tuple[tuple[int, ...], str, int, int, int]:
    """Build a cheap fingerprint for cache validation."""
    allowed_fingerprint = tuple(sorted(config.allowed_users))
    path_fingerprint = str(_ALLOWED_USERS_META_FILE)
    try:
        stat = _ALLOWED_USERS_META_FILE.stat()
        return (
            allowed_fingerprint,
            path_fingerprint,
            stat.st_ino,
            stat.st_mtime_ns,
            stat.st_size,
        )
    except OSError:
        return (allowed_fingerprint, path_fingerprint, -1, -1, -1)


def _clone_allowed_users_meta(
    names: dict[int, str],
    admins: set[int],
    scopes: dict[int, str],
) -> tuple[dict[int, str], set[int], dict[int, str]]:
    return dict(names), set(admins), dict(scopes)


def _load_allowed_users_meta_raw() -> dict[str, object]:
    """Load raw allowed-user metadata payload from disk."""
    if not _ALLOWED_USERS_META_FILE.is_file():
        return {}
    try:
        payload = json.loads(_ALLOWED_USERS_META_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug(
            "Failed reading allowed users metadata %s: %s",
            _ALLOWED_USERS_META_FILE,
            e,
        )
        return {}
    return payload if isinstance(payload, dict) else {}


def _normalize_allowed_users_meta(
    raw: dict[str, object],
) -> tuple[dict[int, str], set[int], dict[int, str], bool]:
    """Normalize allowed-user metadata against current allowlist."""
    changed = False

    # Backward-compatible names-only format.
    raw_names = raw.get("names", raw) if isinstance(raw, dict) else {}
    names: dict[int, str] = {}
    if isinstance(raw_names, dict):
        for raw_uid, raw_name in raw_names.items():
            try:
                uid = int(raw_uid)
            except (TypeError, ValueError):
                changed = True
                continue
            if not isinstance(raw_name, str):
                changed = True
                continue
            clean = raw_name.strip()
            if clean:
                names[uid] = clean

    raw_admins = raw.get("admins", [])
    admins: set[int] = set()
    if isinstance(raw_admins, list):
        for value in raw_admins:
            try:
                admins.add(int(value))
            except (TypeError, ValueError):
                changed = True
    elif raw_admins:
        changed = True

    raw_scopes = raw.get("scopes", {})
    scopes: dict[int, str] = {}
    if isinstance(raw_scopes, dict):
        for raw_uid, raw_scope in raw_scopes.items():
            try:
                uid = int(raw_uid)
            except (TypeError, ValueError):
                changed = True
                continue
            scope = (
                raw_scope
                if isinstance(raw_scope, str)
                and raw_scope in {SCOPE_SINGLE_SESSION, SCOPE_CREATE_SESSIONS}
                else ""
            )
            if not scope:
                changed = True
                continue
            scopes[uid] = scope
    elif raw_scopes:
        changed = True

    # Keep metadata for allowed users only.
    for uid in list(names):
        if uid not in config.allowed_users:
            names.pop(uid, None)
            changed = True
    admins = {uid for uid in admins if uid in config.allowed_users}

    for uid in list(scopes):
        if uid not in config.allowed_users:
            scopes.pop(uid, None)
            changed = True

    for uid, default_name in _DEFAULT_ALLOWED_USER_NAMES.items():
        if uid in config.allowed_users and uid not in names:
            names[uid] = default_name
            changed = True

    for uid in _DEFAULT_ALLOWED_ADMIN_IDS:
        if uid in config.allowed_users and uid not in admins:
            admins.add(uid)
            changed = True

    if config.allowed_users and not admins:
        admins.add(min(config.allowed_users))
        changed = True

    for uid in config.allowed_users:
        if uid not in scopes:
            scopes[uid] = (
                SCOPE_CREATE_SESSIONS if uid in admins else SCOPE_SINGLE_SESSION
            )
            changed = True
    return names, admins, scopes, changed


def _save_allowed_users_meta(
    names: dict[int, str],
    admins: set[int],
    scopes: dict[int, str],
) -> tuple[bool, str]:
    """Persist allowed-user metadata."""
    payload: dict[str, object] = {
        "names": {
            str(uid): name
            for uid, name in sorted(names.items())
            if uid in config.allowed_users and isinstance(name, str) and name.strip()
        },
        "admins": sorted(uid for uid in admins if uid in config.allowed_users),
        "scopes": {
            str(uid): scope
            for uid, scope in sorted(scopes.items())
            if uid in config.allowed_users
            and scope in {SCOPE_SINGLE_SESSION, SCOPE_CREATE_SESSIONS}
        },
    }
    try:
        atomic_write_json(_ALLOWED_USERS_META_FILE, payload, indent=2)
    except OSError as e:
        return (
            False,
            f"Failed to write {_ALLOWED_USERS_META_FILE}: {e}"
            f"{_auth_write_hint(_ALLOWED_USERS_META_FILE, e)}",
        )
    except Exception as e:
        return False, f"Failed to write {_ALLOWED_USERS_META_FILE}: {e}"
    _invalidate_allowed_users_meta_cache()
    return True, ""


def _load_allowed_users_meta() -> tuple[dict[int, str], set[int], dict[int, str]]:
    """Load normalized allowed-user metadata without mutating auth files."""
    global _ALLOWED_USERS_META_CACHE
    global _ALLOWED_USERS_META_CACHE_FINGERPRINT

    fingerprint = _allowed_users_meta_fingerprint()
    if (
        _ALLOWED_USERS_META_CACHE is not None
        and _ALLOWED_USERS_META_CACHE_FINGERPRINT == fingerprint
    ):
        cached_names, cached_admins, cached_scopes = _ALLOWED_USERS_META_CACHE
        return _clone_allowed_users_meta(cached_names, cached_admins, cached_scopes)

    raw = _load_allowed_users_meta_raw()
    names, admins, scopes, changed = _normalize_allowed_users_meta(raw)
    if changed:
        logger.debug(
            "Allowed-user metadata needed normalization; keeping repairs in memory only."
        )

    _ALLOWED_USERS_META_CACHE = _clone_allowed_users_meta(names, admins, scopes)
    _ALLOWED_USERS_META_CACHE_FINGERPRINT = _allowed_users_meta_fingerprint()
    return _clone_allowed_users_meta(names, admins, scopes)


def _get_allowed_user_names() -> dict[int, str]:
    """Return allowed-user name mapping."""
    names, _admins, _scopes = _load_allowed_users_meta()
    return names


def _get_allowed_admins() -> set[int]:
    """Return admin user ids."""
    _names, admins, _scopes = _load_allowed_users_meta()
    return admins


def _get_allowed_scopes() -> dict[int, str]:
    """Return per-user scope mapping."""
    _names, _admins, scopes = _load_allowed_users_meta()
    return scopes


def _is_admin_user(user_id: int | None) -> bool:
    """Return whether a user is an allowlist admin."""
    if user_id is None or user_id not in config.allowed_users:
        return False
    return user_id in _get_allowed_admins()


def _get_user_scope(user_id: int) -> str:
    """Return effective scope for an allowed user."""
    scopes = _get_allowed_scopes()
    return scopes.get(
        user_id,
        SCOPE_CREATE_SESSIONS if _is_admin_user(user_id) else SCOPE_SINGLE_SESSION,
    )


def _can_user_create_sessions(user_id: int | None) -> bool:
    """Return whether user may create/bind new sessions."""
    if user_id is None or user_id not in config.allowed_users:
        return False
    if _is_admin_user(user_id):
        return True
    return _get_user_scope(user_id) == SCOPE_CREATE_SESSIONS


def _set_user_scope(user_id: int, scope: str) -> tuple[bool, str]:
    """Set per-user scope in metadata."""
    if scope not in {SCOPE_SINGLE_SESSION, SCOPE_CREATE_SESSIONS}:
        return False, "Invalid scope."
    if user_id not in config.allowed_users:
        return False, "User is not currently allowed."
    names, admins, scopes = _load_allowed_users_meta()
    scopes[user_id] = scope
    return _save_allowed_users_meta(names, admins, scopes)


def _bind_user_to_single_session(
    *,
    target_user_id: int,
    thread_id: int | None,
    window_id: str | None,
    chat_id: int | None,
) -> tuple[bool, str]:
    """Bind a user to a specific thread/window for single-session access."""
    if thread_id is None or not window_id:
        return False, "Missing target thread/window for single-session access."
    session_manager.bind_thread(
        target_user_id,
        thread_id,
        window_id,
        window_name=session_manager.get_display_name(window_id),
        chat_id=chat_id,
    )
    if chat_id is not None:
        session_manager.set_group_chat_id(target_user_id, thread_id, chat_id)
    return True, ""


def _format_scope_label(scope: str) -> str:
    if scope == SCOPE_CREATE_SESSIONS:
        return "can create sessions"
    return "single session"


def _format_allowed_user_label(user_id: int, names: dict[int, str]) -> str:
    """Format one allowlist entry label."""
    name = names.get(user_id, "")
    if name:
        return f"{name} ({user_id})"
    return str(user_id)


def _build_allowed_overview_text(current_user_id: int) -> str:
    """Build /allowed overview text."""
    names, admins, scopes = _load_allowed_users_meta()
    _purge_expired_allowed_auth_requests()
    pending_count = len(_PENDING_ALLOWED_AUTH_REQUESTS)
    lines = [
        "👥 *Allowed Users*",
        "",
        f"Your ID (locked): `{current_user_id}`",
        "",
        f"Role: {'admin' if current_user_id in admins else 'member'}",
        "",
        _ALLOWED_AUTH_APPROVAL_GUIDE,
        "",
        f"Pending auth requests: `{pending_count}`",
        "",
        "Current allowlist:",
    ]
    for uid in sorted(config.allowed_users):
        label = _format_allowed_user_label(uid, names)
        role = "admin" if uid in admins else "member"
        scope = _format_scope_label(scopes.get(uid, SCOPE_SINGLE_SESSION))
        suffix = " (you, locked)" if uid == current_user_id else ""
        lines.append(f"• {label} — {role}, {scope}{suffix}")
    return "\n".join(lines)


def _build_allowed_overview_keyboard(current_user_id: int) -> InlineKeyboardMarkup:
    """Build /allowed overview action buttons."""
    if _is_admin_user(current_user_id):
        rows = [
            [
                InlineKeyboardButton("Select Members", callback_data=CB_ALLOWED_ADD),
                InlineKeyboardButton(
                    "Request Remove", callback_data=CB_ALLOWED_REMOVE_MENU
                ),
            ],
            [InlineKeyboardButton("Refresh", callback_data=CB_ALLOWED_REFRESH)],
        ]
    else:
        rows = [[InlineKeyboardButton("Refresh", callback_data=CB_ALLOWED_REFRESH)]]
    return InlineKeyboardMarkup(rows)


def _build_allowed_add_mode_keyboard() -> InlineKeyboardMarkup:
    """Build role selector keyboard for selected add targets."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Single Session", callback_data=CB_ALLOWED_ADD_SINGLE
                )
            ],
            [
                InlineKeyboardButton(
                    "Can Create Sessions", callback_data=CB_ALLOWED_ADD_CREATE
                )
            ],
            [InlineKeyboardButton("Back", callback_data=CB_ALLOWED_BACK)],
        ]
    )


def _build_allowed_remove_text(current_user_id: int) -> str:
    """Build remove-menu text."""
    names = _get_allowed_user_names()
    removable = [uid for uid in sorted(config.allowed_users) if uid != current_user_id]
    lines = [
        "🧹 *Remove Allowed User*",
        "",
        f"Your ID (locked): `{current_user_id}`",
    ]
    if removable:
        lines.append("")
        lines.append("Select a user to remove:")
        for uid in removable:
            lines.append(f"• {_format_allowed_user_label(uid, names)}")
    else:
        lines.append("")
        lines.append("No removable users found.")
    return "\n".join(lines)


def _build_allowed_remove_keyboard(current_user_id: int) -> InlineKeyboardMarkup:
    """Build remove-menu keyboard excluding current user."""
    names = _get_allowed_user_names()
    removable = [uid for uid in sorted(config.allowed_users) if uid != current_user_id]

    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for uid in removable:
        label = names.get(uid, str(uid))
        display = label[:18] + "…" if len(label) > 19 else label
        row.append(
            InlineKeyboardButton(
                f"➖ {display}",
                callback_data=f"{CB_ALLOWED_REMOVE}{uid}",
            )
        )
        if len(row) >= 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    rows.append(
        [
            InlineKeyboardButton("Back", callback_data=CB_ALLOWED_BACK),
            InlineKeyboardButton("Refresh", callback_data=CB_ALLOWED_REFRESH),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _parse_allowed_add_input(text: str) -> tuple[int | None, str]:
    """Parse '<user_id> [name]' input for /allowed add flow."""
    stripped = text.strip()
    if not stripped:
        return None, ""
    parts = stripped.split(maxsplit=1)
    if not parts[0].isdigit():
        return None, ""
    uid = int(parts[0])
    name = parts[1].strip() if len(parts) > 1 else ""
    return uid, name


def _apply_allowed_user_add(
    new_user_id: int,
    name: str,
    *,
    scope: str = SCOPE_SINGLE_SESSION,
) -> tuple[bool, str]:
    """Add a user to allowlist and persist metadata."""
    if new_user_id <= 0:
        return False, "User ID must be a positive integer."
    if scope not in {SCOPE_SINGLE_SESSION, SCOPE_CREATE_SESSIONS}:
        return False, "Invalid scope."

    allowed = set(config.allowed_users)
    allowed.add(new_user_id)
    ok, err = _persist_allowed_users_set(allowed)
    if not ok:
        return False, err

    names, admins, scopes = _load_allowed_users_meta()
    clean_name = name.strip()
    if clean_name:
        names[new_user_id] = clean_name
    scopes[new_user_id] = scope
    ok, err = _save_allowed_users_meta(names, admins, scopes)
    if not ok:
        return False, err
    return True, ""


def _set_allowed_user_name(target_user_id: int, name: str) -> tuple[bool, str]:
    """Set or update display name for an existing allowed user."""
    if target_user_id not in config.allowed_users:
        return False, "User is not currently allowed."
    clean_name = name.strip()
    if not clean_name:
        return False, "Name cannot be empty."

    names, admins, scopes = _load_allowed_users_meta()
    names[target_user_id] = clean_name
    ok, err = _save_allowed_users_meta(names, admins, scopes)
    if not ok:
        return False, err
    return True, ""


def _apply_allowed_user_remove(
    target_user_id: int, *, acting_user_id: int
) -> tuple[bool, str]:
    """Remove a user from allowlist, except the acting user."""
    if target_user_id == acting_user_id:
        return False, "You cannot remove your own user ID."
    if target_user_id not in config.allowed_users:
        return False, "User is not currently allowed."

    allowed = set(config.allowed_users)
    allowed.discard(target_user_id)
    ok, err = _persist_allowed_users_set(allowed)
    if not ok:
        return False, err

    names, admins, scopes = _load_allowed_users_meta()
    names.pop(target_user_id, None)
    admins.discard(target_user_id)
    scopes.pop(target_user_id, None)
    ok, err = _save_allowed_users_meta(names, admins, scopes)
    if not ok:
        return False, err

    # Drop all thread bindings for removed user.
    removed_thread_ids = [
        (chat_id, thread_id)
        for uid, chat_id, thread_id, _window_id in session_manager.iter_topic_window_bindings()
        if uid == target_user_id
    ]
    for chat_id, thread_id in removed_thread_ids:
        session_manager.unbind_thread(target_user_id, thread_id, chat_id=chat_id)
    return True, ""


def _normalize_approval_mode(raw: str | None) -> str | None:
    """Normalize user-facing approval mode strings."""
    if raw is None:
        return None
    mode = raw.strip().lower()
    aliases = {
        "default": APPROVAL_MODE_INHERIT,
        "inherited": APPROVAL_MODE_INHERIT,
        "on_request": APPROVAL_MODE_ON_REQUEST,
        "onrequest": APPROVAL_MODE_ON_REQUEST,
        "agent": APPROVAL_MODE_FULL_AUTO,
        "agent_mode": APPROVAL_MODE_FULL_AUTO,
        "agent-mode": APPROVAL_MODE_FULL_AUTO,
        "full_auto": APPROVAL_MODE_FULL_AUTO,
        "fullauto": APPROVAL_MODE_FULL_AUTO,
        "bypass": APPROVAL_MODE_DANGEROUS,
        "danger": APPROVAL_MODE_DANGEROUS,
        "yolo": APPROVAL_MODE_DANGEROUS,
    }
    mode = aliases.get(mode, mode)
    if mode in APPROVAL_MODE_ORDER:
        return mode
    return None


def _approval_mode_button_label(mode: str) -> str:
    labels = {
        APPROVAL_MODE_INHERIT: "Inherit",
        APPROVAL_MODE_ON_REQUEST: "On Request",
        APPROVAL_MODE_UNTRUSTED: "Untrusted",
        APPROVAL_MODE_NEVER: "Never Ask",
        APPROVAL_MODE_FULL_AUTO: "Agent",
        APPROVAL_MODE_DANGEROUS: "Dangerous",
    }
    return labels.get(mode, mode)


def _approval_mode_display_text(mode: str) -> str:
    display = {
        APPROVAL_MODE_INHERIT: "inherit (assistant default)",
        APPROVAL_MODE_ON_REQUEST: "on-request",
        APPROVAL_MODE_UNTRUSTED: "untrusted",
        APPROVAL_MODE_NEVER: "never",
        APPROVAL_MODE_FULL_AUTO: "agent (full-auto)",
        APPROVAL_MODE_DANGEROUS: "dangerously-bypass-approvals-and-sandbox",
    }
    return display.get(mode, mode)


def _get_app_default_approval_mode() -> str:
    """Return the app-wide default approval mode."""
    stored = _normalize_approval_mode(session_manager.get_default_approval_mode())
    if stored:
        return stored
    return _infer_approval_mode_from_command(config.assistant_command)


def _set_app_default_approval_mode(mode: str) -> None:
    """Persist app-wide default approval mode."""
    normalized = _normalize_approval_mode(mode) or APPROVAL_MODE_INHERIT
    persisted = "" if normalized == APPROVAL_MODE_INHERIT else normalized
    session_manager.set_default_approval_mode(persisted)


def _get_window_approval_override(window_id: str) -> str:
    """Return stored per-window approval override (inherit when unset)."""
    stored = _normalize_approval_mode(session_manager.get_window_approval_mode(window_id))
    return stored or APPROVAL_MODE_INHERIT


def _strip_codex_policy_flags(
    args: list[str],
    *,
    drop_sandbox: bool = False,
) -> list[str]:
    """Strip Codex approval/sandbox policy flags from an argv list."""
    cleaned: list[str] = []
    i = 0
    while i < len(args):
        token = args[i]
        if token in {"-a", "--ask-for-approval"}:
            i += 2
            continue
        if token.startswith("--ask-for-approval="):
            i += 1
            continue
        if token in {
            "--full-auto",
            "--dangerously-bypass-approvals-and-sandbox",
        }:
            i += 1
            continue
        if drop_sandbox and token in {"-s", "--sandbox"}:
            i += 2
            continue
        if drop_sandbox and token.startswith("--sandbox="):
            i += 1
            continue
        cleaned.append(token)
        i += 1
    return cleaned


def _infer_approval_mode_from_args(args: list[str]) -> str:
    """Infer approval mode from Codex argv."""
    for i, token in enumerate(args):
        if token == "--dangerously-bypass-approvals-and-sandbox":
            return APPROVAL_MODE_DANGEROUS
        if token == "--full-auto":
            return APPROVAL_MODE_FULL_AUTO
        if token in {"-a", "--ask-for-approval"} and i + 1 < len(args):
            mode = _normalize_approval_mode(args[i + 1])
            if mode in {
                APPROVAL_MODE_UNTRUSTED,
                APPROVAL_MODE_ON_REQUEST,
                APPROVAL_MODE_NEVER,
            }:
                return mode
        if token.startswith("--ask-for-approval="):
            _left, _sep, value = token.partition("=")
            mode = _normalize_approval_mode(value)
            if mode in {
                APPROVAL_MODE_UNTRUSTED,
                APPROVAL_MODE_ON_REQUEST,
                APPROVAL_MODE_NEVER,
            }:
                return mode
    return APPROVAL_MODE_INHERIT


def _infer_approval_mode_from_command(command: str) -> str:
    """Infer approval mode from assistant command string."""
    try:
        args = shlex.split(command)
    except ValueError:
        return APPROVAL_MODE_INHERIT
    return _infer_approval_mode_from_args(args)


def _get_window_approval_mode(window_id: str) -> str:
    """Return effective approval mode for a window."""
    window_override = _get_window_approval_override(window_id)
    if window_override != APPROVAL_MODE_INHERIT:
        return window_override
    return _get_app_default_approval_mode()


def _set_window_approval_mode(window_id: str, mode: str) -> None:
    """Persist per-window approval mode override (or clear to inherit)."""
    normalized = _normalize_approval_mode(mode) or APPROVAL_MODE_INHERIT
    persisted = "" if normalized == APPROVAL_MODE_INHERIT else normalized
    session_manager.set_window_approval_mode(window_id, persisted)


def _build_assistant_args_for_approval_mode(mode: str) -> list[str]:
    """Build assistant argv with selected approval mode override applied."""
    try:
        args = shlex.split(config.assistant_command)
    except ValueError:
        args = []

    if not args:
        fallback = config.assistant_command.strip() or "codex"
        args = [fallback]

    normalized = _normalize_approval_mode(mode) or APPROVAL_MODE_INHERIT
    if normalized == APPROVAL_MODE_INHERIT:
        normalized = _get_app_default_approval_mode()
    if normalized == APPROVAL_MODE_INHERIT:
        return args

    drop_sandbox = normalized in {
        APPROVAL_MODE_FULL_AUTO,
        APPROVAL_MODE_DANGEROUS,
    }
    stripped = _strip_codex_policy_flags(args, drop_sandbox=drop_sandbox)
    if normalized == APPROVAL_MODE_FULL_AUTO:
        stripped.append("--full-auto")
    elif normalized == APPROVAL_MODE_DANGEROUS:
        stripped.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        stripped.extend(["--ask-for-approval", normalized])
    return stripped


def _is_codex_command_args(args: list[str]) -> bool:
    """Best-effort detection that assistant argv launches Codex CLI."""
    if not args:
        return False
    first = Path(args[0]).name.lower()
    if "codex" in first:
        return True
    if first in {"node", "nodejs"}:
        for token in args[1:4]:
            if "codex" in token.lower():
                return True
    return False


def _build_assistant_launch_command(
    mode: str,
    *,
    resume_session_id: str = "",
) -> str:
    """Build shell command to launch assistant with approval mode override."""
    args = _build_assistant_args_for_approval_mode(mode)
    sid = resume_session_id.strip()
    if (
        sid
        and config.session_provider == "codex"
        and _is_codex_command_args(args)
        and "resume" not in args
    ):
        args = [*args, "resume", sid]
    return shlex.join(args)


def _probe_workspace_write_access(
    workspace_dir: str | None,
) -> tuple[str, bool, str | None]:
    """Best-effort probe for write access in the active workspace directory."""
    candidate = (workspace_dir or "").strip()
    path = Path(candidate).expanduser() if candidate else Path.cwd()
    if path.exists() and path.is_file():
        path = path.parent

    if not path.exists():
        return str(path), False, "Directory does not exist"

    probe_file = path / f".coco_write_probe_{os.getpid()}_{int(time.time() * 1000)}"
    try:
        probe_file.write_text("ok", encoding="utf-8")
        probe_file.unlink()
        return str(path), True, None
    except OSError as exc:
        try:
            if probe_file.exists():
                probe_file.unlink()
        except OSError:
            pass
        reason = exc.strerror or str(exc)
        return str(path), False, reason


def _get_browse_root_path(
    user_data: dict | None,
    *,
    chat_id: int | None = None,
) -> Path:
    """Return the configured browse root for this interaction."""
    if user_data:
        raw_root = user_data.get(BROWSE_ROOT_KEY)
        if isinstance(raw_root, str) and raw_root.strip():
            return resolve_browse_root(raw_root)
    return resolve_browse_root(config.resolve_browse_root_for_chat(chat_id))


def _get_browse_current_path(
    user_data: dict | None,
    *,
    chat_id: int | None = None,
) -> tuple[Path, Path]:
    """Return (current_path, root_path) clamped to browse root."""
    root = _get_browse_root_path(user_data, chat_id=chat_id)
    if user_data:
        raw_current = user_data.get(BROWSE_PATH_KEY, str(root))
    else:
        raw_current = str(root)
    current = clamp_browse_path(str(raw_current), root)
    return current, root


def _local_machine_identity() -> tuple[str, str]:
    node = node_registry.get_node(node_registry.local_machine_id)
    if node is not None:
        return node.machine_id, node.display_name
    machine_id = config.machine_id.strip()
    machine_name = config.machine_name.strip() or machine_id
    return machine_id, machine_name


def _sorted_machine_choices() -> list[object]:
    node_registry.ensure_local_node()
    local_machine_id, _ = _local_machine_identity()
    nodes = node_registry.iter_nodes()
    return sorted(
        nodes,
        key=lambda node: (
            0 if node.machine_id == local_machine_id else 1,
            0 if node.status == "online" else 1,
            node.display_name.lower(),
            node.machine_id,
        ),
    )


def _build_machine_picker_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for node in _sorted_machine_choices():
        if node.status != "online":
            continue
        label = node.display_name
        if node.machine_id == config.machine_id:
            label = f"{label} (Local)"
        rows.append(
            [
                InlineKeyboardButton(
                    label[:64],
                    callback_data=f"{CB_DIR_MACHINE_SELECT}{node.machine_id}"[:64],
                )
            ]
        )
    rows.append([InlineKeyboardButton("Cancel", callback_data=CB_DIR_CANCEL)])
    return InlineKeyboardMarkup(rows)


def _build_machine_picker_text() -> str:
    lines = [
        "*Select Machine*",
        "",
        "Choose where this topic should run:",
    ]
    offline_nodes: list[str] = []
    for node in _sorted_machine_choices():
        if node.status == "online":
            status = "online"
        else:
            status = f"{node.status} (unavailable)"
            offline_nodes.append(f"- `{node.display_name}`")
        scope = "local" if node.machine_id == config.machine_id else "remote"
        lines.append(f"- `{node.display_name}` [{scope}, {status}]")
    if offline_nodes:
        lines.extend(["", "Offline machines are shown for reference only."])
    return "\n".join(lines)


async def _open_machine_picker(
    *,
    context_user_data: dict | None,
    thread_id: int | None,
    chat_id: int | None,
) -> tuple[str, InlineKeyboardMarkup]:
    if context_user_data is not None:
        context_user_data[STATE_KEY] = STATE_PICKING_MACHINE
        context_user_data["_pending_thread_id"] = thread_id
        context_user_data[BROWSE_MACHINE_KEY] = ""
        context_user_data[BROWSE_MACHINE_NAME_KEY] = ""
    return _build_machine_picker_text(), _build_machine_picker_keyboard()


async def _load_remote_browse_state(
    context_user_data: dict | None,
    *,
    machine_id: str,
    current_path: str,
    chat_id: int | None,
) -> tuple[str, str, list[str]]:
    from .agent_rpc import agent_rpc_client

    payload = await agent_rpc_client.browse(
        machine_id,
        current_path=current_path,
        chat_id=chat_id,
    )
    root_path = str(payload.get("root_path", "")).strip()
    resolved_current = str(payload.get("current_path", "")).strip() or root_path
    subdirs = payload.get("subdirs", [])
    normalized_subdirs = [
        item.strip() for item in subdirs if isinstance(item, str) and item.strip()
    ]
    if context_user_data is not None:
        context_user_data[BROWSE_ROOT_KEY] = root_path
        context_user_data[BROWSE_PATH_KEY] = resolved_current
        context_user_data[BROWSE_DIRS_KEY] = normalized_subdirs
    return resolved_current, root_path, normalized_subdirs


async def _build_directory_browser_for_context(
    context_user_data: dict | None,
    *,
    chat_id: int | None,
    page: int = 0,
) -> tuple[str, InlineKeyboardMarkup, list[str]]:
    machine_id = ""
    if context_user_data is not None:
        raw_machine_id = context_user_data.get(BROWSE_MACHINE_KEY, "")
        if isinstance(raw_machine_id, str):
            machine_id = raw_machine_id.strip()
    local_machine_id, _local_machine_name = _local_machine_identity()
    if machine_id and machine_id != local_machine_id:
        current_path = ""
        if context_user_data is not None:
            raw_current = context_user_data.get(BROWSE_PATH_KEY, "")
            if isinstance(raw_current, str):
                current_path = raw_current.strip()
        if not current_path:
            current_path = ""
        current_path, root_path, subdirs = await _load_remote_browse_state(
            context_user_data,
            machine_id=machine_id,
            current_path=current_path,
            chat_id=chat_id,
        )
        return build_directory_browser(
            current_path,
            page,
            root_path=root_path,
            subdirs_override=subdirs,
            allow_new_folder=False,
        )

    current_path, root_path = _get_browse_current_path(context_user_data, chat_id=chat_id)
    return build_directory_browser(
        str(current_path),
        page,
        root_path=str(root_path),
    )


def _build_approvals_text(
    current_user_id: int,
    window_id: str,
    *,
    workspace_dir: str | None = None,
    defaults_view: bool = False,
) -> str:
    """Build /approvals panel text for one bound session."""
    app_default_mode = _get_app_default_approval_mode()
    window_override_mode = _get_window_approval_override(window_id)
    effective_mode = _get_window_approval_mode(window_id)
    override_display = (
        "inherit (use app default)"
        if window_override_mode == APPROVAL_MODE_INHERIT
        else _approval_mode_display_text(window_override_mode)
    )
    display = session_manager.get_display_name(window_id)
    checked_path, can_write, write_error = _probe_workspace_write_access(workspace_dir)
    write_state = "writable" if can_write else "not writable"
    panel_label = "app default" if defaults_view else "session override"
    lines = [
        "🛂 *Session Approvals*",
        "",
        f"Session: `{display}`",
        f"Your ID: `{current_user_id}`",
        f"Panel: `{panel_label}`",
        f"App default: `{_approval_mode_display_text(app_default_mode)}`",
        f"Window override: `{override_display}`",
        f"Effective mode: `{_approval_mode_display_text(effective_mode)}`",
        f"Workspace path: `{checked_path}`",
        f"Runtime write check: `{write_state}`",
        "",
        (
            "Changing mode applies to new turns immediately."
            if _codex_app_server_enabled()
            else "Changing mode restarts Codex in this topic."
        ),
        (
            "This panel changes app-wide defaults for inherited topics."
            if defaults_view
            else "This panel changes this topic/session override."
        ),
        (
            "Use `Session` to return to topic-level overrides."
            if defaults_view
            else "Use `Defaults` to adjust app-wide defaults."
        ),
        "Refresh reruns the write check.",
        "Only admins can change this setting.",
    ]
    if write_error:
        lines.insert(9, f"Write error: `{write_error}`")
    return "\n".join(lines)


def _build_approvals_keyboard(
    window_id: str,
    *,
    defaults_view: bool = False,
    can_use_dangerous: bool = False,
) -> InlineKeyboardMarkup:
    """Build approvals keyboard for either session override or app default panel."""
    current_override = _get_window_approval_override(window_id)
    current_default = _get_app_default_approval_mode()
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    allowed_modes = [
        mode
        for mode in APPROVAL_MODE_ORDER
        if can_use_dangerous or mode != APPROVAL_MODE_DANGEROUS
    ]

    if defaults_view:
        rows.append(
            [InlineKeyboardButton("Session", callback_data=CB_APPROVAL_OPEN_WINDOW)]
        )
        selected_mode = current_default
        callback_prefix = CB_APPROVAL_SET_DEFAULT
        refresh_cb = CB_APPROVAL_REFRESH_DEFAULT
    else:
        rows.append(
            [InlineKeyboardButton("Defaults", callback_data=CB_APPROVAL_OPEN_DEFAULTS)]
        )
        selected_mode = current_override
        callback_prefix = CB_APPROVAL_SET
        refresh_cb = CB_APPROVAL_REFRESH

    for mode in allowed_modes:
        label = _approval_mode_button_label(mode)
        if mode == selected_mode:
            label = f"✅ {label}"
        row.append(
            InlineKeyboardButton(
                label,
                callback_data=f"{callback_prefix}{mode}"[:64],
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    rows.append([InlineKeyboardButton("Refresh", callback_data=refresh_cb)])
    return InlineKeyboardMarkup(rows)


def _resolve_workspace_dir_for_window(
    *,
    user_id: int,
    thread_id: int | None,
    chat_id: int | None = None,
    window_id: str,
) -> str | None:
    """Resolve best-effort workspace dir from topic binding/window state."""
    binding = session_manager.resolve_topic_binding(
        user_id,
        thread_id,
        chat_id=chat_id,
    )
    if binding:
        cwd = binding.cwd.strip()
        if cwd:
            return cwd
    state = session_manager.get_window_state(window_id)
    cwd = state.cwd.strip() if isinstance(state.cwd, str) else ""
    return cwd or None


async def _resolve_live_workspace_dir_for_window(
    *,
    user_id: int,
    thread_id: int | None,
    chat_id: int | None = None,
    window_id: str,
) -> tuple[str | None, str]:
    """Resolve workspace dir for app-server transport."""
    workspace_dir = _resolve_workspace_dir_for_window(
        user_id=user_id,
        thread_id=thread_id,
        chat_id=chat_id,
        window_id=window_id,
    )
    if workspace_dir:
        return workspace_dir, ""
    return (
        None,
        "Session binding is incomplete. Send a normal message to reinitialize.",
    )


def _mode_auto_approves_app_server_requests(mode: str) -> bool:
    """Return whether app-server approvals should auto-accept for this mode."""
    return mode in {
        APPROVAL_MODE_FULL_AUTO,
        APPROVAL_MODE_DANGEROUS,
        APPROVAL_MODE_NEVER,
    }


def _new_pending_app_server_approval_token() -> str:
    """Generate a short token for app-server approval callback routing."""
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    for _ in range(24):
        token = "".join(random.choice(alphabet) for _ in range(10))
        if token not in _pending_app_server_approval:
            return token
    return f"{int(time.time() * 1000):x}"


def _register_pending_app_server_approval() -> tuple[str, asyncio.Future[object]]:
    """Create and register one pending app-server approval future."""
    loop = asyncio.get_running_loop()
    token = _new_pending_app_server_approval_token()
    fut: asyncio.Future[object] = loop.create_future()
    _pending_app_server_approval[token] = fut
    return token, fut


def _resolve_pending_app_server_approval(token: str, decision: object) -> bool:
    """Set decision for a pending app-server approval token."""
    fut = _pending_app_server_approval.get(token)
    if not fut or fut.done():
        return False
    fut.set_result(decision)
    return True


def _pop_pending_app_server_approval(token: str) -> None:
    """Remove app-server approval token from pending map."""
    _pending_app_server_approval.pop(token, None)


def _build_app_server_approval_keyboard(token: str) -> InlineKeyboardMarkup:
    """Build callback buttons for one interactive app-server approval request."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Accept",
                    callback_data=(
                        f"{CB_APP_APPROVAL_DECIDE}{token}:{APP_SERVER_APPROVAL_ACTION_ACCEPT}"
                    )[:64],
                ),
                InlineKeyboardButton(
                    "Accept Session",
                    callback_data=(
                        f"{CB_APP_APPROVAL_DECIDE}{token}:"
                        f"{APP_SERVER_APPROVAL_ACTION_ACCEPT_SESSION}"
                    )[:64],
                ),
            ],
            [
                InlineKeyboardButton(
                    "Decline",
                    callback_data=(
                        f"{CB_APP_APPROVAL_DECIDE}{token}:{APP_SERVER_APPROVAL_ACTION_DECLINE}"
                    )[:64],
                ),
                InlineKeyboardButton(
                    "Cancel Turn",
                    callback_data=(
                        f"{CB_APP_APPROVAL_DECIDE}{token}:{APP_SERVER_APPROVAL_ACTION_CANCEL}"
                    )[:64],
                ),
            ],
        ]
    )


def _parse_app_server_approval_callback(data: str) -> tuple[str, str] | None:
    """Parse app-server approval callback payload into (token, action)."""
    if not data.startswith(CB_APP_APPROVAL_DECIDE):
        return None
    rest = data[len(CB_APP_APPROVAL_DECIDE) :]
    token, sep, action = rest.partition(":")
    if not sep or not token or action not in APP_SERVER_APPROVAL_ACTION_TO_DECISION:
        return None
    return token, action


def _build_app_server_approval_text(
    method: str,
    params: dict[str, object],
    *,
    mode: str,
) -> str:
    """Render a readable Telegram prompt for app-server approval requests."""
    def _trim(value: object, *, limit: int = 260) -> str:
        if not isinstance(value, str):
            return ""
        clean = value.strip()
        if not clean:
            return ""
        if len(clean) <= limit:
            return clean
        return f"{clean[: limit - 1]}…"

    lines = [
        "🛂 *Codex Approval Request*",
        "",
        f"Mode: `{_approval_mode_display_text(mode)}`",
    ]

    if method == "item/commandExecution/requestApproval":
        lines.append("Type: `command execution`")
        command = _trim(params.get("command"), limit=320)
        cwd = _trim(params.get("cwd"))
        reason = _trim(params.get("reason"))
        if command:
            lines.append(f"Command: `{command}`")
        if cwd:
            lines.append(f"Cwd: `{cwd}`")
        if reason:
            lines.append(f"Reason: {reason}")
    else:
        lines.append("Type: `file change`")
        reason = _trim(params.get("reason"))
        grant_root = _trim(params.get("grantRoot"))
        if grant_root:
            lines.append(f"Grant root: `{grant_root}`")
        if reason:
            lines.append(f"Reason: {reason}")

    item_id = _trim(params.get("itemId"), limit=120)
    turn_id = _trim(params.get("turnId"), limit=120)
    approval_id = _trim(params.get("approvalId"), limit=120)
    if item_id:
        lines.append(f"Item: `{item_id}`")
    if approval_id:
        lines.append(f"Approval: `{approval_id}`")
    if turn_id:
        lines.append(f"Turn: `{turn_id}`")

    lines.extend(
        [
            "",
            "Choose a decision:",
            "Accept, Accept Session, Decline, or Cancel Turn.",
        ]
    )
    return "\n".join(lines)


async def _apply_window_approval_mode(window_id: str, mode: str) -> tuple[bool, str]:
    """Apply approval mode to one bound session."""
    normalized = _normalize_approval_mode(mode)
    if normalized is None:
        return False, "Unknown approval mode."

    _set_window_approval_mode(window_id, normalized)
    return True, ""


def _clear_worktree_flow_state(user_data: dict | None) -> None:
    """Clear temporary /worktree state keys."""
    if user_data is None:
        return
    if user_data.get(STATE_KEY) in {STATE_WORKTREE_NEW_NAME, STATE_WORKTREE_FOLD_SELECT}:
        user_data.pop(STATE_KEY, None)
    user_data.pop(WORKTREE_PENDING_THREAD_KEY, None)
    user_data.pop(WORKTREE_PENDING_WINDOW_ID_KEY, None)
    user_data.pop(WORKTREE_FOLD_CANDIDATES_KEY, None)
    user_data.pop(WORKTREE_FOLD_SELECTED_KEY, None)


def _clear_apps_flow_state(user_data: dict | None) -> None:
    """Clear temporary /apps looper panel input state keys."""
    if user_data is None:
        return
    if user_data.get(STATE_KEY) in {
        STATE_APPS_AUTORESEARCH_OUTCOME,
        STATE_APPS_LOOPER_PLAN_PATH,
        STATE_APPS_LOOPER_KEYWORD,
        STATE_APPS_LOOPER_INSTRUCTIONS,
        STATE_APPS_LOOPER_INTERVAL,
        STATE_APPS_LOOPER_LIMIT,
    }:
        user_data.pop(STATE_KEY, None)
    user_data.pop(APPS_PENDING_THREAD_KEY, None)
    user_data.pop(APPS_PENDING_WINDOW_ID_KEY, None)
    user_data.pop(APPS_LOOPER_CONFIG_KEY, None)


def _list_markdown_plan_candidates(
    base_dir: Path,
    *,
    max_items: int = 16,
    max_dirs_scanned: int = 400,
) -> list[str]:
    """Find candidate markdown plan files relative to base_dir."""
    root = base_dir.resolve()
    if not root.exists() or not root.is_dir():
        return []

    skip_dirs = {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        ".mypy_cache",
        ".pytest_cache",
        "__pycache__",
        ".tox",
        ".idea",
        ".vscode",
    }

    candidates: list[str] = []
    scanned = 0
    for current_root, dirnames, filenames in os.walk(root):
        scanned += 1
        if scanned > max_dirs_scanned:
            break
        dirnames[:] = [
            d
            for d in dirnames
            if d not in skip_dirs and not d.startswith(".")
        ]
        current = Path(current_root)
        for name in filenames:
            lower = name.lower()
            if not lower.endswith(".md"):
                continue
            if name.startswith("."):
                continue
            full_path = current / name
            try:
                rel = full_path.resolve().relative_to(root)
            except ValueError:
                continue
            rel_str = rel.as_posix()
            candidates.append(rel_str)
            if len(candidates) >= max_items * 4:
                break
        if len(candidates) >= max_items * 4:
            break

    def _plan_rank(path: str) -> tuple[int, int, str]:
        lower = path.lower()
        looks_like_plan = ("plan" in lower) or ("todo" in lower) or ("task" in lower)
        return (0 if looks_like_plan else 1, len(path), lower)

    candidates = sorted(dict.fromkeys(candidates), key=_plan_rank)
    return candidates[:max_items]


def _looper_panel_interval_choices() -> list[tuple[str, int]]:
    return [
        ("5m", 5 * 60),
        ("10m", 10 * 60),
        ("15m", 15 * 60),
        ("30m", 30 * 60),
    ]


def _looper_panel_limit_choices() -> list[tuple[str, int]]:
    return [
        ("none", 0),
        ("1h", 60 * 60),
        ("2h", 2 * 60 * 60),
        ("4h", 4 * 60 * 60),
    ]


_APP_ICON_DEFAULTS: dict[str, str] = {
    "autoresearch": "🔎",
    "looper": "🔁",
    "coco-delivery": "🚚",
}


def _app_icon_for_skill(skill) -> str:
    """Return icon for one app definition."""
    raw_icon = str(getattr(skill, "icon", "") or "").strip()
    if raw_icon:
        return raw_icon
    return _APP_ICON_DEFAULTS.get(str(getattr(skill, "name", "")).strip(), "🧩")


def _app_supports_config(app_name: str) -> bool:
    """Return whether one app has an interactive Configure panel."""
    return app_name in {"autoresearch", "looper"}


def _build_app_actions_text(
    *,
    app,
    enabled: bool,
) -> str:
    """Build one app action sheet text."""
    icon = _app_icon_for_skill(app)
    status = "enabled" if enabled else "disabled"
    desc = " ".join(str(app.description).split())
    lines = [
        f"{icon} *{app.name}*",
        "",
        f"Status in this topic: `{status}`",
    ]
    if desc:
        lines.extend(["", desc])
    lines.extend(
        [
            "",
            "Choose an action:",
            "- Run enables this app for the topic and returns to overview.",
            "- Configure opens app settings (if available).",
        ]
    )
    return "\n".join(lines)


def _build_app_actions_keyboard(
    *,
    app_name: str,
    supports_config: bool,
) -> InlineKeyboardMarkup:
    """Build app action sheet keyboard."""
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("▶ Run", callback_data=f"{CB_APPS_RUN}{app_name}"[:64])],
    ]
    if supports_config:
        rows.append(
            [
                InlineKeyboardButton(
                    "⚙ Configure",
                    callback_data=f"{CB_APPS_CONFIGURE}{app_name}"[:64],
                )
            ]
        )
    rows.append([InlineKeyboardButton("⬅ Back to Apps", callback_data=CB_APPS_BACK)])
    return InlineKeyboardMarkup(rows)


def _normalize_looper_panel_config(
    raw: dict | None,
    *,
    candidates: list[str],
    active_state,
) -> dict[str, object]:
    """Build normalized looper UI config stored in user_data."""
    src = raw if isinstance(raw, dict) else {}

    plan_path = str(src.get("plan_path", "")).strip()
    keyword = str(src.get("keyword", "")).strip().lower()
    instructions = str(src.get("instructions", "")).strip()
    interval_seconds_raw = src.get("interval_seconds", LOOPER_DEFAULT_INTERVAL_SECONDS)
    limit_seconds_raw = src.get("limit_seconds", 0)

    if active_state is not None:
        if not plan_path:
            plan_path = str(active_state.plan_path).strip()
        if not keyword:
            keyword = str(active_state.keyword).strip().lower()
        if not instructions:
            instructions = str(active_state.instructions).strip()
        if not isinstance(interval_seconds_raw, int) or interval_seconds_raw <= 0:
            interval_seconds_raw = int(active_state.interval_seconds)
        if (
            (not isinstance(limit_seconds_raw, int) or limit_seconds_raw < 0)
            and active_state.deadline_at > active_state.started_at
        ):
            limit_seconds_raw = int(active_state.deadline_at - active_state.started_at)

    if not plan_path and candidates:
        plan_path = candidates[0]
    if not keyword:
        keyword = "done"

    interval_seconds = int(interval_seconds_raw)
    interval_seconds = max(LOOPER_MIN_INTERVAL_SECONDS, interval_seconds)
    interval_seconds = min(LOOPER_MAX_INTERVAL_SECONDS, interval_seconds)

    limit_seconds = int(limit_seconds_raw) if isinstance(limit_seconds_raw, int) else 0
    if limit_seconds < 0:
        limit_seconds = 0

    return {
        "plan_path": plan_path,
        "keyword": keyword,
        "instructions": instructions,
        "interval_seconds": interval_seconds,
        "limit_seconds": limit_seconds,
        "candidates": candidates,
    }


def _build_apps_panel_keyboard(
    *,
    enabled_names: list[str],
    catalog: dict,
) -> InlineKeyboardMarkup:
    enabled_set = set(enabled_names)
    rows: list[list[InlineKeyboardButton]] = []
    available = sorted(catalog.values(), key=lambda item: item.name)
    for app in available[:12]:
        is_enabled = app.name in enabled_set
        marker = "✅" if is_enabled else _app_icon_for_skill(app)
        callback = (
            f"{CB_APPS_OPEN}{app.name}"
            if _app_supports_config(app.name)
            else f"{CB_APPS_TOGGLE}{app.name}"
        )
        rows.append(
            [
                InlineKeyboardButton(
                    f"{marker} {app.name}",
                    callback_data=callback[:64],
                )
            ]
        )

    rows.append([InlineKeyboardButton("Refresh", callback_data=CB_APPS_REFRESH)])
    return InlineKeyboardMarkup(rows)


def _build_looper_panel_text(
    *,
    config_data: dict[str, object],
    active_state,
) -> str:
    """Build interactive looper panel text for /apps."""
    plan_path = str(config_data.get("plan_path", "")).strip() or "(select one)"
    keyword = str(config_data.get("keyword", "")).strip() or "(set one)"
    instructions = str(config_data.get("instructions", "")).strip()
    interval_seconds = int(config_data.get("interval_seconds", LOOPER_DEFAULT_INTERVAL_SECONDS))
    limit_seconds = int(config_data.get("limit_seconds", 0))

    lines = [
        "🔁 *Looper App*",
        "",
        f"Plan file: `{plan_path}`",
        f"Completion keyword: `{keyword}`",
        f"Interval: `{_format_duration_brief(interval_seconds)}`",
        (
            f"Time limit: `{_format_duration_brief(limit_seconds)}`"
            if limit_seconds > 0
            else "Time limit: `(none)`"
        ),
        (
            f"Custom instructions: `{instructions}`"
            if instructions
            else "Custom instructions: `(none)`"
        ),
    ]

    if active_state is not None:
        now = time.time()
        next_in = max(0, int(active_state.next_prompt_at - now))
        lines.extend(
            [
                "",
                "Runtime status: `running`",
                f"Nudges sent: `{active_state.prompt_count}`",
                f"Next nudge in: `{_format_duration_brief(next_in)}`",
            ]
        )
    else:
        lines.extend(["", "Runtime status: `stopped`"])

    lines.extend(
        [
            "",
            "Use buttons below to set plan path, interval, limit, keyword, and instructions.",
            "Then tap Start.",
        ]
    )
    return "\n".join(lines)


def _build_looper_panel_keyboard(
    *,
    config_data: dict[str, object],
    active_state,
) -> InlineKeyboardMarkup:
    """Build interactive looper panel keyboard."""
    rows: list[list[InlineKeyboardButton]] = []
    candidates = config_data.get("candidates")
    candidate_list = (
        list(candidates)
        if isinstance(candidates, list)
        else []
    )
    selected_plan = str(config_data.get("plan_path", "")).strip()
    for idx, path in enumerate(candidate_list[:8]):
        marker = "✅" if path == selected_plan else "•"
        rows.append(
            [
                InlineKeyboardButton(
                    f"{marker} {path}",
                    callback_data=f"{CB_APPS_LOOPER_PLAN}{idx}"[:64],
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton("📄 Set Plan Path", callback_data=CB_APPS_LOOPER_PLAN_MANUAL),
        ]
    )

    interval_value = int(config_data.get("interval_seconds", LOOPER_DEFAULT_INTERVAL_SECONDS))
    interval_buttons: list[InlineKeyboardButton] = []
    for label, seconds in _looper_panel_interval_choices():
        marker = "✅ " if seconds == interval_value else ""
        interval_buttons.append(
            InlineKeyboardButton(
                f"{marker}{label}",
                callback_data=f"{CB_APPS_LOOPER_INTERVAL}{seconds}"[:64],
            )
        )
    rows.append(interval_buttons)
    rows.append(
        [
            InlineKeyboardButton(
                "⏱ Custom Interval",
                callback_data=f"{CB_APPS_LOOPER_INTERVAL}custom",
            )
        ]
    )

    limit_value = int(config_data.get("limit_seconds", 0))
    limit_buttons: list[InlineKeyboardButton] = []
    for label, seconds in _looper_panel_limit_choices():
        marker = "✅ " if seconds == limit_value else ""
        limit_buttons.append(
            InlineKeyboardButton(
                f"{marker}{label}",
                callback_data=f"{CB_APPS_LOOPER_LIMIT}{seconds}"[:64],
            )
        )
    rows.append(limit_buttons)
    rows.append(
        [
            InlineKeyboardButton(
                "⌛ Custom Limit",
                callback_data=f"{CB_APPS_LOOPER_LIMIT}custom",
            )
        ]
    )

    rows.append(
        [
            InlineKeyboardButton("🔑 Set Keyword", callback_data=CB_APPS_LOOPER_KEYWORD),
            InlineKeyboardButton(
                "📝 Set Instructions",
                callback_data=CB_APPS_LOOPER_INSTRUCTIONS,
            ),
        ]
    )

    rows.append(
        [
            InlineKeyboardButton("▶ Start", callback_data=CB_APPS_LOOPER_START),
            InlineKeyboardButton("⏹ Stop", callback_data=CB_APPS_LOOPER_STOP),
        ]
    )
    rows.append([InlineKeyboardButton("⬅ Back to Apps", callback_data=CB_APPS_BACK)])
    return InlineKeyboardMarkup(rows)

def _run_git(cwd: Path | str, args: list[str]) -> tuple[bool, str, str]:
    """Run a git command and return (ok, stdout, stderr)."""
    cmd = ["git", *args]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError as e:
        return False, "", str(e)
    return proc.returncode == 0, proc.stdout.strip(), proc.stderr.strip()


def _parse_git_worktree_porcelain(text: str) -> list[dict[str, str]]:
    """Parse `git worktree list --porcelain` output."""
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}

    def _flush() -> None:
        if not current:
            return
        branch = current.get("branch", "")
        if branch.startswith("refs/heads/"):
            branch = branch[len("refs/heads/") :]
        if not branch and current.get("detached"):
            branch = "(detached)"
        current["branch"] = branch
        entries.append(dict(current))
        current.clear()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            _flush()
            continue
        key, sep, value = line.partition(" ")
        if not sep:
            current[key] = "1"
            continue
        if key == "worktree":
            current["path"] = value
        elif key == "HEAD":
            current["head"] = value
        elif key == "branch":
            current["branch"] = value
        elif key == "detached":
            current["detached"] = "1"
        else:
            current[key] = value
    _flush()
    return entries


def _git_repo_root(path: str | Path) -> tuple[Path | None, str]:
    """Return git top-level directory for a path."""
    ok, out, err = _run_git(path, ["rev-parse", "--show-toplevel"])
    if not ok or not out:
        return None, err or "Not a git repository."
    return Path(out).resolve(), ""


def _git_current_branch(path: str | Path) -> tuple[str | None, str]:
    """Return current branch name for a path."""
    ok, out, err = _run_git(path, ["rev-parse", "--abbrev-ref", "HEAD"])
    if not ok or not out:
        return None, err or "Failed to resolve branch."
    return out, ""


def _git_absolute_git_dir(path: str | Path) -> tuple[Path | None, str]:
    """Return absolute git-dir path for a working tree."""
    ok, out, err = _run_git(path, ["rev-parse", "--absolute-git-dir"])
    if not ok or not out:
        return None, err or "Failed to resolve git dir."
    return Path(out).resolve(), ""


def _is_primary_worktree(path: str | Path) -> bool:
    """Return True if path is the primary worktree, not a linked worktree."""
    git_dir, _err = _git_absolute_git_dir(path)
    if not git_dir:
        return False
    return git_dir.name == ".git"


def _git_worktree_list(repo_root: Path) -> tuple[list[dict[str, str]], str]:
    """List worktrees for a repository root."""
    ok, out, err = _run_git(repo_root, ["worktree", "list", "--porcelain"])
    if not ok:
        return [], err or "Failed to list worktrees."
    return _parse_git_worktree_porcelain(out), ""


def _format_worktree_line(
    entry: dict[str, str], *, current_path: str | None = None
) -> str:
    """Render one worktree entry for /worktree list."""
    path = entry.get("path", "")
    branch = entry.get("branch", "") or "(unknown)"
    name = Path(path).name if path else "unknown"
    marker = " (current)" if current_path and path == current_path else ""
    return f"• `{name}` — branch `{branch}`{marker}\n  `{path}`"


def _build_worktree_panel_text(
    *,
    repo_root: Path,
    current_path: str,
    current_branch: str,
    entries: list[dict[str, str]],
) -> str:
    """Build /worktree panel text."""
    display_root = str(repo_root).replace(str(Path.home()), "~")
    lines = [
        "🌳 *Worktrees*",
        "",
        f"Repo: `{display_root}`",
        f"Current branch: `{current_branch}`",
        "",
    ]
    if not entries:
        lines.append("No worktrees found.")
    else:
        lines.append("Known worktrees:")
        for entry in entries:
            lines.append(_format_worktree_line(entry, current_path=current_path))
    lines.extend(
        [
            "",
            "Commands:",
            "`/worktree new <name>`",
            "`/worktree fold <name1> [name2 ...]`",
        ]
    )
    return "\n".join(lines)


def _build_worktree_panel_keyboard() -> InlineKeyboardMarkup:
    """Build /worktree panel keyboard."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("New Worktree Topic", callback_data=CB_WORKTREE_NEW)],
            [InlineKeyboardButton("Fold Worktrees", callback_data=CB_WORKTREE_FOLD_MENU)],
            [InlineKeyboardButton("Refresh", callback_data=CB_WORKTREE_REFRESH)],
        ]
    )


def _build_worktree_fold_candidates(
    *,
    entries: list[dict[str, str]],
    current_path: str,
) -> list[dict[str, str]]:
    """Build selectable fold candidates from worktree list entries."""
    candidates: list[dict[str, str]] = []
    current_resolved = str(Path(current_path).resolve())
    seen_paths: set[str] = set()
    for entry in entries:
        path = str(entry.get("path", "")).strip()
        if not path:
            continue
        try:
            resolved_path = str(Path(path).resolve())
        except OSError:
            resolved_path = path
        if resolved_path == current_resolved:
            continue
        if resolved_path in seen_paths:
            continue
        seen_paths.add(resolved_path)
        branch = str(entry.get("branch", "")).strip() or "(unknown)"
        name = Path(path).name or path
        candidates.append(
            {
                "name": name,
                "branch": branch,
                "path": resolved_path,
            }
        )
    return candidates


def _build_worktree_fold_text(
    *,
    target_branch: str,
    candidates: list[dict[str, str]],
    selected_indices: set[int],
) -> str:
    """Render interactive worktree fold picker text."""
    lines = [
        "🧬 *Fold Worktrees*",
        "",
        f"Target branch: `{target_branch}`",
        "Select one or more worktrees to fold into this branch.",
        "",
    ]
    if not candidates:
        lines.append("No fold candidates found.")
    else:
        for idx, item in enumerate(candidates, start=1):
            marker = "✅" if (idx - 1) in selected_indices else "◻️"
            lines.append(
                f"{marker} {idx}. `{item.get('name', 'unknown')}` "
                f"→ `{item.get('branch', '(unknown)')}`"
            )
    return "\n".join(lines)


def _build_worktree_fold_keyboard(
    *,
    candidates: list[dict[str, str]],
    selected_indices: set[int],
) -> InlineKeyboardMarkup:
    """Render interactive worktree fold picker keyboard."""
    rows: list[list[InlineKeyboardButton]] = []
    for idx, item in enumerate(candidates):
        selected = idx in selected_indices
        label_prefix = "✅" if selected else "◻️"
        label = f"{label_prefix} {item.get('name', 'worktree')}"
        rows.append(
            [
                InlineKeyboardButton(
                    label[:64],
                    callback_data=f"{CB_WORKTREE_FOLD_TOGGLE}{idx}"[:64],
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton("Run Fold", callback_data=CB_WORKTREE_FOLD_RUN),
            InlineKeyboardButton("Back", callback_data=CB_WORKTREE_FOLD_BACK),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _sanitize_worktree_name(raw_name: str) -> str:
    """Normalize user-provided worktree name."""
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", raw_name.strip())
    cleaned = cleaned.strip("._-")
    if not cleaned:
        cleaned = f"wt-{int(time.time())}"
    return cleaned[:48]


def _pick_worktree_path(repo_root: Path, slug: str) -> Path:
    """Pick a non-conflicting worktree path near repo root."""
    base_parent = repo_root.parent
    base_name = f"{repo_root.name}-wt-{slug}"
    candidate = base_parent / base_name
    counter = 2
    while candidate.exists():
        candidate = base_parent / f"{base_name}-{counter}"
        counter += 1
    return candidate


def _resolve_worktree_selector(
    entries: list[dict[str, str]],
    selector: str,
) -> dict[str, str] | None:
    """Resolve selector by path, folder name, or branch."""
    selector = selector.strip()
    if not selector:
        return None

    # Exact path match
    for entry in entries:
        if entry.get("path") == selector:
            return entry

    # Exact basename match
    basename_matches = [
        entry for entry in entries if Path(entry.get("path", "")).name == selector
    ]
    if len(basename_matches) == 1:
        return basename_matches[0]

    # Exact branch match
    branch_matches = [entry for entry in entries if entry.get("branch") == selector]
    if len(branch_matches) == 1:
        return branch_matches[0]
    return None


async def _build_worktree_handoff_prompt(
    source_window_id: str,
    source_thread_id: int,
) -> str:
    """Build a compact context handoff for a newly created worktree session."""
    messages, _count = await session_manager.get_recent_messages(source_window_id)
    tail = messages[-20:]
    lines = [
        "Context handoff from the original topic.",
        f"Source topic thread: {source_thread_id}",
        "",
        "Recent context:",
    ]
    for msg in tail:
        role = str(msg.get("role", "assistant"))
        text = str(msg.get("text", "")).strip()
        if not text:
            continue
        if role == "assistant" and str(msg.get("content_type", "")).strip() != "text":
            continue
        one_line = " ".join(text.split())
        if len(one_line) > 220:
            one_line = one_line[:217] + "..."
        prefix = "User" if role == "user" else "Assistant"
        lines.append(f"- {prefix}: {one_line}")

    if len(lines) <= 4:
        lines.append("- (No recent transcript context available)")
    lines.append("")
    lines.append("Continue this task from here in this new worktree.")

    prompt = "\n".join(lines).strip()
    if len(prompt) > 3600:
        prompt = prompt[:3597] + "..."
    return prompt


async def _show_worktree_panel(
    target_message,
    *,
    user_id: int,
    thread_id: int | None,
    chat_id: int | None = None,
) -> tuple[bool, str]:
    """Render /worktree panel for a bound topic."""
    if thread_id is None:
        await safe_reply(
            target_message,
            "❌ Use /worktree inside a named topic bound to a session.",
        )
        return False, ""

    wid = session_manager.resolve_window_for_thread(
        user_id,
        thread_id,
        chat_id=chat_id,
    )
    if not wid:
        await safe_reply(target_message, "❌ No session bound to this topic.")
        return False, ""

    workspace_dir, workspace_err = await _resolve_live_workspace_dir_for_window(
        user_id=user_id,
        thread_id=thread_id,
        chat_id=chat_id,
        window_id=wid,
    )
    if not workspace_dir:
        await safe_reply(
            target_message,
            f"❌ {workspace_err or 'No workspace bound to this topic.'}",
        )
        return False, ""

    repo_root, repo_err = _git_repo_root(workspace_dir)
    if not repo_root:
        await safe_reply(target_message, f"❌ {repo_err or 'Not a git repository.'}")
        return False, ""

    branch, _branch_err = _git_current_branch(workspace_dir)
    branch_name = branch or "(unknown)"
    entries, list_err = _git_worktree_list(repo_root)
    if list_err:
        await safe_reply(target_message, f"❌ {list_err}")
        return False, ""

    text = _build_worktree_panel_text(
        repo_root=repo_root,
        current_path=str(Path(workspace_dir).resolve()),
        current_branch=branch_name,
        entries=entries,
    )
    await safe_reply(
        target_message,
        text,
        reply_markup=_build_worktree_panel_keyboard(),
    )
    return True, wid


async def _create_worktree_from_topic(
    *,
    bot: Bot,
    user_id: int,
    thread_id: int,
    worktree_name: str,
    chat_id: int | None = None,
) -> tuple[bool, str]:
    """Create a new worktree and topic session from the current topic."""
    if not _can_user_create_sessions(user_id):
        return (
            False,
            "You do not have permission to create new sessions/worktrees.",
        )

    source_wid = session_manager.resolve_window_for_thread(
        user_id,
        thread_id,
        chat_id=chat_id,
    )
    if not source_wid:
        return False, "No session bound to this topic."
    source_workspace_dir, source_workspace_err = await _resolve_live_workspace_dir_for_window(
        user_id=user_id,
        thread_id=thread_id,
        chat_id=chat_id,
        window_id=source_wid,
    )
    if not source_workspace_dir:
        return False, source_workspace_err or "No workspace bound to this topic."

    repo_root, repo_err = _git_repo_root(source_workspace_dir)
    if not repo_root:
        return False, repo_err or "Not a git repository."

    slug = _sanitize_worktree_name(worktree_name)
    branch_name = f"wt/{slug}-{int(time.time())}"
    wt_path = _pick_worktree_path(repo_root, slug)
    ok, _out, err = _run_git(
        repo_root,
        ["worktree", "add", "-b", branch_name, str(wt_path)],
    )
    if not ok:
        return False, err or "Failed to create git worktree."

    resolved_chat_id = session_manager.resolve_chat_id(
        user_id,
        thread_id,
        chat_id=chat_id,
    )
    topic_title = f"{slug}"
    try:
        topic = await bot.create_forum_topic(
            chat_id=resolved_chat_id,
            name=topic_title[:128],
        )
        new_thread_id = topic.message_thread_id
    except Exception as e:
        _run_git(repo_root, ["worktree", "remove", "--force", str(wt_path)])
        return False, f"Failed to create new topic: {e}"

    codex_thread_id = ""
    created_wid = session_manager.allocate_virtual_window_id()
    created_wname = slug
    created_state = session_manager.get_window_state(created_wid)
    created_state.cwd = str(wt_path)
    created_state.window_name = created_wname
    source_model, source_effort = session_manager.get_topic_model_selection(
        user_id,
        thread_id,
        chat_id=chat_id,
    )
    source_service_tier = session_manager.get_topic_service_tier_selection(
        user_id,
        thread_id,
        chat_id=chat_id,
    )
    success = True
    message = f"Created app-server session `{created_wname}`"
    try:
        ensure_kwargs: dict[str, str] = {}
        if source_model:
            ensure_kwargs["model"] = source_model
        if source_effort:
            ensure_kwargs["effort"] = source_effort
        if source_service_tier:
            ensure_kwargs["service_tier"] = source_service_tier
        codex_thread_id, _approval = await session_manager._ensure_codex_thread_for_window(
            window_id=created_wid,
            cwd=str(wt_path),
            **ensure_kwargs,
        )
    except Exception as e:
        logger.warning(
            "Failed to initialize app-server worktree session (user=%d thread=%d path=%s): %s",
            user_id,
            thread_id,
            wt_path,
            e,
        )
        success = False
        message = f"Failed to start app-server session: {e}"
    if not success:
        _run_git(repo_root, ["worktree", "remove", "--force", str(wt_path)])
        return False, message

    inherited_mode = _get_window_approval_mode(source_wid)
    inherit_ok, inherit_err = await _apply_window_approval_mode(
        created_wid,
        inherited_mode,
    )
    if not inherit_ok:
        logger.warning(
            "Failed to inherit approval mode %s from %s to %s: %s",
            inherited_mode,
            source_wid,
            created_wid,
            inherit_err,
        )

    if codex_thread_id:
        session_manager.bind_topic_to_codex_thread(
            user_id=user_id,
            thread_id=new_thread_id,
            chat_id=resolved_chat_id,
            codex_thread_id=codex_thread_id,
            cwd=str(wt_path),
            display_name=created_wname,
            window_id=created_wid,
        )
        if source_model or source_effort:
            session_manager.set_topic_model_selection(
                user_id,
                new_thread_id,
                chat_id=resolved_chat_id,
                model_slug=source_model,
                reasoning_effort=source_effort,
            )
        if source_service_tier:
            session_manager.set_topic_service_tier_selection(
                user_id,
                new_thread_id,
                chat_id=resolved_chat_id,
                service_tier=source_service_tier,
            )
    else:
        session_manager.bind_thread(
            user_id,
            new_thread_id,
            created_wid,
            window_name=created_wname,
            chat_id=resolved_chat_id,
        )
    session_manager.set_group_chat_id(user_id, new_thread_id, resolved_chat_id)

    handoff = await _build_worktree_handoff_prompt(source_wid, thread_id)
    await asyncio.sleep(1.0)
    send_ok, send_msg = await session_manager.send_to_window(created_wid, handoff)
    if not send_ok:
        logger.warning(
            "Failed sending handoff prompt to new worktree window %s: %s",
            created_wid,
            send_msg,
        )
    else:
        # Handoff prompt is system-generated; no no-response retry tracking needed.
        note_run_started(
            user_id=user_id,
            thread_id=new_thread_id,
            window_id=created_wid,
            source="worktree_handoff",
            expect_response=False,
        )

    await safe_send(
        bot,
        resolved_chat_id,
        "🌱 Worktree session ready.\n"
        f"Path: `{wt_path}`\n"
        f"Branch: `{branch_name}`\n"
        f"Approvals: `{_approval_mode_display_text(_get_window_approval_mode(created_wid))}`",
        message_thread_id=new_thread_id,
    )
    return (
        True,
        f"Created worktree `{slug}` on `{branch_name}` and opened a new topic.",
    )


def _fold_worktrees_into_branch(
    *,
    target_cwd: Path,
    selectors: list[str],
) -> tuple[bool, str]:
    """Fold one or more worktree branches into the active target branch."""
    if not _is_primary_worktree(target_cwd):
        return False, "Fold must run from the primary repository worktree (not a linked worktree)."

    repo_root, repo_err = _git_repo_root(target_cwd)
    if not repo_root:
        return False, repo_err or "Not a git repository."

    target_branch, branch_err = _git_current_branch(target_cwd)
    if not target_branch:
        return False, branch_err or "Failed to resolve target branch."
    if target_branch in {"HEAD", "(detached)"}:
        return False, "Target branch is detached; checkout a branch first."

    entries, err = _git_worktree_list(repo_root)
    if err:
        return False, err

    target_root_str = str(repo_root.resolve())
    selected_entries: list[dict[str, str]] = []
    for selector in selectors:
        entry = _resolve_worktree_selector(entries, selector)
        if not entry:
            return False, f"Unknown worktree selector: {selector}"
        if entry.get("path", "") == target_root_str:
            return False, "Cannot fold the target/main worktree into itself."
        selected_entries.append(entry)

    seen_paths: set[str] = set()
    unique_entries: list[dict[str, str]] = []
    for entry in selected_entries:
        path = entry.get("path", "")
        if path in seen_paths:
            continue
        seen_paths.add(path)
        unique_entries.append(entry)

    merged_branches: list[str] = []
    for entry in unique_entries:
        src_path = Path(entry.get("path", ""))
        src_branch = entry.get("branch", "")
        if not src_branch or src_branch == "(detached)":
            return False, f"Worktree {src_path} is detached; skipping."

        ok, out, _err = _run_git(src_path, ["status", "--porcelain"])
        if not ok:
            return False, f"Failed to inspect worktree {src_path}."
        if out.strip():
            return False, f"Worktree {src_path} has uncommitted changes."

        if src_branch == target_branch:
            continue

        ok, _out, merge_err = _run_git(
            repo_root,
            ["merge", "--no-ff", src_branch],
        )
        if not ok:
            _run_git(repo_root, ["merge", "--abort"])
            return False, f"Merge failed for branch {src_branch}: {merge_err}"
        merged_branches.append(src_branch)

    if not merged_branches:
        return True, f"No merges required; target branch `{target_branch}` already up to date."
    merged_text = ", ".join(f"`{b}`" for b in merged_branches)
    return True, f"Folded into `{target_branch}`: {merged_text}"


async def _set_eyes_reaction(message) -> None:
    """Set eyes reaction on a user message (best effort)."""
    try:
        await message.set_reaction(ReactionEmoji.EYES)
    except Exception as e:
        logger.debug(
            "Failed to set reaction ack (chat=%s message=%s): %s",
            message.chat_id,
            message.message_id,
            e,
        )


async def _set_hourglass_reaction(message) -> None:
    """Set hourglass reaction on a queued /q message (best effort)."""
    try:
        await message.set_reaction("⏳")
        return
    except Exception as e:
        logger.debug(
            "Hourglass reaction unsupported, falling back (chat=%s message=%s): %s",
            message.chat_id,
            message.message_id,
            e,
        )
    try:
        await message.set_reaction(ReactionEmoji.THINKING_FACE)
    except Exception as e:
        logger.debug(
            "Failed to set queued reaction ack (chat=%s message=%s): %s",
            message.chat_id,
            message.message_id,
            e,
        )


def _pick_restart_back_up_message() -> str:
    """Pick one dry post-restart message."""
    return random.choice(RESTART_BACK_UP_MESSAGES)


def _pick_restart_shutdown_message() -> str:
    """Pick one dry pre-restart shutdown message."""
    return random.choice(RESTART_SHUTDOWN_MESSAGES)


def _set_restart_notice_target(chat_id: int, thread_id: int | None) -> None:
    """Persist target chat/topic for post-restart notice."""
    os.environ[_RESTART_NOTICE_PENDING_ENV] = "1"
    os.environ[_RESTART_NOTICE_CHAT_ENV] = str(chat_id)
    if thread_id is None:
        os.environ.pop(_RESTART_NOTICE_THREAD_ENV, None)
    else:
        os.environ[_RESTART_NOTICE_THREAD_ENV] = str(thread_id)
    try:
        _RESTART_NOTICE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _RESTART_NOTICE_FILE.write_text(
            json.dumps({"chat_id": chat_id, "thread_id": thread_id}),
            encoding="utf-8",
        )
    except OSError as e:
        logger.debug("Failed writing restart notice file %s: %s", _RESTART_NOTICE_FILE, e)


def _pop_restart_notice_target_from_file() -> tuple[int, int | None] | None:
    """Read and clear pending restart notice target from file."""
    if not _RESTART_NOTICE_FILE.is_file():
        return None
    try:
        payload = json.loads(_RESTART_NOTICE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug("Failed reading restart notice file %s: %s", _RESTART_NOTICE_FILE, e)
        payload = None
    try:
        _RESTART_NOTICE_FILE.unlink(missing_ok=True)
    except OSError:
        pass

    if not isinstance(payload, dict):
        return None
    chat_raw = payload.get("chat_id")
    thread_raw = payload.get("thread_id")
    try:
        chat_id = int(chat_raw)
    except (TypeError, ValueError):
        return None
    if thread_raw is None:
        return chat_id, None
    try:
        return chat_id, int(thread_raw)
    except (TypeError, ValueError):
        return chat_id, None


def _pop_restart_notice_target_from_env() -> tuple[int, int | None] | None:
    """Read and clear pending restart notice target from environment."""
    pending = os.environ.pop(_RESTART_NOTICE_PENDING_ENV, None)
    chat_raw = os.environ.pop(_RESTART_NOTICE_CHAT_ENV, None)
    thread_raw = os.environ.pop(_RESTART_NOTICE_THREAD_ENV, None)
    if pending != "1" or not chat_raw:
        return None

    try:
        chat_id = int(chat_raw)
    except ValueError:
        return None

    if thread_raw is None or thread_raw == "":
        return chat_id, None
    try:
        return chat_id, int(thread_raw)
    except ValueError:
        return chat_id, None


def _pop_restart_notice_target() -> tuple[int, int | None] | None:
    """Read and clear pending restart notice target from file/env."""
    target = _pop_restart_notice_target_from_file()
    if target is not None:
        os.environ.pop(_RESTART_NOTICE_PENDING_ENV, None)
        os.environ.pop(_RESTART_NOTICE_CHAT_ENV, None)
        os.environ.pop(_RESTART_NOTICE_THREAD_ENV, None)
        return target
    return _pop_restart_notice_target_from_env()


def _startup_notice_targets(
    restart_target: tuple[int, int | None] | None,
) -> list[tuple[int, int | None]]:
    """Resolve startup notice recipients.

    Prefer the explicit restart request topic when available. Otherwise,
    notify allowlist admins in private chat so startup always emits a message.
    """
    targets: list[tuple[int, int | None]] = []
    seen: set[tuple[int, int | None]] = set()

    if restart_target is not None:
        targets.append(restart_target)
        seen.add(restart_target)

    if not targets:
        admin_ids = sorted(_get_allowed_admins())
        if not admin_ids:
            admin_ids = sorted(config.allowed_users)
        for user_id in admin_ids:
            key = (user_id, None)
            if key in seen:
                continue
            seen.add(key)
            targets.append(key)

    return targets


def _is_shadow_transcript_message(msg: NewMessage) -> bool:
    """Return whether this message came from the shadow transcript observer."""
    return msg.source == "transcript" and _codex_app_server_enabled()


_TELEGRAM_ATTACHMENT_TAG_RE = re.compile(
    r'(?mi)^[ \t]*<telegram-attachment\s+path=(["\'])(?P<path>.+?)\1\s*/>[ \t]*$'
)
_ALLOWED_TELEGRAM_DOCUMENT_EXTENSIONS = {".pdf", ".txt", ".md"}
_ALLOWED_TELEGRAM_IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}
_TELEGRAM_ATTACHMENT_MAX_BYTES = 45 * 1024 * 1024


def _resolve_telegram_attachment_path(
    *,
    workspace_dir: str,
    raw_path: str,
) -> Path | None:
    """Resolve one Telegram attachment path inside the workspace."""
    if not workspace_dir or not raw_path:
        return None

    try:
        workspace_root = Path(workspace_dir).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None

    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = workspace_root / candidate

    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError, ValueError):
        return None

    try:
        resolved.relative_to(workspace_root)
    except ValueError:
        logger.warning(
            "Rejected Telegram attachment outside workspace: %s (workspace=%s)",
            resolved,
            workspace_root,
        )
        return None
    return resolved


def _resolve_telegram_document_attachment(
    *,
    workspace_dir: str,
    raw_path: str,
) -> tuple[str, bytes] | None:
    """Resolve one explicit Telegram attachment path inside the workspace."""
    resolved = _resolve_telegram_attachment_path(
        workspace_dir=workspace_dir,
        raw_path=raw_path,
    )
    if resolved is None:
        return None
    if resolved.suffix.lower() not in _ALLOWED_TELEGRAM_DOCUMENT_EXTENSIONS:
        logger.warning("Rejected Telegram attachment with unsupported type: %s", resolved)
        return None
    if not resolved.is_file():
        logger.warning("Rejected Telegram attachment missing file: %s", resolved)
        return None

    try:
        size = resolved.stat().st_size
    except OSError:
        return None
    if size > _TELEGRAM_ATTACHMENT_MAX_BYTES:
        logger.warning(
            "Rejected Telegram attachment exceeding size limit: %s (%d bytes)",
            resolved,
            size,
        )
        return None

    try:
        return resolved.name, resolved.read_bytes()
    except OSError:
        logger.warning("Failed reading Telegram attachment file: %s", resolved)
        return None


def _resolve_telegram_image_attachment(
    *,
    workspace_dir: str,
    raw_path: str,
) -> tuple[str, bytes] | None:
    """Resolve one explicit Telegram image attachment inside the workspace."""
    resolved = _resolve_telegram_attachment_path(
        workspace_dir=workspace_dir,
        raw_path=raw_path,
    )
    if resolved is None:
        return None
    media_type = _ALLOWED_TELEGRAM_IMAGE_TYPES.get(resolved.suffix.lower())
    if not media_type:
        return None
    if not resolved.is_file():
        logger.warning("Rejected Telegram attachment missing file: %s", resolved)
        return None
    try:
        size = resolved.stat().st_size
    except OSError:
        return None
    if size > _TELEGRAM_ATTACHMENT_MAX_BYTES:
        logger.warning(
            "Rejected Telegram attachment exceeding size limit: %s (%d bytes)",
            resolved,
            size,
        )
        return None
    try:
        return media_type, resolved.read_bytes()
    except OSError:
        logger.warning("Failed reading Telegram attachment file: %s", resolved)
        return None


def _extract_telegram_attachments(
    text: str,
    *,
    workspace_dir: str | None,
) -> tuple[str, list[tuple[str, bytes]] | None, list[tuple[str, bytes]] | None]:
    """Strip explicit Telegram attachment tags and load allowed images/documents."""
    if not text:
        return text, None, None

    image_attachments: list[tuple[str, bytes]] = []
    document_attachments: list[tuple[str, bytes]] = []

    def _replace(match: re.Match[str]) -> str:
        if workspace_dir:
            raw_path = match.group("path").strip()
            image_resolved = _resolve_telegram_image_attachment(
                workspace_dir=workspace_dir,
                raw_path=raw_path,
            )
            if image_resolved is not None:
                image_attachments.append(image_resolved)
                return ""
            resolved = _resolve_telegram_document_attachment(
                workspace_dir=workspace_dir,
                raw_path=raw_path,
            )
            if resolved is not None:
                document_attachments.append(resolved)
        return ""

    stripped = _TELEGRAM_ATTACHMENT_TAG_RE.sub(_replace, text)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped).strip()
    return stripped, image_attachments or None, document_attachments or None


async def _extract_telegram_attachments_for_window(
    text: str,
    *,
    workspace_dir: str | None,
    window_id: str,
) -> tuple[str, list[tuple[str, bytes]] | None, list[tuple[str, bytes]] | None]:
    """Strip explicit Telegram attachment tags for local or remote workspaces."""
    if not text:
        return text, None, None
    machine_id = session_manager.get_window_machine_id(window_id)
    local_machine_id, _local_machine_name = _local_machine_identity()
    if not machine_id or machine_id == local_machine_id:
        return _extract_telegram_attachments(
            text,
            workspace_dir=workspace_dir,
        )

    raw_paths = [
        match.group("path").strip()
        for match in _TELEGRAM_ATTACHMENT_TAG_RE.finditer(text)
        if match.group("path").strip()
    ]
    stripped = re.sub(r"\n{3,}", "\n\n", _TELEGRAM_ATTACHMENT_TAG_RE.sub("", text)).strip()
    if not raw_paths or not workspace_dir:
        return stripped, None

    from .agent_rpc import agent_rpc_client

    try:
        attachments = await agent_rpc_client.read_attachments(
            machine_id,
            workspace_dir=workspace_dir,
            paths=raw_paths,
        )
    except Exception as exc:
        logger.warning(
            "Failed reading remote Telegram attachments (machine=%s workspace=%s): %s",
            machine_id,
            workspace_dir,
            exc,
        )
        attachments = {"images": [], "documents": []}
    image_attachments = attachments.get("images")
    document_attachments = attachments.get("documents")
    return (
        stripped,
        image_attachments if isinstance(image_attachments, list) and image_attachments else None,
        (
            document_attachments
            if isinstance(document_attachments, list) and document_attachments
            else None
        ),
    )


def _message_has_telegram_attachments(msg: NewMessage) -> bool:
    """Return whether a message carries attachment payloads for Telegram."""
    return bool(msg.image_data)


def _is_native_shadow_milestone_message(msg: NewMessage) -> bool:
    """Return whether a shadow transcript message is a native Codex milestone."""
    return bool(
        _is_shadow_transcript_message(msg)
        and msg.role == "assistant"
        and msg.is_complete
        and isinstance(msg.event_type, str)
        and msg.event_type.startswith("response_item:")
    )


async def _is_window_in_progress(
    user_id: int,
    thread_id: int | None,
    window_id: str,
) -> bool:
    """Return whether a window appears to be actively processing."""
    if session_manager.is_window_external_turn_active(window_id):
        return True
    if _codex_app_server_enabled():
        codex_thread_id = session_manager.get_window_codex_thread_id(window_id)
        if codex_thread_id:
            if codex_app_server_client.is_turn_in_progress(codex_thread_id):
                return True
            # Persisted active turn can survive brief client reconnect gaps.
            if session_manager.get_window_codex_active_turn_id(window_id):
                return True
            # App-server turn is idle; stale process-message state must not force
            # steer mode for the next user message.
            return False
    return is_progress_active(user_id, thread_id)


async def _handle_shadow_transcript_message_for_topic(
    *,
    msg: NewMessage,
    bot: Bot,
    user_id: int,
    chat_id: int | None,
    window_id: str,
    thread_id: int,
) -> bool:
    """Handle transcript messages when app-server is the live Telegram transport.

    Returns True when the message was fully handled/suppressed and should not
    continue through the normal delivery pipeline.
    """
    if not _is_shadow_transcript_message(msg):
        return False

    sync_mode = session_manager.get_topic_sync_mode(
        user_id,
        thread_id,
        chat_id=chat_id,
    )

    if msg.event_type == "task_complete":
        if sync_mode != TOPIC_SYNC_MODE_HOST_FOLLOW_FINAL:
            return True
        session_manager.set_window_external_turn_active(window_id, False)
        if queued_topic_input_count(user_id, thread_id) > 0:
            await _dispatch_next_queued_input(
                bot=bot,
                user_id=user_id,
                thread_id=thread_id,
                window_id=window_id,
                chat_id=chat_id,
            )
        return True

    if msg.role == "user":
        if session_manager.consume_expected_transcript_user_echo(window_id, msg.text):
            return True
        session_manager.set_topic_sync_mode(
            user_id,
            thread_id,
            TOPIC_SYNC_MODE_HOST_FOLLOW_FINAL,
            chat_id=chat_id,
        )
        session_manager.set_window_external_turn_active(window_id, True)
        return True

    if sync_mode != TOPIC_SYNC_MODE_HOST_FOLLOW_FINAL:
        if (
            msg.role == "assistant"
            and msg.is_complete
            and (
                _message_has_telegram_attachments(msg)
                or _is_native_shadow_milestone_message(msg)
            )
        ):
            return False
        return True

    if msg.role != "assistant" or not msg.is_complete:
        return True

    if msg.content_type != "text":
        return True

    session_manager.set_window_external_turn_active(window_id, False)
    return False


async def _dispatch_next_queued_input(
    bot: Bot,
    user_id: int,
    thread_id: int,
    window_id: str,
    *,
    chat_id: int | None = None,
) -> None:
    """Send one queued /q message after current run completion."""
    if await _is_window_in_progress(user_id, thread_id, window_id):
        emit_telemetry(
            "queue.dispatch.deferred_active_turn",
            user_id=user_id,
            thread_id=thread_id,
            window_id=window_id,
        )
        await sync_queued_topic_dock(
            bot,
            user_id,
            thread_id,
            window_id=window_id,
        )
        return

    queued = pop_queued_topic_input(user_id, thread_id)
    if not queued:
        await sync_queued_topic_dock(
            bot,
            user_id,
            thread_id,
            window_id=window_id,
        )
        return

    queued_text, src_chat_id, src_message_id = queued
    await sync_queued_topic_dock(
        bot,
        user_id,
        thread_id,
        window_id=window_id,
    )
    emit_telemetry(
        "queue.dispatch.start",
        user_id=user_id,
        thread_id=thread_id,
        window_id=window_id,
        text_len=len(queued_text),
    )

    queue = get_message_queue(user_id)
    if queue:
        try:
            await asyncio.wait_for(queue.join(), timeout=5.0)
        except TimeoutError:
            logger.debug(
                "Timed out waiting for message queue before dispatching queued /q (user=%d thread=%d)",
                user_id,
                thread_id,
            )
            emit_telemetry(
                "queue.dispatch.queue_wait_timeout",
                user_id=user_id,
                thread_id=thread_id,
                window_id=window_id,
            )

    success, send_msg = await session_manager.send_topic_text_to_window(
        user_id=user_id,
        thread_id=thread_id,
        chat_id=chat_id,
        window_id=window_id,
        text=queued_text,
    )
    emit_telemetry(
        "queue.dispatch.send_result",
        user_id=user_id,
        thread_id=thread_id,
        window_id=window_id,
        success=success,
        error=send_msg,
        text_len=len(queued_text),
    )
    if not success:
        prepend_queued_topic_input(
            user_id,
            thread_id,
            queued_text,
            src_chat_id,
            src_message_id,
        )
        await sync_queued_topic_dock(
            bot,
            user_id,
            thread_id,
            window_id=window_id,
        )
        await safe_send(
            bot,
            session_manager.resolve_chat_id(
                user_id,
                thread_id,
                chat_id=chat_id,
            ),
            f"❌ Failed to send queued `/q` message: {send_msg}",
            message_thread_id=thread_id,
        )
        return
    note_run_started(
        user_id=user_id,
        thread_id=thread_id,
        window_id=window_id,
        source="queued_dispatch",
        pending_text=queued_text,
        expect_response=True,
    )

    try:
        await bot.set_message_reaction(
            chat_id=src_chat_id,
            message_id=src_message_id,
            reaction=ReactionEmoji.EYES,
        )
    except Exception as e:
        logger.debug(
            "Failed to set queued /q reaction ack (chat=%s message=%s): %s",
            src_chat_id,
            src_message_id,
            e,
        )
        emit_telemetry(
            "queue.dispatch.reaction_failed",
            user_id=user_id,
            thread_id=thread_id,
            window_id=window_id,
            source_chat_id=src_chat_id,
            source_message_id=src_message_id,
            error=str(e),
        )


# --- Command handlers ---


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from .handlers import commands as command_handlers
    await command_handlers.start_command(update, context)


async def folder_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from .handlers import commands as command_handlers
    await command_handlers.folder_command(update, context)


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from .handlers import commands as command_handlers
    await command_handlers.history_command(update, context)


async def unbind_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from .handlers import commands as command_handlers
    await command_handlers.unbind_command(update, context)


async def esc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from .handlers import commands as command_handlers
    await command_handlers.esc_command(update, context)


async def queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from .handlers import commands as command_handlers
    await command_handlers.queue_command(update, context)


async def approvals_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from .handlers import commands as command_handlers
    await command_handlers.approvals_command(update, context)


async def mentions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from .handlers import commands as command_handlers
    await command_handlers.mentions_command(update, context)


async def allowed_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from .handlers import commands as command_handlers
    await command_handlers.allowed_command(update, context)


def _build_skills_overview_text(
    *,
    title: str,
    noun_plural: str,
    command_name: str,
    enabled_skill_names: list[str],
    catalog: dict,
    roots: list[Path],
    roots_label: str = "Skill roots",
) -> str:
    """Build per-topic app/skill overview text."""
    enabled_set = set(enabled_skill_names)
    available = sorted(catalog.values(), key=lambda item: item.name)

    lines = [f"🧩 *{title}*", ""]
    if enabled_skill_names:
        lines.append("Enabled in this topic:")
        for name in enabled_skill_names:
            lines.append(f"- `{name}`")
    else:
        lines.append("Enabled in this topic: (none)")

    lines.extend(["", f"Available {noun_plural}:"])
    if not available:
        lines.append("(none found)")
    else:
        max_visible = 20
        for skill in available[:max_visible]:
            marker = "✅" if skill.name in enabled_set else "•"
            desc = " ".join(skill.description.split())
            if len(desc) > 120:
                desc = desc[:117] + "..."
            lines.append(f"{marker} `{skill.name}` — {desc}")
        remaining = len(available) - max_visible
        if remaining > 0:
            lines.append(f"... +{remaining} more")

    lines.extend(
        [
            "",
            "Usage:",
            f"`/{command_name}` or `/{command_name} list`",
            f"`/{command_name} enable <name>`",
            f"`/{command_name} disable <name>`",
            f"`/{command_name} clear`",
            "",
            f"{roots_label}:",
        ]
    )
    for root in roots:
        lines.append(f"- `{root}`")
    return "\n".join(lines)


def _build_apps_overview_payload(
    *,
    enabled_names: list[str],
    catalog: dict,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build /apps overview text + keyboard payload."""
    text = _build_skills_overview_text(
        title="Topic Apps",
        noun_plural="apps",
        command_name="apps",
        enabled_skill_names=enabled_names,
        catalog=catalog,
        roots=config.apps_paths,
        roots_label="App roots",
    )
    text = (
        f"{text}\n\n"
        "Tap an app button to open actions.\n"
        "Run enables the app for this topic. Configure opens app settings."
    )
    keyboard = _build_apps_panel_keyboard(
        enabled_names=enabled_names,
        catalog=catalog,
    )
    return text, keyboard


async def skills_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from .handlers import commands as command_handlers
    await command_handlers.skills_command(update, context)


async def apps_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from .handlers import commands as command_handlers
    await command_handlers.apps_command(update, context)


def _build_apps_panel_payload_for_topic(
    *,
    user_id: int,
    thread_id: int,
    chat_id: int | None = None,
) -> tuple[str, InlineKeyboardMarkup, dict, list[str]]:
    """Build apps panel payload for one topic."""
    catalog = session_manager.discover_skill_catalog()
    resolved_enabled = session_manager.resolve_thread_skills(
        user_id,
        thread_id,
        chat_id=chat_id,
        catalog=catalog,
    )
    enabled_names = [item.name for item in resolved_enabled]
    text, keyboard = _build_apps_overview_payload(
        enabled_names=enabled_names,
        catalog=catalog,
    )
    return text, keyboard, catalog, enabled_names


def _build_app_actions_payload_for_topic(
    *,
    user_id: int,
    thread_id: int,
    app_identifier: str,
    chat_id: int | None = None,
) -> tuple[bool, str, InlineKeyboardMarkup | None, str]:
    """Build one app action sheet payload for the topic."""
    catalog = session_manager.discover_skill_catalog()
    canonical = resolve_skill_identifier(app_identifier, catalog)
    if not canonical:
        return False, "❌ Unknown app.", None, ""
    app = catalog.get(canonical)
    if not app:
        return False, "❌ Unknown app.", None, ""
    enabled_names = [
        item.name
        for item in session_manager.resolve_thread_skills(
            user_id,
            thread_id,
            chat_id=chat_id,
            catalog=catalog,
        )
    ]
    text = _build_app_actions_text(
        app=app,
        enabled=canonical in enabled_names,
    )
    keyboard = _build_app_actions_keyboard(
        app_name=canonical,
        supports_config=_app_supports_config(canonical),
    )
    return True, text, keyboard, canonical


def _build_autoresearch_panel_text(*, state) -> str:
    """Build interactive autoresearch panel text for /apps."""
    outcome = ""
    last_delivered = ""
    if state is not None:
        outcome = str(state.outcome).strip()
        last_delivered = str(state.last_delivered_for_date).strip()

    lines = [
        "🔎 *Auto Research App*",
        "",
        (
            f"Desired outcome: `{outcome}`"
            if outcome
            else "Desired outcome: `(not set)`"
        ),
        "Schedule: `daily research overnight, delivery after 9am server-local time`",
    ]
    if last_delivered:
        lines.append(f"Last delivered for: `{last_delivered}`")
    lines.extend(
        [
            "",
            "Set the outcome you want Coco to optimize for in this topic.",
            "Examples: `Close more inbound leads`, `Reduce flaky deploys`, `Ship cleaner PRs faster`.",
        ]
    )
    return "\n".join(lines)


def _build_autoresearch_panel_keyboard() -> InlineKeyboardMarkup:
    """Build interactive autoresearch panel keyboard."""
    rows = [
        [
            InlineKeyboardButton(
                "🎯 Set Outcome",
                callback_data=CB_APPS_AUTORESEARCH_OUTCOME,
            )
        ],
        [InlineKeyboardButton("⬅ Back to Apps", callback_data=CB_APPS_BACK)],
    ]
    return InlineKeyboardMarkup(rows)


async def _build_autoresearch_panel_payload_for_topic(
    *,
    user_id: int,
    thread_id: int,
    user_data: dict | None,
    chat_id: int | None = None,
) -> tuple[bool, str, InlineKeyboardMarkup | None, str]:
    """Build autoresearch panel payload for one topic."""
    _ = chat_id
    state = get_autoresearch_state(user_id=user_id, thread_id=thread_id)
    if isinstance(user_data, dict):
        user_data[APPS_PENDING_THREAD_KEY] = thread_id
    return True, _build_autoresearch_panel_text(state=state), _build_autoresearch_panel_keyboard(), ""


async def _build_looper_panel_payload_for_topic(
    *,
    user_id: int,
    thread_id: int,
    user_data: dict | None,
    chat_id: int | None = None,
) -> tuple[bool, str, InlineKeyboardMarkup | None, str]:
    """Build looper panel payload for one topic."""
    wid = session_manager.resolve_window_for_thread(
        user_id,
        thread_id,
        chat_id=chat_id,
    )
    if not wid:
        return False, "❌ No session bound to this topic.", None, ""
    workspace_dir = ""
    workspace_dir = (
        _resolve_workspace_dir_for_window(
            user_id=user_id,
            thread_id=thread_id,
            chat_id=chat_id,
            window_id=wid,
        )
        or ""
    )
    if not workspace_dir:
        return (
            False,
            "❌ Session binding is incomplete. Send a normal message to reinitialize.",
            None,
            "",
        )

    candidates = _list_markdown_plan_candidates(Path(workspace_dir))
    active_state = get_looper_state(user_id=user_id, thread_id=thread_id)
    existing = user_data.get(APPS_LOOPER_CONFIG_KEY) if isinstance(user_data, dict) else None
    config_data = _normalize_looper_panel_config(
        existing,
        candidates=candidates,
        active_state=active_state,
    )
    if isinstance(user_data, dict):
        user_data[APPS_LOOPER_CONFIG_KEY] = config_data
        user_data[APPS_PENDING_THREAD_KEY] = thread_id
        user_data[APPS_PENDING_WINDOW_ID_KEY] = wid
    text = _build_looper_panel_text(
        config_data=config_data,
        active_state=active_state,
    )
    keyboard = _build_looper_panel_keyboard(
        config_data=config_data,
        active_state=active_state,
    )
    return True, text, keyboard, wid


def _build_looper_overview_text(
    *,
    state,
    now: float | None = None,
) -> str:
    """Build /looper status/help text."""
    ts = now if now is not None else time.time()
    if state is None:
        return "\n".join(
            [
                "🔁 *Looper*",
                "",
                "Status: `off`",
                "",
                "Usage:",
                "`/looper start <plan.md> <keyword>`",
                "`/looper start <plan.md> <keyword> --every 10m --limit 1h --instructions \"Focus on tests first\"`",
                "`/looper status`",
                "`/looper stop`",
                "",
                "Examples:",
                "`/looper start docs/launch-plan.md done`",
                "`/looper start plans/release.md ship --every 15m --limit 2h`",
            ]
        )

    next_in = max(0, int(state.next_prompt_at - ts))
    lines = [
        "🔁 *Looper*",
        "",
        "Status: `on`",
        f"Plan file: `{state.plan_path}`",
        f"Completion keyword: `{state.keyword}`",
        f"Interval: `{_format_duration_brief(state.interval_seconds)}`",
        f"Next nudge in: `{_format_duration_brief(next_in)}`",
    ]
    if state.deadline_at > 0:
        deadline_in = max(0, int(state.deadline_at - ts))
        lines.append(f"Time limit remaining: `{_format_duration_brief(deadline_in)}`")
    else:
        lines.append("Time limit: `(none)`")

    lines.append(f"Nudges sent: `{state.prompt_count}`")
    if state.instructions:
        lines.append(f"Custom instructions: `{state.instructions}`")
    else:
        lines.append("Custom instructions: `(none)`")

    lines.extend(
        [
            "",
            "Commands:",
            "`/looper status`",
            "`/looper stop`",
        ]
    )
    return "\n".join(lines)


async def looper_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from .handlers import commands as command_handlers
    await command_handlers.looper_command(update, context)


async def worktree_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from .handlers import commands as command_handlers
    await command_handlers.worktree_command(update, context)


def _build_restart_exec_argv() -> list[str]:
    """Build argv for re-execing the current bot process."""
    argv = list(sys.argv)
    if not argv:
        return [sys.executable, "-m", "coco.main"]

    arg0 = argv[0]
    script_path: str | None = None
    if "/" in arg0 or arg0.startswith("."):
        script_path = arg0
    elif os.path.isabs(arg0):
        script_path = arg0
    else:
        script_path = shutil.which(arg0)

    if script_path:
        return [sys.executable, script_path, *argv[1:]]
    return [sys.executable, "-m", "coco.main", *argv[1:]]


async def _restart_process_after_delay(delay_seconds: float = 0.25) -> None:
    """Replace this process with a fresh CoCo process."""
    global _restart_requested
    await asyncio.sleep(delay_seconds)
    args = _build_restart_exec_argv()
    logger.warning("Restarting CoCo process with args: %s", args)
    try:
        os.execv(sys.executable, args)
    except Exception:
        _restart_requested = False
        logger.exception("Failed to restart CoCo process")


@dataclass(frozen=True)
class _CocoUpdateSnapshot:
    """Snapshot of CoCo runtime repo status + update strategy."""

    repo_root: str
    current_branch: str
    upstream_ref: str
    current_commit: str
    latest_commit: str
    behind_count: int
    ahead_count: int
    dirty: bool
    check_error: str
    update_command: str
    update_source: str


@dataclass(frozen=True)
class _CodexUpdateSnapshot:
    """Snapshot of local Codex version status + upgrade strategy."""

    codex_binary: str
    current_version: str
    latest_version: str
    behind: bool | None
    check_error: str
    upgrade_command: str
    upgrade_source: str


def _extract_semver(text: str) -> str:
    """Extract first semver-looking token from text."""
    match = re.search(r"(?<!\d)(\d+\.\d+\.\d+)", text)
    if not match:
        return ""
    return match.group(1)


def _compare_semver(a: str, b: str) -> int | None:
    """Compare two semver strings.

    Returns:
        -1 when a < b
         0 when a == b
         1 when a > b
         None when either side is not parseable
    """
    pa = _extract_semver(a)
    pb = _extract_semver(b)
    if not pa or not pb:
        return None
    try:
        la = tuple(int(part) for part in pa.split("."))
        lb = tuple(int(part) for part in pb.split("."))
    except ValueError:
        return None
    if la < lb:
        return -1
    if la > lb:
        return 1
    return 0


def _tail_text(text: str, *, limit: int = 500) -> str:
    """Return a compact tail of text for error summaries."""
    cleaned = text.strip()
    if len(cleaned) <= limit:
        return cleaned
    return "… " + cleaned[-(limit - 2) :]


def _run_command_sync(
    argv: list[str],
    *,
    timeout_seconds: int,
    cwd: str | Path | None = None,
) -> tuple[bool, str, str, str]:
    """Run one command synchronously and capture compact diagnostics."""
    try:
        proc = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout_seconds,
            cwd=str(cwd) if cwd is not None else None,
        )
    except subprocess.TimeoutExpired as e:
        out = e.stdout or ""
        err = e.stderr or ""
        return False, str(out), str(err), f"timed out after {timeout_seconds}s"
    except OSError as e:
        return False, "", "", str(e)

    if proc.returncode != 0:
        return (
            False,
            proc.stdout or "",
            proc.stderr or "",
            f"exit code {proc.returncode}",
        )
    return True, proc.stdout or "", proc.stderr or "", ""


def _env_int(name: str, *, default: int) -> int:
    """Parse one integer env var with a safe default."""
    raw = env_alias(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, *, default: bool) -> bool:
    """Parse one bool-ish env var with a safe default."""
    raw = env_alias(name)
    if not raw:
        return default
    return raw.lower() not in {"0", "false", "no", "off"}


def _short_commit(commit: str) -> str:
    """Render a compact commit identifier."""
    token = commit.strip()
    if not token:
        return "<unknown>"
    return token[:7]


def _split_upstream_ref(upstream_ref: str) -> tuple[str, str]:
    """Split git upstream ref into remote name and branch name."""
    value = upstream_ref.strip()
    if not value or "/" not in value:
        return "", ""
    remote, branch = value.split("/", 1)
    return remote.strip(), branch.strip()


def _resolve_coco_repo_root_sync() -> tuple[str, str]:
    """Resolve the CoCo git checkout root from the runtime source tree."""
    candidate = Path(__file__).resolve().parents[2]
    ok, stdout, stderr, err = _run_command_sync(
        ["git", "rev-parse", "--show-toplevel"],
        timeout_seconds=_COCO_UPDATE_CHECK_TIMEOUT_SECONDS,
        cwd=candidate,
    )
    if not ok:
        detail = _tail_text(stderr or stdout or err or "unknown git error")
        return "", f"runtime is not a git checkout: {detail}"
    return stdout.strip(), ""


def _resolve_coco_update_command(repo_root: str, upstream_ref: str) -> tuple[str, str]:
    """Resolve command shown to operators for CoCo self-update."""
    custom = env_alias(_COCO_SELF_UPDATE_COMMAND_ENV)
    if custom:
        return custom, "custom"
    remote, branch = _split_upstream_ref(upstream_ref)
    if not repo_root or not remote or not branch:
        return "", "none"
    command = f"git pull --ff-only {shlex.quote(remote)} {shlex.quote(branch)}"
    if shutil.which("uv") and (Path(repo_root) / "pyproject.toml").is_file():
        command = f"{command} && uv sync"
    return command, "git"


def _collect_coco_update_snapshot_sync(*, fetch_remote: bool) -> _CocoUpdateSnapshot:
    """Collect local/current/latest CoCo repo update details."""
    repo_root, repo_err = _resolve_coco_repo_root_sync()
    errors: list[str] = []
    if repo_err:
        errors.append(repo_err)
    if not repo_root:
        update_command, update_source = _resolve_coco_update_command("", "")
        return _CocoUpdateSnapshot(
            repo_root="",
            current_branch="",
            upstream_ref="",
            current_commit="",
            latest_commit="",
            behind_count=0,
            ahead_count=0,
            dirty=False,
            check_error="\n".join(errors).strip(),
            update_command=update_command,
            update_source=update_source,
        )

    current_branch = ""
    upstream_ref = ""
    current_commit = ""
    latest_commit = ""
    behind_count = 0
    ahead_count = 0
    dirty = False

    ok, stdout, stderr, err = _run_command_sync(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        timeout_seconds=_COCO_UPDATE_CHECK_TIMEOUT_SECONDS,
        cwd=repo_root,
    )
    if ok:
        current_branch = stdout.strip()
    else:
        errors.append(f"branch check failed: {_tail_text(stderr or stdout or err or 'unknown error')}")

    ok, stdout, stderr, err = _run_command_sync(
        ["git", "rev-parse", "HEAD"],
        timeout_seconds=_COCO_UPDATE_CHECK_TIMEOUT_SECONDS,
        cwd=repo_root,
    )
    if ok:
        current_commit = stdout.strip()
    else:
        errors.append(f"commit check failed: {_tail_text(stderr or stdout or err or 'unknown error')}")

    ok, stdout, stderr, err = _run_command_sync(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        timeout_seconds=_COCO_UPDATE_CHECK_TIMEOUT_SECONDS,
        cwd=repo_root,
    )
    if ok:
        upstream_ref = stdout.strip()
    else:
        errors.append("upstream branch is not configured")

    ok, stdout, stderr, err = _run_command_sync(
        ["git", "status", "--porcelain"],
        timeout_seconds=_COCO_UPDATE_CHECK_TIMEOUT_SECONDS,
        cwd=repo_root,
    )
    if ok:
        dirty = bool(stdout.strip())
    else:
        errors.append(f"worktree check failed: {_tail_text(stderr or stdout or err or 'unknown error')}")

    if fetch_remote and upstream_ref:
        remote, branch = _split_upstream_ref(upstream_ref)
        if remote and branch:
            ok, stdout, stderr, err = _run_command_sync(
                ["git", "fetch", "--quiet", remote, branch],
                timeout_seconds=_COCO_UPDATE_CHECK_TIMEOUT_SECONDS,
                cwd=repo_root,
            )
            if not ok:
                errors.append(f"fetch failed: {_tail_text(stderr or stdout or err or 'unknown error')}")

    if upstream_ref:
        ok, stdout, stderr, err = _run_command_sync(
            ["git", "rev-parse", "@{upstream}"],
            timeout_seconds=_COCO_UPDATE_CHECK_TIMEOUT_SECONDS,
            cwd=repo_root,
        )
        if ok:
            latest_commit = stdout.strip()
        else:
            errors.append(f"upstream commit check failed: {_tail_text(stderr or stdout or err or 'unknown error')}")

        ok, stdout, stderr, err = _run_command_sync(
            ["git", "rev-list", "--left-right", "--count", "HEAD...@{upstream}"],
            timeout_seconds=_COCO_UPDATE_CHECK_TIMEOUT_SECONDS,
            cwd=repo_root,
        )
        if ok:
            parts = stdout.strip().split()
            if len(parts) == 2:
                try:
                    ahead_count = int(parts[0])
                    behind_count = int(parts[1])
                except ValueError:
                    errors.append(f"unexpected rev-list counts: {stdout.strip()}")
        else:
            errors.append(f"divergence check failed: {_tail_text(stderr or stdout or err or 'unknown error')}")

    update_command, update_source = _resolve_coco_update_command(repo_root, upstream_ref)
    return _CocoUpdateSnapshot(
        repo_root=repo_root,
        current_branch=current_branch,
        upstream_ref=upstream_ref,
        current_commit=current_commit,
        latest_commit=latest_commit,
        behind_count=behind_count,
        ahead_count=ahead_count,
        dirty=dirty,
        check_error="\n".join(errors).strip(),
        update_command=update_command,
        update_source=update_source,
    )


async def _collect_coco_update_snapshot(*, fetch_remote: bool = True) -> _CocoUpdateSnapshot:
    """Collect CoCo self-update status without blocking the event loop."""
    return await asyncio.to_thread(
        _collect_coco_update_snapshot_sync,
        fetch_remote=fetch_remote,
    )


def _build_coco_update_state(snapshot: _CocoUpdateSnapshot) -> str:
    """Render a human-readable CoCo update state label."""
    if snapshot.behind_count > 0 and snapshot.ahead_count > 0:
        return (
            f"Needs attention: behind {snapshot.behind_count}, ahead {snapshot.ahead_count}."
        )
    if snapshot.behind_count > 0:
        return f"Update available ({snapshot.behind_count} commit(s) behind)."
    if snapshot.ahead_count > 0:
        return f"Local branch is ahead by {snapshot.ahead_count} commit(s)."
    if snapshot.current_commit and snapshot.latest_commit:
        return "Up to date."
    return "Version comparison unavailable."


def _fetch_latest_codex_version_sync() -> tuple[str, str]:
    """Fetch latest Codex CLI version from npm registry."""
    req = urllib.request.Request(
        _CODEX_NPM_LATEST_URL,
        headers={
            "Accept": "application/json",
            "User-Agent": "coco-update-check/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        return "", str(e)

    raw_version = payload.get("version")
    if not isinstance(raw_version, str) or not raw_version.strip():
        return "", "registry payload missing version"
    return raw_version.strip(), ""


def _resolve_codex_upgrade_command() -> tuple[str, str]:
    """Resolve command used for Codex CLI upgrade."""
    custom = env_alias(_CODEX_UPGRADE_COMMAND_ENV)
    if custom:
        return custom, "custom"
    if shutil.which("uv"):
        return "uv tool upgrade codex", "uv"
    if shutil.which("pipx"):
        return "pipx upgrade codex", "pipx"
    if shutil.which("npm"):
        return "npm install -g @openai/codex@latest", "npm"
    return "", "none"


async def _collect_codex_update_snapshot() -> _CodexUpdateSnapshot:
    """Collect local/current/latest Codex update details."""
    codex_binary = _resolve_codex_exec_binary() or "codex"

    current_version = ""
    check_errors: list[str] = []
    ok, stdout, stderr, err = await asyncio.to_thread(
        _run_command_sync,
        [codex_binary, "--version"],
        timeout_seconds=_CODEX_VERSION_CHECK_TIMEOUT_SECONDS,
    )
    version_text = (stdout or stderr).strip()
    if ok:
        current_version = _extract_semver(version_text) or version_text
    else:
        if err:
            check_errors.append(f"local version check failed: {err}")
        if version_text:
            check_errors.append(_tail_text(version_text))

    latest_version, latest_err = await asyncio.to_thread(
        _fetch_latest_codex_version_sync
    )
    if latest_err:
        check_errors.append(f"registry check failed: {latest_err}")

    cmp = _compare_semver(current_version, latest_version)
    behind = cmp < 0 if cmp is not None else None

    upgrade_command, upgrade_source = _resolve_codex_upgrade_command()
    return _CodexUpdateSnapshot(
        codex_binary=codex_binary,
        current_version=current_version,
        latest_version=latest_version,
        behind=behind,
        check_error="\n".join(check_errors).strip(),
        upgrade_command=upgrade_command,
        upgrade_source=upgrade_source,
    )


def _build_update_panel_text(
    coco_snapshot: _CocoUpdateSnapshot,
    codex_snapshot: _CodexUpdateSnapshot,
    *,
    can_trigger_upgrade: bool,
) -> str:
    """Build combined /update panel text."""
    codex_current = codex_snapshot.current_version or "<unknown>"
    codex_latest = codex_snapshot.latest_version or "<unknown>"
    if codex_snapshot.behind is True:
        codex_state = "Behind latest release."
    elif codex_snapshot.behind is False:
        codex_state = "Up to date."
    else:
        codex_state = "Version comparison unavailable."

    lines = [
        "⬆️ *Update Center*",
        "",
        "*CoCo Update*",
        f"Repo: `{coco_snapshot.repo_root or '<unknown>'}`",
        f"Branch: `{coco_snapshot.current_branch or '<unknown>'}`",
        f"Upstream: `{coco_snapshot.upstream_ref or '<unknown>'}`",
        f"Current commit: `{_short_commit(coco_snapshot.current_commit)}`",
        f"Latest commit: `{_short_commit(coco_snapshot.latest_commit)}`",
        f"State: {_build_coco_update_state(coco_snapshot)}",
        f"Worktree: {'dirty' if coco_snapshot.dirty else 'clean'}",
    ]
    if coco_snapshot.update_command:
        lines.append(
            f"Apply via `{coco_snapshot.update_source}`: `{coco_snapshot.update_command}`"
        )
    else:
        lines.append(
            "Apply command: unavailable (set `COCO_SELF_UPDATE_COMMAND`)."
        )

    lines.extend(
        [
            "",
            "*Codex Update*",
            f"Binary: `{codex_snapshot.codex_binary}`",
            f"Current: `{codex_current}`",
            f"Latest: `{codex_latest}`",
            f"State: {codex_state}",
        ]
    )
    if codex_snapshot.upgrade_command:
        lines.append(
            f"Upgrade via `{codex_snapshot.upgrade_source}`: `{codex_snapshot.upgrade_command}`"
        )
    else:
        lines.append(
            "Upgrade command: unavailable (set `COCO_CODEX_UPGRADE_COMMAND`)."
        )

    if coco_snapshot.check_error:
        lines.extend(["", "CoCo check notes:", coco_snapshot.check_error])
    if codex_snapshot.check_error:
        lines.extend(["", "Codex check notes:", codex_snapshot.check_error])

    lines.extend(
        [
            "",
            (
                "Admins can apply CoCo, Codex, or both from this panel."
                if can_trigger_upgrade
                else "Only admins can apply updates from this panel."
            ),
        ]
    )
    return "\n".join(lines)


def _build_update_panel_keyboard(*, can_trigger_upgrade: bool) -> InlineKeyboardMarkup:
    """Build /update inline navigation keyboard."""
    rows: list[list[InlineKeyboardButton]] = []
    if can_trigger_upgrade:
        rows.append(
            [
                InlineKeyboardButton("Update CoCo", callback_data=CB_UPDATE_RUN_COCO),
                InlineKeyboardButton("Update Codex", callback_data=CB_UPDATE_RUN_CODEX),
            ]
        )
        rows.append(
            [InlineKeyboardButton("Update Both", callback_data=CB_UPDATE_RUN_BOTH)]
        )
    rows.append([InlineKeyboardButton("Refresh", callback_data=CB_UPDATE_REFRESH)])
    return InlineKeyboardMarkup(rows)


def _update_notice_targets() -> list[tuple[int, int | None]]:
    """Resolve admin private-chat recipients for automatic update notices."""
    admin_ids = sorted(_get_allowed_admins())
    if not admin_ids:
        return []
    return [(user_id, None) for user_id in admin_ids]


def _load_update_notice_state() -> dict[str, str]:
    """Load persisted update notice state."""
    if not _UPDATE_NOTICE_STATE_FILE.is_file():
        return {}
    try:
        payload = json.loads(_UPDATE_NOTICE_STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug(
            "Failed reading update notice file %s: %s",
            _UPDATE_NOTICE_STATE_FILE,
            e,
        )
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        "latest_commit": str(payload.get("latest_commit", "")).strip(),
        "upstream_ref": str(payload.get("upstream_ref", "")).strip(),
        "latest_codex_version": str(payload.get("latest_codex_version", "")).strip(),
    }


def _write_update_notice_state(state: dict[str, str]) -> None:
    """Persist update notice marker state."""
    try:
        atomic_write_json(_UPDATE_NOTICE_STATE_FILE, state)
    except OSError as e:
        logger.debug(
            "Failed writing update notice file %s: %s",
            _UPDATE_NOTICE_STATE_FILE,
            e,
        )


def _store_coco_update_notice_state(snapshot: _CocoUpdateSnapshot) -> None:
    """Persist latest CoCo update notice marker."""
    state = _load_update_notice_state()
    state["latest_commit"] = snapshot.latest_commit
    state["upstream_ref"] = snapshot.upstream_ref
    _write_update_notice_state(state)


def _store_codex_update_notice_state(snapshot: _CodexUpdateSnapshot) -> None:
    """Persist latest Codex update notice marker."""
    state = _load_update_notice_state()
    state["latest_codex_version"] = snapshot.latest_version
    _write_update_notice_state(state)


def _build_coco_update_notice_text(snapshot: _CocoUpdateSnapshot) -> str:
    """Build concise Telegram text for an available CoCo update."""
    return "\n".join(
        [
            "⬆️ *CoCo Update Available*",
            "",
            f"Branch: `{snapshot.current_branch or '<unknown>'}`",
            f"Upstream: `{snapshot.upstream_ref or '<unknown>'}`",
            f"Current commit: `{_short_commit(snapshot.current_commit)}`",
            f"Latest commit: `{_short_commit(snapshot.latest_commit)}`",
            f"State: {_build_coco_update_state(snapshot)}",
            "",
            "Use the buttons below to apply the CoCo update, update Codex too, or refresh the full panel.",
        ]
    )


def _build_codex_update_notice_text(snapshot: _CodexUpdateSnapshot) -> str:
    """Build concise Telegram text for an available Codex update."""
    current_version = snapshot.current_version or "<unknown>"
    latest_version = snapshot.latest_version or "<unknown>"
    return "\n".join(
        [
            "⬆️ *Codex Update Available*",
            "",
            f"Binary: `{snapshot.codex_binary or '<unknown>'}`",
            f"Current: `{current_version}`",
            f"Latest: `{latest_version}`",
            "State: Behind latest release.",
            "",
            "Use the buttons below to update Codex, update CoCo too, or refresh the full panel.",
        ]
    )


async def _maybe_send_coco_update_notice(bot_obj: Bot, snapshot: _CocoUpdateSnapshot) -> bool:
    """Notify admins when a new CoCo update becomes available."""
    if snapshot.behind_count <= 0 or not snapshot.latest_commit:
        return False
    targets = _update_notice_targets()
    if not targets:
        return False
    state = _load_update_notice_state()
    if (
        state.get("latest_commit") == snapshot.latest_commit
        and state.get("upstream_ref") == snapshot.upstream_ref
    ):
        return False

    text = _build_coco_update_notice_text(snapshot)
    keyboard = _build_update_panel_keyboard(can_trigger_upgrade=True)
    for chat_id, thread_id in targets:
        await safe_send(
            bot_obj,
            chat_id,
            text,
            message_thread_id=thread_id,
            reply_markup=keyboard,
        )
    _store_coco_update_notice_state(snapshot)
    return True


async def _maybe_send_codex_update_notice(
    bot_obj: Bot,
    snapshot: _CodexUpdateSnapshot,
) -> bool:
    """Notify admins when a new Codex update becomes available."""
    if snapshot.behind is not True or not snapshot.latest_version:
        return False
    targets = _update_notice_targets()
    if not targets:
        return False
    state = _load_update_notice_state()
    if state.get("latest_codex_version") == snapshot.latest_version:
        return False

    text = _build_codex_update_notice_text(snapshot)
    keyboard = _build_update_panel_keyboard(can_trigger_upgrade=True)
    for chat_id, thread_id in targets:
        await safe_send(
            bot_obj,
            chat_id,
            text,
            message_thread_id=thread_id,
            reply_markup=keyboard,
        )
    _store_codex_update_notice_state(snapshot)
    return True


async def _coco_update_check_loop(bot_obj: Bot) -> None:
    """Periodically check for CoCo and Codex updates and notify admins."""
    initial_delay = max(
        0,
        _env_int(_COCO_UPDATE_CHECK_INITIAL_DELAY_ENV, default=60),
    )
    interval = max(
        300,
        _env_int(_COCO_UPDATE_CHECK_INTERVAL_ENV, default=6 * 60 * 60),
    )
    if initial_delay:
        await asyncio.sleep(initial_delay)
    while True:
        try:
            coco_snapshot, codex_snapshot = await asyncio.gather(
                _collect_coco_update_snapshot(fetch_remote=True),
                _collect_codex_update_snapshot(),
            )
            await _maybe_send_coco_update_notice(bot_obj, coco_snapshot)
            await _maybe_send_codex_update_notice(bot_obj, codex_snapshot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Update check failed")
        await asyncio.sleep(interval)


async def _build_update_panel_payload(
    *,
    can_trigger_upgrade: bool,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build /update panel text + keyboard payload."""
    coco_snapshot, codex_snapshot = await asyncio.gather(
        _collect_coco_update_snapshot(),
        _collect_codex_update_snapshot(),
    )
    text = _build_update_panel_text(
        coco_snapshot,
        codex_snapshot,
        can_trigger_upgrade=can_trigger_upgrade,
    )
    keyboard = _build_update_panel_keyboard(
        can_trigger_upgrade=can_trigger_upgrade,
    )
    return text, keyboard


async def _run_codex_upgrade() -> tuple[bool, str]:
    """Run the Codex upgrade command without restarting CoCo."""
    before = await _collect_codex_update_snapshot()
    if not before.upgrade_command:
        return (
            False,
            "No supported Codex upgrade command found. "
            "Set `COCO_CODEX_UPGRADE_COMMAND` to enable this action.",
        )

    try:
        argv = shlex.split(before.upgrade_command)
    except ValueError as e:
        return False, f"Invalid upgrade command syntax: {e}"
    if not argv:
        return False, "Upgrade command is empty."

    ok, stdout, stderr, err = await asyncio.to_thread(
        _run_command_sync,
        argv,
        timeout_seconds=_CODEX_UPGRADE_TIMEOUT_SECONDS,
    )
    if not ok:
        detail = _tail_text(stderr or stdout or err or "unknown error")
        return False, f"Upgrade failed ({err or 'error'}): {detail}"

    after = await _collect_codex_update_snapshot()
    if not after.current_version:
        detail = after.check_error or "post-upgrade Codex version check failed"
        return False, f"Upgrade completed but Codex is unhealthy: {_tail_text(detail)}"

    before_version = before.current_version or "<unknown>"
    after_version = after.current_version
    if before_version == after_version:
        return (
            True,
            f"Codex upgrade completed (version unchanged at `{after_version}`).",
        )
    return (
        True,
        f"Codex upgraded: `{before_version}` -> `{after_version}`.",
    )


async def _run_coco_update() -> tuple[bool, str]:
    """Run the CoCo self-update command without restarting CoCo."""
    before = await _collect_coco_update_snapshot(fetch_remote=True)
    if not before.repo_root:
        detail = before.check_error or "runtime is not a git checkout"
        return False, f"CoCo update unavailable: {_tail_text(detail)}"
    if before.dirty:
        return False, "CoCo update blocked: worktree has local changes."
    if before.ahead_count > 0:
        return False, (
            "CoCo update blocked: local branch is ahead of upstream. "
            "Push or reconcile local commits first."
        )
    if not before.upstream_ref:
        return False, "CoCo update unavailable: upstream branch is not configured."

    custom = env_alias(_COCO_SELF_UPDATE_COMMAND_ENV)
    if custom:
        ok, stdout, stderr, err = await asyncio.to_thread(
            _run_command_sync,
            ["bash", "-lc", custom],
            timeout_seconds=_COCO_UPDATE_TIMEOUT_SECONDS,
            cwd=before.repo_root,
        )
        if not ok:
            detail = _tail_text(stderr or stdout or err or "unknown error")
            return False, f"CoCo update failed ({err or 'error'}): {detail}"
    else:
        remote, branch = _split_upstream_ref(before.upstream_ref)
        if not remote or not branch:
            return False, "CoCo update unavailable: upstream branch is not configured."
        ok, stdout, stderr, err = await asyncio.to_thread(
            _run_command_sync,
            ["git", "pull", "--ff-only", remote, branch],
            timeout_seconds=_COCO_UPDATE_TIMEOUT_SECONDS,
            cwd=before.repo_root,
        )
        if not ok:
            detail = _tail_text(stderr or stdout or err or "unknown error")
            return False, f"CoCo update failed ({err or 'error'}): {detail}"
        if shutil.which("uv") and (Path(before.repo_root) / "pyproject.toml").is_file():
            ok, stdout, stderr, err = await asyncio.to_thread(
                _run_command_sync,
                ["uv", "sync"],
                timeout_seconds=_COCO_UPDATE_TIMEOUT_SECONDS,
                cwd=before.repo_root,
            )
            if not ok:
                detail = _tail_text(stderr or stdout or err or "unknown error")
                return False, f"CoCo dependency sync failed ({err or 'error'}): {detail}"

    after = await _collect_coco_update_snapshot(fetch_remote=False)
    if not after.current_commit:
        detail = after.check_error or "post-update repo check failed"
        return False, f"CoCo update completed but repo check failed: {_tail_text(detail)}"
    if after.behind_count > 0:
        return False, (
            "CoCo update command completed but the repo is still behind upstream."
        )

    before_commit = _short_commit(before.current_commit)
    after_commit = _short_commit(after.current_commit)
    if before.current_commit == after.current_commit:
        return True, f"CoCo update completed (commit unchanged at `{after_commit}`)."
    return True, f"CoCo updated: `{before_commit}` -> `{after_commit}`."


def _queue_restart(chat_id: int, thread_id: int | None) -> bool:
    """Queue a CoCo restart if one is not already pending."""
    global _restart_requested
    if _restart_requested:
        return False
    _set_restart_notice_target(chat_id, thread_id)
    _restart_requested = True
    asyncio.create_task(_restart_process_after_delay())
    return True


async def _run_codex_upgrade_and_restart(
    *,
    chat_id: int,
    thread_id: int | None,
) -> tuple[bool, str]:
    """Run Codex upgrade, then restart CoCo on success."""
    if _restart_requested:
        return False, "Restart already in progress."
    ok, text = await _run_codex_upgrade()
    if not ok:
        return False, text
    if not _queue_restart(chat_id, thread_id):
        return False, "Restart already in progress."
    return True, f"{text} Restarting CoCo now."


async def _run_coco_update_and_restart(
    *,
    chat_id: int,
    thread_id: int | None,
) -> tuple[bool, str]:
    """Run CoCo self-update, then restart CoCo on success."""
    if _restart_requested:
        return False, "Restart already in progress."
    ok, text = await _run_coco_update()
    if not ok:
        return False, text
    if not _queue_restart(chat_id, thread_id):
        return False, "Restart already in progress."
    return True, f"{text} Restarting CoCo now."


async def _run_both_updates_and_restart(
    *,
    chat_id: int,
    thread_id: int | None,
) -> tuple[bool, str]:
    """Run CoCo self-update and Codex upgrade, then restart once."""
    if _restart_requested:
        return False, "Restart already in progress."
    coco_ok, coco_text = await _run_coco_update()
    if not coco_ok:
        return False, coco_text
    codex_ok, codex_text = await _run_codex_upgrade()
    if not codex_ok:
        return False, f"{coco_text}\n\n{codex_text}"
    if not _queue_restart(chat_id, thread_id):
        return False, "Restart already in progress."
    return True, f"{coco_text}\n{codex_text}\nRestarting CoCo now."


async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from .handlers import commands as command_handlers
    await command_handlers.restart_command(update, context)


def _extract_thread_ids_from_list_payload(payload: dict[str, object]) -> list[str]:
    """Extract thread IDs from app-server thread/list payload."""
    ids: list[str] = []
    raw_items = payload.get("threads")
    if not isinstance(raw_items, list):
        raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raw_items = []

    for item in raw_items:
        if not isinstance(item, dict):
            continue
        thread_id = _extract_lifecycle_thread_id(item, fallback="")
        if not thread_id:
            direct = item.get("id")
            if isinstance(direct, str):
                thread_id = direct.strip()
        if thread_id and thread_id not in ids:
            ids.append(thread_id)
    return ids


def _extract_thread_list_next_cursor(payload: dict[str, object]) -> str:
    """Extract next cursor token from app-server thread/list payload."""
    for key in ("nextCursor", "nextPageCursor", "next"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


async def _list_all_session_threads(
    *,
    machine_id: str = "",
    max_items: int = SESSION_PANEL_LIST_LIMIT,
) -> tuple[list[str], str]:
    """Fetch resumable thread ids across paginated app-server thread/list."""
    local_machine_id, _local_machine_name = _local_machine_identity()
    if machine_id and machine_id != local_machine_id:
        from .agent_rpc import agent_rpc_client

        return await agent_rpc_client.list_threads(machine_id, max_items=max_items)

    all_ids: list[str] = []
    list_error = ""
    cursor: str | None = None
    seen_cursors: set[str] = set()
    truncated = False

    while len(all_ids) < max_items:
        remaining = max_items - len(all_ids)
        request_limit = max(1, min(SESSION_PANEL_LIST_REQUEST_LIMIT, remaining))
        try:
            payload = await codex_app_server_client.thread_list(
                cursor=cursor,
                limit=request_limit,
            )
        except Exception as e:
            list_error = str(e)
            break

        page_ids = _extract_thread_ids_from_list_payload(payload)
        for thread_id in page_ids:
            if thread_id not in all_ids:
                all_ids.append(thread_id)
                if len(all_ids) >= max_items:
                    break

        next_cursor = _extract_thread_list_next_cursor(payload)
        if len(all_ids) >= max_items:
            truncated = bool(next_cursor)
            break
        if not next_cursor:
            break
        if next_cursor in seen_cursors:
            list_error = "thread/list returned a repeated cursor; showing available results."
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    if truncated:
        note = f"Showing first {max_items} threads."
        list_error = f"{list_error}; {note}" if list_error else note
    return all_ids, list_error


def _resolve_session_panel_page(thread_count: int, page: int) -> tuple[int, int]:
    """Return (resolved_page, total_pages) for session panel pagination."""
    per_page = SESSION_PANEL_THREADS_PER_PAGE
    if thread_count <= 0:
        return 0, 1
    total_pages = (thread_count + per_page - 1) // per_page
    resolved = max(0, min(page, total_pages - 1))
    return resolved, total_pages


def _session_picker_page_from_context(
    context_user_data: dict | None,
    *,
    thread_id: int | None,
    window_id: str,
) -> int:
    """Resolve last viewed session panel page from callback context."""
    if not isinstance(context_user_data, dict):
        return 0
    picker = context_user_data.get(SESSION_PICKER_THREADS_KEY)
    if not isinstance(picker, dict):
        return 0
    if picker.get("thread_id") != (thread_id or 0):
        return 0
    if picker.get("window_id") != window_id:
        return 0
    raw_page = picker.get("page")
    if isinstance(raw_page, int) and raw_page >= 0:
        return raw_page
    return 0


def _short_thread_id(thread_id: str) -> str:
    """Compact thread ID for inline button labels."""
    value = thread_id.strip()
    if len(value) <= 14:
        return value
    return f"{value[:7]}…{value[-6:]}"


def _build_session_panel_text(
    *,
    display: str,
    current_thread_id: str,
    current_turn_id: str,
    available_threads: list[str],
    page: int = 0,
    list_error: str = "",
) -> str:
    """Build /resume interactive panel text."""
    resolved_page, total_pages = _resolve_session_panel_page(len(available_threads), page)
    start = resolved_page * SESSION_PANEL_THREADS_PER_PAGE
    end = start + SESSION_PANEL_THREADS_PER_PAGE
    visible_threads = available_threads[start:end]

    lines = [
        "🧵 *Session Lifecycle*",
        "",
        f"Session: `{display}`",
        f"Current thread: `{current_thread_id or '<uninitialized>'}`",
        f"Active turn: `{current_turn_id or '<none>'}`",
        "",
        "Recent resumable threads:",
    ]
    if visible_threads:
        for idx, thread_id in enumerate(visible_threads, start=start + 1):
            marker = "✅ " if thread_id == current_thread_id else ""
            lines.append(f"{idx}. {marker}`{thread_id}`")
    else:
        lines.append("None discovered yet.")
    lines.append(f"Page: `{resolved_page + 1}/{total_pages}`")
    if list_error:
        lines.extend(["", f"Lookup note: `{list_error}`"])
    lines.extend(
        [
            "",
            "Buttons: fork, rollback, resume latest-by-folder, or pick any listed thread.",
            "Use `/resume` to reopen this menu anytime.",
        ]
    )
    return "\n".join(lines)


def _build_session_panel_keyboard(
    *,
    current_thread_id: str,
    available_threads: list[str],
    page: int = 0,
) -> InlineKeyboardMarkup:
    """Build /resume lifecycle keyboard."""
    resolved_page, total_pages = _resolve_session_panel_page(len(available_threads), page)
    start = resolved_page * SESSION_PANEL_THREADS_PER_PAGE
    end = start + SESSION_PANEL_THREADS_PER_PAGE
    visible_threads = available_threads[start:end]

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("Fork", callback_data=CB_SESSION_FORK),
            InlineKeyboardButton("Refresh", callback_data=CB_SESSION_REFRESH),
        ],
        [
            InlineKeyboardButton("Rollback 1", callback_data=f"{CB_SESSION_ROLLBACK}1"),
            InlineKeyboardButton("Rollback 3", callback_data=f"{CB_SESSION_ROLLBACK}3"),
        ],
        [
            InlineKeyboardButton("Resume Latest (cwd)", callback_data=CB_SESSION_RESUME_LATEST),
        ],
    ]
    if total_pages > 1:
        nav_row: list[InlineKeyboardButton] = []
        if resolved_page > 0:
            nav_row.append(
                InlineKeyboardButton(
                    "Prev",
                    callback_data=f"{CB_SESSION_PAGE}{resolved_page - 1}",
                )
            )
        if resolved_page < total_pages - 1:
            nav_row.append(
                InlineKeyboardButton(
                    "Next",
                    callback_data=f"{CB_SESSION_PAGE}{resolved_page + 1}",
                )
            )
        if nav_row:
            rows.append(nav_row)

    for idx, thread_id in enumerate(visible_threads, start=start):
        marker = "✅ " if thread_id == current_thread_id else ""
        rows.append(
            [
                InlineKeyboardButton(
                    f"{marker}Resume {_short_thread_id(thread_id)}"[:64],
                    callback_data=f"{CB_SESSION_RESUME}{idx}"[:64],
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


async def _build_session_panel_payload(
    *,
    user_id: int,
    thread_id: int | None,
    context_user_data: dict | None,
    chat_id: int | None = None,
    page: int = 0,
) -> tuple[bool, str, InlineKeyboardMarkup]:
    """Build /resume panel text + keyboard for command/callback paths."""
    wid = session_manager.resolve_window_for_thread(
        user_id,
        thread_id,
        chat_id=chat_id,
    )
    if not wid:
        return False, "❌ No session bound to this topic.", InlineKeyboardMarkup([])

    state = session_manager.get_window_state(wid)
    machine_id = session_manager.get_window_machine_id(wid)
    current_thread_id = state.codex_thread_id.strip()
    current_turn_id = (
        state.codex_active_turn_id.strip()
        or (codex_app_server_client.get_active_turn_id(current_thread_id) or "")
    )
    display = session_manager.get_display_name(wid)

    available_threads, list_error = await _list_all_session_threads(machine_id=machine_id)

    if current_thread_id and current_thread_id not in available_threads:
        available_threads.insert(0, current_thread_id)
    resolved_page, _total_pages = _resolve_session_panel_page(len(available_threads), page)

    if context_user_data is not None:
        context_user_data[SESSION_PICKER_THREADS_KEY] = {
            "thread_id": thread_id or 0,
            "window_id": wid,
            "machine_id": machine_id,
            "items": available_threads,
            "page": resolved_page,
        }

    text = _build_session_panel_text(
        display=display,
        current_thread_id=current_thread_id,
        current_turn_id=current_turn_id,
        available_threads=available_threads,
        page=resolved_page,
        list_error=list_error,
    )
    keyboard = _build_session_panel_keyboard(
        current_thread_id=current_thread_id,
        available_threads=available_threads,
        page=resolved_page,
    )
    return True, text, keyboard


def _extract_lifecycle_thread_id(
    payload: dict[str, object] | None,
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


def _extract_lifecycle_turn_id(payload: dict[str, object] | None) -> str:
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


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from .handlers import commands as command_handlers
    await command_handlers.resume_command(update, context)


def _clear_directory_session_picker_state(user_data: dict | None) -> None:
    """Clear temporary folder-session picker state."""
    if user_data is not None:
        user_data.pop(DIR_SESSION_PICKER_KEY, None)


def _resolve_directory_session_picker_page(
    session_count: int,
    page: int,
) -> tuple[int, int]:
    """Return (resolved_page, total_pages) for folder-session picker pagination."""
    per_page = DIR_SESSION_PICKER_SESSIONS_PER_PAGE
    if session_count <= 0:
        return 0, 1
    total_pages = (session_count + per_page - 1) // per_page
    resolved = max(0, min(page, total_pages - 1))
    return resolved, total_pages


def _format_directory_session_timestamp(timestamp: float) -> str:
    """Render one absolute UTC timestamp for folder-session picker rows."""
    if timestamp <= 0:
        return "unknown"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(timestamp))
    except Exception:
        return "unknown"


def _build_directory_session_picker_text(
    *,
    selected_path: str,
    sessions: list[dict[str, object]],
    page: int = 0,
) -> str:
    """Build folder-change resume picker text."""
    resolved_page, total_pages = _resolve_directory_session_picker_page(
        len(sessions),
        page,
    )
    start = resolved_page * DIR_SESSION_PICKER_SESSIONS_PER_PAGE
    end = start + DIR_SESSION_PICKER_SESSIONS_PER_PAGE
    visible_sessions = sessions[start:end]
    display_path = selected_path.replace(str(Path.home()), "~")

    lines = [
        "🗂 *Past Codex sessions for this folder*",
        "",
        f"Folder: `{display_path}`",
        "",
        "Pick a previous session or start a fresh one:",
    ]
    for idx, item in enumerate(visible_sessions, start=start + 1):
        thread_id = str(item.get("thread_id", "")).strip()
        created_at = _format_directory_session_timestamp(
            float(item.get("created_at", 0.0) or 0.0)
        )
        last_active_at = _format_directory_session_timestamp(
            float(item.get("last_active_at", 0.0) or 0.0)
        )
        lines.extend(
            [
                f"{idx}. `{thread_id or '<unknown>'}`",
                f"   Created: `{created_at}`",
                f"   Last active: `{last_active_at}`",
            ]
        )
    lines.extend(
        [
            "",
            f"Page: `{resolved_page + 1}/{total_pages}`",
            "Buttons: resume one of the listed sessions, start fresh, or go back.",
        ]
    )
    return "\n".join(lines)


def _build_directory_session_picker_keyboard(
    *,
    sessions: list[dict[str, object]],
    page: int = 0,
) -> InlineKeyboardMarkup:
    """Build folder-change resume picker keyboard."""
    resolved_page, total_pages = _resolve_directory_session_picker_page(
        len(sessions),
        page,
    )
    start = resolved_page * DIR_SESSION_PICKER_SESSIONS_PER_PAGE
    end = start + DIR_SESSION_PICKER_SESSIONS_PER_PAGE
    visible_sessions = sessions[start:end]

    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("Start Fresh", callback_data=CB_DIR_SESSION_FRESH)],
        [InlineKeyboardButton("Back", callback_data=CB_DIR_SESSION_BACK)],
    ]
    if total_pages > 1:
        nav_row: list[InlineKeyboardButton] = []
        if resolved_page > 0:
            nav_row.append(
                InlineKeyboardButton(
                    "Prev",
                    callback_data=f"{CB_DIR_SESSION_PAGE}{resolved_page - 1}",
                )
            )
        if resolved_page < total_pages - 1:
            nav_row.append(
                InlineKeyboardButton(
                    "Next",
                    callback_data=f"{CB_DIR_SESSION_PAGE}{resolved_page + 1}",
                )
            )
        if nav_row:
            rows.append(nav_row)

    for idx, item in enumerate(visible_sessions, start=start):
        thread_id = str(item.get("thread_id", "")).strip()
        rows.append(
            [
                InlineKeyboardButton(
                    f"Resume {_short_thread_id(thread_id)}"[:64],
                    callback_data=f"{CB_DIR_SESSION_RESUME}{idx}"[:64],
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


def _store_directory_session_picker_state(
    context_user_data: dict | None,
    *,
    thread_id: int | None,
    chat_id: int | None,
    machine_id: str,
    machine_name: str,
    selected_path: str,
    root_path: str,
    sessions: list[dict[str, object]],
    page: int,
) -> None:
    """Persist folder-session picker context for follow-up callbacks."""
    if context_user_data is None:
        return
    resolved_page, _ = _resolve_directory_session_picker_page(len(sessions), page)
    context_user_data[DIR_SESSION_PICKER_KEY] = {
        "thread_id": thread_id or 0,
        "chat_id": chat_id if chat_id is not None else 0,
        "machine_id": machine_id,
        "machine_name": machine_name,
        "selected_path": selected_path,
        "root_path": root_path,
        "items": sessions,
        "page": resolved_page,
    }


async def _bind_selected_folder_to_topic(
    *,
    user_id: int,
    chat_id: int | None,
    pending_thread_id: int | None,
    machine_id: str,
    machine_name: str,
    selected_path: str,
    window_name: str,
    resume_thread_id: str = "",
) -> tuple[bool, str, str]:
    """Create a folder binding, optionally by resuming an existing Codex thread."""
    selected_model = ""
    selected_effort = ""
    selected_service_tier = ""
    if pending_thread_id is not None:
        selected_model, selected_effort = session_manager.get_topic_model_selection(
            user_id,
            pending_thread_id,
            chat_id=chat_id,
        )
        selected_service_tier = session_manager.get_topic_service_tier_selection(
            user_id,
            pending_thread_id,
            chat_id=chat_id,
        )
    created_wid = session_manager.allocate_virtual_window_id()
    state = session_manager.get_window_state(created_wid)
    state.cwd = selected_path
    state.window_name = window_name

    resumed_turn_id = ""
    success = True
    message = f"Created app-server session `{window_name}`"
    local_machine_id, _local_machine_name = _local_machine_identity()
    try:
        if machine_id and machine_id != local_machine_id:
            from .agent_rpc import agent_rpc_client

            if resume_thread_id:
                result = await agent_rpc_client.resume_thread(
                    machine_id,
                    window_id=created_wid,
                    cwd=selected_path,
                    thread_id=resume_thread_id,
                    window_name=window_name,
                    approval_mode=state.approval_mode.strip(),
                )
                codex_thread_id = str(result.get("thread_id", "")).strip()
                resumed_turn_id = str(result.get("turn_id", "")).strip()
                if not codex_thread_id:
                    raise CodexAppServerError("remote thread/resume returned no thread id")
                message = f"Resumed app-server session `{window_name}`"
                resumed_model = str(result.get("model_slug", "")).strip()
                resumed_effort = str(result.get("reasoning_effort", "")).strip()
            else:
                result = await agent_rpc_client.ensure_thread(
                    machine_id,
                    window_id=created_wid,
                    cwd=selected_path,
                    window_name=window_name,
                    approval_mode=state.approval_mode.strip(),
                    model_slug=selected_model,
                    reasoning_effort=selected_effort,
                    service_tier=selected_service_tier,
                )
                codex_thread_id = str(result.get("thread_id", "")).strip()
                if not codex_thread_id:
                    raise CodexAppServerError("failed to create remote app-server thread")
                resumed_model = ""
                resumed_effort = ""
        else:
            if resume_thread_id:
                result = await codex_app_server_client.thread_resume(
                    thread_id=resume_thread_id,
                )
                codex_thread_id = _extract_lifecycle_thread_id(
                    result,
                    fallback=resume_thread_id,
                )
                resumed_turn_id = _extract_lifecycle_turn_id(result)
                if not codex_thread_id:
                    raise CodexAppServerError("thread/resume returned no thread id")
                message = f"Resumed app-server session `{window_name}`"
            else:
                ensure_kwargs: dict[str, str] = {}
                if selected_model:
                    ensure_kwargs["model"] = selected_model
                if selected_effort:
                    ensure_kwargs["effort"] = selected_effort
                if selected_service_tier:
                    ensure_kwargs["service_tier"] = selected_service_tier
                codex_thread_id, _approval = await session_manager._ensure_codex_thread_for_window(
                    window_id=created_wid,
                    cwd=selected_path,
                    **ensure_kwargs,
                )
                if not codex_thread_id:
                    raise CodexAppServerError("failed to create app-server thread")
            resumed_model = ""
            resumed_effort = ""
    except Exception as e:
        logger.warning(
            "Failed to initialize app-server thread for %s (user=%d thread=%s): %s",
            selected_path,
            user_id,
            pending_thread_id,
            e,
        )
        return False, f"Failed to start app-server session: {e}", ""

    if pending_thread_id is not None:
        session_manager.bind_topic_to_codex_thread(
            user_id=user_id,
            thread_id=pending_thread_id,
            chat_id=chat_id,
            codex_thread_id=codex_thread_id,
            cwd=selected_path,
            display_name=window_name,
            window_id=created_wid,
            machine_id=machine_id or local_machine_id,
            machine_display_name=machine_name or window_name,
        )
        if resume_thread_id:
            if machine_id and machine_id != local_machine_id:
                changed = session_manager.set_topic_model_selection(
                    user_id,
                    pending_thread_id,
                    chat_id=chat_id,
                    model_slug=resumed_model,
                    reasoning_effort=resumed_effort,
                )
            else:
                changed, resumed_model, resumed_effort = (
                    session_manager.sync_window_topic_model_selection_from_codex_session(
                        window_id=created_wid,
                        codex_thread_id=codex_thread_id,
                        cwd=selected_path,
                    )
                )
            if changed:
                message = (
                    f"{message}\n{_format_model_inherited_notice(resumed_model, resumed_effort)}"
                )
    else:
        session_manager.set_window_codex_thread_id(created_wid, codex_thread_id)

    if resumed_turn_id:
        session_manager.set_window_codex_active_turn_id(created_wid, resumed_turn_id)
    return True, message, created_wid


_STATUS_LABEL_WIDTH = 10
_STATUS_VALUE_WIDTH = 18


def _wrap_status_value(value: str, *, width: int = _STATUS_VALUE_WIDTH) -> list[str]:
    """Wrap a status value for narrow Telegram cards, preferring delimiter breaks."""
    text = value.strip() or "-"
    chunks: list[str] = []
    remaining = text
    delimiters = "-_/ "
    while len(remaining) > width:
        split_at = width
        for idx in range(min(width, len(remaining) - 1), 0, -1):
            if remaining[idx] in delimiters:
                split_at = idx
                break
        if split_at <= 0:
            split_at = width
        chunk = remaining[:split_at].rstrip()
        if not chunk:
            chunk = remaining[:width]
            split_at = width
        chunks.append(chunk)
        remaining = remaining[split_at:].lstrip() if remaining[split_at:split_at + 1] == " " else remaining[split_at:]
    chunks.append(remaining)
    return chunks


def _format_status_block(label: str, value: str) -> list[str]:
    """Render one status field as a wrapped monospace block."""
    chunks = _wrap_status_value(value)
    lines = [f"{label:<{_STATUS_LABEL_WIDTH}}{chunks[0]}"]
    indent = " " * _STATUS_LABEL_WIDTH
    lines.extend(f"{indent}{chunk}" for chunk in chunks[1:])
    return lines


def _format_status_field(label: str, value: str) -> str:
    """Backward-compatible single-line status field helper."""
    return _format_status_block(label, value)[0]


def _format_reset_time(reset_raw: object) -> str:
    """Render a short absolute UTC reset timestamp."""
    if isinstance(reset_raw, int) and reset_raw > 0:
        try:
            return time.strftime("%b %d %H:%M UTC", time.gmtime(reset_raw))
        except Exception:
            return str(reset_raw)
    return "unknown"


def _format_last_seen(ts_raw: object) -> str:
    """Render a short UTC timestamp for node last-seen state."""
    if isinstance(ts_raw, (int, float)) and float(ts_raw) > 0:
        try:
            return time.strftime("%b %d %H:%M UTC", time.gmtime(float(ts_raw)))
        except Exception:
            return str(ts_raw)
    return "unknown"


def _compact_count(value: int) -> str:
    """Render a token count in a compact mobile-friendly form."""
    magnitude = abs(value)
    for threshold, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if magnitude >= threshold:
            compact = value / threshold
            return f"{compact:.1f}{suffix}"
    return str(value)


def _format_token_usage_lines(total: dict[str, object]) -> list[str]:
    """Render compact token usage lines."""
    specs = [
        ("tokens", "total", total.get("totalTokens")),
        ("", "in", total.get("inputTokens")),
        ("", "out", total.get("outputTokens")),
        ("", "reason", total.get("reasoningOutputTokens")),
    ]
    lines: list[str] = []
    for label, short_label, raw in specs:
        if not isinstance(raw, int):
            continue
        value = f"{short_label} {_compact_count(raw)}"
        lines.extend(_format_status_block(label, value))
    return lines


def _render_rate_limit_bar(remaining_percent: int, *, width: int = 20) -> str:
    """Render an ASCII bar that depletes from right to left."""
    clamped = max(0, min(100, remaining_percent))
    filled = max(0, min(width, round((clamped / 100) * width)))
    return "[" + ("=" * filled) + ("." * (width - filled)) + "]"


def _format_rate_limit_window(label: str, window: dict[str, object] | None) -> list[str]:
    """Render one rate-limit window line group."""
    if not isinstance(window, dict):
        return []

    used = window.get("usedPercent")
    reset_raw = window.get("resetsAt")
    duration_raw = window.get("windowDurationMins")
    reset_s = _format_reset_time(reset_raw)
    duration_s = str(duration_raw) if isinstance(duration_raw, int) and duration_raw > 0 else "?"
    short_label = label.replace(" limit", "").strip()
    if isinstance(used, int):
        remaining = max(0, 100 - used)
        meter = _render_rate_limit_bar(remaining)
        return [
            f"{short_label:<{_STATUS_LABEL_WIDTH}}{remaining}% left",
            " " * _STATUS_LABEL_WIDTH + meter,
            " " * _STATUS_LABEL_WIDTH + f"used {used}% | win {duration_s}m",
            " " * _STATUS_LABEL_WIDTH + f"reset {reset_s}",
        ]

    return [
        f"{short_label:<{_STATUS_LABEL_WIDTH}}?% left",
        " " * _STATUS_LABEL_WIDTH + "[....................]",
        " " * _STATUS_LABEL_WIDTH + f"used ? | win {duration_s}m",
        " " * _STATUS_LABEL_WIDTH + f"reset {reset_s}",
    ]


async def _show_app_server_status(
    update: Update,
    *,
    allow_tui_fallback: bool = False,
) -> bool:
    """Show Codex status using app-server APIs (native-first).

    Returns:
        True when a native status message was sent.
        False when native data was insufficient and caller should use TUI fallback.
    """
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return True
    if not update.message:
        return True

    thread_id = _get_thread_id(update)
    chat_id = _group_chat_id(update.effective_chat)
    wid = session_manager.resolve_window_for_thread(
        user.id,
        thread_id,
        chat_id=chat_id,
    )
    if not wid:
        await safe_reply(update.message, "No session bound to this topic.")
        return True

    display = session_manager.get_display_name(wid)
    codex_thread_id = session_manager.get_window_codex_thread_id(wid)
    active_turn = session_manager.get_window_codex_active_turn_id(wid)
    machine_id = session_manager.get_window_machine_id(wid)
    node = node_registry.get_node(machine_id) if machine_id else None
    if codex_thread_id and not active_turn:
        active_turn = codex_app_server_client.get_active_turn_id(codex_thread_id) or ""

    rate_payload: dict[str, object] = {}
    rate_error = ""
    turn_active = bool(active_turn)
    if not turn_active:
        try:
            result = await codex_app_server_client.read_rate_limits()
            if isinstance(result, dict):
                rate_payload = result
        except Exception as e:
            rate_error = str(e)

    snapshot = rate_payload.get("rateLimits")
    if not isinstance(snapshot, dict):
        snapshot = codex_app_server_client.get_rate_limits_snapshot() or {}

    usage_lines_added = 0
    lines = [
        "Codex status",
        "============",
        *_format_status_block("transport", "app-server"),
        *_format_status_block("session", display),
        *_format_status_block(
            "machine",
            (
                node.display_name
                if node is not None and node.display_name
                else (machine_id or config.machine_name)
            ),
        ),
        *_format_status_block(
            "node",
            (
                f"{node.status} | {node.transport}"
                if node is not None
                else "online | local"
            ),
        ),
    ]
    if node is not None and not node.is_local:
        lines.extend(_format_status_block("seen", _format_last_seen(node.last_seen_ts)))
    lines.extend(
        [
            *_format_status_block("thread", codex_thread_id or "<uninitialized>"),
            *_format_status_block("active", active_turn or "idle"),
            "",
        ]
    )

    plan = snapshot.get("planType") if isinstance(snapshot, dict) else None
    limit_name = snapshot.get("limitName") if isinstance(snapshot, dict) else None
    if isinstance(plan, str) and plan:
        lines.extend(_format_status_block("plan", plan))
        usage_lines_added += 1
    if isinstance(limit_name, str) and limit_name:
        lines.extend(_format_status_block("bucket", limit_name))
        usage_lines_added += 1
    primary_lines = _format_rate_limit_window(
        "Primary limit",
        snapshot.get("primary") if isinstance(snapshot, dict) else None,
    )
    secondary_lines = _format_rate_limit_window(
        "Secondary limit",
        snapshot.get("secondary") if isinstance(snapshot, dict) else None,
    )
    lines.extend(primary_lines)
    lines.extend(secondary_lines)
    usage_lines_added += len(primary_lines) + len(secondary_lines)

    credits = snapshot.get("credits") if isinstance(snapshot, dict) else None
    if isinstance(credits, dict):
        has_credits = credits.get("hasCredits")
        unlimited = credits.get("unlimited")
        balance = credits.get("balance")
        credit_lines: list[str] = []
        if isinstance(has_credits, bool):
            credit_lines.extend(_format_status_block("credits", "yes" if has_credits else "no"))
        if isinstance(unlimited, bool):
            credit_lines.extend(_format_status_block("unlimited", "yes" if unlimited else "no"))
        if isinstance(balance, str) and balance:
            credit_lines.extend(_format_status_block("balance", balance))
        if credit_lines:
            lines.append("")
            lines.extend(credit_lines)
            usage_lines_added += len(credit_lines)

    if codex_thread_id:
        token_usage = codex_app_server_client.get_thread_token_usage(codex_thread_id)
        if isinstance(token_usage, dict):
            total = token_usage.get("total")
            if isinstance(total, dict):
                token_lines = _format_token_usage_lines(total)
                if token_lines:
                    lines.append("")
                    lines.extend(token_lines)
                    usage_lines_added += len(token_lines)

    native_usage_available = usage_lines_added > 0
    if allow_tui_fallback and not native_usage_available:
        logger.info(
            "Native status unavailable for window %s (thread=%s); falling back to TUI parsing",
            wid,
            thread_id,
        )
        return False

    if rate_error and native_usage_available:
        lines.append("")
        lines.append(f"Rate-limit read error: {rate_error}")

    text = "\n".join(lines).strip() or "No status available."
    await safe_reply(update.message, f"```\n{text}\n```")
    return True


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from .handlers import commands as command_handlers
    await command_handlers.status_command(update, context)


def _ordered_reasoning_levels(levels: list[str]) -> list[str]:
    """Sort reasoning levels by known effort order with unknowns appended."""
    known_order = ["minimal", "low", "medium", "high", "xhigh"]
    deduped: list[str] = []
    for level in levels:
        if level not in deduped:
            deduped.append(level)
    return [lvl for lvl in known_order if lvl in deduped] + [
        lvl for lvl in deduped if lvl not in known_order
    ]


_MODEL_GREETING_ENDINGS = [
    "the chosen model stared at the task and approved itself.",
    "everyone nodded, including the logs.",
    "the keyboard remains emotionally neutral.",
    "the config file felt seen for 0.3 seconds.",
    "the reasoning slider is pretending to be a personality test.",
    "the previous model left a polite out-of-office message.",
    "this is exactly as dramatic as a spreadsheet update.",
    "the terminal applauded in complete silence.",
    "your choice has been forwarded to the Department of Dry Humor.",
    "no confetti was deployed, per policy.",
]

MODEL_GREETING_MESSAGES = list(_MODEL_GREETING_ENDINGS)


def _pick_model_greeting() -> str:
    """Pick one dry/silly greeting for the model selector message."""
    return random.choice(MODEL_GREETING_MESSAGES)


def _load_codex_default_service_tier() -> str:
    """Load the default service tier from local Codex config."""
    config_path = Path.home() / ".codex" / "config.toml"
    if config_path.is_file():
        try:
            with config_path.open("rb") as f:
                cfg = tomllib.load(f)
            raw_service_tier = cfg.get("service_tier", "")
            if isinstance(raw_service_tier, str):
                normalized = raw_service_tier.strip().lower()
                if normalized in {"fast", "flex"}:
                    return normalized
        except Exception as e:
            logger.debug("Failed reading Codex config %s: %s", config_path, e)
    return "flex"


def _load_codex_model_catalog() -> dict[str, object]:
    """Load current model config + model catalog from local Codex files."""
    config_path = Path.home() / ".codex" / "config.toml"
    cache_path = Path.home() / ".codex" / "models_cache.json"

    current_model = "unknown"
    current_effort = "unknown"
    if config_path.is_file():
        try:
            with config_path.open("rb") as f:
                cfg = tomllib.load(f)
            current_model = str(cfg.get("model", current_model))
            current_effort = str(cfg.get("model_reasoning_effort", current_effort))
        except Exception as e:
            logger.debug("Failed reading Codex config %s: %s", config_path, e)

    catalog: dict[str, object] = {
        "current_model": current_model,
        "current_effort": current_effort,
        "models": [],
        "reasoning_options": [],
        "fetched_at": None,
        "client_version": None,
        "cache_error": None,
    }
    if not cache_path.is_file():
        catalog["cache_error"] = "Model cache not found at `~/.codex/models_cache.json`."
        return catalog

    try:
        with cache_path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as e:
        logger.debug("Failed reading model cache %s: %s", cache_path, e)
        catalog["cache_error"] = "Failed to read `~/.codex/models_cache.json`."
        return catalog

    fetched_at = str(payload.get("fetched_at")) if payload.get("fetched_at") else None
    client_version = (
        str(payload.get("client_version")) if payload.get("client_version") else None
    )
    models_raw = payload.get("models") or []
    if not isinstance(models_raw, list):
        models_raw = []

    visible_models: list[dict] = [
        m for m in models_raw if isinstance(m, dict) and m.get("visibility") == "list"
    ]
    if not visible_models:
        visible_models = [m for m in models_raw if isinstance(m, dict)]

    visible_models.sort(
        key=lambda m: (
            int(m.get("priority")) if isinstance(m.get("priority"), int) else 9999,
            str(m.get("slug", "")),
        )
    )

    reasoning_values: list[str] = []
    normalized_models: list[dict[str, object]] = []
    for model in visible_models:
        slug = str(model.get("slug", "unknown"))
        default_effort = str(model.get("default_reasoning_level", "unknown"))
        supported = model.get("supported_reasoning_levels") or []
        supported_efforts: list[str] = []
        if isinstance(supported, list):
            for item in supported:
                if isinstance(item, dict) and "effort" in item:
                    effort = str(item["effort"])
                    supported_efforts.append(effort)
                    if effort not in reasoning_values:
                        reasoning_values.append(effort)
        normalized_models.append(
            {
                "slug": slug,
                "default_effort": default_effort,
                "levels": _ordered_reasoning_levels(supported_efforts),
            }
        )

    catalog["models"] = normalized_models
    catalog["reasoning_options"] = _ordered_reasoning_levels(reasoning_values)
    catalog["fetched_at"] = fetched_at
    catalog["client_version"] = client_version
    return catalog


def _get_model_entry(catalog: dict[str, object], slug: str) -> dict[str, object] | None:
    """Return a model entry from catalog by slug."""
    models = catalog.get("models")
    if not isinstance(models, list):
        return None
    for model in models:
        if isinstance(model, dict) and str(model.get("slug", "")) == slug:
            return model
    return None


def _build_model_info_text(catalog: dict[str, object] | None = None) -> str:
    """Build /model response from local Codex config + model cache."""
    catalog = catalog or _load_codex_model_catalog()
    current_model = str(catalog.get("current_model", "unknown"))
    current_effort = str(catalog.get("current_effort", "unknown"))
    cache_error = catalog.get("cache_error")

    lines = [
        f"🤖 {_pick_model_greeting()}",
        "",
        f"Topic model: `{current_model}`",
        f"Topic reasoning: `{current_effort}`",
        "",
        "Stored per topic. Fresh threads use this. Resumed threads inherit their own model.",
    ]

    if isinstance(cache_error, str) and cache_error:
        lines.append("")
        lines.append(cache_error)
        return "\n".join(lines)

    models = catalog.get("models")
    if not isinstance(models, list):
        models = []
    if not models:
        lines.append("")
        lines.append("No selectable model options were found in local cache.")

    return "\n".join(lines)


def _catalog_with_topic_selection(
    catalog: dict[str, object],
    *,
    current_model: str,
    current_effort: str,
) -> dict[str, object]:
    """Return a catalog copy with topic-scoped current selection applied."""
    updated = dict(catalog)
    updated["current_model"] = current_model
    updated["current_effort"] = current_effort
    return updated


def _resolve_topic_model_catalog(
    *,
    user_id: int,
    thread_id: int | None,
    chat_id: int | None = None,
) -> dict[str, object]:
    """Load the shared model catalog and overlay the current topic selection."""
    catalog = _load_codex_model_catalog()
    selected_model, selected_effort = session_manager.get_topic_model_selection(
        user_id,
        thread_id,
        chat_id=chat_id,
    )
    return _catalog_with_topic_selection(
        catalog,
        current_model=selected_model or str(catalog.get("current_model", "unknown")),
        current_effort=selected_effort or str(catalog.get("current_effort", "unknown")),
    )


def _format_model_inherited_notice(model_slug: str, reasoning_effort: str) -> str:
    """Format a short visible note after resuming a session with different model settings."""
    parts = [f"`{value}`" for value in [model_slug.strip(), reasoning_effort.strip()] if value.strip()]
    summary = " / ".join(parts) if parts else "`unknown`"
    return f"Model inherited from resumed session: {summary}"


def _build_model_keyboard(catalog: dict[str, object]) -> InlineKeyboardMarkup | None:
    """Build inline keyboard for selecting model + reasoning."""
    models = catalog.get("models")
    if not isinstance(models, list) or not models:
        return None

    current_model = str(catalog.get("current_model", ""))
    current_effort = str(catalog.get("current_effort", ""))
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for model in models:
        if not isinstance(model, dict):
            continue
        slug = str(model.get("slug", "unknown"))
        label = f"✅ {slug}" if slug == current_model else slug
        row.append(
            InlineKeyboardButton(label, callback_data=f"{CB_MODEL_SET}{slug}"[:64])
        )
        if len(row) >= 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    selected = _get_model_entry(catalog, current_model)
    levels: list[str] = []
    if selected and isinstance(selected.get("levels"), list):
        levels = [str(item) for item in selected["levels"]]
    if not levels:
        raw_levels = catalog.get("reasoning_options")
        if isinstance(raw_levels, list):
            levels = [str(item) for item in raw_levels]

    if levels:
        level_row: list[InlineKeyboardButton] = []
        for effort in levels:
            label = f"✅ {effort}" if effort == current_effort else effort
            level_row.append(
                InlineKeyboardButton(
                    label,
                    callback_data=f"{CB_MODEL_EFFORT_SET}{effort}"[:64],
                )
            )
        rows.append(level_row)

    rows.append([InlineKeyboardButton("Refresh", callback_data=CB_MODEL_REFRESH)])
    return InlineKeyboardMarkup(rows)


def _toml_string(value: str) -> str:
    """Return TOML-safe quoted string."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _set_codex_config_value(key: str, value: str) -> tuple[bool, str]:
    """Set top-level key in ~/.codex/config.toml."""
    if key not in {"model", "model_reasoning_effort"}:
        return False, f"Unsupported key: {key}"

    config_path = Path.home() / ".codex" / "config.toml"
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        content = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    except OSError as e:
        return False, f"Failed to read config: {e}"

    replacement = f"{key} = {_toml_string(value)}"
    lines = content.splitlines()
    updated = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        left, sep, _right = line.partition("=")
        if sep and left.strip() == key:
            indent = line[: len(line) - len(line.lstrip())]
            lines[i] = f"{indent}{replacement}"
            updated = True
            break
    if not updated:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(replacement)

    new_content = "\n".join(lines)
    if lines:
        new_content += "\n"
    try:
        config_path.write_text(new_content, encoding="utf-8")
    except OSError as e:
        return False, f"Failed to write config: {e}"
    return True, ""


def _ensure_codex_project_trust(
    project_path: Path,
    *,
    trust_level: str = "trusted",
) -> tuple[bool, str]:
    """Ensure Codex marks project_path as trusted in ~/.codex/config.toml.

    This is required for Codex tool execution (file writes, git, outbound
    network) in app-server mode. Without it, Codex may sandbox operations and
    surface confusing "Permission denied" / DNS failures.
    """
    config_path = Path.home() / ".codex" / "config.toml"
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        content = (
            config_path.read_text(encoding="utf-8")
            if config_path.exists()
            else ""
        )
    except OSError as e:
        return False, f"Failed to read config: {e}"

    project_key = str(project_path)
    section_header = f"[projects.{_toml_string(project_key)}]"
    desired_line = f"trust_level = {_toml_string(trust_level)}"

    lines = content.splitlines()

    # Find the target table and its boundaries.
    start = None
    end = len(lines)
    for i, line in enumerate(lines):
        if line.strip() == section_header:
            start = i
            for j in range(i + 1, len(lines)):
                maybe_header = lines[j].strip()
                if maybe_header.startswith("[") and maybe_header.endswith("]"):
                    end = j
                    break
            break

    if start is None:
        # Append a new table at the end.
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(section_header)
        lines.append(desired_line)
    else:
        # Update or insert trust_level within the existing table.
        updated = False
        for k in range(start + 1, end):
            stripped = lines[k].strip()
            if not stripped or stripped.startswith("#"):
                continue
            left, sep, _right = lines[k].partition("=")
            if sep and left.strip() == "trust_level":
                indent = lines[k][: len(lines[k]) - len(lines[k].lstrip())]
                lines[k] = f"{indent}{desired_line}"
                updated = True
                break
        if not updated:
            lines.insert(start + 1, desired_line)

    new_content = "\n".join(lines)
    if lines:
        new_content += "\n"
    try:
        config_path.write_text(new_content, encoding="utf-8")
    except OSError as e:
        return False, f"Failed to write config: {e}"
    return True, ""


def _ensure_codex_trust_for_runtime() -> None:
    """Ensure Codex trust covers the bot's browse roots."""
    candidates: list[Path] = [config.browse_root]
    for root in config.group_browse_roots.values():
        candidates.append(root)
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        ok, err = _ensure_codex_project_trust(path)
        if not ok:
            logger.warning("Failed ensuring Codex trust for %s: %s", path, err)


async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from .handlers import commands as command_handlers
    await command_handlers.model_command(update, context)


async def fast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from .handlers import commands as command_handlers
    await command_handlers.fast_command(update, context)


async def transcription_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from .handlers import commands as command_handlers
    await command_handlers.transcription_command(update, context)


async def update_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from .handlers import commands as command_handlers
    await command_handlers.update_command(update, context)


async def topic_closed_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle topic closure and clean up associated runtime state."""
    chat = update.effective_chat
    if not _is_chat_allowed(chat):
        return
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return

    thread_id = _get_thread_id(update)
    chat_id = _group_chat_id(chat)
    if thread_id is None:
        return

    wid = session_manager.get_window_for_thread(
        user.id,
        thread_id,
        chat_id=chat_id,
    )
    if wid:
        display = session_manager.get_display_name(wid)
        logger.info(
            "Topic closed: binding %s cleaned up (user=%d, thread=%d)",
            display,
            user.id,
            thread_id,
        )
        session_manager.unbind_thread(user.id, thread_id, chat_id=chat_id)
        # Clean up all memory state for this topic
        await clear_topic_state(user.id, thread_id, context.bot, context.user_data)
    else:
        logger.debug(
            "Topic closed: no binding (user=%d, thread=%d)", user.id, thread_id
        )


async def forward_command_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Forward any non-bot command to the active assistant session."""
    chat = update.effective_chat
    if not _is_chat_allowed(chat):
        if update.message:
            await safe_reply(update.message, "❌ This group is not allowed to use this bot.")
        return

    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    chat_id = _group_chat_id(chat)

    # Capture group chat_id for supergroup forum topic routing.
    # Required: Telegram Bot API needs group chat_id (not user_id) to send
    # messages with message_thread_id. Do NOT remove — see session.py docs.
    if chat_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat_id)

    cmd_text = update.message.text or ""
    # The full text is already a slash command like "/clear" or "/compact foo"
    cc_slash = cmd_text.split("@")[0]  # strip bot mention
    cmd_name = cc_slash.split(maxsplit=1)[0].lower()

    if cmd_name == "/memory":
        await safe_reply(
            update.message,
            "❌ `/memory` is not supported in Codex mode.\nUse `/model` to see available model/reasoning options.",
        )
        return

    wid = session_manager.resolve_window_for_thread(
        user.id,
        thread_id,
        chat_id=chat_id,
    )
    if not wid:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    binding = session_manager.resolve_topic_binding(
        user.id,
        thread_id,
        chat_id=chat_id,
    )
    if binding is None or (not binding.codex_thread_id and not binding.cwd):
        await safe_reply(
            update.message,
            "❌ Session binding is incomplete. Send a normal message to reinitialize.",
        )
        return

    display = session_manager.get_display_name(wid)
    logger.info(
        "Forwarding command %s to window %s (user=%d)", cc_slash, display, user.id
    )
    await update.message.chat.send_action(ChatAction.TYPING)
    success, message = await session_manager.send_to_window(wid, cc_slash)
    if success:
        note_run_started(
            user_id=user.id,
            thread_id=thread_id,
            window_id=wid,
            source=f"slash:{cmd_name}",
            expect_response=False,
        )
        await safe_reply(update.message, f"⚡ [{display}] Sent: {cc_slash}")
        # If /clear command was sent, clear the session association
        # so we can detect the new session after first message
        if cc_slash.strip().lower() == "/clear":
            logger.info("Clearing session for window %s after /clear", display)
            session_manager.clear_window_session(wid)
    else:
        await safe_reply(update.message, f"❌ {message}")


async def unsupported_content_handler(
    update: Update,
    _context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Reply to unsupported non-text messages."""
    if not update.message:
        return
    if not _is_chat_allowed(update.effective_chat):
        await safe_reply(update.message, "❌ This group is not allowed to use this bot.")
        return
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    logger.debug("Unsupported content from user %d", user.id)
    await safe_reply(
        update.message,
        "⚠ This media type is not supported yet. Send text, photos, voice notes, or audio files.",
    )


# --- Image directory for incoming photos ---
_IMAGES_DIR = coco_dir() / "images"
_IMAGES_DIR.mkdir(parents=True, exist_ok=True)

# --- Audio directory for incoming voice/audio media ---
_AUDIO_DIR = coco_dir() / "audio"
_AUDIO_DIR.mkdir(parents=True, exist_ok=True)

# Per-topic lock for Codex image resume submissions: (user_id, thread_id) -> lock
_photo_resume_locks: dict[tuple[int, int], asyncio.Lock] = {}


def _pick_image_prompt(caption: str | None) -> str:
    """Build prompt text for an attached image."""
    text = (caption or "").strip()
    if text:
        return text
    return "Please analyze this image."


def _pick_audio_suffix(media: object) -> str:
    """Infer a useful local file extension for Telegram audio media."""
    file_name = str(getattr(media, "file_name", "") or "").strip()
    if file_name:
        suffix = Path(file_name).suffix.strip().lower()
        if suffix:
            return suffix

    mime_type = str(getattr(media, "mime_type", "") or "").strip().lower()
    mime_map = {
        "audio/flac": ".flac",
        "audio/mp3": ".mp3",
        "audio/mp4": ".m4a",
        "audio/mpeg": ".mp3",
        "audio/ogg": ".ogg",
        "audio/opus": ".opus",
        "audio/wav": ".wav",
        "audio/webm": ".webm",
    }
    return mime_map.get(mime_type, ".ogg")


def _build_audio_prompt(*, transcript: str, caption: str | None) -> str:
    """Combine optional Telegram caption text with the transcript."""
    caption_text = (caption or "").strip()
    transcript_text = transcript.strip()
    if caption_text:
        return f"{caption_text}\n\n{transcript_text}"
    return transcript_text


def _resolve_codex_exec_binary() -> str | None:
    """Resolve executable used for Codex CLI resume bridge."""
    try:
        parts = shlex.split(config.assistant_command)
    except ValueError:
        parts = []

    candidate = parts[0] if parts else "codex"
    if os.path.isabs(candidate):
        return candidate if Path(candidate).is_file() else None

    resolved = shutil.which(candidate)
    if resolved:
        return resolved

    if candidate != "codex":
        return shutil.which("codex")
    return None


def _build_codex_image_resume_cmd(
    codex_binary: str,
    session_id: str,
    image_path: Path,
    prompt: str,
) -> list[str]:
    """Build `codex exec resume` command for image + prompt."""
    return [
        codex_binary,
        "exec",
        "resume",
        session_id,
        "--skip-git-repo-check",
        "-i",
        str(image_path),
        prompt,
    ]


def _tail_command_output(data: bytes, limit: int = 700) -> str:
    """Decode command output and keep only the tail for compact errors."""
    text = data.decode("utf-8", errors="replace").strip()
    if len(text) <= limit:
        return text
    return "… " + text[-(limit - 2) :]


def _get_photo_resume_lock(user_id: int, thread_id: int) -> asyncio.Lock:
    """Get a per-topic lock for Codex image resume submissions."""
    key = (user_id, thread_id)
    lock = _photo_resume_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _photo_resume_locks[key] = lock
    return lock


async def _submit_image_to_codex_session(
    *,
    user_id: int,
    thread_id: int,
    window_id: str,
    image_path: Path,
    prompt: str,
) -> tuple[bool, str]:
    """Submit an image prompt into the existing Codex session."""
    if config.session_provider != "codex":
        return False, "Image bridge is only available in Codex provider mode."

    # Try app-server image input first whenever app-server transport is preferred.
    if _codex_app_server_preferred():
        steer = bool(session_manager.get_window_codex_active_turn_id(window_id))
        if not steer:
            codex_thread_id = session_manager.get_window_codex_thread_id(window_id)
            if codex_thread_id and codex_app_server_client.is_turn_in_progress(codex_thread_id):
                steer = True
        inputs = [
            {"type": "localImage", "path": str(image_path)},
            {"type": "text", "text": prompt},
        ]
        ok, err = await session_manager.send_inputs_to_window(
            window_id,
            inputs,
            steer=steer,
        )
        if ok:
            session_manager.mark_topic_telegram_live(
                user_id=user_id,
                thread_id=thread_id,
                chat_id=None,
                window_id=window_id,
            )
            return True, ""
        logger.warning(
            "App-server image send failed; falling back to CLI resume bridge (user=%d thread=%d window=%s): %s",
            user_id,
            thread_id,
            window_id,
            err,
        )

    codex_binary = _resolve_codex_exec_binary()
    if not codex_binary:
        return False, "Codex CLI executable not found in PATH."

    session = await session_manager.resolve_session_for_window(window_id)
    if not session or not session.session_id:
        return False, "No active Codex session found for this topic yet."

    window_state = session_manager.get_window_state(window_id)
    workdir = window_state.cwd or str(Path.cwd())
    cmd = _build_codex_image_resume_cmd(
        codex_binary,
        session.session_id,
        image_path,
        prompt,
    )

    logger.info(
        "Submitting image via Codex resume (user=%d thread=%d window=%s session=%s image=%s)",
        user_id,
        thread_id,
        window_id,
        session.session_id,
        image_path,
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        return False, f"Failed to start Codex image bridge: {e}"

    stdout, stderr = await proc.communicate()
    if proc.returncode == 0:
        session_manager.register_expected_transcript_user_echo(window_id, prompt)
        session_manager.mark_topic_telegram_live(
            user_id=user_id,
            thread_id=thread_id,
            chat_id=None,
            window_id=window_id,
        )
        return True, ""

    details = _tail_command_output(stderr or stdout)
    if details:
        return False, f"Codex image bridge failed (exit {proc.returncode}): {details}"
    return False, f"Codex image bridge failed (exit {proc.returncode})."


async def _run_photo_bridge_task(
    *,
    bot: Bot,
    user_id: int,
    thread_id: int,
    chat_id: int | None,
    window_id: str,
    image_path: Path,
    prompt: str,
) -> None:
    """Run one photo bridge submission; serialize per topic."""
    lock = _get_photo_resume_lock(user_id, thread_id)
    async with lock:
        try:
            ok, err = await _submit_image_to_codex_session(
                user_id=user_id,
                thread_id=thread_id,
                window_id=window_id,
                image_path=image_path,
                prompt=prompt,
            )
            if ok:
                return

            logger.warning(
                "Codex image bridge failed, falling back to path hint (user=%d thread=%d): %s",
                user_id,
                thread_id,
                err,
            )
            fallback_text = f"{prompt}\n\n(image attached: {image_path})"
            send_ok, send_msg = await session_manager.send_topic_text_to_window(
                user_id=user_id,
                thread_id=thread_id,
                chat_id=chat_id,
                window_id=window_id,
                text=fallback_text,
            )
            if not send_ok:
                await safe_send(
                    bot,
                    session_manager.resolve_chat_id(
                        user_id,
                        thread_id,
                        chat_id=chat_id,
                    ),
                    f"❌ {err}\nFallback send failed: {send_msg}",
                    message_thread_id=thread_id,
                )
            else:
                note_run_started(
                    user_id=user_id,
                    thread_id=thread_id,
                    window_id=window_id,
                    source="photo_fallback",
                    pending_text=fallback_text,
                    expect_response=True,
                )
        except Exception as e:
            logger.exception(
                "Unexpected photo bridge error (user=%d thread=%d): %s",
                user_id,
                thread_id,
                e,
            )
            await safe_send(
                bot,
                session_manager.resolve_chat_id(
                    user_id,
                    thread_id,
                    chat_id=chat_id,
                ),
                f"❌ Image bridge failed unexpectedly: {e}",
                message_thread_id=thread_id,
            )


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photos sent by the user: download and forward path to assistant."""
    chat = update.effective_chat
    if not _is_chat_allowed(chat):
        if update.message:
            await safe_reply(update.message, "❌ This group is not allowed to use this bot.")
        return

    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return

    if not update.message or not update.message.photo:
        return

    thread_id = _get_thread_id(update)
    chat_id = _group_chat_id(chat)
    if chat_id is not None and thread_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat_id)

    # Must be in a named topic
    if thread_id is None:
        await safe_reply(
            update.message,
            "❌ Please use a named topic. Create a new topic to start a session.",
        )
        return

    wid = session_manager.get_window_for_thread(
        user.id,
        thread_id,
        chat_id=chat_id,
    )
    if wid is None:
        await safe_reply(
            update.message,
            "❌ No session bound to this topic. Send a text message first to create one.",
        )
        return

    binding = session_manager.resolve_topic_binding(
        user.id,
        thread_id,
        chat_id=chat_id,
    )
    if binding is None or (not binding.codex_thread_id and not binding.cwd):
        await safe_reply(
            update.message,
            "❌ Session binding is incomplete. Send a normal message to reinitialize.",
        )
        return

    # Download the highest-resolution photo
    photo = update.message.photo[-1]
    tg_file = await photo.get_file()

    # Save to the active CoCo runtime dir under images/<timestamp>_<file_unique_id>.jpg
    filename = f"{int(time.time())}_{photo.file_unique_id}.jpg"
    file_path = _IMAGES_DIR / filename
    await tg_file.download_to_drive(file_path)

    prompt = _pick_image_prompt(update.message.caption)

    await update.message.chat.send_action(ChatAction.TYPING)
    clear_status_msg_info(user.id, thread_id)

    if config.session_provider == "codex":
        asyncio.create_task(
            _run_photo_bridge_task(
                bot=context.bot,
                user_id=user.id,
                thread_id=thread_id,
                chat_id=chat_id,
                window_id=wid,
                image_path=file_path,
                prompt=prompt,
            )
        )
        await _set_eyes_reaction(update.message)
        return

    # Non-Codex providers: keep existing path-hint behavior.
    text_to_send = f"{prompt}\n\n(image attached: {file_path})"
    success, message = await session_manager.send_topic_text_to_window(
        user_id=user.id,
        thread_id=thread_id,
        chat_id=chat_id,
        window_id=wid,
        text=text_to_send,
    )
    if not success:
        await safe_reply(update.message, f"❌ {message}")
        return
    note_run_started(
        user_id=user.id,
        thread_id=thread_id,
        window_id=wid,
        source="photo_direct",
        pending_text=text_to_send,
        expect_response=True,
    )
    await _set_eyes_reaction(update.message)


async def _forward_topic_text_message(
    *,
    message,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    thread_id: int | None,
    chat_id: int | None,
    text: str,
) -> None:
    """Forward one text prompt through the normal topic/session path."""
    if thread_id is None:
        await safe_reply(
            message,
            "❌ Please use a named topic. Create a new topic to start a session.",
        )
        return

    wid = session_manager.get_window_for_thread(
        user_id,
        thread_id,
        chat_id=chat_id,
    )
    if wid is None:
        if not _can_user_create_sessions(user_id):
            await safe_reply(
                message,
                "❌ You only have single-session access in this bot.\n"
                "Ask an admin to add you to an existing session/topic.",
            )
            return

        logger.info(
            "Unbound topic: directory browser (user=%d, thread=%d)",
            user_id,
            thread_id,
        )
        machine_choices = _sorted_machine_choices()
        if len(machine_choices) > 1:
            msg_text, keyboard = await _open_machine_picker(
                context_user_data=context.user_data,
                thread_id=thread_id,
                chat_id=chat_id,
            )
            if context.user_data is not None:
                context.user_data["_pending_thread_text"] = text
        else:
            local_machine_id, local_machine_name = _local_machine_identity()
            if context.user_data is not None:
                context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
                context.user_data[BROWSE_MACHINE_KEY] = local_machine_id
                context.user_data[BROWSE_MACHINE_NAME_KEY] = local_machine_name
                context.user_data["_pending_thread_id"] = thread_id
                context.user_data["_pending_thread_text"] = text
            msg_text, keyboard, subdirs = await _build_directory_browser_for_context(
                context.user_data,
                chat_id=chat_id,
            )
            if context.user_data is not None:
                context.user_data[BROWSE_PAGE_KEY] = 0
                context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_reply(message, msg_text, reply_markup=keyboard)
        return

    binding = session_manager.resolve_topic_binding(
        user_id,
        thread_id,
        chat_id=chat_id,
    )
    if binding is None or (not binding.codex_thread_id and not binding.cwd):
        display = session_manager.get_display_name(wid)
        logger.info(
            "Incomplete binding for %s (user=%d, thread=%d); unbinding",
            display,
            user_id,
            thread_id,
        )
        session_manager.unbind_thread(user_id, thread_id, chat_id=chat_id)
        clear_queued_topic_inputs(user_id, thread_id)
        await clear_queued_topic_dock(context.bot, user_id, thread_id)
        await safe_reply(
            message,
            "❌ Session binding is incomplete. Send a message to start a new session.",
        )
        return

    if session_manager.get_window_mention_only(wid):
        bot_username = _resolve_bot_username(context)
        if not _text_mentions_bot_username(text, bot_username):
            logger.debug(
                "Mention-only mode: skipped non-mention message (user=%d, thread=%d, window=%s)",
                user_id,
                thread_id,
                wid,
            )
            return

    if session_manager.is_window_external_turn_active(wid):
        source_chat_id = getattr(message, "chat_id", None)
        chat = getattr(message, "chat", None)
        if source_chat_id is None and chat is not None:
            source_chat_id = getattr(chat, "id", None)
        enqueue_queued_topic_input(
            user_id,
            thread_id,
            text,
            source_chat_id,
            message.message_id,
        )
        await _set_hourglass_reaction(message)
        await sync_queued_topic_dock(
            context.bot,
            user_id,
            thread_id,
            window_id=wid,
        )
        return

    is_steer_message = await _is_window_in_progress(user_id, thread_id, wid)

    await message.chat.send_action(ChatAction.TYPING)
    if is_steer_message:
        logger.info(
            "Steer message accepted (user=%d, thread=%d, window=%s)",
            user_id,
            thread_id,
            wid,
        )
    else:
        await enqueue_status_update(context.bot, user_id, wid, None, thread_id=thread_id)
        await enqueue_progress_clear(context.bot, user_id, thread_id=thread_id)

    success, send_msg = await session_manager.send_topic_text_to_window(
        user_id=user_id,
        thread_id=thread_id,
        chat_id=chat_id,
        window_id=wid,
        text=text,
    )
    if not success:
        await safe_reply(message, f"❌ {send_msg}")
        return
    if is_steer_message:
        note_run_activity(
            user_id=user_id,
            thread_id=thread_id,
            window_id=wid,
            source="steer_input",
        )
    else:
        await enqueue_progress_start(
            context.bot,
            user_id,
            window_id=wid,
            thread_id=thread_id,
        )
        note_run_started(
            user_id=user_id,
            thread_id=thread_id,
            window_id=wid,
            source="user_input",
            pending_text=text,
            expect_response=True,
        )
    await _set_eyes_reaction(message)


async def audio_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Telegram voice/audio messages with local transcription."""
    chat = update.effective_chat
    if not _is_chat_allowed(chat):
        if update.message:
            await safe_reply(update.message, "❌ This group is not allowed to use this bot.")
        return

    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return

    message = update.effective_message
    if not message:
        return

    media = getattr(message, "voice", None) or getattr(message, "audio", None)
    if media is None:
        return

    thread_id = _get_thread_id(update)
    chat_id = _group_chat_id(chat)
    if chat_id is not None and thread_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat_id)
    selected_profile = get_default_transcription_profile()

    tg_file = await media.get_file()
    file_path = _AUDIO_DIR / f"{int(time.time())}_{media.file_unique_id}{_pick_audio_suffix(media)}"
    await tg_file.download_to_drive(file_path)

    await message.chat.send_action(ChatAction.TYPING)
    bootstrap_handle = begin_transcription_bootstrap(profile=selected_profile)
    if bootstrap_handle is not None:
        await safe_reply(
            message,
            "⏳ Downloading the local transcription model for first use. This can take a minute.",
        )

    try:
        transcript = await asyncio.to_thread(
            transcribe_audio_file,
            file_path,
            profile=selected_profile,
        )
    except TranscriptionError as exc:
        complete_transcription_bootstrap(bootstrap_handle, success=False)
        logger.warning(
            "Audio transcription failed (user=%d thread=%s): %s",
            user.id,
            thread_id,
            exc,
        )
        await safe_reply(message, f"❌ Audio transcription failed: {exc}")
        try:
            file_path.unlink(missing_ok=True)
        except OSError:
            logger.debug("Failed to delete temporary audio file %s", file_path)
        return
    except Exception as exc:
        complete_transcription_bootstrap(bootstrap_handle, success=False)
        logger.exception(
            "Unexpected audio transcription error (user=%d thread=%s): %s",
            user.id,
            thread_id,
            exc,
        )
        await safe_reply(message, f"❌ Audio transcription failed: {exc}")
        try:
            file_path.unlink(missing_ok=True)
        except OSError:
            logger.debug("Failed to delete temporary audio file %s", file_path)
        return

    try:
        file_path.unlink(missing_ok=True)
    except OSError:
        logger.debug("Failed to delete temporary audio file %s", file_path)

    await safe_reply(message, transcript)

    if complete_transcription_bootstrap(bootstrap_handle, success=True):
        await safe_reply(
            message,
            "✅ Local transcription is ready. The model finished downloading and the first transcription is complete.",
        )

    prompt = _build_audio_prompt(
        transcript=transcript,
        caption=getattr(message, "caption", None),
    )
    await _forward_topic_text_message(
        message=message,
        context=context,
        user_id=user.id,
        thread_id=thread_id,
        chat_id=chat_id,
        text=prompt,
    )

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.text:
        return

    user = update.effective_user
    thread_id = _get_thread_id(update)
    chat = update.effective_chat
    chat_id = _group_chat_id(chat)
    if not _is_chat_allowed(chat):
        await safe_reply(message, "❌ This group is not allowed to use this bot.")
        return
    user_id: int | None = user.id if (user and is_user_allowed(user.id)) else None

    # Support anonymous admin messages in supergroup topics by resolving the
    # bound thread owner when sender user is unavailable/unallowed.
    if (
        user_id is None
        and chat
        and chat.type in ("group", "supergroup")
        and thread_id is not None
    ):
        candidate_user_ids: set[int] = set()
        for (
            bound_uid,
            bound_chat_id,
            bound_tid,
            _bound_wid,
        ) in session_manager.iter_topic_window_bindings():
            if bound_tid != thread_id or not is_user_allowed(bound_uid):
                continue
            if (
                bound_chat_id == chat.id
                or session_manager.resolve_chat_id(
                    bound_uid,
                    thread_id,
                    chat_id=chat.id,
                )
                == chat.id
            ):
                candidate_user_ids.add(bound_uid)

        if len(candidate_user_ids) == 1:
            user_id = next(iter(candidate_user_ids))
            logger.info(
                "Resolved anonymous topic message to user %d (sender=%s, thread=%s, chat=%s)",
                user_id,
                user.id if user else None,
                thread_id,
                chat.id,
            )

    if user_id is None:
        await safe_reply(message, "You are not authorized to use this bot.")
        return

    logger.info(
        "Text message received (user=%d, thread=%s, chat_type=%s, text=%r)",
        user_id,
        thread_id,
        chat.type if chat else "unknown",
        (message.text or "")[:120],
    )

    # Capture group chat_id for supergroup forum topic routing.
    # Required: Telegram Bot API needs group chat_id (not user_id) to send
    # messages with message_thread_id. Do NOT remove — see session.py docs.
    if chat_id is not None:
        session_manager.set_group_chat_id(user_id, thread_id, chat_id)

    text = message.text

    # /allowed add-flow text capture (legacy state cleanup only).
    if context.user_data:
        allowed_state = context.user_data.get(STATE_KEY)
        if allowed_state in {STATE_ALLOWED_ADD_ID, STATE_ALLOWED_ADD_NAME}:
            _clear_allowed_flow_state(context.user_data)
            await safe_reply(
                message,
                "Add/remove flow was reset.\n"
                "Use `/allowed request_add ...`, `/allowed request_remove ...`, and `/allowed approve <token>`.",
            )
            return

    # /apps autoresearch panel text capture.
    if context.user_data and context.user_data.get(STATE_KEY) == STATE_APPS_AUTORESEARCH_OUTCOME:
        pending_tid = context.user_data.get(APPS_PENDING_THREAD_KEY)
        if pending_tid is None or pending_tid != thread_id:
            _clear_apps_flow_state(context.user_data)
        else:
            outcome = text.strip()
            if not outcome:
                await safe_reply(
                    message,
                    "Outcome cannot be empty. Send a short sentence describing what you want.",
                )
                return
            set_autoresearch_outcome(
                user_id=user_id,
                thread_id=thread_id,
                outcome=outcome,
            )
            context.user_data[STATE_KEY] = ""
            await safe_reply(message, "✅ Auto research outcome updated.")
            ok, panel_text, panel_keyboard, _wid = await _build_autoresearch_panel_payload_for_topic(
                user_id=user_id,
                thread_id=thread_id,
                user_data=context.user_data,
                chat_id=chat_id,
            )
            if ok:
                await safe_reply(
                    message,
                    panel_text,
                    reply_markup=panel_keyboard,
                )
            else:
                await safe_reply(message, panel_text)
            return

    # /apps looper panel text capture.
    if context.user_data:
        apps_state = context.user_data.get(STATE_KEY)
        if apps_state in {
            STATE_APPS_LOOPER_PLAN_PATH,
            STATE_APPS_LOOPER_KEYWORD,
            STATE_APPS_LOOPER_INSTRUCTIONS,
            STATE_APPS_LOOPER_INTERVAL,
            STATE_APPS_LOOPER_LIMIT,
        }:
            pending_tid = context.user_data.get(APPS_PENDING_THREAD_KEY)
            pending_wid = context.user_data.get(APPS_PENDING_WINDOW_ID_KEY)
            if pending_tid is None or pending_tid != thread_id:
                _clear_apps_flow_state(context.user_data)
            elif not isinstance(pending_wid, str) or not pending_wid:
                _clear_apps_flow_state(context.user_data)
                await safe_reply(
                    message,
                    "❌ Looper panel expired. Open `/apps` and configure Looper again.",
                )
                return
            else:
                raw_cfg = context.user_data.get(APPS_LOOPER_CONFIG_KEY)
                cfg = (
                    dict(raw_cfg)
                    if isinstance(raw_cfg, dict)
                    else {
                        "plan_path": "",
                        "keyword": "done",
                        "instructions": "",
                        "interval_seconds": LOOPER_DEFAULT_INTERVAL_SECONDS,
                        "limit_seconds": 0,
                        "candidates": [],
                    }
                )

                if apps_state == STATE_APPS_LOOPER_PLAN_PATH:
                    plan_path = text.strip()
                    if not plan_path:
                        await safe_reply(message, "Plan path cannot be empty. Send a `.md` path.")
                        return
                    if not plan_path.lower().endswith(".md"):
                        await safe_reply(message, "Plan path must point to a markdown file (`.md`).")
                        return
                    if not os.path.isabs(plan_path):
                        base_dir = (
                            _resolve_workspace_dir_for_window(
                                user_id=user_id,
                                thread_id=thread_id,
                                window_id=pending_wid,
                            )
                            or ""
                        )
                        if base_dir:
                            root = Path(base_dir).resolve()
                            candidate = (root / plan_path).resolve()
                            try:
                                plan_path = candidate.relative_to(root).as_posix()
                            except ValueError:
                                plan_path = str(candidate)
                    cfg["plan_path"] = plan_path
                    context.user_data[STATE_KEY] = ""
                    await safe_reply(message, f"✅ Looper plan file set to `{plan_path}`.")

                elif apps_state == STATE_APPS_LOOPER_KEYWORD:
                    keyword = normalize_looper_keyword(text)
                    if not keyword or " " in keyword:
                        await safe_reply(
                            message,
                            "Keyword must be a single word. Example: `done`",
                        )
                        return
                    cfg["keyword"] = keyword
                    context.user_data[STATE_KEY] = ""
                    await safe_reply(message, f"✅ Looper completion keyword set to `{keyword}`.")

                elif apps_state == STATE_APPS_LOOPER_INSTRUCTIONS:
                    instructions = text.strip()
                    if instructions in {"-", "none", "clear"}:
                        instructions = ""
                    cfg["instructions"] = instructions
                    context.user_data[STATE_KEY] = ""
                    if instructions:
                        await safe_reply(message, "✅ Looper custom instructions updated.")
                    else:
                        await safe_reply(message, "✅ Looper custom instructions cleared.")

                elif apps_state == STATE_APPS_LOOPER_INTERVAL:
                    parsed = _parse_duration_to_seconds(text, default_unit="m")
                    if parsed is None:
                        await safe_reply(
                            message,
                            "Invalid interval. Examples: `10m`, `15 minutes`, `1h`.",
                        )
                        return
                    if parsed < LOOPER_MIN_INTERVAL_SECONDS:
                        await safe_reply(
                            message,
                            (
                                "Interval is too short. "
                                f"Minimum is `{_format_duration_brief(LOOPER_MIN_INTERVAL_SECONDS)}`."
                            ),
                        )
                        return
                    if parsed > LOOPER_MAX_INTERVAL_SECONDS:
                        await safe_reply(
                            message,
                            (
                                "Interval is too long. "
                                f"Maximum is `{_format_duration_brief(LOOPER_MAX_INTERVAL_SECONDS)}`."
                            ),
                        )
                        return
                    cfg["interval_seconds"] = int(parsed)
                    context.user_data[STATE_KEY] = ""
                    await safe_reply(
                        message,
                        f"✅ Looper interval set to `{_format_duration_brief(parsed)}`.",
                    )

                elif apps_state == STATE_APPS_LOOPER_LIMIT:
                    raw_limit = text.strip().lower()
                    if raw_limit in {"none", "off", "0", "-"}:
                        cfg["limit_seconds"] = 0
                        context.user_data[STATE_KEY] = ""
                        await safe_reply(message, "✅ Looper time limit cleared.")
                    else:
                        parsed = _parse_duration_to_seconds(raw_limit, default_unit="h")
                        if parsed is None:
                            await safe_reply(
                                message,
                                "Invalid limit. Examples: `1h`, `2 hours`, `none`.",
                            )
                            return
                        cfg["limit_seconds"] = int(parsed)
                        context.user_data[STATE_KEY] = ""
                        await safe_reply(
                            message,
                            f"✅ Looper time limit set to `{_format_duration_brief(parsed)}`.",
                        )

                context.user_data[APPS_LOOPER_CONFIG_KEY] = cfg
                ok, panel_text, panel_keyboard, _wid = await _build_looper_panel_payload_for_topic(
                    user_id=user_id,
                    thread_id=thread_id,
                    user_data=context.user_data,
                    chat_id=chat_id,
                )
                if ok:
                    await safe_reply(
                        message,
                        panel_text,
                        reply_markup=panel_keyboard,
                    )
                else:
                    await safe_reply(message, panel_text)
                return

    # /worktree create-flow text capture (name only).
    if context.user_data and context.user_data.get(STATE_KEY) == STATE_WORKTREE_NEW_NAME:
        pending_tid = context.user_data.get(WORKTREE_PENDING_THREAD_KEY)
        if pending_tid is not None and pending_tid != thread_id:
            _clear_worktree_flow_state(context.user_data)
        else:
            if not _can_user_create_sessions(user_id):
                _clear_worktree_flow_state(context.user_data)
                await safe_reply(
                    message,
                    "❌ You do not have permission to create worktrees/sessions.",
                )
                return

            worktree_name = text.strip()
            if not worktree_name:
                await safe_reply(
                    message,
                    "Name cannot be empty. Send a worktree name like `auth-fix`.",
                )
                return

            _clear_worktree_flow_state(context.user_data)
            ok, msg = await _create_worktree_from_topic(
                bot=context.bot,
                user_id=user_id,
                thread_id=thread_id,
                worktree_name=worktree_name,
                chat_id=chat_id,
            )
            if ok:
                await safe_reply(message, f"✅ {msg}")
            else:
                await safe_reply(message, f"❌ {msg}")
            return

    if context.user_data and context.user_data.get(STATE_KEY) == STATE_WORKTREE_FOLD_SELECT:
        pending_tid = context.user_data.get(WORKTREE_PENDING_THREAD_KEY)
        if pending_tid is not None and pending_tid != thread_id:
            _clear_worktree_flow_state(context.user_data)
        else:
            await safe_reply(
                message,
                "Use the fold selector buttons above, or tap Back.",
            )
            return

    # Folder-creation text capture for directory browser.
    if context.user_data and context.user_data.get(STATE_KEY) == STATE_CREATING_DIRECTORY:
        pending_tid = context.user_data.get("_pending_thread_id")
        if pending_tid is not None and pending_tid != thread_id:
            clear_browse_state(context.user_data)
            context.user_data.pop("_pending_thread_id", None)
            context.user_data.pop("_pending_thread_text", None)
        else:
            folder_name = text.strip()
            if not folder_name:
                await safe_reply(
                    message,
                    "Folder name cannot be empty. Send a short name like `new-script`.",
                )
                return
            if folder_name in {".", ".."} or "/" in folder_name or "\\" in folder_name:
                await safe_reply(
                    message,
                    "Invalid folder name. Send a single folder name without path separators.",
                )
                return

            current_path, root_path = _get_browse_current_path(
                context.user_data,
                chat_id=chat_id,
            )
            candidate = (current_path / folder_name).resolve()
            if not is_within_browse_root(candidate, root_path):
                await safe_reply(
                    message,
                    "❌ Folder must stay inside the configured browser root.",
                )
                return
            if candidate.exists():
                if candidate.is_dir():
                    await safe_reply(
                        message,
                        f"`{folder_name}` already exists.",
                    )
                else:
                    await safe_reply(
                        message,
                        f"`{folder_name}` already exists and is not a directory.",
                    )
                return
            try:
                candidate.mkdir(parents=False, exist_ok=False)
            except OSError as exc:
                await safe_reply(
                    message,
                    f"❌ Failed to create folder: {exc.strerror or str(exc)}",
                )
                return

            msg_text, keyboard, subdirs = build_directory_browser(
                str(candidate),
                root_path=str(root_path),
            )
            context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
            context.user_data[BROWSE_PATH_KEY] = str(candidate)
            context.user_data[BROWSE_ROOT_KEY] = str(root_path)
            context.user_data[BROWSE_PAGE_KEY] = 0
            context.user_data[BROWSE_DIRS_KEY] = subdirs

            await safe_reply(message, f"✅ Created folder `{folder_name}`.")
            await safe_reply(message, msg_text, reply_markup=keyboard)
            return

    # Ignore text in directory browsing mode (only for the same thread)
    if (
        context.user_data
        and context.user_data.get(STATE_KEY) == STATE_BROWSING_DIRECTORY
    ):
        pending_tid = context.user_data.get("_pending_thread_id")
        if pending_tid == thread_id:
            await safe_reply(
                update.message,
                "Please use the directory browser buttons above "
                "(including `➕ Folder`), or tap Cancel.",
            )
            return
        # Stale browsing state from a different thread — clear it
        clear_browse_state(context.user_data)
        context.user_data.pop("_pending_thread_id", None)
        context.user_data.pop("_pending_thread_text", None)

    await _forward_topic_text_message(
        message=message,
        context=context,
        user_id=user_id,
        thread_id=thread_id,
        chat_id=chat_id,
        text=text,
    )


async def channel_post_text_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle supergroup channel-post text messages in topics.

    Some groups send topic messages as channel posts (e.g. posting as channel).
    Route plain text channel posts through the standard text flow.
    """
    msg = update.channel_post
    if not msg or not msg.text or msg.text.startswith("/"):
        return
    await text_handler(update, context)


async def inbound_update_probe(
    update: Update, _context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Log inbound Telegram update shape for troubleshooting delivery issues."""
    msg = update.effective_message
    if not msg:
        return

    if update.channel_post:
        kind = "channel_post"
    elif update.message:
        kind = "message"
    elif update.edited_channel_post:
        kind = "edited_channel_post"
    elif update.edited_message:
        kind = "edited_message"
    else:
        kind = "other"

    logger.info(
        "Inbound update: kind=%s chat_type=%s thread=%s from_user=%s sender_chat=%s text=%r",
        kind,
        msg.chat.type if msg.chat else None,
        getattr(msg, "message_thread_id", None),
        msg.from_user.id if msg.from_user else None,
        msg.sender_chat.id if msg.sender_chat else None,
        (msg.text or "")[:120],
    )

    incoming_text = msg.text or msg.caption
    group_chat_id = _group_chat_id(msg.chat)
    if group_chat_id is not None:
        _remember_group_member(group_chat_id, msg.from_user)
        for member in getattr(msg, "new_chat_members", []) or []:
            _remember_group_member(group_chat_id, member)
        left_member = getattr(msg, "left_chat_member", None)
        if left_member is not None:
            _remember_group_member(group_chat_id, left_member)
    log_incoming_message(
        kind=kind,
        text=incoming_text,
        chat_id=msg.chat_id if msg.chat_id is not None else None,
        thread_id=getattr(msg, "message_thread_id", None),
        message_id=getattr(msg, "message_id", None),
        from_user_id=msg.from_user.id if msg.from_user else None,
        sender_chat_id=msg.sender_chat.id if msg.sender_chat else None,
        chat_type=msg.chat.type if msg.chat else None,
    )


# --- Callback query handler ---


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    chat = update.effective_chat
    if not _is_chat_allowed(chat):
        await query.answer("This group is not allowed to use this bot.", show_alert=True)
        return

    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        await query.answer("Not authorized")
        return

    data = query.data

    # Capture group chat_id for supergroup forum topic routing.
    # Required: Telegram Bot API needs group chat_id (not user_id) to send
    # messages with message_thread_id. Do NOT remove — see session.py docs.
    cb_thread_id = _get_thread_id(update)
    cb_chat_id = _group_chat_id(chat)
    if cb_chat_id is not None:
        session_manager.set_group_chat_id(user.id, cb_thread_id, cb_chat_id)

    # History: older/newer pagination
    # Format: hp:<page>:<window_id>:<start>:<end> or hn:<page>:<window_id>:<start>:<end>
    if data.startswith(CB_HISTORY_PREV) or data.startswith(CB_HISTORY_NEXT):
        await query.answer(
            "History pagination is unavailable in app-server mode.",
            show_alert=True,
        )
        return

    # Directory browser handlers
    elif data.startswith(CB_DIR_MACHINE_SELECT):
        if not _can_user_create_sessions(user.id):
            await query.answer(
                "Single-session users cannot create/bind sessions.",
                show_alert=True,
            )
            return
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale browser (topic mismatch)", show_alert=True)
            return
        machine_id = data[len(CB_DIR_MACHINE_SELECT) :].strip()
        node = node_registry.get_node(machine_id)
        if node is None:
            await query.answer("Machine not found.", show_alert=True)
            return
        if node.status != "online":
            await query.answer("Machine is offline.", show_alert=True)
            return
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
            context.user_data[BROWSE_MACHINE_KEY] = node.machine_id
            context.user_data[BROWSE_MACHINE_NAME_KEY] = node.display_name
            context.user_data[BROWSE_PAGE_KEY] = 0
        try:
            msg_text, keyboard, subdirs = await _build_directory_browser_for_context(
                context.user_data,
                chat_id=cb_chat_id,
                page=0,
            )
        except Exception as exc:
            await query.answer(f"Failed to load machine folders: {exc}", show_alert=True)
            return
        if context.user_data is not None:
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer(node.display_name)

    elif data.startswith(CB_DIR_SELECT):
        if not _can_user_create_sessions(user.id):
            await query.answer(
                "Single-session users cannot create/bind sessions.",
                show_alert=True,
            )
            return
        # Validate: callback must come from the same topic that started browsing
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale browser (topic mismatch)", show_alert=True)
            return
        # callback_data contains index, not dir name (to avoid 64-byte limit)
        try:
            idx = int(data[len(CB_DIR_SELECT) :])
        except ValueError:
            await query.answer("Invalid data")
            return

        # Look up dir name from cached subdirs
        cached_dirs: list[str] = (
            context.user_data.get(BROWSE_DIRS_KEY, []) if context.user_data else []
        )
        if idx < 0 or idx >= len(cached_dirs):
            await query.answer(
                "Directory list changed, please refresh", show_alert=True
            )
            return
        subdir_name = cached_dirs[idx]

        raw_machine_id = (
            context.user_data.get(BROWSE_MACHINE_KEY, "") if context.user_data else ""
        )
        machine_id = raw_machine_id.strip() if isinstance(raw_machine_id, str) else ""
        local_machine_id, _local_machine_name = _local_machine_identity()
        if machine_id and machine_id != local_machine_id:
            current_raw = (
                context.user_data.get(BROWSE_PATH_KEY, "") if context.user_data else ""
            )
            current_text = current_raw.strip() if isinstance(current_raw, str) else ""
            new_path_str = str(Path(current_text or "/") / subdir_name)
        else:
            current_path, root_path = _get_browse_current_path(
                context.user_data,
                chat_id=cb_chat_id,
            )
            new_path = (current_path / subdir_name).resolve()

            if not is_within_browse_root(new_path, root_path):
                await query.answer(
                    "Folder is outside browse root.",
                    show_alert=True,
                )
                return
            if not new_path.exists() or not new_path.is_dir():
                await query.answer("Directory not found", show_alert=True)
                return
            new_path_str = str(new_path)
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
            context.user_data[BROWSE_PATH_KEY] = new_path_str
            context.user_data[BROWSE_PAGE_KEY] = 0

        msg_text, keyboard, subdirs = await _build_directory_browser_for_context(
            context.user_data,
            chat_id=cb_chat_id,
            page=0,
        )
        if context.user_data is not None:
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    elif data == CB_DIR_UP:
        if not _can_user_create_sessions(user.id):
            await query.answer(
                "Single-session users cannot create/bind sessions.",
                show_alert=True,
            )
            return
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale browser (topic mismatch)", show_alert=True)
            return
        raw_machine_id = (
            context.user_data.get(BROWSE_MACHINE_KEY, "") if context.user_data else ""
        )
        machine_id = raw_machine_id.strip() if isinstance(raw_machine_id, str) else ""
        local_machine_id, _local_machine_name = _local_machine_identity()
        if machine_id and machine_id != local_machine_id:
            current_raw = (
                context.user_data.get(BROWSE_PATH_KEY, "") if context.user_data else ""
            )
            root_raw = (
                context.user_data.get(BROWSE_ROOT_KEY, "") if context.user_data else ""
            )
            current = Path(current_raw or "/")
            root_path = Path(root_raw or str(current))
            parent = current.parent if str(current) != str(root_path) else root_path
            parent_path = str(parent)
        else:
            current, root_path = _get_browse_current_path(
                context.user_data,
                chat_id=cb_chat_id,
            )
            parent = current.parent if current != root_path else root_path
            if not is_within_browse_root(parent, root_path):
                parent = root_path
            parent_path = str(parent)
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
            context.user_data[BROWSE_PATH_KEY] = parent_path
            context.user_data[BROWSE_PAGE_KEY] = 0

        msg_text, keyboard, subdirs = await _build_directory_browser_for_context(
            context.user_data,
            chat_id=cb_chat_id,
            page=0,
        )
        if context.user_data is not None:
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    elif data == CB_DIR_NEW_FOLDER:
        if not _can_user_create_sessions(user.id):
            await query.answer(
                "Single-session users cannot create/bind sessions.",
                show_alert=True,
            )
            return
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale browser (topic mismatch)", show_alert=True)
            return
        raw_machine_id = (
            context.user_data.get(BROWSE_MACHINE_KEY, "") if context.user_data else ""
        )
        machine_id = raw_machine_id.strip() if isinstance(raw_machine_id, str) else ""
        local_machine_id, _local_machine_name = _local_machine_identity()
        if machine_id and machine_id != local_machine_id:
            await query.answer("Remote folder creation is unavailable.", show_alert=True)
            return
        if context.user_data is None:
            await query.answer("Browser state unavailable.", show_alert=True)
            return

        current_path, root_path = _get_browse_current_path(
            context.user_data,
            chat_id=cb_chat_id,
        )
        raw_page = context.user_data.get(BROWSE_PAGE_KEY, 0)
        try:
            page = int(raw_page)
        except (TypeError, ValueError):
            page = 0
        msg_text, keyboard, subdirs = await _build_directory_browser_for_context(
            context.user_data,
            chat_id=cb_chat_id,
            page=page,
        )
        context.user_data[STATE_KEY] = STATE_CREATING_DIRECTORY
        context.user_data[BROWSE_PATH_KEY] = str(current_path)
        context.user_data[BROWSE_ROOT_KEY] = str(root_path)
        context.user_data[BROWSE_DIRS_KEY] = subdirs
        context.user_data[BROWSE_PAGE_KEY] = page

        await safe_edit(
            query,
            f"{msg_text}\n\nSend the new folder name in this topic.",
            reply_markup=keyboard,
        )
        await query.answer("Waiting for folder name", show_alert=True)

    elif data.startswith(CB_DIR_PAGE):
        if not _can_user_create_sessions(user.id):
            await query.answer(
                "Single-session users cannot create/bind sessions.",
                show_alert=True,
            )
            return
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale browser (topic mismatch)", show_alert=True)
            return
        try:
            pg = int(data[len(CB_DIR_PAGE) :])
        except ValueError:
            await query.answer("Invalid data")
            return
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
            context.user_data[BROWSE_PAGE_KEY] = pg
        msg_text, keyboard, subdirs = await _build_directory_browser_for_context(
            context.user_data,
            chat_id=cb_chat_id,
            page=pg,
        )
        if context.user_data is not None:
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    elif data == CB_DIR_CONFIRM:
        if not _can_user_create_sessions(user.id):
            await query.answer(
                "Single-session users cannot create/bind sessions.",
                show_alert=True,
            )
            return
        raw_machine_id = (
            context.user_data.get(BROWSE_MACHINE_KEY, "") if context.user_data else ""
        )
        machine_id = raw_machine_id.strip() if isinstance(raw_machine_id, str) else ""
        raw_machine_name = (
            context.user_data.get(BROWSE_MACHINE_NAME_KEY, "") if context.user_data else ""
        )
        machine_name = raw_machine_name.strip() if isinstance(raw_machine_name, str) else ""
        local_machine_id, local_machine_name = _local_machine_identity()
        if not machine_id:
            machine_id = local_machine_id
        if not machine_name:
            machine_name = local_machine_name

        if machine_id != local_machine_id:
            root_path = str(context.user_data.get(BROWSE_ROOT_KEY, "") if context.user_data else "")
            selected_path = str(context.user_data.get(BROWSE_PATH_KEY, "") if context.user_data else "")
            created_wname = Path(selected_path).name.strip() if selected_path else "codex"
            if not root_path or not selected_path:
                await query.answer("Directory browser expired.", show_alert=True)
                return
        else:
            current_path, root_path = _get_browse_current_path(
                context.user_data,
                chat_id=cb_chat_id,
            )
            selected_path = str(current_path)
            created_wname = current_path.name.strip() if current_path.name else "codex"
            if not is_within_browse_root(current_path, root_path):
                await query.answer("Directory outside browse root", show_alert=True)
                return
            if not current_path.exists() or not current_path.is_dir():
                await query.answer("Directory not found", show_alert=True)
                return
        # Check if this was initiated from a thread bind flow
        pending_thread_id: int | None = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )

        # Validate: confirm button must come from the same topic that started browsing
        confirm_thread_id = _get_thread_id(update)
        if pending_thread_id is not None and confirm_thread_id != pending_thread_id:
            clear_browse_state(context.user_data)
            _clear_directory_session_picker_state(context.user_data)
            if context.user_data is not None:
                context.user_data.pop("_pending_thread_id", None)
                context.user_data.pop("_pending_thread_text", None)
            await query.answer("Stale browser (topic mismatch)", show_alert=True)
            return

        clear_browse_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop(BROWSE_MACHINE_KEY, None)
            context.user_data.pop(BROWSE_MACHINE_NAME_KEY, None)
        _clear_directory_session_picker_state(context.user_data)
        if machine_id != local_machine_id:
            from .agent_rpc import agent_rpc_client

            prior_sessions = await agent_rpc_client.folder_sessions(
                machine_id,
                cwd=selected_path,
                limit=100,
            )
        else:
            prior_sessions = [
                {
                    "thread_id": summary.thread_id,
                    "created_at": summary.created_at,
                    "last_active_at": summary.last_active_at,
                }
                for summary in session_manager.list_codex_session_summaries_for_cwd(selected_path)
            ]

        if prior_sessions:
            _store_directory_session_picker_state(
                context.user_data,
                thread_id=pending_thread_id,
                chat_id=cb_chat_id,
                machine_id=machine_id,
                machine_name=machine_name,
                selected_path=selected_path,
                root_path=str(root_path),
                sessions=prior_sessions,
                page=0,
            )
            text = _build_directory_session_picker_text(
                selected_path=selected_path,
                sessions=prior_sessions,
                page=0,
            )
            keyboard = _build_directory_session_picker_keyboard(
                sessions=prior_sessions,
                page=0,
            )
            await safe_edit(query, text, reply_markup=keyboard)
            await query.answer("Choose a previous session or start fresh.")
            return

        success, message, created_wid = await _bind_selected_folder_to_topic(
            user_id=user.id,
            chat_id=cb_chat_id,
            pending_thread_id=pending_thread_id,
            machine_id=machine_id,
            machine_name=machine_name,
            selected_path=selected_path,
            window_name=created_wname,
        )
        codex_thread_id = session_manager.get_window_codex_thread_id(created_wid) if success else ""

        if success:
            logger.info(
                "Window created: %s (id=%s) at %s (user=%d, thread=%s)",
                created_wname,
                created_wid,
                selected_path,
                user.id,
                pending_thread_id,
            )
            if pending_thread_id is not None:
                # Thread bind flow: bind thread to newly created window
                # Rename the topic to match the window name
                resolved_chat = session_manager.resolve_chat_id(
                    user.id,
                    pending_thread_id,
                    chat_id=cb_chat_id,
                )
                try:
                    await context.bot.edit_forum_topic(
                        chat_id=resolved_chat,
                        message_thread_id=pending_thread_id,
                        name=created_wname,
                    )
                except Exception as e:
                    logger.debug(f"Failed to rename topic: {e}")

                await safe_edit(
                    query,
                    f"✅ {message}\n\nBound to this topic. Send messages here.",
                )

                # Send pending text if any
                pending_text = (
                    context.user_data.get("_pending_thread_text")
                    if context.user_data
                    else None
                )
                if pending_text:
                    logger.debug(
                        "Forwarding pending text to window %s (len=%d)",
                        created_wname,
                        len(pending_text),
                    )
                    if context.user_data is not None:
                        context.user_data.pop("_pending_thread_text", None)
                        context.user_data.pop("_pending_thread_id", None)
                    send_ok, send_msg = await session_manager.send_topic_text_to_window(
                        user_id=user.id,
                        thread_id=pending_thread_id,
                        chat_id=cb_chat_id,
                        window_id=created_wid,
                        text=pending_text,
                    )
                    if not send_ok:
                        logger.warning("Failed to forward pending text: %s", send_msg)
                        await safe_send(
                            context.bot,
                            resolved_chat,
                            f"❌ Failed to send pending message: {send_msg}",
                            message_thread_id=pending_thread_id,
                        )
                    else:
                        note_run_started(
                            user_id=user.id,
                            thread_id=pending_thread_id,
                            window_id=created_wid,
                            source="pending_text_new_window",
                            pending_text=pending_text,
                            expect_response=True,
                        )
                elif context.user_data is not None:
                    context.user_data.pop("_pending_thread_id", None)
            else:
                # Should not happen in topic-only mode, but handle gracefully
                await safe_edit(query, f"✅ {message}")
        else:
            await safe_edit(query, f"❌ {message}")
            if pending_thread_id is not None and context.user_data is not None:
                context.user_data.pop("_pending_thread_id", None)
                context.user_data.pop("_pending_thread_text", None)
        await query.answer("Created" if success else "Failed")

    elif data.startswith(CB_DIR_SESSION_PAGE):
        picker = context.user_data.get(DIR_SESSION_PICKER_KEY) if context.user_data else None
        if not isinstance(picker, dict):
            await query.answer("Folder session picker expired.", show_alert=True)
            return
        if picker.get("chat_id") != (cb_chat_id if cb_chat_id is not None else 0):
            await query.answer("Stale picker (chat mismatch)", show_alert=True)
            return
        if picker.get("thread_id") != (cb_thread_id or 0):
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        try:
            page = int(data[len(CB_DIR_SESSION_PAGE) :])
        except ValueError:
            await query.answer("Invalid selection.", show_alert=True)
            return
        sessions = picker.get("items")
        selected_path = str(picker.get("selected_path", "")).strip()
        if not isinstance(sessions, list) or not selected_path:
            await query.answer("Folder session picker expired.", show_alert=True)
            return
        _store_directory_session_picker_state(
            context.user_data,
            thread_id=cb_thread_id,
            chat_id=cb_chat_id,
            machine_id=str(picker.get("machine_id", "")).strip(),
            machine_name=str(picker.get("machine_name", "")).strip(),
            selected_path=selected_path,
            root_path=str(picker.get("root_path", "")),
            sessions=[item for item in sessions if isinstance(item, dict)],
            page=page,
        )
        await safe_edit(
            query,
            _build_directory_session_picker_text(
                selected_path=selected_path,
                sessions=[item for item in sessions if isinstance(item, dict)],
                page=page,
            ),
            reply_markup=_build_directory_session_picker_keyboard(
                sessions=[item for item in sessions if isinstance(item, dict)],
                page=page,
            ),
        )
        await query.answer()

    elif data == CB_DIR_SESSION_BACK:
        picker = context.user_data.get(DIR_SESSION_PICKER_KEY) if context.user_data else None
        if not isinstance(picker, dict):
            await query.answer("Folder session picker expired.", show_alert=True)
            return
        if picker.get("chat_id") != (cb_chat_id if cb_chat_id is not None else 0):
            await query.answer("Stale picker (chat mismatch)", show_alert=True)
            return
        if picker.get("thread_id") != (cb_thread_id or 0):
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        selected_path = str(picker.get("selected_path", "")).strip()
        root_path = str(picker.get("root_path", "")).strip()
        machine_id = str(picker.get("machine_id", "")).strip()
        machine_name = str(picker.get("machine_name", "")).strip()
        local_machine_id, local_machine_name = _local_machine_identity()
        _clear_directory_session_picker_state(context.user_data)
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
            context.user_data[BROWSE_MACHINE_KEY] = machine_id or local_machine_id
            context.user_data[BROWSE_MACHINE_NAME_KEY] = machine_name or local_machine_name
            context.user_data[BROWSE_PATH_KEY] = selected_path or root_path
            context.user_data[BROWSE_ROOT_KEY] = root_path or selected_path
            context.user_data[BROWSE_PAGE_KEY] = 0
        msg_text, keyboard, subdirs = await _build_directory_browser_for_context(
            context.user_data,
            chat_id=cb_chat_id,
            page=0,
        )
        if context.user_data is not None:
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer("Back")

    elif data == CB_DIR_SESSION_FRESH or data.startswith(CB_DIR_SESSION_RESUME):
        picker = context.user_data.get(DIR_SESSION_PICKER_KEY) if context.user_data else None
        if not isinstance(picker, dict):
            await query.answer("Folder session picker expired.", show_alert=True)
            return
        if picker.get("chat_id") != (cb_chat_id if cb_chat_id is not None else 0):
            await query.answer("Stale picker (chat mismatch)", show_alert=True)
            return
        pending_thread_id = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if picker.get("thread_id") != (pending_thread_id or 0):
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        machine_id = str(picker.get("machine_id", "")).strip()
        machine_name = str(picker.get("machine_name", "")).strip()
        selected_path = str(picker.get("selected_path", "")).strip()
        root_path = str(picker.get("root_path", "")).strip()
        if not selected_path:
            await query.answer("Selected folder missing.", show_alert=True)
            return
        local_machine_id, local_machine_name = _local_machine_identity()
        if machine_id and machine_id != local_machine_id:
            created_wname = Path(selected_path).name.strip() or "codex"
        else:
            selected_dir = Path(selected_path)
            if not selected_dir.exists() or not selected_dir.is_dir():
                await query.answer("Directory not found", show_alert=True)
                return
            if root_path and not is_within_browse_root(selected_dir, root_path):
                await query.answer("Directory outside browse root", show_alert=True)
                return
            created_wname = selected_dir.name.strip() if selected_dir.name else "codex"

        resume_thread_id = ""
        if data.startswith(CB_DIR_SESSION_RESUME):
            raw_idx = data[len(CB_DIR_SESSION_RESUME) :]
            try:
                idx = int(raw_idx)
            except ValueError:
                await query.answer("Invalid selection.", show_alert=True)
                return
            raw_items = picker.get("items")
            if not isinstance(raw_items, list) or idx < 0 or idx >= len(raw_items):
                await query.answer("Selection is out of range.", show_alert=True)
                return
            selected_item = raw_items[idx]
            if not isinstance(selected_item, dict):
                await query.answer("Selection is invalid.", show_alert=True)
                return
            resume_thread_id = str(selected_item.get("thread_id", "")).strip()
            if not resume_thread_id:
                await query.answer("Selection is invalid.", show_alert=True)
                return

        success, message, created_wid = await _bind_selected_folder_to_topic(
            user_id=user.id,
            chat_id=cb_chat_id,
            pending_thread_id=pending_thread_id,
            machine_id=machine_id or local_machine_id,
            machine_name=machine_name or local_machine_name,
            selected_path=selected_path,
            window_name=created_wname,
            resume_thread_id=resume_thread_id,
        )
        if not success:
            await query.answer(message, show_alert=True)
            return

        _clear_directory_session_picker_state(context.user_data)
        resolved_chat = session_manager.resolve_chat_id(
            user.id,
            pending_thread_id,
            chat_id=cb_chat_id,
        )
        try:
            await context.bot.edit_forum_topic(
                chat_id=resolved_chat,
                message_thread_id=pending_thread_id,
                name=created_wname,
            )
        except Exception as e:
            logger.debug(f"Failed to rename topic: {e}")

        await safe_edit(
            query,
            f"✅ {message}\n\nBound to this topic. Send messages here.",
        )

        pending_text = (
            context.user_data.get("_pending_thread_text") if context.user_data else None
        )
        if pending_text:
            logger.debug(
                "Forwarding pending text to window %s (len=%d)",
                created_wname,
                len(pending_text),
            )
            if context.user_data is not None:
                context.user_data.pop("_pending_thread_text", None)
                context.user_data.pop("_pending_thread_id", None)
            send_ok, send_msg = await session_manager.send_topic_text_to_window(
                user_id=user.id,
                thread_id=pending_thread_id,
                chat_id=cb_chat_id,
                window_id=created_wid,
                text=pending_text,
            )
            if not send_ok:
                logger.warning("Failed to forward pending text: %s", send_msg)
                await safe_send(
                    context.bot,
                    resolved_chat,
                    f"❌ Failed to send pending message: {send_msg}",
                    message_thread_id=pending_thread_id,
                )
            else:
                note_run_started(
                    user_id=user.id,
                    thread_id=pending_thread_id,
                    window_id=created_wid,
                    source="pending_text_new_window",
                    pending_text=pending_text,
                    expect_response=True,
                )
        elif context.user_data is not None:
            context.user_data.pop("_pending_thread_id", None)
        await query.answer("Resumed" if resume_thread_id else "Created")

    elif data == CB_DIR_CANCEL:
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale browser (topic mismatch)", show_alert=True)
            return
        clear_browse_state(context.user_data)
        _clear_directory_session_picker_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop(BROWSE_MACHINE_KEY, None)
            context.user_data.pop(BROWSE_MACHINE_NAME_KEY, None)
            context.user_data.pop("_pending_thread_id", None)
            context.user_data.pop("_pending_thread_text", None)
        await safe_edit(query, "Cancelled")
        await query.answer("Cancelled")

    # Model menu: refresh current catalog/config view
    elif data == CB_MODEL_REFRESH:
        if cb_thread_id is None:
            await query.answer("Use this inside a named topic.", show_alert=True)
            return
        session_manager.ensure_topic_binding(user.id, cb_thread_id, chat_id=cb_chat_id)
        catalog = _resolve_topic_model_catalog(
            user_id=user.id,
            thread_id=cb_thread_id,
            chat_id=cb_chat_id,
        )
        await safe_edit(
            query,
            _build_model_info_text(catalog),
            reply_markup=_build_model_keyboard(catalog),
        )
        await query.answer("Refreshed")

    # Model menu: set per-topic model selection
    elif data.startswith(CB_MODEL_SET):
        if cb_thread_id is None:
            await query.answer("Use this inside a named topic.", show_alert=True)
            return
        session_manager.ensure_topic_binding(user.id, cb_thread_id, chat_id=cb_chat_id)
        previous_model, previous_effort = session_manager.get_topic_model_selection(
            user.id,
            cb_thread_id,
            chat_id=cb_chat_id,
        )
        selected_slug = data[len(CB_MODEL_SET) :]
        catalog = _resolve_topic_model_catalog(
            user_id=user.id,
            thread_id=cb_thread_id,
            chat_id=cb_chat_id,
        )
        selected = _get_model_entry(catalog, selected_slug)
        if not selected:
            await query.answer("Unknown model", show_alert=True)
            return

        adjusted_effort: str | None = None
        levels_raw = selected.get("levels")
        levels = [str(item) for item in levels_raw] if isinstance(levels_raw, list) else []
        current_effort = str(catalog.get("current_effort", ""))
        if levels and current_effort not in levels:
            default_effort = str(selected.get("default_effort", ""))
            adjusted_effort = default_effort if default_effort in levels else levels[0]
        session_manager.set_topic_model_selection(
            user.id,
            cb_thread_id,
            chat_id=cb_chat_id,
            model_slug=selected_slug,
            reasoning_effort=adjusted_effort or current_effort,
        )
        changed_model, changed_effort = (
            previous_model != selected_slug,
            (adjusted_effort or current_effort) != previous_effort,
        )
        if changed_model or changed_effort:
            model_window_id = session_manager.resolve_window_for_thread(
                user.id,
                cb_thread_id,
                chat_id=cb_chat_id,
            )
            if model_window_id:
                session_manager.set_window_codex_thread_id(model_window_id, "")
        updated_catalog = _resolve_topic_model_catalog(
            user_id=user.id,
            thread_id=cb_thread_id,
            chat_id=cb_chat_id,
        )
        await safe_edit(
            query,
            _build_model_info_text(updated_catalog),
            reply_markup=_build_model_keyboard(updated_catalog),
        )
        if adjusted_effort:
            await query.answer(f"Saved {selected_slug} for this topic")
        else:
            await query.answer(f"Saved {selected_slug} for this topic")

    # Model menu: set per-topic reasoning effort
    elif data.startswith(CB_MODEL_EFFORT_SET):
        if cb_thread_id is None:
            await query.answer("Use this inside a named topic.", show_alert=True)
            return
        session_manager.ensure_topic_binding(user.id, cb_thread_id, chat_id=cb_chat_id)
        previous_model, previous_effort = session_manager.get_topic_model_selection(
            user.id,
            cb_thread_id,
            chat_id=cb_chat_id,
        )
        selected_effort = data[len(CB_MODEL_EFFORT_SET) :]
        catalog = _resolve_topic_model_catalog(
            user_id=user.id,
            thread_id=cb_thread_id,
            chat_id=cb_chat_id,
        )
        current_slug = str(catalog.get("current_model", ""))

        allowed_levels: list[str] = []
        current_entry = _get_model_entry(catalog, current_slug)
        if current_entry and isinstance(current_entry.get("levels"), list):
            allowed_levels = [str(item) for item in current_entry["levels"]]
        if not allowed_levels:
            raw_levels = catalog.get("reasoning_options")
            if isinstance(raw_levels, list):
                allowed_levels = [str(item) for item in raw_levels]

        if allowed_levels and selected_effort not in allowed_levels:
            await query.answer("Reasoning level not supported for current model", show_alert=True)
            return

        session_manager.set_topic_model_selection(
            user.id,
            cb_thread_id,
            chat_id=cb_chat_id,
            model_slug=current_slug,
            reasoning_effort=selected_effort,
        )
        if selected_effort != previous_effort:
            effort_window_id = session_manager.resolve_window_for_thread(
                user.id,
                cb_thread_id,
                chat_id=cb_chat_id,
            )
            if effort_window_id:
                session_manager.set_window_codex_thread_id(effort_window_id, "")
        updated_catalog = _resolve_topic_model_catalog(
            user_id=user.id,
            thread_id=cb_thread_id,
            chat_id=cb_chat_id,
        )
        await safe_edit(
            query,
            _build_model_info_text(updated_catalog),
            reply_markup=_build_model_keyboard(updated_catalog),
        )
        await query.answer(f"Saved {selected_effort} for this topic")

    # Update panel: refresh status
    elif data == CB_UPDATE_REFRESH:
        can_trigger_upgrade = _is_admin_user(user.id)
        text, keyboard = await _build_update_panel_payload(
            can_trigger_upgrade=can_trigger_upgrade,
        )
        await safe_edit(query, text, reply_markup=keyboard)
        await query.answer("Refreshed")

    # Update panel: run CoCo and/or Codex update + restart
    elif data in {
        CB_UPDATE_RUN,
        CB_UPDATE_RUN_CODEX,
        CB_UPDATE_RUN_COCO,
        CB_UPDATE_RUN_BOTH,
    }:
        if not _is_admin_user(user.id):
            await query.answer("Only admins can run updates.", show_alert=True)
            return
        message_chat_id = getattr(query.message, "chat_id", None)
        if message_chat_id is None and chat is not None:
            message_chat_id = chat.id
        if message_chat_id is None:
            await query.answer("Unable to resolve chat for restart.", show_alert=True)
            return
        if data == CB_UPDATE_RUN_COCO:
            await safe_edit(query, "⏳ Running CoCo update...")
            ok, text = await _run_coco_update_and_restart(
                chat_id=int(message_chat_id),
                thread_id=cb_thread_id,
            )
        elif data == CB_UPDATE_RUN_BOTH:
            await safe_edit(query, "⏳ Running CoCo + Codex updates...")
            ok, text = await _run_both_updates_and_restart(
                chat_id=int(message_chat_id),
                thread_id=cb_thread_id,
            )
        else:
            await safe_edit(query, "⏳ Running Codex upgrade...")
            ok, text = await _run_codex_upgrade_and_restart(
                chat_id=int(message_chat_id),
                thread_id=cb_thread_id,
            )
        await safe_edit(query, text)
        if ok:
            await query.answer("Update queued")
        else:
            await query.answer("Update failed", show_alert=True)

    # App-server approval: admin selects accept/decline decision.
    elif data.startswith(CB_APP_APPROVAL_DECIDE):
        if not _is_admin_user(user.id):
            await query.answer("Only admins can approve actions.", show_alert=True)
            return
        parsed = _parse_app_server_approval_callback(data)
        if parsed is None:
            await query.answer("Invalid approval callback.", show_alert=True)
            return
        token, action = parsed
        decision = APP_SERVER_APPROVAL_ACTION_TO_DECISION.get(action)
        if not decision:
            await query.answer("Unknown approval action.", show_alert=True)
            return
        resolved = _resolve_pending_app_server_approval(token, decision)
        emit_telemetry(
            "approval.callback.decision",
            user_id=user.id,
            token=token,
            action=action,
            decision=decision,
            resolved=resolved,
        )
        if not resolved:
            await query.answer("Approval request already resolved.", show_alert=True)
            return
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.answer(APP_SERVER_APPROVAL_DECISION_LABEL.get(decision, "Decision recorded"))

    # Session approvals: open/refresh window override panel
    elif data in {CB_APPROVAL_REFRESH, CB_APPROVAL_OPEN_WINDOW}:
        if not _is_admin_user(user.id):
            await query.answer("Only admins can change approvals.", show_alert=True)
            return
        if config.session_provider != "codex":
            await query.answer(
                "Available only in Codex provider mode.",
                show_alert=True,
            )
            return
        if cb_thread_id is None:
            await query.answer("Use this inside a named topic.", show_alert=True)
            return
        wid = session_manager.resolve_window_for_thread(user.id, cb_thread_id, chat_id=cb_chat_id)
        if not wid:
            await query.answer("No session bound to this topic.", show_alert=True)
            return
        can_use_dangerous = _is_admin_user(user.id)
        workspace_dir = _resolve_workspace_dir_for_window(
            user_id=user.id,
            thread_id=cb_thread_id,
            window_id=wid,
        )
        await safe_edit(
            query,
            _build_approvals_text(
                user.id,
                wid,
                workspace_dir=workspace_dir,
                defaults_view=False,
            ),
            reply_markup=_build_approvals_keyboard(
                wid,
                defaults_view=False,
                can_use_dangerous=can_use_dangerous,
            ),
        )
        await query.answer("Refreshed" if data == CB_APPROVAL_REFRESH else "Session")

    # Session approvals: open/refresh app default panel
    elif data in {CB_APPROVAL_REFRESH_DEFAULT, CB_APPROVAL_OPEN_DEFAULTS}:
        if not _is_admin_user(user.id):
            await query.answer("Only admins can change approvals.", show_alert=True)
            return
        if config.session_provider != "codex":
            await query.answer(
                "Available only in Codex provider mode.",
                show_alert=True,
            )
            return
        if cb_thread_id is None:
            await query.answer("Use this inside a named topic.", show_alert=True)
            return
        wid = session_manager.resolve_window_for_thread(user.id, cb_thread_id, chat_id=cb_chat_id)
        if not wid:
            await query.answer("No session bound to this topic.", show_alert=True)
            return
        can_use_dangerous = _is_admin_user(user.id)
        workspace_dir = _resolve_workspace_dir_for_window(
            user_id=user.id,
            thread_id=cb_thread_id,
            window_id=wid,
        )
        await safe_edit(
            query,
            _build_approvals_text(
                user.id,
                wid,
                workspace_dir=workspace_dir,
                defaults_view=True,
            ),
            reply_markup=_build_approvals_keyboard(
                wid,
                defaults_view=True,
                can_use_dangerous=can_use_dangerous,
            ),
        )
        await query.answer(
            "Refreshed" if data == CB_APPROVAL_REFRESH_DEFAULT else "Defaults"
        )

    # Session approvals: set mode and restart assistant in current window
    elif data.startswith(CB_APPROVAL_SET):
        if not _is_admin_user(user.id):
            await query.answer("Only admins can change approvals.", show_alert=True)
            return
        if config.session_provider != "codex":
            await query.answer(
                "Available only in Codex provider mode.",
                show_alert=True,
            )
            return
        selected_mode = data[len(CB_APPROVAL_SET) :]
        normalized_mode = _normalize_approval_mode(selected_mode)
        if normalized_mode is None:
            await query.answer("Unknown approval mode.", show_alert=True)
            return
        can_use_dangerous = _is_admin_user(user.id)
        if normalized_mode == APPROVAL_MODE_DANGEROUS and not can_use_dangerous:
            await query.answer(
                "Only admins can set Dangerous mode.",
                show_alert=True,
            )
            return
        if cb_thread_id is None:
            await query.answer("Use this inside a named topic.", show_alert=True)
            return
        wid = session_manager.resolve_window_for_thread(user.id, cb_thread_id, chat_id=cb_chat_id)
        if not wid:
            await query.answer("No session bound to this topic.", show_alert=True)
            return

        ok, err = await _apply_window_approval_mode(wid, normalized_mode)
        if not ok:
            await query.answer(err, show_alert=True)
            return

        workspace_dir = _resolve_workspace_dir_for_window(
            user_id=user.id,
            thread_id=cb_thread_id,
            window_id=wid,
        )
        await safe_edit(
            query,
            _build_approvals_text(
                user.id,
                wid,
                workspace_dir=workspace_dir,
                defaults_view=False,
            ),
            reply_markup=_build_approvals_keyboard(
                wid,
                defaults_view=False,
                can_use_dangerous=can_use_dangerous,
            ),
        )
        await query.answer(f"Set to {_approval_mode_button_label(normalized_mode)}")

    # Session approvals: set app-wide default
    elif data.startswith(CB_APPROVAL_SET_DEFAULT):
        if not _is_admin_user(user.id):
            await query.answer("Only admins can change approvals.", show_alert=True)
            return
        if config.session_provider != "codex":
            await query.answer(
                "Available only in Codex provider mode.",
                show_alert=True,
            )
            return
        selected_mode = data[len(CB_APPROVAL_SET_DEFAULT) :]
        normalized_mode = _normalize_approval_mode(selected_mode)
        if normalized_mode is None:
            await query.answer("Unknown approval mode.", show_alert=True)
            return
        can_use_dangerous = _is_admin_user(user.id)
        if normalized_mode == APPROVAL_MODE_DANGEROUS and not can_use_dangerous:
            await query.answer(
                "Only admins can set Dangerous mode.",
                show_alert=True,
            )
            return
        if cb_thread_id is None:
            await query.answer("Use this inside a named topic.", show_alert=True)
            return
        wid = session_manager.resolve_window_for_thread(user.id, cb_thread_id, chat_id=cb_chat_id)
        if not wid:
            await query.answer("No session bound to this topic.", show_alert=True)
            return

        _set_app_default_approval_mode(normalized_mode)
        workspace_dir = _resolve_workspace_dir_for_window(
            user_id=user.id,
            thread_id=cb_thread_id,
            window_id=wid,
        )
        await safe_edit(
            query,
            _build_approvals_text(
                user.id,
                wid,
                workspace_dir=workspace_dir,
                defaults_view=True,
            ),
            reply_markup=_build_approvals_keyboard(
                wid,
                defaults_view=True,
                can_use_dangerous=can_use_dangerous,
            ),
        )
        await query.answer(f"Default set to {_approval_mode_button_label(normalized_mode)}")

    # Session lifecycle panel actions
    elif data == CB_SESSION_REFRESH:
        if config.session_provider != "codex" or not _codex_app_server_preferred():
            await query.answer("Session controls require Codex app-server.", show_alert=True)
            return
        if cb_thread_id is None:
            await query.answer("Use this inside a named topic.", show_alert=True)
            return
        wid = session_manager.resolve_window_for_thread(user.id, cb_thread_id, chat_id=cb_chat_id)
        if not wid:
            await query.answer("No session bound to this topic.", show_alert=True)
            return
        page = _session_picker_page_from_context(
            context.user_data,
            thread_id=cb_thread_id,
            window_id=wid,
        )
        ok, text, keyboard = await _build_session_panel_payload(
            user_id=user.id,
            thread_id=cb_thread_id,
            context_user_data=context.user_data,
            chat_id=cb_chat_id,
            page=page,
        )
        await safe_edit(query, text, reply_markup=keyboard if ok else None)
        await query.answer("Refreshed")

    elif data.startswith(CB_SESSION_PAGE):
        if config.session_provider != "codex" or not _codex_app_server_preferred():
            await query.answer("Session controls require Codex app-server.", show_alert=True)
            return
        if cb_thread_id is None:
            await query.answer("Use this inside a named topic.", show_alert=True)
            return
        wid = session_manager.resolve_window_for_thread(user.id, cb_thread_id, chat_id=cb_chat_id)
        if not wid:
            await query.answer("No session bound to this topic.", show_alert=True)
            return
        raw_page = data[len(CB_SESSION_PAGE) :]
        try:
            page = max(0, int(raw_page))
        except ValueError:
            await query.answer("Invalid page.", show_alert=True)
            return
        ok, text, keyboard = await _build_session_panel_payload(
            user_id=user.id,
            thread_id=cb_thread_id,
            context_user_data=context.user_data,
            chat_id=cb_chat_id,
            page=page,
        )
        await safe_edit(query, text, reply_markup=keyboard if ok else None)
        await query.answer(f"Page {page + 1}")

    elif data == CB_SESSION_FORK:
        if config.session_provider != "codex" or not _codex_app_server_preferred():
            await query.answer("Session controls require Codex app-server.", show_alert=True)
            return
        if cb_thread_id is None:
            await query.answer("Use this inside a named topic.", show_alert=True)
            return
        wid = session_manager.resolve_window_for_thread(user.id, cb_thread_id, chat_id=cb_chat_id)
        if not wid:
            await query.answer("No session bound to this topic.", show_alert=True)
            return
        state = session_manager.get_window_state(wid)
        machine_id = session_manager.get_window_machine_id(wid)
        local_machine_id, _local_machine_name = _local_machine_identity()
        current_thread_id = state.codex_thread_id.strip()
        current_turn_id = (
            state.codex_active_turn_id.strip()
            or (codex_app_server_client.get_active_turn_id(current_thread_id) or "")
        )
        if not current_thread_id:
            await query.answer(
                "No app-server thread yet. Send a prompt first.",
                show_alert=True,
            )
            return
        try:
            if machine_id and machine_id != local_machine_id:
                from .agent_rpc import agent_rpc_client

                result = await agent_rpc_client.fork_thread(
                    machine_id,
                    window_id=wid,
                    thread_id=current_thread_id,
                    turn_id=current_turn_id,
                )
            else:
                result = await codex_app_server_client.thread_fork(
                    thread_id=current_thread_id,
                    turn_id=current_turn_id or None,
                )
        except Exception as e:
            await query.answer(f"Fork failed: {e}", show_alert=True)
            return
        new_thread_id = str(result.get("thread_id", "")).strip() if isinstance(result, dict) else ""
        if not new_thread_id and isinstance(result, dict):
            new_thread_id = _extract_lifecycle_thread_id(result, fallback="")
        if not new_thread_id:
            await query.answer("Fork returned no thread id.", show_alert=True)
            return
        new_turn_id = str(result.get("turn_id", "")).strip() if isinstance(result, dict) else ""
        if not new_turn_id and isinstance(result, dict):
            new_turn_id = _extract_lifecycle_turn_id(result)
        session_manager.set_window_codex_thread_id(wid, new_thread_id)
        session_manager.set_window_codex_active_turn_id(wid, new_turn_id)
        page = _session_picker_page_from_context(
            context.user_data,
            thread_id=cb_thread_id,
            window_id=wid,
        )
        ok, text, keyboard = await _build_session_panel_payload(
            user_id=user.id,
            thread_id=cb_thread_id,
            context_user_data=context.user_data,
            chat_id=cb_chat_id,
            page=page,
        )
        await safe_edit(query, text, reply_markup=keyboard if ok else None)
        await query.answer("Forked")

    elif data.startswith(CB_SESSION_ROLLBACK):
        if config.session_provider != "codex" or not _codex_app_server_preferred():
            await query.answer("Session controls require Codex app-server.", show_alert=True)
            return
        if cb_thread_id is None:
            await query.answer("Use this inside a named topic.", show_alert=True)
            return
        wid = session_manager.resolve_window_for_thread(user.id, cb_thread_id, chat_id=cb_chat_id)
        if not wid:
            await query.answer("No session bound to this topic.", show_alert=True)
            return
        raw_count = data[len(CB_SESSION_ROLLBACK) :]
        try:
            rollback_turns = max(1, int(raw_count))
        except ValueError:
            await query.answer("Invalid rollback count.", show_alert=True)
            return
        state = session_manager.get_window_state(wid)
        machine_id = session_manager.get_window_machine_id(wid)
        local_machine_id, _local_machine_name = _local_machine_identity()
        current_thread_id = state.codex_thread_id.strip()
        if not current_thread_id:
            await query.answer("No app-server thread yet.", show_alert=True)
            return
        try:
            if machine_id and machine_id != local_machine_id:
                from .agent_rpc import agent_rpc_client

                result = await agent_rpc_client.rollback_thread(
                    machine_id,
                    window_id=wid,
                    thread_id=current_thread_id,
                    num_turns=rollback_turns,
                )
            else:
                result = await codex_app_server_client.thread_rollback(
                    thread_id=current_thread_id,
                    num_turns=rollback_turns,
                )
        except Exception as e:
            await query.answer(f"Rollback failed: {e}", show_alert=True)
            return
        rolled_thread_id = (
            str(result.get("thread_id", "")).strip()
            if isinstance(result, dict)
            else ""
        )
        if not rolled_thread_id and isinstance(result, dict):
            rolled_thread_id = _extract_lifecycle_thread_id(
                result,
                fallback=current_thread_id,
            )
        rolled_turn_id = str(result.get("turn_id", "")).strip() if isinstance(result, dict) else ""
        if not rolled_turn_id and isinstance(result, dict):
            rolled_turn_id = _extract_lifecycle_turn_id(result)
        session_manager.set_window_codex_thread_id(wid, rolled_thread_id)
        session_manager.set_window_codex_active_turn_id(wid, rolled_turn_id)
        page = _session_picker_page_from_context(
            context.user_data,
            thread_id=cb_thread_id,
            window_id=wid,
        )
        ok, text, keyboard = await _build_session_panel_payload(
            user_id=user.id,
            thread_id=cb_thread_id,
            context_user_data=context.user_data,
            chat_id=cb_chat_id,
            page=page,
        )
        await safe_edit(query, text, reply_markup=keyboard if ok else None)
        await query.answer(f"Rolled back {rollback_turns}")

    elif data == CB_SESSION_RESUME_LATEST:
        if config.session_provider != "codex" or not _codex_app_server_preferred():
            await query.answer("Session controls require Codex app-server.", show_alert=True)
            return
        if cb_thread_id is None:
            await query.answer("Use this inside a named topic.", show_alert=True)
            return
        wid = session_manager.resolve_window_for_thread(user.id, cb_thread_id, chat_id=cb_chat_id)
        if not wid:
            await query.answer("No session bound to this topic.", show_alert=True)
            return
        state = session_manager.get_window_state(wid)
        machine_id = session_manager.get_window_machine_id(wid)
        local_machine_id, _local_machine_name = _local_machine_identity()
        workspace_dir = state.cwd.strip()
        if not workspace_dir:
            await query.answer("No workspace bound to this topic.", show_alert=True)
            return
        previous_model_selection = session_manager.get_topic_model_selection(
            user.id,
            cb_thread_id,
            chat_id=cb_chat_id,
        )
        try:
            if machine_id and machine_id != local_machine_id:
                from .agent_rpc import agent_rpc_client

                result = await agent_rpc_client.resume_latest(
                    machine_id,
                    window_id=wid,
                    cwd=workspace_dir,
                    window_name=state.window_name or session_manager.get_display_name(wid),
                    approval_mode=state.approval_mode.strip(),
                )
                resumed_thread_id = str(result.get("thread_id", "")).strip()
                if resumed_thread_id:
                    session_manager.set_window_codex_thread_id(wid, resumed_thread_id)
                    session_manager.set_window_codex_active_turn_id(
                        wid,
                        str(result.get("turn_id", "")).strip(),
                    )
                    resumed_model = str(result.get("model_slug", "")).strip()
                    resumed_effort = str(result.get("reasoning_effort", "")).strip()
                    if resumed_model or resumed_effort:
                        session_manager.set_topic_model_selection(
                            user.id,
                            cb_thread_id,
                            chat_id=cb_chat_id,
                            model_slug=resumed_model,
                            reasoning_effort=resumed_effort,
                        )
            else:
                resumed_thread_id = await session_manager.resume_latest_codex_session_for_window(
                    window_id=wid,
                    cwd=workspace_dir,
                )
        except Exception as e:
            await query.answer(f"Resume latest failed: {e}", show_alert=True)
            return
        if not resumed_thread_id:
            await query.answer("No resumable session found for this workspace.", show_alert=True)
            return
        page = _session_picker_page_from_context(
            context.user_data,
            thread_id=cb_thread_id,
            window_id=wid,
        )
        ok, text, keyboard = await _build_session_panel_payload(
            user_id=user.id,
            thread_id=cb_thread_id,
            context_user_data=context.user_data,
            chat_id=cb_chat_id,
            page=page,
        )
        current_model_selection = session_manager.get_topic_model_selection(
            user.id,
            cb_thread_id,
            chat_id=cb_chat_id,
        )
        if previous_model_selection != current_model_selection and any(current_model_selection):
            text = (
                f"{text}\n\n"
                f"{_format_model_inherited_notice(*current_model_selection)}"
            )
        await safe_edit(query, text, reply_markup=keyboard if ok else None)
        await query.answer("Resumed latest")

    elif data.startswith(CB_SESSION_RESUME):
        if config.session_provider != "codex" or not _codex_app_server_preferred():
            await query.answer("Session controls require Codex app-server.", show_alert=True)
            return
        if cb_thread_id is None:
            await query.answer("Use this inside a named topic.", show_alert=True)
            return
        wid = session_manager.resolve_window_for_thread(user.id, cb_thread_id, chat_id=cb_chat_id)
        if not wid:
            await query.answer("No session bound to this topic.", show_alert=True)
            return
        raw_idx = data[len(CB_SESSION_RESUME) :]
        try:
            idx = int(raw_idx)
        except ValueError:
            await query.answer("Invalid selection.", show_alert=True)
            return

        machine_id = session_manager.get_window_machine_id(wid)
        local_machine_id, _local_machine_name = _local_machine_identity()
        available_threads: list[str] = []
        picker = context.user_data.get(SESSION_PICKER_THREADS_KEY) if context.user_data else None
        if isinstance(picker, dict):
            if picker.get("thread_id") == (cb_thread_id or 0) and picker.get("window_id") == wid:
                raw_items = picker.get("items")
                if isinstance(raw_items, list):
                    available_threads = [str(item) for item in raw_items if isinstance(item, str)]

        if not available_threads:
            state = session_manager.get_window_state(wid)
            current_thread_id = state.codex_thread_id.strip()
            available_threads, _list_error = await _list_all_session_threads(machine_id=machine_id)
            if current_thread_id and current_thread_id not in available_threads:
                available_threads.insert(0, current_thread_id)
            current_page = _session_picker_page_from_context(
                context.user_data,
                thread_id=cb_thread_id,
                window_id=wid,
            )
            if context.user_data is not None:
                context.user_data[SESSION_PICKER_THREADS_KEY] = {
                    "thread_id": cb_thread_id or 0,
                    "window_id": wid,
                    "machine_id": machine_id,
                    "items": available_threads,
                    "page": current_page,
                }

        if idx < 0 or idx >= len(available_threads):
            await query.answer("Selection is out of range.", show_alert=True)
            return

        target_thread_id = available_threads[idx]
        previous_model_selection = session_manager.get_topic_model_selection(
            user.id,
            cb_thread_id,
            chat_id=cb_chat_id,
        )
        try:
            if machine_id and machine_id != local_machine_id:
                from .agent_rpc import agent_rpc_client

                result = await agent_rpc_client.resume_thread(
                    machine_id,
                    window_id=wid,
                    cwd=session_manager.get_window_state(wid).cwd.strip(),
                    thread_id=target_thread_id,
                    window_name=session_manager.get_window_state(wid).window_name.strip(),
                    approval_mode=session_manager.get_window_state(wid).approval_mode.strip(),
                )
            else:
                result = await codex_app_server_client.thread_resume(
                    thread_id=target_thread_id,
                )
        except Exception as e:
            await query.answer(f"Resume failed: {e}", show_alert=True)
            return
        resumed_thread_id = (
            str(result.get("thread_id", "")).strip()
            if isinstance(result, dict)
            else ""
        )
        if not resumed_thread_id and isinstance(result, dict):
            resumed_thread_id = _extract_lifecycle_thread_id(
                result,
                fallback=target_thread_id,
            )
        resumed_turn_id = str(result.get("turn_id", "")).strip() if isinstance(result, dict) else ""
        if not resumed_turn_id and isinstance(result, dict):
            resumed_turn_id = _extract_lifecycle_turn_id(result)
        session_manager.set_window_codex_thread_id(wid, resumed_thread_id)
        session_manager.set_window_codex_active_turn_id(wid, resumed_turn_id)
        if machine_id and machine_id != local_machine_id:
            resumed_model = str(result.get("model_slug", "")).strip() if isinstance(result, dict) else ""
            resumed_effort = str(result.get("reasoning_effort", "")).strip() if isinstance(result, dict) else ""
            changed = session_manager.set_topic_model_selection(
                user.id,
                cb_thread_id,
                chat_id=cb_chat_id,
                model_slug=resumed_model,
                reasoning_effort=resumed_effort,
            )
        else:
            changed, resumed_model, resumed_effort = (
                session_manager.sync_window_topic_model_selection_from_codex_session(
                    window_id=wid,
                    codex_thread_id=resumed_thread_id,
                    cwd=session_manager.get_window_state(wid).cwd.strip(),
                )
            )
        page = _session_picker_page_from_context(
            context.user_data,
            thread_id=cb_thread_id,
            window_id=wid,
        )
        ok, text, keyboard = await _build_session_panel_payload(
            user_id=user.id,
            thread_id=cb_thread_id,
            context_user_data=context.user_data,
            chat_id=cb_chat_id,
            page=page,
        )
        if not changed:
            current_model_selection = session_manager.get_topic_model_selection(
                user.id,
                cb_thread_id,
                chat_id=cb_chat_id,
            )
            changed = (
                previous_model_selection != current_model_selection
                and any(current_model_selection)
            )
            resumed_model, resumed_effort = current_model_selection
        if changed:
            text = (
                f"{text}\n\n"
                f"{_format_model_inherited_notice(resumed_model, resumed_effort)}"
            )
        await safe_edit(query, text, reply_markup=keyboard if ok else None)
        await query.answer("Resumed")

    # Apps menu: overview refresh/back
    elif data in {CB_APPS_BACK, CB_APPS_REFRESH}:
        _clear_apps_flow_state(context.user_data)
        if cb_thread_id is None:
            await query.answer("Use this inside a named topic.", show_alert=True)
            return
        text, keyboard, _catalog, _enabled = _build_apps_panel_payload_for_topic(
            user_id=user.id,
            thread_id=cb_thread_id,
            chat_id=cb_chat_id,
        )
        await safe_edit(query, text, reply_markup=keyboard)
        await query.answer("Refreshed")

    # Apps menu: open one app action sheet.
    elif data.startswith(CB_APPS_OPEN):
        if cb_thread_id is None:
            await query.answer("Use this inside a named topic.", show_alert=True)
            return
        raw_identifier = data[len(CB_APPS_OPEN) :].strip()
        ok, text, keyboard, _canonical = _build_app_actions_payload_for_topic(
            user_id=user.id,
            thread_id=cb_thread_id,
            app_identifier=raw_identifier,
            chat_id=cb_chat_id,
        )
        await safe_edit(query, text, reply_markup=keyboard if ok else None)
        await query.answer("App actions" if ok else "Unknown app.", show_alert=not ok)

    # Apps menu: toggle enable/disable (used for non-config apps).
    elif data.startswith(CB_APPS_TOGGLE):
        if cb_thread_id is None:
            await query.answer("Use this inside a named topic.", show_alert=True)
            return
        raw_identifier = data[len(CB_APPS_TOGGLE) :].strip()
        catalog = session_manager.discover_skill_catalog()
        canonical = resolve_skill_identifier(raw_identifier, catalog) if raw_identifier else None
        if not canonical:
            await query.answer("Unknown app.", show_alert=True)
            return
        enabled_names = [
            item.name
            for item in session_manager.resolve_thread_skills(
                user.id,
                cb_thread_id,
                chat_id=cb_chat_id,
                catalog=catalog,
            )
        ]
        if canonical in enabled_names:
            enabled_names = [name for name in enabled_names if name != canonical]
            action = "Disabled"
        else:
            enabled_names = [*enabled_names, canonical]
            action = "Enabled"
        session_manager.set_thread_skills(
            user.id,
            cb_thread_id,
            enabled_names,
            chat_id=cb_chat_id,
        )
        text, keyboard = _build_apps_overview_payload(
            enabled_names=enabled_names,
            catalog=catalog,
        )
        await safe_edit(query, text, reply_markup=keyboard)
        await query.answer(f"{action} {canonical}")

    # Apps menu: run one app (enable in topic), then return to overview.
    elif data.startswith(CB_APPS_RUN):
        if cb_thread_id is None:
            await query.answer("Use this inside a named topic.", show_alert=True)
            return
        raw_identifier = data[len(CB_APPS_RUN) :].strip()
        catalog = session_manager.discover_skill_catalog()
        canonical = resolve_skill_identifier(raw_identifier, catalog) if raw_identifier else None
        if not canonical:
            await query.answer("Unknown app.", show_alert=True)
            return
        enabled_names = [
            item.name
            for item in session_manager.resolve_thread_skills(
                user.id,
                cb_thread_id,
                chat_id=cb_chat_id,
                catalog=catalog,
            )
        ]
        if canonical not in enabled_names:
            session_manager.set_thread_skills(
                user.id,
                cb_thread_id,
                [*enabled_names, canonical],
                chat_id=cb_chat_id,
            )
            enabled_names = [*enabled_names, canonical]
            response_text = f"Enabled {canonical}"
        else:
            response_text = f"{canonical} already enabled"
        text, keyboard = _build_apps_overview_payload(
            enabled_names=enabled_names,
            catalog=catalog,
        )
        await safe_edit(query, text, reply_markup=keyboard)
        await query.answer(response_text)

    # Apps menu: configure one app (if supported).
    elif data.startswith(CB_APPS_CONFIGURE):
        if cb_thread_id is None:
            await query.answer("Use this inside a named topic.", show_alert=True)
            return
        raw_identifier = data[len(CB_APPS_CONFIGURE) :].strip()
        catalog = session_manager.discover_skill_catalog()
        canonical = resolve_skill_identifier(raw_identifier, catalog) if raw_identifier else None
        if not canonical:
            await query.answer("Unknown app.", show_alert=True)
            return
        if canonical == "autoresearch":
            ok, text, keyboard, _wid = await _build_autoresearch_panel_payload_for_topic(
                user_id=user.id,
                thread_id=cb_thread_id,
                user_data=context.user_data,
                chat_id=cb_chat_id,
            )
            await safe_edit(query, text, reply_markup=keyboard if ok else None)
            if ok:
                await query.answer("Auto research config")
            else:
                await query.answer("Auto research unavailable", show_alert=True)
            return
        if canonical != "looper":
            await query.answer("No configurable settings for this app yet.", show_alert=True)
            return
        ok, text, keyboard, _wid = await _build_looper_panel_payload_for_topic(
            user_id=user.id,
            thread_id=cb_thread_id,
            user_data=context.user_data,
            chat_id=cb_chat_id,
        )
        await safe_edit(query, text, reply_markup=keyboard if ok else None)
        if ok:
            await query.answer("Looper config")
        else:
            await query.answer("Looper unavailable", show_alert=True)

    # Apps menu: autoresearch outcome prompt
    elif data == CB_APPS_AUTORESEARCH_OUTCOME:
        if cb_thread_id is None:
            await query.answer("Use this inside a named topic.", show_alert=True)
            return
        ok, text, keyboard, _wid = await _build_autoresearch_panel_payload_for_topic(
            user_id=user.id,
            thread_id=cb_thread_id,
            user_data=context.user_data,
            chat_id=cb_chat_id,
        )
        await safe_edit(query, text, reply_markup=keyboard if ok else None)
        if not ok:
            await query.answer("Auto research unavailable", show_alert=True)
            return
        if context.user_data is None:
            await query.answer("State unavailable.", show_alert=True)
            return
        context.user_data[STATE_KEY] = STATE_APPS_AUTORESEARCH_OUTCOME
        context.user_data[APPS_PENDING_THREAD_KEY] = cb_thread_id
        await query.answer(
            "Send the outcome you want this research to optimize for.",
            show_alert=True,
        )

    # Apps menu: open Looper panel
    elif data == CB_APPS_LOOPER_OPEN:
        if cb_thread_id is None:
            await query.answer("Use this inside a named topic.", show_alert=True)
            return
        ok, text, keyboard, _wid = await _build_looper_panel_payload_for_topic(
            user_id=user.id,
            thread_id=cb_thread_id,
            user_data=context.user_data,
            chat_id=cb_chat_id,
        )
        await safe_edit(query, text, reply_markup=keyboard if ok else None)
        if ok:
            await query.answer("Looper")
        else:
            await query.answer("Looper unavailable", show_alert=True)

    # Apps menu: Looper plan path from manual text input
    elif data == CB_APPS_LOOPER_PLAN_MANUAL:
        if cb_thread_id is None:
            await query.answer("Use this inside a named topic.", show_alert=True)
            return
        ok, text, keyboard, wid = await _build_looper_panel_payload_for_topic(
            user_id=user.id,
            thread_id=cb_thread_id,
            user_data=context.user_data,
            chat_id=cb_chat_id,
        )
        await safe_edit(query, text, reply_markup=keyboard if ok else None)
        if not ok:
            await query.answer("Looper unavailable", show_alert=True)
            return
        if context.user_data is None:
            await query.answer("State unavailable.", show_alert=True)
            return
        context.user_data[STATE_KEY] = STATE_APPS_LOOPER_PLAN_PATH
        context.user_data[APPS_PENDING_THREAD_KEY] = cb_thread_id
        context.user_data[APPS_PENDING_WINDOW_ID_KEY] = wid
        await query.answer("Send a `.md` plan path in this topic.", show_alert=True)

    # Apps menu: Looper preset plan candidate selection
    elif data.startswith(CB_APPS_LOOPER_PLAN):
        if cb_thread_id is None:
            await query.answer("Use this inside a named topic.", show_alert=True)
            return
        ok, panel_text, panel_keyboard, _wid = await _build_looper_panel_payload_for_topic(
            user_id=user.id,
            thread_id=cb_thread_id,
            user_data=context.user_data,
            chat_id=cb_chat_id,
        )
        if not ok:
            await safe_edit(query, panel_text, reply_markup=None)
            await query.answer("Looper unavailable", show_alert=True)
            return
        if context.user_data is None:
            await query.answer("State unavailable.", show_alert=True)
            return
        raw_idx = data[len(CB_APPS_LOOPER_PLAN) :]
        try:
            idx = int(raw_idx)
        except ValueError:
            await query.answer("Invalid selection.", show_alert=True)
            return
        raw_cfg = context.user_data.get(APPS_LOOPER_CONFIG_KEY)
        if not isinstance(raw_cfg, dict):
            await safe_edit(query, panel_text, reply_markup=panel_keyboard)
            await query.answer("Looper panel expired. Re-open it.", show_alert=True)
            return
        raw_candidates = raw_cfg.get("candidates")
        candidates = list(raw_candidates) if isinstance(raw_candidates, list) else []
        if idx < 0 or idx >= len(candidates):
            await query.answer("Selection out of range.", show_alert=True)
            return
        cfg = dict(raw_cfg)
        cfg["plan_path"] = str(candidates[idx])
        context.user_data[APPS_LOOPER_CONFIG_KEY] = cfg
        context.user_data[STATE_KEY] = ""
        ok, text, keyboard, _wid = await _build_looper_panel_payload_for_topic(
            user_id=user.id,
            thread_id=cb_thread_id,
            user_data=context.user_data,
            chat_id=cb_chat_id,
        )
        await safe_edit(query, text, reply_markup=keyboard if ok else None)
        await query.answer("Plan selected")

    # Apps menu: Looper interval (preset or custom text input)
    elif data.startswith(CB_APPS_LOOPER_INTERVAL):
        if cb_thread_id is None:
            await query.answer("Use this inside a named topic.", show_alert=True)
            return
        ok, panel_text, panel_keyboard, wid = await _build_looper_panel_payload_for_topic(
            user_id=user.id,
            thread_id=cb_thread_id,
            user_data=context.user_data,
            chat_id=cb_chat_id,
        )
        if not ok:
            await safe_edit(query, panel_text, reply_markup=None)
            await query.answer("Looper unavailable", show_alert=True)
            return
        if context.user_data is None:
            await query.answer("State unavailable.", show_alert=True)
            return
        raw_value = data[len(CB_APPS_LOOPER_INTERVAL) :].strip().lower()
        if raw_value == "custom":
            context.user_data[STATE_KEY] = STATE_APPS_LOOPER_INTERVAL
            context.user_data[APPS_PENDING_THREAD_KEY] = cb_thread_id
            context.user_data[APPS_PENDING_WINDOW_ID_KEY] = wid
            await safe_edit(query, panel_text, reply_markup=panel_keyboard)
            await query.answer("Send interval like `10m` or `1h`.", show_alert=True)
            return
        try:
            interval_seconds = int(raw_value)
        except ValueError:
            await query.answer("Invalid interval.", show_alert=True)
            return
        if interval_seconds < LOOPER_MIN_INTERVAL_SECONDS:
            await query.answer(
                (
                    "Interval too short. "
                    f"Minimum is {_format_duration_brief(LOOPER_MIN_INTERVAL_SECONDS)}."
                ),
                show_alert=True,
            )
            return
        if interval_seconds > LOOPER_MAX_INTERVAL_SECONDS:
            await query.answer(
                (
                    "Interval too long. "
                    f"Maximum is {_format_duration_brief(LOOPER_MAX_INTERVAL_SECONDS)}."
                ),
                show_alert=True,
            )
            return
        raw_cfg = context.user_data.get(APPS_LOOPER_CONFIG_KEY)
        cfg = dict(raw_cfg) if isinstance(raw_cfg, dict) else {}
        cfg["interval_seconds"] = interval_seconds
        context.user_data[APPS_LOOPER_CONFIG_KEY] = cfg
        context.user_data[STATE_KEY] = ""
        ok, text, keyboard, _wid = await _build_looper_panel_payload_for_topic(
            user_id=user.id,
            thread_id=cb_thread_id,
            user_data=context.user_data,
            chat_id=cb_chat_id,
        )
        await safe_edit(query, text, reply_markup=keyboard if ok else None)
        await query.answer("Interval updated")

    # Apps menu: Looper limit (preset or custom text input)
    elif data.startswith(CB_APPS_LOOPER_LIMIT):
        if cb_thread_id is None:
            await query.answer("Use this inside a named topic.", show_alert=True)
            return
        ok, panel_text, panel_keyboard, wid = await _build_looper_panel_payload_for_topic(
            user_id=user.id,
            thread_id=cb_thread_id,
            user_data=context.user_data,
            chat_id=cb_chat_id,
        )
        if not ok:
            await safe_edit(query, panel_text, reply_markup=None)
            await query.answer("Looper unavailable", show_alert=True)
            return
        if context.user_data is None:
            await query.answer("State unavailable.", show_alert=True)
            return
        raw_value = data[len(CB_APPS_LOOPER_LIMIT) :].strip().lower()
        if raw_value == "custom":
            context.user_data[STATE_KEY] = STATE_APPS_LOOPER_LIMIT
            context.user_data[APPS_PENDING_THREAD_KEY] = cb_thread_id
            context.user_data[APPS_PENDING_WINDOW_ID_KEY] = wid
            await safe_edit(query, panel_text, reply_markup=panel_keyboard)
            await query.answer("Send limit like `1h`, `2h`, or `none`.", show_alert=True)
            return
        try:
            limit_seconds = int(raw_value)
        except ValueError:
            await query.answer("Invalid limit.", show_alert=True)
            return
        if limit_seconds < 0:
            await query.answer("Limit must be zero or positive.", show_alert=True)
            return
        raw_cfg = context.user_data.get(APPS_LOOPER_CONFIG_KEY)
        cfg = dict(raw_cfg) if isinstance(raw_cfg, dict) else {}
        cfg["limit_seconds"] = limit_seconds
        context.user_data[APPS_LOOPER_CONFIG_KEY] = cfg
        context.user_data[STATE_KEY] = ""
        ok, text, keyboard, _wid = await _build_looper_panel_payload_for_topic(
            user_id=user.id,
            thread_id=cb_thread_id,
            user_data=context.user_data,
            chat_id=cb_chat_id,
        )
        await safe_edit(query, text, reply_markup=keyboard if ok else None)
        await query.answer("Limit updated")

    # Apps menu: Looper keyword prompt
    elif data == CB_APPS_LOOPER_KEYWORD:
        if cb_thread_id is None:
            await query.answer("Use this inside a named topic.", show_alert=True)
            return
        ok, text, keyboard, wid = await _build_looper_panel_payload_for_topic(
            user_id=user.id,
            thread_id=cb_thread_id,
            user_data=context.user_data,
            chat_id=cb_chat_id,
        )
        await safe_edit(query, text, reply_markup=keyboard if ok else None)
        if not ok:
            await query.answer("Looper unavailable", show_alert=True)
            return
        if context.user_data is None:
            await query.answer("State unavailable.", show_alert=True)
            return
        context.user_data[STATE_KEY] = STATE_APPS_LOOPER_KEYWORD
        context.user_data[APPS_PENDING_THREAD_KEY] = cb_thread_id
        context.user_data[APPS_PENDING_WINDOW_ID_KEY] = wid
        await query.answer("Send a single-word completion keyword.", show_alert=True)

    # Apps menu: Looper custom instructions prompt
    elif data == CB_APPS_LOOPER_INSTRUCTIONS:
        if cb_thread_id is None:
            await query.answer("Use this inside a named topic.", show_alert=True)
            return
        ok, text, keyboard, wid = await _build_looper_panel_payload_for_topic(
            user_id=user.id,
            thread_id=cb_thread_id,
            user_data=context.user_data,
            chat_id=cb_chat_id,
        )
        await safe_edit(query, text, reply_markup=keyboard if ok else None)
        if not ok:
            await query.answer("Looper unavailable", show_alert=True)
            return
        if context.user_data is None:
            await query.answer("State unavailable.", show_alert=True)
            return
        context.user_data[STATE_KEY] = STATE_APPS_LOOPER_INSTRUCTIONS
        context.user_data[APPS_PENDING_THREAD_KEY] = cb_thread_id
        context.user_data[APPS_PENDING_WINDOW_ID_KEY] = wid
        await query.answer("Send optional custom instructions, or `-` to clear.", show_alert=True)

    # Apps menu: Looper start
    elif data == CB_APPS_LOOPER_START:
        if cb_thread_id is None:
            await query.answer("Use this inside a named topic.", show_alert=True)
            return
        wid = session_manager.resolve_window_for_thread(user.id, cb_thread_id, chat_id=cb_chat_id)
        if not wid:
            await query.answer("No session bound to this topic.", show_alert=True)
            return
        workspace_dir = _resolve_workspace_dir_for_window(
            user_id=user.id,
            thread_id=cb_thread_id,
            window_id=wid,
        )
        if not workspace_dir:
            await query.answer(
                "Session binding is incomplete. Send a normal message to reinitialize.",
                show_alert=True,
            )
            return
        ok, panel_text, panel_keyboard, _wid = await _build_looper_panel_payload_for_topic(
            user_id=user.id,
            thread_id=cb_thread_id,
            user_data=context.user_data,
            chat_id=cb_chat_id,
        )
        if not ok:
            await safe_edit(query, panel_text, reply_markup=None)
            await query.answer("Looper unavailable", show_alert=True)
            return
        if context.user_data is None:
            await query.answer("State unavailable.", show_alert=True)
            return
        raw_cfg = context.user_data.get(APPS_LOOPER_CONFIG_KEY)
        cfg = dict(raw_cfg) if isinstance(raw_cfg, dict) else {}
        plan_path = str(cfg.get("plan_path", "")).strip()
        keyword = normalize_looper_keyword(str(cfg.get("keyword", "")))
        instructions = str(cfg.get("instructions", "")).strip()
        try:
            interval_seconds = int(cfg.get("interval_seconds", LOOPER_DEFAULT_INTERVAL_SECONDS))
            limit_seconds = int(cfg.get("limit_seconds", 0))
        except (TypeError, ValueError):
            await safe_edit(query, panel_text, reply_markup=panel_keyboard)
            await query.answer("Invalid Looper config. Re-open panel.", show_alert=True)
            return

        if not plan_path:
            await query.answer("Set a plan file first.", show_alert=True)
            return
        if not plan_path.lower().endswith(".md"):
            await query.answer("Plan file must end with `.md`.", show_alert=True)
            return
        if not keyword or " " in keyword:
            await query.answer("Keyword must be one word.", show_alert=True)
            return
        if interval_seconds < LOOPER_MIN_INTERVAL_SECONDS:
            await query.answer(
                (
                    "Interval too short. "
                    f"Minimum is {_format_duration_brief(LOOPER_MIN_INTERVAL_SECONDS)}."
                ),
                show_alert=True,
            )
            return
        if interval_seconds > LOOPER_MAX_INTERVAL_SECONDS:
            await query.answer(
                (
                    "Interval too long. "
                    f"Maximum is {_format_duration_brief(LOOPER_MAX_INTERVAL_SECONDS)}."
                ),
                show_alert=True,
            )
            return
        if limit_seconds < 0:
            limit_seconds = 0

        try:
            state = start_looper(
                user_id=user.id,
                thread_id=cb_thread_id,
                window_id=wid,
                plan_path=plan_path,
                keyword=keyword,
                interval_seconds=interval_seconds,
                limit_seconds=limit_seconds,
                instructions=instructions,
            )
        except ValueError as e:
            await safe_edit(query, panel_text, reply_markup=panel_keyboard)
            await query.answer(str(e), show_alert=True)
            return

        # Auto-enable looper app once when present in local app catalog.
        auto_enabled = False
        app_catalog = session_manager.discover_skill_catalog()
        if "looper" in app_catalog:
            enabled = [
                item.name
                for item in session_manager.resolve_thread_skills(
                    user.id,
                    cb_thread_id,
                    chat_id=cb_chat_id,
                    catalog=app_catalog,
                )
            ]
            if "looper" not in enabled:
                session_manager.set_thread_skills(
                    user.id,
                    cb_thread_id,
                    [*enabled, "looper"],
                    chat_id=cb_chat_id,
                )
                auto_enabled = True

        context.user_data[APPS_LOOPER_CONFIG_KEY] = {
            **cfg,
            "plan_path": state.plan_path,
            "keyword": state.keyword,
            "instructions": state.instructions,
            "interval_seconds": state.interval_seconds,
            "limit_seconds": (
                int(state.deadline_at - state.started_at)
                if state.deadline_at > state.started_at
                else 0
            ),
        }
        context.user_data[STATE_KEY] = ""
        context.user_data[APPS_PENDING_THREAD_KEY] = cb_thread_id
        context.user_data[APPS_PENDING_WINDOW_ID_KEY] = wid
        ok, text, keyboard, _wid = await _build_looper_panel_payload_for_topic(
            user_id=user.id,
            thread_id=cb_thread_id,
            user_data=context.user_data,
            chat_id=cb_chat_id,
        )
        await safe_edit(query, text, reply_markup=keyboard if ok else None)
        await query.answer(
            "Looper started and app enabled" if auto_enabled else "Looper started"
        )

    # Apps menu: Looper stop
    elif data == CB_APPS_LOOPER_STOP:
        if cb_thread_id is None:
            await query.answer("Use this inside a named topic.", show_alert=True)
            return
        stopped = stop_looper(user_id=user.id, thread_id=cb_thread_id, reason="manual_stop")
        if context.user_data is not None:
            context.user_data[STATE_KEY] = ""
        ok, text, keyboard, _wid = await _build_looper_panel_payload_for_topic(
            user_id=user.id,
            thread_id=cb_thread_id,
            user_data=context.user_data,
            chat_id=cb_chat_id,
        )
        await safe_edit(query, text, reply_markup=keyboard if ok else None)
        await query.answer("Looper stopped" if stopped else "Looper already stopped")

    # Allowed users menu: back/refresh
    elif data in {CB_ALLOWED_BACK, CB_ALLOWED_REFRESH}:
        _clear_allowed_flow_state(context.user_data)
        await safe_edit(
            query,
            _build_allowed_overview_text(user.id),
            reply_markup=_build_allowed_overview_keyboard(user.id),
        )
        await query.answer("Refreshed")

    # Allowed users menu: open member picker for batch add.
    elif data == CB_ALLOWED_ADD:
        if not _is_admin_user(user.id):
            await query.answer("Only admins can request allowlist changes.", show_alert=True)
            return
        if cb_chat_id is None:
            await query.answer("Use this in a group topic.", show_alert=True)
            return
        bot_for_admins = getattr(context, "bot", None)
        if bot_for_admins is not None:
            await _remember_group_admins_from_api(bot_for_admins, cb_chat_id)
        wid: str | None = None
        if cb_thread_id is not None:
            wid = session_manager.resolve_window_for_thread(
                user.id,
                cb_thread_id,
                chat_id=cb_chat_id,
            )
        if context.user_data is not None:
            _clear_allowed_flow_state(context.user_data)
            context.user_data[STATE_KEY] = STATE_ALLOWED_PICK_USERS
            context.user_data[ALLOWED_PICK_CHAT_KEY] = cb_chat_id
            context.user_data[ALLOWED_PICK_PAGE_KEY] = 0
            context.user_data[ALLOWED_PICK_SELECTED_IDS_KEY] = []
            context.user_data[ALLOWED_PICK_THREAD_KEY] = cb_thread_id
            context.user_data[ALLOWED_PICK_WINDOW_KEY] = wid
        text, entries, page, page_count = _build_allowed_picker_text(
            chat_id=cb_chat_id,
            page=0,
            selected_ids=set(),
        )
        await safe_edit(
            query,
            text,
            reply_markup=_build_allowed_picker_keyboard(
                entries=entries,
                page=page,
                page_count=page_count,
                selected_ids=set(),
            ),
        )
        await query.answer()

    # Allowed users menu: picker page switch.
    elif data.startswith(CB_ALLOWED_PICK_PAGE):
        if not _is_admin_user(user.id):
            await query.answer("Only admins can request allowlist changes.", show_alert=True)
            return
        if not context.user_data:
            await query.answer("Picker expired.", show_alert=True)
            return
        if context.user_data.get(STATE_KEY) not in {STATE_ALLOWED_PICK_USERS, STATE_ALLOWED_PICK_ROLE}:
            await query.answer("Picker not active.", show_alert=True)
            return
        try:
            target_page = int(data[len(CB_ALLOWED_PICK_PAGE) :])
        except ValueError:
            await query.answer("Invalid page.", show_alert=True)
            return
        chat_id = context.user_data.get(ALLOWED_PICK_CHAT_KEY)
        if not isinstance(chat_id, int):
            await query.answer("Picker expired.", show_alert=True)
            return
        selected_ids = _allowed_pick_selected_ids(context.user_data)
        text, entries, page, page_count = _build_allowed_picker_text(
            chat_id=chat_id,
            page=target_page,
            selected_ids=selected_ids,
        )
        context.user_data[ALLOWED_PICK_PAGE_KEY] = page
        context.user_data[STATE_KEY] = STATE_ALLOWED_PICK_USERS
        await safe_edit(
            query,
            text,
            reply_markup=_build_allowed_picker_keyboard(
                entries=entries,
                page=page,
                page_count=page_count,
                selected_ids=selected_ids,
            ),
        )
        await query.answer()

    # Allowed users menu: picker toggle one member.
    elif data.startswith(CB_ALLOWED_PICK_TOGGLE):
        if not _is_admin_user(user.id):
            await query.answer("Only admins can request allowlist changes.", show_alert=True)
            return
        if not context.user_data:
            await query.answer("Picker expired.", show_alert=True)
            return
        if context.user_data.get(STATE_KEY) not in {STATE_ALLOWED_PICK_USERS, STATE_ALLOWED_PICK_ROLE}:
            await query.answer("Picker not active.", show_alert=True)
            return
        chat_id = context.user_data.get(ALLOWED_PICK_CHAT_KEY)
        if not isinstance(chat_id, int):
            await query.answer("Picker expired.", show_alert=True)
            return
        try:
            uid = int(data[len(CB_ALLOWED_PICK_TOGGLE) :])
        except ValueError:
            await query.answer("Invalid member.", show_alert=True)
            return
        selected_ids = _allowed_pick_selected_ids(context.user_data)
        if uid in selected_ids:
            selected_ids.remove(uid)
        else:
            selected_ids.add(uid)
        context.user_data[ALLOWED_PICK_SELECTED_IDS_KEY] = sorted(selected_ids)
        page = int(context.user_data.get(ALLOWED_PICK_PAGE_KEY, 0) or 0)
        text, entries, page, page_count = _build_allowed_picker_text(
            chat_id=chat_id,
            page=page,
            selected_ids=selected_ids,
        )
        context.user_data[ALLOWED_PICK_PAGE_KEY] = page
        context.user_data[STATE_KEY] = STATE_ALLOWED_PICK_USERS
        await safe_edit(
            query,
            text,
            reply_markup=_build_allowed_picker_keyboard(
                entries=entries,
                page=page,
                page_count=page_count,
                selected_ids=selected_ids,
            ),
        )
        await query.answer()

    # Allowed users menu: picker clear selection.
    elif data == CB_ALLOWED_PICK_CLEAR:
        if not _is_admin_user(user.id):
            await query.answer("Only admins can request allowlist changes.", show_alert=True)
            return
        if not context.user_data:
            await query.answer("Picker expired.", show_alert=True)
            return
        chat_id = context.user_data.get(ALLOWED_PICK_CHAT_KEY)
        if not isinstance(chat_id, int):
            await query.answer("Picker expired.", show_alert=True)
            return
        context.user_data[ALLOWED_PICK_SELECTED_IDS_KEY] = []
        page = int(context.user_data.get(ALLOWED_PICK_PAGE_KEY, 0) or 0)
        text, entries, page, page_count = _build_allowed_picker_text(
            chat_id=chat_id,
            page=page,
            selected_ids=set(),
        )
        context.user_data[ALLOWED_PICK_PAGE_KEY] = page
        context.user_data[STATE_KEY] = STATE_ALLOWED_PICK_USERS
        await safe_edit(
            query,
            text,
            reply_markup=_build_allowed_picker_keyboard(
                entries=entries,
                page=page,
                page_count=page_count,
                selected_ids=set(),
            ),
        )
        await query.answer("Selection cleared")

    # Allowed users menu: picker next -> role selection.
    elif data == CB_ALLOWED_PICK_NEXT:
        if not _is_admin_user(user.id):
            await query.answer("Only admins can request allowlist changes.", show_alert=True)
            return
        if not context.user_data:
            await query.answer("Picker expired.", show_alert=True)
            return
        selected_ids = _allowed_pick_selected_ids(context.user_data)
        if not selected_ids:
            await query.answer("Select at least one user.", show_alert=True)
            return
        context.user_data[STATE_KEY] = STATE_ALLOWED_PICK_ROLE
        await safe_edit(
            query,
            "🛂 *Select Role*\n\n"
            f"Selected users: `{len(selected_ids)}`\n\n"
            "Choose one role for all selected users.",
            reply_markup=_build_allowed_add_mode_keyboard(),
        )
        await query.answer()

    # Allowed users menu: finalize batch add with chosen role.
    elif data in {CB_ALLOWED_ADD_SINGLE, CB_ALLOWED_ADD_CREATE}:
        if not _is_admin_user(user.id):
            await query.answer("Only admins can request allowlist changes.", show_alert=True)
            return
        if not context.user_data:
            await query.answer("Picker expired.", show_alert=True)
            return
        if context.user_data.get(STATE_KEY) != STATE_ALLOWED_PICK_ROLE:
            await query.answer("Select users first.", show_alert=True)
            return
        chat_id = context.user_data.get(ALLOWED_PICK_CHAT_KEY)
        if not isinstance(chat_id, int):
            await query.answer("Picker expired.", show_alert=True)
            return
        selected_ids = _allowed_pick_selected_ids(context.user_data)
        if not selected_ids:
            await query.answer("Select at least one user.", show_alert=True)
            return
        scope = SCOPE_SINGLE_SESSION if data == CB_ALLOWED_ADD_SINGLE else SCOPE_CREATE_SESSIONS
        bind_thread_id = context.user_data.get(ALLOWED_PICK_THREAD_KEY)
        bind_window_id = context.user_data.get(ALLOWED_PICK_WINDOW_KEY)
        if scope == SCOPE_SINGLE_SESSION and (
            not isinstance(bind_thread_id, int)
            or not isinstance(bind_window_id, str)
            or not bind_window_id
        ):
            await query.answer(
                "Single-session role needs a bound topic/session.",
                show_alert=True,
            )
            return

        candidate_names = dict(_group_member_candidates(chat_id))
        targets = [
            _PendingAllowedAddTarget(
                user_id=uid,
                name=candidate_names.get(uid, ""),
                scope=scope,
                bind_thread_id=bind_thread_id if scope == SCOPE_SINGLE_SESSION else None,
                bind_window_id=bind_window_id if scope == SCOPE_SINGLE_SESSION else None,
                bind_chat_id=chat_id if scope == SCOPE_SINGLE_SESSION else None,
            )
            for uid in sorted(selected_ids)
        ]
        ok, err, request = _queue_allowed_add_batch_request(
            requested_by=user.id,
            targets=targets,
        )
        if not ok or request is None:
            await query.answer(err or "Failed to queue request.", show_alert=True)
            return

        delivered, total = await _notify_allowed_auth_token(
            bot=context.bot,
            request=request,
        )
        _clear_allowed_flow_state(context.user_data)
        await safe_edit(
            query,
            _build_allowed_overview_text(user.id),
            reply_markup=_build_allowed_overview_keyboard(user.id),
        )
        await query.answer(
            f"Batch request queued for {len(targets)} user(s). Token sent to {delivered}/{total} admins.",
            show_alert=True,
        )

    # Allowed users menu: remove picker.
    elif data == CB_ALLOWED_REMOVE_MENU:
        if not _is_admin_user(user.id):
            await query.answer("Only admins can request allowlist changes.", show_alert=True)
            return
        _clear_allowed_flow_state(context.user_data)
        await safe_edit(
            query,
            _build_allowed_remove_text(user.id),
            reply_markup=_build_allowed_remove_keyboard(user.id),
        )
        await query.answer()

    # Allowed users menu: remove request for specific user.
    elif data.startswith(CB_ALLOWED_REMOVE):
        if not _is_admin_user(user.id):
            await query.answer("Only admins can request allowlist changes.", show_alert=True)
            return
        raw_user_id = data[len(CB_ALLOWED_REMOVE) :]
        try:
            target_user_id = int(raw_user_id)
        except ValueError:
            await query.answer("Invalid user ID", show_alert=True)
            return

        ok, err, request = _queue_allowed_remove_request(
            requested_by=user.id,
            target_user_id=target_user_id,
        )
        if not ok or request is None:
            await query.answer(err or "Failed to queue request.", show_alert=True)
            return

        delivered, total = await _notify_allowed_auth_token(
            bot=context.bot,
            request=request,
        )
        await safe_edit(
            query,
            _build_allowed_remove_text(user.id),
            reply_markup=_build_allowed_remove_keyboard(user.id),
        )
        await query.answer(
            f"Approval token sent to {delivered}/{total} admins.",
            show_alert=True,
        )

    # Worktree panel: refresh
    elif data == CB_WORKTREE_REFRESH:
        if cb_thread_id is None:
            await query.answer("Use this inside a named topic.", show_alert=True)
            return
        wid = session_manager.resolve_window_for_thread(user.id, cb_thread_id, chat_id=cb_chat_id)
        if not wid:
            await query.answer("No session bound to this topic.", show_alert=True)
            return
        workspace_dir, workspace_err = await _resolve_live_workspace_dir_for_window(
            user_id=user.id,
            thread_id=cb_thread_id,
            window_id=wid,
        )
        if not workspace_dir:
            await query.answer(
                workspace_err or "No workspace bound to this topic.",
                show_alert=True,
            )
            return
        repo_root, repo_err = _git_repo_root(workspace_dir)
        if not repo_root:
            await query.answer(repo_err or "Not a git repository.", show_alert=True)
            return
        branch, _branch_err = _git_current_branch(workspace_dir)
        entries, err = _git_worktree_list(repo_root)
        if err:
            await query.answer(err, show_alert=True)
            return
        text = _build_worktree_panel_text(
            repo_root=repo_root,
            current_path=str(Path(workspace_dir).resolve()),
            current_branch=branch or "(unknown)",
            entries=entries,
        )
        await safe_edit(query, text, reply_markup=_build_worktree_panel_keyboard())
        await query.answer("Refreshed")

    # Worktree panel: open interactive fold selector
    elif data == CB_WORKTREE_FOLD_MENU:
        if not _can_user_create_sessions(user.id):
            await query.answer(
                "You do not have permission to fold worktrees.",
                show_alert=True,
            )
            return
        if cb_thread_id is None:
            await query.answer("Use this inside a named topic.", show_alert=True)
            return
        wid = session_manager.resolve_window_for_thread(user.id, cb_thread_id, chat_id=cb_chat_id)
        if not wid:
            await query.answer("No session bound to this topic.", show_alert=True)
            return
        workspace_dir, workspace_err = await _resolve_live_workspace_dir_for_window(
            user_id=user.id,
            thread_id=cb_thread_id,
            window_id=wid,
        )
        if not workspace_dir:
            await query.answer(
                workspace_err or "No workspace bound to this topic.",
                show_alert=True,
            )
            return

        cwd_path = Path(workspace_dir)
        if not _is_primary_worktree(cwd_path):
            await query.answer(
                "Fold can only run from the primary repository worktree.",
                show_alert=True,
            )
            return

        repo_root, repo_err = _git_repo_root(workspace_dir)
        if not repo_root:
            await query.answer(repo_err or "Not a git repository.", show_alert=True)
            return
        branch, branch_err = _git_current_branch(workspace_dir)
        if not branch:
            await query.answer(branch_err or "Failed to resolve branch.", show_alert=True)
            return
        entries, err = _git_worktree_list(repo_root)
        if err:
            await query.answer(err, show_alert=True)
            return
        candidates = _build_worktree_fold_candidates(
            entries=entries,
            current_path=str(cwd_path.resolve()),
        )
        selected: set[int] = set()
        if context.user_data is not None:
            _clear_worktree_flow_state(context.user_data)
            context.user_data[STATE_KEY] = STATE_WORKTREE_FOLD_SELECT
            context.user_data[WORKTREE_PENDING_THREAD_KEY] = cb_thread_id
            context.user_data[WORKTREE_PENDING_WINDOW_ID_KEY] = wid
            context.user_data[WORKTREE_FOLD_CANDIDATES_KEY] = candidates
            context.user_data[WORKTREE_FOLD_SELECTED_KEY] = []
        await safe_edit(
            query,
            _build_worktree_fold_text(
                target_branch=branch,
                candidates=candidates,
                selected_indices=selected,
            ),
            reply_markup=_build_worktree_fold_keyboard(
                candidates=candidates,
                selected_indices=selected,
            ),
        )
        await query.answer("Select worktrees to fold")

    # Worktree fold picker: toggle selection
    elif data.startswith(CB_WORKTREE_FOLD_TOGGLE):
        if not _can_user_create_sessions(user.id):
            await query.answer(
                "You do not have permission to fold worktrees.",
                show_alert=True,
            )
            return
        if cb_thread_id is None:
            await query.answer("Use this inside a named topic.", show_alert=True)
            return
        if not context.user_data:
            await query.answer("Fold picker expired.", show_alert=True)
            return
        if context.user_data.get(STATE_KEY) != STATE_WORKTREE_FOLD_SELECT:
            await query.answer("Fold picker not active.", show_alert=True)
            return
        pending_tid = context.user_data.get(WORKTREE_PENDING_THREAD_KEY)
        if pending_tid != cb_thread_id:
            await query.answer("Stale fold picker.", show_alert=True)
            return
        raw_candidates = context.user_data.get(WORKTREE_FOLD_CANDIDATES_KEY)
        if not isinstance(raw_candidates, list):
            await query.answer("Fold picker expired.", show_alert=True)
            return
        candidates = [item for item in raw_candidates if isinstance(item, dict)]
        raw_selected = context.user_data.get(WORKTREE_FOLD_SELECTED_KEY, [])
        selected = {
            int(item)
            for item in raw_selected
            if isinstance(item, int) or (isinstance(item, str) and item.isdigit())
        }
        try:
            idx = int(data[len(CB_WORKTREE_FOLD_TOGGLE) :])
        except ValueError:
            await query.answer("Invalid selection.", show_alert=True)
            return
        if idx < 0 or idx >= len(candidates):
            await query.answer("Selection out of range.", show_alert=True)
            return
        if idx in selected:
            selected.remove(idx)
        else:
            selected.add(idx)

        wid = session_manager.resolve_window_for_thread(user.id, cb_thread_id, chat_id=cb_chat_id)
        if not wid:
            await query.answer("No session bound to this topic.", show_alert=True)
            return
        workspace_dir, workspace_err = await _resolve_live_workspace_dir_for_window(
            user_id=user.id,
            thread_id=cb_thread_id,
            window_id=wid,
        )
        if not workspace_dir:
            await query.answer(
                workspace_err or "No workspace bound to this topic.",
                show_alert=True,
            )
            return
        branch, _branch_err = _git_current_branch(workspace_dir)
        branch_name = branch or "(unknown)"

        context.user_data[WORKTREE_FOLD_SELECTED_KEY] = sorted(selected)
        await safe_edit(
            query,
            _build_worktree_fold_text(
                target_branch=branch_name,
                candidates=candidates,
                selected_indices=selected,
            ),
            reply_markup=_build_worktree_fold_keyboard(
                candidates=candidates,
                selected_indices=selected,
            ),
        )
        await query.answer()

    # Worktree fold picker: run fold
    elif data == CB_WORKTREE_FOLD_RUN:
        if not _can_user_create_sessions(user.id):
            await query.answer(
                "You do not have permission to fold worktrees.",
                show_alert=True,
            )
            return
        if cb_thread_id is None:
            await query.answer("Use this inside a named topic.", show_alert=True)
            return
        if not context.user_data:
            await query.answer("Fold picker expired.", show_alert=True)
            return
        if context.user_data.get(STATE_KEY) != STATE_WORKTREE_FOLD_SELECT:
            await query.answer("Fold picker not active.", show_alert=True)
            return
        pending_tid = context.user_data.get(WORKTREE_PENDING_THREAD_KEY)
        if pending_tid != cb_thread_id:
            await query.answer("Stale fold picker.", show_alert=True)
            return
        raw_candidates = context.user_data.get(WORKTREE_FOLD_CANDIDATES_KEY)
        raw_selected = context.user_data.get(WORKTREE_FOLD_SELECTED_KEY, [])
        if not isinstance(raw_candidates, list):
            await query.answer("Fold picker expired.", show_alert=True)
            return
        candidates = [item for item in raw_candidates if isinstance(item, dict)]
        selected = {
            int(item)
            for item in raw_selected
            if isinstance(item, int) or (isinstance(item, str) and item.isdigit())
        }
        if not selected:
            await query.answer("Select at least one worktree.", show_alert=True)
            return
        selectors = []
        for idx in sorted(selected):
            if idx < 0 or idx >= len(candidates):
                continue
            selector = str(candidates[idx].get("path", "")).strip()
            if selector:
                selectors.append(selector)
        if not selectors:
            await query.answer("No valid worktrees selected.", show_alert=True)
            return

        wid = session_manager.resolve_window_for_thread(user.id, cb_thread_id, chat_id=cb_chat_id)
        if not wid:
            await query.answer("No session bound to this topic.", show_alert=True)
            return
        workspace_dir, workspace_err = await _resolve_live_workspace_dir_for_window(
            user_id=user.id,
            thread_id=cb_thread_id,
            window_id=wid,
        )
        if not workspace_dir:
            await query.answer(
                workspace_err or "No workspace bound to this topic.",
                show_alert=True,
            )
            return

        ok, msg = await asyncio.to_thread(
            _fold_worktrees_into_branch,
            target_cwd=Path(workspace_dir),
            selectors=selectors,
        )
        _clear_worktree_flow_state(context.user_data)
        if ok:
            await safe_edit(
                query,
                f"✅ {msg}",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Back", callback_data=CB_WORKTREE_REFRESH)]]
                ),
            )
            await query.answer("Fold complete")
        else:
            await safe_edit(
                query,
                f"❌ {msg}",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Back", callback_data=CB_WORKTREE_REFRESH)]]
                ),
            )
            await query.answer("Fold failed")

    # Worktree fold picker: go back to panel
    elif data == CB_WORKTREE_FOLD_BACK:
        _clear_worktree_flow_state(context.user_data)
        if cb_thread_id is None:
            await query.answer("Use this inside a named topic.", show_alert=True)
            return
        wid = session_manager.resolve_window_for_thread(user.id, cb_thread_id, chat_id=cb_chat_id)
        if not wid:
            await query.answer("No session bound to this topic.", show_alert=True)
            return
        workspace_dir, workspace_err = await _resolve_live_workspace_dir_for_window(
            user_id=user.id,
            thread_id=cb_thread_id,
            window_id=wid,
        )
        if not workspace_dir:
            await query.answer(
                workspace_err or "No workspace bound to this topic.",
                show_alert=True,
            )
            return
        repo_root, repo_err = _git_repo_root(workspace_dir)
        if not repo_root:
            await query.answer(repo_err or "Not a git repository.", show_alert=True)
            return
        branch, _branch_err = _git_current_branch(workspace_dir)
        entries, err = _git_worktree_list(repo_root)
        if err:
            await query.answer(err, show_alert=True)
            return
        text = _build_worktree_panel_text(
            repo_root=repo_root,
            current_path=str(Path(workspace_dir).resolve()),
            current_branch=branch or "(unknown)",
            entries=entries,
        )
        await safe_edit(query, text, reply_markup=_build_worktree_panel_keyboard())
        await query.answer()

    # Worktree panel: prompt for a new worktree name
    elif data == CB_WORKTREE_NEW:
        if not _can_user_create_sessions(user.id):
            await query.answer(
                "You do not have permission to create worktrees/sessions.",
                show_alert=True,
            )
            return
        if cb_thread_id is None:
            await query.answer("Use this inside a named topic.", show_alert=True)
            return
        wid = session_manager.resolve_window_for_thread(user.id, cb_thread_id, chat_id=cb_chat_id)
        if not wid:
            await query.answer("No session bound to this topic.", show_alert=True)
            return
        if context.user_data is not None:
            _clear_worktree_flow_state(context.user_data)
            context.user_data[STATE_KEY] = STATE_WORKTREE_NEW_NAME
            context.user_data[WORKTREE_PENDING_THREAD_KEY] = cb_thread_id
            context.user_data[WORKTREE_PENDING_WINDOW_ID_KEY] = wid
        await safe_edit(
            query,
            "🌱 *New Worktree*\n\n"
            "Send the new worktree name in this topic.\n"
            "Example: `auth-fix`",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Back", callback_data=CB_WORKTREE_REFRESH)]]
            ),
        )
        await query.answer("Waiting for name")

    elif data == "noop":
        await query.answer()

# --- Streaming response / notifications ---


async def _update_user_read_offset_for_window(
    *,
    user_id: int,
    window_id: str,
    file_path: str | None = None,
) -> None:
    """Advance per-user read offset to current end-of-file for a session."""
    resolved_file_path: Path | None = Path(file_path) if file_path else None
    if resolved_file_path is None:
        session = await session_manager.resolve_session_for_window(window_id)
        if session and session.file_path:
            resolved_file_path = Path(session.file_path)

    if resolved_file_path is None:
        return

    try:
        file_size = resolved_file_path.stat().st_size
    except OSError:
        return

    session_manager.update_user_window_offset(user_id, window_id, file_size)


def _extract_app_server_response_item_text(item: dict[str, object]) -> str:
    """Extract assistant text from app-server response item payloads."""
    # Newer app-server payloads may deliver final assistant text directly as
    # `item.text` (for item/completed type=agentMessage).
    text_field = item.get("text")
    if isinstance(text_field, str):
        text_field = text_field.strip()
        if text_field:
            return text_field

    # Backward-compatible path for rawResponseItem/completed message content.
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            text = block.strip()
            if text:
                parts.append(text)
            continue
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type not in {"output_text", "text", "input_text"}:
            continue
        text_val = block.get("text")
        if isinstance(text_val, str):
            text_val = text_val.strip()
            if text_val:
                parts.append(text_val)
    return "\n".join(parts).strip()


def _is_transient_app_server_turn_error(message: str, details: str = "") -> bool:
    """Return whether an app-server error message looks like a transient run failure."""
    text = " ".join(part.strip() for part in (message, details) if part.strip()).lower()
    if not text:
        return False
    return (
        "stream disconnected before completion" in text
        and "an error occurred while processing your request" in text
    )


async def _retry_failed_turn_after_transient_app_server_error(
    *,
    bot: Bot,
    codex_thread_id: str,
    status: str,
    error_message: str,
) -> set[tuple[int, int | None]]:
    """Retry one failed turn from watchdog state after a transient app-server error."""
    suppressed_topics: set[tuple[int, int | None]] = set()
    retry_attempted = False

    for (
        user_id,
        bound_chat_id,
        wid,
        bound_thread_id,
    ) in session_manager.find_users_for_codex_thread(codex_thread_id):
        candidate = get_immediate_auto_retry_candidate(
            user_id=user_id,
            thread_id=bound_thread_id,
            window_id=wid,
        )
        if candidate is None:
            emit_telemetry(
                "transport.app_server.turn_failed_auto_retry_skipped",
                codex_thread_id=codex_thread_id,
                window_id=wid,
                thread_id=bound_thread_id,
                user_id=user_id,
                status=status,
                reason="no_pending_state",
                error=error_message,
            )
            continue
        if not candidate.auto_retry_allowed:
            emit_telemetry(
                "transport.app_server.turn_failed_auto_retry_skipped",
                codex_thread_id=codex_thread_id,
                window_id=wid,
                thread_id=bound_thread_id,
                user_id=user_id,
                status=status,
                reason=candidate.auto_retry_reason,
                retry_count=candidate.retry_count,
                retry_limit=candidate.max_auto_retries,
                error=error_message,
            )
            continue
        if retry_attempted:
            emit_telemetry(
                "transport.app_server.turn_failed_auto_retry_skipped",
                codex_thread_id=codex_thread_id,
                window_id=wid,
                thread_id=bound_thread_id,
                user_id=user_id,
                status=status,
                reason="already_retried_for_thread",
                retry_count=candidate.retry_count,
                retry_limit=candidate.max_auto_retries,
                error=error_message,
            )
            continue

        retry_count, retry_limit = note_auto_retry_attempt(
            user_id=user_id,
            thread_id=bound_thread_id,
            window_id=wid,
        )
        await enqueue_progress_clear(
            bot,
            user_id,
            thread_id=bound_thread_id,
        )
        send_ok, send_msg = await session_manager.send_topic_text_to_window(
            user_id=user_id,
            thread_id=bound_thread_id,
            chat_id=bound_chat_id,
            window_id=wid,
            text=candidate.resend_text,
        )
        note_auto_retry_result(
            user_id=user_id,
            thread_id=bound_thread_id,
            window_id=wid,
            send_success=send_ok,
        )
        emit_telemetry(
            "transport.app_server.turn_failed_auto_retry",
            codex_thread_id=codex_thread_id,
            window_id=wid,
            thread_id=bound_thread_id,
            user_id=user_id,
            status=status,
            send_success=send_ok,
            retry_count=retry_count,
            retry_limit=retry_limit,
            resend_text_len=candidate.resend_text_len,
            error=error_message,
        )

        resolved_chat_id = session_manager.resolve_chat_id(
            user_id,
            bound_thread_id,
            chat_id=bound_chat_id,
        )
        if send_ok:
            await enqueue_progress_start(
                bot,
                user_id,
                window_id=wid,
                thread_id=bound_thread_id,
            )
            await safe_send(
                bot,
                resolved_chat_id,
                (
                    "↻ Retrying last message after transient Codex stream failure "
                    f"({retry_count}/{retry_limit})."
                ),
                message_thread_id=bound_thread_id,
            )
        else:
            await safe_send(
                bot,
                resolved_chat_id,
                (
                    "⚠️ Automatic retry after transient Codex stream failure failed "
                    f"({retry_count}/{retry_limit}).\nReason: {send_msg}"
                ),
                message_thread_id=bound_thread_id,
            )
        retry_attempted = True
        suppressed_topics.add((user_id, bound_thread_id))

    return suppressed_topics


async def _handle_codex_app_server_request(
    method: str,
    params: dict[str, object],
    *,
    bot: Bot,
) -> dict[str, object] | None:
    """Handle app-server request callbacks (approvals, request_user_input)."""
    thread_id = params.get("threadId")
    bindings = (
        session_manager.find_users_for_codex_thread(thread_id)
        if isinstance(thread_id, str)
        else []
    )

    if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
        emit_telemetry(
            "approval.request.received",
            method=method,
            codex_thread_id=thread_id if isinstance(thread_id, str) else "",
            binding_count=len(bindings),
        )
        if not bindings:
            logger.warning("App-server approval request has no bound user/thread")
            emit_telemetry(
                "approval.request.finalized",
                method=method,
                codex_thread_id=thread_id if isinstance(thread_id, str) else "",
                decision=APP_SERVER_APPROVAL_DECISION_DECLINE,
                reason="no_bindings",
            )
            return {"decision": APP_SERVER_APPROVAL_DECISION_DECLINE}

        _uid, _chat_id, window_id, _tid = bindings[0]
        mode = _get_window_approval_mode(window_id)
        if _mode_auto_approves_app_server_requests(mode):
            emit_telemetry(
                "approval.request.finalized",
                method=method,
                codex_thread_id=thread_id if isinstance(thread_id, str) else "",
                window_id=window_id,
                mode=mode,
                decision=APP_SERVER_APPROVAL_DECISION_ACCEPT_SESSION,
                reason="mode_auto_accept",
            )
            return {"decision": APP_SERVER_APPROVAL_DECISION_ACCEPT_SESSION}

        all_targets: list[tuple[int, int | None]] = []
        admin_targets: list[tuple[int, int | None]] = []
        seen: set[tuple[int, int | None]] = set()
        for user_id, bound_chat_id, _bound_window_id, bound_thread_id in bindings:
            chat_id = session_manager.resolve_chat_id(
                user_id,
                bound_thread_id,
                chat_id=bound_chat_id,
            )
            key = (chat_id, bound_thread_id)
            if key in seen:
                continue
            seen.add(key)
            all_targets.append(key)
            if _is_admin_user(user_id):
                admin_targets.append(key)

        if not admin_targets:
            for chat_id, bound_thread_id in all_targets:
                await safe_send(
                    bot,
                    chat_id,
                    "⚠️ Codex requested approval, but no admin is available in this session. Auto-declined.",
                    message_thread_id=bound_thread_id,
                )
            emit_telemetry(
                "approval.request.finalized",
                method=method,
                codex_thread_id=thread_id if isinstance(thread_id, str) else "",
                window_id=window_id,
                mode=mode,
                decision=APP_SERVER_APPROVAL_DECISION_DECLINE,
                reason="no_admin_targets",
                target_count=len(all_targets),
            )
            return {"decision": APP_SERVER_APPROVAL_DECISION_DECLINE}

        token, pending = _register_pending_app_server_approval()
        prompt = _build_app_server_approval_text(method, params, mode=mode)
        keyboard = _build_app_server_approval_keyboard(token)
        sent = 0
        for chat_id, bound_thread_id in admin_targets:
            try:
                await safe_send(
                    bot,
                    chat_id,
                    prompt,
                    message_thread_id=bound_thread_id,
                    reply_markup=keyboard,
                )
                sent += 1
            except Exception as e:
                logger.debug(
                    "Failed to send app-server approval prompt (chat=%s thread=%s): %s",
                    chat_id,
                    bound_thread_id,
                    e,
                )
        emit_telemetry(
            "approval.request.prompt_sent",
            method=method,
            codex_thread_id=thread_id if isinstance(thread_id, str) else "",
            window_id=window_id,
            mode=mode,
            admin_target_count=len(admin_targets),
            sent_count=sent,
        )

        if sent <= 0:
            _pop_pending_app_server_approval(token)
            emit_telemetry(
                "approval.request.finalized",
                method=method,
                codex_thread_id=thread_id if isinstance(thread_id, str) else "",
                window_id=window_id,
                mode=mode,
                decision=APP_SERVER_APPROVAL_DECISION_DECLINE,
                reason="prompt_send_failed",
            )
            return {"decision": APP_SERVER_APPROVAL_DECISION_DECLINE}

        decision: object = APP_SERVER_APPROVAL_DECISION_DECLINE
        decision_reason = "pending_decision"
        try:
            decision = await asyncio.wait_for(
                pending,
                timeout=APP_SERVER_APPROVAL_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            decision_reason = "timeout"
            for chat_id, bound_thread_id in admin_targets:
                await safe_send(
                    bot,
                    chat_id,
                    "⏳ Approval request timed out. Codex continued with decline.",
                    message_thread_id=bound_thread_id,
                )
        finally:
            _pop_pending_app_server_approval(token)

        if isinstance(decision, str) and decision in APP_SERVER_APPROVAL_DECISION_LABEL:
            emit_telemetry(
                "approval.request.finalized",
                method=method,
                codex_thread_id=thread_id if isinstance(thread_id, str) else "",
                window_id=window_id,
                mode=mode,
                decision=decision,
                reason=decision_reason,
            )
            return {"decision": decision}
        emit_telemetry(
            "approval.request.finalized",
            method=method,
            codex_thread_id=thread_id if isinstance(thread_id, str) else "",
            window_id=window_id,
            mode=mode,
            decision=APP_SERVER_APPROVAL_DECISION_DECLINE,
            reason="invalid_decision_payload",
            raw_decision=decision,
        )
        return {"decision": APP_SERVER_APPROVAL_DECISION_DECLINE}

    if method == "item/tool/requestUserInput":
        answers: dict[str, dict[str, list[str]]] = {}
        questions = params.get("questions")
        if isinstance(questions, list):
            readable: list[str] = []
            for q in questions:
                if not isinstance(q, dict):
                    continue
                qid = q.get("id")
                question_text = q.get("question")
                options = q.get("options")
                picked: list[str] = []
                if isinstance(options, list) and options:
                    first = options[0]
                    if isinstance(first, dict):
                        label = first.get("label")
                        if isinstance(label, str) and label:
                            picked = [label]
                if isinstance(qid, str) and qid:
                    answers[qid] = {"answers": picked}
                if isinstance(question_text, str) and question_text:
                    readable.append(question_text)
            if bindings and readable:
                summary = "\n".join(f"- {line}" for line in readable[:5])
                for user_id, bound_chat_id, _window_id, bound_thread_id in bindings:
                    await safe_send(
                        bot,
                        session_manager.resolve_chat_id(
                            user_id,
                            bound_thread_id,
                            chat_id=bound_chat_id,
                        ),
                        "ℹ️ Codex requested structured input; auto-selected default options.\n"
                        f"{summary}",
                        message_thread_id=bound_thread_id,
                    )
        return {"answers": answers}

    return None


async def _handle_codex_app_server_notification(
    method: str,
    params: dict[str, object],
    *,
    bot: Bot,
) -> None:
    """Translate app-server notifications into existing Telegram message flow."""
    def _notification_targets(
        *,
        codex_thread_id: str | None = None,
    ) -> list[tuple[int, int | None]]:
        seen: set[tuple[int, int | None]] = set()
        targets: list[tuple[int, int | None]] = []

        if codex_thread_id:
            bindings = session_manager.find_users_for_codex_thread(codex_thread_id)
            for user_id, bound_chat_id, _wid, bound_thread_id in bindings:
                chat_id = session_manager.resolve_chat_id(
                    user_id,
                    bound_thread_id,
                    chat_id=bound_chat_id,
                )
                key = (chat_id, bound_thread_id)
                if key in seen:
                    continue
                seen.add(key)
                targets.append(key)
            return targets

        for (
            user_id,
            bound_chat_id,
            bound_thread_id,
            _wid,
        ) in session_manager.iter_topic_window_bindings():
            chat_id = session_manager.resolve_chat_id(
                user_id,
                bound_thread_id,
                chat_id=bound_chat_id,
            )
            key = (chat_id, bound_thread_id)
            if key in seen:
                continue
            seen.add(key)
            targets.append(key)
        return targets

    if method == "error":
        codex_thread_id = params.get("threadId")
        error_obj = params.get("error")
        turn_id = params.get("turnId")
        will_retry = params.get("willRetry")

        msg = ""
        details = ""
        if isinstance(error_obj, dict):
            raw_msg = error_obj.get("message")
            raw_details = error_obj.get("additionalDetails")
            if isinstance(raw_msg, str):
                msg = raw_msg.strip()
            if isinstance(raw_details, str):
                details = raw_details.strip()

        lines = ["⚠️ Codex app-server error"]
        if msg:
            lines.append(f"Message: {msg}")
        if details:
            lines.append(f"Details: {details}")
        if isinstance(turn_id, str) and turn_id:
            lines.append(f"Turn: {turn_id}")
        if isinstance(will_retry, bool):
            lines.append(f"Will retry: {'yes' if will_retry else 'no'}")
        text = "\n".join(lines)

        targets = _notification_targets(
            codex_thread_id=codex_thread_id if isinstance(codex_thread_id, str) else None
        )
        for chat_id, thread_id in targets:
            await safe_send(
                bot,
                chat_id,
                text,
                message_thread_id=thread_id,
            )
        if isinstance(codex_thread_id, str) and codex_thread_id:
            if (
                will_retry is False
                and _is_transient_app_server_turn_error(msg, details)
            ):
                _pending_transient_app_server_errors[codex_thread_id] = (msg, details)
                emit_telemetry(
                    "transport.app_server.turn_failed_transient_error",
                    codex_thread_id=codex_thread_id,
                    turn_id=turn_id if isinstance(turn_id, str) else "",
                    will_retry=False,
                    message=msg,
                    details=details,
                )
            else:
                _pending_transient_app_server_errors.pop(codex_thread_id, None)
        return

    if method == "configWarning":
        summary = params.get("summary")
        details = params.get("details")
        path = params.get("path")

        lines = ["⚠️ Codex config warning"]
        if isinstance(summary, str) and summary.strip():
            lines.append(f"Summary: {summary.strip()}")
        if isinstance(path, str) and path.strip():
            lines.append(f"Path: {path.strip()}")
        if isinstance(details, str) and details.strip():
            lines.append(f"Details: {details.strip()}")
        text = "\n".join(lines)

        for chat_id, thread_id in _notification_targets():
            await safe_send(
                bot,
                chat_id,
                text,
                message_thread_id=thread_id,
            )
        return

    if method == "deprecationNotice":
        summary = params.get("summary")
        details = params.get("details")

        lines = ["ℹ️ Codex deprecation notice"]
        if isinstance(summary, str) and summary.strip():
            lines.append(f"Summary: {summary.strip()}")
        if isinstance(details, str) and details.strip():
            lines.append(f"Details: {details.strip()}")
        text = "\n".join(lines)

        for chat_id, thread_id in _notification_targets():
            await safe_send(
                bot,
                chat_id,
                text,
                message_thread_id=thread_id,
            )
        return

    if method == "turn/started":
        thread_id = params.get("threadId")
        turn = params.get("turn")
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if isinstance(thread_id, str) and isinstance(turn_id, str):
            _pending_transient_app_server_errors.pop(thread_id, None)
            session_manager.set_codex_turn_for_thread(thread_id, turn_id)
            _turn_has_final_text[thread_id] = False
        return

    if method == "turn/completed":
        thread_id = params.get("threadId")
        turn = params.get("turn")
        if not isinstance(thread_id, str):
            return
        status = turn.get("status") if isinstance(turn, dict) else ""
        if isinstance(status, str) and status == "inProgress":
            turn_id = turn.get("id") if isinstance(turn, dict) else ""
            if isinstance(turn_id, str):
                session_manager.set_codex_turn_for_thread(thread_id, turn_id)
            return

        had_final_text = _turn_has_final_text.pop(thread_id, False)
        session_manager.set_codex_turn_for_thread(thread_id, "")
        transient_error = _pending_transient_app_server_errors.pop(thread_id, None)
        suppressed_topics: set[tuple[int, int | None]] = set()
        if (
            isinstance(status, str)
            and status in {"failed", "interrupted"}
            and transient_error is not None
        ):
            error_msg, error_details = transient_error
            combined_error = "\n".join(
                part for part in (error_msg, error_details) if part.strip()
            )
            suppressed_topics = await _retry_failed_turn_after_transient_app_server_error(
                bot=bot,
                codex_thread_id=thread_id,
                status=status,
                error_message=combined_error,
            )
        # Any terminal turn completion should allow queued `/q` input to advance.
        # Restricting this to failed/interrupted leaves successful turns parked.
        dispatch_after_completion = True
        clear_progress_on_completion = status in {
            "failed",
            "interrupted",
            "cancelled",
            "canceled",
        }
        for (
            user_id,
            bound_chat_id,
            wid,
            bound_thread_id,
        ) in session_manager.find_users_for_codex_thread(
            thread_id
        ):
            if (user_id, bound_thread_id) in suppressed_topics:
                continue
            note_run_completed(
                user_id=user_id,
                thread_id=bound_thread_id,
                reason=f"turn_completed:{status or 'unknown'}",
            )
            missing_final_text = not clear_progress_on_completion and not had_final_text
            if clear_progress_on_completion:
                await enqueue_progress_clear(
                    bot,
                    user_id,
                    thread_id=bound_thread_id,
                )
            else:
                # Ensure process-message tracking is reset even when no final
                # assistant text item arrives for this turn.
                await enqueue_progress_finalize(
                    bot,
                    user_id,
                    window_id=wid,
                    thread_id=bound_thread_id,
                    # Always compact the finalized process message. The actual
                    # assistant response (or fallback) should be delivered as a
                    # separate message.
                    compact=True,
                )
                if missing_final_text:
                    fallback_note = ""
                    fallback_final_text = get_progress_text(
                        user_id=user_id,
                        thread_id=bound_thread_id,
                    ).strip()
                    if fallback_final_text:
                        fallback_note = fallback_final_text
                    else:
                        fallback_note = (
                            "⚠️ Turn completed without a final assistant response. "
                            "Please retry."
                        )
                    final_parts = build_response_parts(
                        fallback_note,
                        True,
                        "text",
                        "assistant",
                    )
                    await enqueue_content_message(
                        bot=bot,
                        user_id=user_id,
                        window_id=wid,
                        parts=final_parts,
                        content_type="text",
                        text=fallback_note,
                        thread_id=bound_thread_id,
                    )
            should_dispatch = queued_topic_input_count(user_id, bound_thread_id) > 0 and (
                dispatch_after_completion or missing_final_text
            )
            if should_dispatch:
                await _dispatch_next_queued_input(
                    bot=bot,
                    user_id=user_id,
                    thread_id=bound_thread_id,
                    window_id=wid,
                    chat_id=bound_chat_id,
                )
        return

    if method == "item/reasoning/textDelta":
        # Ignore token-level reasoning deltas to keep working updates concise.
        return

    if method == "item/agentMessage/delta":
        # Ignore token-level agent message deltas. These can arrive at very high
        # frequency and overwhelm both the Telegram edit queue and the Codex
        # app-server reader loop (causing `turn/start` timeouts).
        return

    if method == "item/completed":
        thread_id = params.get("threadId")
        item = params.get("item")
        if not isinstance(thread_id, str) or not isinstance(item, dict):
            return
        item_type = item.get("type")
        if item_type != "agentMessage":
            return
        text = _extract_app_server_response_item_text(item)
        if not text:
            return
        _turn_has_final_text[thread_id] = True
        await handle_new_message(
            NewMessage(
                session_id=thread_id,
                text=text,
                is_complete=True,
                content_type="text",
                role="assistant",
                source="app_server",
            ),
            bot,
        )
        return

    if method == "rawResponseItem/completed":
        thread_id = params.get("threadId")
        item = params.get("item")
        if not isinstance(thread_id, str) or not isinstance(item, dict):
            return
        item_type = item.get("type")
        role = item.get("role")
        if item_type != "message" or role != "assistant":
            return
        text = _extract_app_server_response_item_text(item)
        if not text:
            return
        content_type = TranscriptParser.assistant_phase_to_content_type(
            item.get("phase")
        )
        if content_type == "text":
            _turn_has_final_text[thread_id] = True
        await handle_new_message(
            NewMessage(
                session_id=thread_id,
                text=text,
                is_complete=True,
                content_type=content_type,
                role="assistant",
                source="app_server",
            ),
            bot,
        )


async def handle_new_message(msg: NewMessage, bot: Bot) -> None:
    """Handle a new assistant message — enqueue for sequential processing.

    Messages are queued per-user to ensure status messages always appear last.
    Routes via topic bindings to deliver to the correct topic.
    """
    status = "complete" if msg.is_complete else "streaming"
    logger.info(
        f"handle_new_message [{status}]: session={msg.session_id}, "
        f"text_len={len(msg.text)}"
    )

    # Find users whose thread-bound window matches this session/thread.
    active_users: list[tuple[int, int | None, str, int]] = []
    if _codex_app_server_enabled():
        active_users = session_manager.find_users_for_codex_thread(msg.session_id)
    if not active_users:
        active_users = await session_manager.find_users_for_session(msg.session_id)

    if not active_users:
        logger.info(f"No active users for session {msg.session_id}")
        return

    for user_id, chat_id, wid, thread_id in active_users:
        if await _handle_shadow_transcript_message_for_topic(
            msg=msg,
            bot=bot,
            user_id=user_id,
            chat_id=chat_id,
            window_id=wid,
            thread_id=thread_id,
        ):
            continue

        if msg.role == "system":
            continue
        if msg.role == "user" and not config.show_user_messages:
            continue

        if msg.role == "assistant":
            note_run_activity(
                user_id=user_id,
                thread_id=thread_id,
                window_id=wid,
                source=f"assistant_{msg.content_type}",
            )

        delivery_text = msg.text
        attachment_image_data: list[tuple[str, bytes]] | None = None
        document_data: list[tuple[str, bytes]] | None = None
        if msg.role == "assistant" and msg.is_complete and msg.content_type == "text":
            workspace_dir = _resolve_workspace_dir_for_window(
                user_id=user_id,
                thread_id=thread_id,
                chat_id=chat_id,
                window_id=wid,
            )
            (
                delivery_text,
                attachment_image_data,
                document_data,
            ) = await _extract_telegram_attachments_for_window(
                msg.text,
                workspace_dir=workspace_dir,
                window_id=wid,
            )

        combined_image_data: list[tuple[str, bytes]] | None = None
        if msg.image_data or attachment_image_data:
            combined_image_data = []
            if msg.image_data:
                combined_image_data.extend(msg.image_data)
            if attachment_image_data:
                combined_image_data.extend(attachment_image_data)
            if not combined_image_data:
                combined_image_data = None

        parts = (
            build_response_parts(
                delivery_text,
                msg.is_complete,
                msg.content_type,
                msg.role,
            )
            if delivery_text
            else []
        )

        if msg.is_complete:
            is_progress_update = (
                msg.role == "assistant"
                and msg.content_type in ("thinking", "progress")
            )
            is_final_assistant_text = (
                msg.role == "assistant" and msg.content_type == "text"
            )
            should_update_read_offset = not is_progress_update
            looper_completed_state = None

            if is_progress_update:
                progress_text = "".join(parts)
                if progress_text.strip():
                    await enqueue_progress_update(
                        bot=bot,
                        user_id=user_id,
                        window_id=wid,
                        progress_text=progress_text,
                        thread_id=thread_id,
                    )
            else:
                if is_final_assistant_text:
                    # Keep process message and mark it complete before final answer message.
                    await enqueue_progress_finalize(
                        bot,
                        user_id,
                        window_id=wid,
                        thread_id=thread_id,
                        compact=True,
                    )
                    note_run_completed(
                        user_id=user_id,
                        thread_id=thread_id,
                        reason="final_assistant_text",
                    )
                    if thread_id is not None:
                        looper_completed_state = consume_looper_completion_keyword(
                            user_id=user_id,
                            thread_id=thread_id,
                            window_id=wid,
                            assistant_text=delivery_text,
                        )

            # Enqueue content message task
            # Note: tool_result editing is handled inside _process_content_task
            # to ensure sequential processing with tool_use message sending
                await enqueue_content_message(
                    bot=bot,
                    user_id=user_id,
                    window_id=wid,
                    parts=parts,
                    tool_use_id=msg.tool_use_id,
                    content_type=msg.content_type,
                    text=delivery_text,
                    thread_id=thread_id,
                    image_data=combined_image_data,
                    document_data=document_data,
                )
                if looper_completed_state is not None:
                    await safe_send(
                        bot,
                        session_manager.resolve_chat_id(
                            user_id,
                            thread_id,
                            chat_id=chat_id,
                        ),
                        (
                            "✅ Looper stopped after completion keyword match.\n"
                            f"Plan: `{looper_completed_state.plan_path}`\n"
                            f"Keyword: `{looper_completed_state.keyword}`"
                        ),
                        message_thread_id=thread_id,
                    )

            # Update user's read offset to current file position
            # This marks these messages as "read" for this user
            if should_update_read_offset:
                await _update_user_read_offset_for_window(
                    user_id=user_id,
                    window_id=wid,
                    file_path=msg.file_path,
                )


# --- App lifecycle ---


async def post_init(application: Application) -> None:
    global session_monitor, _status_poll_task, _controller_rpc_server

    emit_telemetry(
        "runtime.mode",
        runtime_mode=config.runtime_mode,
        session_provider=config.session_provider,
        codex_transport=config.codex_transport,
    )

    await application.bot.delete_my_commands()

    bot_commands = [
        BotCommand("start", "Show welcome message"),
        BotCommand("folder", "Open folder picker for this topic"),
        BotCommand("resume", "Open session lifecycle menu"),
        BotCommand("history", "Message history for this topic"),
        BotCommand("esc", "Send Escape to interrupt assistant"),
        BotCommand("q", "Queue next message until current run completes"),
        BotCommand("approvals", "Show/change session approval mode"),
        BotCommand("mentions", "Toggle mention-only invocation for this session"),
        BotCommand("allowed", "Manage allowed user IDs"),
        BotCommand("apps", "Manage per-topic CoCo apps"),
        BotCommand("skills", "Manage per-topic Codex skills"),
        BotCommand("worktree", "List/create/fold git worktrees"),
        BotCommand("restart", "Restart CoCo bot process"),
        BotCommand("unbind", "Unbind topic from session (keeps window running)"),
        BotCommand("status", "Show current Codex status panel"),
        BotCommand("model", "Show Codex model options/reasoning levels"),
        BotCommand("transcription", "Show/change server transcription mode"),
        BotCommand("update", "Check CoCo/Codex updates and trigger safe upgrade"),
    ]
    # Add assistant slash commands
    for cmd_name, desc in CC_COMMANDS.items():
        bot_commands.append(BotCommand(cmd_name, desc))

    await application.bot.set_my_commands(bot_commands)

    notice_target = _pop_restart_notice_target()
    notice_targets = _startup_notice_targets(notice_target)
    notice_text = _pick_restart_back_up_message()
    for notice_chat_id, notice_thread_id in notice_targets:
        logger.info(
            "Sending startup notice to chat=%s thread=%s",
            notice_chat_id,
            notice_thread_id,
        )
        try:
            await safe_send(
                application.bot,
                notice_chat_id,
                notice_text,
                message_thread_id=notice_thread_id,
            )
            logger.info("Startup notice sent")
        except Exception as e:
            logger.debug(
                "Failed to send startup notice (chat=%s thread=%s): %s",
                notice_chat_id,
                notice_thread_id,
                e,
            )

    await session_manager.resolve_stale_ids()

    # Pre-fill global rate limiter bucket on restart.
    # AsyncLimiter starts at _level=0 (full burst capacity), but Telegram's
    # server-side counter persists across bot restarts.  Setting _level=max_rate
    # forces the bucket to start "full" so capacity drains in naturally (~1s).
    # AIORateLimiter has no per-private-chat limiter, so max_retries is the
    # primary protection (retry + pause all concurrent requests on 429).
    rate_limiter = application.bot.rate_limiter
    if rate_limiter and rate_limiter._base_limiter:
        rate_limiter._base_limiter._level = rate_limiter._base_limiter.max_rate
        logger.info("Pre-filled global rate limiter bucket")

    use_app_server_stream = False
    if _codex_app_server_preferred():
        _ensure_codex_trust_for_runtime()

        async def app_server_notification_handler(
            method: str,
            params: dict[str, object],
        ) -> None:
            await _handle_codex_app_server_notification(
                method,
                params,
                bot=application.bot,
            )

        async def app_server_request_handler(
            method: str,
            params: dict[str, object],
        ) -> dict[str, object] | None:
            return await _handle_codex_app_server_request(
                method,
                params,
                bot=application.bot,
            )

        await codex_app_server_client.set_handlers(
            notification_handler=app_server_notification_handler,
            server_request_handler=app_server_request_handler,
        )
        try:
            await codex_app_server_client.ensure_started()
            use_app_server_stream = True
            try:
                validation = await session_manager.validate_codex_topic_bindings()
                emit_telemetry(
                    "transport.app_server.binding_validation",
                    runtime_mode=config.runtime_mode,
                    session_provider=config.session_provider,
                    codex_transport=config.codex_transport,
                    checked=validation.get("checked", 0),
                    invalid=validation.get("invalid", 0),
                    repaired=validation.get("repaired", 0),
                )
                if int(validation.get("repaired", 0)) > 0:
                    logger.warning(
                        "Cleared %d invalid persisted Codex thread binding(s)",
                        int(validation.get("repaired", 0)),
                    )
            except Exception as validation_error:
                emit_telemetry(
                    "transport.app_server.binding_validation_failed",
                    runtime_mode=config.runtime_mode,
                    session_provider=config.session_provider,
                    codex_transport=config.codex_transport,
                    error=str(validation_error),
                )
                logger.warning(
                    "Failed validating persisted Codex thread bindings: %s",
                    validation_error,
                )
            emit_telemetry(
                "transport.app_server.start_ok",
                runtime_mode=config.runtime_mode,
                session_provider=config.session_provider,
                codex_transport=config.codex_transport,
            )
            logger.info("Codex app-server transport enabled")
        except Exception as e:
            emit_telemetry(
                "transport.app_server.start_failed",
                runtime_mode=config.runtime_mode,
                session_provider=config.session_provider,
                codex_transport=config.codex_transport,
                fallback_allowed=(
                    config.runtime_mode != "app_server_only"
                    and config.codex_transport != "app_server"
                ),
                error=str(e),
            )
            if (
                config.codex_transport == "app_server"
                or config.runtime_mode == "app_server_only"
            ):
                raise
            logger.warning(
                "Codex app-server unavailable, falling back to transcript monitor: %s",
                e,
            )

    monitor = SessionMonitor()

    async def message_callback(msg: NewMessage) -> None:
        await handle_new_message(msg, application.bot)

    monitor.set_message_callback(message_callback)
    monitor.start()
    session_monitor = monitor
    if use_app_server_stream:
        logger.info("Session monitor started in shadow mode")
    else:
        logger.info("Session monitor started")

    if _env_bool(_COCO_UPDATE_CHECK_ENABLED_ENV, default=True):
        global _update_check_task
        _update_check_task = asyncio.create_task(
            _coco_update_check_loop(application.bot)
        )
        logger.info("CoCo update check task started")

    # Start status polling task
    _status_poll_task = asyncio.create_task(status_poll_loop(application.bot))
    logger.info("Status polling task started")

    async def _rpc_heartbeat_handler(params: dict[str, object]) -> dict[str, object]:
        machine_id = str(params.get("machine_id", "")).strip()
        display_name = str(params.get("display_name", "")).strip() or machine_id
        tailnet_name = str(params.get("tailnet_name", "")).strip()
        transport = str(params.get("transport", "agent_rpc")).strip() or "agent_rpc"
        browse_roots = [
            item.strip()
            for item in params.get("browse_roots", [])
            if isinstance(item, str) and item.strip()
        ]
        capabilities = [
            item.strip()
            for item in params.get("capabilities", [])
            if isinstance(item, str) and item.strip()
        ]
        agent_version = str(params.get("agent_version", "")).strip()
        rpc_host = str(params.get("rpc_host", "")).strip()
        rpc_port_raw = params.get("rpc_port", 0)
        try:
            rpc_port = int(rpc_port_raw or 0)
        except (TypeError, ValueError):
            rpc_port = 0
        node_registry.note_heartbeat(
            machine_id=machine_id,
            display_name=display_name,
            tailnet_name=tailnet_name,
            transport=transport,
            rpc_host=rpc_host,
            rpc_port=rpc_port,
            is_local=False,
            browse_roots=browse_roots,
            capabilities=capabilities,
            agent_version=agent_version,
            controller_capable=bool(params.get("controller_capable", False)),
            controller_active=bool(params.get("controller_active", False)),
            preferred_controller=bool(params.get("preferred_controller", False)),
        )
        return {"ok": True}

    async def _rpc_notification_handler(params: dict[str, object]) -> None:
        method = params.get("method")
        inner_params = params.get("params")
        if not isinstance(method, str) or not isinstance(inner_params, dict):
            return
        await _handle_codex_app_server_notification(
            method,
            inner_params,
            bot=application.bot,
        )

    async def _rpc_request_handler(params: dict[str, object]) -> dict[str, object] | None:
        method = params.get("method")
        inner_params = params.get("params")
        if not isinstance(method, str) or not isinstance(inner_params, dict):
            return None
        return await _handle_codex_app_server_request(
            method,
            inner_params,
            bot=application.bot,
        )

    _controller_rpc_server = ControllerRpcServer(
        shared_secret=config.cluster_shared_secret,
        heartbeat_handler=_rpc_heartbeat_handler,
        notification_handler=_rpc_notification_handler,
        request_handler=_rpc_request_handler,
    )
    await _controller_rpc_server.start(
        host=config.rpc_listen_host,
        port=config.rpc_port,
    )
    rpc_host, rpc_port = _controller_rpc_server.bound_address()
    logger.info("Controller RPC listening on %s:%s", rpc_host, rpc_port)


async def post_shutdown(application: Application) -> None:
    global _status_poll_task, _controller_rpc_server, _update_check_task

    if _update_check_task:
        _update_check_task.cancel()
        try:
            await _update_check_task
        except asyncio.CancelledError:
            pass
        _update_check_task = None
        logger.info("CoCo update check task stopped")

    # Stop status polling
    if _status_poll_task:
        _status_poll_task.cancel()
        try:
            await _status_poll_task
        except asyncio.CancelledError:
            pass
        _status_poll_task = None
        logger.info("Status polling stopped")

    if _controller_rpc_server is not None:
        await _controller_rpc_server.stop()
        _controller_rpc_server = None
        logger.info("Controller RPC stopped")

    # Stop all queue workers
    await shutdown_workers()

    if session_monitor:
        session_monitor.stop()
        logger.info("Session monitor stopped")

    await codex_app_server_client.stop()


def create_bot() -> Application:
    application = (
        Application.builder()
        .token(config.telegram_bot_token)
        .rate_limiter(AIORateLimiter(max_retries=5))
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Probe all incoming update message shapes (message vs channel_post, etc.).
    application.add_handler(TypeHandler(Update, inbound_update_probe), group=-10)

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("folder", folder_command))
    application.add_handler(CommandHandler("resume", resume_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("esc", esc_command))
    application.add_handler(CommandHandler("q", queue_command))
    application.add_handler(CommandHandler("approvals", approvals_command))
    application.add_handler(CommandHandler("mentions", mentions_command))
    application.add_handler(CommandHandler("allowed", allowed_command))
    application.add_handler(CommandHandler("apps", apps_command))
    application.add_handler(CommandHandler("skills", skills_command))
    application.add_handler(CommandHandler("worktree", worktree_command))
    application.add_handler(CommandHandler("restart", restart_command))
    application.add_handler(CommandHandler("unbind", unbind_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("model", model_command))
    application.add_handler(CommandHandler("fast", fast_command))
    application.add_handler(CommandHandler("transcription", transcription_command))
    application.add_handler(CommandHandler("update", update_command))
    application.add_handler(CallbackQueryHandler(callback_handler))
    # Topic closed event — auto-kill associated window
    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.FORUM_TOPIC_CLOSED,
            topic_closed_handler,
        )
    )
    # Forward any other /command to assistant
    application.add_handler(MessageHandler(filters.COMMAND, forward_command_handler))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)
    )
    # Channel-post text (e.g. "send as channel" in supergroup topics)
    application.add_handler(
        MessageHandler(filters.UpdateType.CHANNEL_POST, channel_post_text_handler)
    )
    # Photos: download and forward file path to assistant
    application.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    # Voice/audio: download, transcribe locally, and forward transcript
    application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, audio_handler))
    # Catch-all: non-text content (stickers, voice, etc.)
    application.add_handler(
        MessageHandler(
            ~filters.COMMAND & ~filters.TEXT & ~filters.StatusUpdate.ALL,
            unsupported_content_handler,
        )
    )

    return application
