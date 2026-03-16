"""Tests for Codex app-server client transport and handshake."""

import asyncio
import json
from types import SimpleNamespace

import pytest

import coco.codex_app_server as cas


class _FakeProc:
    def __init__(self) -> None:
        self.returncode = None
        self.stdin = SimpleNamespace()
        self.stdout = SimpleNamespace()
        self.stderr = SimpleNamespace()

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return int(self.returncode)


class _FakeStdin:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None


class _FakeStdoutOverrun:
    def __init__(self) -> None:
        self.readline_events: list[bytes | BaseException] = [
            ValueError("Separator is not found, and chunk exceed the limit"),
            b'{"jsonrpc":"2.0","id":"9","result":{"ok":true}}\n',
        ]
        self.readuntil_events: list[bytes | BaseException] = [
            asyncio.LimitOverrunError(
                "Separator is not found, and chunk exceed the limit",
                consumed=10,
            ),
            b'{"oversized":"line"}\n',
            b'{"jsonrpc":"2.0","id":"9","result":{"ok":true}}\n',
        ]
        self.readexactly_calls: list[int] = []

    async def readline(self) -> bytes:
        event = self.readline_events.pop(0)
        if isinstance(event, BaseException):
            raise event
        return event

    async def readuntil(self, _separator: bytes = b"\n") -> bytes:
        event = self.readuntil_events.pop(0)
        if isinstance(event, BaseException):
            raise event
        return event

    async def readexactly(self, n: int) -> bytes:
        self.readexactly_calls.append(n)
        return b"x" * n


