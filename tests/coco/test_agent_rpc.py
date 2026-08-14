from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from coco.agent_rpc import AgentRpcClient, AgentRpcServer
import coco.agent_rpc as agent_rpc
import coco.codex_trust as codex_trust
from coco.cluster_rpc import ClusterRpcError
from coco.node_registry import NodeRegistry
from coco.node_registry import node_registry
from coco.session import session_manager


@pytest.fixture(autouse=True)
def _avoid_writing_real_codex_trust(monkeypatch):
    monkeypatch.setattr(
        agent_rpc,
        "ensure_codex_project_trust",
        lambda _path: (True, ""),
    )


@pytest.mark.asyncio
async def test_agent_ensure_control_workspace_round_trip_is_idempotent(
    monkeypatch,
    tmp_path,
):
    trusted: list[object] = []
    monkeypatch.setattr(
        agent_rpc,
        "ensure_codex_project_trust",
        lambda path: (trusted.append(path) is None, ""),
    )
    monkeypatch.setattr(agent_rpc.config, "config_dir", tmp_path)
    monkeypatch.setattr(agent_rpc.config, "machine_id", "control-node")

    server = AgentRpcServer(shared_secret="rpc-secret")
    await server.start(host="127.0.0.1", port=0)
    try:
        host, port = server.bound_address()
        node_registry.note_heartbeat(
            machine_id="control-node",
            display_name="Control Node",
            transport="agent_rpc",
            rpc_host=host,
            rpc_port=port,
            is_local=False,
            now=100.0,
        )
        client = AgentRpcClient(shared_secret="rpc-secret")

        first = await client.ensure_control_workspace("control-node", -100123)
        second = await client.ensure_control_workspace("control-node", -100123)

        expected = tmp_path / "_coco" / "chat-100123" / "control"
        assert first == second
        assert first["machine_id"] == "control-node"
        assert first["chat_id"] == -100123
        assert first["workspace_path"] == str(expected)
        assert first["can_write"] is True
        assert first["write_error"] == ""
        assert expected.is_dir()
        assert not list(expected.glob(".coco-write-probe-*"))
        assert trusted == [expected, expected]
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_agent_ensure_control_workspace_allows_symlinked_config_root(
    monkeypatch,
    tmp_path,
):
    real_config_root = tmp_path / "real-config"
    real_config_root.mkdir()
    config_link = tmp_path / "config-link"
    config_link.symlink_to(real_config_root, target_is_directory=True)
    monkeypatch.setattr(agent_rpc.config, "config_dir", config_link)

    result = await AgentRpcServer(shared_secret="rpc-secret")._ensure_control_workspace(
        {"chat_id": -100123}
    )

    expected = real_config_root / "_coco" / "chat-100123" / "control"
    assert result["workspace_path"] == str(expected)
    assert expected.is_dir()


@pytest.mark.asyncio
async def test_agent_ensure_control_workspace_trust_uses_custom_codex_home(
    monkeypatch, tmp_path
):
    codex_home = tmp_path / "codex-home"
    monkeypatch.setattr(
        agent_rpc,
        "ensure_codex_project_trust",
        codex_trust.ensure_codex_project_trust,
    )
    monkeypatch.setattr(agent_rpc.config, "config_dir", tmp_path / "agent-config")
    monkeypatch.setattr(agent_rpc.config, "machine_id", "control-node")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    result = await AgentRpcServer(shared_secret="rpc-secret")._ensure_control_workspace(
        {"chat_id": -100123}
    )

    workspace = Path(result["workspace_path"])
    config_text = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert f'[projects."{workspace}"]' in config_text
    assert not (tmp_path / "agent-config" / ".codex" / "config.toml").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_id", [0, 1, True, None, "-100"])
async def test_agent_ensure_control_workspace_rejects_non_group_chat_id(
    monkeypatch,
    tmp_path,
    chat_id,
):
    monkeypatch.setattr(agent_rpc.config, "config_dir", tmp_path)
    with pytest.raises(ClusterRpcError, match="negative nonzero group chat_id"):
        await AgentRpcServer(shared_secret="rpc-secret")._ensure_control_workspace(
            {"chat_id": chat_id}
        )


@pytest.mark.asyncio
async def test_agent_ensure_control_workspace_rejects_symlink_escape(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(agent_rpc.config, "config_dir", tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "_coco").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ClusterRpcError, match="symlink"):
        await AgentRpcServer(shared_secret="rpc-secret")._ensure_control_workspace(
            {"chat_id": -100123}
        )


@pytest.mark.asyncio
async def test_agent_ensure_control_workspace_rejects_symlink_chat_directory(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(agent_rpc.config, "config_dir", tmp_path)
    coco_root = tmp_path / "_coco"
    coco_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (coco_root / "chat-100123").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ClusterRpcError, match="symlink"):
        await AgentRpcServer(shared_secret="rpc-secret")._ensure_control_workspace(
            {"chat_id": -100123}
        )
    assert not (outside / "control").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("component", ["_coco", "chat-100123", "control"])
async def test_agent_ensure_control_workspace_rejects_non_directory_component(
    monkeypatch,
    tmp_path,
    component,
):
    monkeypatch.setattr(agent_rpc.config, "config_dir", tmp_path)
    target = tmp_path / "_coco" / "chat-100123" / "control"
    if component == "_coco":
        target = tmp_path / component
    elif component == "chat-100123":
        (tmp_path / "_coco").mkdir()
        target = tmp_path / "_coco" / component
    else:
        (tmp_path / "_coco" / "chat-100123").mkdir(parents=True)
    target.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ClusterRpcError, match="not a directory"):
        await AgentRpcServer(shared_secret="rpc-secret")._ensure_control_workspace(
            {"chat_id": -100123}
        )


