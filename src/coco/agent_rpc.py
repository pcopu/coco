"""High-level agent RPC surface for multi-machine controller -> agent calls."""

from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path
from typing import Any

from .cluster_rpc import ClusterRpcClient, ClusterRpcError, ClusterRpcServer
from .codex_app_server import codex_app_server_client
from .config import config
from .handlers.directory_browser import clamp_browse_path, resolve_browse_root
from .node_registry import node_registry
from .session import session_manager


logger = logging.getLogger(__name__)

SESSION_PANEL_LIST_REQUEST_LIMIT = 50
SESSION_PANEL_LIST_LIMIT = 100
ALLOWED_TELEGRAM_DOCUMENT_EXTENSIONS = {".pdf", ".txt", ".md"}
TELEGRAM_DOCUMENT_MAX_BYTES = 45 * 1024 * 1024


def _resolve_document_attachment(
    *,
    workspace_dir: str,
    raw_path: str,
) -> tuple[str, bytes] | None:
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

    if resolved.suffix.lower() not in ALLOWED_TELEGRAM_DOCUMENT_EXTENSIONS:
        return None
    if not resolved.is_file():
        return None
    try:
        size = resolved.stat().st_size
    except OSError:
        return None
    if size > TELEGRAM_DOCUMENT_MAX_BYTES:
        return None
    try:
        return resolved.name, resolved.read_bytes()
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
        self._server.register("agent/probe_machine", self._probe_machine)
        self._server.register("agent/browse", self._browse)
        self._server.register("agent/folder_sessions", self._folder_sessions)
        self._server.register("agent/list_threads", self._list_threads)
        self._server.register("agent/ensure_thread", self._ensure_thread)
        self._server.register("agent/fork_thread", self._fork_thread)
        self._server.register("agent/rollback_thread", self._rollback_thread)
        self._server.register("agent/read_documents", self._read_documents)
        self._server.register("agent/resume_latest", self._resume_latest)
        self._server.register("agent/resume_thread", self._resume_thread)
        self._server.register("agent/send_inputs", self._send_inputs)

    async def start(self, *, host: str, port: int) -> None:
        await self._server.start(host=host, port=port)

    async def stop(self) -> None:
        await self._server.stop()

    def bound_address(self) -> tuple[str, int]:
        return self._server.bound_address()

    async def _ping(self, _params: dict[str, Any]) -> dict[str, Any]:
        node = node_registry.ensure_local_node(now=asyncio.get_running_loop().time())
        return node.to_dict()

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
        window_id = str(params.get("window_id", "")).strip()
        cwd = str(params.get("cwd", "")).strip()
        window_name = str(params.get("window_name", "")).strip()
        approval_mode = str(params.get("approval_mode", "")).strip()
        model_slug = str(params.get("model_slug", "")).strip()
        reasoning_effort = str(params.get("reasoning_effort", "")).strip()
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
        thread_id, _approval = await session_manager._ensure_codex_thread_for_window(
            window_id=window_id,
            cwd=cwd,
            **ensure_kwargs,
        )
        return {
            "thread_id": thread_id,
            "turn_id": session_manager.get_window_codex_active_turn_id(window_id),
        }

    async def _resume_latest(self, params: dict[str, Any]) -> dict[str, Any]:
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
        }

    async def _fork_thread(self, params: dict[str, Any]) -> dict[str, Any]:
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
        return {"thread_id": forked_thread_id, "turn_id": forked_turn_id}

    async def _rollback_thread(self, params: dict[str, Any]) -> dict[str, Any]:
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
        return {"thread_id": rolled_thread_id, "turn_id": rolled_turn_id}

    async def _read_documents(self, params: dict[str, Any]) -> dict[str, Any]:
        workspace_dir = str(params.get("workspace_dir", "")).strip()
        raw_paths = params.get("paths", [])
        if not isinstance(raw_paths, list):
            raise ClusterRpcError("paths must be a list")
        documents: list[dict[str, str]] = []
        for raw_path in raw_paths:
            if not isinstance(raw_path, str):
                continue
            resolved = _resolve_document_attachment(
                workspace_dir=workspace_dir,
                raw_path=raw_path,
            )
            if resolved is None:
                continue
            name, raw_bytes = resolved
            documents.append(
                {
                    "name": name,
                    "data_b64": base64.b64encode(raw_bytes).decode("ascii"),
                }
            )
        return {"documents": documents}

    async def _resume_thread(self, params: dict[str, Any]) -> dict[str, Any]:
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
            codex_thread_id=requested_thread_id,
        )
        result = await codex_app_server_client.thread_resume(thread_id=requested_thread_id)
        resumed_thread_id = session_manager._extract_lifecycle_thread_id(
            result,
            fallback=requested_thread_id,
        )
        resumed_turn_id = session_manager._extract_lifecycle_turn_id(result)
        session_manager.set_window_codex_thread_id(window_id, resumed_thread_id)
        session_manager.set_window_codex_active_turn_id(window_id, resumed_turn_id)
        model_slug, reasoning_effort = session_manager.get_codex_session_model_selection_for_thread(
            resumed_thread_id,
            cwd=cwd,
        ) if resumed_thread_id else ("", "")
        return {
            "thread_id": resumed_thread_id,
            "turn_id": resumed_turn_id,
            "model_slug": model_slug,
            "reasoning_effort": reasoning_effort,
        }

    async def _send_inputs(self, params: dict[str, Any]) -> dict[str, Any]:
        window_id = str(params.get("window_id", "")).strip()
        cwd = str(params.get("cwd", "")).strip()
        window_name = str(params.get("window_name", "")).strip()
        approval_mode = str(params.get("approval_mode", "")).strip()
        codex_thread_id = str(params.get("thread_id", "")).strip()
        model_slug = str(params.get("model_slug", "")).strip()
        reasoning_effort = str(params.get("reasoning_effort", "")).strip()
        steer = bool(params.get("steer", False))
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
        ok, message = await session_manager.send_inputs_to_window(
            window_id,
            inputs,
            steer=steer,
            model_slug=model_slug,
            reasoning_effort=reasoning_effort,
        )
        state = session_manager.get_window_state(window_id)
        return {
            "ok": ok,
            "message": message,
            "thread_id": state.codex_thread_id,
            "turn_id": state.codex_active_turn_id,
        }


