from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from coco.agent_rpc import AgentRpcClient, AgentRpcServer
import coco.agent_rpc as agent_rpc


def test_run_command_sync_decodes_timeout_output(monkeypatch):
    def _timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(
            cmd=["demo"],
            timeout=1,
            output=b"partial stdout\xff",
            stderr=b"partial stderr\xfe",
        )

    monkeypatch.setattr(agent_rpc.subprocess, "run", _timeout)

    ok, stdout, stderr, error = agent_rpc._run_command_sync(["demo"], timeout_seconds=1)

    assert ok is False
    assert stdout == "partial stdout�"
    assert stderr == "partial stderr�"
    assert error == "timeout"
from coco.node_registry import NodeRegistry
from coco.node_registry import node_registry
from coco.session import session_manager


def test_agent_rpc_resolve_codex_upgrade_command_prefers_npm_for_nvm_installs(monkeypatch):
    monkeypatch.setattr(agent_rpc, "env_alias", lambda _name: "")
    monkeypatch.setattr(
        agent_rpc.shutil,
        "which",
        lambda name: "/home/pcopu/.nvm/versions/node/v24.13.1/bin/codex"
        if name == "codex"
        else (f"/usr/bin/{name}" if name in {"uv", "pipx", "npm"} else None),
    )

    command, source = agent_rpc._resolve_codex_upgrade_command()

    assert source == "npm"
    assert command == "npm install -g @openai/codex@latest"


def test_agent_rpc_resolve_codex_upgrade_command_recognizes_windows_pipx_install(monkeypatch):
    monkeypatch.setattr(agent_rpc, "env_alias", lambda _name: "")
    monkeypatch.setattr(
        agent_rpc.shutil,
        "which",
        lambda name: r"C:\Users\coco\pipx\venvs\codex\Scripts\codex.exe"
        if name == "codex"
        else (f"C:\\tools\\{name}.exe" if name in {"uv", "pipx", "npm"} else None),
    )

    command, source = agent_rpc._resolve_codex_upgrade_command()

    assert source == "pipx"
    assert command == "pipx upgrade codex"


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

    async def _fake_send_inputs_to_window(
        window_id,
        inputs,
        *,
        steer=False,
        force_new_turn=False,
        model_slug="",
        reasoning_effort="",
        service_tier="",
    ):
        captured["window_id"] = window_id
        captured["inputs"] = inputs
        captured["steer"] = steer
        captured["force_new_turn"] = force_new_turn
        captured["model_slug"] = model_slug
        captured["reasoning_effort"] = reasoning_effort
        captured["service_tier"] = service_tier
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
            service_tier="fast",
        )
        assert captured["window_id"] == "@remote"
        assert captured["model_slug"] == "gpt-5.4"
        assert captured["reasoning_effort"] == "high"
        assert captured["service_tier"] == "fast"
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