@pytest.mark.asyncio
async def test_ensure_started_runs_initialize_handshake_once(monkeypatch):
    client = cas.CodexAppServerClient()
    events: list[tuple[str, str]] = []
    spawn_kwargs: list[dict[str, object]] = []

    async def _fake_create_subprocess_exec(*_args, **_kwargs):
        spawn_kwargs.append(dict(_kwargs))
        return _FakeProc()

    async def _noop_loop():
        return None

    async def _fake_request_started(method: str, params: dict, *, timeout: float = 60.0):
        _ = timeout
        events.append(("request", method))
        assert method == "initialize"
        assert "clientInfo" in params
        return {"userAgent": "codex/test"}

    async def _fake_write_jsonrpc(payload: dict):
        method = payload.get("method")
        if isinstance(method, str):
            events.append(("notify", method))

    monkeypatch.setattr(cas.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(client, "_reader_loop", _noop_loop)
    monkeypatch.setattr(client, "_stderr_loop", _noop_loop)
    monkeypatch.setattr(client, "_request_started", _fake_request_started)
    monkeypatch.setattr(client, "_write_jsonrpc", _fake_write_jsonrpc)

    await client.ensure_started()
    await client.ensure_started()

    assert events.count(("request", "initialize")) == 1
    assert events.count(("notify", "initialized")) == 1
    assert client.get_server_user_agent() == "codex/test"
    assert spawn_kwargs
    assert spawn_kwargs[0].get("limit") == cas.APP_SERVER_STREAM_LIMIT


@pytest.mark.asyncio
async def test_ensure_started_stops_process_when_handshake_fails(monkeypatch):
    client = cas.CodexAppServerClient()

    async def _fake_create_subprocess_exec(*_args, **_kwargs):
        return _FakeProc()

    async def _noop_loop():
        return None

    async def _boom_request_started(*_args, **_kwargs):
        raise cas.CodexAppServerError("boom")

    monkeypatch.setattr(cas.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(client, "_reader_loop", _noop_loop)
    monkeypatch.setattr(client, "_stderr_loop", _noop_loop)
    monkeypatch.setattr(client, "_request_started", _boom_request_started)

    with pytest.raises(cas.CodexAppServerError, match="boom"):
        await client.ensure_started()

    assert client.is_running() is False


@pytest.mark.asyncio
async def test_lifecycle_helpers_call_expected_methods(monkeypatch):
    client = cas.CodexAppServerClient()
    calls: list[tuple[str, dict[str, object], float]] = []

    async def _request(method: str, params: dict[str, object], *, timeout: float = 60.0):
        calls.append((method, params, timeout))
        if method == "thread/fork":
            return {"thread": {"id": "th_forked"}}
        if method == "thread/resume":
            return {"thread": {"id": "th_resumed"}}
        if method == "thread/list":
            return {"threads": [{"id": "th_main"}, {"id": "th_resumed"}]}
        if method == "thread/read":
            return {"thread": {"id": "th_main"}}
        if method == "thread/rollback":
            return {"threadId": "th_main"}
        return {}

    monkeypatch.setattr(client, "request", _request)

    forked = await client.thread_fork(thread_id="th_main", turn_id="turn_1")
    resumed = await client.thread_resume(thread_id="th_resumed")
    listed = await client.thread_list(limit=10)
    read = await client.thread_read(thread_id="th_main")
    rolled = await client.thread_rollback(thread_id="th_main", num_turns=2)

    assert forked["thread"]["id"] == "th_forked"
    assert resumed["thread"]["id"] == "th_resumed"
    assert listed["threads"][0]["id"] == "th_main"
    assert read["thread"]["id"] == "th_main"
    assert rolled["threadId"] == "th_main"
    assert calls[0] == (
        "thread/fork",
        {"threadId": "th_main", "turnId": "turn_1"},
        120.0,
    )
    assert calls[1] == (
        "thread/resume",
        {"threadId": "th_resumed"},
        120.0,
    )
    assert calls[2] == (
        "thread/list",
        {"limit": 10},
        60.0,
    )
    assert calls[3] == (
        "thread/read",
        {"threadId": "th_main"},
        60.0,
    )
    assert calls[4] == (
        "thread/rollback",
        {"threadId": "th_main", "numTurns": 2},
        120.0,
    )


@pytest.mark.asyncio
async def test_write_jsonrpc_uses_jsonl_wire_format():
    client = cas.CodexAppServerClient()
    fake_stdin = _FakeStdin()
    client._proc = SimpleNamespace(
        returncode=None,
        stdin=fake_stdin,
    )

    payload = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "initialize",
        "params": {"clientInfo": {"name": "test"}},
    }
    await client._write_jsonrpc(payload)

    assert len(fake_stdin.writes) == 1
    wire = fake_stdin.writes[0]
    assert wire.endswith(b"\n")
    assert not wire.startswith(b"Content-Length:")
    assert json.loads(wire.decode("utf-8").strip()) == payload


@pytest.mark.asyncio
async def test_notification_handling_does_not_block_reader_loop():
    client = cas.CodexAppServerClient()
    gate = asyncio.Event()

    async def _slow_handler(_method: str, _params: dict[str, object]) -> None:
        await gate.wait()

    await client.set_handlers(notification_handler=_slow_handler)

    # _handle_message should not await the notification handler.
    await asyncio.wait_for(
        client._handle_message(
            {
                "method": "turn/started",
                "params": {"threadId": "th_1", "turn": {"id": "turn_1"}},
            }
        ),
        timeout=0.2,
    )
    assert client.get_active_turn_id("th_1") == "turn_1"

    await client.stop()


@pytest.mark.asyncio
async def test_read_one_message_recovers_from_oversized_line_and_keeps_stream_usable():
    client = cas.CodexAppServerClient()
    fake_stdout = _FakeStdoutOverrun()
    client._proc = SimpleNamespace(
        returncode=None,
        stdout=fake_stdout,
    )

    first = await client._read_one_message()
    second = await client._read_one_message()

    assert first == {}
    assert second == {"jsonrpc": "2.0", "id": "9", "result": {"ok": True}}
    assert fake_stdout.readexactly_calls == [10]
    assert client._transport_needs_restart is True


@pytest.mark.asyncio
async def test_ensure_started_recycles_when_transport_marked_unhealthy(monkeypatch):
    client = cas.CodexAppServerClient()
    client._proc = _FakeProc()
    client._initialized = True
    client._transport_needs_restart = True

    stop_calls: list[str] = []
    spawn_calls: list[str] = []
    events: list[tuple[str, str]] = []

    async def _fake_stop() -> None:
        stop_calls.append("stop")
        client._proc = None
        client._initialized = False
        client._transport_needs_restart = False
        client._reader_task = None
        client._stderr_task = None
        client._notification_task = None

    async def _fake_create_subprocess_exec(*_args, **_kwargs):
        spawn_calls.append("spawn")
        return _FakeProc()

    async def _noop_loop():
        return None

    async def _fake_request_started(method: str, params: dict, *, timeout: float = 60.0):
        _ = timeout
        events.append(("request", method))
        assert method == "initialize"
        assert "clientInfo" in params
        return {"userAgent": "codex/test"}

    async def _fake_write_jsonrpc(payload: dict):
        method = payload.get("method")
        if isinstance(method, str):
            events.append(("notify", method))

    monkeypatch.setattr(client, "stop", _fake_stop)
    monkeypatch.setattr(cas.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(client, "_reader_loop", _noop_loop)
    monkeypatch.setattr(client, "_stderr_loop", _noop_loop)
    monkeypatch.setattr(client, "_request_started", _fake_request_started)
    monkeypatch.setattr(client, "_write_jsonrpc", _fake_write_jsonrpc)

    await client.ensure_started()

    assert stop_calls == ["stop"]
    assert spawn_calls == ["spawn"]
    assert events.count(("request", "initialize")) == 1
    assert events.count(("notify", "initialized")) == 1


@pytest.mark.asyncio
async def test_request_recycles_and_retries_once_on_turn_start_timeout(monkeypatch):
    client = cas.CodexAppServerClient()

    ensure_calls: list[str] = []
    stop_calls: list[str] = []
    request_calls: list[str] = []

    async def _fake_ensure_started() -> None:
        ensure_calls.append("ensure")

    async def _fake_stop() -> None:
        stop_calls.append("stop")

    async def _fake_request_started(method: str, params: dict, *, timeout: float = 60.0):
        _ = params
        request_calls.append(method)
        if len(request_calls) == 1:
            raise cas.CodexAppServerError(
                f"Timed out waiting for app-server response: {method}"
            )
        assert timeout == 90.0
        return {"turn": {"id": "turn-1"}}

    monkeypatch.setattr(client, "ensure_started", _fake_ensure_started)
    monkeypatch.setattr(client, "stop", _fake_stop)
    monkeypatch.setattr(client, "_request_started", _fake_request_started)

    result = await client.request("turn/start", {"threadId": "th_1"}, timeout=90.0)

    assert result == {"turn": {"id": "turn-1"}}
    assert request_calls == ["turn/start", "turn/start"]
    assert stop_calls == ["stop"]
    # One initial start + one restart before retry.
    assert ensure_calls == ["ensure", "ensure"]
