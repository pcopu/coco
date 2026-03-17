"""Tests for /update command, self-update checks, and update panel callbacks."""

from types import SimpleNamespace

import pytest
from telegram import InlineKeyboardMarkup

import coco.bot as bot
from coco.handlers.callback_data import (
    CB_UPDATE_REFRESH,
    CB_UPDATE_RUN,
    CB_UPDATE_RUN_BOTH,
    CB_UPDATE_RUN_COCO,
    CB_UPDATE_RUN_CODEX,
)


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


def test_build_update_panel_text_mentions_coco_self_update():
    coco_snapshot = bot._CocoUpdateSnapshot(
        repo_root="/srv/coco",
        current_branch="main",
        upstream_ref="origin/main",
        current_commit="1111111",
        latest_commit="2222222",
        behind_count=1,
        ahead_count=0,
        dirty=False,
        check_error="",
        update_command="git pull --ff-only origin main",
        update_source="git",
    )
    codex_snapshot = bot._CodexUpdateSnapshot(
        codex_binary="codex",
        current_version="0.1.0",
        latest_version="0.1.1",
        behind=True,
        check_error="",
        upgrade_command="uv tool upgrade codex",
        upgrade_source="uv",
    )

    text = bot._build_update_panel_text(
        coco_snapshot,
        codex_snapshot,
        can_trigger_upgrade=True,
    )

    assert "CoCo Update" in text
    assert "Codex Update" in text
    assert "git pull --ff-only origin main" in text
    assert "uv tool upgrade codex" in text
    assert "Admins can apply CoCo, Codex, or both from this panel." in text


@pytest.mark.asyncio
async def test_maybe_send_coco_update_notice_dedupes_by_target_commit(monkeypatch, tmp_path):
    snapshot = bot._CocoUpdateSnapshot(
        repo_root="/srv/coco",
        current_branch="main",
        upstream_ref="origin/main",
        current_commit="1111111",
        latest_commit="2222222",
        behind_count=1,
        ahead_count=0,
        dirty=False,
        check_error="",
        update_command="git pull --ff-only origin main",
        update_source="git",
    )
    sent: list[tuple[int, str, int | None, object | None]] = []

    monkeypatch.setattr(bot, "_UPDATE_NOTICE_STATE_FILE", tmp_path / "update_notice.json")
    monkeypatch.setattr(bot, "_update_notice_targets", lambda: [(3, None)])

    async def _safe_send(_bot, chat_id: int, text: str, message_thread_id: int | None = None, **kwargs):
        sent.append((chat_id, text, message_thread_id, kwargs.get("reply_markup")))

    monkeypatch.setattr(bot, "safe_send", _safe_send)

    await bot._maybe_send_coco_update_notice(object(), snapshot)
    await bot._maybe_send_coco_update_notice(object(), snapshot)

    assert len(sent) == 1
    assert sent[0][0] == 3
    assert "Update available" in sent[0][1]
    assert isinstance(sent[0][3], InlineKeyboardMarkup)


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
async def test_update_command_run_defaults_to_codex_update_flow(monkeypatch):
    update = _make_update("/update run")
    replies: list[str] = []
    run_calls: list[tuple[int, int | None]] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: True)

    async def _run_codex_upgrade_and_restart(*, chat_id: int, thread_id: int | None):
        run_calls.append((chat_id, thread_id))
        return True, "Codex updated. Restarting."

    monkeypatch.setattr(bot, "_run_codex_upgrade_and_restart", _run_codex_upgrade_and_restart)

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.update_command(update, SimpleNamespace(bot=object(), user_data={}))

    assert run_calls == [(-100123, 77)]
    assert replies
    assert "Codex updated. Restarting." in replies[-1]