@pytest.mark.asyncio
async def test_agent_rpc_probe_workspace_write_access_round_trip(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    server = AgentRpcServer(shared_secret="rpc-secret")
    await server.start(host="127.0.0.1", port=0)
    try:
        host, port = server.bound_address()
        node_registry.note_heartbeat(
            machine_id="probe-node",
            display_name="Probe Node",
            transport="agent_rpc",
            rpc_host=host,
            rpc_port=port,
            is_local=False,
            now=100.0,
        )
        client = AgentRpcClient(shared_secret="rpc-secret")
        payload = await client.probe_workspace_write_access(
            "probe-node",
            workspace_dir=str(workspace),
        )
        assert payload["workspace_path"] == str(workspace)
        assert payload["can_write"] is True
        assert payload["write_error"] == ""
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_agent_rpc_ping_includes_runtime_summary(monkeypatch):
    server = AgentRpcServer(shared_secret="rpc-secret")
    monkeypatch.setattr(
        "coco.agent_rpc.node_registry.ensure_local_node",
        lambda now=None: SimpleNamespace(
            to_dict=lambda: {
                "machine_id": "local-node",
                "display_name": "Local Node",
                "capabilities": ["monitor", "tts"],
                "runtime": {
                    "tts": {"available": True, "default_voice": "F2", "default_speed": 1.4},
                },
            }
        ),
    )
    await server.start(host="127.0.0.1", port=0)
    try:
        host, port = server.bound_address()
        node_registry.note_heartbeat(
            machine_id="ping-node",
            display_name="Ping Node",
            transport="agent_rpc",
            rpc_host=host,
            rpc_port=port,
            is_local=False,
            now=100.0,
        )
        client = AgentRpcClient(shared_secret="rpc-secret")
        payload = await client.ping("ping-node")
        assert payload["runtime"]["tts"]["default_voice"] == "F2"
        assert payload["runtime"]["tts"]["default_speed"] == 1.4
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_agent_rpc_read_documents_rejects_invalid_base64(monkeypatch):
    client = AgentRpcClient(shared_secret="rpc-secret")
    monkeypatch.setattr(client, "_resolve_endpoint", lambda _machine_id: ("127.0.0.1", 1))

    async def _call(**_kwargs):
        return {
            "documents": [
                {"name": "corrupt.txt", "data_b64": "!!!!"},
                {"name": "valid.txt", "data_b64": "VkFMSUQ="},
            ]
        }

    monkeypatch.setattr(client._client, "call", _call)

    assert await client.read_documents(
        "remote", workspace_dir="/tmp", paths=["corrupt.txt", "valid.txt"]
    ) == [("valid.txt", b"VALID")]


@pytest.mark.asyncio
async def test_agent_rpc_read_attachments_rejects_invalid_base64(monkeypatch):
    client = AgentRpcClient(shared_secret="rpc-secret")
    monkeypatch.setattr(client, "_resolve_endpoint", lambda _machine_id: ("127.0.0.1", 1))

    async def _call(**_kwargs):
        corrupt = {"data_b64": "not base64!"}
        return {
            "documents": [{"name": "bad.txt", **corrupt}],
            "images": [{"media_type": "image/png", **corrupt}],
            "videos": [{"media_type": "video/mp4", **corrupt}],
        }

    monkeypatch.setattr(client._client, "call", _call)

    assert await client.read_attachments(
        "remote", workspace_dir="/tmp", paths=["bad.txt"]
    ) == {"documents": [], "images": [], "videos": []}


@pytest.mark.asyncio
async def test_agent_rpc_thread_goal_round_trip(monkeypatch):
    async def _fake_goal_get(*, thread_id: str):
        assert thread_id == "thread-1"
        return {"goal": {"objective": "Ship it", "status": "active"}}

    async def _fake_goal_set(*, thread_id: str, goal: str):
        assert thread_id == "thread-1"
        assert goal == "Ship it"
        return {"goal": {"objective": goal, "status": "active"}}

    async def _fake_goal_clear(*, thread_id: str):
        assert thread_id == "thread-1"
        return {"cleared": True}

    monkeypatch.setattr(
        "coco.agent_rpc.codex_app_server_client.thread_goal_get",
        _fake_goal_get,
    )
    monkeypatch.setattr(
        "coco.agent_rpc.codex_app_server_client.thread_goal_set",
        _fake_goal_set,
    )
    monkeypatch.setattr(
        "coco.agent_rpc.codex_app_server_client.thread_goal_clear",
        _fake_goal_clear,
    )

    server = AgentRpcServer(shared_secret="rpc-secret")
    await server.start(host="127.0.0.1", port=0)
    try:
        host, port = server.bound_address()
        node_registry.note_heartbeat(
            machine_id="goal-node",
            display_name="Goal Node",
            transport="agent_rpc",
            rpc_host=host,
            rpc_port=port,
            is_local=False,
            now=100.0,
        )
        client = AgentRpcClient(shared_secret="rpc-secret")
        payload = await client.thread_goal_get("goal-node", thread_id="thread-1")
        assert payload["goal"]["objective"] == "Ship it"
        payload = await client.thread_goal_set(
            "goal-node",
            thread_id="thread-1",
            goal="Ship it",
        )
        assert payload["goal"]["status"] == "active"
        payload = await client.thread_goal_clear("goal-node", thread_id="thread-1")
        assert payload["cleared"] is True
    finally:
        await server.stop()