@pytest.mark.asyncio
async def test_agent_turn_interrupt_round_trip(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_rpc.config, "config_dir", tmp_path)
    monkeypatch.setattr(agent_rpc.config, "machine_id", "interrupt-node")
    calls: list[tuple[str, str]] = []

    async def _interrupt(*, thread_id: str, turn_id: str):
        calls.append((thread_id, turn_id))

    monkeypatch.setattr(
        agent_rpc.codex_app_server_client,
        "turn_interrupt",
        _interrupt,
    )
    monkeypatch.setattr(
        agent_rpc.codex_app_server_client,
        "transport_state_snapshot",
        lambda: {"epoch": "interrupt-epoch", "epoch_started_at": 100.0},
    )

    server = AgentRpcServer(shared_secret="rpc-secret")
    await server.start(host="127.0.0.1", port=0)
    try:
        host, port = server.bound_address()
        node_registry.note_heartbeat(
            machine_id="interrupt-node",
            display_name="Interrupt Node",
            transport="agent_rpc",
            rpc_host=host,
            rpc_port=port,
            is_local=False,
            now=100.0,
        )
        client = AgentRpcClient(shared_secret="rpc-secret")
        client.set_codex_mutation_dispatch_gate(
            lambda _machine_id: asyncio.sleep(
                0,
                result=("interrupt-epoch", 100.0),
            )
        )
        payload = await client.turn_interrupt(
            "interrupt-node",
            thread_id="thread-1",
            turn_id="turn-1",
        )

        assert calls == [("thread-1", "turn-1")]
        assert payload == {
            "ok": True,
            "machine_id": "interrupt-node",
            "thread_id": "thread-1",
            "turn_id": "turn-1",
        }
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_agent_turn_interrupt_requires_transport_fence(monkeypatch):
    server = AgentRpcServer(shared_secret="rpc-secret")
    monkeypatch.setattr(
        agent_rpc.codex_app_server_client,
        "turn_interrupt",
        lambda **_kwargs: pytest.fail("unfenced interrupt must not dispatch"),
    )

    with pytest.raises(
        agent_rpc.RemoteCodexMutationDeferredError,
        match="transport fence is required",
    ):
        await server._turn_interrupt(
            {"thread_id": "thread-1", "turn_id": "turn-1"}
        )


@pytest.mark.asyncio
async def test_agent_turn_interrupt_marks_lost_response_as_uncertain(monkeypatch):
    client = AgentRpcClient(shared_secret="rpc-secret")
    client.set_codex_mutation_dispatch_gate(
        lambda _machine_id: asyncio.sleep(
            0,
            result=("interrupt-epoch", 100.0),
        )
    )
    monkeypatch.setattr(
        client,
        "_resolve_endpoint",
        lambda _machine_id: ("127.0.0.1", 8787),
    )

    async def _call(**_kwargs):
        raise ClusterRpcError("empty_response", request_dispatched=True)

    monkeypatch.setattr(client._client, "call", _call)

    with pytest.raises(ClusterRpcError) as raised:
        await client.turn_interrupt(
            "remote-node",
            thread_id="thread-1",
            turn_id="turn-1",
        )
    assert type(raised.value).__name__ == "RemoteCodexMutationUncertainError"
    assert raised.value.request_dispatched is True


@pytest.mark.asyncio
async def test_agent_turn_interrupt_keeps_definite_predispatch_failure(monkeypatch):
    client = AgentRpcClient(shared_secret="rpc-secret")
    client.set_codex_mutation_dispatch_gate(
        lambda _machine_id: asyncio.sleep(
            0,
            result=("interrupt-epoch", 100.0),
        )
    )
    monkeypatch.setattr(
        client,
        "_resolve_endpoint",
        lambda _machine_id: ("127.0.0.1", 8787),
    )

    async def _call(**_kwargs):
        raise ClusterRpcError("connect_failed", request_dispatched=False)

    monkeypatch.setattr(client._client, "call", _call)

    with pytest.raises(ClusterRpcError) as raised:
        await client.turn_interrupt(
            "remote-node",
            thread_id="thread-1",
            turn_id="turn-1",
        )
    assert type(raised.value).__name__ != "RemoteCodexMutationUncertainError"
    assert raised.value.request_dispatched is False


@pytest.mark.asyncio
async def test_agent_ensure_control_workspace_client_validates_identity_and_writable_path(
    monkeypatch,
):
    client = AgentRpcClient(shared_secret="rpc-secret")
    monkeypatch.setattr(
        client,
        "_resolve_endpoint",
        lambda _machine_id: ("127.0.0.1", 8787),
    )

    async def _call(**_kwargs):
        return {
            "machine_id": "other-node",
            "chat_id": -100123,
            "workspace_path": "/tmp/control",
            "can_write": True,
            "write_error": "",
        }

    monkeypatch.setattr(client._client, "call", _call)
    with pytest.raises(ClusterRpcError, match="machine identity"):
        await client.ensure_control_workspace("control-node", -100123)

    async def _missing_path(**_kwargs):
        return {
            "machine_id": "control-node",
            "chat_id": -100123,
            "workspace_path": "",
            "can_write": True,
            "write_error": "",
        }

    monkeypatch.setattr(client._client, "call", _missing_path)
    with pytest.raises(ClusterRpcError, match="workspace path"):
        await client.ensure_control_workspace("control-node", -100123)


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


