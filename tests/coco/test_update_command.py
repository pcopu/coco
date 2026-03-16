"""Tests for /update command and update panel callbacks."""

from types import SimpleNamespace

import pytest
from telegram import InlineKeyboardMarkup

import coco.bot as bot
from coco.handlers.callback_data import CB_UPDATE_REFRESH, CB_UPDATE_RUN


def _make_update(text: str, *, thread_id: int = 77, user_id: int = 1147817421):
    chat = SimpleNamespace(type="supergroup", id=-100123)
    message = SimpleNamespace(
        text=text,
        message_thread_id=thread_id,
        chat=chat,
        chat_id=chat.id,
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_message=message,
        effective_chat=chat,
        message=message,
    )


class _FakeQuery:
    def __init__(self, *, data: str, message) -> None:
        self.data = data
        self.message = message
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False):
        self.answers.append((text, show_alert))


def _make_callback_update(data: str, *, thread_id: int = 77, user_id: int = 1147817421):
    chat = SimpleNamespace(type="supergroup", id=-100123)
    message = SimpleNamespace(message_thread_id=thread_id, chat=chat, chat_id=chat.id)
    query = _FakeQuery(data=data, message=message)
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=chat,
        effective_message=message,
    )
    return update, query


def test_resolve_codex_upgrade_command_prefers_coco_env(monkeypatch):
    monkeypatch.setenv("COCO_CODEX_UPGRADE_COMMAND", "custom coco upgrade")

    command, source = bot._resolve_codex_upgrade_command()

    assert (command, source) == ("custom coco upgrade", "custom")


def test_resolve_codex_upgrade_command_falls_back_to_uv(monkeypatch):
    monkeypatch.delenv("COCO_CODEX_UPGRADE_COMMAND", raising=False)
    monkeypatch.setattr(
        bot.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name == "uv" else None,
    )

    command, source = bot._resolve_codex_upgrade_command()

    assert (command, source) == ("uv tool upgrade codex", "uv")


def test_build_update_panel_text_mentions_coco_upgrade_env_first():
    snapshot = bot._CodexUpdateSnapshot(
        codex_binary="codex",
        current_version="0.1.0",
        latest_version="0.1.1",
        behind=True,
        check_error="",
        upgrade_command="",
        upgrade_source="none",
    )

    text = bot._build_update_panel_text(snapshot, can_trigger_upgrade=True)

    assert "COCO_CODEX_UPGRADE_COMMAND" in text
    assert "Admins can trigger upgrade + restart from this panel." in text


@pytest.mark.asyncio
async def test_update_command_shows_inline_panel(monkeypatch):
    update = _make_update("/update")
    replies: list[tuple[str, object | None]] = []
    keyboard = InlineKeyboardMarkup([])

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: True)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )

    async def _build_update_panel_payload(*, can_trigger_upgrade: bool):
        assert can_trigger_upgrade is True
        return "update panel", keyboard

    monkeypatch.setattr(bot, "_build_update_panel_payload", _build_update_panel_payload)

    async def _safe_reply(_message, text: str, **kwargs):
        replies.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.update_command(update, SimpleNamespace(user_data={}))

    assert replies == [("update panel", keyboard)]


@pytest.mark.asyncio
async def test_update_command_run_requires_admin(monkeypatch):
    update = _make_update("/update run")
    replies: list[str] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: False)

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.update_command(update, SimpleNamespace(bot=object(), user_data={}))

    assert replies
    assert "Only admins can run updates" in replies[-1]


@pytest.mark.asyncio
async def test_update_command_run_triggers_upgrade_flow(monkeypatch):
    update = _make_update("/update run")
    replies: list[str] = []
    run_calls: list[tuple[int, int | None]] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: True)

    async def _run_codex_upgrade_and_restart(*, chat_id: int, thread_id: int | None):
        run_calls.append((chat_id, thread_id))
        return True, "Upgrade complete. Restarting."

    monkeypatch.setattr(bot, "_run_codex_upgrade_and_restart", _run_codex_upgrade_and_restart)

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.update_command(update, SimpleNamespace(bot=object(), user_data={}))

    assert run_calls == [(-100123, 77)]
    assert replies
    assert "Upgrade complete. Restarting." in replies[-1]


@pytest.mark.asyncio
async def test_update_refresh_callback_updates_panel(monkeypatch):
    update, query = _make_callback_update(CB_UPDATE_REFRESH)
    edits: list[tuple[str, object | None]] = []
    keyboard = InlineKeyboardMarkup([])

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: True)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )

    async def _build_update_panel_payload(*, can_trigger_upgrade: bool):
        assert can_trigger_upgrade is True
        return "update panel", keyboard

    monkeypatch.setattr(bot, "_build_update_panel_payload", _build_update_panel_payload)

    async def _safe_edit(_query, text: str, **kwargs):
        edits.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(bot, "safe_edit", _safe_edit)

    await bot.callback_handler(update, SimpleNamespace(user_data={}))

    assert edits == [("update panel", keyboard)]
    assert query.answers
    assert query.answers[-1] == ("Refreshed", False)


@pytest.mark.asyncio
async def test_update_run_callback_executes_upgrade(monkeypatch):
    update, query = _make_callback_update(CB_UPDATE_RUN)
    edits: list[str] = []
    run_calls: list[tuple[int, int | None]] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: True)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )

    async def _run_codex_upgrade_and_restart(*, chat_id: int, thread_id: int | None):
        run_calls.append((chat_id, thread_id))
        return True, "Upgrade complete. Restarting."

    monkeypatch.setattr(bot, "_run_codex_upgrade_and_restart", _run_codex_upgrade_and_restart)

    async def _safe_edit(_query, text: str, **_kwargs):
        edits.append(text)

    monkeypatch.setattr(bot, "safe_edit", _safe_edit)

    await bot.callback_handler(update, SimpleNamespace(user_data={}))

    assert run_calls == [(-100123, 77)]
    assert edits
    assert "Upgrade complete. Restarting." in edits[-1]
    assert query.answers
    assert query.answers[-1] == ("Upgrade queued", False)
