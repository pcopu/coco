"""Tests for managed local TTS server lifecycle."""

import asyncio
import sys
from pathlib import Path

import pytest

import coco.tts_runtime as tts_runtime


@pytest.mark.asyncio
async def test_ensure_tts_server_started_noops_for_non_local_base_url(monkeypatch):
    monkeypatch.setattr(tts_runtime, "_tts_base_url", lambda: "https://tts.example.com")

    popen_calls: list[object] = []

    def _fake_popen(*args, **kwargs):
        popen_calls.append((args, kwargs))
        raise AssertionError("should not spawn process for non-local base URL")

    monkeypatch.setattr(tts_runtime.subprocess, "Popen", _fake_popen)

    await tts_runtime.ensure_tts_server_started()

    assert popen_calls == []


@pytest.mark.asyncio
async def test_ensure_tts_server_started_spawns_and_waits_until_healthy(monkeypatch):
    monkeypatch.setattr(tts_runtime, "_tts_base_url", lambda: "http://127.0.0.1:7788")
    monkeypatch.setattr(tts_runtime, "_resolve_tts_command", lambda: ["supertonic", "serve"])
    monkeypatch.setattr(tts_runtime, "_tts_binary_exists", lambda: True)
    monkeypatch.setattr(tts_runtime, "_tts_server_start_timeout", lambda: 1.0)
    monkeypatch.setattr(tts_runtime, "_tts_server_poll_interval", lambda: 0.0)

    class _FakeProc:
        def __init__(self) -> None:
            self.returncode = None

        def poll(self):
            return self.returncode

    fake_proc = _FakeProc()
    popen_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    health_checks = {"count": 0}

    def _fake_popen(*args, **kwargs):
        popen_calls.append((args, kwargs))
        return fake_proc

    async def _fake_health() -> bool:
        health_checks["count"] += 1
        return health_checks["count"] >= 2

    monkeypatch.setattr(tts_runtime.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(tts_runtime, "is_tts_server_healthy", _fake_health)
    monkeypatch.setattr(tts_runtime, "_tts_server_process", None)

    await tts_runtime.ensure_tts_server_started()

    assert popen_calls
    assert popen_calls[0][0][0] == ["supertonic", "serve"]
    assert health_checks["count"] >= 2
    assert tts_runtime._tts_server_process is fake_proc


@pytest.mark.asyncio
async def test_ensure_tts_server_started_bootstraps_missing_binary(monkeypatch):
    monkeypatch.setattr(tts_runtime, "_tts_base_url", lambda: "http://127.0.0.1:7788")
    monkeypatch.setattr(tts_runtime, "_resolve_tts_command", lambda: ["supertonic", "serve"])
    monkeypatch.setattr(tts_runtime, "_tts_server_start_timeout", lambda: 1.0)
    monkeypatch.setattr(tts_runtime, "_tts_server_poll_interval", lambda: 0.0)

    class _FakeProc:
        def __init__(self) -> None:
            self.returncode = None

        def poll(self):
            return self.returncode

    fake_proc = _FakeProc()
    popen_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    install_calls: list[str] = []
    health_checks = {"count": 0}
    binary_checks = {"count": 0}

    def _fake_popen(*args, **kwargs):
        popen_calls.append((args, kwargs))
        return fake_proc

    def _fake_binary_exists() -> bool:
        binary_checks["count"] += 1
        return binary_checks["count"] >= 2

    async def _fake_health() -> bool:
        health_checks["count"] += 1
        return health_checks["count"] >= 2

    def _fake_install_sync():
        install_calls.append("install")
        return True, ""

    monkeypatch.setattr(tts_runtime.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(tts_runtime, "_tts_binary_exists", _fake_binary_exists)
    monkeypatch.setattr(tts_runtime, "_install_tts_runtime_sync", _fake_install_sync)
    monkeypatch.setattr(tts_runtime, "is_tts_server_healthy", _fake_health)
    monkeypatch.setattr(tts_runtime, "_tts_server_process", None)

    await tts_runtime.ensure_tts_server_started()

    assert install_calls == ["install"]
    assert popen_calls
    assert health_checks["count"] >= 2


@pytest.mark.asyncio
async def test_ensure_tts_server_started_raises_when_bootstrap_fails(monkeypatch):
    monkeypatch.setattr(tts_runtime, "_tts_base_url", lambda: "http://127.0.0.1:7788")
    monkeypatch.setattr(tts_runtime, "_tts_binary_exists", lambda: False)
    monkeypatch.setattr(tts_runtime, "_install_tts_runtime_sync", lambda: (False, "no network"))
    monkeypatch.setattr(tts_runtime, "is_tts_server_healthy", lambda: asyncio.sleep(0, result=False))

    with pytest.raises(RuntimeError, match="managed TTS bootstrap failed: no network"):
        await tts_runtime.ensure_tts_server_started()


@pytest.mark.asyncio
async def test_ensure_tts_server_started_stops_process_after_health_timeout(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(tts_runtime, "_tts_base_url", lambda: "http://127.0.0.1:7788")
    monkeypatch.setattr(tts_runtime, "_resolve_tts_command", lambda: ["supertonic", "serve"])
    monkeypatch.setattr(tts_runtime, "_tts_binary_exists", lambda: True)
    monkeypatch.setattr(tts_runtime, "_tts_server_start_timeout", lambda: 0.0)
    monkeypatch.setattr(tts_runtime, "is_tts_server_healthy", lambda: asyncio.sleep(0, result=False))

    class _FakeProc:
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            calls.append("terminate")
            self.returncode = 0

        def kill(self):
            calls.append("kill")
            self.returncode = -9

        def wait(self, timeout=None):
            calls.append(f"wait:{timeout}")
            return self.returncode

    monkeypatch.setattr(tts_runtime.subprocess, "Popen", lambda *_args, **_kwargs: _FakeProc())
    monkeypatch.setattr(tts_runtime, "_tts_server_process", None)

    with pytest.raises(RuntimeError, match="did not become healthy"):
        await tts_runtime.ensure_tts_server_started()

    assert calls == ["terminate", "wait:5.0"]
    assert tts_runtime._tts_server_process is None


@pytest.mark.asyncio
async def test_stop_tts_server_terminates_managed_process(monkeypatch):
    calls: list[str] = []

    class _FakeProc:
        def __init__(self) -> None:
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            calls.append("terminate")
            self.returncode = 0

        def kill(self):
            calls.append("kill")
            self.returncode = -9

        def wait(self, timeout=None):
            calls.append(f"wait:{timeout}")
            return 0

    fake_proc = _FakeProc()
    monkeypatch.setattr(tts_runtime, "_tts_server_process", fake_proc)

    await tts_runtime.stop_tts_server()

    assert calls == ["terminate", "wait:5.0"]
    assert tts_runtime._tts_server_process is None


@pytest.mark.asyncio
async def test_stop_tts_server_waits_for_in_progress_start(monkeypatch):
    events: list[str] = []
    spawned = asyncio.Event()
    allow_health = asyncio.Event()

    monkeypatch.setattr(tts_runtime, "_tts_base_url", lambda: "http://127.0.0.1:7788")
    monkeypatch.setattr(tts_runtime, "_resolve_tts_command", lambda: ["supertonic", "serve"])
    monkeypatch.setattr(tts_runtime, "_tts_binary_exists", lambda: True)
    monkeypatch.setattr(tts_runtime, "_tts_server_start_timeout", lambda: 5.0)
    monkeypatch.setattr(tts_runtime, "_tts_server_poll_interval", lambda: 0.0)
    monkeypatch.setattr(tts_runtime, "_tts_server_process", None)

    class _FakeProc:
        def __init__(self) -> None:
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            events.append("terminate")
            self.returncode = 0

        def kill(self):
            events.append("kill")
            self.returncode = -9

        def wait(self, timeout=None):
            events.append(f"wait:{timeout}")
            return 0

    def _fake_popen(*_args, **_kwargs):
        events.append("spawn")
        spawned.set()
        return _FakeProc()

    async def _fake_health() -> bool:
        if not spawned.is_set():
            return False
        await allow_health.wait()
        return True

    async def _run_start() -> None:
        await tts_runtime.ensure_tts_server_started()
        events.append("start_done")

    monkeypatch.setattr(tts_runtime.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(tts_runtime, "is_tts_server_healthy", _fake_health)

    start_task = asyncio.create_task(_run_start())
    await asyncio.wait_for(spawned.wait(), timeout=1.0)
    stop_task = asyncio.create_task(tts_runtime.stop_tts_server())
    await asyncio.sleep(0)

    try:
        assert "terminate" not in events
    finally:
        allow_health.set()
        await asyncio.gather(start_task, stop_task)

    assert events.index("start_done") < events.index("terminate")


@pytest.mark.asyncio
async def test_tts_usage_stops_managed_server_after_idle_timeout(monkeypatch):
    calls: list[str] = []

    class _FakeProc:
        def __init__(self) -> None:
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            calls.append("terminate")
            self.returncode = 0

        def kill(self):
            calls.append("kill")
            self.returncode = -9

        def wait(self, timeout=None):
            calls.append(f"wait:{timeout}")
            return 0

    monkeypatch.setattr(tts_runtime, "_tts_server_process", _FakeProc())
    monkeypatch.setattr(tts_runtime, "_is_local_managed_base_url", lambda: True)
    monkeypatch.setattr(tts_runtime, "_tts_idle_timeout_seconds", lambda: 0.01)

    tts_runtime.begin_tts_server_usage()
    tts_runtime.end_tts_server_usage()
    await asyncio.sleep(0.05)

    assert calls == ["terminate", "wait:5.0"]
    assert tts_runtime._tts_server_process is None


def test_resolve_tts_command_prefers_virtualenv_bin(monkeypatch, tmp_path):
    venv_dir = tmp_path / "venv"
    bin_dir = venv_dir / "bin"
    bin_dir.mkdir(parents=True)
    fake_supertonic = bin_dir / "supertonic"
    fake_supertonic.write_text("#!/bin/sh\n", encoding="utf-8")

    monkeypatch.setenv("VIRTUAL_ENV", str(venv_dir))
    monkeypatch.setattr(tts_runtime, "_tts_base_url", lambda: "http://127.0.0.1:7788")
    monkeypatch.setattr(tts_runtime, "_tts_command_override", lambda: "")
    monkeypatch.setattr(tts_runtime, "_tts_server_model", lambda: "supertonic-3")
    monkeypatch.setattr(tts_runtime, "_tts_server_log_level", lambda: "warning")
    monkeypatch.setattr(tts_runtime.shutil, "which", lambda _name: None)

    command = tts_runtime._resolve_tts_command()

    assert command[0] == str(fake_supertonic)


def test_resolve_tts_install_command_prefers_uv_with_current_environment(monkeypatch, tmp_path):
    venv_dir = tmp_path / "venv"
    bin_dir = venv_dir / "bin"
    bin_dir.mkdir(parents=True)
    fake_python = bin_dir / "python"
    fake_python.write_text("#!/bin/sh\n", encoding="utf-8")

    monkeypatch.setenv("VIRTUAL_ENV", str(venv_dir))
    monkeypatch.setattr(tts_runtime, "_tts_install_command_override", lambda: "")
    monkeypatch.setattr(tts_runtime.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)

    command = tts_runtime._resolve_tts_install_command()

    assert command == [
        "uv",
        "pip",
        "install",
        "--python",
        str(fake_python),
        "supertonic[serve]",
    ]
