"""Agent-only runtime bootstrap."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from .agent_rpc import AgentRpcServer
from .codex_app_server import codex_app_server_client
from .config import config
from .controller_rpc import ControllerRpcClient
from .node_registry import node_registry


logger = logging.getLogger(__name__)


async def _heartbeat_loop(controller_client: ControllerRpcClient) -> None:
    while True:
        try:
            node = node_registry.ensure_local_node(transport="agent_rpc")
            await controller_client.heartbeat(node.to_dict())
        except Exception as exc:
            logger.warning("Agent heartbeat failed: %s", exc)
        await asyncio.sleep(max(5.0, float(config.node_heartbeat_interval)))


async def run_agent_async() -> None:
    """Start the non-Telegram agent runtime."""
    logger.info("Starting CoCo agent")
    logger.info("Machine: %s (%s)", config.machine_name, config.machine_id)
    logger.info("Tailnet name: %s", config.tailnet_name or "<unset>")
    logger.info("Sessions path: %s", config.sessions_path)
    logger.info("Assistant command: %s", config.assistant_command)

    node_registry.ensure_local_node(transport="agent_rpc")
    server = AgentRpcServer(shared_secret=config.cluster_shared_secret)
    await server.start(host=config.rpc_listen_host, port=config.rpc_port)
    bound_host, bound_port = server.bound_address()
    logger.info("Agent RPC listening on %s:%s", bound_host, bound_port)

    controller_client: ControllerRpcClient | None = None
    heartbeat_task: asyncio.Task[None] | None = None
    if config.controller_rpc_host:
        controller_client = ControllerRpcClient(shared_secret=config.cluster_shared_secret)

        async def _notification_forwarder(method: str, params: dict[str, object]) -> None:
            assert controller_client is not None
            await controller_client.notification(method=method, params=params)

        async def _request_forwarder(
            method: str,
            params: dict[str, object],
        ) -> dict[str, object] | None:
            assert controller_client is not None
            return await controller_client.request(method=method, params=params)

        await codex_app_server_client.set_handlers(
            notification_handler=_notification_forwarder,
            server_request_handler=_request_forwarder,
        )
        heartbeat_task = asyncio.create_task(_heartbeat_loop(controller_client))
    else:
        logger.warning("COCO_CONTROLLER_RPC_HOST is unset; agent will not report upstream")

    try:
        await asyncio.Event().wait()
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task
        await server.stop()
        await codex_app_server_client.stop()


def run_agent() -> None:
    asyncio.run(run_agent_async())
