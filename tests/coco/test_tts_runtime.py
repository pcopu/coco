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
