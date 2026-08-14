from __future__ import annotations

import asyncio
import json

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


@pytest.mark.asyncio
async def test_cluster_rpc_rejects_non_object_json_request():
    server = ClusterRpcServer(shared_secret="expected")

    response = await server._handle_request_line(b"[]")

    assert response == {"id": "", "ok": False, "error": "invalid_request"}


@pytest.mark.asyncio
async def test_cluster_rpc_converts_unserializable_handler_result_to_error():
    server = ClusterRpcServer(shared_secret="expected")

    async def _bad_result(_params: dict[str, object]):
        return {"not-json"}

    server.register("bad", _bad_result)
    response = await server._handle_request_line(
        b'{"id":"1","secret":"expected","method":"bad","params":{}}'
    )

    assert response == {"id": "1", "ok": False, "error": "invalid_result"}
    json.dumps(response)


@pytest.mark.asyncio
@pytest.mark.parametrize("response_payload", [[], None, "bad", 7])
async def test_cluster_rpc_client_rejects_non_object_response(response_payload):
    async def _respond(reader, writer):
        await reader.readline()
        writer.write((json.dumps(response_payload) + "\n").encode("utf-8"))
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(_respond, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    try:
        client = ClusterRpcClient(shared_secret="secret", timeout_seconds=1)
        with pytest.raises(ClusterRpcError, match="invalid_response"):
            await client.call(host=host, port=port, method="ping", params={})
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_cluster_rpc_client_times_out_blocked_write(monkeypatch):
    class _Reader:
        async def readline(self):
            return b""

    class _Writer:
        def write(self, _data):
            return None

        async def drain(self):
            await asyncio.Event().wait()

        def close(self):
            return None

        async def wait_closed(self):
            return None

    async def _open_connection(_host, _port, **_kwargs):
        return _Reader(), _Writer()

    monkeypatch.setattr(asyncio, "open_connection", _open_connection)
    client = ClusterRpcClient(shared_secret="secret", timeout_seconds=0.01)

    with pytest.raises(ClusterRpcError, match="request_timeout") as raised:
        await asyncio.wait_for(
            client.call(host="127.0.0.1", port=1, method="ping", params={}),
            timeout=0.2,
        )
    assert raised.value.request_dispatched is True


@pytest.mark.asyncio
async def test_cluster_rpc_response_loss_marks_request_as_dispatched():
    applied: list[dict[str, object]] = []

    async def _apply_and_drop_response(reader, writer):
        raw = await reader.readline()
        applied.append(json.loads(raw.decode("utf-8")))
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(_apply_and_drop_response, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    try:
        client = ClusterRpcClient(shared_secret="secret", timeout_seconds=1)
        with pytest.raises(ClusterRpcError) as raised:
            await client.call(host=host, port=port, method="interrupt", params={})
        assert raised.value.request_dispatched is True
        assert applied and applied[0]["method"] == "interrupt"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_cluster_rpc_client_close_error_does_not_mask_protocol_error(monkeypatch):
    class _Reader:
        async def readline(self):
            return b""

    class _Writer:
        def write(self, _data):
            return None

        async def drain(self):
            return None

        def close(self):
            return None

        async def wait_closed(self):
            raise OSError("close failed")

    async def _open_connection(_host, _port, **_kwargs):
        return _Reader(), _Writer()

    monkeypatch.setattr(asyncio, "open_connection", _open_connection)
    client = ClusterRpcClient(shared_secret="secret", timeout_seconds=0.1)

    with pytest.raises(ClusterRpcError, match="empty_response"):
        await client.call(host="127.0.0.1", port=1, method="ping", params={})


@pytest.mark.asyncio
async def test_cluster_rpc_refuses_unauthenticated_non_loopback_listener(monkeypatch):
    server = ClusterRpcServer(shared_secret="")

    async def _unexpected_start(*_args, **_kwargs):
        raise AssertionError("listener should not start")

    monkeypatch.setattr(asyncio, "start_server", _unexpected_start)

    with pytest.raises(ClusterRpcError, match="shared secret"):
        await server.start(host="0.0.0.0", port=8787)


@pytest.mark.asyncio
async def test_cluster_rpc_server_uses_bounded_request_stream_limit(monkeypatch):
    captured: dict[str, object] = {}

    async def _start_server(*_args, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(asyncio, "start_server", _start_server)
    server = ClusterRpcServer(shared_secret="secret")

    await server.start(host="127.0.0.1", port=8787)

    assert captured["limit"] == 1024 * 1024


@pytest.mark.asyncio
async def test_cluster_rpc_round_trips_response_larger_than_default_stream_limit():
    server = ClusterRpcServer(shared_secret="test-secret")
    large_value = "x" * 100_000

    async def _large(_params):
        return {"value": large_value}

    server.register("large", _large)
    await server.start(host="127.0.0.1", port=0)
    try:
        host, port = server.bound_address()
        client = ClusterRpcClient(shared_secret="test-secret", timeout_seconds=1)
        result = await client.call(host=host, port=port, method="large", params={})
        assert result == {"value": large_value}
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_cluster_rpc_server_ignores_client_close_error():
    class _Reader:
        async def readline(self):
            return b""

    class _Writer:
        def close(self):
            return None

        async def wait_closed(self):
            raise ConnectionResetError("peer reset")

    server = ClusterRpcServer(shared_secret="test-secret")

    await server._handle_client(_Reader(), _Writer())


@pytest.mark.asyncio
async def test_cluster_rpc_server_times_out_idle_client(monkeypatch):
    class _Reader:
        async def readline(self):
            await asyncio.Event().wait()

    class _Writer:
        closed = False

        def close(self):
            self.closed = True

        async def wait_closed(self):
            return None

    monkeypatch.setattr("coco.cluster_rpc.RPC_REQUEST_TIMEOUT_SECONDS", 0.01)
    writer = _Writer()
    server = ClusterRpcServer(shared_secret="test-secret")

    await asyncio.wait_for(server._handle_client(_Reader(), writer), timeout=0.2)

    assert writer.closed is True


@pytest.mark.asyncio
async def test_cluster_rpc_server_rejects_connections_over_limit(monkeypatch):
    class _Reader:
        async def readline(self):
            raise AssertionError("over-limit connection must not be read")

    class _Writer:
        closed = False

        def close(self):
            self.closed = True

        async def wait_closed(self):
            return None

    monkeypatch.setattr("coco.cluster_rpc.RPC_MAX_ACTIVE_CONNECTIONS", 1)
    writer = _Writer()
    server = ClusterRpcServer(shared_secret="test-secret")
    server._active_connections = 1

    await server._handle_client(_Reader(), writer)

    assert writer.closed is True


@pytest.mark.asyncio
async def test_cluster_rpc_server_closes_after_one_unauthorized_request():
    class _Reader:
        calls = 0

        async def readline(self):
            self.calls += 1
            if self.calls == 1:
                return b'{"id":"bad","secret":"wrong","method":"ping","params":{}}\n'
            if self.calls == 2:
                return b'{"id":"bad2","secret":"wrong","method":"ping","params":{}}\n'
            return b""

    class _Writer:
        closed = False

        def __init__(self):
            self.payloads: list[bytes] = []

        def write(self, data):
            self.payloads.append(data)

        async def drain(self):
            return None

        def close(self):
            self.closed = True

        async def wait_closed(self):
            return None

    reader = _Reader()
    writer = _Writer()
    server = ClusterRpcServer(shared_secret="expected")

    await server._handle_client(reader, writer)

    assert reader.calls == 1
    assert len(writer.payloads) == 1
    assert b'"error":"unauthorized"' in writer.payloads[0]
    assert writer.closed is True
