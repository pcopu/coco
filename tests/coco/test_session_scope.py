"""Single-session grants must not become machine-wide thread access."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coco import bot
from coco.handlers import commands
from coco.session import SessionManager

CHAT = -100123
TOPIC = 77
GUEST, OWNER, ADMIN, FULL = 2, 3, 4, 5


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    mgr = SessionManager()
    monkeypatch.setattr(bot, "session_manager", mgr)
    monkeypatch.setattr(bot.config, "allowed_users", {GUEST, OWNER, ADMIN, FULL})
    monkeypatch.setattr(bot.config, "session_provider", "codex")
    monkeypatch.setattr(bot, "_get_allowed_admins", lambda: {ADMIN})
    monkeypatch.setattr(
        bot,
        "_get_allowed_scopes",
        lambda: {
            GUEST: bot.SCOPE_SINGLE_SESSION,
            OWNER: bot.SCOPE_SINGLE_SESSION,
            FULL: bot.SCOPE_CREATE_SESSIONS,
        },
    )
    monkeypatch.setattr(bot, "_is_chat_allowed", lambda chat: True)
    monkeypatch.setattr(bot, "_codex_app_server_preferred", lambda: True)
    monkeypatch.setattr(bot, "_codex_app_server_enabled", lambda: True)
    monkeypatch.setattr(bot, "_local_machine_identity", lambda: ("local", "Local"))
    monkeypatch.setattr(mgr, "get_window_machine_id", lambda wid: "local")
    monkeypatch.setattr(bot, "safe_reply", AsyncMock())
    monkeypatch.setattr(bot, "safe_edit", AsyncMock())
    monkeypatch.setattr(bot.codex_app_server_client, "get_active_turn_id", lambda _: "")
    for uid, wid, tid in [
        (GUEST, "guest", "assigned"),
        (OWNER, "owner", "private"),
        (ADMIN, "admin", "admin-thread"),
        (FULL, "full", "full-thread"),
    ]:
        state = mgr.get_window_state(wid)
        state.codex_thread_id = tid
        state.cwd = "/test/workspace"
        mgr.bind_thread(uid, TOPIC, wid, chat_id=CHAT)
    listing = AsyncMock(return_value=(["private", "assigned"], ""))
    monkeypatch.setattr(bot, "_list_all_session_threads", listing)
    calls = {}
    for obj, name in [
        (mgr, "resume_codex_session_for_window"),
        (mgr, "resume_latest_codex_session_for_window"),
        (bot.codex_app_server_client, "thread_fork"),
        (bot.codex_app_server_client, "thread_rollback"),
        (bot.codex_app_server_client, "thread_read"),
        (bot.agent_rpc_client, "resume_thread"),
        (bot.agent_rpc_client, "resume_latest"),
        (bot.agent_rpc_client, "fork_thread"),
    ]:
        mock = AsyncMock(return_value={"thread_id": "assigned"})
        monkeypatch.setattr(obj, name, mock)
        calls[name] = mock
    return mgr, listing, calls


def update(data=None, user=GUEST, text="/resume"):
    chat = SimpleNamespace(type="supergroup", id=CHAT)
    message = SimpleNamespace(
        message_thread_id=TOPIC, chat=chat, chat_id=CHAT, text=text
    )
    query = (
        SimpleNamespace(data=data, message=message, answer=AsyncMock())
        if data
        else None
    )
    return SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=user),
        effective_chat=chat,
        effective_message=message,
        message=message,
    )


@pytest.mark.parametrize(
    "data",
    [
        bot.CB_SESSION_FORK,
        bot.CB_SESSION_RESUME_LATEST,
        bot.CB_SESSION_RESUME + "0",
        bot.CB_SESSION_RESUME + "-1",
    ],
)
@pytest.mark.parametrize("remote", [False, True])
async def test_forged_rebind_cannot_read_or_change_other_user(
    env, data, remote, monkeypatch
):
    mgr, listing, calls = env
    if remote:
        monkeypatch.setattr(mgr, "get_window_machine_id", lambda wid: "remote")
    context = SimpleNamespace(
        user_data={
            bot.SESSION_PICKER_THREADS_KEY: {
                "thread_id": TOPIC,
                "window_id": "guest",
                "items": ["private"],
            }
        },
        bot=None,
    )
    request = update(data)
    await bot.callback_handler(request, context)
    assert request.callback_query.answer.call_args.kwargs["show_alert"]
    assert "Single-session" in request.callback_query.answer.call_args.args[0]
    listing.assert_not_awaited()
    for call in calls.values():
        call.assert_not_awaited()
    assert mgr.get_window_codex_thread_id("guest") == "assigned"
    assert mgr.get_window_codex_thread_id("owner") == "private"
    # The subsequent ordinary history command still resolves the assigned thread.
    await commands.history_command(update(text="/history"), context)
    calls["thread_read"].assert_awaited_once_with(thread_id="assigned")


@pytest.mark.parametrize(
    "data", [None, bot.CB_SESSION_REFRESH, bot.CB_SESSION_PAGE + "1"]
)
async def test_guest_panel_does_not_discover_other_threads(env, data):
    _mgr, listing, _calls = env
    context = SimpleNamespace(
        user_data={bot.SESSION_PICKER_THREADS_KEY: {"items": ["private"]}}, bot=None
    )
    if data:
        await bot.callback_handler(update(data), context)
        response = bot.safe_edit.call_args
    else:
        await commands.resume_command(update(text="/resume private"), context)
        response = bot.safe_reply.call_args
    assert "private" not in response.args[1]
    assert "assigned" in response.args[1]
    buttons = response.kwargs["reply_markup"].inline_keyboard
    assert all(
        not button.callback_data.startswith(bot.CB_SESSION_RESUME)
        and button.callback_data != bot.CB_SESSION_FORK
        for row in buttons
        for button in row
    )
    assert bot.SESSION_PICKER_THREADS_KEY not in context.user_data
    listing.assert_not_awaited()


async def test_guest_can_rollback_assigned_session(env):
    mgr, listing, calls = env
    request = update(bot.CB_SESSION_ROLLBACK + "1")
    await bot.callback_handler(request, SimpleNamespace(user_data={}, bot=None))
    calls["thread_rollback"].assert_awaited_once_with(thread_id="assigned", num_turns=1)
    assert mgr.get_window_codex_thread_id("owner") == "private"
    listing.assert_not_awaited()


@pytest.mark.parametrize("user", [ADMIN, FULL])
async def test_authorized_users_keep_picker_and_resume(env, user):
    mgr, listing, calls = env
    context = SimpleNamespace(user_data={}, bot=None)
    await commands.resume_command(update(user=user), context)
    listing.assert_awaited_once()
    calls["resume_codex_session_for_window"].return_value = "private"
    mgr.sync_window_topic_model_selection_from_codex_session = lambda **_: (
        False,
        "",
        "",
    )
    # Current thread is inserted before discovered threads, so private is index 1.
    await bot.callback_handler(update(bot.CB_SESSION_RESUME + "1", user=user), context)
    calls["resume_codex_session_for_window"].assert_awaited_once()
    wid = "admin" if user == ADMIN else "full"
    assert mgr.get_window_codex_thread_id(wid) == "private"


async def test_scope_revoked_during_discovery_blocks_selected_target(env, monkeypatch):
    mgr, listing, calls = env

    async def revoke(**kwargs):
        monkeypatch.setattr(bot, "_get_allowed_scopes", dict)
        return ["private"], ""

    listing.side_effect = revoke
    await bot.callback_handler(
        update(bot.CB_SESSION_RESUME + "1", user=FULL),
        SimpleNamespace(user_data={}, bot=None),
    )
    calls["resume_codex_session_for_window"].assert_not_awaited()
    assert mgr.get_window_codex_thread_id("full") == "full-thread"


def test_target_guard_rejects_other_owner_or_stale_binding(env):
    assert not bot._can_user_rebind_session(
        caller_user_id=FULL,
        owner_user_id=OWNER,
        thread_id=TOPIC,
        chat_id=CHAT,
        window_id="owner",
    )
    assert not bot._can_user_rebind_session(
        caller_user_id=ADMIN,
        owner_user_id=ADMIN,
        thread_id=TOPIC,
        chat_id=CHAT,
        window_id="owner",
    )


@pytest.mark.parametrize("user", [ADMIN, FULL])
@pytest.mark.parametrize(
    "data,method",
    [
        (bot.CB_SESSION_FORK, "thread_fork"),
        (bot.CB_SESSION_RESUME_LATEST, "resume_latest_codex_session_for_window"),
    ],
)
async def test_authorized_users_keep_fork_and_latest(env, user, data, method):
    _mgr, _listing, calls = env
    if method == "resume_latest_codex_session_for_window":
        calls[method].return_value = "assigned"
    request = update(data, user=user)
    await bot.callback_handler(request, SimpleNamespace(user_data={}, bot=None))
    calls[method].assert_awaited_once()
    assert request.callback_query.answer.call_args.args[0] in {
        "Forked",
        "Resumed latest",
    }


async def test_stale_binding_after_discovery_cannot_mutate_old_window(env):
    mgr, listing, calls = env

    async def rebind(**kwargs):
        mgr.bind_thread(FULL, TOPIC, "replacement", chat_id=CHAT)
        return ["private"], ""

    listing.side_effect = rebind
    await bot.callback_handler(
        update(bot.CB_SESSION_RESUME + "1", user=FULL),
        SimpleNamespace(user_data={}, bot=None),
    )
    calls["resume_codex_session_for_window"].assert_not_awaited()
    assert mgr.get_window_codex_thread_id("full") == "full-thread"