def test_agent_rpc_coco_update_ignores_untracked_files(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    commands: list[list[str]] = []

    monkeypatch.setattr(agent_rpc, "_resolve_repo_root", lambda: repo)
    monkeypatch.setattr(agent_rpc, "env_alias", lambda _name: "")
    monkeypatch.setattr(agent_rpc.shutil, "which", lambda _name: None)

    def _run_command(argv, **_kwargs):
        commands.append(argv)
        if argv[:3] == ["git", "status", "--porcelain"]:
            if "--untracked-files=no" in argv:
                return True, "", "", ""
            return True, "?? scratch.tmp\n", "", ""
        if argv[:3] == ["git", "pull", "--ff-only"]:
            return True, "Already up to date.\n", "", ""
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(agent_rpc, "_run_command_sync", _run_command)

    ok, message = agent_rpc._run_remote_coco_update_sync()

    assert ok is True
    assert message == "CoCo update completed."
    assert ["git", "pull", "--ff-only"] in commands


def test_agent_rpc_coco_update_reinstalls_uv_tool_without_git_checkout(
    monkeypatch, tmp_path
):
    runtime_root = tmp_path / "site-packages"
    runtime_root.mkdir()
    commands: list[list[str]] = []

    monkeypatch.setattr(agent_rpc, "_resolve_repo_root", lambda: runtime_root)
    monkeypatch.setattr(agent_rpc, "env_alias", lambda _name: "")
    monkeypatch.setattr(
        agent_rpc,
        "_resolve_coco_tool_update_argv",
        lambda: ["/home/coco/.local/bin/uv", "tool", "install", "--force", "git+https://github.com/pcopu/coco.git"],
        raising=False,
    )

    def _run_command(argv, **_kwargs):
        commands.append(argv)
        return True, "Installed coco", "", ""

    monkeypatch.setattr(agent_rpc, "_run_command_sync", _run_command)

    ok, message = agent_rpc._run_remote_coco_update_sync()

    assert ok is True
    assert message == "CoCo package updated."
    assert commands == [[
        "/home/coco/.local/bin/uv",
        "tool",
        "install",
        "--force",
        "git+https://github.com/pcopu/coco.git",
    ]]


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
async def test_agent_resume_response_preserves_metadata_only_active_turn(
    monkeypatch,
    tmp_path,
):
    window_id = "@remote-metadata-resume"
    thread_id = "thread-remote-metadata"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions_root = tmp_path / "sessions"
    session_dir = sessions_root / "2026" / "07"
    session_dir.mkdir(parents=True)
    rollout = session_dir / f"rollout-2026-07-23T12-00-00-{thread_id}.jsonl"
    rollout.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "id": thread_id,
                    "cwd": str(workspace.resolve()),
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(agent_rpc.config, "sessions_path", sessions_root)
    monkeypatch.setattr(
        agent_rpc.config,
        "codex_max_resume_bytes",
        rollout.stat().st_size + 1,
        raising=False,
    )
    monkeypatch.setattr(session_manager, "_save_state", lambda: None)

    previous_state = session_manager.window_states.pop(window_id, None)
    state = session_manager.get_window_state(window_id)
    state.cwd = str(workspace)
    state.codex_thread_id = thread_id
    state.codex_active_turn_id = "turn-remote-live"
    state.codex_transport_epoch = "agent-epoch-live"
    state.codex_transport_epoch_started_at = 100.0
    state.codex_transport_generation = 7
    transport_state = {
        "epoch": "agent-epoch-live",
        "epoch_started_at": 100.0,
        "generation": 7,
        "reset_sequence": 0,
        "last_reset_generation": 0,
        "last_reset_reason": "",
        "last_reset_at": 0.0,
    }

    async def _thread_resume(*, thread_id: str):
        return {"thread": {"id": thread_id}}

    monkeypatch.setattr(
        agent_rpc.codex_app_server_client,
        "thread_resume",
        _thread_resume,
    )
    monkeypatch.setattr(
        agent_rpc.codex_app_server_client,
        "get_active_turn_id",
        lambda _thread_id: None,
    )
    monkeypatch.setattr(
        agent_rpc.codex_app_server_client,
        "transport_state_snapshot",
        lambda: dict(transport_state),
    )

    try:
        payload = await AgentRpcServer(
            shared_secret="rpc-secret"
        )._resume_thread(
            {
                "window_id": window_id,
                "cwd": str(workspace),
                "thread_id": thread_id,
            }
        )
    finally:
        if previous_state is None:
            session_manager.window_states.pop(window_id, None)
        else:
            session_manager.window_states[window_id] = previous_state

    assert payload["thread_id"] == thread_id
    assert payload["turn_id"] == "turn-remote-live"


@pytest.mark.asyncio
async def test_agent_resume_latest_marks_empty_result_as_lifecycle_noop(
    monkeypatch,
):
    window_id = "@remote-empty-resume"
    transport_state = {
        "epoch": "agent-epoch-idle",
        "epoch_started_at": 100.0,
        "generation": 0,
        "reset_sequence": 0,
        "last_reset_generation": 0,
        "last_reset_reason": "",
        "last_reset_at": 0.0,
    }

    async def _resume_latest(*, window_id: str, cwd: str) -> str:
        _ = window_id, cwd
        session_manager.mark_window_pending_session_start_reason(
            window_id,
            "oversized_rollover",
        )
        return ""

    monkeypatch.setattr(
        session_manager,
        "resume_latest_codex_session_for_window",
        _resume_latest,
    )
    monkeypatch.setattr(
        agent_rpc.codex_app_server_client,
        "transport_state_snapshot",
        lambda: dict(transport_state),
    )
    monkeypatch.setattr(session_manager, "_save_state", lambda: None)

    previous_state = session_manager.window_states.pop(window_id, None)
    try:
        payload = await AgentRpcServer(
            shared_secret="rpc-secret"
        )._resume_latest(
            {
                "window_id": window_id,
                "cwd": "/tmp/demo",
                "window_name": "demo",
            }
        )
    finally:
        session_manager.consume_window_pending_session_start_reason(window_id)
        if previous_state is None:
            session_manager.window_states.pop(window_id, None)
        else:
            session_manager.window_states[window_id] = previous_state

    assert payload["thread_id"] == ""
    assert payload["turn_id"] == ""
    assert payload["session_start_reason"] == "oversized_rollover"
    assert payload["transport_generation"] == 0
    assert payload["transport_lifecycle_noop"] is True


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
        remote_thread_id=None,
        remote_cwd="",
        remote_window_name="",
        remote_approval_mode="",
        result_snapshot=None,
    ):
        captured["window_id"] = window_id
        captured["inputs"] = inputs
        captured["steer"] = steer
        captured["force_new_turn"] = force_new_turn
        captured["model_slug"] = model_slug
        captured["reasoning_effort"] = reasoning_effort
        captured["service_tier"] = service_tier
        captured["remote_thread_id"] = remote_thread_id
        captured["remote_cwd"] = remote_cwd
        captured["remote_window_name"] = remote_window_name
        captured["remote_approval_mode"] = remote_approval_mode
        current = session_manager.get_window_state(window_id)
        current.codex_thread_id = "thread-1"
        current.codex_active_turn_id = "turn-1"
        if result_snapshot is not None:
            result_snapshot.update(thread_id="thread-1", turn_id="turn-1")
        return True, "ok"

    async def _ensure_started() -> None:
        return None

    monkeypatch.setattr(session_manager, "send_inputs_to_window", _fake_send_inputs_to_window)
    monkeypatch.setattr(
        agent_rpc.codex_app_server_client,
        "ensure_started",
        _ensure_started,
    )
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
        assert captured["remote_thread_id"] == ""
        assert captured["remote_cwd"] == "/tmp/demo"
        assert captured["remote_window_name"] == "demo"
        assert payload["thread_id"] == "thread-1"
        assert payload["turn_id"] == "turn-1"
        assert isinstance(payload["transport_epoch"], str)
        assert payload["transport_epoch"]
        assert payload["transport_epoch_started_at"] > 0
        assert payload["transport_generation"] >= 0
        assert payload["transport_reset_sequence"] >= 0
        assert "transport_reset_occurred" in payload
        assert "transport_last_reset_generation" in payload
        assert "transport_last_reset_reason" in payload
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_lifecycle_validation_uses_target_machine_before_window_is_bound(
    monkeypatch,
):
    client = AgentRpcClient(shared_secret="rpc-secret")
    monkeypatch.setattr(
        client,
        "_resolve_endpoint",
        lambda machine_id: (
            ("127.0.0.1", 8787)
            if machine_id == "remote-node"
            else ("", 0)
        ),
    )

    async def _call(**_kwargs):
        return {"thread_id": "thread-1", "turn_id": ""}

    observed: dict[str, object] = {}

    async def _accept_remote_result(*, window_id, result):
        observed["window_id"] = window_id
        observed["result"] = result
        return result.get("_coco_remote_machine_id") == "remote-node"

    monkeypatch.setattr(client._client, "call", _call)
    monkeypatch.setattr(
        session_manager,
        "_accept_remote_transport_result",
        _accept_remote_result,
    )

    payload = await client.ensure_thread(
        "remote-node",
        window_id="@new-unbound-window",
        cwd="/tmp/demo",
    )

    assert payload == {"thread_id": "thread-1", "turn_id": ""}
    assert observed == {
        "window_id": "@new-unbound-window",
        "result": {
            "thread_id": "thread-1",
            "turn_id": "",
            "_coco_remote_machine_id": "remote-node",
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    [
        "ensure_thread",
        "resume_latest",
        "resume_thread",
        "fork_thread",
        "rollback_thread",
        "send_inputs",
        "thread_goal_set",
        "thread_goal_clear",
        "turn_interrupt",
    ],
)
async def test_agent_rpc_blocks_mutation_before_unconfirmed_epoch_dispatch(
    monkeypatch,
    operation,
):
    client = AgentRpcClient(shared_secret="rpc-secret")
    rpc_calls: list[str] = []

    async def _reject_gate(machine_id: str) -> bool:
        assert machine_id == "remote-node"
        return False

    gate_setter = getattr(
        client,
        "set_codex_mutation_dispatch_gate",
        None,
    )
    assert callable(gate_setter)
    gate_setter(_reject_gate)
    monkeypatch.setattr(
        client,
        "_resolve_endpoint",
        lambda _machine_id: ("127.0.0.1", 8787),
    )

    async def _call(**kwargs):
        rpc_calls.append(str(kwargs.get("method", "")))
        return {"thread_id": "", "turn_id": ""}

    monkeypatch.setattr(client._client, "call", _call)

    with pytest.raises(ClusterRpcError, match="was not dispatched"):
        if operation == "ensure_thread":
            await client.ensure_thread(
                "remote-node",
                window_id="@remote",
                cwd="/tmp/demo",
            )
        elif operation == "resume_latest":
            await client.resume_latest(
                "remote-node",
                window_id="@remote",
                cwd="/tmp/demo",
            )
        elif operation == "resume_thread":
            await client.resume_thread(
                "remote-node",
                window_id="@remote",
                cwd="/tmp/demo",
                thread_id="thread-1",
            )
        elif operation == "fork_thread":
            await client.fork_thread(
                "remote-node",
                window_id="@remote",
                thread_id="thread-1",
            )
        elif operation == "rollback_thread":
            await client.rollback_thread(
                "remote-node",
                window_id="@remote",
                thread_id="thread-1",
                num_turns=1,
            )
        elif operation == "send_inputs":
            await client.send_inputs(
                "remote-node",
                window_id="@remote",
                cwd="/tmp/demo",
                window_name="demo",
                inputs=[{"type": "text", "text": "hello"}],
                steer=False,
            )
        elif operation == "thread_goal_set":
            await client.thread_goal_set(
                "remote-node",
                thread_id="thread-1",
                goal="ship it",
            )
        elif operation == "thread_goal_clear":
            await client.thread_goal_clear(
                "remote-node",
                thread_id="thread-1",
            )
        else:
            await client.turn_interrupt(
                "remote-node",
                thread_id="thread-1",
                turn_id="turn-1",
            )

    assert rpc_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    [
        "ensure_thread",
        "resume_latest",
        "resume_thread",
        "fork_thread",
        "rollback_thread",
        "send_inputs",
        "thread_goal_set",
        "thread_goal_clear",
        "turn_interrupt",
    ],
)
async def test_agent_rpc_binds_confirmed_epoch_to_each_mutation(
    monkeypatch,
    operation,
):
    client = AgentRpcClient(shared_secret="rpc-secret")
    rpc_params: list[dict[str, object]] = []

    async def _confirmed_gate(machine_id: str):
        assert machine_id == "remote-node"
        return "confirmed-agent-epoch", 100.0

    async def _call(**kwargs):
        rpc_params.append(dict(kwargs["params"]))
        if kwargs.get("method") == "agent/turn_interrupt":
            return {
                "ok": True,
                "machine_id": "remote-node",
                "thread_id": "thread-1",
                "turn_id": "turn-1",
            }
        return {"thread_id": "thread-1", "turn_id": ""}

    async def _accept_remote_result(*, window_id, result):
        _ = window_id, result
        return True

    client.set_codex_mutation_dispatch_gate(_confirmed_gate)
    monkeypatch.setattr(
        client,
        "_resolve_endpoint",
        lambda _machine_id: ("127.0.0.1", 8787),
    )
    monkeypatch.setattr(client._client, "call", _call)
    monkeypatch.setattr(
        session_manager,
        "_accept_remote_transport_result",
        _accept_remote_result,
    )

    if operation == "ensure_thread":
        await client.ensure_thread(
            "remote-node",
            window_id="@remote",
            cwd="/tmp/demo",
        )
    elif operation == "resume_latest":
        await client.resume_latest(
            "remote-node",
            window_id="@remote",
            cwd="/tmp/demo",
        )
    elif operation == "resume_thread":
        await client.resume_thread(
            "remote-node",
            window_id="@remote",
            cwd="/tmp/demo",
            thread_id="thread-1",
        )
    elif operation == "fork_thread":
        await client.fork_thread(
            "remote-node",
            window_id="@remote",
            thread_id="thread-1",
        )
    elif operation == "rollback_thread":
        await client.rollback_thread(
            "remote-node",
            window_id="@remote",
            thread_id="thread-1",
            num_turns=1,
        )
    elif operation == "send_inputs":
        await client.send_inputs(
            "remote-node",
            window_id="@remote",
            cwd="/tmp/demo",
            window_name="demo",
            inputs=[{"type": "text", "text": "hello"}],
            steer=False,
        )
    elif operation == "thread_goal_set":
        await client.thread_goal_set(
            "remote-node",
            thread_id="thread-1",
            goal="ship it",
        )
    elif operation == "thread_goal_clear":
        await client.thread_goal_clear(
            "remote-node",
            thread_id="thread-1",
        )
    else:
        await client.turn_interrupt(
            "remote-node",
            thread_id="thread-1",
            turn_id="turn-1",
        )

    assert len(rpc_params) == 1
    assert rpc_params[0]["expected_transport_epoch"] == (
        "confirmed-agent-epoch"
    )
    assert rpc_params[0]["expected_transport_epoch_started_at"] == 100.0


