import json

from coco.node_registry import NODE_STATUS_OFFLINE, NODE_STATUS_ONLINE, NodeRegistry


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
