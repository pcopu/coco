from __future__ import annotations

from types import SimpleNamespace

import pytest

from coco.agent_rpc import AgentRpcClient, AgentRpcServer
from coco.node_registry import NodeRegistry
from coco.node_registry import node_registry
from coco.session import session_manager


@pytest.mark.asyncio
async def test_agent_rpc_browse_round_trip(monkeypatch, tmp_path):
    root = tmp_path / "root"
    child = root / "child"
    child.mkdir(parents=True)

    monkeypatch.setattr(
        "coco.agent_rpc.config.resolve_browse_root_for_chat",
        lambda _chat_id: root,
    )

    server = AgentRpcServer(shared_secret="rpc-secret")
    await server.start(host="127.0.0.1", port=0)
    try:
        host, port = server.bound_address()
        node_registry.note_heartbeat(
            machine_id="browse-node",
            display_name="Browse Node",
            transport="agent_rpc",
            rpc_host=host,
            rpc_port=port,
            is_local=False,
            now=100.0,
        )
        client = AgentRpcClient(shared_secret="rpc-secret")
        payload = await client.browse(
            "browse-node",
            current_path=str(root),
        )
        assert payload["root_path"] == str(root.resolve())
        assert payload["current_path"] == str(root.resolve())
        assert payload["subdirs"] == ["child"]
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_agent_rpc_send_inputs_passes_model_selection(monkeypatch):
    state = session_manager.get_window_state("@remote")
    state.cwd = ""
    state.window_name = ""
    state.approval_mode = ""
    state.codex_thread_id = ""
    state.codex_active_turn_id = ""

    captured: dict[str, object] = {}

    async def _fake_send_inputs_to_window(window_id, inputs, *, steer=False, model_slug="", reasoning_effort=""):
        captured["window_id"] = window_id
        captured["inputs"] = inputs
        captured["steer"] = steer
        captured["model_slug"] = model_slug
        captured["reasoning_effort"] = reasoning_effort
        current = session_manager.get_window_state(window_id)
        current.codex_thread_id = "thread-1"
        current.codex_active_turn_id = "turn-1"
        return True, "ok"

    monkeypatch.setattr(session_manager, "send_inputs_to_window", _fake_send_inputs_to_window)
    monkeypatch.setattr(session_manager, "_save_state", lambda: None)

    server = AgentRpcServer(shared_secret="rpc-secret")
    await server.start(host="127.0.0.1", port=0)
    try:
        host, port = server.bound_address()
        node_registry.note_heartbeat(
            machine_id="send-node",
            display_name="Send Node",
            transport="agent_rpc",
            rpc_host=host,
            rpc_port=port,
            is_local=False,
            now=100.0,
        )
        client = AgentRpcClient(shared_secret="rpc-secret")
        payload = await client.send_inputs(
            "send-node",
            window_id="@remote",
            cwd="/tmp/demo",
            window_name="demo",
            inputs=[{"type": "text", "text": "hello"}],
            steer=False,
            model_slug="gpt-5.4",
            reasoning_effort="high",
        )
        assert captured["window_id"] == "@remote"
        assert captured["model_slug"] == "gpt-5.4"
        assert captured["reasoning_effort"] == "high"
        assert payload["thread_id"] == "thread-1"
        assert payload["turn_id"] == "turn-1"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_agent_rpc_probe_machine_via_remote_monitor(monkeypatch, tmp_path):
    registry = NodeRegistry(
        state_file=tmp_path / "nodes.json",
        offline_timeout_seconds=45.0,
    )
    monkeypatch.setattr("coco.agent_rpc.node_registry", registry)

    worker = AgentRpcServer(shared_secret="rpc-secret")
    await worker.start(host="127.0.0.1", port=0)
    try:
        worker_host, worker_port = worker.bound_address()
        target_host, target_port = "100.64.0.10", 8787
        registry.note_heartbeat(
            machine_id="target-node",
            display_name="Target Node",
            transport="agent_rpc",
            rpc_host=target_host,
            rpc_port=target_port,
            is_local=False,
            now=100.0,
        )
        registry.note_heartbeat(
            machine_id="worker-node",
            display_name="Worker Node",
            transport="agent_rpc",
            rpc_host=worker_host,
            rpc_port=worker_port,
            is_local=False,
            now=100.0,
        )

        async def _fake_probe_call(*, host: str, port: int, method: str, params: dict[str, object]):
            assert host == target_host
            assert port == target_port
            assert method == "agent/ping"
            assert params == {}
            return {
                "machine_id": "target-node",
                "display_name": "Target Node",
            }

        monkeypatch.setattr(worker._probe_client, "call", _fake_probe_call)

        client = AgentRpcClient(shared_secret="rpc-secret")
        payload = await client.probe_machine(
            "target-node",
            via_machine_id="worker-node",
        )
        assert payload["machine_id"] == "target-node"
        assert payload["display_name"] == "Target Node"
    finally:
        await worker.stop()
