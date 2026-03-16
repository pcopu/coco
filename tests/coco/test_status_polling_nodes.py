from types import SimpleNamespace

import pytest

import coco.handlers.status_polling as status_polling
from coco.node_registry import NodeRegistry


@pytest.mark.asyncio
async def test_emit_node_status_notifications_sends_offline_and_recovery(monkeypatch, tmp_path):
    registry = NodeRegistry(
        state_file=tmp_path / "nodes.json",
        offline_timeout_seconds=45.0,
    )
    registry.note_heartbeat(
        machine_id="macbook",
        display_name="MacBook",
        transport="agent_rpc",
        is_local=False,
        now=100.0,
    )
    registry.drain_status_changes()
    registry.mark_stale_nodes_offline(now=146.0)

    sent: list[tuple[int, int | None, str]] = []

    async def _safe_send(_bot, chat_id, text, *, message_thread_id=None, **_kwargs):
        sent.append((chat_id, message_thread_id, text))

    monkeypatch.setattr(status_polling, "node_registry", registry)
    monkeypatch.setattr(status_polling, "safe_send", _safe_send)
    monkeypatch.setattr(
        status_polling.session_manager,
        "iter_topic_bindings",
        lambda: iter(
            [
                (
                    100,
                    -1001,
                    10,
                    SimpleNamespace(
                        machine_id="macbook",
                        machine_display_name="MacBook",
                    ),
                ),
                (
                    100,
                    -1001,
                    11,
                    SimpleNamespace(
                        machine_id="other-node",
                        machine_display_name="Other Node",
                    ),
                ),
            ]
        ),
    )
    monkeypatch.setattr(
        status_polling.session_manager,
        "resolve_chat_id",
        lambda _user_id, _thread_id, *, chat_id=None: chat_id or _user_id,
    )

    await status_polling._emit_node_status_notifications(object())

    assert sent == [
        (-1001, 10, "🖥️ Machine offline: `MacBook`\nLast seen: `1970-01-01 00:01 UTC`"),
    ]

    registry.note_heartbeat(
        machine_id="macbook",
        display_name="MacBook",
        transport="agent_rpc",
        is_local=False,
        now=200.0,
    )
    sent.clear()

    await status_polling._emit_node_status_notifications(object())

    assert sent == [
        (-1001, 10, "🟢 Machine back online: `MacBook`"),
    ]


@pytest.mark.asyncio
async def test_probe_stale_nodes_keeps_target_online_when_remote_monitor_confirms_reachability(
    monkeypatch,
    tmp_path,
):
    registry = NodeRegistry(
        state_file=tmp_path / "nodes.json",
        offline_timeout_seconds=45.0,
    )
    registry.note_heartbeat(
        machine_id="controller",
        display_name="Controller",
        transport="local",
        is_local=True,
        capabilities=["controller", "monitor"],
        now=140.0,
    )
    registry.note_heartbeat(
        machine_id="macbook",
        display_name="MacBook",
        transport="agent_rpc",
        rpc_host="100.64.0.10",
        rpc_port=8787,
        is_local=False,
        capabilities=["monitor"],
        now=100.0,
    )
    registry.note_heartbeat(
        machine_id="server-b",
        display_name="Server B",
        transport="agent_rpc",
        rpc_host="100.64.0.20",
        rpc_port=8787,
        is_local=False,
        capabilities=["monitor"],
        now=140.0,
    )
    registry.drain_status_changes()

    probes: list[tuple[str, str]] = []

    async def _probe(machine_id: str, *, via_machine_id: str = "") -> dict[str, object]:
        probes.append((machine_id, via_machine_id))
        assert machine_id == "macbook"
        assert via_machine_id == "server-b"
        return {
            "machine_id": "macbook",
            "display_name": "MacBook",
            "tailnet_name": "macbook.tail",
            "transport": "agent_rpc",
            "rpc_host": "100.64.0.10",
            "rpc_port": 8787,
            "is_local": False,
            "capabilities": ["monitor"],
            "status": "online",
            "last_seen_ts": 146.0,
        }

    monkeypatch.setattr(status_polling, "node_registry", registry)
    monkeypatch.setattr(status_polling, "_local_machine_identity", lambda: ("controller", "Controller"))
    monkeypatch.setattr(status_polling, "_probe_machine_from_monitor", _probe)

    await status_polling._probe_stale_nodes(bot=None, now=146.0)

    node = registry.get_node("macbook")
    assert node is not None
    assert node.status == "online"
    assert node.last_seen_ts == 146.0
    assert probes == [("macbook", "server-b")]
    assert registry.drain_status_changes() == []


@pytest.mark.asyncio
async def test_probe_stale_nodes_marks_target_offline_after_failed_monitor_probe(
    monkeypatch,
    tmp_path,
):
    registry = NodeRegistry(
        state_file=tmp_path / "nodes.json",
        offline_timeout_seconds=45.0,
    )
    registry.note_heartbeat(
        machine_id="controller",
        display_name="Controller",
        transport="local",
        is_local=True,
        capabilities=["controller", "monitor"],
        now=140.0,
    )
    registry.note_heartbeat(
        machine_id="macbook",
        display_name="MacBook",
        transport="agent_rpc",
        rpc_host="100.64.0.10",
        rpc_port=8787,
        is_local=False,
        capabilities=["monitor"],
        now=100.0,
    )
    registry.drain_status_changes()

    async def _probe(machine_id: str, *, via_machine_id: str = "") -> dict[str, object]:
        raise RuntimeError(f"unreachable:{machine_id}:{via_machine_id}")

    monkeypatch.setattr(status_polling, "node_registry", registry)
    monkeypatch.setattr(status_polling, "_local_machine_identity", lambda: ("controller", "Controller"))
    monkeypatch.setattr(status_polling, "_probe_machine_from_monitor", _probe)

    await status_polling._probe_stale_nodes(bot=None, now=146.0)

    node = registry.get_node("macbook")
    assert node is not None
    assert node.status == "offline"
    changes = registry.drain_status_changes()
    assert len(changes) == 1
    assert changes[0].machine_id == "macbook"
    assert changes[0].new_status == "offline"
