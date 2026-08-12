"""Codex app-server JSON-RPC client.

Provides a lightweight async client over stdio for the experimental
`codex app-server` protocol. The client tracks active turns and exposes
helpers for thread/turn operations used by Telegram handlers.
"""

from __future__ import annotations

import asyncio
from collections import deque
import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import config
from .utils import atomic_write_json, coco_dir, env_alias

logger = logging.getLogger(__name__)

NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None]]
ServerRequestHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any] | None]]
TransportResetHandler = Callable[[str, int], Awaitable[None]]

# Default asyncio StreamReader limit (64 KiB) is too small for app-server
# JSONL payloads that can inline generated images. Keep this above the largest
# single session line observed in production (~1.7 MiB).
APP_SERVER_STREAM_LIMIT = 16 * 1024 * 1024
APP_SERVER_NOTIFICATION_QUEUE_MAXSIZE = 256
APP_SERVER_NOTIFICATION_OVERFLOW_MAXSIZE = 64
APP_SERVER_NOTIFICATION_DROP_WHEN_FULL = frozenset(
    {
        "account/rateLimits/updated",
        "thread/tokenUsage/updated",
    }
)
SAFE_TIMEOUT_RETRY_METHODS = frozenset({"account/rateLimits/read"})
SAFE_TIMEOUT_RECYCLE_METHODS = frozenset(
    {
        "account/rateLimits/read",
        "thread/goal/get",
        "thread/list",
        "thread/read",
    }
)
APP_SERVER_MUTATION_TIMEOUT_SECONDS = 20.0
APP_SERVER_ENABLED_FEATURES = ("goals",)
INTERNAL_TRANSPORT_CONTEXT_KEY = "__coco_transport"
_APP_SERVER_START_FAILURE_FILE = coco_dir() / "app_server_start_failures.json"
_APP_SERVER_OWNED_PIDS_DIR = coco_dir() / "app_server_owned_pids"
_APP_SERVER_START_FAILURE_WINDOW_SECONDS = 15 * 60
_APP_SERVER_START_FAILURE_RESET_THRESHOLD = 3
_CODEX_LOGS_DB_NAME = "logs_2.sqlite"
_CODEX_LOGS_DB_RECOVERY_MIN_BYTES = 1024 * 1024 * 1024
_CODEX_LOGS_DB_WAL_RECOVERY_MIN_BYTES = 256 * 1024 * 1024
_CODEX_LOGS_DB_RECOVERY_TRUE_VALUES = {"1", "true", "yes", "on"}
_CODEX_LOGS_DB_PROCESS_EXIT_TIMEOUT_SECONDS = 1.0
_CODEX_LOGS_DB_PROCESS_EXIT_POLL_SECONDS = 0.05


class CodexAppServerError(RuntimeError):
    """Raised for app-server request/transport failures."""