@pytest.mark.asyncio
async def test_agent_rpc_marks_confirmed_legacy_mutation_for_replacement_fence(
    monkeypatch,
):
    client = AgentRpcClient(shared_secret="rpc-secret")
    rpc_params: list[dict[str, object]] = []

    async def _confirmed_legacy_gate(_machine_id: str) -> bool:
        return True

    async def _call(**kwargs):
        rpc_params.append(dict(kwargs["params"]))
        return {"cleared": True}

    client.set_codex_mutation_dispatch_gate(_confirmed_legacy_gate)
    monkeypatch.setattr(
        client,
        "_resolve_endpoint",
        lambda _machine_id: ("127.0.0.1", 8787),
    )
    monkeypatch.setattr(client._client, "call", _call)

    assert await client.thread_goal_clear(
        "remote-node",
        thread_id="thread-1",
    ) == {"cleared": True}
    assert rpc_params == [
        {
            "thread_id": "thread-1",
            "expected_transport_legacy": True,
        }
    ]


@pytest.mark.asyncio
async def test_agent_rpc_preserves_remote_epoch_rejection_as_deferred(
    monkeypatch,
):
    client = AgentRpcClient(shared_secret="rpc-secret")

    async def _confirmed_gate(_machine_id: str):
        return "confirmed-agent-epoch", 100.0

    async def _call(**_kwargs):
        raise ClusterRpcError(
            "Remote Codex mutation was not dispatched: expected transport "
            "epoch confirmed-agent-epoch, but the replacement agent has "
            "epoch replacement-agent-epoch"
        )

    client.set_codex_mutation_dispatch_gate(_confirmed_gate)
    monkeypatch.setattr(
        client,
        "_resolve_endpoint",
        lambda _machine_id: ("127.0.0.1", 8787),
    )
    monkeypatch.setattr(client._client, "call", _call)

    with pytest.raises(
        agent_rpc.RemoteCodexMutationDeferredError,
        match="replacement agent",
    ):
        await client.thread_goal_clear(
            "remote-node",
            thread_id="thread-1",
        )