@pytest.mark.asyncio
async def test_update_command_run_coco_triggers_coco_update_flow(monkeypatch):
    update = _make_update("/update run coco")
    replies: list[str] = []
    run_calls: list[tuple[int, int | None]] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: True)

    async def _run_coco_update_and_restart(*, chat_id: int, thread_id: int | None):
        run_calls.append((chat_id, thread_id))
        return True, "CoCo updated. Restarting."

    monkeypatch.setattr(bot, "_run_coco_update_and_restart", _run_coco_update_and_restart)

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.update_command(update, SimpleNamespace(bot=object(), user_data={}))

    assert run_calls == [(-100123, 77)]
    assert replies
    assert "CoCo updated. Restarting." in replies[-1]


@pytest.mark.asyncio
async def test_update_command_run_both_triggers_combined_update_flow(monkeypatch):
    update = _make_update("/update run both")
    replies: list[str] = []
    run_calls: list[tuple[int, int | None]] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: True)

    async def _run_both_updates_and_restart(*, chat_id: int, thread_id: int | None):
        run_calls.append((chat_id, thread_id))
        return True, "CoCo and Codex updated. Restarting."

    monkeypatch.setattr(bot, "_run_both_updates_and_restart", _run_both_updates_and_restart)

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.update_command(update, SimpleNamespace(bot=object(), user_data={}))

    assert run_calls == [(-100123, 77)]
    assert replies
    assert "CoCo and Codex updated. Restarting." in replies[-1]


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
async def test_update_run_callback_executes_coco_update(monkeypatch):
    update, query = _make_callback_update(CB_UPDATE_RUN_COCO)
    edits: list[str] = []
    run_calls: list[tuple[int, int | None]] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: True)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )

    async def _run_coco_update_and_restart(*, chat_id: int, thread_id: int | None):
        run_calls.append((chat_id, thread_id))
        return True, "CoCo updated. Restarting."

    monkeypatch.setattr(bot, "_run_coco_update_and_restart", _run_coco_update_and_restart)

    async def _safe_edit(_query, text: str, **_kwargs):
        edits.append(text)

    monkeypatch.setattr(bot, "safe_edit", _safe_edit)

    await bot.callback_handler(update, SimpleNamespace(user_data={}))

    assert run_calls == [(-100123, 77)]
    assert edits
    assert "CoCo updated. Restarting." in edits[-1]
    assert query.answers
    assert query.answers[-1] == ("Update queued", False)


@pytest.mark.asyncio
async def test_update_run_callback_executes_codex_update(monkeypatch):
    update, query = _make_callback_update(CB_UPDATE_RUN_CODEX)
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
        return True, "Codex updated. Restarting."

    monkeypatch.setattr(bot, "_run_codex_upgrade_and_restart", _run_codex_upgrade_and_restart)

    async def _safe_edit(_query, text: str, **_kwargs):
        edits.append(text)

    monkeypatch.setattr(bot, "safe_edit", _safe_edit)

    await bot.callback_handler(update, SimpleNamespace(user_data={}))

    assert run_calls == [(-100123, 77)]
    assert edits
    assert "Codex updated. Restarting." in edits[-1]
    assert query.answers
    assert query.answers[-1] == ("Update queued", False)


@pytest.mark.asyncio
async def test_update_run_callback_executes_both_updates(monkeypatch):
    update, query = _make_callback_update(CB_UPDATE_RUN_BOTH)
    edits: list[str] = []
    run_calls: list[tuple[int, int | None]] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: True)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )

    async def _run_both_updates_and_restart(*, chat_id: int, thread_id: int | None):
        run_calls.append((chat_id, thread_id))
        return True, "CoCo and Codex updated. Restarting."

    monkeypatch.setattr(bot, "_run_both_updates_and_restart", _run_both_updates_and_restart)

    async def _safe_edit(_query, text: str, **_kwargs):
        edits.append(text)

    monkeypatch.setattr(bot, "safe_edit", _safe_edit)

    await bot.callback_handler(update, SimpleNamespace(user_data={}))

    assert run_calls == [(-100123, 77)]
    assert edits
    assert "CoCo and Codex updated. Restarting." in edits[-1]
    assert query.answers
    assert query.answers[-1] == ("Update queued", False)
