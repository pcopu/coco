"""Controller-side RPC server and agent-side client for cluster events."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .cluster_rpc import ClusterRpcClient, ClusterRpcError, ClusterRpcServer
from .config import config


HeartbeatHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]
NotificationHandler = Callable[[dict[str, Any]], Awaitable[None]]
RequestHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]


class ControllerRpcServer:
    """Controller listener used by remote agents for heartbeats and app-server events."""

    def __init__(
        self,
        *,
        shared_secret: str,
        heartbeat_handler: HeartbeatHandler,
        notification_handler: NotificationHandler,
        request_handler: RequestHandler,
    ) -> None:
        self._server = ClusterRpcServer(shared_secret=shared_secret)
        self._server.register("controller/heartbeat", heartbeat_handler)
        self._server.register("controller/notification", self._wrap_notification(notification_handler))
        self._server.register("controller/request", request_handler)

    @staticmethod
    def _wrap_notification(
        handler: NotificationHandler,
    ) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]:
        async def _wrapped(params: dict[str, Any]) -> dict[str, Any] | None:
            await handler(params)
            return {"ok": True}

        return _wrapped

    async def start(self, *, host: str, port: int) -> None:
        await self._server.start(host=host, port=port)

    async def stop(self) -> None:
        await self._server.stop()

    def bound_address(self) -> tuple[str, int]:
        return self._server.bound_address()


class ControllerRpcClient:
    """Agent-side client for controller callbacks."""

    def __init__(self, *, shared_secret: str) -> None:
        self._client = ClusterRpcClient(shared_secret=shared_secret)

    async def heartbeat(self, params: dict[str, Any]) -> dict[str, Any] | None:
        if not config.controller_rpc_host:
            raise ClusterRpcError("controller RPC host is not configured")
        result = await self._client.call(
            host=config.controller_rpc_host,
            port=config.controller_rpc_port,
            method="controller/heartbeat",
            params=params,
        )
        return result if isinstance(result, dict) else None

    async def notification(self, *, method: str, params: dict[str, Any]) -> None:
        if not config.controller_rpc_host:
            raise ClusterRpcError("controller RPC host is not configured")
        await self._client.call(
            host=config.controller_rpc_host,
            port=config.controller_rpc_port,
            method="controller/notification",
            params={"method": method, "params": params},
        )

    async def request(self, *, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        if not config.controller_rpc_host:
            raise ClusterRpcError("controller RPC host is not configured")
        result = await self._client.call(
            host=config.controller_rpc_host,
            port=config.controller_rpc_port,
            method="controller/request",
            params={"method": method, "params": params},
        )
        return result if isinstance(result, dict) else None
