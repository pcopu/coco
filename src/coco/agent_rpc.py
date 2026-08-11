"""High-level agent RPC surface for multi-machine controller -> agent calls."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .cluster_rpc import ClusterRpcClient, ClusterRpcError, ClusterRpcServer
from .codex_app_server import codex_app_server_client
from .config import config
from .controller_rpc import REMOTE_CODEX_MACHINE_CONTEXT_KEY
from .handlers.directory_browser import clamp_browse_path, resolve_browse_root
from .node_registry import node_registry
from .session import session_manager
from .self_update import resolve_coco_tool_update_argv as _resolve_coco_tool_update_argv
from .utils import env_alias


logger = logging.getLogger(__name__)

SESSION_PANEL_LIST_REQUEST_LIMIT = 50
SESSION_PANEL_LIST_LIMIT = 100
ALLOWED_TELEGRAM_DOCUMENT_EXTENSIONS = {".pdf", ".txt", ".md"}
ALLOWED_TELEGRAM_IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}
ALLOWED_TELEGRAM_VIDEO_TYPES = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".mpeg": "video/mpeg",
    ".mpg": "video/mpeg",
}
TELEGRAM_ATTACHMENT_MAX_BYTES = 45 * 1024 * 1024
EXPECTED_CODEX_TRANSPORT_LEGACY_PARAM = "expected_transport_legacy"
EXPECTED_CODEX_TRANSPORT_EPOCH_PARAM = "expected_transport_epoch"
EXPECTED_CODEX_TRANSPORT_EPOCH_STARTED_AT_PARAM = (
    "expected_transport_epoch_started_at"
)
REMOTE_CODEX_MUTATION_DEFERRED_MESSAGE_PREFIX = (
    "Remote Codex mutation was not dispatched"
)
_remote_restart_requested = False


class RemoteCodexMutationDeferredError(ClusterRpcError):
    """A remote Codex mutation was definitively blocked before dispatch."""


def _require_expected_codex_transport(params: dict[str, Any]) -> None:
    """Reject a modern controller mutation routed to another agent epoch."""
    has_expected_legacy = EXPECTED_CODEX_TRANSPORT_LEGACY_PARAM in params
    has_expected_epoch = EXPECTED_CODEX_TRANSPORT_EPOCH_PARAM in params
    has_expected_started_at = (
        EXPECTED_CODEX_TRANSPORT_EPOCH_STARTED_AT_PARAM in params
    )
    if has_expected_legacy:
        if (
            params.get(EXPECTED_CODEX_TRANSPORT_LEGACY_PARAM) is not True
            or has_expected_epoch
            or has_expected_started_at
        ):
            raise RemoteCodexMutationDeferredError(
                "Remote Codex mutation was not dispatched because the "
                "legacy transport fence is invalid"
            )
        # A legacy agent ignores this new field. Reaching this validator means
        # the endpoint changed to a modern replacement after controller gating.
        raise RemoteCodexMutationDeferredError(
            "Remote Codex mutation was not dispatched because its legacy "
            "transport fence reached a replacement agent"
        )
    if not has_expected_epoch and not has_expected_started_at:
        # Older controllers do not carry a fence.
        return

    raw_expected_epoch = params.get(EXPECTED_CODEX_TRANSPORT_EPOCH_PARAM)
    raw_expected_started_at = params.get(
        EXPECTED_CODEX_TRANSPORT_EPOCH_STARTED_AT_PARAM
    )
    if (
        not isinstance(raw_expected_epoch, str)
        or not raw_expected_epoch.strip()
        or isinstance(raw_expected_started_at, bool)
    ):
        raise RemoteCodexMutationDeferredError(
            "Remote Codex mutation was not dispatched because the expected "
            "transport epoch fence is invalid"
        )
    try:
        expected_started_at = float(raw_expected_started_at)
    except (TypeError, ValueError) as exc:
        raise RemoteCodexMutationDeferredError(
            "Remote Codex mutation was not dispatched because the expected "
            "transport epoch fence is invalid"
        ) from exc
    if expected_started_at <= 0:
        raise RemoteCodexMutationDeferredError(
            "Remote Codex mutation was not dispatched because the expected "
            "transport epoch fence is invalid"
        )

    transport_state = codex_app_server_client.transport_state_snapshot()
    observed_epoch = str(transport_state.get("epoch", "")).strip()
    try:
        observed_started_at = float(
            transport_state.get("epoch_started_at", 0.0) or 0.0
        )
    except (TypeError, ValueError):
        observed_started_at = 0.0
    expected_epoch = raw_expected_epoch.strip()
    if (
        observed_epoch != expected_epoch
        or observed_started_at != expected_started_at
    ):
        raise RemoteCodexMutationDeferredError(
            "Remote Codex mutation was not dispatched: expected transport "
            f"epoch {expected_epoch}, but the replacement agent has epoch "
            f"{observed_epoch or '<unknown>'}"
        )


def _codex_transport_response_fields(
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return transport identity fields shared by agent lifecycle responses."""
    if state is None:
        state = codex_app_server_client.transport_state_snapshot()
    return {
        "transport_epoch": state["epoch"],
        "transport_epoch_started_at": state["epoch_started_at"],
        "transport_generation": state["generation"],
        "transport_reset_sequence": state["reset_sequence"],
        "transport_last_reset_generation": state["last_reset_generation"],
        "transport_last_reset_reason": state["last_reset_reason"],
    }


_COCO_SELF_UPDATE_COMMAND_ENV = "COCO_SELF_UPDATE_COMMAND"
_CODEX_UPGRADE_COMMAND_ENV = "COCO_CODEX_UPGRADE_COMMAND"


def _probe_workspace_write_access(workspace_dir: str) -> tuple[str, bool, str | None]:
    checked_path = workspace_dir or ""
    if not checked_path:
        return checked_path, False, "No workspace configured"
    try:
        target = Path(checked_path).expanduser()
    except (RuntimeError, ValueError) as exc:
        return checked_path, False, str(exc)
    if not target.exists():
        return str(target), False, "Directory does not exist"
    if not target.is_dir():
        return str(target), False, "Path is not a directory"
    probe = target / ".coco_write_probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return str(target), True, None
    except Exception as exc:
        return str(target), False, str(exc)


def _resolve_attachment_path(
    *,
    workspace_dir: str,
    raw_path: str,
) -> Path | None:
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
        return None
    return resolved


def _resolve_document_attachment(
    *,
    workspace_dir: str,
    raw_path: str,
) -> tuple[str, bytes] | None:
    resolved = _resolve_attachment_path(
        workspace_dir=workspace_dir,
        raw_path=raw_path,
    )
    if resolved is None:
        return None

    if resolved.suffix.lower() not in ALLOWED_TELEGRAM_DOCUMENT_EXTENSIONS:
        return None
    if not resolved.is_file():
        return None
    try:
        size = resolved.stat().st_size
    except OSError:
        return None
    if size > TELEGRAM_ATTACHMENT_MAX_BYTES:
        return None
    try:
        return resolved.name, resolved.read_bytes()
    except OSError:
        return None


