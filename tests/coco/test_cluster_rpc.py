from __future__ import annotations

import pytest

from coco.cluster_rpc import ClusterRpcClient, ClusterRpcError, ClusterRpcServer


@pytest.mark.asyncio
async def test_cluster_rpc_round_trip():
    server = ClusterRpcServer(shared_secret="test-secret")

    async def _ping(params: dict[str, object]) -> dict[str, object]:
        return {"echo": params.get("value")}

    server.register("ping", _ping)
    await server.start(host="127.0.0.1", port=0)
    try:
        host, port = server.bound_address()
        client = ClusterRpcClient(shared_secret="test-secret")
        result = await client.call(host=host, port=port, method="ping", params={"value": "ok"})
        assert result == {"echo": "ok"}
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_cluster_rpc_rejects_invalid_secret():
    server = ClusterRpcServer(shared_secret="expected")

    async def _ping(params: dict[str, object]) -> dict[str, object]:
        return {"ok": True}

    server.register("ping", _ping)
    await server.start(host="127.0.0.1", port=0)
    try:
        host, port = server.bound_address()
        client = ClusterRpcClient(shared_secret="wrong")
        with pytest.raises(ClusterRpcError, match="unauthorized"):
            await client.call(host=host, port=port, method="ping", params={})
    finally:
        await server.stop()