class AgentRpcClient:
    """Controller-side high-level client for agent RPC operations."""

    def __init__(self, *, shared_secret: str) -> None:
        self._client = ClusterRpcClient(shared_secret=shared_secret)

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
        host, port = self._resolve_endpoint(machine_id)
        result = await self._client.call(
            host=host,
            port=port,
            method="agent/resume_latest",
            params={
                "window_id": window_id,
                "cwd": cwd,
                "window_name": window_name,
                "approval_mode": approval_mode,
            },
        )
        if not isinstance(result, dict):
            raise ClusterRpcError("invalid resume latest response")
        return result

    async def fork_thread(
        self,
        machine_id: str,
        *,
        window_id: str,
        thread_id: str,
        turn_id: str = "",
    ) -> dict[str, Any]:
        host, port = self._resolve_endpoint(machine_id)
        result = await self._client.call(
            host=host,
            port=port,
            method="agent/fork_thread",
            params={
                "window_id": window_id,
                "thread_id": thread_id,
                "turn_id": turn_id,
            },
        )
        if not isinstance(result, dict):
            raise ClusterRpcError("invalid fork response")
        return result

    async def rollback_thread(
        self,
        machine_id: str,
        *,
        window_id: str,
        thread_id: str,
        num_turns: int,
    ) -> dict[str, Any]:
        host, port = self._resolve_endpoint(machine_id)
        result = await self._client.call(
            host=host,
            port=port,
            method="agent/rollback_thread",
            params={
                "window_id": window_id,
                "thread_id": thread_id,
                "num_turns": num_turns,
            },
        )
        if not isinstance(result, dict):
            raise ClusterRpcError("invalid rollback response")
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
                resolved.append((name, base64.b64decode(data_b64)))
            except Exception:
                continue
        return resolved

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
    ) -> dict[str, Any]:
        host, port = self._resolve_endpoint(machine_id)
        result = await self._client.call(
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
            },
        )
        if not isinstance(result, dict):
            raise ClusterRpcError("invalid ensure thread response")
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
        host, port = self._resolve_endpoint(machine_id)
        result = await self._client.call(
            host=host,
            port=port,
            method="agent/resume_thread",
            params={
                "window_id": window_id,
                "cwd": cwd,
                "thread_id": thread_id,
                "window_name": window_name,
                "approval_mode": approval_mode,
            },
        )
        if not isinstance(result, dict):
            raise ClusterRpcError("invalid resume thread response")
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
        thread_id: str = "",
        approval_mode: str = "",
        model_slug: str = "",
        reasoning_effort: str = "",
    ) -> dict[str, Any]:
        host, port = self._resolve_endpoint(machine_id)
        result = await self._client.call(
            host=host,
            port=port,
            method="agent/send_inputs",
            params={
                "window_id": window_id,
                "cwd": cwd,
                "window_name": window_name,
                "inputs": inputs,
                "steer": steer,
                "thread_id": thread_id,
                "approval_mode": approval_mode,
                "model_slug": model_slug,
                "reasoning_effort": reasoning_effort,
            },
        )
        if not isinstance(result, dict):
            raise ClusterRpcError("invalid send response")
        return result


agent_rpc_client = AgentRpcClient(shared_secret=config.cluster_shared_secret)