@pytest.mark.asyncio
async def test_modern_agent_rejects_legacy_fence_before_codex_mutation(
    monkeypatch,
):
    mutation_calls: list[str] = []

    async def _goal_clear(*, thread_id: str):
        mutation_calls.append(thread_id)
        return {"cleared": True}

    monkeypatch.setattr(
        agent_rpc.codex_app_server_client,
        "thread_goal_clear",
        _goal_clear,
    )

    with pytest.raises(
        ClusterRpcError,
        match="legacy transport fence.*replacement agent",
    ):
        await AgentRpcServer(
            shared_secret="rpc-secret"
        )._thread_goal_clear(
            {
                "thread_id": "thread-1",
                "expected_transport_legacy": True,
            }
        )

    assert mutation_calls == []


@pytest.mark.asyncio
async def test_agent_allows_unfenced_mutation_from_older_controller(
    monkeypatch,
):
    mutation_calls: list[str] = []

    async def _goal_clear(*, thread_id: str):
        mutation_calls.append(thread_id)
        return {"cleared": True}

    monkeypatch.setattr(
        agent_rpc.codex_app_server_client,
        "thread_goal_clear",
        _goal_clear,
    )

    assert await AgentRpcServer(
        shared_secret="rpc-secret"
    )._thread_goal_clear({"thread_id": "thread-1"}) == {"cleared": True}
    assert mutation_calls == ["thread-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fence",
    [
        {"expected_transport_epoch": "agent-epoch-1"},
        {"expected_transport_epoch_started_at": 100.0},
        {"expected_transport_legacy": False},
        {
            "expected_transport_legacy": True,
            "expected_transport_epoch": "agent-epoch-1",
            "expected_transport_epoch_started_at": 100.0,
        },
    ],
)
async def test_agent_rejects_partial_or_invalid_transport_fence_before_mutation(
    monkeypatch,
    fence,
):
    mutation_calls: list[str] = []

    async def _goal_clear(*, thread_id: str):
        mutation_calls.append(thread_id)
        return {"cleared": True}

    monkeypatch.setattr(
        agent_rpc.codex_app_server_client,
        "thread_goal_clear",
        _goal_clear,
    )

    with pytest.raises(ClusterRpcError, match="fence is invalid"):
        await AgentRpcServer(
            shared_secret="rpc-secret"
        )._thread_goal_clear(
            {"thread_id": "thread-1", **fence}
        )

    assert mutation_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    [
        "ensure_thread",
        "resume_latest",
        "resume_thread",
        "fork_thread",
        "rollback_thread",
        "send_inputs",
        "thread_goal_set",
        "thread_goal_clear",
    ],
)
async def test_agent_rejects_mismatched_expected_epoch_before_codex_mutation(
    monkeypatch,
    operation,
):
    mutation_calls: list[str] = []

    async def _record_mutation(*_args, **_kwargs):
        mutation_calls.append(operation)
        if operation == "ensure_thread":
            return "thread-new", ""
        if operation in {"resume_latest", "resume_thread"}:
            return "thread-new"
        if operation == "send_inputs":
            return True, "ok"
        return {"thread": {"id": "thread-new"}}

    async def _ensure_started() -> None:
        return None

    monkeypatch.setattr(
        agent_rpc.codex_app_server_client,
        "transport_state_snapshot",
        lambda: {
            "epoch": "replacement-agent-epoch",
            "epoch_started_at": 200.0,
            "generation": 1,
            "reset_sequence": 0,
            "last_reset_generation": 0,
            "last_reset_reason": "",
            "last_reset_at": 0.0,
        },
    )
    monkeypatch.setattr(
        agent_rpc.codex_app_server_client,
        "ensure_started",
        _ensure_started,
    )
    monkeypatch.setattr(agent_rpc, "_configure_remote_window", lambda **_kwargs: None)
    monkeypatch.setattr(session_manager, "_save_state", lambda: None)

    if operation == "ensure_thread":
        monkeypatch.setattr(
            session_manager,
            "_ensure_codex_thread_for_window",
            _record_mutation,
        )
    elif operation == "resume_latest":
        monkeypatch.setattr(
            session_manager,
            "resume_latest_codex_session_for_window",
            _record_mutation,
        )
    elif operation == "resume_thread":
        monkeypatch.setattr(
            session_manager,
            "resume_codex_session_for_window",
            _record_mutation,
        )
    elif operation == "fork_thread":
        monkeypatch.setattr(
            agent_rpc.codex_app_server_client,
            "thread_fork",
            _record_mutation,
        )
    elif operation == "rollback_thread":
        monkeypatch.setattr(
            agent_rpc.codex_app_server_client,
            "thread_rollback",
            _record_mutation,
        )
    elif operation == "send_inputs":
        monkeypatch.setattr(
            session_manager,
            "send_inputs_to_window",
            _record_mutation,
        )
    elif operation == "thread_goal_set":
        monkeypatch.setattr(
            agent_rpc.codex_app_server_client,
            "thread_goal_set",
            _record_mutation,
        )
    else:
        monkeypatch.setattr(
            agent_rpc.codex_app_server_client,
            "thread_goal_clear",
            _record_mutation,
        )

    params = {
        "expected_transport_epoch": "confirmed-agent-epoch",
        "expected_transport_epoch_started_at": 100.0,
        "window_id": "@remote",
        "cwd": "/tmp/demo",
        "window_name": "demo",
        "thread_id": "thread-1",
        "turn_id": "turn-1",
        "num_turns": 1,
        "inputs": [{"type": "text", "text": "hello"}],
        "goal": "ship it",
    }
    server = AgentRpcServer(shared_secret="rpc-secret")

    with pytest.raises(
        ClusterRpcError,
        match="expected transport epoch.*replacement agent",
    ):
        await getattr(server, f"_{operation}")(params)

    assert mutation_calls == []


