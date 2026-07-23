"""Tests for Codex app-server client transport and handshake."""

import asyncio
import contextlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import coco.codex_app_server as cas


class _FakeProc:
    def __init__(self) -> None:
        self.pid = 4242
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


def test_app_server_argv_enables_goals_feature(monkeypatch):
    client = cas.CodexAppServerClient()

    monkeypatch.setattr(client, "_resolve_codex_binary", lambda: "/usr/bin/codex")
    monkeypatch.setattr(cas.config, "codex_sandbox_mode", "")
    monkeypatch.setattr(client, "_systemd_run_enabled", lambda: False)

    argv = client._app_server_argv()

    assert argv[:4] == ["/usr/bin/codex", "app-server", "--listen", "stdio://"]
    assert "--enable" in argv
    enable_idx = argv.index("--enable")
    assert argv[enable_idx + 1] == "goals"


def test_app_server_argv_can_wrap_in_systemd_run(monkeypatch):
    client = cas.CodexAppServerClient()

    monkeypatch.setattr(client, "_resolve_codex_binary", lambda: "/usr/bin/codex")
    monkeypatch.setattr(cas.config, "codex_sandbox_mode", "danger-full-access")
    monkeypatch.setattr(client, "_systemd_run_enabled", lambda: True)
    monkeypatch.setattr(client, "_new_systemd_unit_name", lambda: "coco-codex-app-server-test")
    monkeypatch.setattr(
        client,
        "_systemd_run_properties",
        lambda: ["MemoryHigh=8G", "MemoryMax=10G"],
    )

    argv = client._app_server_argv()

    assert argv[:6] == [
        "systemd-run",
        "--user",
        "--pipe",
        "--quiet",
        "--collect",
        "--unit=coco-codex-app-server-test",
    ]
    assert "-p" in argv
    assert "MemoryHigh=8G" in argv
    assert argv[-8:] == [
        "/usr/bin/codex",
        "app-server",
        "--listen",
        "stdio://",
        "--enable",
        "goals",
        "-c",
        'sandbox_mode="danger-full-access"',
    ]
    assert client._systemd_unit_name == "coco-codex-app-server-test"


def test_app_server_env_sets_user_bus_for_systemd_run(monkeypatch):
    client = cas.CodexAppServerClient()

    monkeypatch.setattr(client, "_systemd_run_enabled", lambda: True)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.setattr(cas.os, "getuid", lambda: 1000)

    env = client._app_server_env()

    assert env is not None
    assert env["XDG_RUNTIME_DIR"] == "/run/user/1000"
    assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/1000/bus"


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
async def test_stop_stops_systemd_run_unit_before_wrapper(monkeypatch):
    client = cas.CodexAppServerClient()
    events: list[str] = []
    proc = _FakeProc()
    client._proc = proc
    client._systemd_unit_name = "coco-codex-app-server-123"

    async def _fake_stop_unit(unit_name: str) -> None:
        events.append(f"stop-unit:{unit_name}")

    def _terminate() -> None:
        events.append("terminate-wrapper")
        proc.returncode = 0

    proc.terminate = _terminate  # type: ignore[method-assign]
    monkeypatch.setattr(client, "_stop_systemd_unit", _fake_stop_unit)

    await client.stop()

    assert events == [
        "stop-unit:coco-codex-app-server-123",
        "terminate-wrapper",
    ]
    assert client._systemd_unit_name == ""


@pytest.mark.asyncio
async def test_explicit_stop_publishes_one_transport_reset():
    client = cas.CodexAppServerClient()
    client._proc = _FakeProc()
    client._initialized = True
    client._transport_generation = 5
    client._active_turns["thread-live"] = "turn-live"
    reset_calls: list[tuple[str, int]] = []

    async def _transport_reset_handler(
        reason: str,
        generation: int,
    ) -> None:
        reset_calls.append((reason, generation))

    await client.set_handlers(
        transport_reset_handler=_transport_reset_handler,
    )

    await client.stop()
    snapshot_after_first_stop = client.transport_state_snapshot()
    await client.stop()

    assert reset_calls == [("explicit_stop", 5)]
    assert snapshot_after_first_stop["reset_sequence"] == 1
    assert snapshot_after_first_stop["last_reset_generation"] == 5
    assert snapshot_after_first_stop["last_reset_reason"] == "explicit_stop"
    assert client.transport_state_snapshot()["reset_sequence"] == 1
    assert client.get_active_turn_id("thread-live") is None


@pytest.mark.asyncio
async def test_stopping_never_started_transport_does_not_publish_reset():
    client = cas.CodexAppServerClient()
    reset_calls: list[tuple[str, int]] = []

    async def _transport_reset_handler(
        reason: str,
        generation: int,
    ) -> None:
        reset_calls.append((reason, generation))

    await client.set_handlers(
        transport_reset_handler=_transport_reset_handler,
    )

    await client.stop()

    assert reset_calls == []
    assert client.transport_state_snapshot()["reset_sequence"] == 0


def test_reap_stale_processes_stops_stale_systemd_app_server_units(monkeypatch):
    client = cas.CodexAppServerClient()
    events: list[tuple[str, tuple[str, ...]]] = []

    monkeypatch.setattr(client, "_systemd_run_enabled", lambda: True)
    client._systemd_unit_name = "coco-codex-app-server-current.service"

    def _fake_run(argv, **kwargs):
        events.append(("run", tuple(argv)))
        if argv[:3] == ["systemctl", "--user", "list-units"]:
            return SimpleNamespace(
                stdout=(
                    "coco-codex-app-server-current.service loaded active running current\n"
                    "coco-codex-app-server-stale.service loaded active running stale\n"
                ),
                returncode=0,
            )
        if argv[:3] == ["systemctl", "--user", "stop"]:
            return SimpleNamespace(stdout="", returncode=0)
        if argv[:2] == ["ps", "-eo"]:
            return SimpleNamespace(stdout="")
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(cas.subprocess, "run", _fake_run)

    client._reap_stale_local_app_server_processes()

    assert ("run", ("systemctl", "--user", "stop", "coco-codex-app-server-stale.service")) in events
    assert ("run", ("systemctl", "--user", "stop", "coco-codex-app-server-current.service")) not in events