def _resolve_image_attachment(
    *,
    workspace_dir: str,
    raw_path: str,
) -> tuple[str, bytes] | None:
    resolved = _resolve_attachment_path(
        workspace_dir=workspace_dir,
        raw_path=raw_path,
    )
    if resolved is None:
        return None
    media_type = ALLOWED_TELEGRAM_IMAGE_TYPES.get(resolved.suffix.lower())
    if not media_type:
        return None
    if not resolved.is_file():
        return None
    try:
        size = resolved.stat().st_size
    except OSError:
        return None
    if size > TELEGRAM_ATTACHMENT_MAX_BYTES:
        return None
    try:
        return media_type, resolved.read_bytes()
    except OSError:
        return None


def _resolve_video_attachment(
    *,
    workspace_dir: str,
    raw_path: str,
) -> tuple[str, bytes] | None:
    resolved = _resolve_attachment_path(
        workspace_dir=workspace_dir,
        raw_path=raw_path,
    )
    if resolved is None:
        return None
    media_type = ALLOWED_TELEGRAM_VIDEO_TYPES.get(resolved.suffix.lower())
    if not media_type:
        return None
    if not resolved.is_file():
        return None
    try:
        size = resolved.stat().st_size
    except OSError:
        return None
    if size > TELEGRAM_ATTACHMENT_MAX_BYTES:
        return None
    try:
        return media_type, resolved.read_bytes()
    except OSError:
        return None


def _extract_thread_ids_from_list_payload(payload: dict[str, object]) -> list[str]:
    items = payload.get("threads")
    if not isinstance(items, list):
        return []
    results: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        thread_id = item.get("id")
        if isinstance(thread_id, str) and thread_id.strip():
            results.append(thread_id.strip())
    return results


