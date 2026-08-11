"""Agent-only runtime bootstrap."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from .agent_rpc import AgentRpcServer
from .codex_app_server import (
    INTERNAL_TRANSPORT_CONTEXT_KEY,
    codex_app_server_client,
)
from .config import config
from .controller_rpc import (
    CODEX_TRANSPORT_PROTOCOL_VERSION,
    CODEX_TRANSPORT_PROTOCOL_VERSION_KEY,
    ControllerRpcClient,
)
from .node_registry import node_registry
from .session import session_manager
from .tts_runtime import stop_tts_server


logger = logging.getLogger(__name__)

AGENT_SHUTDOWN_RESET_REPORT_TIMEOUT_SECONDS = 5.0


def _forwarded_transport_context(
    params: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    forwarded_params = dict(params)
    raw_context = forwarded_params.pop(INTERNAL_TRANSPORT_CONTEXT_KEY, None)
    context = (
        dict(raw_context)
        if isinstance(raw_context, dict)
        else codex_app_server_client.transport_state_snapshot()
    )
    context["machine_id"] = config.machine_id
    return forwarded_params, context


async def _heartbeat_loop(controller_client: ControllerRpcClient) -> None:
    while True:
        try:
            node = node_registry.ensure_local_node(transport="agent_rpc")
            payload = node.to_dict()
            transport_state = codex_app_server_client.transport_state_snapshot()
            payload.update(
                {
                    CODEX_TRANSPORT_PROTOCOL_VERSION_KEY: (
                        CODEX_TRANSPORT_PROTOCOL_VERSION
                    ),
                    "codex_transport_epoch": transport_state["epoch"],
                    "codex_transport_epoch_started_at": transport_state[
                        "epoch_started_at"
                    ],
                    "codex_transport_generation": transport_state["generation"],
                    "codex_transport_reset_sequence": transport_state[
                        "reset_sequence"
                    ],
                    "codex_last_reset_generation": transport_state[
                        "last_reset_generation"
                    ],
                    "codex_last_reset_reason": transport_state[
                        "last_reset_reason"
                    ],
                    "codex_last_reset_at": transport_state["last_reset_at"],
                }
            )
            await controller_client.heartbeat(payload)
        except Exception as exc:
            logger.warning("Agent heartbeat failed: %s", exc)
        await asyncio.sleep(max(5.0, float(config.node_heartbeat_interval)))


async def _drain_shutdown_reset_reports(
    *,
    earlier_reset_reports: set[asyncio.Task[None]],
    reset_report_tasks: set[asyncio.Task[None]],
    final_reset_report_scheduled: bool | None = None,
) -> None:
    """Prefer the final reset report, or preserve the sole earlier report."""
    shutdown_reset_reports = reset_report_tasks - earlier_reset_reports
    has_final_report = (
        bool(shutdown_reset_reports)
        if final_reset_report_scheduled is None
        else final_reset_report_scheduled
    )
    if has_final_report:
        for task in earlier_reset_reports:
            task.cancel()
        if earlier_reset_reports:
            await asyncio.gather(*earlier_reset_reports, return_exceptions=True)
        reports_to_drain = shutdown_reset_reports
    else:
        reports_to_drain = earlier_reset_reports

    if not reports_to_drain:
        return
    _done, still_pending = await asyncio.wait(
        reports_to_drain,
        timeout=AGENT_SHUTDOWN_RESET_REPORT_TIMEOUT_SECONDS,
    )
    for task in still_pending:
        task.cancel()
    await asyncio.gather(*reports_to_drain, return_exceptions=True)


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
    reset_report_tasks: set[asyncio.Task[None]] = set()
    reset_report_sequence = 0

    async def _report_transport_reset(
        *,
        machine_id: str,
        reason: str,
        generation: int,
        reset_sequence: int,
        transport_epoch: str,
        transport_epoch_started_at: float,
    ) -> None:
        if controller_client is None:
            return
        try:
            await controller_client.notification(
                method="coco/transportReset",
                params={
                    "machineId": machine_id,
                    "reason": reason,
                    "generation": generation,
                    "resetSequence": reset_sequence,
                    "transportEpoch": transport_epoch,
                    "transportEpochStartedAt": transport_epoch_started_at,
                },
                transport={
                    "machine_id": machine_id,
                    **codex_app_server_client.transport_state_snapshot(),
                },
            )
        except Exception as exc:
            logger.warning(
                "Failed reporting app-server transport reset to controller; "
                "the next heartbeat will reconcile it: %s",
                exc,
            )

    async def _transport_reset_handler(reason: str, generation: int) -> None:
        nonlocal reset_report_sequence
        local_machine_id, _local_machine_name = (
            session_manager._local_machine_identity()
        )
        cleared_turns = session_manager.clear_window_codex_turns_for_machine(
            local_machine_id
        )
        logger.warning(
            "Codex app-server transport reset "
            "(reason=%s generation=%d cleared_turns=%d)",
            reason,
            generation,
            cleared_turns,
        )
        if controller_client is not None:
            reset_report_sequence += 1
            transport_state = codex_app_server_client.transport_state_snapshot()
            reset_sequence = int(transport_state["reset_sequence"])
            task = asyncio.create_task(
                _report_transport_reset(
                    machine_id=local_machine_id,
                    reason=reason,
                    generation=generation,
                    reset_sequence=reset_sequence,
                    transport_epoch=str(transport_state["epoch"]),
                    transport_epoch_started_at=float(
                        transport_state["epoch_started_at"]
                    ),
                )
            )
            reset_report_tasks.add(task)
            task.add_done_callback(reset_report_tasks.discard)

    if config.controller_rpc_host:
        controller_client = ControllerRpcClient(shared_secret=config.cluster_shared_secret)

        async def _notification_forwarder(method: str, params: dict[str, object]) -> None:
            assert controller_client is not None
            forwarded_params, transport = _forwarded_transport_context(params)
            await controller_client.notification(
                method=method,
                params=forwarded_params,
                transport=transport,
            )

        async def _request_forwarder(
            method: str,
            params: dict[str, object],
        ) -> dict[str, object] | None:
            assert controller_client is not None
            forwarded_params, transport = _forwarded_transport_context(params)
            return await controller_client.request(
                method=method,
                params=forwarded_params,
                transport=transport,
            )

        await codex_app_server_client.set_handlers(
            notification_handler=_notification_forwarder,
            server_request_handler=_request_forwarder,
            transport_reset_handler=_transport_reset_handler,
        )
        heartbeat_task = asyncio.create_task(_heartbeat_loop(controller_client))
    else:
        set_handlers = getattr(codex_app_server_client, "set_handlers", None)
        if set_handlers is not None:
            await set_handlers(transport_reset_handler=_transport_reset_handler)
        logger.warning("COCO_CONTROLLER_RPC_HOST is unset; agent will not report upstream")

    try:
        await asyncio.Event().wait()
    finally:
        earlier_reset_reports = set(reset_report_tasks)
        reset_report_sequence_before_stop = reset_report_sequence
        try:
            await server.stop()
        finally:
            try:
                await codex_app_server_client.stop()
            finally:
                try:
                    await _drain_shutdown_reset_reports(
                        earlier_reset_reports=earlier_reset_reports,
                        reset_report_tasks=set(reset_report_tasks),
                        final_reset_report_scheduled=(
                            reset_report_sequence
                            > reset_report_sequence_before_stop
                        ),
                    )
                finally:
                    try:
                        if heartbeat_task is not None:
                            heartbeat_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await heartbeat_task
                    finally:
                        await stop_tts_server()


def run_agent() -> None:
    asyncio.run(run_agent_async())
