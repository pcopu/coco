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
    CB_UPDATE_RUN_NODE,
    CB_UPDATE_ROLL_AGENTS,
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


def test_build_update_panel_text_lists_remote_nodes(monkeypatch):
    monkeypatch.setattr(
        bot.node_registry,
        "iter_nodes",
        lambda: [
            SimpleNamespace(
                machine_id="userver",
                display_name="userver",
                status="online",
                is_local=True,
                rpc_host="100.83.15.19",
                rpc_port=8787,
                agent_version="",
            ),
            SimpleNamespace(
                machine_id="desktop-hsfeb9e",
                display_name="DESKTOP-HSFEB9E",
                status="online",
                is_local=False,
                rpc_host="100.78.23.5",
                rpc_port=8787,
                agent_version="abc1234",
            ),
        ],
    )
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

    assert "Nodes" in text
    assert "DESKTOP-HSFEB9E" in text
    assert "100.78.23.5:8787" in text
    assert "abc1234" in text


def test_build_update_panel_keyboard_includes_remote_node_actions(monkeypatch):
    monkeypatch.setattr(
        bot.node_registry,
        "iter_nodes",
        lambda: [
            SimpleNamespace(
                machine_id="userver",
                display_name="userver",
                status="online",
                is_local=True,
            ),
            SimpleNamespace(
                machine_id="desktop-hsfeb9e",
                display_name="DESKTOP-HSFEB9E",
                status="online",
                is_local=False,
            ),
        ],
    )

    keyboard = bot._build_update_panel_keyboard(can_trigger_upgrade=True)
    callback_data = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data
    ]

    assert f"{CB_UPDATE_RUN_NODE}desktop-hsfeb9e:coco" in callback_data
    assert f"{CB_UPDATE_RUN_NODE}desktop-hsfeb9e:codex" in callback_data
    assert f"{CB_UPDATE_RUN_NODE}desktop-hsfeb9e:both" in callback_data
    assert CB_UPDATE_ROLL_AGENTS in callback_data


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
async def test_maybe_send_codex_update_notice_dedupes_by_latest_version(
    monkeypatch, tmp_path
):
    snapshot = bot._CodexUpdateSnapshot(
        codex_binary="codex",
        current_version="1.0.0",
        latest_version="1.1.0",
        behind=True,
        check_error="",
        upgrade_command="uv tool upgrade codex",
        upgrade_source="uv",
    )
    sent: list[tuple[int, str, int | None, object | None]] = []

    monkeypatch.setattr(bot, "_UPDATE_NOTICE_STATE_FILE", tmp_path / "update_notice.json")
    monkeypatch.setattr(bot, "_update_notice_targets", lambda: [(7, None)])

    async def _safe_send(
        _bot,
        chat_id: int,
        text: str,
        message_thread_id: int | None = None,
        **kwargs,
    ):
        sent.append((chat_id, text, message_thread_id, kwargs.get("reply_markup")))

    monkeypatch.setattr(bot, "safe_send", _safe_send)

    await bot._maybe_send_codex_update_notice(object(), snapshot)
    await bot._maybe_send_codex_update_notice(object(), snapshot)

    assert len(sent) == 1
    assert sent[0][0] == 7
    assert "Codex Update Available" in sent[0][1]
    assert "`1.0.0`" in sent[0][1]
    assert "`1.1.0`" in sent[0][1]
    assert isinstance(sent[0][3], InlineKeyboardMarkup)


@pytest.mark.asyncio
async def test_maybe_send_codex_update_notice_skips_when_not_behind(monkeypatch, tmp_path):
    snapshot = bot._CodexUpdateSnapshot(
        codex_binary="codex",
        current_version="1.1.0",
        latest_version="1.1.0",
        behind=False,
        check_error="",
        upgrade_command="uv tool upgrade codex",
        upgrade_source="uv",
    )
    sent: list[tuple[int, str, int | None, object | None]] = []

    monkeypatch.setattr(bot, "_UPDATE_NOTICE_STATE_FILE", tmp_path / "update_notice.json")
    monkeypatch.setattr(bot, "_update_notice_targets", lambda: [(7, None)])

    async def _safe_send(
        _bot,
        chat_id: int,
        text: str,
        message_thread_id: int | None = None,
        **kwargs,
    ):
        sent.append((chat_id, text, message_thread_id, kwargs.get("reply_markup")))

    monkeypatch.setattr(bot, "safe_send", _safe_send)

    result = await bot._maybe_send_codex_update_notice(object(), snapshot)

    assert result is False
    assert sent == []


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


