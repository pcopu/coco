"""Tests for the agent-only runtime bootstrap."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import coco.agent_runtime as agent_runtime


@pytest.mark.asyncio
async def test_run_agent_async_logs_coco_first_controller_host_warning(monkeypatch, caplog):
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
