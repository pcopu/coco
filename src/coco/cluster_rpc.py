"""Small authenticated line-delimited JSON RPC transport for controller/agent nodes."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any


class ClusterRpcError(RuntimeError):
    """Raised for cluster RPC transport or application errors."""


RpcHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | list[Any] | str | int | float | None]]


class ClusterRpcServer:
    """Async JSON-RPC server over newline-delimited TCP frames."""

    def __init__(self, *, shared_secret: str) -> None:
        self._shared_secret = shared_secret.strip()
        self._handlers: dict[str, RpcHandler] = {}
        self._server: asyncio.AbstractServer | None = None

    def register(self, method: str, handler: RpcHandler) -> None:
        self._handlers[method] = handler

    async def start(self, *, host: str, port: int) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(self._handle_client, host=host, port=port)

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    def bound_address(self) -> tuple[str, int]:
        if self._server is None or not self._server.sockets:
            raise ClusterRpcError("RPC server is not running")
        sock = self._server.sockets[0]
        host, port = sock.getsockname()[:2]
        return str(host), int(port)

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                response = await self._handle_request_line(raw)
                writer.write((json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8"))
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def _handle_request_line(self, raw: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"id": "", "ok": False, "error": "invalid_json"}

        request_id = str(payload.get("id", "")).strip()
        secret = str(payload.get("secret", "")).strip()
        method = str(payload.get("method", "")).strip()
        params = payload.get("params", {})
        if secret != self._shared_secret:
            return {"id": request_id, "ok": False, "error": "unauthorized"}
        if method not in self._handlers:
            return {"id": request_id, "ok": False, "error": "unknown_method"}
        if not isinstance(params, dict):
            return {"id": request_id, "ok": False, "error": "invalid_params"}

        try:
            result = await self._handlers[method](params)
        except Exception as exc:
            return {"id": request_id, "ok": False, "error": str(exc) or "handler_error"}
        return {"id": request_id, "ok": True, "result": result}


class ClusterRpcClient:
    """Async JSON-RPC client matching ClusterRpcServer framing."""

    def __init__(self, *, shared_secret: str, timeout_seconds: float = 30.0) -> None:
        self._shared_secret = shared_secret.strip()
        self._timeout_seconds = float(timeout_seconds)

    async def call(
        self,
        *,
        host: str,
        port: int,
        method: str,
        params: dict[str, Any],
    ) -> Any:
        reader: asyncio.StreamReader
        writer: asyncio.StreamWriter
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=self._timeout_seconds,
            )
        except Exception as exc:
            raise ClusterRpcError(str(exc) or "connect_failed") from exc

        request_id = uuid.uuid4().hex
        payload = {
            "id": request_id,
            "secret": self._shared_secret,
            "method": method,
            "params": params,
        }
        try:
            writer.write((json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"))
            await writer.drain()
            raw = await asyncio.wait_for(reader.readline(), timeout=self._timeout_seconds)
        finally:
            writer.close()
            await writer.wait_closed()

        if not raw:
            raise ClusterRpcError("empty_response")
        try:
            response = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ClusterRpcError("invalid_response") from exc
        if response.get("id") != request_id:
            raise ClusterRpcError("mismatched_response")
        if response.get("ok") is not True:
            raise ClusterRpcError(str(response.get("error", "rpc_error")))
        return response.get("result")
