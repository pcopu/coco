"""Small authenticated line-delimited JSON RPC transport for controller/agent nodes."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any


class ClusterRpcError(RuntimeError):
    """Raised for cluster RPC transport or application errors."""


RpcHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | list[Any] | str | int | float | None]]

# Attachment responses are base64-encoded into one JSON line.  The asyncio
# default (64 KiB) is too small even for ordinary Telegram documents.
RPC_REQUEST_STREAM_LIMIT_BYTES = 1024 * 1024
RPC_RESPONSE_STREAM_LIMIT_BYTES = 128 * 1024 * 1024
RPC_REQUEST_TIMEOUT_SECONDS = 10.0
RPC_MAX_ACTIVE_CONNECTIONS = 64


class ClusterRpcServer:
    """Async JSON-RPC server over newline-delimited TCP frames."""

    def __init__(self, *, shared_secret: str) -> None:
        self._shared_secret = shared_secret.strip()
        self._handlers: dict[str, RpcHandler] = {}
        self._server: asyncio.AbstractServer | None = None
        self._active_connections = 0

    def register(self, method: str, handler: RpcHandler) -> None:
        self._handlers[method] = handler

    async def start(self, *, host: str, port: int) -> None:
        if self._server is not None:
            return
        normalized_host = host.strip().lower()
        is_loopback = normalized_host == "localhost"
        if not is_loopback:
            with contextlib.suppress(ValueError):
                is_loopback = ipaddress.ip_address(normalized_host).is_loopback
        if not self._shared_secret and not is_loopback:
            raise ClusterRpcError(
                "cluster shared secret is required for a non-loopback listener"
            )
        self._server = await asyncio.start_server(
            self._handle_client,
            host=host,
            port=port,
            limit=RPC_REQUEST_STREAM_LIMIT_BYTES,
        )

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
        if self._active_connections >= RPC_MAX_ACTIVE_CONNECTIONS:
            writer.close()
            with contextlib.suppress(OSError, TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
            return
        self._active_connections += 1
        try:
            while True:
                try:
                    async with asyncio.timeout(RPC_REQUEST_TIMEOUT_SECONDS):
                        raw = await reader.readline()
                except TimeoutError:
                    break
                if not raw:
                    break
                response = await self._handle_request_line(raw)
                writer.write((json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8"))
                await writer.drain()
                # The client protocol is one request per connection. Closing
                # here also prevents unauthenticated keep-alive sockets from
                # occupying every server connection slot indefinitely.
                break
        finally:
            self._active_connections -= 1
            writer.close()
            with contextlib.suppress(OSError, TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)

    async def _handle_request_line(self, raw: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"id": "", "ok": False, "error": "invalid_json"}
        if not isinstance(payload, dict):
            return {"id": "", "ok": False, "error": "invalid_request"}

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
        response = {"id": request_id, "ok": True, "result": result}
        try:
            json.dumps(response)
        except (TypeError, ValueError):
            return {"id": request_id, "ok": False, "error": "invalid_result"}
        return response


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
                asyncio.open_connection(host, port, limit=RPC_RESPONSE_STREAM_LIMIT_BYTES),
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
            try:
                async with asyncio.timeout(self._timeout_seconds):
                    await writer.drain()
                    raw = await reader.readline()
            except TimeoutError as exc:
                raise ClusterRpcError("request_timeout") from exc
        finally:
            writer.close()
            with contextlib.suppress(OSError, TimeoutError):
                await asyncio.wait_for(
                    writer.wait_closed(),
                    timeout=self._timeout_seconds,
                )

        if not raw:
            raise ClusterRpcError("empty_response")
        try:
            response = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ClusterRpcError("invalid_response") from exc
        if not isinstance(response, dict):
            raise ClusterRpcError("invalid_response")
        if response.get("id") != request_id:
            raise ClusterRpcError("mismatched_response")
        if response.get("ok") is not True:
            raise ClusterRpcError(str(response.get("error", "rpc_error")))
        return response.get("result")