@pytest.mark.asyncio
async def test_ensure_started_reaps_stale_processes_after_repeated_initialize_failures(
    monkeypatch, tmp_path
):
    client = cas.CodexAppServerClient()
    attempts = 0
    reaps: list[str] = []
    failure_file = tmp_path / "failures.json"
    failure_file.write_text(
        '{"count": 2, "first_failure_ts": 500, "last_failure_ts": 900}',
        encoding="utf-8",
    )

    async def _fake_create_subprocess_exec(*_args, **_kwargs):
        return _FakeProc()

    async def _noop_loop():
        return None

    async def _fake_request_started(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            raise cas.CodexAppServerError("failed to initialize sqlite state runtime: database is locked")
        return {"userAgent": "codex/test"}

    async def _fake_write_jsonrpc(_payload: dict):
        return None

    async def _fake_sleep(_seconds: float):
        return None

    monkeypatch.setattr(cas.time, "time", lambda: 1000.0)
    monkeypatch.setattr(cas, "_APP_SERVER_START_FAILURE_FILE", failure_file)
    monkeypatch.setattr(cas.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(cas.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(client, "_reader_loop", _noop_loop)
    monkeypatch.setattr(client, "_stderr_loop", _noop_loop)
    monkeypatch.setattr(client, "_request_started", _fake_request_started)
    monkeypatch.setattr(client, "_write_jsonrpc", _fake_write_jsonrpc)
    monkeypatch.setattr(
        client,
        "_reap_stale_local_app_server_processes",
        lambda: reaps.append("reap"),
    )

    await client.ensure_started()

    assert attempts == 2
    assert reaps == ["reap"]
    assert client.get_server_user_agent() == "codex/test"
    assert not failure_file.exists()


def test_recover_oversized_codex_logs_db_quarantines_only_logs_files(monkeypatch, tmp_path):
    monkeypatch.setenv("COCO_CODEX_APP_SERVER_RECOVER_LOGS_DB", "true")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    logs_db = codex_home / "logs_2.sqlite"
    logs_wal = codex_home / "logs_2.sqlite-wal"
    logs_shm = codex_home / "logs_2.sqlite-shm"
    state_db = codex_home / "state_5.sqlite"
    logs_db.write_bytes(b"database-bytes")
    logs_wal.write_bytes(b"wal-bytes")
    logs_shm.write_bytes(b"shm")
    state_db.write_bytes(b"state")

    monkeypatch.setattr(cas, "_CODEX_LOGS_DB_RECOVERY_MIN_BYTES", 8)
    monkeypatch.setattr(cas, "_CODEX_LOGS_DB_WAL_RECOVERY_MIN_BYTES", 8)

    backup = cas.CodexAppServerClient._recover_oversized_codex_logs_db(codex_home)

    assert backup is not None
    assert backup.parent == codex_home
    assert backup.name.startswith("logs-sqlite-backup-")
    assert not logs_db.exists()
    assert not logs_wal.exists()
    assert not logs_shm.exists()
    assert state_db.read_bytes() == b"state"
    assert (backup / "logs_2.sqlite").read_bytes() == b"database-bytes"
    assert (backup / "logs_2.sqlite-wal").read_bytes() == b"wal-bytes"
    assert (backup / "logs_2.sqlite-shm").read_bytes() == b"shm"


def test_codex_logs_db_recovery_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("COCO_CODEX_APP_SERVER_RECOVER_LOGS_DB", raising=False)

    assert cas.CodexAppServerClient._codex_logs_db_recovery_enabled() is False


@pytest.mark.parametrize("value", ["tru", "enabled", "2", "recover"])
def test_codex_logs_db_recovery_rejects_invalid_opt_in_values(monkeypatch, value):
    monkeypatch.setenv("COCO_CODEX_APP_SERVER_RECOVER_LOGS_DB", value)

    assert cas.CodexAppServerClient._codex_logs_db_recovery_enabled() is False


def test_codex_logs_db_recovery_rolls_back_partial_move(monkeypatch, tmp_path):
    monkeypatch.setenv("COCO_CODEX_APP_SERVER_RECOVER_LOGS_DB", "true")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    logs_db = codex_home / "logs_2.sqlite"
    logs_wal = codex_home / "logs_2.sqlite-wal"
    logs_shm = codex_home / "logs_2.sqlite-shm"
    logs_db.write_bytes(b"database-bytes")
    logs_wal.write_bytes(b"wal-bytes")
    logs_shm.write_bytes(b"shm")
    monkeypatch.setattr(cas, "_CODEX_LOGS_DB_RECOVERY_MIN_BYTES", 8)
    monkeypatch.setattr(cas, "_CODEX_LOGS_DB_WAL_RECOVERY_MIN_BYTES", 8)
    original_replace = Path.replace

    def _replace(path: Path, target: Path):
        if path.name.endswith("-wal"):
            raise OSError("simulated WAL move failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", _replace)

    with pytest.raises(OSError, match="simulated WAL move failure"):
        cas.CodexAppServerClient._recover_oversized_codex_logs_db(codex_home)

    assert logs_db.read_bytes() == b"database-bytes"
    assert logs_wal.read_bytes() == b"wal-bytes"
    assert logs_shm.read_bytes() == b"shm"
    assert list(codex_home.glob("logs-sqlite-backup-*")) == []


def test_recover_oversized_codex_logs_db_leaves_small_logs_in_place(monkeypatch, tmp_path):
    monkeypatch.setenv("COCO_CODEX_APP_SERVER_RECOVER_LOGS_DB", "true")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    logs_db = codex_home / "logs_2.sqlite"
    logs_wal = codex_home / "logs_2.sqlite-wal"
    logs_db.write_bytes(b"db")
    logs_wal.write_bytes(b"wal")

    monkeypatch.setattr(cas, "_CODEX_LOGS_DB_RECOVERY_MIN_BYTES", 8)
    monkeypatch.setattr(cas, "_CODEX_LOGS_DB_WAL_RECOVERY_MIN_BYTES", 8)

    backup = cas.CodexAppServerClient._recover_oversized_codex_logs_db(codex_home)

    assert backup is None
    assert logs_db.read_bytes() == b"db"
    assert logs_wal.read_bytes() == b"wal"
    assert list(codex_home.glob("logs-sqlite-backup-*")) == []


@pytest.mark.asyncio
async def test_attempt_recovery_quarantines_oversized_logs_db_after_repeated_failures(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("COCO_CODEX_APP_SERVER_RECOVER_LOGS_DB", "true")
    client = cas.CodexAppServerClient()
    events: list[object] = []
    backup = tmp_path / "backup"

    async def _fake_sleep(seconds: float):
        events.append(("sleep", seconds))

    monkeypatch.setattr(
        client,
        "_record_start_failure",
        lambda: cas._APP_SERVER_START_FAILURE_RESET_THRESHOLD,
    )
    monkeypatch.setattr(client, "_clear_start_failure_state", lambda: events.append("clear"))
    monkeypatch.setattr(
        client,
        "_reap_stale_local_app_server_processes",
        lambda: events.append("reap"),
    )
    monkeypatch.setattr(
        client,
        "_recover_oversized_codex_logs_db",
        lambda: events.append("recover") or backup,
    )
    async def _wait_for_quiet():
        events.append("wait-for-exit")
        return True

    monkeypatch.setattr(client, "_wait_for_local_app_servers_to_exit", _wait_for_quiet)
    monkeypatch.setattr(cas.asyncio, "sleep", _fake_sleep)

    recovered = await client._attempt_recovery_after_start_failure(
        cas.CodexAppServerError("Timed out waiting for app-server response: initialize")
    )

    assert recovered is True
    assert events == ["clear", "reap", "wait-for-exit", "recover", ("sleep", 0.5)]


@pytest.mark.asyncio
async def test_attempt_recovery_skips_logs_quarantine_while_app_server_is_live(monkeypatch):
    monkeypatch.setenv("COCO_CODEX_APP_SERVER_RECOVER_LOGS_DB", "true")
    client = cas.CodexAppServerClient()
    events: list[object] = []

    monkeypatch.setattr(
        client,
        "_record_start_failure",
        lambda: cas._APP_SERVER_START_FAILURE_RESET_THRESHOLD,
    )
    monkeypatch.setattr(client, "_clear_start_failure_state", lambda: events.append("clear"))
    monkeypatch.setattr(
        client,
        "_reap_stale_local_app_server_processes",
        lambda: events.append("reap"),
    )

    async def _wait_for_quiet():
        events.append("wait-for-exit")
        return False

    async def _fake_sleep(seconds: float):
        events.append(("sleep", seconds))

    monkeypatch.setattr(client, "_wait_for_local_app_servers_to_exit", _wait_for_quiet)
    monkeypatch.setattr(
        client,
        "_recover_oversized_codex_logs_db",
        lambda: events.append("recover"),
    )
    monkeypatch.setattr(cas.asyncio, "sleep", _fake_sleep)

    recovered = await client._attempt_recovery_after_start_failure(
        cas.CodexAppServerError("database is locked")
    )

    assert recovered is True
    assert events == ["clear", "reap", "wait-for-exit", ("sleep", 0.5)]


@pytest.mark.asyncio
async def test_successful_ensure_started_clears_persisted_failure_state(monkeypatch, tmp_path):
    client = cas.CodexAppServerClient()
    failure_file = tmp_path / "failures.json"
    failure_file.write_text('{"count": 2, "first_failure_ts": 1, "last_failure_ts": 2}', encoding="utf-8")

    async def _fake_create_subprocess_exec(*_args, **_kwargs):
        return _FakeProc()

    async def _noop_loop():
        return None

    async def _fake_request_started(method: str, params: dict, *, timeout: float = 60.0):
        _ = timeout
        assert method == "initialize"
        assert "clientInfo" in params
        return {"userAgent": "codex/test"}

    async def _fake_write_jsonrpc(_payload: dict):
        return None

    monkeypatch.setattr(cas, "_APP_SERVER_START_FAILURE_FILE", failure_file)
    monkeypatch.setattr(cas.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(client, "_reader_loop", _noop_loop)
    monkeypatch.setattr(client, "_stderr_loop", _noop_loop)
    monkeypatch.setattr(client, "_request_started", _fake_request_started)
    monkeypatch.setattr(client, "_write_jsonrpc", _fake_write_jsonrpc)

    await client.ensure_started()

    assert not failure_file.exists()


def test_reap_stale_local_app_server_processes_only_kills_owned_pids(monkeypatch, tmp_path):
    client = cas.CodexAppServerClient()
    owned_pids_dir = tmp_path / "owned-pids"
    owned_pids_dir.mkdir()
    (owned_pids_dir / "101.pid").write_text("", encoding="utf-8")
    (owned_pids_dir / "303.pid").write_text("", encoding="utf-8")
    killed: list[int] = []

    proc_result = SimpleNamespace(
        stdout=(
            "101 1 codex app-server --listen stdio://\n"
            "202 999 codex app-server --listen stdio://\n"
            "303 1 python other.py\n"
        )
    )

    monkeypatch.setattr(cas, "_APP_SERVER_OWNED_PIDS_DIR", owned_pids_dir)
    monkeypatch.setattr(
        cas.subprocess,
        "run",
        lambda *_args, **_kwargs: proc_result,
    )
    monkeypatch.setattr(cas.os, "kill", lambda pid, _sig: killed.append(pid))

    client._reap_stale_local_app_server_processes()

    assert killed == [101]
    assert sorted(path.name for path in owned_pids_dir.iterdir()) == ["101.pid"]


def test_running_local_app_server_pids_detects_npm_node_launcher(monkeypatch):
    client = cas.CodexAppServerClient()
    proc_result = SimpleNamespace(
        returncode=0,
        stdout=(
            "101 node /home/coco/.nvm/versions/node/v24/lib/node_modules/"
            "@openai/codex/bin/codex.js app-server --listen stdio://\n"
            "202 python unrelated.py\n"
        ),
    )
    monkeypatch.setattr(cas.subprocess, "run", lambda *_args, **_kwargs: proc_result)

    assert client._running_local_app_server_pids() == {101}


def test_reap_stale_local_app_server_processes_skips_pid_reuse_marker(monkeypatch, tmp_path):
    client = cas.CodexAppServerClient()
    owned_pids_dir = tmp_path / "owned-pids"
    owned_pids_dir.mkdir()
    (owned_pids_dir / "101.pid").write_text("old-start", encoding="utf-8")
    killed: list[int] = []

    proc_result = SimpleNamespace(stdout="101 1 codex app-server --listen stdio://\n")

    monkeypatch.setattr(cas, "_APP_SERVER_OWNED_PIDS_DIR", owned_pids_dir)
    monkeypatch.setattr(
        cas.subprocess,
        "run",
        lambda *_args, **_kwargs: proc_result,
    )
    monkeypatch.setattr(
        client,
        "_read_process_identity",
        lambda pid: "new-start" if pid == 101 else "",
    )
    monkeypatch.setattr(cas.os, "kill", lambda pid, _sig: killed.append(pid))

    client._reap_stale_local_app_server_processes()

    assert killed == []
    assert list(owned_pids_dir.iterdir()) == []


def test_reap_stale_local_app_server_processes_falls_back_to_orphaned_unmarked_pids(
    monkeypatch, tmp_path
):
    client = cas.CodexAppServerClient()
    owned_pids_dir = tmp_path / "owned-pids"
    killed: list[int] = []

    proc_result = SimpleNamespace(
        stdout=(
            "101 1 codex app-server --listen stdio://\n"
            "202 999 codex app-server --listen stdio://\n"
        )
    )

    monkeypatch.setattr(cas, "_APP_SERVER_OWNED_PIDS_DIR", owned_pids_dir)
    monkeypatch.setattr(
        cas.subprocess,
        "run",
        lambda *_args, **_kwargs: proc_result,
    )
    monkeypatch.setattr(cas.os, "kill", lambda pid, _sig: killed.append(pid))

    client._reap_stale_local_app_server_processes()

    assert killed == [101]


def test_reap_stale_local_app_server_processes_skips_live_owned_pid_and_unowned_orphan(
    monkeypatch, tmp_path
):
    client = cas.CodexAppServerClient()
    owned_pids_dir = tmp_path / "owned-pids"
    owned_pids_dir.mkdir()
    (owned_pids_dir / "303.pid").write_text(
        json.dumps({"identity": "", "owner_pid": 777}),
        encoding="utf-8",
    )
    killed: list[int] = []

    proc_result = SimpleNamespace(
        stdout=(
            "101 1 codex app-server --listen stdio://\n"
            "303 777 codex app-server --listen stdio://\n"
        )
    )

    monkeypatch.setattr(cas, "_APP_SERVER_OWNED_PIDS_DIR", owned_pids_dir)
    monkeypatch.setattr(
        cas.subprocess,
        "run",
        lambda *_args, **_kwargs: proc_result,
    )
    monkeypatch.setattr(cas.os, "kill", lambda pid, _sig: killed.append(pid))

    client._reap_stale_local_app_server_processes()

    assert killed == []
    assert sorted(path.name for path in owned_pids_dir.iterdir()) == ["303.pid"]


def test_reap_stale_local_app_server_processes_reaps_current_runtime_owned_child(
    monkeypatch, tmp_path
):
    client = cas.CodexAppServerClient()
    owned_pids_dir = tmp_path / "owned-pids"
    owned_pids_dir.mkdir()
    (owned_pids_dir / "101.pid").write_text(
        json.dumps({"identity": "", "owner_pid": 555}),
        encoding="utf-8",
    )
    killed: list[int] = []

    proc_result = SimpleNamespace(stdout="101 555 codex app-server --listen stdio://\n")

    monkeypatch.setattr(cas, "_APP_SERVER_OWNED_PIDS_DIR", owned_pids_dir)
    monkeypatch.setattr(
        cas.subprocess,
        "run",
        lambda *_args, **_kwargs: proc_result,
    )
    monkeypatch.setattr(cas.os, "kill", lambda pid, _sig: killed.append(pid))
    monkeypatch.setattr(cas.os, "getpid", lambda: 555)

    client._reap_stale_local_app_server_processes()

    assert killed == [101]


def test_reap_stale_local_app_server_processes_reaps_orphan_with_current_runtime_marker(
    monkeypatch, tmp_path
):
    client = cas.CodexAppServerClient()
    client._proc = SimpleNamespace(pid=303)
    owned_pids_dir = tmp_path / "owned-pids"
    owned_pids_dir.mkdir()
    (owned_pids_dir / "303.pid").write_text(
        json.dumps({"identity": "", "owner_pid": 555}),
        encoding="utf-8",
    )
    killed: list[int] = []

    proc_result = SimpleNamespace(
        stdout=(
            "101 1 codex app-server --listen stdio://\n"
            "303 555 codex app-server --listen stdio://\n"
        )
    )

    monkeypatch.setattr(cas, "_APP_SERVER_OWNED_PIDS_DIR", owned_pids_dir)
    monkeypatch.setattr(
        cas.subprocess,
        "run",
        lambda *_args, **_kwargs: proc_result,
    )
    monkeypatch.setattr(cas.os, "kill", lambda pid, _sig: killed.append(pid))
    monkeypatch.setattr(cas.os, "getpid", lambda: 555)

    client._reap_stale_local_app_server_processes()

    assert killed == [101]


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
        if method == "thread/goal/get":
            return {"goal": {"objective": "Ship the feature", "status": "active"}}
        if method == "thread/goal/set":
            return {"goal": {"objective": "Ship the feature", "status": "active"}}
        if method == "thread/goal/clear":
            return {"cleared": True}
        return {}

    monkeypatch.setattr(client, "request", _request)

    forked = await client.thread_fork(thread_id="th_main", turn_id="turn_1", service_tier="fast")
    resumed = await client.thread_resume(thread_id="th_resumed", service_tier="flex")
    listed = await client.thread_list(limit=10)
    read = await client.thread_read(thread_id="th_main")
    rolled = await client.thread_rollback(thread_id="th_main", num_turns=2)
    goal = await client.thread_goal_get(thread_id="th_main")
    updated_goal = await client.thread_goal_set(
        thread_id="th_main",
        goal="Ship the feature",
    )
    cleared_goal = await client.thread_goal_clear(thread_id="th_main")

    assert forked["thread"]["id"] == "th_forked"
    assert resumed["thread"]["id"] == "th_resumed"
    assert listed["threads"][0]["id"] == "th_main"
    assert read["thread"]["id"] == "th_main"
    assert rolled["threadId"] == "th_main"
    assert goal["goal"]["objective"] == "Ship the feature"
    assert updated_goal["goal"]["status"] == "active"
    assert cleared_goal["cleared"] is True
    assert calls[0] == (
        "thread/fork",
        {"threadId": "th_main", "turnId": "turn_1", "serviceTier": "fast"},
        120.0,
    )
    assert calls[1] == (
        "thread/resume",
        {
            "threadId": "th_resumed",
            "excludeTurns": True,
            "serviceTier": "flex",
        },
        30.0,
    )
    assert calls[2] == (
        "thread/list",
        {"limit": 10},
        60.0,
    )
    assert calls[3] == (
        "thread/read",
        {"threadId": "th_main", "includeTurns": True},
        60.0,
    )
    assert calls[4] == (
        "thread/rollback",
        {"threadId": "th_main", "numTurns": 2},
        120.0,
    )
    assert calls[5] == (
        "thread/goal/get",
        {"threadId": "th_main"},
        30.0,
    )
    assert calls[6] == (
        "thread/goal/set",
        {"threadId": "th_main", "objective": "Ship the feature"},
        60.0,
    )
    assert calls[7] == (
        "thread/goal/clear",
        {"threadId": "th_main"},
        30.0,
    )


@pytest.mark.asyncio
async def test_turn_start_includes_model_and_effort_overrides(monkeypatch):
    client = cas.CodexAppServerClient()
    calls: list[tuple[str, dict[str, object], float]] = []

    async def _request(method: str, params: dict[str, object], *, timeout: float = 60.0):
        calls.append((method, params, timeout))
        return {"turn": {"id": "turn-1"}}

    monkeypatch.setattr(client, "request", _request)

    result = await client.turn_start(
        thread_id="thread-1",
        inputs=[{"type": "text", "text": "continue"}],
        model="gpt-5.6-sol",
        effort="ultra",
    )

    assert result == {"turn": {"id": "turn-1"}}
    assert calls == [
        (
            "turn/start",
            {
                "threadId": "thread-1",
                "input": [{"type": "text", "text": "continue"}],
                "model": "gpt-5.6-sol",
                "effort": "ultra",
            },
            cas.APP_SERVER_MUTATION_TIMEOUT_SECONDS,
        )
    ]


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
async def test_request_waiting_for_start_does_not_dispatch_after_stop(
    monkeypatch,
):
    client = cas.CodexAppServerClient()
    ensure_entered = asyncio.Event()
    allow_start = asyncio.Event()

    class _RespondingStdin(_FakeStdin):
        async def drain(self) -> None:
            payload = json.loads(self.writes[-1])
            await client._handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"turn": {"id": "must-not-run"}},
                }
            )

    fake_stdin = _RespondingStdin()

    async def _delayed_ensure_started() -> None:
        ensure_entered.set()
        await allow_start.wait()
        client._proc = SimpleNamespace(
            returncode=None,
            stdin=fake_stdin,
        )
        client._initialized = True
        client._transport_generation += 1

    monkeypatch.setattr(client, "ensure_started", _delayed_ensure_started)

    request_task = asyncio.create_task(
        client.request(
            "turn/start",
            {"threadId": "thread-before-stop"},
        )
    )
    await asyncio.wait_for(ensure_entered.wait(), timeout=0.2)
    await client.stop()
    allow_start.set()

    with pytest.raises(cas.CodexAppServerError, match="transport changed"):
        await asyncio.wait_for(request_task, timeout=0.2)

    assert fake_stdin.writes == []


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
async def test_notification_carries_the_generation_captured_from_stdout():
    client = cas.CodexAppServerClient()
    client._transport_generation = 3
    delivered = asyncio.Event()
    observed: dict[str, object] = {}

    async def _handler(_method: str, params: dict[str, object]) -> None:
        observed.update(params)
        delivered.set()

    await client.set_handlers(notification_handler=_handler)

    await client._handle_message(
        {
            "method": "turn/started",
            "params": {"threadId": "th_1", "turn": {"id": "turn_1"}},
        }
    )
    await asyncio.wait_for(delivered.wait(), timeout=0.2)

    transport = observed[cas.INTERNAL_TRANSPORT_CONTEXT_KEY]
    assert isinstance(transport, dict)
    assert transport["epoch"] == client._transport_epoch
    assert transport["generation"] == 3

    await client.stop()


@pytest.mark.asyncio
async def test_notification_delivery_drops_an_old_transport_generation():
    client = cas.CodexAppServerClient()
    client._transport_generation = 4
    delivered = asyncio.Event()

    async def _handler(_method: str, _params: dict[str, object]) -> None:
        delivered.set()

    await client.set_handlers(notification_handler=_handler)
    client._ensure_notification_worker()
    client._enqueue_notification(
        "turn/completed",
        {
            "threadId": "th_1",
            cas.INTERNAL_TRANSPORT_CONTEXT_KEY: {
                "epoch": client._transport_epoch,
                "generation": 3,
            },
        },
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert delivered.is_set() is False

    await client.stop()


@pytest.mark.asyncio
async def test_slow_server_request_handler_does_not_block_unrelated_response(
    monkeypatch,
):
    client = cas.CodexAppServerClient()
    handler_gate = asyncio.Event()
    response_written = asyncio.Event()

    async def _slow_request_handler(
        _method: str,
        _params: dict[str, object],
    ) -> dict[str, object]:
        await handler_gate.wait()
        return {"decision": "decline"}

    async def _write_response(
        _request_id: object,
        *,
        result: dict[str, object] | None = None,
    ) -> None:
        _ = result
        response_written.set()

    await client.set_handlers(server_request_handler=_slow_request_handler)
    monkeypatch.setattr(client, "_write_response", _write_response)

    loop = asyncio.get_running_loop()
    unrelated_response: asyncio.Future[object] = loop.create_future()
    client._pending["unrelated"] = unrelated_response

    try:
        await asyncio.wait_for(
            client._handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": "approval",
                    "method": "item/commandExecution/requestApproval",
                    "params": {"threadId": "thread-slow"},
                }
            ),
            timeout=0.2,
        )
        await client._handle_message(
            {
                "jsonrpc": "2.0",
                "id": "unrelated",
                "result": {"ok": True},
            }
        )

        assert await asyncio.wait_for(unrelated_response, timeout=0.2) == {"ok": True}

        handler_gate.set()
        await asyncio.wait_for(response_written.wait(), timeout=0.2)
    finally:
        handler_gate.set()
        if not unrelated_response.done():
            unrelated_response.cancel()
        await client.stop()


@pytest.mark.asyncio
async def test_slow_notification_does_not_block_a_different_thread():
    client = cas.CodexAppServerClient()
    slow_gate = asyncio.Event()
    fast_delivered = asyncio.Event()

    async def _handler(_method: str, params: dict[str, object]) -> None:
        if params.get("threadId") == "th-slow":
            await slow_gate.wait()
        else:
            fast_delivered.set()

    await client.set_handlers(notification_handler=_handler)
    client._ensure_notification_worker()
    client._enqueue_notification("item/completed", {"threadId": "th-slow"})
    client._enqueue_notification("turn/started", {"threadId": "th-fast"})

    try:
        await asyncio.wait_for(fast_delivered.wait(), timeout=0.2)
    finally:
        slow_gate.set()
        await client.stop()


@pytest.mark.asyncio
async def test_same_thread_backlog_does_not_consume_other_delivery_slots(monkeypatch):
    monkeypatch.setattr(cas, "APP_SERVER_NOTIFICATION_QUEUE_MAXSIZE", 2)
    client = cas.CodexAppServerClient()
    slow_gate = asyncio.Event()
    fast_delivered = asyncio.Event()

    async def _handler(_method: str, params: dict[str, object]) -> None:
        if params.get("threadId") == "th-slow":
            await slow_gate.wait()
        else:
            fast_delivered.set()

    await client.set_handlers(notification_handler=_handler)
    client._ensure_notification_worker()
    client._enqueue_notification("item/completed", {"threadId": "th-slow", "index": 1})
    client._enqueue_notification("item/completed", {"threadId": "th-slow", "index": 2})
    client._enqueue_notification("turn/started", {"threadId": "th-fast"})

    try:
        await asyncio.wait_for(fast_delivered.wait(), timeout=0.2)
    finally:
        slow_gate.set()
        await client.stop()


@pytest.mark.asyncio
async def test_same_thread_notification_backlog_remains_bounded(monkeypatch):
    monkeypatch.setattr(cas, "APP_SERVER_NOTIFICATION_QUEUE_MAXSIZE", 2)
    client = cas.CodexAppServerClient()
    client._notification_queue = asyncio.Queue(maxsize=20)
    gate = asyncio.Event()

    async def _handler(_method: str, _params: dict[str, object]) -> None:
        await gate.wait()

    await client.set_handlers(notification_handler=_handler)
    client._ensure_notification_worker()
    for index in range(10):
        client._enqueue_notification(
            "thread/tokenUsage/updated",
            {"threadId": "th-slow", "index": index},
        )

    try:
        for _ in range(10):
            await asyncio.sleep(0)
        backlog = client._notification_partition_backlog.get("th-slow")
        assert backlog is not None
        assert len(backlog) <= 2
    finally:
        gate.set()
        await client.stop()


@pytest.mark.asyncio
async def test_notification_partition_backlogs_share_one_global_bound(monkeypatch):
    monkeypatch.setattr(cas, "APP_SERVER_NOTIFICATION_QUEUE_MAXSIZE", 2)
    client = cas.CodexAppServerClient()

    for partition in ("th-1", "th-2", "th-3"):
        for index in range(2):
            await client._append_notification_partition_backlog(
                partition,
                "thread/tokenUsage/updated",
                {"threadId": partition, "index": index},
            )

    assert sum(map(len, client._notification_partition_backlog.values())) <= 2
    assert client._notification_partition_backlog_size <= 2


@pytest.mark.asyncio
async def test_full_partition_backlog_preserves_critical_notifications(monkeypatch):
    monkeypatch.setattr(cas, "APP_SERVER_NOTIFICATION_QUEUE_MAXSIZE", 2)
    client = cas.CodexAppServerClient()
    client._notification_queue = asyncio.Queue(maxsize=20)
    permits: asyncio.Queue[None] = asyncio.Queue()
    delivered: list[int] = []

    async def _handler(_method: str, params: dict[str, object]) -> None:
        delivered.append(int(params["index"]))
        await permits.get()

    await client.set_handlers(notification_handler=_handler)
    client._ensure_notification_worker()
    for index in range(4):
        client._enqueue_notification(
            "turn/completed",
            {"threadId": "th-slow", "index": index},
        )

    try:
        for _ in range(20):
            await asyncio.sleep(0)
        for expected_count in range(2, 5):
            permits.put_nowait(None)
            for _ in range(50):
                if len(delivered) >= expected_count:
                    break
                await asyncio.sleep(0)
        assert delivered == [0, 1, 2, 3]
    finally:
        for _ in range(4):
            permits.put_nowait(None)
        await client.stop()


@pytest.mark.asyncio
async def test_notification_delivery_tasks_remain_bounded(monkeypatch):
    monkeypatch.setattr(cas, "APP_SERVER_NOTIFICATION_QUEUE_MAXSIZE", 2)
    client = cas.CodexAppServerClient()
    client._notification_queue = asyncio.Queue(maxsize=10)
    gate = asyncio.Event()

    async def _handler(_method: str, _params: dict[str, object]) -> None:
        await gate.wait()

    await client.set_handlers(notification_handler=_handler)
    client._ensure_notification_worker()
    for idx in range(4):
        client._enqueue_notification(
            "item/completed",
            {"threadId": f"th-{idx}"},
        )

    try:
        for _ in range(5):
            await asyncio.sleep(0)
        assert len(client._notification_delivery_tasks) == 2
        assert client._notification_queue.qsize() >= 1
    finally:
        gate.set()
        await client.stop()


def test_notification_queue_drops_snapshot_when_full():
    client = cas.CodexAppServerClient()
    client._notification_queue = asyncio.Queue(maxsize=1)
    client._notification_queue.put_nowait(
        ("account/rateLimits/updated", {"rateLimits": {"remaining": 1}})
    )

    accepted = client._enqueue_notification(
        "thread/tokenUsage/updated",
        {"threadId": "th_1", "tokenUsage": {"totalTokens": 9}},
    )

    assert accepted is False
    assert client._notification_queue.get_nowait() == (
        "account/rateLimits/updated",
        {"rateLimits": {"remaining": 1}},
    )


def test_notification_queue_evicts_snapshot_for_turn_lifecycle_notification():
    client = cas.CodexAppServerClient()
    client._notification_queue = asyncio.Queue(maxsize=1)
    client._notification_queue.put_nowait(
        ("thread/tokenUsage/updated", {"threadId": "th_1", "tokenUsage": {"totalTokens": 9}})
    )

    accepted = client._enqueue_notification(
        "turn/started",
        {"threadId": "th_1", "turn": {"id": "turn_1"}},
    )

    assert accepted is True
    assert client._notification_queue.get_nowait() == (
        "turn/started",
        {"threadId": "th_1", "turn": {"id": "turn_1"}},
    )


def test_notification_queue_spills_turn_lifecycle_when_only_critical_items_remain():
    client = cas.CodexAppServerClient()
    client._notification_queue = asyncio.Queue(maxsize=1)
    client._notification_queue.put_nowait(
        ("turn/completed", {"threadId": "th_0", "turn": {"status": "completed"}})
    )

    accepted = client._enqueue_notification(
        "turn/started",
        {"threadId": "th_1", "turn": {"id": "turn_1"}},
    )

    assert accepted is True
    assert client._notification_queue.get_nowait() == (
        "turn/completed",
        {"threadId": "th_0", "turn": {"status": "completed"}},
    )
    assert list(client._notification_overflow) == [
        ("turn/started", {"threadId": "th_1", "turn": {"id": "turn_1"}})
    ]


@pytest.mark.asyncio
async def test_notification_loop_preserves_fifo_when_overflow_contains_newer_item():
    client = cas.CodexAppServerClient()
    delivered: list[tuple[str, dict[str, object]]] = []

    async def _handler(method: str, params: dict[str, object]) -> None:
        delivered.append((method, params))

    await client.set_handlers(notification_handler=_handler)
    client._notification_queue = asyncio.Queue(maxsize=1)
    client._notification_queue.put_nowait(
        ("turn/completed", {"threadId": "th_0", "turn": {"status": "completed"}})
    )
    client._notification_overflow.append(
        ("turn/started", {"threadId": "th_1", "turn": {"id": "turn_1"}})
    )

    task = asyncio.create_task(client._notification_loop())
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert delivered == [
        ("turn/completed", {"threadId": "th_0", "turn": {"status": "completed"}}),
        ("turn/started", {"threadId": "th_1", "turn": {"id": "turn_1"}}),
    ]


def test_notification_queue_appends_new_critical_items_behind_existing_overflow():
    client = cas.CodexAppServerClient()
    client._notification_queue = asyncio.Queue(maxsize=2)
    client._notification_queue.put_nowait(
        ("turn/completed", {"threadId": "th_0", "turn": {"status": "completed"}})
    )
    client._notification_overflow.append(
        ("turn/started", {"threadId": "th_1", "turn": {"id": "turn_1"}})
    )

    accepted = client._enqueue_notification(
        "turn/completed",
        {"threadId": "th_1", "turn": {"status": "completed"}},
    )

    assert accepted is True
    assert list(client._notification_overflow) == [
        ("turn/started", {"threadId": "th_1", "turn": {"id": "turn_1"}}),
        ("turn/completed", {"threadId": "th_1", "turn": {"status": "completed"}}),
    ]


def test_notification_overflow_keeps_latest_when_bounded(monkeypatch):
    monkeypatch.setattr(cas, "APP_SERVER_NOTIFICATION_OVERFLOW_MAXSIZE", 2)
    client = cas.CodexAppServerClient()
    client._notification_queue = asyncio.Queue(maxsize=1)
    client._notification_queue.put_nowait(
        ("turn/completed", {"threadId": "th_0", "turn": {"status": "completed"}})
    )

    for idx in range(3):
        accepted = client._enqueue_notification(
            "turn/started",
            {"threadId": f"th_{idx + 1}", "turn": {"id": f"turn_{idx + 1}"}},
        )
        assert accepted is True

    assert list(client._notification_overflow) == [
        ("turn/started", {"threadId": "th_2", "turn": {"id": "turn_2"}}),
        ("turn/started", {"threadId": "th_3", "turn": {"id": "turn_3"}}),
    ]


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

    monkeypatch.setattr(client, "_stop_locked", _fake_stop)
    monkeypatch.setattr(
        cas.asyncio,
        "create_subprocess_exec",
        _fake_create_subprocess_exec,
    )
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
async def test_request_recycles_and_retries_once_for_safe_read_timeout(monkeypatch):
    client = cas.CodexAppServerClient()

    ensure_calls: list[str] = []
    stop_calls: list[str] = []
    request_calls: list[str] = []

    async def _fake_ensure_started() -> None:
        ensure_calls.append("ensure")
        client._proc = _FakeProc()
        client._initialized = True

    async def _fake_stop() -> None:
        stop_calls.append("stop")

    async def _fake_request_started(
        method: str,
        params: dict,
        *,
        timeout: float = 60.0,
        expected_stop_sequence: int | None = None,
    ):
        _ = params
        _ = expected_stop_sequence
        request_calls.append(method)
        if len(request_calls) == 1:
            raise cas.CodexAppServerError(
                f"Timed out waiting for app-server response: {method}"
            )
        assert timeout == 90.0
        return {"turn": {"id": "turn-1"}}

    monkeypatch.setattr(client, "ensure_started", _fake_ensure_started)
    monkeypatch.setattr(client, "_ensure_started_locked", _fake_ensure_started)
    monkeypatch.setattr(client, "_stop_locked", _fake_stop)
    monkeypatch.setattr(client, "_request_started", _fake_request_started)

    result = await client.request("account/rateLimits/read", {}, timeout=90.0)

    assert result == {"turn": {"id": "turn-1"}}
    assert request_calls == [
        "account/rateLimits/read",
        "account/rateLimits/read",
    ]
    assert stop_calls == ["stop"]
    # One initial start + one restart before retry.
    assert ensure_calls == ["ensure", "ensure"]


@pytest.mark.asyncio
async def test_request_timeout_does_not_recycle_when_retry_disabled(monkeypatch):
    client = cas.CodexAppServerClient()
    client._transport_generation = 7
    client._transport_reset_sequence = 3
    client._active_turns["thread-live"] = "turn-live"
    recycle_calls: list[dict[str, object]] = []

    async def _ensure_started() -> None:
        return None

    async def _request_started(
        method: str,
        _params: dict[str, object],
        *,
        timeout: float = 60.0,
        expected_stop_sequence: int | None = None,
    ) -> object:
        _ = timeout
        _ = expected_stop_sequence
        raise cas.CodexAppServerError(
            f"Timed out waiting for app-server response: {method}"
        )

    async def _recycle_timed_out_transport(**kwargs: object) -> bool:
        recycle_calls.append(kwargs)
        client._transport_generation += 1
        client._transport_reset_sequence += 1
        client._active_turns.clear()
        return True

    monkeypatch.setattr(client, "ensure_started", _ensure_started)
    monkeypatch.setattr(client, "_request_started", _request_started)
    monkeypatch.setattr(
        client,
        "_recycle_timed_out_transport",
        _recycle_timed_out_transport,
    )

    with pytest.raises(
        cas.CodexAppServerError,
        match="Timed out waiting for app-server response: account/rateLimits/read",
    ):
        await client.request(
            "account/rateLimits/read",
            {},
            timeout=0.01,
            retry_safe_timeout=False,
        )

    assert recycle_calls == []
    assert client._transport_generation == 7
    assert client._transport_reset_sequence == 3
    assert client.get_active_turn_id("thread-live") == "turn-live"


@pytest.mark.asyncio
async def test_timeout_recycles_without_replaying_mutating_request(monkeypatch):
    client = cas.CodexAppServerClient()

    ensure_calls: list[str] = []
    stop_calls: list[str] = []
    request_calls: list[str] = []

    async def _fake_ensure_started() -> None:
        ensure_calls.append("ensure")

    async def _fake_stop() -> None:
        stop_calls.append("stop")

    async def _fake_request_started(
        method: str,
        _params: dict[str, object],
        *,
        timeout: float = 60.0,
        expected_stop_sequence: int | None = None,
    ) -> object:
        _ = timeout
        _ = expected_stop_sequence
        request_calls.append(method)
        raise cas.CodexAppServerError(
            f"Timed out waiting for app-server response: {method}"
        )

    monkeypatch.setattr(client, "ensure_started", _fake_ensure_started)
    monkeypatch.setattr(client, "_ensure_started_locked", _fake_ensure_started)
    monkeypatch.setattr(client, "_stop_locked", _fake_stop)
    monkeypatch.setattr(client, "_request_started", _fake_request_started)

    with pytest.raises(
        cas.CodexAppServerError,
        match="Timed out waiting for app-server response: turn/steer",
    ):
        await client.request(
            "turn/steer",
            {
                "threadId": "thread-1",
                "expectedTurnId": "turn-stale",
                "input": [{"type": "text", "text": "continue"}],
            },
            timeout=0.01,
        )

    assert request_calls == ["turn/steer"]
    assert stop_calls == ["stop"]
    assert ensure_calls == ["ensure", "ensure"]


@pytest.mark.asyncio
async def test_success_result_is_rejected_after_concurrent_timeout_recycles_transport(
    monkeypatch,
):
    client = cas.CodexAppServerClient()
    client._proc = _FakeProc()
    client._initialized = True
    client._transport_generation = 7
    client._transport_reset_sequence = 3
    loop = asyncio.get_running_loop()
    successful_response: asyncio.Future[object] = loop.create_future()
    successful_request_waiting = asyncio.Event()
    timeout_request_waiting = asyncio.Event()
    successful_response_resolved = asyncio.Event()
    allow_timeout = asyncio.Event()
    recycled = asyncio.Event()
    request_calls: list[str] = []

    async def _request_started(
        method: str,
        params: dict[str, object],
        *,
        timeout: float = 60.0,
        expected_stop_sequence: int | None = None,
    ) -> object:
        _ = timeout
        _ = expected_stop_sequence
        thread_id = str(params["threadId"])
        request_calls.append(thread_id)
        if thread_id == "thread-success":
            successful_request_waiting.set()
            result = await successful_response
            successful_response_resolved.set()
            await recycled.wait()
            return result
        timeout_request_waiting.set()
        await allow_timeout.wait()
        raise cas.CodexAppServerError(
            f"Timed out waiting for app-server response: {method}"
        )

    async def _recycle_timed_out_transport(**_kwargs) -> bool:
        client._transport_generation = 8
        client._transport_reset_sequence = 4
        client._proc = _FakeProc()
        client._initialized = True
        recycled.set()
        return True

    monkeypatch.setattr(client, "_request_started", _request_started)
    monkeypatch.setattr(
        client,
        "_recycle_timed_out_transport",
        _recycle_timed_out_transport,
    )

    successful_task = asyncio.create_task(
        client.request("turn/start", {"threadId": "thread-success"})
    )
    timeout_task = asyncio.create_task(
        client.request("turn/start", {"threadId": "thread-timeout"})
    )
    await asyncio.wait_for(successful_request_waiting.wait(), timeout=0.2)
    await asyncio.wait_for(timeout_request_waiting.wait(), timeout=0.2)

    successful_response.set_result({"turn": {"id": "turn-stale"}})
    await asyncio.wait_for(successful_response_resolved.wait(), timeout=0.2)
    allow_timeout.set()

    successful_result, timeout_result = await asyncio.gather(
        successful_task,
        timeout_task,
        return_exceptions=True,
    )

    assert isinstance(successful_result, cas.CodexAppServerError)
    assert "transport changed" in str(successful_result)
    assert isinstance(timeout_result, cas.CodexAppServerError)
    assert request_calls == ["thread-success", "thread-timeout"]


@pytest.mark.asyncio
async def test_concurrent_timeouts_share_one_generation_reset(monkeypatch):
    client = cas.CodexAppServerClient()
    spawn_calls: list[str] = []
    reset_calls: list[tuple[object, ...]] = []
    request_calls: list[str] = []
    timeout_barrier = asyncio.Event()

    async def _transport_reset_handler(*args: object) -> None:
        reset_calls.append(args)

    try:
        await client.set_handlers(
            transport_reset_handler=_transport_reset_handler,
        )
    except TypeError:
        pytest.fail(
            "set_handlers must accept a transport_reset_handler for generation resets"
        )

    async def _fake_create_subprocess_exec(*_args, **_kwargs):
        spawn_calls.append("spawn")
        return _FakeProc()

    async def _noop_loop() -> None:
        return None

    async def _fake_request_started(
        method: str,
        _params: dict[str, object],
        *,
        timeout: float = 60.0,
        expected_stop_sequence: int | None = None,
    ) -> object:
        _ = timeout
        _ = expected_stop_sequence
        if method == "initialize":
            return {"userAgent": "codex/test"}
        request_calls.append(method)
        if len(request_calls) >= 2:
            timeout_barrier.set()
        await timeout_barrier.wait()
        raise cas.CodexAppServerError(
            f"Timed out waiting for app-server response: {method}"
        )

    async def _fake_write_jsonrpc(_payload: dict[str, object]) -> None:
        return None

    monkeypatch.setattr(cas.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(client, "_app_server_argv", lambda: ["codex", "app-server"])
    monkeypatch.setattr(client, "_reader_loop", _noop_loop)
    monkeypatch.setattr(client, "_stderr_loop", _noop_loop)
    monkeypatch.setattr(client, "_request_started", _fake_request_started)
    monkeypatch.setattr(client, "_write_jsonrpc", _fake_write_jsonrpc)
    monkeypatch.setattr(client, "_remember_owned_pid", lambda _pid: None)
    monkeypatch.setattr(client, "_forget_owned_pid", lambda _pid: None)
    monkeypatch.setattr(client, "_clear_start_failure_state", lambda: None)

    await client.ensure_started()
    reset_calls.clear()

    try:
        errors = await asyncio.gather(
            client.request("turn/start", {"threadId": "thread-1"}, timeout=0.01),
            client.request("turn/start", {"threadId": "thread-2"}, timeout=0.01),
            return_exceptions=True,
        )

        assert all(isinstance(error, cas.CodexAppServerError) for error in errors)
        assert request_calls == ["turn/start", "turn/start"]
        assert spawn_calls == ["spawn", "spawn"]
        assert len(reset_calls) == 1
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_explicit_stop_fences_queued_timeout_recyclers(monkeypatch):
    client = cas.CodexAppServerClient()
    client._proc = _FakeProc()
    client._initialized = True
    client._transport_generation = 1
    both_requests_timed_out = asyncio.Event()
    first_recycle_stop_started = asyncio.Event()
    allow_first_recycle_stop = asyncio.Event()
    request_calls: list[str] = []
    stop_calls: list[str] = []
    ensure_calls: list[str] = []

    async def _request_started(
        method: str,
        _params: dict[str, object],
        *,
        timeout: float = 60.0,
        expected_stop_sequence: int | None = None,
    ) -> object:
        _ = timeout
        _ = expected_stop_sequence
        request_calls.append(method)
        if len(request_calls) == 2:
            both_requests_timed_out.set()
        await both_requests_timed_out.wait()
        raise cas.CodexAppServerError(
            f"Timed out waiting for app-server response: {method}"
        )

    async def _stop_locked() -> None:
        stop_calls.append("stop")
        client._proc = None
        client._initialized = False
        if len(stop_calls) == 1:
            first_recycle_stop_started.set()
            await allow_first_recycle_stop.wait()

    async def _ensure_started_locked() -> None:
        ensure_calls.append("ensure")
        client._proc = _FakeProc()
        client._initialized = True
        client._transport_generation += 1

    monkeypatch.setattr(client, "_request_started", _request_started)
    monkeypatch.setattr(client, "_stop_locked", _stop_locked)
    monkeypatch.setattr(client, "_ensure_started_locked", _ensure_started_locked)
    monkeypatch.setattr(client, "_notify_transport_reset", lambda *_args: asyncio.sleep(0))

    request_tasks = [
        asyncio.create_task(
            client.request("turn/start", {"threadId": f"thread-{index}"})
        )
        for index in range(2)
    ]
    await asyncio.wait_for(first_recycle_stop_started.wait(), timeout=0.2)

    stop_task = asyncio.create_task(client.stop())
    await asyncio.sleep(0)
    allow_first_recycle_stop.set()

    errors = await asyncio.gather(*request_tasks, return_exceptions=True)
    await stop_task

    assert all(isinstance(error, cas.CodexAppServerError) for error in errors)
    assert stop_calls == ["stop", "stop"]
    assert ensure_calls == []
    assert client.is_running() is False


@pytest.mark.asyncio
async def test_recycle_blocks_ordinary_start_until_cleanup_finishes(monkeypatch):
    client = cas.CodexAppServerClient()
    client._proc = _FakeProc()
    client._initialized = True
    client._transport_generation = 1
    stop_started = asyncio.Event()
    allow_stop_to_finish = asyncio.Event()
    ensure_calls: list[str] = []

    async def _stop_locked() -> None:
        client._proc = None
        stop_started.set()
        await allow_stop_to_finish.wait()
        client._initialized = False

    async def _ensure_started_locked() -> None:
        ensure_calls.append("ensure")
        client._proc = _FakeProc()
        client._initialized = True
        client._transport_generation += 1

    monkeypatch.setattr(client, "_stop_locked", _stop_locked)
    monkeypatch.setattr(client, "_ensure_started_locked", _ensure_started_locked)
    monkeypatch.setattr(client, "_notify_transport_reset", lambda *_args: asyncio.sleep(0))

    recycle_task = asyncio.create_task(
        client._recycle_timed_out_transport(
            expected_generation=1,
            method="turn/start",
        )
    )
    await asyncio.wait_for(stop_started.wait(), timeout=0.2)
    ordinary_start = asyncio.create_task(client.ensure_started())
    await asyncio.sleep(0)

    assert ensure_calls == []

    allow_stop_to_finish.set()
    await asyncio.gather(recycle_task, ordinary_start)
    assert ensure_calls


@pytest.mark.asyncio
async def test_health_probe_reraises_non_recycling_protocol_error(monkeypatch):
    client = cas.CodexAppServerClient()
    client._transport_generation = 3

    async def _ensure_started() -> None:
        return None

    async def _request(*_args, **_kwargs):
        raise cas.CodexAppServerError("authentication required")

    monkeypatch.setattr(client, "ensure_started", _ensure_started)
    monkeypatch.setattr(client, "request", _request)

    with pytest.raises(cas.CodexAppServerError, match="authentication required"):
        await client.probe_health(timeout=1.0)


@pytest.mark.asyncio
async def test_health_probe_timeout_is_inconclusive_without_recycling(monkeypatch):
    client = cas.CodexAppServerClient()
    client._transport_generation = 4
    client._transport_reset_sequence = 2
    client._active_turns["thread-live"] = "turn-live"
    recycle_calls: list[dict[str, object]] = []

    async def _ensure_started() -> None:
        return None

    async def _request_started(
        method: str,
        _params: dict[str, object],
        *,
        timeout: float = 60.0,
        expected_stop_sequence: int | None = None,
    ) -> object:
        _ = timeout
        _ = expected_stop_sequence
        raise cas.CodexAppServerError(
            f"Timed out waiting for app-server response: {method}"
        )

    async def _recycle_timed_out_transport(**kwargs: object) -> bool:
        recycle_calls.append(kwargs)
        return True

    monkeypatch.setattr(client, "ensure_started", _ensure_started)
    monkeypatch.setattr(client, "_request_started", _request_started)
    monkeypatch.setattr(
        client,
        "_recycle_timed_out_transport",
        _recycle_timed_out_transport,
    )

    with pytest.raises(
        cas.CodexAppServerError,
        match="Timed out waiting for app-server response: account/rateLimits/read",
    ):
        await client.probe_health(timeout=0.01)

    assert recycle_calls == []
    assert client._transport_generation == 4
    assert client._transport_reset_sequence == 2
    assert client.get_active_turn_id("thread-live") == "turn-live"


@pytest.mark.asyncio
async def test_ensure_started_resets_state_after_abrupt_process_exit(monkeypatch):
    client = cas.CodexAppServerClient()
    exited_proc = _FakeProc()
    exited_proc.returncode = 1
    client._proc = exited_proc
    client._initialized = True
    client._transport_generation = 4
    client._active_turns["thread-old"] = "turn-old"
    reset_calls: list[tuple[str, int]] = []
    spawn_calls: list[str] = []

    async def _reset_handler(reason: str, generation: int) -> None:
        reset_calls.append((reason, generation))

    async def _fake_create_subprocess_exec(*_args, **_kwargs):
        spawn_calls.append("spawn")
        return _FakeProc()

    async def _noop_loop() -> None:
        return None

    async def _fake_request_started(
        method: str,
        _params: dict[str, object],
        *,
        timeout: float = 60.0,
    ) -> object:
        _ = timeout
        assert method == "initialize"
        return {"userAgent": "codex/test"}

    async def _fake_write_jsonrpc(_payload: dict[str, object]) -> None:
        return None

    await client.set_handlers(transport_reset_handler=_reset_handler)
    monkeypatch.setattr(
        cas.asyncio,
        "create_subprocess_exec",
        _fake_create_subprocess_exec,
    )
    monkeypatch.setattr(client, "_reader_loop", _noop_loop)
    monkeypatch.setattr(client, "_stderr_loop", _noop_loop)
    monkeypatch.setattr(client, "_request_started", _fake_request_started)
    monkeypatch.setattr(client, "_write_jsonrpc", _fake_write_jsonrpc)
    monkeypatch.setattr(client, "_remember_owned_pid", lambda _pid: None)
    monkeypatch.setattr(client, "_forget_owned_pid", lambda _pid: None)
    monkeypatch.setattr(client, "_clear_start_failure_state", lambda: None)

    await client.ensure_started()

    assert spawn_calls == ["spawn"]
    assert reset_calls == [("process_exited", 4)]
    assert client.get_active_turn_id("thread-old") is None
    await client.stop()


@pytest.mark.asyncio
async def test_health_probe_detects_recovery_during_ensure_started(monkeypatch):
    client = cas.CodexAppServerClient()
    client._transport_generation = 8
    client._transport_reset_sequence = 2

    async def _ensure_started() -> None:
        client._transport_generation = 9
        client._transport_reset_sequence = 3

    async def _request(*_args, **_kwargs):
        return {"rateLimits": {}}

    monkeypatch.setattr(client, "ensure_started", _ensure_started)
    monkeypatch.setattr(client, "request", _request)

    assert await client.probe_health(timeout=1.0) is False


@pytest.mark.asyncio
async def test_health_probe_does_not_report_cold_start_as_recovery(monkeypatch):
    client = cas.CodexAppServerClient()

    async def _ensure_started() -> None:
        client._transport_generation = 1

    async def _request(*_args, **_kwargs):
        return {"rateLimits": {}}

    monkeypatch.setattr(client, "ensure_started", _ensure_started)
    monkeypatch.setattr(client, "request", _request)

    assert await client.probe_health(timeout=1.0) is True


@pytest.mark.asyncio
async def test_stop_does_not_cancel_or_await_current_notification_task():
    client = cas.CodexAppServerClient()
    completed = asyncio.Event()

    async def _stop_from_delivery_task() -> None:
        current = asyncio.current_task()
        assert current is not None
        client._notification_delivery_tasks.add(current)
        await client.stop()
        completed.set()

    task = asyncio.create_task(_stop_from_delivery_task())
    await asyncio.wait_for(completed.wait(), timeout=0.2)
    await task
