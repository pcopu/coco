"""Application configuration — reads env vars and exposes a singleton.

Loads TELEGRAM_BOT_TOKEN, ALLOWED_USERS, assistant/runtime paths, and
monitoring intervals from environment variables (with .env support).
.env loading priority: local .env (cwd) > the CoCo config dir's `.env`
(`COCO_DIR` or the default). The module-level
`config` instance is imported by nearly every other module.

Key class: Config (singleton instantiated as `config`).
"""

import logging
import socket
from pathlib import Path

from dotenv import dotenv_values, load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

from .utils import coco_dir, env_alias

logger = logging.getLogger(__name__)


class _EnvSettings(BaseSettings):
    """Raw environment values parsed once from process env."""

    model_config = SettingsConfigDict(
        env_prefix="",
        extra="ignore",
    )

    TELEGRAM_BOT_TOKEN: str = ""
    ALLOWED_USERS: str = ""
    ALLOWED_GROUP_IDS: str = ""
    ASSISTANT_COMMAND: str | None = None
    CODEX_COMMAND: str | None = None
    CODEX_TRANSPORT: str = "app_server"
    SESSIONS_PATH: str | None = None
    CODEX_SESSIONS_PATH: str | None = None
    MONITOR_POLL_INTERVAL: str = "2.0"
    SHOW_USER_MESSAGES: str = "false"
    BROWSE_ROOT: str = ""
    GROUP_BROWSE_ROOTS: str = ""
    CODEX_SANDBOX_MODE: str = ""


def _resolve_path_list(raw_paths: list[str]) -> list[Path]:
    seen: set[str] = set()
    resolved_paths: list[Path] = []
    for raw_path in raw_paths:
        path = Path(raw_path).expanduser()
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        resolved_paths.append(resolved)
    return resolved_paths


def _parse_bool(raw_value: str, *, default: bool) -> bool:
    value = raw_value.strip().lower()
    if not value:
        return default
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    logger.warning("Boolean config value %r is invalid; using default %s", raw_value, default)
    return default