@pytest.mark.asyncio
async def test_update_run_node_callback_executes_remote_both_update(monkeypatch):
    update, query = _make_callback_update(f"{CB_UPDATE_RUN_NODE}desktop-hsfeb9e:both")
    edits: list[str] = []
    run_calls: list[tuple[str, str, int, int | None]] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: True)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )

    async def _run_remote_node_update_and_restart(
        *,
        machine_id: str,
        action: str,
        chat_id: int,
        thread_id: int | None,
    ):
        run_calls.append((machine_id, action, chat_id, thread_id))
        return True, "Remote node updated. Restarting."

    monkeypatch.setattr(
        bot,
        "_run_remote_node_update_and_restart",
        _run_remote_node_update_and_restart,
    )

    async def _safe_edit(_query, text: str, **_kwargs):
        edits.append(text)

    monkeypatch.setattr(bot, "safe_edit", _safe_edit)

    await bot.callback_handler(update, SimpleNamespace(user_data={}))

    assert run_calls == [("desktop-hsfeb9e", "both", -100123, 77)]
    assert edits
    assert "Remote node updated. Restarting." in edits[-1]
    assert query.answers
    assert query.answers[-1] == ("Update queued", False)


@pytest.mark.asyncio
async def test_update_roll_agents_callback_executes_rolling_update(monkeypatch):
    update, query = _make_callback_update(CB_UPDATE_ROLL_AGENTS)
    edits: list[str] = []
    roll_calls: list[tuple[int, int | None]] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: True)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )

    async def _run_rolling_agent_updates(*, chat_id: int, thread_id: int | None):
        roll_calls.append((chat_id, thread_id))
        return True, "Rolled remote agents."

    monkeypatch.setattr(bot, "_run_rolling_agent_updates", _run_rolling_agent_updates)

    async def _safe_edit(_query, text: str, **_kwargs):
        edits.append(text)

    monkeypatch.setattr(bot, "safe_edit", _safe_edit)

    await bot.callback_handler(update, SimpleNamespace(user_data={}))

    assert roll_calls == [(-100123, 77)]
    assert edits
    assert "Rolled remote agents." in edits[-1]
    assert query.answers
    assert query.answers[-1] == ("Update queued", False)


@pytest.mark.asyncio
async def test_run_remote_node_update_and_restart_calls_agent_rpc_and_verifies(monkeypatch):
    rpc_calls: list[tuple[str, str, int, int | None]] = []
    verify_calls: list[str] = []

    async def _run_update(machine_id: str, *, action: str, notice_chat_id: int, notice_thread_id: int | None):
        rpc_calls.append((machine_id, action, notice_chat_id, notice_thread_id))
        return {"ok": True, "message": "remote updated"}

    async def _wait(machine_id: str, *, timeout_seconds: float = 0.0):
        verify_calls.append(machine_id)
        return True, "online"

    monkeypatch.setattr(bot.agent_rpc_client, "run_update", _run_update)
    monkeypatch.setattr(bot, "_wait_for_remote_node_online", _wait)

    ok, text = await bot._run_remote_node_update_and_restart(
        machine_id="desktop-hsfeb9e",
        action="both",
        chat_id=-100123,
        thread_id=77,
    )

    assert ok is True
    assert rpc_calls == [("desktop-hsfeb9e", "both", -100123, 77)]
    assert verify_calls == ["desktop-hsfeb9e"]
    assert "remote updated" in text
    assert "online" in text


@pytest.mark.asyncio
async def test_run_rolling_agent_updates_runs_online_remote_nodes_only(monkeypatch):
    calls: list[str] = []

    monkeypatch.setattr(
        bot.node_registry,
        "iter_nodes",
        lambda: [
            SimpleNamespace(machine_id="userver", display_name="userver", status="online", is_local=True),
            SimpleNamespace(machine_id="b-node", display_name="B Node", status="offline", is_local=False),
            SimpleNamespace(machine_id="a-node", display_name="A Node", status="online", is_local=False),
            SimpleNamespace(machine_id="c-node", display_name="C Node", status="online", is_local=False),
        ],
    )

    async def _run_remote(*, machine_id: str, action: str, chat_id: int, thread_id: int | None):
        calls.append(machine_id)
        return True, f"{machine_id} ok"

    monkeypatch.setattr(bot, "_run_remote_node_update_and_restart", _run_remote)

    ok, text = await bot._run_rolling_agent_updates(chat_id=-100123, thread_id=77)

    assert ok is True
    assert calls == ["a-node", "c-node"]
    assert "a-node ok" in text
    assert "c-node ok" in text
