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
async def test_run_agent_async_does_not_eagerly_start_tts(monkeypatch, caplog):
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
    monkeypatch.setattr(
        agent_runtime,
        "codex_app_server_client",
        SimpleNamespace(stop=lambda: asyncio.sleep(0)),
    )
    monkeypatch.setattr(agent_runtime.asyncio, "Event", _FakeEvent)

    with caplog.at_level("WARNING", logger=agent_runtime.logger.name):
        with pytest.raises(asyncio.CancelledError):
            await agent_runtime.run_agent_async()

    assert "COCO_CONTROLLER_RPC_HOST is unset; agent will not report upstream" in caplog.text
    assert tts_events == ["stop"]


@pytest.mark.asyncio
async def test_agent_shutdown_cancels_reset_report_and_heartbeat_carries_epoch(
    monkeypatch,
) -> None:
    captured_handlers: dict[str, object] = {}
    heartbeat_payloads: list[dict[str, object]] = []
    notification_started = asyncio.Event()
    notification_cancelled = asyncio.Event()

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
            return None

    class _FakeControllerClient:
        async def heartbeat(self, payload: dict[str, object]) -> None:
            heartbeat_payloads.append(payload)
            await asyncio.Future()

        async def notification(self, **_kwargs) -> None:
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

    with pytest.raises(asyncio.CancelledError):
        await agent_runtime.run_agent_async()

    assert notification_cancelled.is_set()
    assert heartbeat_payloads
    assert heartbeat_payloads[0]["codex_transport_epoch"] == "agent-epoch-1"
    assert heartbeat_payloads[0]["codex_transport_epoch_started_at"] == 100.0
    assert heartbeat_payloads[0]["codex_transport_generation"] == 3
    assert heartbeat_payloads[0]["codex_transport_reset_sequence"] == 1
    assert heartbeat_payloads[0]["codex_transport_protocol_version"] == 1
