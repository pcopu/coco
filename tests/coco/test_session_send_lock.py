"""Tests for per-window send/steer concurrency guard."""

import asyncio

import pytest

import coco.session as session_mod
from coco.session import SessionManager


@pytest.fixture
def mgr(monkeypatch) -> SessionManager:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    return SessionManager()


@pytest.mark.asyncio
async def test_send_to_window_serializes_same_window(monkeypatch, mgr: SessionManager):
    monkeypatch.setattr(session_mod.config, "runtime_mode", "hybrid")
    monkeypatch.setattr(mgr, "_codex_app_server_mode_enabled", lambda: True)
    mgr.get_window_state("@1").cwd = "/tmp/demo"
    mgr.get_window_state("@1").window_name = "demo"

    active = 0
    max_active = 0

    async def _fake_send_inputs_via_codex_app_server(
        *,
        window_id: str,
        inputs: list[dict[str, object]],
        steer: bool,
        window_name: str,
        cwd: str,
    ):
        _ = window_id, inputs, steer, window_name, cwd
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.03)
        active -= 1
        return True, "ok"

    monkeypatch.setattr(mgr, "_send_inputs_via_codex_app_server", _fake_send_inputs_via_codex_app_server)

    results = await asyncio.gather(
        mgr.send_to_window("@1", "first"),
        mgr.send_to_window("@1", "second"),
    )

    assert max_active == 1
    assert all(ok for ok, _msg in results)


@pytest.mark.asyncio
async def test_send_to_window_allows_parallel_across_windows(monkeypatch, mgr: SessionManager):
    monkeypatch.setattr(session_mod.config, "runtime_mode", "hybrid")
    monkeypatch.setattr(mgr, "_codex_app_server_mode_enabled", lambda: True)
    mgr.get_window_state("@1").cwd = "/tmp/demo"
    mgr.get_window_state("@1").window_name = "demo"
    mgr.get_window_state("@2").cwd = "/tmp/demo"
    mgr.get_window_state("@2").window_name = "demo"

    active = 0
    max_active = 0
    started = 0
    barrier = asyncio.Event()

    async def _fake_send_inputs_via_codex_app_server(
        *,
        window_id: str,
        inputs: list[dict[str, object]],
        steer: bool,
        window_name: str,
        cwd: str,
    ):
        _ = window_id, inputs, steer, window_name, cwd
        nonlocal active, max_active, started
        active += 1
        max_active = max(max_active, active)
        started += 1
        if started >= 2:
            barrier.set()
        await asyncio.wait_for(barrier.wait(), timeout=0.2)
        active -= 1
        return True, "ok"

    monkeypatch.setattr(mgr, "_send_inputs_via_codex_app_server", _fake_send_inputs_via_codex_app_server)

    results = await asyncio.gather(
        mgr.send_to_window("@1", "first"),
        mgr.send_to_window("@2", "second"),
    )

    assert max_active >= 2
    assert all(ok for ok, _msg in results)


@pytest.mark.asyncio
async def test_send_to_window_serializes_app_server_turn_mutations(monkeypatch, mgr: SessionManager):
    monkeypatch.setattr(session_mod.config, "runtime_mode", "hybrid")
    monkeypatch.setattr(mgr, "_codex_app_server_mode_enabled", lambda: True)
    mgr.get_window_state("@1").cwd = "/tmp/demo"
    mgr.get_window_state("@1").window_name = "demo"

    active = 0
    max_active = 0

    async def _fake_send_inputs_via_codex_app_server(
        *,
        window_id: str,
        inputs: list[dict[str, object]],
        steer: bool,
        window_name: str,
        cwd: str,
    ):
        _ = window_id, inputs, steer, window_name, cwd
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.03)
        active -= 1
        return True, "ok"

    monkeypatch.setattr(
        mgr,
        "_send_inputs_via_codex_app_server",
        _fake_send_inputs_via_codex_app_server,
    )

    results = await asyncio.gather(
        mgr.send_to_window("@1", "first", steer=False),
        mgr.send_to_window("@1", "second", steer=True),
    )

    assert max_active == 1
    assert all(ok for ok, _msg in results)
