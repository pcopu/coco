from __future__ import annotations

import pytest

from coco.controller_rpc import ControllerRpcServer


@pytest.mark.asyncio
async def test_controller_rpc_server_routes_heartbeat_and_request():
    calls: list[tuple[str, dict[str, object]]] = []

    async def _heartbeat(params: dict[str, object]) -> dict[str, object]:
        calls.append(("heartbeat", params))
        return {"ok": True}

    async def _notification(params: dict[str, object]) -> None:
        calls.append(("notification", params))

    async def _request(params: dict[str, object]) -> dict[str, object]:
        calls.append(("request", params))
        return {"decision": "approve"}

    server = ControllerRpcServer(
        shared_secret="controller-secret",
        heartbeat_handler=_heartbeat,
        notification_handler=_notification,
        request_handler=_request,
    )
    await server.start(host="127.0.0.1", port=0)
    try:
        host, port = server.bound_address()

        from coco.cluster_rpc import ClusterRpcClient

        client = ClusterRpcClient(shared_secret="controller-secret")
        heartbeat_result = await client.call(
            host=host,
            port=port,
            method="controller/heartbeat",
            params={"machine_id": "node-a"},
        )
        request_result = await client.call(
            host=host,
            port=port,
            method="controller/request",
            params={"method": "item/tool/call", "params": {"foo": "bar"}},
        )
        notification_result = await client.call(
            host=host,
            port=port,
            method="controller/notification",
            params={"method": "turn/started", "params": {"threadId": "t1"}},
        )

        assert heartbeat_result == {"ok": True}
        assert request_result == {"decision": "approve"}
        assert notification_result == {"ok": True}
        assert calls == [
            ("heartbeat", {"machine_id": "node-a"}),
            ("request", {"method": "item/tool/call", "params": {"foo": "bar"}}),
            ("notification", {"method": "turn/started", "params": {"threadId": "t1"}}),
        ]
    finally:
        await server.stop()
