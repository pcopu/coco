import contextlib
import asyncio
from types import SimpleNamespace

import pytest

import coco.bot as bot


def test_create_bot_registers_status_but_not_usage_command():
    app = bot.create_bot()
    commands = {
        next(iter(handler.commands))
        for handler in app.handlers.get(0, [])
        if getattr(handler, "commands", None)
    }

    assert "status" in commands
    assert "usage" not in commands


@pytest.mark.asyncio
async def test_post_init_does_not_publish_usage_command(monkeypatch):
    published = []

    class _FakeBot:
        def __init__(self) -> None:
            self.rate_limiter = SimpleNamespace(_base_limiter=None)

        async def delete_my_commands(self):
            return None

        async def set_my_commands(self, commands):
            published.extend(commands)

    app = SimpleNamespace(bot=_FakeBot())

    class _FakeMonitor:
        def __init__(self) -> None:
            self.callback = None
            self.started = False

        def set_message_callback(self, callback):
            self.callback = callback

        def start(self):
            self.started = True

        def stop(self):
            self.started = False

    fake_monitor = _FakeMonitor()
    fake_task = asyncio.create_task(asyncio.sleep(0))

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bot, "SessionMonitor", lambda: fake_monitor)
    monkeypatch.setattr(bot, "_pop_restart_notice_target", lambda: None)
    monkeypatch.setattr(bot, "_startup_notice_targets", lambda _target: [])
    monkeypatch.setattr(bot, "_codex_app_server_preferred", lambda: False)
    monkeypatch.setattr(bot.session_manager, "resolve_stale_ids", _noop)
    monkeypatch.setattr(bot, "status_poll_loop", lambda _bot: asyncio.sleep(0))

    class _FakeControllerRpcServer:
        def __init__(self, *args, **kwargs):
            self.started = False

        async def start(self, *, host: str, port: int):
            _ = host, port
            self.started = True

        async def stop(self):
            self.started = False

        def bound_address(self):
            return ("127.0.0.1", 8787)

    monkeypatch.setattr(bot, "ControllerRpcServer", _FakeControllerRpcServer)

    def _create_task(coro):
        coro.close()
        return fake_task

    monkeypatch.setattr(bot.asyncio, "create_task", _create_task)

    try:
        await bot.post_init(app)
    finally:
        fake_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await fake_task

    published_names = [command.command for command in published]
    assert "status" in published_names
    assert "usage" not in published_names