@pytest.mark.asyncio
async def test_agent_rpc_mutation_gate_does_not_block_goal_read(monkeypatch):
    client = AgentRpcClient(shared_secret="rpc-secret")

    async def _reject_gate(_machine_id: str) -> bool:
        return False

    client.set_codex_mutation_dispatch_gate(_reject_gate)
    monkeypatch.setattr(
        client,
        "_resolve_endpoint",
        lambda _machine_id: ("127.0.0.1", 8787),
    )

    async def _call(**kwargs):
        assert kwargs["method"] == "agent/thread_goal_get"
        return {"goal": "ship it"}

    monkeypatch.setattr(client._client, "call", _call)

    assert await client.thread_goal_get(
        "remote-node",
        thread_id="thread-1",
    ) == {"goal": "ship it"}


@pytest.mark.asyncio
async def test_agent_send_rejects_ack_if_transport_recycles_during_mutation(
    monkeypatch,
):
    window_id = "@remote-send-reset-race"
    transport_state: dict[str, object] = {
        "epoch": "agent-epoch-1",
        "epoch_started_at": 100.0,
        "generation": 11,
        "reset_sequence": 3,
        "last_reset_generation": 0,
        "last_reset_reason": "",
        "last_reset_at": 0.0,
    }
    send_entered = asyncio.Event()
    allow_send_to_return = asyncio.Event()

    async def _ensure_started() -> None:
        return None

    async def _send_inputs_to_window(
        _window_id,
        _inputs,
        **_kwargs,
    ):
        state = session_manager.get_window_state(window_id)
        state.codex_thread_id = "thread-before-reset"
        state.codex_active_turn_id = "turn-before-reset"
        send_entered.set()
        await allow_send_to_return.wait()
        return True, "ok"

    monkeypatch.setattr(
        agent_rpc.codex_app_server_client,
        "ensure_started",
        _ensure_started,
    )
    monkeypatch.setattr(
        agent_rpc.codex_app_server_client,
        "transport_state_snapshot",
        lambda: dict(transport_state),
    )
    monkeypatch.setattr(
        session_manager,
        "send_inputs_to_window",
        _send_inputs_to_window,
    )
    monkeypatch.setattr(session_manager, "_save_state", lambda: None)

    previous_state = session_manager.window_states.pop(window_id, None)
    try:
        send_task = asyncio.create_task(
            AgentRpcServer(shared_secret="rpc-secret")._send_inputs(
                {
                    "window_id": window_id,
                    "cwd": "/tmp/demo",
                    "window_name": "demo",
                    "inputs": [{"type": "text", "text": "hello"}],
                }
            )
        )
        await send_entered.wait()
        transport_state.update(
            {
                "generation": 12,
                "reset_sequence": 4,
                "last_reset_generation": 11,
                "last_reset_reason": "request_timeout:turn/start",
                "last_reset_at": 101.0,
            }
        )
        allow_send_to_return.set()

        with pytest.raises(
            ClusterRpcError,
            match="transport changed.*outcome is uncertain",
        ):
            await send_task
    finally:
        allow_send_to_return.set()
        if previous_state is None:
            session_manager.window_states.pop(window_id, None)
        else:
            session_manager.window_states[window_id] = previous_state