class Config:
    """Application configuration loaded from environment variables."""

    def __init__(self) -> None:
        self.config_dir = coco_dir()
        self.config_dir.mkdir(parents=True, exist_ok=True)

        # Load .env: local (cwd) takes priority over config_dir.
        # load_dotenv default override=False means first-loaded wins.
        local_env = Path(".env")
        global_env = self.config_dir / ".env"
        if local_env.is_file():
            load_dotenv(local_env)
            logger.debug("Loaded env from %s", local_env.resolve())
        if global_env.is_file():
            load_dotenv(global_env)
            logger.debug("Loaded env from %s", global_env)

        env = _EnvSettings()

        auth_env_raw = env_alias("COCO_AUTH_ENV_FILE")
        self.auth_env_file = Path(auth_env_raw).expanduser() if auth_env_raw else None

        auth_meta_raw = env_alias("COCO_AUTH_META_FILE")
        self.auth_meta_file = (
            Path(auth_meta_raw).expanduser()
            if auth_meta_raw
            else self.config_dir / "allowed_users_meta.json"
        )

        node_role_raw = env_alias("COCO_NODE_ROLE", default="controller").lower()
        if node_role_raw not in {"controller", "agent"}:
            logger.warning(
                "COCO_NODE_ROLE=%r is invalid; forcing 'controller'",
                node_role_raw,
            )
            node_role_raw = "controller"
        self.node_role = node_role_raw or "controller"
        self.telegram_enabled = self.node_role == "controller"

        self.telegram_bot_token = env.TELEGRAM_BOT_TOKEN or ""
        if self.telegram_enabled and not self.telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required")

        allowed_users_str = env.ALLOWED_USERS
        if self.telegram_enabled and self.auth_env_file is not None:
            if not self.auth_env_file.is_file():
                raise ValueError(
                    f"COCO_AUTH_ENV_FILE does not exist: {self.auth_env_file}"
                )
            auth_values = dotenv_values(self.auth_env_file)
            auth_allowed_users = (auth_values.get("ALLOWED_USERS") or "").strip()
            if not auth_allowed_users:
                raise ValueError(
                    f"ALLOWED_USERS is required in auth file: {self.auth_env_file}"
                )
            allowed_users_str = auth_allowed_users
        if self.telegram_enabled and not allowed_users_str:
            raise ValueError("ALLOWED_USERS environment variable is required")
        if allowed_users_str:
            try:
                self.allowed_users: set[int] = {
                    int(uid.strip()) for uid in allowed_users_str.split(",") if uid.strip()
                }
            except ValueError as e:
                raise ValueError(
                    f"ALLOWED_USERS contains non-numeric value: {e}. "
                    "Expected comma-separated Telegram user IDs."
                ) from e
        else:
            self.allowed_users = set()

        allowed_groups_raw = env.ALLOWED_GROUP_IDS.strip()
        if allowed_groups_raw:
            try:
                self.allowed_group_ids: set[int] = {
                    int(gid.strip()) for gid in allowed_groups_raw.split(",") if gid.strip()
                }
            except ValueError as e:
                raise ValueError(
                    f"ALLOWED_GROUP_IDS contains non-numeric value: {e}. "
                    "Expected comma-separated Telegram chat IDs."
                ) from e
        else:
            self.allowed_group_ids = set()

        # Transcript provider: codex only.
        self.session_provider = "codex"

        # Assistant command to run in new windows.
        # Backward-compatible alias:
        # - ASSISTANT_COMMAND (preferred)
        # - CODEX_COMMAND
        self.assistant_command = (
            env.ASSISTANT_COMMAND
            or env.CODEX_COMMAND
            or "codex"
        )

        # Codex transport mode (app-server only).
        transport_raw = env.CODEX_TRANSPORT.strip().lower()
        if transport_raw != "app_server":
            logger.warning(
                "CODEX_TRANSPORT=%r is no longer supported, forcing 'app_server'",
                transport_raw,
            )
            transport_raw = "app_server"
        self.codex_transport = transport_raw

        sandbox_mode_raw = env.CODEX_SANDBOX_MODE.strip().lower()
        # Codex sandbox modes used by CLI/app-server. Defaulting to full access
        # avoids confusing "Permission denied" / DNS failures when users expect
        # git + file writes to work inside a trusted workspace.
        if not sandbox_mode_raw:
            sandbox_mode_raw = "danger-full-access"
        allowed_sandbox_modes = {"read-only", "workspace-write", "danger-full-access"}
        if sandbox_mode_raw not in allowed_sandbox_modes:
            logger.warning(
                "CODEX_SANDBOX_MODE=%r is invalid; forcing %r",
                sandbox_mode_raw,
                "danger-full-access",
            )
            sandbox_mode_raw = "danger-full-access"
        self.codex_sandbox_mode = sandbox_mode_raw

        # Runtime mode (app-server only).
        runtime_mode = env_alias("COCO_RUNTIME_MODE", default="app_server_only").lower()
        if runtime_mode != "app_server_only":
            logger.warning(
                "COCO_RUNTIME_MODE=%r is no longer supported, forcing 'app_server_only'",
                runtime_mode,
            )
            runtime_mode = "app_server_only"
        self.runtime_mode = runtime_mode

        # All state files live under config_dir.
        self.state_file = self.config_dir / "state.json"
        self.monitor_state_file = self.config_dir / "monitor_state.json"
        node_registry_file_raw = env_alias("COCO_NODE_REGISTRY_FILE")
        self.node_registry_file = (
            Path(node_registry_file_raw).expanduser()
            if node_registry_file_raw
            else self.config_dir / "nodes.json"
        )

        # Session transcript path.
        # Backward-compatible alias:
        # - SESSIONS_PATH (preferred)
        # - CODEX_SESSIONS_PATH
        sessions_path_raw = env.SESSIONS_PATH or env.CODEX_SESSIONS_PATH
        if sessions_path_raw:
            self.sessions_path = Path(sessions_path_raw).expanduser()
        else:
            self.sessions_path = Path.home() / ".codex" / "sessions"
        self.monitor_poll_interval = float(env.MONITOR_POLL_INTERVAL)
        self.node_heartbeat_interval = float(env_alias("COCO_NODE_HEARTBEAT_INTERVAL", default="15.0"))
        self.node_offline_timeout = float(env_alias("COCO_NODE_OFFLINE_TIMEOUT", default="45.0"))

        hostname = socket.gethostname().strip().lower().replace(" ", "-")
        self.machine_id = env_alias("COCO_MACHINE_ID") or hostname or "coco-node"
        self.machine_name = (
            env_alias("COCO_MACHINE_NAME")
            or socket.gethostname().strip()
            or self.machine_id
        )
        self.tailnet_name = env_alias("COCO_TAILNET_NAME")
        self.rpc_listen_host = env_alias("COCO_RPC_LISTEN_HOST", default="127.0.0.1")
        try:
            self.rpc_port = int(env_alias("COCO_RPC_PORT", default="8787"))
        except ValueError as e:
            raise ValueError("COCO_RPC_PORT must be an integer") from e
        self.rpc_advertise_host = env_alias("COCO_RPC_ADVERTISE_HOST")
        self.controller_rpc_host = env_alias("COCO_CONTROLLER_RPC_HOST")
        try:
            self.controller_rpc_port = int(
                env_alias("COCO_CONTROLLER_RPC_PORT", default=str(self.rpc_port))
            )
        except ValueError as e:
            raise ValueError("COCO_CONTROLLER_RPC_PORT must be an integer") from e
        self.cluster_shared_secret = env_alias("COCO_CLUSTER_SHARED_SECRET")
        self.controller_capable = _parse_bool(
            env_alias("COCO_CONTROLLER_CAPABLE", default="true"),
            default=True,
        )
        self.controller_active = _parse_bool(
            env_alias("COCO_CONTROLLER_ACTIVE", default="true"),
            default=True,
        )
        self.preferred_controller = _parse_bool(
            env_alias("COCO_PREFERRED_CONTROLLER", default="true"),
            default=True,
        )

        # Display user messages in history and real-time notifications.
        # Default False to avoid echoing the user's own text back in Telegram.
        show_user_messages_raw = env.SHOW_USER_MESSAGES.strip().lower()
        self.show_user_messages = show_user_messages_raw in {"1", "true", "yes", "on"}

        # Restrict directory browser to one safe root: ~/env.
        browse_root_candidate = Path(env.BROWSE_ROOT).expanduser() if env.BROWSE_ROOT.strip() else Path.home() / "env"
        try:
            browse_root_resolved = browse_root_candidate.resolve()
        except OSError:
            browse_root_resolved = Path.cwd().resolve()
        if not browse_root_resolved.exists() or not browse_root_resolved.is_dir():
            logger.warning(
                "Browse root %s is unavailable, defaulting to cwd %s",
                browse_root_candidate,
                Path.cwd().resolve(),
            )
            browse_root_resolved = Path.cwd().resolve()
        self.browse_root = browse_root_resolved

        self.group_browse_roots: dict[int, Path] = {}
        group_roots_raw = env.GROUP_BROWSE_ROOTS.strip()
        if group_roots_raw:
            for item in [part.strip() for part in group_roots_raw.split(",") if part.strip()]:
                if "=" not in item:
                    raise ValueError(
                        "GROUP_BROWSE_ROOTS entry must be '<chat_id>=<path>'"
                    )
                raw_chat_id, raw_path = item.split("=", 1)
                try:
                    chat_id = int(raw_chat_id.strip())
                except ValueError as e:
                    raise ValueError(
                        f"GROUP_BROWSE_ROOTS chat id is invalid: {raw_chat_id!r}"
                    ) from e
                path_candidate = Path(raw_path.strip()).expanduser()
                try:
                    resolved_path = path_candidate.resolve()
                except OSError:
                    resolved_path = self.browse_root
                if not resolved_path.exists() or not resolved_path.is_dir():
                    logger.warning(
                        "Group browse root %s for chat %s is unavailable; using default %s",
                        path_candidate,
                        chat_id,
                        self.browse_root,
                    )
                    resolved_path = self.browse_root
                self.group_browse_roots[chat_id] = resolved_path

        # Local app roots used by /apps and topic app injection.
        # Comma-separated absolute/relative paths can override defaults.
        # Example:
        #   COCO_APPS_PATHS=/home/user/.coco/apps,/srv/coco/apps
        # Legacy compatibility:
        #   COCO_SKILLS_PATHS=/home/user/.coco/apps,/srv/coco/apps
        apps_paths_raw = env_alias("COCO_APPS_PATHS") or env_alias("COCO_SKILLS_PATHS")
        if apps_paths_raw:
            raw_paths = [item.strip() for item in apps_paths_raw.split(",") if item.strip()]
        else:
            raw_paths = [
                str(Path.cwd() / "apps"),
                str(self.config_dir / "apps"),
            ]
        self.apps_paths = _resolve_path_list(raw_paths)
        self.skills_paths = self.apps_paths

        # Codex skill roots surfaced by /skills.
        codex_skills_paths_raw = env_alias("COCO_CODEX_SKILLS_PATHS")
        if codex_skills_paths_raw:
            codex_raw_paths = [
                item.strip() for item in codex_skills_paths_raw.split(",") if item.strip()
            ]
        else:
            codex_raw_paths = [str(Path.home() / ".codex" / "skills")]
        self.codex_skills_paths = _resolve_path_list(codex_raw_paths)

        logger.debug(
            "Config initialized: dir=%s, token=%s..., allowed_users=%d, "
            "provider=%s, transport=%s, runtime_mode=%s, "
            "sessions_path=%s, assistant_command=%s, "
            "allowed_group_ids=%s, browse_root=%s, group_browse_roots=%s, "
            "apps_paths=%s, codex_skills_paths=%s, "
            "auth_env_file=%s, auth_meta_file=%s, machine_id=%s, machine_name=%s, "
            "rpc_listen=%s:%s, rpc_advertise_host=%s, controller_rpc=%s:%s, "
            "node_registry_file=%s",
            self.config_dir,
            self.telegram_bot_token[:8],
            len(self.allowed_users),
            self.session_provider,
            self.codex_transport,
            self.runtime_mode,
            self.sessions_path,
            self.assistant_command,
            sorted(self.allowed_group_ids),
            self.browse_root,
            {k: str(v) for k, v in self.group_browse_roots.items()},
            [str(path) for path in self.apps_paths],
            [str(path) for path in self.codex_skills_paths],
            str(self.auth_env_file) if self.auth_env_file else "",
            self.auth_meta_file,
            self.machine_id,
            self.machine_name,
            self.rpc_listen_host,
            self.rpc_port,
            self.rpc_advertise_host,
            self.controller_rpc_host,
            self.controller_rpc_port,
            self.node_registry_file,
        )

    def is_user_allowed(self, user_id: int) -> bool:
        """Check if a user is in the allowed list."""
        return user_id in self.allowed_users

    def is_group_allowed(self, chat_id: int | None) -> bool:
        """Check if a group is allowed when ALLOWED_GROUP_IDS is configured."""
        if not self.allowed_group_ids:
            return True
        if chat_id is None:
            return True
        return chat_id in self.allowed_group_ids

    def resolve_browse_root_for_chat(self, chat_id: int | None) -> Path:
        """Resolve browse root for a group, falling back to the default root."""
        if chat_id is None:
            return self.browse_root
        return self.group_browse_roots.get(chat_id, self.browse_root)


config = Config()
