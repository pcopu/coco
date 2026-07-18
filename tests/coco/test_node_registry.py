import json
import math

from coco.node_registry import NODE_STATUS_OFFLINE, NODE_STATUS_ONLINE, NodeRegistry
import coco.node_registry as node_registry_mod


def test_note_heartbeat_marks_remote_node_online_and_persists(tmp_path):
    state_file = tmp_path / "nodes.json"
    registry = NodeRegistry(
        state_file=state_file,
        offline_timeout_seconds=45.0,
    )

    registry.note_heartbeat(
        machine_id="macbook",
        display_name="MacBook",
        transport="agent_rpc",
        is_local=False,
        now=100.0,
    )

    node = registry.get_node("macbook")
    assert node is not None
    assert node.status == NODE_STATUS_ONLINE
    assert node.display_name == "MacBook"
    assert node.transport == "agent_rpc"
    assert node.is_local is False
    assert node.rpc_host == ""
    assert node.rpc_port == 0

    payload = json.loads(state_file.read_text(encoding="utf-8"))
    assert payload["nodes"]["macbook"]["display_name"] == "MacBook"
    assert payload["nodes"]["macbook"]["status"] == NODE_STATUS_ONLINE


def test_mark_stale_nodes_offline_and_recover_with_heartbeat(tmp_path):
    registry = NodeRegistry(
        state_file=tmp_path / "nodes.json",
        offline_timeout_seconds=45.0,
    )
    registry.note_heartbeat(
        machine_id="server-a",
        display_name="Server A",
        transport="agent_rpc",
        is_local=False,
        now=100.0,
    )

    assert registry.drain_status_changes() == []

    registry.mark_stale_nodes_offline(now=146.0)
    changes = registry.drain_status_changes()

    assert len(changes) == 1
    assert changes[0].machine_id == "server-a"
    assert changes[0].old_status == NODE_STATUS_ONLINE
    assert changes[0].new_status == NODE_STATUS_OFFLINE

    registry.note_heartbeat(
        machine_id="server-a",
        display_name="Server A",
        transport="agent_rpc",
        is_local=False,
        now=150.0,
    )
    changes = registry.drain_status_changes()

    assert len(changes) == 1
    assert changes[0].machine_id == "server-a"
    assert changes[0].old_status == NODE_STATUS_OFFLINE
    assert changes[0].new_status == NODE_STATUS_ONLINE


def test_note_heartbeat_persists_rpc_endpoint_metadata(tmp_path):
    registry = NodeRegistry(
        state_file=tmp_path / "nodes.json",
        offline_timeout_seconds=45.0,
    )

    registry.note_heartbeat(
        machine_id="server-b",
        display_name="Server B",
        transport="agent_rpc",
        is_local=False,
        rpc_host="100.90.80.70",
        rpc_port=8787,
        now=100.0,
    )

    node = registry.get_node("server-b")
    assert node is not None
    assert node.rpc_host == "100.90.80.70"
    assert node.rpc_port == 8787


def test_ensure_local_node_includes_runtime_capabilities(tmp_path, monkeypatch):
    registry = NodeRegistry(
        state_file=tmp_path / "nodes.json",
        offline_timeout_seconds=45.0,
    )

    monkeypatch.setattr(
        node_registry_mod,
        "config",
        type(
            "_Cfg",
            (),
            {
                "node_registry_file": tmp_path / "nodes.json",
                "node_offline_timeout": 45.0,
                "machine_id": "local-node",
                "machine_name": "Local Node",
                "tailnet_name": "local.tail",
                "rpc_advertise_host": "100.64.0.10",
                "rpc_port": 8787,
                "browse_root": "/repo",
                "controller_capable": True,
                "controller_active": True,
                "preferred_controller": True,
            },
        )(),
    )
    monkeypatch.setattr(
        node_registry_mod,
        "get_local_runtime_capabilities",
        lambda *, controller_capable=False: [
            "controller",
            "monitor",
            "transcription",
            "tts",
        ],
    )
    monkeypatch.setattr(
        node_registry_mod,
        "get_local_runtime_summary",
        lambda *, controller_capable=False: {
            "capabilities": [
                "controller",
                "monitor",
                "transcription",
                "tts",
            ],
            "transcription": {"mode": "compatible", "model_name": "base"},
            "tts": {"available": True, "default_voice": "F2", "default_speed": 1.4},
        },
    )

    node = registry.ensure_local_node()

    assert node.capabilities == ["controller", "monitor", "transcription", "tts"]
    assert node.runtime == {
        "capabilities": [
            "controller",
            "monitor",
            "transcription",
            "tts",
        ],
        "transcription": {"mode": "compatible", "model_name": "base"},
        "tts": {"available": True, "default_voice": "F2", "default_speed": 1.4},
    }


def test_registry_load_tolerates_malformed_persisted_endpoint_and_flags(tmp_path):
    state_file = tmp_path / "nodes.json"
    state_file.write_text(
        json.dumps(
            {
                "nodes": {
                    "broken": {
                        "machine_id": "broken",
                        "display_name": "Broken",
                        "rpc_port": "not-a-port",
                        "rpc_host": None,
                        "tailnet_name": None,
                        "transport": None,
                        "status": "nonsense",
                        "browse_roots": None,
                        "capabilities": 7,
                        "is_local": "false",
                        "controller_active": "false",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    registry = NodeRegistry(state_file=state_file, offline_timeout_seconds=45.0)

    node = registry.get_node("broken")
    assert node is not None
    assert node.rpc_port == 0
    assert node.rpc_host == ""
    assert node.tailnet_name == ""
    assert node.transport == "local"
    assert node.status == NODE_STATUS_OFFLINE
    assert node.browse_roots == []
    assert node.capabilities == []
    assert node.is_local is False
    assert node.controller_active is False


def test_registry_load_recovers_from_non_object_json(tmp_path):
    state_file = tmp_path / "nodes.json"
    state_file.write_text("[]", encoding="utf-8")

    registry = NodeRegistry(state_file=state_file, offline_timeout_seconds=45.0)

    assert registry.iter_nodes() == []


def test_registry_load_rejects_non_finite_timestamp_and_invalid_port(tmp_path):
    state_file = tmp_path / "nodes.json"
    state_file.write_text(
        json.dumps(
            {
                "nodes": {
                    "broken": {
                        "machine_id": "broken",
                        "last_seen_ts": math.inf,
                        "rpc_port": 70000,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    registry = NodeRegistry(state_file=state_file, offline_timeout_seconds=45.0)

    node = registry.get_node("broken")
    assert node is not None
    assert node.last_seen_ts == 0.0
    assert node.rpc_port == 0


def test_note_heartbeat_rejects_out_of_range_rpc_port(tmp_path):
    registry = NodeRegistry(
        state_file=tmp_path / "nodes.json",
        offline_timeout_seconds=45.0,
    )

    node = registry.note_heartbeat(
        machine_id="remote",
        display_name="Remote",
        transport="agent_rpc",
        rpc_port=70000,
        is_local=False,
        now=100.0,
    )

    assert node.rpc_port == 0