def _extract_thread_list_next_cursor(payload: dict[str, object]) -> str:
    for key in ("nextCursor", "nextPageCursor", "next"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


async def _list_all_session_threads(
    *,
    max_items: int = SESSION_PANEL_LIST_LIMIT,
) -> tuple[list[str], str]:
    all_ids: list[str] = []
    list_error = ""
    cursor: str | None = None
    seen_cursors: set[str] = set()

    while len(all_ids) < max_items:
        remaining = max_items - len(all_ids)
        request_limit = max(1, min(SESSION_PANEL_LIST_REQUEST_LIMIT, remaining))
        try:
            payload = await codex_app_server_client.thread_list(
                cursor=cursor,
                limit=request_limit,
            )
        except Exception as exc:
            list_error = str(exc)
            break

        page_ids = _extract_thread_ids_from_list_payload(payload)
        for thread_id in page_ids:
            if thread_id not in all_ids:
                all_ids.append(thread_id)
                if len(all_ids) >= max_items:
                    break

        next_cursor = _extract_thread_list_next_cursor(payload)
        if len(all_ids) >= max_items or not next_cursor:
            break
        if next_cursor in seen_cursors:
            list_error = "thread/list returned a repeated cursor; showing available results."
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    return all_ids, list_error


def _configure_remote_window(
    *,
    window_id: str,
    cwd: str,
    window_name: str,
    approval_mode: str,
    codex_thread_id: str,
) -> None:
    state = session_manager.get_window_state(window_id)
    changed = False
    normalized_cwd = str(Path(cwd).expanduser().resolve()) if cwd else ""
    if normalized_cwd and state.cwd != normalized_cwd:
        state.cwd = normalized_cwd
        changed = True
    if window_name and state.window_name != window_name:
        state.window_name = window_name
        changed = True
    if approval_mode and state.approval_mode != approval_mode:
        state.approval_mode = approval_mode
        changed = True
    normalized_thread = codex_thread_id.strip()
    if normalized_thread and state.codex_thread_id != normalized_thread:
        state.codex_thread_id = normalized_thread
        state.codex_active_turn_id = ""
        changed = True
    if changed:
        session_manager._save_state()


class AgentRpcServer:
    """Machine-local RPC server used by the active controller."""

    def __init__(self, *, shared_secret: str) -> None:
        self._server = ClusterRpcServer(shared_secret=shared_secret)
        self._probe_client = ClusterRpcClient(shared_secret=shared_secret, timeout_seconds=10.0)
        self._server.register("agent/ping", self._ping)
        self._server.register("agent/probe_codex_health", self._probe_codex_health)
        self._server.register("agent/probe_machine", self._probe_machine)
        self._server.register("agent/probe_workspace_write_access", self._probe_workspace_write_access)
        self._server.register("agent/browse", self._browse)
        self._server.register("agent/folder_sessions", self._folder_sessions)
        self._server.register("agent/list_threads", self._list_threads)
        self._server.register("agent/ensure_thread", self._ensure_thread)
        self._server.register("agent/fork_thread", self._fork_thread)
        self._server.register("agent/rollback_thread", self._rollback_thread)
        self._server.register("agent/read_attachments", self._read_attachments)
        self._server.register("agent/read_documents", self._read_documents)
        self._server.register("agent/resume_latest", self._resume_latest)
        self._server.register("agent/resume_thread", self._resume_thread)
        self._server.register("agent/thread_goal_get", self._thread_goal_get)
        self._server.register("agent/thread_goal_set", self._thread_goal_set)
        self._server.register("agent/thread_goal_clear", self._thread_goal_clear)
        self._server.register("agent/send_inputs", self._send_inputs)
        self._server.register("agent/run_update", self._run_update)

    async def start(self, *, host: str, port: int) -> None:
        await self._server.start(host=host, port=port)

    async def stop(self) -> None:
        await self._server.stop()

    def bound_address(self) -> tuple[str, int]:
        return self._server.bound_address()

    async def _ping(self, _params: dict[str, Any]) -> dict[str, Any]:
        node = node_registry.ensure_local_node(now=asyncio.get_running_loop().time())
        return node.to_dict()

    async def _probe_codex_health(
        self,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            timeout = float(params.get("timeout", 5.0) or 5.0)
        except (TypeError, ValueError):
            timeout = 5.0
        healthy = await codex_app_server_client.probe_health(timeout=timeout)
        return {
            "healthy": healthy,
            **codex_app_server_client.transport_state_snapshot(),
        }

    async def _probe_machine(self, params: dict[str, Any]) -> dict[str, Any]:
        target_host = str(params.get("target_host", "")).strip()
        target_port = int(params.get("target_port", 0) or 0)
        expected_machine_id = str(params.get("expected_machine_id", "")).strip()
        if not target_host or target_port <= 0:
            raise ClusterRpcError("target endpoint is required")
        result = await self._probe_client.call(
            host=target_host,
            port=target_port,
            method="agent/ping",
            params={},
        )
        if not isinstance(result, dict):
            raise ClusterRpcError("invalid probe response")
        observed_machine_id = str(result.get("machine_id", "")).strip()
        if expected_machine_id and observed_machine_id != expected_machine_id:
            raise ClusterRpcError(
                f"probe target mismatch: expected {expected_machine_id}, got {observed_machine_id or '<unknown>'}"
            )
        return result

    async def _browse(self, params: dict[str, Any]) -> dict[str, Any]:
        chat_id = params.get("chat_id")
        chat_value = int(chat_id) if isinstance(chat_id, int | bool) else None
        root = resolve_browse_root(config.resolve_browse_root_for_chat(chat_value))
        requested = str(params.get("current_path", "")).strip() or str(root)
        current = clamp_browse_path(requested, root)
        try:
            subdirs = sorted(
                d.name
                for d in current.iterdir()
                if d.is_dir() and not d.name.startswith(".")
            )
        except (PermissionError, OSError):
            subdirs = []
        return {
            "root_path": str(root),
            "current_path": str(current),
            "subdirs": subdirs,
        }

    async def _probe_workspace_write_access(self, params: dict[str, Any]) -> dict[str, Any]:
        workspace_dir = str(params.get("workspace_dir", "")).strip()
        checked_path, can_write, write_error = _probe_workspace_write_access(workspace_dir)
        return {
            "workspace_path": checked_path,
            "can_write": can_write,
            "write_error": write_error or "",
        }

    async def _folder_sessions(self, params: dict[str, Any]) -> dict[str, Any]:
        cwd = str(params.get("cwd", "")).strip()
        limit = int(params.get("limit", 100) or 100)
        items = session_manager.list_codex_session_summaries_for_cwd(cwd, limit=limit)
        return {
            "items": [
                {
                    "thread_id": item.thread_id,
                    "created_at": item.created_at,
                    "last_active_at": item.last_active_at,
                }
                for item in items
            ]
        }

    async def _list_threads(self, params: dict[str, Any]) -> dict[str, Any]:
        max_items = int(params.get("max_items", SESSION_PANEL_LIST_LIMIT) or SESSION_PANEL_LIST_LIMIT)
        items, list_error = await _list_all_session_threads(max_items=max_items)
        return {"items": items, "list_error": list_error}

    async def _ensure_thread(self, params: dict[str, Any]) -> dict[str, Any]:
        _require_expected_codex_transport(params)
        window_id = str(params.get("window_id", "")).strip()
        cwd = str(params.get("cwd", "")).strip()
        window_name = str(params.get("window_name", "")).strip()
        approval_mode = str(params.get("approval_mode", "")).strip()
        model_slug = str(params.get("model_slug", "")).strip()
        reasoning_effort = str(params.get("reasoning_effort", "")).strip()
        service_tier = str(params.get("service_tier", "")).strip().lower()
        _configure_remote_window(
            window_id=window_id,
            cwd=cwd,
            window_name=window_name,
            approval_mode=approval_mode,
            codex_thread_id="",
        )
        ensure_kwargs: dict[str, str] = {}
        if model_slug:
            ensure_kwargs["model"] = model_slug
        if reasoning_effort:
            ensure_kwargs["effort"] = reasoning_effort
        if service_tier:
            ensure_kwargs["service_tier"] = service_tier
        thread_id, _approval = await session_manager._ensure_codex_thread_for_window(
            window_id=window_id,
            cwd=cwd,
            **ensure_kwargs,
        )
        return {
            "thread_id": thread_id,
            "turn_id": session_manager.get_window_codex_active_turn_id(window_id),
            **_codex_transport_response_fields(),
        }

    async def _resume_latest(self, params: dict[str, Any]) -> dict[str, Any]:
        _require_expected_codex_transport(params)
        window_id = str(params.get("window_id", "")).strip()
        cwd = str(params.get("cwd", "")).strip()
        window_name = str(params.get("window_name", "")).strip()
        approval_mode = str(params.get("approval_mode", "")).strip()
        _configure_remote_window(
            window_id=window_id,
            cwd=cwd,
            window_name=window_name,
            approval_mode=approval_mode,
            codex_thread_id="",
        )
        thread_id = await session_manager.resume_latest_codex_session_for_window(
            window_id=window_id,
            cwd=cwd,
        )
        turn_id = session_manager.get_window_codex_active_turn_id(window_id)
        model_slug, reasoning_effort = session_manager.get_codex_session_model_selection_for_thread(
            thread_id,
            cwd=cwd,
        ) if thread_id else ("", "")
        return {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "model_slug": model_slug,
            "reasoning_effort": reasoning_effort,
            "session_start_reason": (
                session_manager.peek_window_pending_session_start_reason(
                    window_id
                )
            ),
            "transport_lifecycle_noop": not bool(thread_id),
            **_codex_transport_response_fields(),
        }

    async def _fork_thread(self, params: dict[str, Any]) -> dict[str, Any]:
        _require_expected_codex_transport(params)
        window_id = str(params.get("window_id", "")).strip()
        thread_id = str(params.get("thread_id", "")).strip()
        turn_id = str(params.get("turn_id", "")).strip()
        result = await codex_app_server_client.thread_fork(
            thread_id=thread_id,
            turn_id=turn_id or None,
        )
        forked_thread_id = session_manager._extract_lifecycle_thread_id(result, fallback="")
        forked_turn_id = session_manager._extract_lifecycle_turn_id(result)
        if forked_thread_id:
            session_manager.set_window_codex_thread_id(window_id, forked_thread_id)
            session_manager.set_window_codex_active_turn_id(window_id, forked_turn_id)
        return {
            "thread_id": forked_thread_id,
            "turn_id": forked_turn_id,
            **_codex_transport_response_fields(),
        }

    async def _rollback_thread(self, params: dict[str, Any]) -> dict[str, Any]:
        _require_expected_codex_transport(params)
        window_id = str(params.get("window_id", "")).strip()
        thread_id = str(params.get("thread_id", "")).strip()
        num_turns = int(params.get("num_turns", 1) or 1)
        result = await codex_app_server_client.thread_rollback(
            thread_id=thread_id,
            num_turns=num_turns,
        )
        rolled_thread_id = session_manager._extract_lifecycle_thread_id(result, fallback=thread_id)
        rolled_turn_id = session_manager._extract_lifecycle_turn_id(result)
        if rolled_thread_id:
            session_manager.set_window_codex_thread_id(window_id, rolled_thread_id)
            session_manager.set_window_codex_active_turn_id(window_id, rolled_turn_id)
        return {
            "thread_id": rolled_thread_id,
            "turn_id": rolled_turn_id,
            **_codex_transport_response_fields(),
        }

    async def _read_attachments(self, params: dict[str, Any]) -> dict[str, Any]:
        workspace_dir = str(params.get("workspace_dir", "")).strip()
        raw_paths = params.get("paths", [])
        if not isinstance(raw_paths, list):
            raise ClusterRpcError("paths must be a list")
        documents: list[dict[str, str]] = []
        images: list[dict[str, str]] = []
        videos: list[dict[str, str]] = []
        for raw_path in raw_paths:
            if not isinstance(raw_path, str):
                continue
            image_resolved = _resolve_image_attachment(
                workspace_dir=workspace_dir,
                raw_path=raw_path,
            )
            if image_resolved is not None:
                media_type, raw_bytes = image_resolved
                images.append(
                    {
                        "media_type": media_type,
                        "data_b64": base64.b64encode(raw_bytes).decode("ascii"),
                    }
                )
                continue
            video_resolved = _resolve_video_attachment(
                workspace_dir=workspace_dir,
                raw_path=raw_path,
            )
            if video_resolved is not None:
                media_type, raw_bytes = video_resolved
                videos.append(
                    {
                        "media_type": media_type,
                        "data_b64": base64.b64encode(raw_bytes).decode("ascii"),
                    }
                )
                continue
            document_resolved = _resolve_document_attachment(
                workspace_dir=workspace_dir,
                raw_path=raw_path,
            )
            if document_resolved is None:
                continue
            name, raw_bytes = document_resolved
            documents.append(
                {
                    "name": name,
                    "data_b64": base64.b64encode(raw_bytes).decode("ascii"),
                }
            )
        return {"documents": documents, "images": images, "videos": videos}

    async def _read_documents(self, params: dict[str, Any]) -> dict[str, Any]:
        result = await self._read_attachments(params)
        return {"documents": result.get("documents", [])}

    async def _resume_thread(self, params: dict[str, Any]) -> dict[str, Any]:
        _require_expected_codex_transport(params)
        window_id = str(params.get("window_id", "")).strip()
        cwd = str(params.get("cwd", "")).strip()
        requested_thread_id = str(params.get("thread_id", "")).strip()
        window_name = str(params.get("window_name", "")).strip()
        approval_mode = str(params.get("approval_mode", "")).strip()
        _configure_remote_window(
            window_id=window_id,
            cwd=cwd,
            window_name=window_name,
            approval_mode=approval_mode,
            codex_thread_id="",
        )
        resumed_thread_id = await session_manager.resume_codex_session_for_window(
            window_id=window_id,
            cwd=cwd,
            thread_id=requested_thread_id,
        )
        resumed_turn_id = session_manager.get_window_codex_active_turn_id(
            window_id
        )
        model_slug, reasoning_effort = session_manager.get_codex_session_model_selection_for_thread(
            resumed_thread_id,
            cwd=cwd,
        ) if resumed_thread_id else ("", "")
        return {
            "thread_id": resumed_thread_id,
            "turn_id": resumed_turn_id,
            "model_slug": model_slug,
            "reasoning_effort": reasoning_effort,
            **_codex_transport_response_fields(),
        }

    async def _thread_goal_get(self, params: dict[str, Any]) -> dict[str, Any]:
        thread_id = str(params.get("thread_id", "")).strip()
        if not thread_id:
            raise ClusterRpcError("thread_id is required")
        result = await codex_app_server_client.thread_goal_get(thread_id=thread_id)
        if not isinstance(result, dict):
            raise ClusterRpcError("invalid goal get response")
        return result

    async def _thread_goal_set(self, params: dict[str, Any]) -> dict[str, Any]:
        _require_expected_codex_transport(params)
        thread_id = str(params.get("thread_id", "")).strip()
        goal = str(params.get("goal", "")).strip()
        if not thread_id:
            raise ClusterRpcError("thread_id is required")
        if not goal:
            raise ClusterRpcError("goal is required")
        result = await codex_app_server_client.thread_goal_set(
            thread_id=thread_id,
            goal=goal,
        )
        if not isinstance(result, dict):
            raise ClusterRpcError("invalid goal set response")
        return result

    async def _thread_goal_clear(self, params: dict[str, Any]) -> dict[str, Any]:
        _require_expected_codex_transport(params)
        thread_id = str(params.get("thread_id", "")).strip()
        if not thread_id:
            raise ClusterRpcError("thread_id is required")
        result = await codex_app_server_client.thread_goal_clear(thread_id=thread_id)
        if not isinstance(result, dict):
            raise ClusterRpcError("invalid goal clear response")
        return result

    async def _send_inputs(self, params: dict[str, Any]) -> dict[str, Any]:
        _require_expected_codex_transport(params)
        window_id = str(params.get("window_id", "")).strip()
        cwd = str(params.get("cwd", "")).strip()
        window_name = str(params.get("window_name", "")).strip()
        approval_mode = str(params.get("approval_mode", "")).strip()
        codex_thread_id = str(params.get("thread_id", "")).strip()
        model_slug = str(params.get("model_slug", "")).strip()
        reasoning_effort = str(params.get("reasoning_effort", "")).strip()
        service_tier = str(params.get("service_tier", "")).strip().lower()
        steer = bool(params.get("steer", False))
        force_new_turn = bool(params.get("force_new_turn", False))
        inputs = params.get("inputs", [])
        if not isinstance(inputs, list):
            raise ClusterRpcError("inputs must be a list")
        _configure_remote_window(
            window_id=window_id,
            cwd=cwd,
            window_name=window_name,
            approval_mode=approval_mode,
            codex_thread_id=codex_thread_id,
        )
        # Establish the transport before fencing the mutation so an ordinary
        # first startup is not mistaken for a concurrent recycle.
        await codex_app_server_client.ensure_started()
        transport_state_before = (
            codex_app_server_client.transport_state_snapshot()
        )
        ok, message = await session_manager.send_inputs_to_window(
            window_id,
            inputs,
            steer=steer,
            force_new_turn=force_new_turn,
            model_slug=model_slug,
            reasoning_effort=reasoning_effort,
            service_tier=service_tier,
        )
        state = session_manager.get_window_state(window_id)
        transport_state = codex_app_server_client.transport_state_snapshot()
        transport_identity_before = (
            transport_state_before["epoch"],
            transport_state_before["epoch_started_at"],
            transport_state_before["generation"],
            transport_state_before["reset_sequence"],
        )
        transport_identity_after = (
            transport_state["epoch"],
            transport_state["epoch_started_at"],
            transport_state["generation"],
            transport_state["reset_sequence"],
        )
        if transport_identity_after != transport_identity_before:
            logger.warning(
                "Rejecting remote send acknowledgement after transport change "
                "(window_id=%s before=%r after=%r)",
                window_id,
                transport_identity_before,
                transport_identity_after,
            )
            raise ClusterRpcError(
                "Codex transport changed during remote send; "
                "the mutation outcome is uncertain and will not be replayed"
            )
        return {
            "ok": ok,
            "message": message,
            "thread_id": state.codex_thread_id,
            "turn_id": state.codex_active_turn_id,
            **_codex_transport_response_fields(transport_state),
            "transport_reset_occurred": False,
        }

    async def _run_update(self, params: dict[str, Any]) -> dict[str, Any]:
        action = str(params.get("action", "")).strip().lower()
        if action not in {"coco", "codex", "both"}:
            raise ClusterRpcError("invalid update action")
        if _remote_restart_requested:
            return {"ok": False, "message": "Restart already in progress."}

        messages: list[str] = []
        if action in {"coco", "both"}:
            ok, message = await asyncio.to_thread(_run_remote_coco_update_sync)
            if not ok:
                return {"ok": False, "message": message}
            messages.append(message)
        if action in {"codex", "both"}:
            ok, message = await asyncio.to_thread(_run_remote_codex_upgrade_sync)
            if not ok:
                return {"ok": False, "message": message}
            messages.append(message)

        _queue_remote_restart()
        summary = "\n".join(part for part in messages if part).strip() or "Remote update completed."
        return {"ok": True, "message": f"{summary}\nRestarting remote CoCo now."}


class AgentRpcClient:
    """Controller-side high-level client for agent RPC operations."""

    def __init__(self, *, shared_secret: str) -> None:
        self._client = ClusterRpcClient(shared_secret=shared_secret)
        self._codex_mutation_dispatch_gate: (
            Callable[
                [str],
                Awaitable[bool | tuple[str, float]],
            ]
            | None
        ) = None

    def set_codex_mutation_dispatch_gate(
        self,
        handler: (
            Callable[
                [str],
                Awaitable[bool | tuple[str, float]],
            ]
            | None
        ),
    ) -> None:
        """Register the controller gate for remote Codex mutations."""
        self._codex_mutation_dispatch_gate = handler

    async def _require_codex_mutation_dispatch(
        self,
        *,
        machine_id: str,
        operation: str,
    ) -> dict[str, str | float | bool]:
        handler = self._codex_mutation_dispatch_gate
        if handler is None:
            return {}
        decision = await handler(machine_id)
        if decision is False:
            raise RemoteCodexMutationDeferredError(
                f"Remote Codex {operation} was not dispatched because "
                "transport replacement confirmation is pending"
            )
        if decision is True:
            # Legacy agents ignore this field; a modern replacement rejects it.
            return {EXPECTED_CODEX_TRANSPORT_LEGACY_PARAM: True}
        if (
            not isinstance(decision, tuple)
            or len(decision) != 2
            or not isinstance(decision[0], str)
            or not decision[0].strip()
            or isinstance(decision[1], bool)
        ):
            raise RemoteCodexMutationDeferredError(
                f"Remote Codex {operation} was not dispatched because its "
                "transport epoch binding is invalid"
            )
        try:
            epoch_started_at = float(decision[1])
        except (TypeError, ValueError) as exc:
            raise RemoteCodexMutationDeferredError(
                f"Remote Codex {operation} was not dispatched because its "
                "transport epoch binding is invalid"
            ) from exc
        if epoch_started_at <= 0:
            raise RemoteCodexMutationDeferredError(
                f"Remote Codex {operation} was not dispatched because its "
                "transport epoch binding is invalid"
            )
        return {
            EXPECTED_CODEX_TRANSPORT_EPOCH_PARAM: decision[0].strip(),
            EXPECTED_CODEX_TRANSPORT_EPOCH_STARTED_AT_PARAM: epoch_started_at,
        }

    async def _call_codex_mutation(
        self,
        *,
        host: str,
        port: int,
        method: str,
        params: dict[str, Any],
    ) -> Any:
        """Preserve a definitive agent-side fence rejection for callers."""
        try:
            return await self._client.call(
                host=host,
                port=port,
                method=method,
                params=params,
            )
        except ClusterRpcError as exc:
            message = str(exc)
            if message.startswith(
                REMOTE_CODEX_MUTATION_DEFERRED_MESSAGE_PREFIX
            ):
                raise RemoteCodexMutationDeferredError(message) from exc
            raise

    @staticmethod
    async def _record_remote_transport_result(
        *,
        machine_id: str,
        window_id: str,
        result: dict[str, Any],
        operation: str,
    ) -> None:
        validation_result = {
            **result,
            REMOTE_CODEX_MACHINE_CONTEXT_KEY: machine_id.strip(),
        }
        if not await session_manager._accept_remote_transport_result(
            window_id=window_id,
            result=validation_result,
        ):
            raise ClusterRpcError(
                f"remote Codex transport changed during {operation}"
            )

    @staticmethod
    def _resolve_endpoint(machine_id: str) -> tuple[str, int]:
        node = node_registry.get_node(machine_id)
        if node is None:
            raise ClusterRpcError(f"unknown machine: {machine_id}")
        host = node.rpc_host.strip()
        port = int(node.rpc_port)
        if not host or port <= 0:
            raise ClusterRpcError(f"machine has no reachable RPC endpoint: {machine_id}")
        return host, port

    async def ping(self, machine_id: str) -> dict[str, Any]:
        host, port = self._resolve_endpoint(machine_id)
        result = await self._client.call(host=host, port=port, method="agent/ping", params={})
        if not isinstance(result, dict):
            raise ClusterRpcError("invalid ping response")
        return result

    async def probe_codex_health(
        self,
        machine_id: str,
        *,
        timeout: float = 5.0,
    ) -> bool:
        result = await self.probe_codex_health_state(
            machine_id,
            timeout=timeout,
        )
        return bool(result["healthy"])

    async def probe_codex_health_state(
        self,
        machine_id: str,
        *,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        """Probe Codex and retain the transport identity used by the result."""
        host, port = self._resolve_endpoint(machine_id)
        result = await self._client.call(
            host=host,
            port=port,
            method="agent/probe_codex_health",
            params={"timeout": timeout},
        )
        if not isinstance(result, dict) or not isinstance(
            result.get("healthy"),
            bool,
        ):
            raise ClusterRpcError("invalid Codex health probe response")
        epoch = str(
            result.get("transport_epoch", result.get("epoch", ""))
        ).strip()
        try:
            epoch_started_at = float(
                result.get(
                    "transport_epoch_started_at",
                    result.get("epoch_started_at", 0.0),
                )
                or 0.0
            )
            generation = int(
                result.get(
                    "transport_generation",
                    result.get("generation", -1),
                )
            )
            reset_sequence = int(
                result.get(
                    "transport_reset_sequence",
                    result.get("reset_sequence", -1),
                )
            )
            last_reset_generation = int(
                result.get(
                    "transport_last_reset_generation",
                    result.get("last_reset_generation", -1),
                )
            )
            last_reset_at = float(
                result.get(
                    "transport_last_reset_at",
                    result.get("last_reset_at", 0.0),
                )
                or 0.0
            )
        except (TypeError, ValueError) as exc:
            raise ClusterRpcError(
                "invalid Codex health probe transport metadata"
            ) from exc
        if (
            not epoch
            or epoch_started_at <= 0
            or generation < 0
            or reset_sequence < 0
            or last_reset_generation < 0
            or last_reset_at < 0
        ):
            raise ClusterRpcError(
                "invalid Codex health probe transport metadata"
            )
        return {
            "healthy": bool(result["healthy"]),
            "transport_epoch": epoch,
            "transport_epoch_started_at": epoch_started_at,
            "transport_generation": generation,
            "transport_reset_sequence": reset_sequence,
            "transport_last_reset_generation": last_reset_generation,
            "transport_last_reset_reason": str(
                result.get(
                    "transport_last_reset_reason",
                    result.get("last_reset_reason", ""),
                )
            ).strip(),
            "transport_last_reset_at": last_reset_at,
        }

    async def probe_machine(
        self,
        machine_id: str,
        *,
        via_machine_id: str = "",
    ) -> dict[str, Any]:
        target_host, target_port = self._resolve_endpoint(machine_id)
        normalized_via = via_machine_id.strip()
        if normalized_via and normalized_via not in {machine_id.strip(), node_registry.local_machine_id}:
            worker_host, worker_port = self._resolve_endpoint(normalized_via)
            result = await self._client.call(
                host=worker_host,
                port=worker_port,
                method="agent/probe_machine",
                params={
                    "target_host": target_host,
                    "target_port": target_port,
                    "expected_machine_id": machine_id,
                },
            )
        else:
            result = await self._client.call(
                host=target_host,
                port=target_port,
                method="agent/ping",
                params={},
            )
        if not isinstance(result, dict):
            raise ClusterRpcError("invalid probe response")
        return result

    async def browse(
        self,
        machine_id: str,
        *,
        current_path: str,
        chat_id: int | None = None,
    ) -> dict[str, Any]:
        host, port = self._resolve_endpoint(machine_id)
        result = await self._client.call(
            host=host,
            port=port,
            method="agent/browse",
            params={"current_path": current_path, "chat_id": chat_id},
        )
        if not isinstance(result, dict):
            raise ClusterRpcError("invalid browse response")
        return result

    async def probe_workspace_write_access(
        self,
        machine_id: str,
        *,
        workspace_dir: str,
    ) -> dict[str, Any]:
        host, port = self._resolve_endpoint(machine_id)
        result = await self._client.call(
            host=host,
            port=port,
            method="agent/probe_workspace_write_access",
            params={"workspace_dir": workspace_dir},
        )
        if not isinstance(result, dict):
            raise ClusterRpcError("invalid workspace probe response")
        return result

    async def folder_sessions(
        self,
        machine_id: str,
        *,
        cwd: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        host, port = self._resolve_endpoint(machine_id)
        result = await self._client.call(
            host=host,
            port=port,
            method="agent/folder_sessions",
            params={"cwd": cwd, "limit": limit},
        )
        if not isinstance(result, dict):
            raise ClusterRpcError("invalid folder session response")
        items = result.get("items", [])
        return items if isinstance(items, list) else []

    async def list_threads(
        self,
        machine_id: str,
        *,
        max_items: int = SESSION_PANEL_LIST_LIMIT,
    ) -> tuple[list[str], str]:
        host, port = self._resolve_endpoint(machine_id)
        result = await self._client.call(
            host=host,
            port=port,
            method="agent/list_threads",
            params={"max_items": max_items},
        )
        if not isinstance(result, dict):
            raise ClusterRpcError("invalid thread list response")
        items = result.get("items", [])
        list_error = result.get("list_error", "")
        return (
            [item for item in items if isinstance(item, str) and item.strip()],
            list_error if isinstance(list_error, str) else "",
        )

    async def resume_latest(
        self,
        machine_id: str,
        *,
        window_id: str,
        cwd: str,
        window_name: str = "",
        approval_mode: str = "",
    ) -> dict[str, Any]:
        dispatch_fence = await self._require_codex_mutation_dispatch(
            machine_id=machine_id,
            operation="resume latest",
        )
        host, port = self._resolve_endpoint(machine_id)
        result = await self._call_codex_mutation(
            host=host,
            port=port,
            method="agent/resume_latest",
            params={
                "window_id": window_id,
                "cwd": cwd,
                "window_name": window_name,
                "approval_mode": approval_mode,
                **dispatch_fence,
            },
        )
        if not isinstance(result, dict):
            raise ClusterRpcError("invalid resume latest response")
        await self._record_remote_transport_result(
            machine_id=machine_id,
            window_id=window_id,
            result=result,
            operation="resume latest",
        )
        return result

    async def fork_thread(
        self,
        machine_id: str,
        *,
        window_id: str,
        thread_id: str,
        turn_id: str = "",
    ) -> dict[str, Any]:
        dispatch_fence = await self._require_codex_mutation_dispatch(
            machine_id=machine_id,
            operation="fork",
        )
        host, port = self._resolve_endpoint(machine_id)
        result = await self._call_codex_mutation(
            host=host,
            port=port,
            method="agent/fork_thread",
            params={
                "window_id": window_id,
                "thread_id": thread_id,
                "turn_id": turn_id,
                **dispatch_fence,
            },
        )
        if not isinstance(result, dict):
            raise ClusterRpcError("invalid fork response")
        await self._record_remote_transport_result(
            machine_id=machine_id,
            window_id=window_id,
            result=result,
            operation="fork",
        )
        return result

    async def rollback_thread(
        self,
        machine_id: str,
        *,
        window_id: str,
        thread_id: str,
        num_turns: int,
    ) -> dict[str, Any]:
        dispatch_fence = await self._require_codex_mutation_dispatch(
            machine_id=machine_id,
            operation="rollback",
        )
        host, port = self._resolve_endpoint(machine_id)
        result = await self._call_codex_mutation(
            host=host,
            port=port,
            method="agent/rollback_thread",
            params={
                "window_id": window_id,
                "thread_id": thread_id,
                "num_turns": num_turns,
                **dispatch_fence,
            },
        )
        if not isinstance(result, dict):
            raise ClusterRpcError("invalid rollback response")
        await self._record_remote_transport_result(
            machine_id=machine_id,
            window_id=window_id,
            result=result,
            operation="rollback",
        )
        return result

    async def read_documents(
        self,
        machine_id: str,
        *,
        workspace_dir: str,
        paths: list[str],
    ) -> list[tuple[str, bytes]]:
        host, port = self._resolve_endpoint(machine_id)
        result = await self._client.call(
            host=host,
            port=port,
            method="agent/read_documents",
            params={
                "workspace_dir": workspace_dir,
                "paths": paths,
            },
        )
        if not isinstance(result, dict):
            raise ClusterRpcError("invalid read documents response")
        documents = result.get("documents", [])
        if not isinstance(documents, list):
            return []
        resolved: list[tuple[str, bytes]] = []
        for item in documents:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            data_b64 = item.get("data_b64")
            if not isinstance(name, str) or not isinstance(data_b64, str):
                continue
            try:
                resolved.append((name, base64.b64decode(data_b64, validate=True)))
            except Exception:
                continue
        return resolved

    async def read_attachments(
        self,
        machine_id: str,
        *,
        workspace_dir: str,
        paths: list[str],
    ) -> dict[str, list[tuple[str, bytes]]]:
        host, port = self._resolve_endpoint(machine_id)
        result = await self._client.call(
            host=host,
            port=port,
            method="agent/read_attachments",
            params={
                "workspace_dir": workspace_dir,
                "paths": paths,
            },
        )
        if not isinstance(result, dict):
            raise ClusterRpcError("invalid read attachments response")

        resolved_documents: list[tuple[str, bytes]] = []
        documents = result.get("documents", [])
        if isinstance(documents, list):
            for item in documents:
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                data_b64 = item.get("data_b64")
                if not isinstance(name, str) or not isinstance(data_b64, str):
                    continue
                try:
                    resolved_documents.append(
                        (name, base64.b64decode(data_b64, validate=True))
                    )
                except Exception:
                    continue

        resolved_images: list[tuple[str, bytes]] = []
        images = result.get("images", [])
        if isinstance(images, list):
            for item in images:
                if not isinstance(item, dict):
                    continue
                media_type = item.get("media_type")
                data_b64 = item.get("data_b64")
                if not isinstance(media_type, str) or not isinstance(data_b64, str):
                    continue
                try:
                    resolved_images.append(
                        (media_type, base64.b64decode(data_b64, validate=True))
                    )
                except Exception:
                    continue

        resolved_videos: list[tuple[str, bytes]] = []
        videos = result.get("videos", [])
        if isinstance(videos, list):
            for item in videos:
                if not isinstance(item, dict):
                    continue
                media_type = item.get("media_type")
                data_b64 = item.get("data_b64")
                if not isinstance(media_type, str) or not isinstance(data_b64, str):
                    continue
                try:
                    resolved_videos.append(
                        (media_type, base64.b64decode(data_b64, validate=True))
                    )
                except Exception:
                    continue

        return {
            "documents": resolved_documents,
            "images": resolved_images,
            "videos": resolved_videos,
        }

    async def ensure_thread(
        self,
        machine_id: str,
        *,
        window_id: str,
        cwd: str,
        window_name: str = "",
        approval_mode: str = "",
        model_slug: str = "",
        reasoning_effort: str = "",
        service_tier: str = "",
    ) -> dict[str, Any]:
        dispatch_fence = await self._require_codex_mutation_dispatch(
            machine_id=machine_id,
            operation="ensure thread",
        )
        host, port = self._resolve_endpoint(machine_id)
        result = await self._call_codex_mutation(
            host=host,
            port=port,
            method="agent/ensure_thread",
            params={
                "window_id": window_id,
                "cwd": cwd,
                "window_name": window_name,
                "approval_mode": approval_mode,
                "model_slug": model_slug,
                "reasoning_effort": reasoning_effort,
                "service_tier": service_tier,
                **dispatch_fence,
            },
        )
        if not isinstance(result, dict):
            raise ClusterRpcError("invalid ensure thread response")
        await self._record_remote_transport_result(
            machine_id=machine_id,
            window_id=window_id,
            result=result,
            operation="ensure thread",
        )
        return result

    async def resume_thread(
        self,
        machine_id: str,
        *,
        window_id: str,
        cwd: str,
        thread_id: str,
        window_name: str = "",
        approval_mode: str = "",
    ) -> dict[str, Any]:
        dispatch_fence = await self._require_codex_mutation_dispatch(
            machine_id=machine_id,
            operation="resume thread",
        )
        host, port = self._resolve_endpoint(machine_id)
        result = await self._call_codex_mutation(
            host=host,
            port=port,
            method="agent/resume_thread",
            params={
                "window_id": window_id,
                "cwd": cwd,
                "thread_id": thread_id,
                "window_name": window_name,
                "approval_mode": approval_mode,
                **dispatch_fence,
            },
        )
        if not isinstance(result, dict):
            raise ClusterRpcError("invalid resume thread response")
        await self._record_remote_transport_result(
            machine_id=machine_id,
            window_id=window_id,
            result=result,
            operation="resume thread",
        )
        return result

    async def send_inputs(
        self,
        machine_id: str,
        *,
        window_id: str,
        cwd: str,
        window_name: str,
        inputs: list[dict[str, Any]],
        steer: bool,
        force_new_turn: bool = False,
        thread_id: str = "",
        approval_mode: str = "",
        model_slug: str = "",
        reasoning_effort: str = "",
        service_tier: str = "",
    ) -> dict[str, Any]:
        dispatch_fence = await self._require_codex_mutation_dispatch(
            machine_id=machine_id,
            operation="send",
        )
        host, port = self._resolve_endpoint(machine_id)
        result = await self._call_codex_mutation(
            host=host,
            port=port,
            method="agent/send_inputs",
            params={
                "window_id": window_id,
                "cwd": cwd,
                "window_name": window_name,
                "inputs": inputs,
                "steer": steer,
                "force_new_turn": force_new_turn,
                "thread_id": thread_id,
                "approval_mode": approval_mode,
                "model_slug": model_slug,
                "reasoning_effort": reasoning_effort,
                "service_tier": service_tier,
                **dispatch_fence,
            },
        )
        if not isinstance(result, dict):
            raise ClusterRpcError("invalid send response")
        return result

    async def thread_goal_get(
        self,
        machine_id: str,
        *,
        thread_id: str,
    ) -> dict[str, Any]:
        host, port = self._resolve_endpoint(machine_id)
        result = await self._client.call(
            host=host,
            port=port,
            method="agent/thread_goal_get",
            params={"thread_id": thread_id},
        )
        if not isinstance(result, dict):
            raise ClusterRpcError("invalid goal get response")
        return result

    async def thread_goal_set(
        self,
        machine_id: str,
        *,
        thread_id: str,
        goal: str,
    ) -> dict[str, Any]:
        dispatch_fence = await self._require_codex_mutation_dispatch(
            machine_id=machine_id,
            operation="goal set",
        )
        host, port = self._resolve_endpoint(machine_id)
        result = await self._call_codex_mutation(
            host=host,
            port=port,
            method="agent/thread_goal_set",
            params={
                "thread_id": thread_id,
                "goal": goal,
                **dispatch_fence,
            },
        )
        if not isinstance(result, dict):
            raise ClusterRpcError("invalid goal set response")
        return result

    async def thread_goal_clear(
        self,
        machine_id: str,
        *,
        thread_id: str,
    ) -> dict[str, Any]:
        dispatch_fence = await self._require_codex_mutation_dispatch(
            machine_id=machine_id,
            operation="goal clear",
        )
        host, port = self._resolve_endpoint(machine_id)
        result = await self._call_codex_mutation(
            host=host,
            port=port,
            method="agent/thread_goal_clear",
            params={"thread_id": thread_id, **dispatch_fence},
        )
        if not isinstance(result, dict):
            raise ClusterRpcError("invalid goal clear response")
        return result

    async def run_update(
        self,
        machine_id: str,
        *,
        action: str,
        notice_chat_id: int,
        notice_thread_id: int | None,
    ) -> dict[str, Any]:
        host, port = self._resolve_endpoint(machine_id)
        result = await self._client.call(
            host=host,
            port=port,
            method="agent/run_update",
            params={
                "action": action,
                "notice_chat_id": notice_chat_id,
                "notice_thread_id": notice_thread_id,
            },
        )
        if not isinstance(result, dict):
            raise ClusterRpcError("invalid run update response")
        return result


agent_rpc_client = AgentRpcClient(shared_secret=config.cluster_shared_secret)


def _resolve_repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _run_command_sync(
    argv: list[str],
    *,
    cwd: str | Path | None = None,
    timeout_seconds: float = 600.0,
) -> tuple[bool, str, str, str]:
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        return False, "", "", str(exc)
    except subprocess.TimeoutExpired as exc:
        stdout = (
            exc.stdout.decode("utf-8", errors="replace")
            if isinstance(exc.stdout, bytes)
            else exc.stdout or ""
        )
        stderr = (
            exc.stderr.decode("utf-8", errors="replace")
            if isinstance(exc.stderr, bytes)
            else exc.stderr or ""
        )
        return False, stdout, stderr, "timeout"
    except OSError as exc:
        return False, "", "", str(exc)
    return proc.returncode == 0, proc.stdout, proc.stderr, ""


def _tail_text(value: str, *, limit: int = 280) -> str:
    text = " ".join(value.strip().split())
    if len(text) <= limit:
        return text
    return "…" + text[-(limit - 1) :]


def _resolve_codex_upgrade_command() -> tuple[str, str]:
    custom = env_alias(_CODEX_UPGRADE_COMMAND_ENV)
    if custom:
        return custom, "custom"
    codex_binary = shutil.which("codex") or ""
    codex_realpath = os.path.realpath(codex_binary) if codex_binary else ""
    normalized_binary = codex_realpath.lower().replace("\\", "/")
    if "/node_modules/@openai/codex/" in normalized_binary:
        return "npm install -g @openai/codex@latest", "npm"
    if "/pipx/venvs/" in normalized_binary:
        return "pipx upgrade codex", "pipx"
    if "/uv/tools/" in normalized_binary:
        return "uv tool upgrade codex", "uv"
    if shutil.which("uv"):
        return "uv tool upgrade codex", "uv"
    if shutil.which("pipx"):
        return "pipx upgrade codex", "pipx"
    if shutil.which("npm"):
        return "npm install -g @openai/codex@latest", "npm"
    return "", "none"


def _run_remote_codex_upgrade_sync() -> tuple[bool, str]:
    command, _source = _resolve_codex_upgrade_command()
    if not command:
        return False, "No supported Codex upgrade command found."
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return False, f"Invalid upgrade command syntax: {exc}"
    ok, stdout, stderr, err = _run_command_sync(argv, cwd=_resolve_repo_root())
    if not ok:
        return False, f"Codex upgrade failed: {_tail_text(stderr or stdout or err or 'unknown error')}"
    return True, "Codex upgrade completed."


def _run_remote_coco_update_sync() -> tuple[bool, str]:
    repo_root = _resolve_repo_root()
    custom = env_alias(_COCO_SELF_UPDATE_COMMAND_ENV)
    if not (repo_root / ".git").exists():
        update_argv = (
            ["bash", "-lc", custom]
            if custom
            else _resolve_coco_tool_update_argv()
        )
        if not update_argv:
            return False, (
                "CoCo update unavailable: runtime is not a git checkout and uv "
                "was not found for package reinstall."
            )
        ok, stdout, stderr, err = _run_command_sync(update_argv, cwd=Path.home())
        if not ok:
            return False, (
                "CoCo package update failed: "
                f"{_tail_text(stderr or stdout or err or 'unknown error')}"
            )
        return True, "CoCo package updated."

    if custom:
        ok, stdout, stderr, err = _run_command_sync(["bash", "-lc", custom], cwd=repo_root)
        if not ok:
            return False, f"CoCo update failed: {_tail_text(stderr or stdout or err or 'unknown error')}"
        return True, "CoCo update completed."

    ok, stdout, stderr, err = _run_command_sync(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=repo_root,
    )
    if not ok:
        return False, f"CoCo update failed: {_tail_text(stderr or stdout or err or 'git status failed')}"
    if stdout.strip():
        return False, "CoCo update blocked: worktree has local changes."

    ok, stdout, stderr, err = _run_command_sync(["git", "pull", "--ff-only"], cwd=repo_root)
    if not ok:
        return False, f"CoCo update failed: {_tail_text(stderr or stdout or err or 'git pull failed')}"
    if shutil.which("uv") and (repo_root / "pyproject.toml").is_file():
        ok, stdout, stderr, err = _run_command_sync(["uv", "sync"], cwd=repo_root)
        if not ok:
            return False, f"CoCo dependency sync failed: {_tail_text(stderr or stdout or err or 'uv sync failed')}"
    return True, "CoCo update completed."


def _queue_remote_restart() -> None:
    global _remote_restart_requested
    if _remote_restart_requested:
        return
    _remote_restart_requested = True
    asyncio.create_task(_restart_remote_process_after_delay())


async def _restart_remote_process_after_delay(delay_seconds: float = 0.25) -> None:
    global _remote_restart_requested
    await asyncio.sleep(delay_seconds)
    argv = [sys.executable, *sys.argv]
    try:
        os.execv(sys.executable, argv)
    except Exception:
        _remote_restart_requested = False
        logger.exception("Failed to restart remote CoCo process")