class CodexAppServerClient:
    """Minimal JSON-RPC client for `codex app-server` over stdio."""

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._notification_task: asyncio.Task[None] | None = None
        self._notification_delivery_tasks: set[asyncio.Task[None]] = set()
        self._server_request_tasks: set[asyncio.Task[None]] = set()
        self._notification_locks: dict[str, asyncio.Lock] = {}
        self._notification_active_partitions: set[str] = set()
        self._notification_partition_backlog: dict[
            str, deque[tuple[str, dict[str, Any]]]
        ] = {}
        self._notification_partition_backlog_size = 0
        self._notification_backlog_available = asyncio.Event()
        self._notification_delivery_slots = asyncio.Semaphore(
            APP_SERVER_NOTIFICATION_QUEUE_MAXSIZE
        )
        self._notification_queue: asyncio.Queue[tuple[str, dict[str, Any]]] = (
            asyncio.Queue(maxsize=APP_SERVER_NOTIFICATION_QUEUE_MAXSIZE)
        )
        self._notification_overflow: deque[tuple[str, dict[str, Any]]] = deque(
            maxlen=APP_SERVER_NOTIFICATION_OVERFLOW_MAXSIZE
        )
        self._write_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._recycle_lock = asyncio.Lock()
        self._request_lifecycle_lock = asyncio.Lock()
        self._request_id = 1
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._in_flight_mutation_requests: dict[asyncio.Task[Any], str] = {}
        self._dispatched_mutation_generations: dict[asyncio.Task[Any], int] = {}
        self._uncertain_mutation_generations: set[int] = set()

        self._notification_handler: NotificationHandler | None = None
        self._server_request_handler: ServerRequestHandler | None = None
        self._transport_reset_handler: TransportResetHandler | None = None

        self._active_turns: dict[str, str] = {}
        self._thread_token_usage: dict[str, dict[str, Any]] = {}
        self._rate_limits: dict[str, Any] | None = None
        self._initialized = False
        self._server_user_agent = ""
        self._transport_needs_restart = False
        self._transport_epoch = uuid.uuid4().hex
        self._transport_epoch_started_at = time.time()
        self._transport_generation = 0
        self._stop_sequence = 0
        self._transport_reset_sequence = 0
        self._last_transport_reset_generation = 0
        self._last_transport_reset_reason = ""
        self._last_transport_reset_at = 0.0
        self._systemd_unit_name = ""

    @staticmethod
    def transport_prefers_app_server() -> bool:
        if config.runtime_mode == "app_server_only":
            return True
        return config.codex_transport in {"app_server", "auto"}

    @staticmethod
    def _resolve_codex_binary() -> str:
        try:
            parts = shlex.split(config.assistant_command)
        except ValueError:
            parts = []

        candidate = parts[0] if parts else "codex"
        if Path(candidate).is_file():
            return candidate

        resolved = shutil.which(candidate)
        if resolved:
            return resolved

        fallback = shutil.which("codex")
        if fallback:
            return fallback

        raise CodexAppServerError("Codex CLI executable not found in PATH")

    def _app_server_argv(self) -> list[str]:
        argv = [self._resolve_codex_binary(), "app-server", "--listen", "stdio://"]

        for feature_name in APP_SERVER_ENABLED_FEATURES:
            argv.extend(["--enable", feature_name])

        # By default, Codex may sandbox tool execution (read-only + no network),
        # which breaks common workflows like `git pull`. Configure the app-server
        # process to inherit the desired sandbox mode.
        sandbox_mode = getattr(config, "codex_sandbox_mode", "").strip()
        if sandbox_mode:
            argv.extend(["-c", f'sandbox_mode="{sandbox_mode}"'])

        if self._systemd_run_enabled():
            unit_name = self._new_systemd_unit_name()
            self._systemd_unit_name = unit_name
            wrapped = [
                "systemd-run",
                "--user",
                "--pipe",
                "--quiet",
                "--collect",
                f"--unit={unit_name}",
            ]
            for property_value in self._systemd_run_properties():
                wrapped.extend(["-p", property_value])
            return [*wrapped, *argv]

        return argv

    @staticmethod
    def _systemd_run_enabled() -> bool:
        raw = env_alias(
            "COCO_CODEX_APP_SERVER_SYSTEMD_RUN",
            default="false",
        ).strip().lower()
        return raw in {"1", "true", "yes", "on"}

    @staticmethod
    def _systemd_run_properties() -> list[str]:
        raw = env_alias("COCO_CODEX_APP_SERVER_SYSTEMD_PROPERTIES", default="")
        return [part.strip() for part in raw.split(",") if part.strip()]

    @staticmethod
    def _new_systemd_unit_name() -> str:
        return f"coco-codex-app-server-{os.getpid()}-{int(time.time() * 1000)}"

    @staticmethod
    def _systemd_user_env() -> dict[str, str]:
        env = os.environ.copy()
        runtime_dir = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        env.setdefault("XDG_RUNTIME_DIR", runtime_dir)
        env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime_dir}/bus")
        return env

    def _app_server_env(self) -> dict[str, str] | None:
        if not self._systemd_run_enabled():
            return None
        return self._systemd_user_env()

    async def set_handlers(
        self,
        *,
        notification_handler: NotificationHandler | None = None,
        server_request_handler: ServerRequestHandler | None = None,
        transport_reset_handler: TransportResetHandler | None = None,
    ) -> None:
        self._notification_handler = notification_handler
        self._server_request_handler = server_request_handler
        self._transport_reset_handler = transport_reset_handler

    async def ensure_started(self) -> None:
        if self._is_transport_ready():
            return

        async with self._start_lock:
            await self._ensure_started_locked()

    async def _ensure_started_locked(self) -> None:
        """Start and initialize a transport while holding ``_start_lock``."""
        recovery_attempted = False
        while True:
            if self._is_transport_ready():
                return

            if self._proc and self._proc.returncode is not None:
                stale_generation = self._transport_generation
                logger.warning(
                    "Replacing exited Codex app-server transport "
                    "(generation=%d returncode=%s)",
                    stale_generation,
                    self._proc.returncode,
                )
                await self._stop_locked()
                await self._notify_transport_reset(
                    "process_exited",
                    stale_generation,
                )

            if self._proc and self._proc.returncode is None and (
                self._transport_needs_restart or not self._initialized
            ):
                stale_generation = self._transport_generation
                logger.warning(
                    "Recycling unhealthy Codex app-server transport "
                    "(initialized=%s, needs_restart=%s)",
                    self._initialized,
                    self._transport_needs_restart,
                )
                await self._stop_locked()
                await self._notify_transport_reset(
                    "unhealthy_transport",
                    stale_generation,
                )

            if not self._proc or self._proc.returncode is not None:
                argv = self._app_server_argv()
                logger.info("Starting Codex app-server: %s", argv)
                try:
                    spawn_kwargs: dict[str, Any] = {
                        "stdin": asyncio.subprocess.PIPE,
                        "stdout": asyncio.subprocess.PIPE,
                        "stderr": asyncio.subprocess.PIPE,
                        "limit": APP_SERVER_STREAM_LIMIT,
                    }
                    app_server_env = self._app_server_env()
                    if app_server_env is not None:
                        spawn_kwargs["env"] = app_server_env
                    self._proc = await asyncio.create_subprocess_exec(
                        *argv,
                        **spawn_kwargs,
                    )
                except OSError as e:
                    raise CodexAppServerError(
                        f"Failed to start codex app-server: {e}"
                    ) from e

                self._remember_owned_pid(self._proc.pid)
                self._transport_generation += 1
                self._uncertain_mutation_generations.clear()
                self._initialized = False
                self._server_user_agent = ""
                self._transport_needs_restart = False
                self._reader_task = asyncio.create_task(self._reader_loop())
                self._stderr_task = asyncio.create_task(self._stderr_loop())
                self._ensure_notification_worker()

            try:
                await self._run_initialize_handshake()
                self._clear_start_failure_state()
                return
            except Exception as exc:
                await self._stop_locked()
                if recovery_attempted:
                    raise
                if not await self._attempt_recovery_after_start_failure(exc):
                    raise
                recovery_attempted = True

    def is_running(self) -> bool:
        """Return whether the app-server process is currently running."""
        return bool(self._proc and self._proc.returncode is None)

    async def stop(self) -> None:
        # Signal shutdown intent before waiting for the lifecycle lock. Timeout
        # recyclers already queued on another lock must not restart afterward.
        # This increment happens before the first await. Request dispatch checks
        # it immediately before the synchronous stdin write, so an older queued
        # request cannot cross the shutdown boundary.
        self._stop_sequence += 1
        async with self._start_lock:
            stopped_generation = self._transport_generation
            had_transport_state = bool(
                self._proc is not None
                or self._initialized
                or self._active_turns
            )
            await self._stop_locked()
            if had_transport_state:
                await self._notify_transport_reset(
                    "explicit_stop",
                    stopped_generation,
                )

    async def _stop_locked(self) -> None:
        """Stop one transport lifecycle while holding ``_start_lock``."""
        proc = self._proc
        self._proc = None
        systemd_unit_name = self._systemd_unit_name
        self._systemd_unit_name = ""

        current_task = asyncio.current_task()
        lifecycle_tasks = [
            task
            for task in (
                self._reader_task,
                self._stderr_task,
                self._notification_task,
            )
            if task is not None and task is not current_task
        ]
        for task in lifecycle_tasks:
            task.cancel()
        self._reader_task = None
        self._stderr_task = None
        self._notification_task = None
        delivery_tasks = [
            task
            for task in self._notification_delivery_tasks
            if task is not current_task
        ]
        server_request_tasks = [
            task
            for task in self._server_request_tasks
            if task is not current_task
        ]
        self._notification_partition_backlog.clear()
        self._notification_partition_backlog_size = 0
        self._notification_backlog_available.set()
        for task in delivery_tasks:
            task.cancel()
        for task in server_request_tasks:
            task.cancel()
        tasks_to_wait = [
            *lifecycle_tasks,
            *delivery_tasks,
            *server_request_tasks,
        ]
        if tasks_to_wait:
            await asyncio.gather(*tasks_to_wait, return_exceptions=True)
        self._notification_delivery_tasks.clear()
        self._server_request_tasks.clear()
        self._notification_locks.clear()
        self._notification_active_partitions.clear()
        # Drop any queued notifications from the previous process lifecycle.
        self._notification_queue = asyncio.Queue(maxsize=APP_SERVER_NOTIFICATION_QUEUE_MAXSIZE)
        self._notification_overflow.clear()

        if systemd_unit_name:
            await self._stop_systemd_unit(systemd_unit_name)

        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        if proc and getattr(proc, "pid", None):
            self._forget_owned_pid(proc.pid)

        for key, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(CodexAppServerError("codex app-server stopped"))
            self._pending.pop(key, None)

        self._active_turns.clear()
        self._thread_token_usage.clear()
        self._initialized = False
        self._server_user_agent = ""
        self._transport_needs_restart = False
        self._uncertain_mutation_generations.clear()

    async def _notify_transport_reset(self, reason: str, generation: int) -> None:
        self._transport_reset_sequence += 1
        self._last_transport_reset_generation = generation
        self._last_transport_reset_reason = reason
        self._last_transport_reset_at = time.time()
        handler = self._transport_reset_handler
        if handler is None:
            return
        try:
            await handler(reason, generation)
        except Exception:
            logger.exception(
                "Codex app-server transport reset handler failed "
                "(reason=%s generation=%d)",
                reason,
                generation,
            )

    def transport_state_snapshot(self) -> dict[str, Any]:
        """Return monotonic lifecycle state for remote reset reconciliation."""
        return {
            "epoch": self._transport_epoch,
            "epoch_started_at": self._transport_epoch_started_at,
            "generation": self._transport_generation,
            "reset_sequence": self._transport_reset_sequence,
            "last_reset_generation": self._last_transport_reset_generation,
            "last_reset_reason": self._last_transport_reset_reason,
            "last_reset_at": self._last_transport_reset_at,
        }

    async def _stop_systemd_unit(self, unit_name: str) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemctl",
                "--user",
                "stop",
                unit_name,
                stdout=subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=self._systemd_user_env(),
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except TimeoutError:
                proc.kill()
                await proc.wait()
            if proc.returncode not in {0, None}:
                stderr = b""
                if proc.stderr is not None:
                    stderr = await proc.stderr.read()
                detail = stderr.decode("utf-8", errors="replace").strip()
                logger.warning(
                    "Failed to stop Codex app-server systemd unit %s: %s",
                    unit_name,
                    detail or f"exit {proc.returncode}",
                )
        except Exception:
            logger.warning(
                "Failed to stop Codex app-server systemd unit %s",
                unit_name,
                exc_info=True,
            )

    @staticmethod
    def _load_start_failure_state() -> dict[str, Any]:
        try:
            raw = json.loads(_APP_SERVER_START_FAILURE_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception:
            logger.debug(
                "Failed reading app-server startup failure state from %s",
                _APP_SERVER_START_FAILURE_FILE,
                exc_info=True,
            )
            return {}
        return raw if isinstance(raw, dict) else {}

    @staticmethod
    def _clear_start_failure_state() -> None:
        try:
            _APP_SERVER_START_FAILURE_FILE.unlink()
        except FileNotFoundError:
            return
        except Exception:
            logger.debug(
                "Failed clearing app-server startup failure state %s",
                _APP_SERVER_START_FAILURE_FILE,
                exc_info=True,
            )

    @staticmethod
    def _load_owned_pid_state() -> dict[int, tuple[str, int]]:
        path = _APP_SERVER_OWNED_PIDS_DIR
        if not path.is_dir():
            return {}
        owned: dict[int, tuple[str, int]] = {}
        try:
            entries = list(path.iterdir())
        except Exception:
            logger.debug(
                "Failed listing owned app-server pid state from %s",
                path,
                exc_info=True,
            )
            return {}
        for entry in entries:
            if entry.suffix != ".pid":
                continue
            try:
                pid = int(entry.stem)
            except (TypeError, ValueError):
                continue
            if pid > 0:
                try:
                    raw_marker = entry.read_text(encoding="utf-8").strip()
                except OSError:
                    raw_marker = ""
                marker_identity = raw_marker
                owner_pid = 0
                if raw_marker:
                    try:
                        payload = json.loads(raw_marker)
                    except json.JSONDecodeError:
                        payload = None
                    if isinstance(payload, dict):
                        marker_identity = str(payload.get("identity", "")).strip()
                        try:
                            owner_pid = int(payload.get("owner_pid", 0) or 0)
                        except (TypeError, ValueError):
                            owner_pid = 0
                owned[pid] = (marker_identity, owner_pid)
        return owned

    @staticmethod
    def _owned_pid_marker(pid: int) -> Path:
        return _APP_SERVER_OWNED_PIDS_DIR / f"{pid}.pid"

    @staticmethod
    def _read_process_identity(pid: int) -> str:
        if os.name == "nt" or pid <= 0:
            return ""
        try:
            stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except OSError:
            return ""
        try:
            after_comm = stat_text.rsplit(") ", 1)[1]
            fields = after_comm.split()
            return fields[19]
        except (IndexError, ValueError):
            return ""

    @classmethod
    def _remember_owned_pid(cls, pid: int | None) -> None:
        if not pid or pid <= 0:
            return
        path = cls._owned_pid_marker(pid)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(
                path,
                {
                    "identity": cls._read_process_identity(pid),
                    "owner_pid": os.getpid(),
                },
            )
        except Exception:
            logger.debug(
                "Failed recording owned app-server pid state %s",
                path,
                exc_info=True,
            )

    @classmethod
    def _forget_owned_pid(cls, pid: int | None) -> None:
        if not pid or pid <= 0:
            return
        path = cls._owned_pid_marker(pid)
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except Exception:
            logger.debug(
                "Failed clearing owned app-server pid state %s",
                path,
                exc_info=True,
            )

    @staticmethod
    def _record_start_failure() -> int:
        now = time.time()
        state = CodexAppServerClient._load_start_failure_state()
        first_failure = float(state.get("first_failure_ts", now))
        count = int(state.get("count", 0))
        if now - first_failure > _APP_SERVER_START_FAILURE_WINDOW_SECONDS:
            first_failure = now
            count = 0
        count += 1
        try:
            atomic_write_json(
                _APP_SERVER_START_FAILURE_FILE,
                {
                    "first_failure_ts": first_failure,
                    "last_failure_ts": now,
                    "count": count,
                },
            )
        except Exception:
            logger.debug(
                "Failed writing app-server startup failure state %s",
                _APP_SERVER_START_FAILURE_FILE,
                exc_info=True,
            )
        return count

    @staticmethod
    def _should_force_recover_start_failure(err: Exception) -> bool:
        if not isinstance(err, CodexAppServerError):
            return False
        text = str(err).lower()
        return (
            "database is locked" in text
            or "codex app-server disconnected" in text
            or "timed out waiting for app-server response: initialize" in text
        )

    @staticmethod
    def _codex_home() -> Path:
        raw = env_alias("CODEX_HOME", default="").strip()
        if raw:
            return Path(raw).expanduser()
        return Path.home() / ".codex"

    @staticmethod
    def _codex_logs_db_recovery_enabled() -> bool:
        raw = env_alias(
            "COCO_CODEX_APP_SERVER_RECOVER_LOGS_DB",
            default="false",
        ).strip().lower()
        return raw in _CODEX_LOGS_DB_RECOVERY_TRUE_VALUES

    @staticmethod
    def _path_size(path: Path) -> int:
        try:
            return path.stat().st_size
        except FileNotFoundError:
            return 0

    @staticmethod
    def _new_codex_logs_db_backup_dir(codex_home: Path) -> Path:
        timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        base = codex_home / f"logs-sqlite-backup-{timestamp}"
        for suffix in range(1000):
            candidate = base if suffix == 0 else codex_home / f"{base.name}-{suffix}"
            try:
                candidate.mkdir(parents=True, exist_ok=False)
                return candidate
            except FileExistsError:
                continue
        raise CodexAppServerError(
            f"Unable to create Codex logs SQLite backup directory under {codex_home}"
        )

    @staticmethod
    def _recover_oversized_codex_logs_db(codex_home: Path | None = None) -> Path | None:
        if not CodexAppServerClient._codex_logs_db_recovery_enabled():
            return None

        codex_home = codex_home or CodexAppServerClient._codex_home()
        logs_db = codex_home / _CODEX_LOGS_DB_NAME
        logs_wal = codex_home / f"{_CODEX_LOGS_DB_NAME}-wal"
        logs_shm = codex_home / f"{_CODEX_LOGS_DB_NAME}-shm"
        db_size = CodexAppServerClient._path_size(logs_db)
        wal_size = CodexAppServerClient._path_size(logs_wal)
        if (
            db_size < _CODEX_LOGS_DB_RECOVERY_MIN_BYTES
            and wal_size < _CODEX_LOGS_DB_WAL_RECOVERY_MIN_BYTES
        ):
            return None

        backup_dir = CodexAppServerClient._new_codex_logs_db_backup_dir(codex_home)
        moved: list[tuple[Path, Path]] = []
        try:
            for path in (logs_db, logs_wal, logs_shm):
                target = backup_dir / path.name
                try:
                    path.replace(target)
                    moved.append((path, target))
                except FileNotFoundError:
                    continue
        except Exception:
            rollback_errors: list[Exception] = []
            for original, target in reversed(moved):
                try:
                    target.replace(original)
                except Exception as rollback_exc:
                    rollback_errors.append(rollback_exc)
            if not rollback_errors:
                try:
                    backup_dir.rmdir()
                except OSError:
                    pass
            else:
                logger.error(
                    "Failed rolling back %d Codex logs SQLite quarantine move(s)",
                    len(rollback_errors),
                )
            raise

        if not moved:
            try:
                backup_dir.rmdir()
            except OSError:
                pass
            return None

        logger.warning(
            (
                "Quarantined oversized Codex logs SQLite files to %s after "
                "app-server startup failures; %s=%d bytes, wal=%d bytes, moved=%s"
            ),
            backup_dir,
            _CODEX_LOGS_DB_NAME,
            db_size,
            wal_size,
            ",".join(original.name for original, _target in moved),
        )
        return backup_dir

    async def _attempt_recovery_after_start_failure(self, err: Exception) -> bool:
        failure_count = self._record_start_failure()
        emit_recovery = (
            failure_count >= _APP_SERVER_START_FAILURE_RESET_THRESHOLD
            and self._should_force_recover_start_failure(err)
        )
        if not emit_recovery:
            return False

        logger.warning(
            "Codex app-server startup failed %d times in %ds; reaping stale local app-server processes and retrying once",
            failure_count,
            _APP_SERVER_START_FAILURE_WINDOW_SECONDS,
        )
        self._clear_start_failure_state()
        self._reap_stale_local_app_server_processes()
        if self._codex_logs_db_recovery_enabled():
            if await self._wait_for_local_app_servers_to_exit():
                try:
                    self._recover_oversized_codex_logs_db()
                except Exception:
                    logger.warning(
                        (
                            "Failed recovering oversized Codex logs SQLite files after "
                            "app-server startup failures"
                        ),
                        exc_info=True,
                    )
            else:
                logger.warning(
                    "Skipping Codex logs SQLite quarantine because an app-server process is still live"
                )
        await asyncio.sleep(0.5)
        return True

    def _running_local_app_server_pids(self) -> set[int] | None:
        """Return local app-server PIDs, or None when liveness cannot be proven."""
        if os.name == "nt":
            return None
        try:
            proc = subprocess.run(
                ["ps", "-eo", "pid=,args="],
                check=False,
                capture_output=True,
                text=True,
            )
        except Exception:
            logger.warning("Failed checking app-server exit state", exc_info=True)
            return None
        if proc.returncode != 0:
            return None

        pids: set[int] = set()
        for raw_line in proc.stdout.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                pid_text, args = line.split(None, 1)
                pid = int(pid_text)
            except (ValueError, TypeError):
                continue
            if self._is_codex_app_server_command(args):
                pids.add(pid)
        return pids

    @staticmethod
    def _is_codex_app_server_command(args: str) -> bool:
        """Recognize direct and node-launched Codex app-server commands."""
        if "--listen stdio://" not in args:
            return False
        try:
            argv = shlex.split(args)
        except ValueError:
            argv = args.split()
        if "app-server" not in argv:
            return False
        for token in argv:
            normalized = token.replace("\\", "/").lower()
            executable = normalized.rsplit("/", 1)[-1]
            if executable in {"codex", "codex.exe", "codex.js", "codex.mjs"}:
                return True
            if "/@openai/codex/" in normalized:
                return True
        return False

    async def _wait_for_local_app_servers_to_exit(self) -> bool:
        """Wait until no local Codex app-server can still write the shared logs DB."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _CODEX_LOGS_DB_PROCESS_EXIT_TIMEOUT_SECONDS
        while True:
            pids = self._running_local_app_server_pids()
            if pids == set():
                return True
            if pids is None or loop.time() >= deadline:
                return False
            await asyncio.sleep(_CODEX_LOGS_DB_PROCESS_EXIT_POLL_SECONDS)

    def _reap_stale_systemd_app_server_units(self) -> None:
        if not self._systemd_run_enabled():
            return
        try:
            proc = subprocess.run(
                ["systemctl", "--user", "list-units", "coco-codex-app-server-*"],
                check=False,
                capture_output=True,
                text=True,
                env=self._systemd_user_env(),
            )
        except Exception:
            logger.warning("Failed listing stale systemd app-server units", exc_info=True)
            return

        current_unit = self._systemd_unit_name
        stopped = 0
        for raw_line in proc.stdout.splitlines():
            parts = raw_line.split()
            if not parts:
                continue
            unit_name = parts[0].strip()
            if not unit_name.startswith("coco-codex-app-server-"):
                continue
            if current_unit and unit_name == current_unit:
                continue
            try:
                stop_proc = subprocess.run(
                    ["systemctl", "--user", "stop", unit_name],
                    check=False,
                    capture_output=True,
                    text=True,
                    env=self._systemd_user_env(),
                )
                if stop_proc.returncode == 0:
                    stopped += 1
                else:
                    detail = (stop_proc.stderr or stop_proc.stdout or "").strip()
                    logger.warning(
                        "Failed stopping stale app-server unit %s: %s",
                        unit_name,
                        detail or f"exit {stop_proc.returncode}",
                    )
            except Exception:
                logger.warning(
                    "Failed stopping stale app-server unit %s",
                    unit_name,
                    exc_info=True,
                )
        if stopped:
            logger.warning(
                "App-server recovery stopped %d stale systemd app-server unit(s)",
                stopped,
            )

    def _reap_stale_local_app_server_processes(self) -> None:
        self._reap_stale_systemd_app_server_units()

        if os.name == "nt":
            logger.warning(
                "Automatic stale app-server reap is not implemented on Windows; skipping"
            )
            return
        owned_pids = self._load_owned_pid_state()
        try:
            proc = subprocess.run(
                ["ps", "-eo", "pid=,ppid=,args="],
                check=False,
                capture_output=True,
                text=True,
            )
        except Exception:
            logger.warning("Failed listing local processes for app-server recovery", exc_info=True)
            return

        current_pid = getattr(self._proc, "pid", None)
        app_server_rows: list[tuple[int, int]] = []
        app_server_ppids: dict[int, int] = {}
        running_app_server_pids: set[int] = set()
        reused_marker_pids: set[int] = set()
        for raw_line in proc.stdout.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                pid_text, ppid_text, args = line.split(None, 2)
            except ValueError:
                continue
            if not self._is_codex_app_server_command(args):
                continue
            try:
                pid = int(pid_text)
                ppid = int(ppid_text)
            except ValueError:
                continue
            app_server_rows.append((pid, ppid))
            app_server_ppids[pid] = ppid
            running_app_server_pids.add(pid)

        for pid, (marker_identity, _owner_pid) in list(owned_pids.items()):
            if pid not in running_app_server_pids:
                self._forget_owned_pid(pid)
                owned_pids.pop(pid, None)
                continue
            current_identity = self._read_process_identity(pid)
            if marker_identity and current_identity and marker_identity != current_identity:
                self._forget_owned_pid(pid)
                owned_pids.pop(pid, None)
                reused_marker_pids.add(pid)

        live_foreign_owner_present = any(
            owner_pid > 0
            and owner_pid != os.getpid()
            and app_server_ppids.get(pid) == owner_pid
            for pid, (_marker_identity, owner_pid) in owned_pids.items()
        )

        if not owned_pids:
            logger.warning(
                "No live owned app-server pids recorded for recovery reap; falling back to orphaned app-server scan"
            )
        compat_reap_orphans = not live_foreign_owner_present

        killed = 0
        for pid, ppid in app_server_rows:
            if current_pid and pid == current_pid:
                continue
            marker = owned_pids.get(pid)
            if marker is None:
                if pid in reused_marker_pids or not compat_reap_orphans or ppid > 1:
                    continue
            else:
                _marker_identity, owner_pid = marker
                if owner_pid == os.getpid():
                    pass
                elif owner_pid > 0 and ppid == owner_pid:
                    continue
                elif ppid > 1:
                    continue
            try:
                os.kill(pid, signal.SIGTERM)
                killed += 1
            except ProcessLookupError:
                self._forget_owned_pid(pid)
                continue
            except Exception:
                logger.warning("Failed terminating stale codex app-server pid=%s", pid, exc_info=True)
        logger.warning("App-server recovery reaped %d stale local codex app-server process(es)", killed)

    def _is_transport_ready(self) -> bool:
        if not self._proc or self._proc.returncode is not None:
            return False
        if not self._initialized:
            return False
        if self._transport_needs_restart:
            return False
        return True

    async def _stderr_loop(self) -> None:
        proc = self._proc
        if not proc or not proc.stderr:
            return

        try:
            while True:
                raw = await proc.stderr.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    logger.debug("codex app-server stderr: %s", line)
        except asyncio.CancelledError:
            pass

    async def _read_stdout_line(self) -> bytes | None:
        proc = self._proc
        if not proc or not proc.stdout:
            return None

        stream = proc.stdout
        discarded = 0
        while True:
            try:
                line = await stream.readuntil(b"\n")
            except asyncio.LimitOverrunError as e:
                # An oversized JSONL payload exceeded StreamReader's line limit.
                # Drop this line in chunks, then continue processing subsequent
                # messages instead of crashing the reader loop.
                self._transport_needs_restart = True
                consume = max(int(getattr(e, "consumed", 0)), 1)
                try:
                    await stream.readexactly(consume)
                except asyncio.IncompleteReadError:
                    return None
                discarded += consume
                continue
            except asyncio.IncompleteReadError as e:
                if discarded:
                    discarded += len(e.partial)
                    self._transport_needs_restart = True
                    logger.warning(
                        "Discarded oversized app-server line (%d bytes)", discarded
                    )
                    return b"\n"
                if e.partial:
                    return e.partial
                return None

            if discarded:
                discarded += len(line)
                self._transport_needs_restart = True
                logger.warning("Discarded oversized app-server line (%d bytes)", discarded)
                return b"\n"
            return line

    async def _read_one_message(self) -> dict[str, Any] | None:
        proc = self._proc
        if not proc or not proc.stdout:
            return None

        # Support both LSP-style framed JSON-RPC and plain JSONL.
        line = await self._read_stdout_line()
        if not line:
            return None

        stripped = line.strip()
        if not stripped:
            return {}

        if stripped.lower().startswith(b"content-length:"):
            try:
                length = int(stripped.split(b":", 1)[1].strip())
            except Exception:
                logger.debug("Invalid Content-Length header from app-server: %r", stripped)
                return {}

            # Consume header lines until the empty separator line.
            while True:
                hdr = await self._read_stdout_line()
                if not hdr:
                    return None
                if hdr in (b"\n", b"\r\n"):
                    break

            try:
                payload = await proc.stdout.readexactly(length)
            except asyncio.IncompleteReadError:
                return None
            text = payload.decode("utf-8", errors="replace")
        else:
            text = stripped.decode("utf-8", errors="replace")

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.debug("Failed parsing app-server JSON payload: %r", text[:400])
            return {}

        if isinstance(data, dict):
            return data
        return {}

    async def _reader_loop(self) -> None:
        generation = self._transport_generation
        try:
            while True:
                msg = await self._read_one_message()
                if msg is None:
                    break
                if not msg:
                    continue
                await self._handle_message(msg)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.exception("codex app-server reader loop failed: %s", e)
        finally:
            if generation != self._transport_generation:
                return
            if self._proc and self._proc.returncode is None:
                self._transport_needs_restart = True
            for key, fut in list(self._pending.items()):
                if not fut.done():
                    fut.set_exception(CodexAppServerError("codex app-server disconnected"))
                self._pending.pop(key, None)

    def _ensure_notification_worker(self) -> None:
        if self._notification_task and not self._notification_task.done():
            return
        self._notification_task = asyncio.create_task(self._notification_loop())

    async def _notification_loop(self) -> None:
        try:
            while True:
                if not self._notification_queue.empty():
                    method, params = await self._notification_queue.get()
                elif self._notification_overflow:
                    method, params = self._notification_overflow.popleft()
                else:
                    method, params = await self._notification_queue.get()
                if not self._notification_handler:
                    continue
                # Preserve ordering within one Codex thread while allowing an
                # unrelated topic to bypass slow Telegram I/O or completion
                # grace periods in another thread.
                partition = self._notification_partition(params)
                if partition in self._notification_active_partitions:
                    await self._append_notification_partition_backlog(
                        partition,
                        method,
                        params,
                    )
                    continue
                await self._notification_delivery_slots.acquire()
                self._notification_active_partitions.add(partition)
                self._start_notification_delivery(partition, method, params)
        except asyncio.CancelledError:
            return

    @staticmethod
    def _notification_partition(params: dict[str, Any]) -> str:
        thread_id = params.get("threadId")
        if isinstance(thread_id, str) and thread_id:
            return thread_id
        return "__global__"

    def _start_notification_delivery(
        self,
        partition: str,
        method: str,
        params: dict[str, Any],
    ) -> None:
        task = asyncio.create_task(self._deliver_notification(method, params))
        self._notification_delivery_tasks.add(task)
        task.add_done_callback(
            lambda completed, key=partition: self._notification_delivery_done(
                completed, key
            )
        )

    async def _append_notification_partition_backlog(
        self,
        partition: str,
        method: str,
        params: dict[str, Any],
    ) -> None:
        limit = APP_SERVER_NOTIFICATION_QUEUE_MAXSIZE
        while True:
            backlog = self._notification_partition_backlog.get(partition)
            if self._notification_partition_backlog_size < limit:
                if backlog is None:
                    backlog = deque()
                    self._notification_partition_backlog[partition] = backlog
                backlog.append((method, params))
                self._notification_partition_backlog_size += 1
                return

            if method in APP_SERVER_NOTIFICATION_DROP_WHEN_FULL:
                if backlog is not None:
                    for index in range(len(backlog) - 1, -1, -1):
                        if backlog[index][0] == method:
                            backlog[index] = (method, params)
                            logger.debug(
                                "Coalesced %s notification in full global backlog for %s",
                                method,
                                partition,
                            )
                            return
                logger.warning(
                    "Dropping %s notification from full global backlog for %s",
                    method,
                    partition,
                )
                return

            evicted = False
            for backlog_partition, candidate_backlog in list(
                self._notification_partition_backlog.items()
            ):
                for item in candidate_backlog:
                    if item[0] not in APP_SERVER_NOTIFICATION_DROP_WHEN_FULL:
                        continue
                    candidate_backlog.remove(item)
                    self._notification_partition_backlog_size -= 1
                    if not candidate_backlog:
                        self._notification_partition_backlog.pop(backlog_partition, None)
                    evicted = True
                    break
                if evicted:
                    break
            if evicted:
                backlog = self._notification_partition_backlog.setdefault(
                    partition, deque()
                )
                backlog.append((method, params))
                self._notification_partition_backlog_size += 1
                logger.warning(
                    "Evicted replaceable notification from global backlog for %s",
                    method,
                )
                return

            # Every queued item is lifecycle/final output. Preserve it and
            # apply backpressure until the active delivery advances.
            self._notification_backlog_available.clear()
            if self._notification_partition_backlog_size >= limit:
                await self._notification_backlog_available.wait()

    def _notification_delivery_done(
        self,
        task: asyncio.Task[None],
        partition: str,
    ) -> None:
        self._notification_delivery_tasks.discard(task)
        backlog = self._notification_partition_backlog.get(partition)
        if backlog:
            method, params = backlog.popleft()
            self._notification_partition_backlog_size -= 1
            self._notification_backlog_available.set()
            if not backlog:
                self._notification_partition_backlog.pop(partition, None)
                self._notification_locks.pop(partition, None)
            self._start_notification_delivery(partition, method, params)
            return
        self._notification_locks.pop(partition, None)
        self._notification_active_partitions.discard(partition)
        self._notification_delivery_slots.release()

    async def _deliver_notification(
        self,
        method: str,
        params: dict[str, Any],
    ) -> None:
        transport_context = params.get(INTERNAL_TRANSPORT_CONTEXT_KEY)
        if isinstance(transport_context, dict):
            context_epoch = str(transport_context.get("epoch", "")).strip()
            try:
                context_generation = int(
                    transport_context.get("generation", 0) or 0
                )
            except (TypeError, ValueError):
                context_generation = 0
            if (
                context_epoch != self._transport_epoch
                or context_generation != self._transport_generation
            ):
                logger.debug(
                    "Dropping stale app-server notification "
                    "(method=%s generation=%d current=%d)",
                    method,
                    context_generation,
                    self._transport_generation,
                )
                return
        partition = self._notification_partition(params)
        lock = self._notification_locks.setdefault(partition, asyncio.Lock())
        async with lock:
            handler = self._notification_handler
            if not handler:
                return
            try:
                await handler(method, params)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("app-server notification handler failed: %s", method)

    def _drop_replaceable_queued_notification(self) -> bool:
        queue_items = getattr(self._notification_queue, "_queue", None)
        if queue_items is None:
            return False
        for item in list(queue_items):
            try:
                method, _params = item
            except (TypeError, ValueError):
                continue
            if method in APP_SERVER_NOTIFICATION_DROP_WHEN_FULL:
                queue_items.remove(item)
                return True
        return False

    def _enqueue_notification(self, method: str, params: dict[str, Any]) -> bool:
        if self._notification_overflow:
            if method in APP_SERVER_NOTIFICATION_DROP_WHEN_FULL:
                logger.warning(
                    "app-server notification overflow pending; dropping %s snapshot",
                    method,
                )
                return False
            self._notification_overflow.append((method, params))
            return True
        try:
            self._notification_queue.put_nowait((method, params))
            return True
        except asyncio.QueueFull:
            if method in APP_SERVER_NOTIFICATION_DROP_WHEN_FULL:
                logger.warning(
                    "app-server notification queue full (%d); dropping %s snapshot",
                    self._notification_queue.maxsize,
                    method,
                )
                return False
            if self._drop_replaceable_queued_notification():
                try:
                    self._notification_queue.put_nowait((method, params))
                    logger.warning(
                        "app-server notification queue full (%d); evicted stale snapshot for %s",
                        self._notification_queue.maxsize,
                        method,
                    )
                    return True
                except asyncio.QueueFull:
                    pass
            overflow_was_full = (
                self._notification_overflow.maxlen is not None
                and len(self._notification_overflow) >= self._notification_overflow.maxlen
            )
            self._notification_overflow.append((method, params))
            if overflow_was_full:
                logger.warning(
                    "app-server notification overflow full (%d); dropped oldest item for %s",
                    self._notification_overflow.maxlen,
                    method,
                )
            else:
                logger.warning(
                    "app-server notification queue full (%d); spilling %s notification to overflow (%d/%d)",
                    self._notification_queue.maxsize,
                    method,
                    len(self._notification_overflow),
                    self._notification_overflow.maxlen or 0,
                )
            return True

    async def _handle_message(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        has_id = "id" in msg

        # Server request (method + id)
        if isinstance(method, str) and has_id:
            req_id = msg.get("id")
            params = msg.get("params")
            params_dict = params if isinstance(params, dict) else {}
            generation = self._transport_generation
            request_params = dict(params_dict)
            request_params[INTERNAL_TRANSPORT_CONTEXT_KEY] = (
                self.transport_state_snapshot()
            )
            task = asyncio.create_task(
                self._handle_server_request(
                    req_id=req_id,
                    method=method,
                    params=request_params,
                    generation=generation,
                )
            )
            self._server_request_tasks.add(task)
            task.add_done_callback(self._server_request_tasks.discard)
            return

        # Notification (method, no id)
        if isinstance(method, str):
            params = msg.get("params")
            params_dict = params if isinstance(params, dict) else {}
            self._update_state_from_notification(method, params_dict)
            if self._notification_handler:
                # Do not await notification handling in the read loop: Telegram
                # work (progress edits, etc.) can be slow and would otherwise
                # starve request/response processing, leading to turn/start timeouts.
                self._ensure_notification_worker()
                notification_params = dict(params_dict)
                notification_params[INTERNAL_TRANSPORT_CONTEXT_KEY] = (
                    self.transport_state_snapshot()
                )
                self._enqueue_notification(method, notification_params)
            return

        # Response (id, maybe result/error)
        if has_id:
            req_id = str(msg.get("id"))
            fut = self._pending.pop(req_id, None)
            if not fut:
                return
            if "error" in msg:
                err = msg.get("error")
                if isinstance(err, dict):
                    message = err.get("message") or json.dumps(err)
                else:
                    message = str(err)
                fut.set_exception(CodexAppServerError(message))
                return
            fut.set_result(msg.get("result"))

    async def _handle_server_request(
        self,
        *,
        req_id: Any,
        method: str,
        params: dict[str, Any],
        generation: int,
    ) -> None:
        """Handle one server-initiated request without blocking stdout reads."""
        try:
            result: dict[str, Any] | None = None
            if self._server_request_handler:
                try:
                    result = await self._server_request_handler(method, params)
                except Exception as exc:
                    logger.exception(
                        "app-server request handler failed (%s): %s",
                        method,
                        exc,
                    )
            if result is None:
                result = self._default_server_request_result(method, params)
            if generation != self._transport_generation:
                logger.debug(
                    "Dropping late app-server request response "
                    "(method=%s generation=%d current=%d)",
                    method,
                    generation,
                    self._transport_generation,
                )
                return
            await self._write_response(req_id, result=result)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Failed writing app-server request response (%s)",
                method,
            )

    @staticmethod
    def _default_server_request_result(
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        # Conservative defaults: deny approvals; best-effort answer for request_user_input
        # to avoid protocol deadlocks when no Telegram UI bridge is active.
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            return {"decision": "decline"}

        if method == "item/tool/requestUserInput":
            answers: dict[str, dict[str, list[str]]] = {}
            questions = params.get("questions")
            if isinstance(questions, list):
                for q in questions:
                    if not isinstance(q, dict):
                        continue
                    qid = q.get("id")
                    if not isinstance(qid, str) or not qid:
                        continue
                    choice: list[str] = []
                    options = q.get("options")
                    if isinstance(options, list) and options:
                        first = options[0]
                        if isinstance(first, dict):
                            label = first.get("label")
                            if isinstance(label, str) and label:
                                choice = [label]
                    answers[qid] = {"answers": choice}
            return {"answers": answers}

        # Unknown server request; reply with empty result object.
        return {}

    def _update_state_from_notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "turn/started":
            thread_id = params.get("threadId")
            turn = params.get("turn")
            if isinstance(thread_id, str) and isinstance(turn, dict):
                turn_id = turn.get("id")
                if isinstance(turn_id, str) and turn_id:
                    self._active_turns[thread_id] = turn_id
            return

        if method == "turn/completed":
            thread_id = params.get("threadId")
            turn = params.get("turn")
            if isinstance(thread_id, str):
                status = ""
                if isinstance(turn, dict):
                    st = turn.get("status")
                    status = st if isinstance(st, str) else ""
                if status != "inProgress":
                    self._active_turns.pop(thread_id, None)
            return

        if method == "account/rateLimits/updated":
            snapshot = params.get("rateLimits")
            if isinstance(snapshot, dict):
                self._rate_limits = snapshot
            return

        if method == "thread/tokenUsage/updated":
            thread_id = params.get("threadId")
            token_usage = params.get("tokenUsage")
            if isinstance(thread_id, str) and isinstance(token_usage, dict):
                self._thread_token_usage[thread_id] = token_usage

    async def _begin_mutation_request(self, method: str) -> asyncio.Task[Any]:
        task = asyncio.current_task()
        if task is None:
            raise CodexAppServerError("Mutation request has no asyncio task")
        async with self._request_lifecycle_lock:
            self._in_flight_mutation_requests[task] = method
            self._dispatched_mutation_generations.pop(task, None)
        return task

    async def _finish_mutation_request(
        self,
        task: asyncio.Task[Any],
        *,
        outcome_uncertain: bool,
    ) -> None:
        async with self._request_lifecycle_lock:
            self._in_flight_mutation_requests.pop(task, None)
            dispatched_generation = self._dispatched_mutation_generations.pop(
                task,
                None,
            )
            if outcome_uncertain and dispatched_generation is not None:
                self._uncertain_mutation_generations.add(dispatched_generation)

    def _mark_mutation_request_dispatched(self, method: object) -> None:
        if not isinstance(method, str):
            return
        task = asyncio.current_task()
        if task is None:
            return
        if self._in_flight_mutation_requests.get(task) == method:
            self._dispatched_mutation_generations[task] = (
                self._transport_generation
            )

    def _has_mutation_recycle_fence(self) -> bool:
        return bool(
            self._in_flight_mutation_requests
            or self._transport_generation
            in self._uncertain_mutation_generations
        )

    async def _write_jsonrpc(
        self,
        payload: dict[str, Any],
        *,
        expected_stop_sequence: int | None = None,
        on_dispatch: Callable[[], None] | None = None,
    ) -> None:
        # Codex CLI app-server currently expects JSONL over stdio.
        # Reader remains dual-format to tolerate framed responses.
        raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        frame = raw + b"\n"

        async with self._write_lock:
            if (
                expected_stop_sequence is not None
                and self._stop_sequence != expected_stop_sequence
            ):
                raise CodexAppServerError(
                    "App-server transport changed before request dispatch"
                )
            proc = self._proc
            if not proc or not proc.stdin:
                raise CodexAppServerError("codex app-server is not running")
            proc.stdin.write(frame)
            if on_dispatch is not None:
                on_dispatch()
            self._mark_mutation_request_dispatched(payload.get("method"))
            await proc.stdin.drain()

    async def _write_response(
        self,
        req_id: Any,
        *,
        result: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": result or {},
        }
        await self._write_jsonrpc(payload)

    async def _request_started(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 60.0,
        expected_stop_sequence: int | None = None,
        on_dispatch: Callable[[], None] | None = None,
    ) -> Any:
        if not self._proc or self._proc.returncode is not None:
            raise CodexAppServerError("codex app-server is not running")

        req_id = str(self._request_id)
        self._request_id += 1
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._pending[req_id] = fut

        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params or {},
        }

        try:
            await self._write_jsonrpc(
                payload,
                expected_stop_sequence=expected_stop_sequence,
                on_dispatch=on_dispatch,
            )
            try:
                return await asyncio.wait_for(fut, timeout=timeout)
            except TimeoutError as e:
                raise CodexAppServerError(
                    f"Timed out waiting for app-server response: {method}"
                ) from e
        finally:
            self._pending.pop(req_id, None)

    async def _run_initialize_handshake(self) -> None:
        """Initialize app-server protocol once per process lifecycle."""
        if self._initialized:
            return

        params: dict[str, Any] = {
            "clientInfo": {
                "name": "coco",
                "title": "coco telegram bridge",
                "version": "1",
            },
            "capabilities": {
                "experimentalApi": True,
            },
        }

        result = await self._request_started("initialize", params, timeout=20.0)
        if not isinstance(result, dict):
            raise CodexAppServerError("initialize returned an invalid response payload")
        user_agent = result.get("userAgent")
        if isinstance(user_agent, str) and user_agent.strip():
            self._server_user_agent = user_agent.strip()

        await self._write_jsonrpc(
            {
                "jsonrpc": "2.0",
                "method": "initialized",
            }
        )
        self._initialized = True

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 60.0,
        retry_safe_timeout: bool = True,
        on_dispatch: Callable[[], None] | None = None,
    ) -> Any:
        dispatch_kwargs = (
            {"on_dispatch": on_dispatch} if on_dispatch is not None else {}
        )
        if method in SAFE_TIMEOUT_RECYCLE_METHODS:
            return await self._request_with_timeout_recovery(
                method,
                params,
                timeout=timeout,
                retry_safe_timeout=retry_safe_timeout,
                **dispatch_kwargs,
            )

        mutation_task = await self._begin_mutation_request(method)
        outcome_uncertain = False
        try:
            try:
                return await self._request_with_timeout_recovery(
                    method,
                    params,
                    timeout=timeout,
                    retry_safe_timeout=retry_safe_timeout,
                    **dispatch_kwargs,
                )
            except asyncio.CancelledError:
                outcome_uncertain = True
                raise
            except CodexAppServerError as exc:
                outcome_uncertain = self._is_request_timeout(method, exc)
                raise
        finally:
            await self._finish_mutation_request(
                mutation_task,
                outcome_uncertain=outcome_uncertain,
            )

    async def _request_with_timeout_recovery(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 60.0,
        retry_safe_timeout: bool = True,
        on_dispatch: Callable[[], None] | None = None,
    ) -> Any:
        dispatch_kwargs = (
            {"on_dispatch": on_dispatch} if on_dispatch is not None else {}
        )
        stop_sequence = self._stop_sequence
        await self.ensure_started()
        generation = self._transport_generation
        reset_sequence = self._transport_reset_sequence
        try:
            result = await self._request_started(
                method,
                params,
                timeout=timeout,
                expected_stop_sequence=stop_sequence,
                **dispatch_kwargs,
            )
            self._validate_response_transport_state(
                method=method,
                expected_generation=generation,
                expected_stop_sequence=stop_sequence,
                expected_reset_sequence=reset_sequence,
            )
            return result
        except CodexAppServerError as e:
            if not self._is_request_timeout(method, e):
                raise
            if not retry_safe_timeout:
                # A short health/read probe can time out while the app-server
                # is busy servicing a healthy backend request. Treat that as
                # inconclusive and leave the transport (and active turns)
                # untouched.
                logger.warning(
                    "App-server request timed out (%s); leaving transport "
                    "running because timeout recovery is disabled",
                    method,
                )
                raise
            if method not in SAFE_TIMEOUT_RECYCLE_METHODS:
                logger.warning(
                    "App-server request timed out (%s); preserving shared "
                    "transport because the request outcome is ambiguous",
                    method,
                )
                raise
            if self._has_mutation_recycle_fence():
                logger.warning(
                    "App-server request timed out (%s); preserving shared "
                    "transport because a mutation is in flight or uncertain",
                    method,
                )
                raise
            if self._active_turns:
                logger.warning(
                    "App-server request timed out (%s); preserving shared "
                    "transport because %d turn(s) remain active",
                    method,
                    len(self._active_turns),
                )
                raise
            logger.warning(
                "App-server request timed out (%s); recycling transport",
                method,
            )
            await self._recycle_timed_out_transport(
                expected_generation=generation,
                expected_stop_sequence=stop_sequence,
                method=method,
            )
            if self._stop_sequence != stop_sequence:
                raise
            if method not in SAFE_TIMEOUT_RETRY_METHODS:
                # A timed-out mutation has an unknown outcome. Never replay it
                # at the transport layer, where doing so can duplicate work.
                raise
            retry_generation = self._transport_generation
            retry_reset_sequence = self._transport_reset_sequence
            try:
                result = await self._request_started(
                    method,
                    params,
                    timeout=timeout,
                    expected_stop_sequence=stop_sequence,
                    **dispatch_kwargs,
                )
                self._validate_response_transport_state(
                    method=method,
                    expected_generation=retry_generation,
                    expected_stop_sequence=stop_sequence,
                    expected_reset_sequence=retry_reset_sequence,
                )
                return result
            except CodexAppServerError as retry_error:
                if self._is_request_timeout(method, retry_error):
                    await self._recycle_timed_out_transport(
                        expected_generation=retry_generation,
                        expected_stop_sequence=stop_sequence,
                        method=method,
                    )
                raise

    def _validate_response_transport_state(
        self,
        *,
        method: str,
        expected_generation: int,
        expected_stop_sequence: int,
        expected_reset_sequence: int,
    ) -> None:
        """Reject responses that became stale before their waiter resumed."""
        if (
            self._transport_generation == expected_generation
            and self._stop_sequence == expected_stop_sequence
            and self._transport_reset_sequence == expected_reset_sequence
            and self._is_transport_ready()
        ):
            return
        raise CodexAppServerError(
            f"App-server transport changed while awaiting response: {method}"
        )

    @staticmethod
    def _is_request_timeout(method: str, err: Exception) -> bool:
        if not isinstance(err, CodexAppServerError):
            return False
        return f"Timed out waiting for app-server response: {method}" in str(err)

    async def recover_uncertain_turn_timeout(
        self,
        *,
        method: str,
        thread_id: str = "",
        turn_id: str = "",
    ) -> bool:
        """Replace a poisoned turn transport once, without replaying the request."""
        if method not in {"turn/start", "turn/steer"}:
            raise ValueError(f"Unsupported uncertain turn method: {method}")

        uncertain_thread_id = thread_id.strip() if method == "turn/steer" else ""
        uncertain_turn_id = turn_id.strip() if uncertain_thread_id else ""
        expected_generation = self._transport_generation
        expected_stop_sequence = self._stop_sequence
        recycled = await self._recycle_timed_out_transport(
            expected_generation=expected_generation,
            expected_stop_sequence=expected_stop_sequence,
            method=method,
            recover_uncertain_turn=True,
            uncertain_thread_id=uncertain_thread_id,
            uncertain_turn_id=uncertain_turn_id,
        )
        if recycled:
            return True
        return bool(
            (
                self._transport_generation != expected_generation
                or expected_generation
                not in self._uncertain_mutation_generations
            )
            and self._is_transport_ready()
        )

    async def _recycle_timed_out_transport(
        self,
        *,
        expected_generation: int,
        expected_stop_sequence: int | None = None,
        method: str,
        recover_uncertain_turn: bool = False,
        uncertain_thread_id: str = "",
        uncertain_turn_id: str = "",
    ) -> bool:
        """Recycle one failed generation exactly once across concurrent callers."""
        if expected_stop_sequence is None:
            expected_stop_sequence = self._stop_sequence
        async with self._recycle_lock:
            async with self._request_lifecycle_lock:
                async with self._start_lock:
                    if self._stop_sequence != expected_stop_sequence:
                        return False
                    if self._transport_generation != expected_generation:
                        await self._ensure_started_locked()
                        return False
                    if recover_uncertain_turn and (
                        expected_generation
                        not in self._uncertain_mutation_generations
                    ):
                        # The failed generation was already recovered while this
                        # caller waited. Never recycle its healthy replacement.
                        return self._is_transport_ready()
                    if recover_uncertain_turn and self._in_flight_mutation_requests:
                        logger.warning(
                            "Skipping uncertain app-server timeout recovery (%s); "
                            "%d unrelated mutation(s) remain in flight",
                            method,
                            len(self._in_flight_mutation_requests),
                        )
                        return False
                    unrelated_active_turns = sum(
                        1
                        for active_thread_id, active_turn_id in self._active_turns.items()
                        if not (
                            active_thread_id == uncertain_thread_id
                            and active_turn_id == uncertain_turn_id
                        )
                    )
                    if recover_uncertain_turn and unrelated_active_turns:
                        logger.warning(
                            "Skipping uncertain app-server timeout recovery (%s); "
                            "%d unrelated turn(s) remain active",
                            method,
                            unrelated_active_turns,
                        )
                        return False
                    if (
                        not recover_uncertain_turn
                        and self._has_mutation_recycle_fence()
                    ):
                        logger.warning(
                            "Skipping app-server timeout recycle (%s); a "
                            "mutation became in flight or uncertain before cleanup",
                            method,
                        )
                        return False
                    if not recover_uncertain_turn and self._active_turns:
                        logger.warning(
                            "Skipping app-server timeout recycle (%s); %d turn(s) "
                            "became active before cleanup",
                            method,
                            len(self._active_turns),
                        )
                        return False
                    if recover_uncertain_turn:
                        logger.warning(
                            "Recycling app-server after uncertain %s timeout "
                            "(generation=%d)",
                            method,
                            expected_generation,
                        )
                    await self._stop_locked()
                    await self._notify_transport_reset(
                        (
                            f"uncertain_timeout:{method}"
                            if recover_uncertain_turn
                            else f"request_timeout:{method}"
                        ),
                        expected_generation,
                    )
                    if self._stop_sequence != expected_stop_sequence:
                        return False
                    await self._ensure_started_locked()
                    return True

    async def thread_start(
        self,
        *,
        cwd: str | None = None,
        approval_policy: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        service_tier: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if cwd:
            params["cwd"] = cwd
        if approval_policy:
            params["approvalPolicy"] = approval_policy
        if model:
            params["model"] = model
        if effort:
            params["reasoningEffort"] = effort
        if service_tier:
            params["serviceTier"] = service_tier
        result = await self.request("thread/start", params, timeout=30.0)
        return result if isinstance(result, dict) else {}

    async def thread_fork(
        self,
        *,
        thread_id: str,
        turn_id: str | None = None,
        service_tier: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"threadId": thread_id}
        if turn_id:
            params["turnId"] = turn_id
        if service_tier:
            params["serviceTier"] = service_tier
        result = await self.request("thread/fork", params, timeout=120.0)
        return result if isinstance(result, dict) else {}

    async def thread_resume(
        self,
        *,
        thread_id: str,
        service_tier: str | None = None,
    ) -> dict[str, Any]:
        params = {
            "threadId": thread_id,
            # CoCo only needs lifecycle metadata and current live state. Avoid
            # materializing historical turns into one large JSON response.
            "excludeTurns": True,
        }
        if service_tier:
            params["serviceTier"] = service_tier
        result = await self.request("thread/resume", params, timeout=30.0)
        return result if isinstance(result, dict) else {}

    async def thread_list(
        self,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 100))}
        if cursor:
            params["cursor"] = cursor
        result = await self.request("thread/list", params, timeout=60.0)
        return result if isinstance(result, dict) else {}

    async def thread_read(
        self,
        *,
        thread_id: str,
        timeout: float = 60.0,
        include_turns: bool = True,
    ) -> dict[str, Any]:
        params = {
            "threadId": thread_id,
            "includeTurns": include_turns,
        }
        result = await self.request("thread/read", params, timeout=timeout)
        return result if isinstance(result, dict) else {}

    async def thread_rollback(
        self,
        *,
        thread_id: str,
        num_turns: int | None = None,
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"threadId": thread_id}
        if isinstance(num_turns, int) and num_turns > 0:
            params["numTurns"] = num_turns
        elif turn_id:
            params["turnId"] = turn_id
        result = await self.request("thread/rollback", params, timeout=120.0)
        return result if isinstance(result, dict) else {}

    async def thread_goal_get(
        self,
        *,
        thread_id: str,
    ) -> dict[str, Any]:
        params = {"threadId": thread_id}
        result = await self.request("thread/goal/get", params, timeout=30.0)
        return result if isinstance(result, dict) else {}

    async def thread_goal_set(
        self,
        *,
        thread_id: str,
        goal: str,
    ) -> dict[str, Any]:
        params = {
            "threadId": thread_id,
            "objective": goal,
        }
        result = await self.request("thread/goal/set", params, timeout=60.0)
        return result if isinstance(result, dict) else {}

    async def thread_goal_clear(
        self,
        *,
        thread_id: str,
    ) -> dict[str, Any]:
        params = {"threadId": thread_id}
        result = await self.request("thread/goal/clear", params, timeout=30.0)
        return result if isinstance(result, dict) else {}

    async def turn_start(
        self,
        *,
        thread_id: str,
        inputs: list[dict[str, Any]],
        approval_policy: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        service_tier: str | None = None,
        timeout: float = APP_SERVER_MUTATION_TIMEOUT_SECONDS,
        on_dispatch: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": inputs,
        }
        if approval_policy:
            params["approvalPolicy"] = approval_policy
        if model:
            params["model"] = model
        if effort:
            params["effort"] = effort
        if service_tier:
            params["serviceTier"] = service_tier
        dispatch_kwargs = (
            {"on_dispatch": on_dispatch} if on_dispatch is not None else {}
        )
        result = await self.request(
            "turn/start",
            params,
            timeout=timeout,
            **dispatch_kwargs,
        )
        if isinstance(result, dict):
            turn = result.get("turn")
            if isinstance(turn, dict):
                turn_id = turn.get("id")
                if isinstance(turn_id, str) and turn_id:
                    self._active_turns[thread_id] = turn_id
            return result
        return {}

    async def turn_steer(
        self,
        *,
        thread_id: str,
        expected_turn_id: str,
        inputs: list[dict[str, Any]],
        timeout: float = APP_SERVER_MUTATION_TIMEOUT_SECONDS,
        on_dispatch: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "expectedTurnId": expected_turn_id,
            "input": inputs,
        }
        dispatch_kwargs = (
            {"on_dispatch": on_dispatch} if on_dispatch is not None else {}
        )
        result = await self.request(
            "turn/steer",
            params,
            timeout=timeout,
            **dispatch_kwargs,
        )
        if isinstance(result, dict):
            turn_id = result.get("turnId")
            if isinstance(turn_id, str) and turn_id:
                self._active_turns[thread_id] = turn_id
            return result
        return {}

    async def turn_interrupt(self, *, thread_id: str, turn_id: str) -> None:
        params = {
            "threadId": thread_id,
            "turnId": turn_id,
        }
        await self.request("turn/interrupt", params, timeout=30.0)

    async def read_rate_limits(self) -> dict[str, Any]:
        result = await self.request("account/rateLimits/read", {}, timeout=20.0)
        if isinstance(result, dict):
            snapshot = result.get("rateLimits")
            if isinstance(snapshot, dict):
                self._rate_limits = snapshot
            return result
        return {}

    async def probe_health(self, *, timeout: float = 5.0) -> bool:
        """Probe request/response health, returning false when recovery occurred."""
        bounded_timeout = max(0.1, min(float(timeout), 10.0))
        reset_sequence_before_start = self._transport_reset_sequence
        generation = self._transport_generation
        reset_sequence = reset_sequence_before_start
        try:
            await self.ensure_started()
            generation = self._transport_generation
            reset_sequence = self._transport_reset_sequence
            if reset_sequence != reset_sequence_before_start:
                return False
            await self.request(
                "account/rateLimits/read",
                {},
                timeout=bounded_timeout,
                retry_safe_timeout=False,
            )
            return (
                self._transport_generation == generation
                and self._transport_reset_sequence == reset_sequence
            )
        except Exception as exc:
            recovered = (
                self._transport_generation != generation
                or self._transport_reset_sequence != reset_sequence
            )
            if not recovered and (
                self._transport_needs_restart or not self.is_running()
            ):
                try:
                    await self.ensure_started()
                except Exception:
                    logger.warning(
                        "Codex app-server health recovery failed",
                        exc_info=True,
                    )
                recovered = (
                    self._transport_generation != generation
                    or self._transport_reset_sequence != reset_sequence
                )
            if recovered:
                logger.warning(
                    "Codex app-server health probe triggered recovery: %s",
                    exc,
                )
                return False
            raise

    def get_active_turn_id(self, thread_id: str) -> str | None:
        turn = self._active_turns.get(thread_id)
        return turn if turn else None

    def is_turn_in_progress(self, thread_id: str) -> bool:
        return bool(self._active_turns.get(thread_id))

    def clear_active_turn(self, thread_id: str) -> None:
        self._active_turns.pop(thread_id, None)

    def get_thread_token_usage(self, thread_id: str) -> dict[str, Any] | None:
        value = self._thread_token_usage.get(thread_id)
        return value if isinstance(value, dict) else None

    def get_rate_limits_snapshot(self) -> dict[str, Any] | None:
        value = self._rate_limits
        return value if isinstance(value, dict) else None

    def get_server_user_agent(self) -> str:
        """Return app-server user-agent string from initialize response."""
        return self._server_user_agent


codex_app_server_client = CodexAppServerClient()