@pytest.mark.asyncio
async def test_agent_send_starts_transport_before_acknowledgement_fence(
    monkeypatch,
):
    window_id = "@remote-send-startup-fence"
    transport_state: dict[str, object] = {
        "epoch": "agent-epoch-1",
        "epoch_started_at": 100.0,
        "generation": 0,
        "reset_sequence": 0,
        "last_reset_generation": 0,
        "last_reset_reason": "",
        "last_reset_at": 0.0,
    }
    events: list[str] = []

    async def _ensure_started() -> None:
        events.append("ensure_started")
        transport_state["generation"] = 1

    async def _send_inputs_to_window(
        _window_id,
        _inputs,
        **_kwargs,
    ):
        assert events == ["ensure_started"]
        assert transport_state["generation"] == 1
        events.append("send")
        state = session_manager.get_window_state(window_id)
        state.codex_thread_id = "thread-1"
        state.codex_active_turn_id = "turn-1"
        return True, "ok"

    monkeypatch.setattr(
        agent_rpc.codex_app_server_client,
        "ensure_started",
        _ensure_started,
    )
    monkeypatch.setattr(
        agent_rpc.codex_app_server_client,
        "transport_state_snapshot",
        lambda: dict(transport_state),
    )
    monkeypatch.setattr(
        session_manager,
        "send_inputs_to_window",
        _send_inputs_to_window,
    )
    monkeypatch.setattr(session_manager, "_save_state", lambda: None)

    previous_state = session_manager.window_states.pop(window_id, None)
    try:
        payload = await AgentRpcServer(
            shared_secret="rpc-secret"
        )._send_inputs(
            {
                "window_id": window_id,
                "cwd": "/tmp/demo",
                "window_name": "demo",
                "inputs": [{"type": "text", "text": "hello"}],
            }
        )
    finally:
        if previous_state is None:
            session_manager.window_states.pop(window_id, None)
        else:
            session_manager.window_states[window_id] = previous_state

    assert payload["ok"] is True
    assert payload["transport_generation"] == 1
    assert payload["transport_reset_occurred"] is False
    assert events == ["ensure_started", "send"]


