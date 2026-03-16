"""Codex app-server JSON-RPC client.

Provides a lightweight async client over stdio for the experimental
`codex app-server` protocol. The client tracks active turns and exposes
helpers for thread/turn operations used by Telegram handlers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import shutil
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import config

logger = logging.getLogger(__name__)

NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None]]
ServerRequestHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any] | None]]

# Default asyncio StreamReader limit (64 KiB) is too small for some app-server
# JSONL payloads; use a larger cap to avoid read-loop termination.
APP_SERVER_STREAM_LIMIT = 1024 * 1024
TIMEOUT_RECYCLE_METHODS = frozenset({"thread/start", "turn/start", "turn/steer"})


class CodexAppServerError(RuntimeError):
    """Raised for app-server request/transport failures."""


class CodexAppServerClient:
    """Minimal JSON-RPC client for `codex app-server` over stdio."""

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._notification_task: asyncio.Task[None] | None = None
        self._notification_queue: asyncio.Queue[tuple[str, dict[str, Any]]] = (
            asyncio.Queue()
        )
        self._write_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._request_id = 1
        self._pending: dict[str, asyncio.Future[Any]] = {}

        self._notification_handler: NotificationHandler | None = None
        self._server_request_handler: ServerRequestHandler | None = None

        self._active_turns: dict[str, str] = {}
        self._thread_token_usage: dict[str, dict[str, Any]] = {}
        self._rate_limits: dict[str, Any] | None = None
        self._initialized = False
        self._server_user_agent = ""
        self._transport_needs_restart = False

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

        # By default, Codex may sandbox tool execution (read-only + no network),
        # which breaks common workflows like `git pull`. Configure the app-server
        # process to inherit the desired sandbox mode.
        sandbox_mode = getattr(config, "codex_sandbox_mode", "").strip()
        if sandbox_mode:
            argv.extend(["-c", f'sandbox_mode="{sandbox_mode}"'])

        return argv

    async def set_handlers(
        self,
        *,
        notification_handler: NotificationHandler | None = None,
        server_request_handler: ServerRequestHandler | None = None,
    ) -> None:
        self._notification_handler = notification_handler
        self._server_request_handler = server_request_handler

    async def ensure_started(self) -> None:
        if self._is_transport_ready():
            return

        async with self._start_lock:
            if self._is_transport_ready():
                return

            if self._proc and self._proc.returncode is None and (
                self._transport_needs_restart or not self._initialized
            ):
                logger.warning(
                    "Recycling unhealthy Codex app-server transport "
                    "(initialized=%s, needs_restart=%s)",
                    self._initialized,
                    self._transport_needs_restart,
                )
                await self.stop()

            if not self._proc or self._proc.returncode is not None:
                argv = self._app_server_argv()
                logger.info("Starting Codex app-server: %s", argv)
                try:
                    self._proc = await asyncio.create_subprocess_exec(
                        *argv,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        limit=APP_SERVER_STREAM_LIMIT,
                    )
                except OSError as e:
                    raise CodexAppServerError(
                        f"Failed to start codex app-server: {e}"
                    ) from e

                self._initialized = False
                self._server_user_agent = ""
                self._transport_needs_restart = False
                self._reader_task = asyncio.create_task(self._reader_loop())
                self._stderr_task = asyncio.create_task(self._stderr_loop())
                self._ensure_notification_worker()

            try:
                await self._run_initialize_handshake()
            except Exception:
                await self.stop()
                raise

    def is_running(self) -> bool:
        """Return whether the app-server process is currently running."""
        return bool(self._proc and self._proc.returncode is None)

    async def stop(self) -> None:
        proc = self._proc
        self._proc = None

        for task in (self._reader_task, self._stderr_task, self._notification_task):
            if task:
                task.cancel()
        self._reader_task = None
        self._stderr_task = None
        self._notification_task = None
        # Drop any queued notifications from the previous process lifecycle.
        self._notification_queue = asyncio.Queue()

        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except TimeoutError:
                proc.kill()
                await proc.wait()

        for key, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(CodexAppServerError("codex app-server stopped"))
            self._pending.pop(key, None)

        self._active_turns.clear()
        self._thread_token_usage.clear()
        self._initialized = False
        self._server_user_agent = ""
        self._transport_needs_restart = False

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
                method, params = await self._notification_queue.get()
                handler = self._notification_handler
                if not handler:
                    continue
                try:
                    await handler(method, params)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("app-server notification handler failed: %s", method)
        except asyncio.CancelledError:
            return

    async def _handle_message(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        has_id = "id" in msg

        # Server request (method + id)
        if isinstance(method, str) and has_id:
            req_id = msg.get("id")
            params = msg.get("params")
            params_dict = params if isinstance(params, dict) else {}
            result: dict[str, Any] | None = None
            if self._server_request_handler:
                try:
                    result = await self._server_request_handler(method, params_dict)
                except Exception as e:
                    logger.exception("app-server request handler failed (%s): %s", method, e)
            if result is None:
                result = self._default_server_request_result(method, params_dict)
            await self._write_response(req_id, result=result)
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
                self._notification_queue.put_nowait((method, params_dict))
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

    async def _write_jsonrpc(self, payload: dict[str, Any]) -> None:
        proc = self._proc
        if not proc or not proc.stdin:
            raise CodexAppServerError("codex app-server is not running")

        # Codex CLI app-server currently expects JSONL over stdio.
        # Reader remains dual-format to tolerate framed responses.
        raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        frame = raw + b"\n"

        async with self._write_lock:
            proc.stdin.write(frame)
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
            await self._write_jsonrpc(payload)
        except Exception:
            self._pending.pop(req_id, None)
            raise

        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except TimeoutError as e:
            self._pending.pop(req_id, None)
            raise CodexAppServerError(
                f"Timed out waiting for app-server response: {method}"
            ) from e

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
    ) -> Any:
        await self.ensure_started()
        try:
            return await self._request_started(method, params, timeout=timeout)
        except CodexAppServerError as e:
            if not self._is_timeout_recycle_candidate(method, e):
                raise
            logger.warning(
                "App-server request timed out (%s); recycling transport and retrying once",
                method,
            )
            await self.stop()
            await self.ensure_started()
            return await self._request_started(method, params, timeout=timeout)

    @staticmethod
    def _is_timeout_recycle_candidate(method: str, err: Exception) -> bool:
        if method not in TIMEOUT_RECYCLE_METHODS:
            return False
        if not isinstance(err, CodexAppServerError):
            return False
        return f"Timed out waiting for app-server response: {method}" in str(err)

    async def thread_start(
        self,
        *,
        cwd: str | None = None,
        approval_policy: str | None = None,
        model: str | None = None,
        effort: str | None = None,
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
        result = await self.request("thread/start", params, timeout=120.0)
        return result if isinstance(result, dict) else {}

    async def thread_fork(
        self,
        *,
        thread_id: str,
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"threadId": thread_id}
        if turn_id:
            params["turnId"] = turn_id
        result = await self.request("thread/fork", params, timeout=120.0)
        return result if isinstance(result, dict) else {}

    async def thread_resume(
        self,
        *,
        thread_id: str,
    ) -> dict[str, Any]:
        params = {"threadId": thread_id}
        result = await self.request("thread/resume", params, timeout=120.0)
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
    ) -> dict[str, Any]:
        params = {"threadId": thread_id}
        result = await self.request("thread/read", params, timeout=60.0)
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

    async def turn_start(
        self,
        *,
        thread_id: str,
        inputs: list[dict[str, Any]],
        approval_policy: str | None = None,
        timeout: float = 90.0,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": inputs,
        }
        if approval_policy:
            params["approvalPolicy"] = approval_policy
        result = await self.request("turn/start", params, timeout=timeout)
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
        timeout: float = 90.0,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "expectedTurnId": expected_turn_id,
            "input": inputs,
        }
        result = await self.request("turn/steer", params, timeout=timeout)
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
