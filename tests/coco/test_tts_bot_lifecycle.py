"""Tests for TTS runtime hooks in bot lifecycle."""

from types import SimpleNamespace

import pytest

import coco.bot as bot


@pytest.mark.asyncio
async def test_post_init_starts_managed_tts_runtime(monkeypatch):
    app = SimpleNamespace(bot=SimpleNamespace())

    started: list[str] = []
    fake_task = None

    class _FakeBot:
        def __init__(self) -> None:
            self.rate_limiter = SimpleNamespace(_base_limiter=None)

        async def delete_my_commands(self):
            return None

        async def set_my_commands(self, commands):
            _ = commands
            return None

    class _FakeMonitor:
        def set_message_callback(self, callback):
            _ = callback

        def start(self):
            return None

        def stop(self):
            return None

    class _FakeControllerRpcServer:
        def __init__(self, *args, **kwargs):
            _ = args, kwargs

        async def start(self, *, host: str, port: int):
            _ = host, port

        async def stop(self):
            return None

        def bound_address(self):
            return ("127.0.0.1", 8787)

    async def _noop(*_args, **_kwargs):
        return None

    async def _fake_start():
        started.append("tts")

    monkeypatch.setattr(bot, "SessionMonitor", lambda: _FakeMonitor())
    monkeypatch.setattr(bot, "_pop_restart_notice_target", lambda: None)
    monkeypatch.setattr(bot, "_startup_notice_targets", lambda _target: [])
    monkeypatch.setattr(bot, "_codex_app_server_preferred", lambda: False)
    monkeypatch.setattr(bot.session_manager, "resolve_stale_ids", _noop)
    monkeypatch.setattr(bot, "status_poll_loop", lambda _bot: _noop())
    monkeypatch.setattr(bot, "ControllerRpcServer", _FakeControllerRpcServer)
    monkeypatch.setattr(bot, "ensure_tts_server_started", _fake_start)

    def _create_task(coro):
        nonlocal fake_task
        fake_task = coro
        coro.close()
        return SimpleNamespace(cancel=lambda: None)

    monkeypatch.setattr(bot.asyncio, "create_task", _create_task)

    await bot.post_init(SimpleNamespace(bot=_FakeBot()))

    assert started == ["tts"]


@pytest.mark.asyncio
async def test_post_shutdown_stops_managed_tts_runtime(monkeypatch):
    stopped: list[str] = []

    async def _fake_stop():
        stopped.append("tts")

    monkeypatch.setattr(bot, "stop_tts_server", _fake_stop)
    monkeypatch.setattr(bot, "_status_poll_task", None)
    monkeypatch.setattr(bot, "_update_check_task", None)
    monkeypatch.setattr(bot, "_controller_rpc_server", None)
    monkeypatch.setattr(bot, "session_monitor", None)
    monkeypatch.setattr(bot, "shutdown_workers", lambda: _fake_done())
    monkeypatch.setattr(bot.codex_app_server_client, "stop", _fake_done)

    await bot.post_shutdown(SimpleNamespace(bot=None))

    assert stopped == ["tts"]


async def _fake_done():
    return None