@pytest.mark.asyncio
async def test_agent_concurrent_sends_configure_expected_thread_inside_send_lock(
    monkeypatch,
):
    window_id = "@remote-send-thread-race"
    first_start_entered = asyncio.Event()
    release_first_start = asyncio.Event()
    ensure_calls = 0
    dispatched: list[tuple[str, str]] = []
    transport_state = {
        "epoch": "agent-epoch-1",
        "epoch_started_at": 100.0,
        "generation": 1,
        "reset_sequence": 0,
        "last_reset_generation": 0,
        "last_reset_reason": "",
        "last_reset_at": 0.0,
    }

    async def _ensure_started() -> None:
        nonlocal ensure_calls
        ensure_calls += 1
        if ensure_calls == 1:
            first_start_entered.set()
            await release_first_start.wait()

    async def _send_inputs_via_codex_app_server(
        *, window_id: str, inputs: list[dict[str, object]], **_kwargs
    ):
        text = str(inputs[0]["text"])
        current_thread_id = session_manager.get_window_codex_thread_id(window_id)
        dispatched.append((text, current_thread_id))
        return True, "ok"

    monkeypatch.setattr(
        agent_rpc.codex_app_server_client,
        "ensure_started",
        _ensure_started,
    )
    monkeypatch.setattr(
        agent_rpc.codex_app_server_client,
        "transport_state_snapshot",
        lambda: dict(transport_state),
    )
    monkeypatch.setattr(
        session_manager,
        "_send_inputs_via_codex_app_server",
        _send_inputs_via_codex_app_server,
    )
    monkeypatch.setattr(session_manager, "_save_state", lambda: None)

    previous_state = session_manager.window_states.pop(window_id, None)
    server = AgentRpcServer(shared_secret="rpc-secret")
    try:
        send_a = asyncio.create_task(
            server._send_inputs(
                {
                    "window_id": window_id,
                    "cwd": "/tmp/demo",
                    "window_name": "demo",
                    "thread_id": "thread-a",
                    "inputs": [{"type": "text", "text": "A"}],
                }
            )
        )
        await first_start_entered.wait()
        send_b = asyncio.create_task(
            server._send_inputs(
                {
                    "window_id": window_id,
                    "cwd": "/tmp/demo",
                    "window_name": "demo",
                    "thread_id": "thread-b",
                    "inputs": [{"type": "text", "text": "B"}],
                }
            )
        )
        await send_b
        release_first_start.set()
        await send_a
    finally:
        release_first_start.set()
        if previous_state is None:
            session_manager.window_states.pop(window_id, None)
        else:
            session_manager.window_states[window_id] = previous_state

    assert dispatched == [("B", "thread-b"), ("A", "thread-a")]


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


@pytest.mark.asyncio
async def test_agent_rpc_codex_health_probe_round_trip(monkeypatch):
    observed_timeouts: list[float] = []

    async def _probe_health(*, timeout: float) -> bool:
        observed_timeouts.append(timeout)
        return False

    monkeypatch.setattr(
        agent_rpc.codex_app_server_client,
        "probe_health",
        _probe_health,
    )

    server = AgentRpcServer(shared_secret="rpc-secret")
    await server.start(host="127.0.0.1", port=0)
    try:
        host, port = server.bound_address()
        node_registry.note_heartbeat(
            machine_id="health-node",
            display_name="Health Node",
            transport="agent_rpc",
            rpc_host=host,
            rpc_port=port,
            is_local=False,
            now=100.0,
        )
        client = AgentRpcClient(shared_secret="rpc-secret")

        healthy = await client.probe_codex_health(
            "health-node",
            timeout=4.0,
        )
        health_state = await client.probe_codex_health_state(
            "health-node",
            timeout=3.0,
        )

        assert healthy is False
        assert health_state["healthy"] is False
        assert health_state["transport_epoch"]
        assert health_state["transport_epoch_started_at"] > 0
        assert health_state["transport_generation"] >= 0
        assert health_state["transport_reset_sequence"] >= 0
        assert observed_timeouts == [4.0, 3.0]
    finally:
        await server.stop()
