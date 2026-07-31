"""Tests for the agent-only runtime bootstrap."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import coco.agent_runtime as agent_runtime
from coco.codex_app_server import INTERNAL_TRANSPORT_CONTEXT_KEY


def test_forwarded_transport_context_uses_captured_notification_generation(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        agent_runtime,
        "config",
        SimpleNamespace(machine_id="remote-node"),
    )

    params, transport = agent_runtime._forwarded_transport_context(
        {
            "threadId": "thread-1",
            INTERNAL_TRANSPORT_CONTEXT_KEY: {
                "epoch": "agent-epoch-1",
                "epoch_started_at": 100.0,
                "generation": 3,
                "reset_sequence": 0,
                "last_reset_generation": 0,
                "last_reset_reason": "",
                "last_reset_at": 0.0,
            },
        }
    )

    assert params == {"threadId": "thread-1"}
    assert transport["machine_id"] == "remote-node"
    assert transport["generation"] == 3


@pytest.mark.asyncio
async def test_agent_shutdown_stops_tts_when_codex_stop_fails(monkeypatch, caplog):
    tts_events: list[str] = []

    class _FakeServer:
        async def start(self, *, host: str, port: int) -> None:
            self.host = host
            self.port = port

        def bound_address(self) -> tuple[str, int]:
            return ("127.0.0.1", 8787)

        async def stop(self) -> None:
            return None

    class _FakeEvent:
        async def wait(self) -> None:
            raise asyncio.CancelledError()

    monkeypatch.setattr(
        agent_runtime,
        "config",
        SimpleNamespace(
            node_heartbeat_interval=15.0,
            machine_name="Test Node",
            machine_id="test-node",
            tailnet_name="",
            sessions_path="/tmp/sessions",
            assistant_command="codex",
            cluster_shared_secret="secret",
            rpc_listen_host="127.0.0.1",
            rpc_port=8787,
            controller_rpc_host="",
        ),
    )
    monkeypatch.setattr(
        agent_runtime,
        "node_registry",
        SimpleNamespace(ensure_local_node=lambda **_kwargs: None),
    )
    monkeypatch.setattr(
        agent_runtime,
        "ensure_tts_server_started",
        lambda: asyncio.sleep(0, result=tts_events.append("start")),
        raising=False,
    )
    monkeypatch.setattr(
        agent_runtime,
        "stop_tts_server",
        lambda: asyncio.sleep(0, result=tts_events.append("stop")),
    )
    monkeypatch.setattr(agent_runtime, "AgentRpcServer", lambda shared_secret: _FakeServer())
    async def _failing_codex_stop() -> None:
        raise RuntimeError("codex stop failed")

    monkeypatch.setattr(
        agent_runtime,
        "codex_app_server_client",
        SimpleNamespace(stop=_failing_codex_stop),
    )
    monkeypatch.setattr(agent_runtime.asyncio, "Event", _FakeEvent)

    with caplog.at_level("WARNING", logger=agent_runtime.logger.name):
        with pytest.raises(RuntimeError, match="codex stop failed"):
            await agent_runtime.run_agent_async()

    assert "COCO_CONTROLLER_RPC_HOST is unset; agent will not report upstream" in caplog.text
    assert tts_events == ["stop"]


@pytest.mark.asyncio
async def test_shutdown_drain_waits_for_only_existing_reset_report() -> None:
    release_report = asyncio.Event()
    report_completed = asyncio.Event()

    async def _report() -> None:
        await release_report.wait()
        report_completed.set()

    report_task = asyncio.create_task(_report())
    drain = getattr(agent_runtime, "_drain_shutdown_reset_reports", None)
    try:
        assert callable(drain)
        drain_task = asyncio.create_task(
            drain(
                earlier_reset_reports={report_task},
                reset_report_tasks={report_task},
            )
        )
        await asyncio.sleep(0)
        assert not drain_task.done()
        assert not report_task.cancelled()
        release_report.set()
        await drain_task
        assert report_completed.is_set()
    finally:
        release_report.set()
        if not report_task.done():
            report_task.cancel()
        await asyncio.gather(report_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_agent_shutdown_reports_explicit_stop_before_teardown(
    monkeypatch,
) -> None:
    captured_handlers: dict[str, object] = {}
    heartbeat_payloads: list[dict[str, object]] = []
    notification_reasons: list[str] = []
    notification_started = asyncio.Event()
    notification_cancelled = asyncio.Event()
    explicit_stop_started = asyncio.Event()
    allow_explicit_stop_report = asyncio.Event()
    explicit_stop_reported = asyncio.Event()

    class _FakeServer:
        async def start(self, *, host: str, port: int) -> None:
            _ = host, port

        def bound_address(self) -> tuple[str, int]:
            return ("127.0.0.1", 8787)

        async def stop(self) -> None:
            return None

    class _FakeNode:
        def to_dict(self) -> dict[str, object]:
            return {"machine_id": "test-node"}

    class _FakeCodexClient:
        async def set_handlers(self, **handlers) -> None:
            captured_handlers.update(handlers)

        def transport_state_snapshot(self) -> dict[str, object]:
            return {
                "epoch": "agent-epoch-1",
                "epoch_started_at": 100.0,
                "generation": 3,
                "reset_sequence": 1,
                "last_reset_generation": 2,
                "last_reset_reason": "request_timeout:turn/start",
                "last_reset_at": 101.0,
            }

        async def stop(self) -> None:
            handler = captured_handlers["transport_reset_handler"]
            await handler("explicit_stop", 3)

    class _FakeControllerClient:
        async def heartbeat(self, payload: dict[str, object]) -> None:
            heartbeat_payloads.append(payload)
            await asyncio.Future()

        async def notification(self, **kwargs) -> None:
            params = kwargs["params"]
            reason = str(params["reason"])
            notification_reasons.append(reason)
            if reason == "explicit_stop":
                explicit_stop_started.set()
                await allow_explicit_stop_report.wait()
                explicit_stop_reported.set()
                return
            notification_started.set()
            try:
                await asyncio.Future()
            finally:
                notification_cancelled.set()

        async def request(self, **_kwargs):
            return None

    class _FakeMainEvent:
        async def wait(self) -> None:
            while "transport_reset_handler" not in captured_handlers:
                await asyncio.sleep(0)
            handler = captured_handlers["transport_reset_handler"]
            await handler("request_timeout:turn/start", 2)
            await notification_started.wait()
            raise asyncio.CancelledError()

    monkeypatch.setattr(
        agent_runtime,
        "config",
        SimpleNamespace(
            node_heartbeat_interval=15.0,
            machine_name="Test Node",
            machine_id="test-node",
            tailnet_name="",
            sessions_path="/tmp/sessions",
            assistant_command="codex",
            cluster_shared_secret="secret",
            rpc_listen_host="127.0.0.1",
            rpc_port=8787,
            controller_rpc_host="127.0.0.1",
        ),
    )
    monkeypatch.setattr(
        agent_runtime,
        "node_registry",
        SimpleNamespace(ensure_local_node=lambda **_kwargs: _FakeNode()),
    )
    monkeypatch.setattr(
        agent_runtime.session_manager,
        "_local_machine_identity",
        lambda: ("test-node", "Test Node"),
    )
    monkeypatch.setattr(
        agent_runtime.session_manager,
        "clear_window_codex_turns_for_machine",
        lambda _machine_id: 1,
    )
    monkeypatch.setattr(
        agent_runtime,
        "stop_tts_server",
        lambda: asyncio.sleep(0),
    )
    monkeypatch.setattr(
        agent_runtime,
        "AgentRpcServer",
        lambda shared_secret: _FakeServer(),
    )
    monkeypatch.setattr(
        agent_runtime,
        "ControllerRpcClient",
        lambda shared_secret: _FakeControllerClient(),
    )
    monkeypatch.setattr(
        agent_runtime,
        "codex_app_server_client",
        _FakeCodexClient(),
    )
    monkeypatch.setattr(agent_runtime.asyncio, "Event", _FakeMainEvent)

    run_task = asyncio.create_task(agent_runtime.run_agent_async())
    await asyncio.wait_for(explicit_stop_started.wait(), timeout=1.0)
    await asyncio.sleep(0)
    shutdown_waited_for_report = not run_task.done()
    allow_explicit_stop_report.set()
    with pytest.raises(asyncio.CancelledError):
        await run_task

    assert notification_cancelled.is_set()
    assert shutdown_waited_for_report
    assert explicit_stop_reported.is_set()
    assert notification_reasons == ["request_timeout:turn/start", "explicit_stop"]
    assert heartbeat_payloads
    assert heartbeat_payloads[0]["codex_transport_epoch"] == "agent-epoch-1"
    assert heartbeat_payloads[0]["codex_transport_epoch_started_at"] == 100.0
    assert heartbeat_payloads[0]["codex_transport_generation"] == 3
    assert heartbeat_payloads[0]["codex_transport_reset_sequence"] == 1
    assert heartbeat_payloads[0]["codex_transport_protocol_version"] == 1
