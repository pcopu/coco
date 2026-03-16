"""Tests for /restart process re-exec helpers."""

import sys
from types import SimpleNamespace

import pytest

import coco.bot as bot


def test_build_restart_exec_argv_uses_module_when_argv_empty(monkeypatch):
    monkeypatch.setattr(bot.sys, "argv", [])
    args = bot._build_restart_exec_argv()
    assert args == [sys.executable, "-m", "coco.main"]


def test_build_restart_exec_argv_uses_resolved_entrypoint(monkeypatch):
    monkeypatch.setattr(bot.sys, "argv", ["coco", "--flag"])
    monkeypatch.setattr(bot.shutil, "which", lambda _name: "/tmp/coco")
    args = bot._build_restart_exec_argv()
    assert args == [sys.executable, "/tmp/coco", "--flag"]


def test_build_restart_exec_argv_falls_back_to_module_if_unresolved(monkeypatch):
    monkeypatch.setattr(bot.sys, "argv", ["coco", "--debug"])
    monkeypatch.setattr(bot.shutil, "which", lambda _name: None)
    args = bot._build_restart_exec_argv()
    assert args == [sys.executable, "-m", "coco.main", "--debug"]


def test_restart_notice_target_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "_RESTART_NOTICE_FILE", tmp_path / "restart_notice.json")
    monkeypatch.delenv(bot._RESTART_NOTICE_PENDING_ENV, raising=False)
    monkeypatch.delenv(bot._RESTART_NOTICE_CHAT_ENV, raising=False)
    monkeypatch.delenv(bot._RESTART_NOTICE_THREAD_ENV, raising=False)

    bot._set_restart_notice_target(-100123, 77)
    target = bot._pop_restart_notice_target()
    assert target == (-100123, 77)
    assert bot._pop_restart_notice_target() is None


def test_restart_notice_target_without_thread(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "_RESTART_NOTICE_FILE", tmp_path / "restart_notice.json")
    monkeypatch.delenv(bot._RESTART_NOTICE_PENDING_ENV, raising=False)
    monkeypatch.delenv(bot._RESTART_NOTICE_CHAT_ENV, raising=False)
    monkeypatch.delenv(bot._RESTART_NOTICE_THREAD_ENV, raising=False)

    bot._set_restart_notice_target(42, None)
    assert bot._pop_restart_notice_target() == (42, None)


def test_restart_notice_target_sets_coco_envs(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "_RESTART_NOTICE_FILE", tmp_path / "restart_notice.json")
    monkeypatch.delenv(bot._RESTART_NOTICE_PENDING_ENV, raising=False)
    monkeypatch.delenv(bot._RESTART_NOTICE_CHAT_ENV, raising=False)
    monkeypatch.delenv(bot._RESTART_NOTICE_THREAD_ENV, raising=False)

    bot._set_restart_notice_target(-100123, 77)

    assert bot.os.environ[bot._RESTART_NOTICE_PENDING_ENV] == "1"
    assert bot.os.environ[bot._RESTART_NOTICE_CHAT_ENV] == "-100123"
    assert bot.os.environ[bot._RESTART_NOTICE_THREAD_ENV] == "77"


def test_restart_notice_requires_pending_flag(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "_RESTART_NOTICE_FILE", tmp_path / "restart_notice.json")
    monkeypatch.setenv(bot._RESTART_NOTICE_CHAT_ENV, "123")
    monkeypatch.setenv(bot._RESTART_NOTICE_THREAD_ENV, "7")
    monkeypatch.delenv(bot._RESTART_NOTICE_PENDING_ENV, raising=False)
    assert bot._pop_restart_notice_target() is None


def test_pick_restart_back_up_message_is_from_pool():
    msg = bot._pick_restart_back_up_message()
    assert msg in bot.RESTART_BACK_UP_MESSAGES


def test_pick_restart_shutdown_message_is_from_pool():
    msg = bot._pick_restart_shutdown_message()
    assert msg in bot.RESTART_SHUTDOWN_MESSAGES


def test_restart_message_pools_have_100_messages_each():
    assert len(bot.RESTART_BACK_UP_MESSAGES) == 100
    assert len(bot.RESTART_SHUTDOWN_MESSAGES) == 100


@pytest.mark.asyncio
async def test_restart_command_uses_safe_send_and_shutdown_message(monkeypatch):
    sent: list[tuple[int, str, int | None]] = []
    notice_calls: list[tuple[int, int | None]] = []

    chat = SimpleNamespace(type="supergroup", id=-100123)
    message = SimpleNamespace(chat_id=chat.id, message_thread_id=77, chat=chat)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1147817421),
        effective_chat=chat,
        effective_message=message,
        message=message,
    )
    context = SimpleNamespace(bot=object())

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(
        bot,
        "_pick_restart_shutdown_message",
        lambda: "shutdown notice",
    )
    monkeypatch.setattr(
        bot,
        "_set_restart_notice_target",
        lambda chat_id, thread_id: notice_calls.append((chat_id, thread_id)),
    )

    async def _safe_send(_bot, chat_id: int, text: str, message_thread_id: int | None = None, **_kwargs):
        sent.append((chat_id, text, message_thread_id))

    async def _safe_reply(*_args, **_kwargs):  # pragma: no cover - must not be used
        raise AssertionError("restart_command should not use safe_reply")

    monkeypatch.setattr(bot, "safe_send", _safe_send)
    monkeypatch.setattr(bot, "safe_reply", _safe_reply)
    monkeypatch.setattr(
        bot.asyncio,
        "create_task",
        lambda coro: (coro.close(), SimpleNamespace())[1],
    )
    bot._restart_requested = False

    await bot.restart_command(update, context)

    assert notice_calls == [(-100123, 77)]
    assert sent == [(-100123, "shutdown notice", 77)]
    assert bot._restart_requested is True
    bot._restart_requested = False


def test_pop_restart_notice_prefers_file_over_env(monkeypatch, tmp_path):
    notice_file = tmp_path / "restart_notice.json"
    monkeypatch.setattr(bot, "_RESTART_NOTICE_FILE", notice_file)
    notice_file.write_text('{"chat_id": -1001, "thread_id": 88}', encoding="utf-8")
    monkeypatch.setenv(bot._RESTART_NOTICE_PENDING_ENV, "1")
    monkeypatch.setenv(bot._RESTART_NOTICE_CHAT_ENV, "55")
    monkeypatch.setenv(bot._RESTART_NOTICE_THREAD_ENV, "6")

    assert bot._pop_restart_notice_target() == (-1001, 88)
    assert bot._pop_restart_notice_target() is None


def test_startup_notice_targets_uses_restart_target(monkeypatch):
    monkeypatch.setattr(bot, "_get_allowed_admins", lambda: {3, 9})
    assert bot._startup_notice_targets((-100123, 77)) == [(-100123, 77)]


def test_startup_notice_targets_falls_back_to_admin_private_chats(monkeypatch):
    monkeypatch.setattr(bot, "_get_allowed_admins", lambda: {9, 3})
    assert bot._startup_notice_targets(None) == [(3, None), (9, None)]


def test_startup_notice_targets_falls_back_to_allowed_users_when_no_admins(monkeypatch):
    monkeypatch.setattr(bot, "_get_allowed_admins", lambda: set())
    monkeypatch.setattr(bot.config, "allowed_users", {22, 11})
    assert bot._startup_notice_targets(None) == [(11, None), (22, None)]
