"""Tests for /allowed metadata and permission helpers."""

import errno
from types import SimpleNamespace

import pytest

import coco.bot as bot
from coco.handlers import commands
from coco.handlers.callback_data import CB_ALLOWED_REMOVE


def test_parse_allowed_add_input():
    uid, name = bot._parse_allowed_add_input("123456789")
    assert uid == 123456789
    assert name == ""

    uid, name = bot._parse_allowed_add_input("123456789 Alice")
    assert uid == 123456789
    assert name == "Alice"

    uid, name = bot._parse_allowed_add_input("Alice 123")
    assert uid is None
    assert name == ""


def test_resolve_allowed_users_env_path_prefers_auth_env_file(tmp_path, monkeypatch):
    auth_env = tmp_path / "auth" / "auth.env"
    monkeypatch.setattr(bot.config, "auth_env_file", auth_env)

    assert bot._resolve_allowed_users_env_path() == auth_env


def test_auth_write_hint_for_auth_permission_error(tmp_path, monkeypatch):
    auth_env = tmp_path / "secure-auth" / "auth.env"
    monkeypatch.setattr(bot.config, "auth_env_file", auth_env)

    hint = bot._auth_write_hint(
        auth_env.parent / "allowed_users_meta.json",
        PermissionError(errno.EACCES, "permission denied"),
    )
    assert "needs write access" in hint
    assert str(auth_env.parent) in hint


def test_auth_write_hint_ignores_non_auth_paths(tmp_path, monkeypatch):
    auth_env = tmp_path / "secure-auth" / "auth.env"
    monkeypatch.setattr(bot.config, "auth_env_file", auth_env)

    hint = bot._auth_write_hint(
        tmp_path / "somewhere-else" / "allowed_users_meta.json",
        PermissionError(errno.EACCES, "permission denied"),
    )
    assert hint == ""


def test_persist_allowed_users_set_updates_env(tmp_path, monkeypatch):
    original_allowed = set(bot.config.allowed_users)
    original_env = bot.os.environ.get("ALLOWED_USERS")
    monkeypatch.setattr(bot.config, "config_dir", tmp_path)
    monkeypatch.chdir(tmp_path)
    if original_env is None:
        monkeypatch.delenv("ALLOWED_USERS", raising=False)
    else:
        monkeypatch.setenv("ALLOWED_USERS", original_env)

    try:
        ok, err = bot._persist_allowed_users_set({9, 3})
        assert ok is True
        assert err == ""
        assert bot.config.allowed_users == {3, 9}
        env_text = (tmp_path / ".env").read_text(encoding="utf-8")
        assert "ALLOWED_USERS=3,9" in env_text
    finally:
        bot.config.allowed_users = original_allowed


def test_load_allowed_users_meta_applies_defaults(tmp_path, monkeypatch):
    original_allowed = set(bot.config.allowed_users)
    monkeypatch.setattr(bot, "_ALLOWED_USERS_META_FILE", tmp_path / "allowed_users_meta.json")
    bot.config.allowed_users = {1147817421, 2}

    names, admins, scopes = bot._load_allowed_users_meta()
    assert names[1147817421] == "Peter"
    assert 1147817421 in admins
    assert scopes[1147817421] == bot.SCOPE_CREATE_SESSIONS
    assert scopes[2] == bot.SCOPE_SINGLE_SESSION
    bot.config.allowed_users = original_allowed


def test_build_allowed_overview_keyboard_admin_vs_member(tmp_path, monkeypatch):
    original_allowed = set(bot.config.allowed_users)
    monkeypatch.setattr(bot, "_ALLOWED_USERS_META_FILE", tmp_path / "allowed_users_meta.json")
    bot.config.allowed_users = {1, 2}
    ok, err = bot._save_allowed_users_meta(
        names={1: "Admin", 2: "Member"},
        admins={1},
        scopes={1: bot.SCOPE_CREATE_SESSIONS, 2: bot.SCOPE_SINGLE_SESSION},
    )
    assert ok is True
    assert err == ""

    admin_markup = bot._build_allowed_overview_keyboard(1)
    member_markup = bot._build_allowed_overview_keyboard(2)

    admin_callbacks = {
        b.callback_data for row in admin_markup.inline_keyboard for b in row if b.callback_data
    }
    member_callbacks = {
        b.callback_data for row in member_markup.inline_keyboard for b in row if b.callback_data
    }

    assert bot.CB_ALLOWED_ADD in admin_callbacks
    assert bot.CB_ALLOWED_REMOVE_MENU in admin_callbacks
    assert bot.CB_ALLOWED_REFRESH in admin_callbacks
    assert bot.CB_ALLOWED_ADD not in member_callbacks
    assert bot.CB_ALLOWED_REMOVE_MENU not in member_callbacks
    assert member_callbacks == {bot.CB_ALLOWED_REFRESH}
    bot.config.allowed_users = original_allowed


def test_build_allowed_overview_text_includes_token_notice(tmp_path, monkeypatch):
    original_allowed = set(bot.config.allowed_users)
    monkeypatch.setattr(bot, "_ALLOWED_USERS_META_FILE", tmp_path / "allowed_users_meta.json")
    bot.config.allowed_users = {1}
    text = bot._build_allowed_overview_text(1)
    assert "one-time approval token" in text
    assert "/allowed approve <token>" in text
    bot.config.allowed_users = original_allowed


def test_build_allowed_remove_keyboard_excludes_current_user(tmp_path, monkeypatch):
    original_allowed = set(bot.config.allowed_users)
    bot.config.allowed_users = {1, 2, 3}
    monkeypatch.setattr(bot, "_ALLOWED_USERS_META_FILE", tmp_path / "allowed_users_meta.json")
    ok, err = bot._save_allowed_users_meta(
        names={1: "Self", 2: "Alice", 3: "Bob"},
        admins={1},
        scopes={
            1: bot.SCOPE_CREATE_SESSIONS,
            2: bot.SCOPE_SINGLE_SESSION,
            3: bot.SCOPE_SINGLE_SESSION,
        },
    )
    assert ok is True
    assert err == ""

    markup = bot._build_allowed_remove_keyboard(current_user_id=1)
    callback_data = {
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    }

    assert f"{CB_ALLOWED_REMOVE}1" not in callback_data
    assert f"{CB_ALLOWED_REMOVE}2" in callback_data
    assert f"{CB_ALLOWED_REMOVE}3" in callback_data
    bot.config.allowed_users = original_allowed


def test_apply_allowed_user_remove_blocks_self(tmp_path, monkeypatch):
    original_allowed = set(bot.config.allowed_users)
    original_env = bot.os.environ.get("ALLOWED_USERS")
    monkeypatch.setattr(bot.config, "config_dir", tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bot, "_ALLOWED_USERS_META_FILE", tmp_path / "allowed_users_meta.json")
    if original_env is None:
        monkeypatch.delenv("ALLOWED_USERS", raising=False)
    else:
        monkeypatch.setenv("ALLOWED_USERS", original_env)
    bot.config.allowed_users = {10, 20}
    ok, err = bot._save_allowed_users_meta(
        names={10: "Self", 20: "Other"},
        admins={10},
        scopes={10: bot.SCOPE_CREATE_SESSIONS, 20: bot.SCOPE_SINGLE_SESSION},
    )
    assert ok is True
    assert err == ""

    ok, err = bot._apply_allowed_user_remove(10, acting_user_id=10)
    assert ok is False
    assert "cannot remove your own" in err.lower()
    assert bot.config.allowed_users == {10, 20}
    bot.config.allowed_users = original_allowed


def test_scope_helpers(tmp_path, monkeypatch):
    original_allowed = set(bot.config.allowed_users)
    monkeypatch.setattr(bot, "_ALLOWED_USERS_META_FILE", tmp_path / "allowed_users_meta.json")
    bot.config.allowed_users = {1, 2}
    ok, err = bot._save_allowed_users_meta(
        names={1: "Admin", 2: "Member"},
        admins={1},
        scopes={1: bot.SCOPE_CREATE_SESSIONS, 2: bot.SCOPE_SINGLE_SESSION},
    )
    assert ok is True
    assert err == ""

    assert bot._is_admin_user(1) is True
    assert bot._is_admin_user(2) is False
    assert bot._can_user_create_sessions(1) is True
    assert bot._can_user_create_sessions(2) is False

    ok, err = bot._set_user_scope(2, bot.SCOPE_CREATE_SESSIONS)
    assert ok is True
    assert err == ""
    assert bot._can_user_create_sessions(2) is True
    bot.config.allowed_users = original_allowed


class _FakeQuery:
    def __init__(self, *, data: str, message) -> None:
        self.data = data
        self.message = message
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False):
        self.answers.append((text, show_alert))


def _make_callback_update(data: str, *, user_id: int = 1):
    chat = SimpleNamespace(type="supergroup", id=-1001)
    message = SimpleNamespace(message_thread_id=77, chat=chat)
    query = _FakeQuery(data=data, message=message)
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=chat,
        effective_message=message,
    )
    return update, query


def _make_command_update(*, text: str, user_id: int = 1, thread_id: int | None = 77):
    chat = SimpleNamespace(type="supergroup", id=-1001)
    message = SimpleNamespace(
        text=text,
        chat=chat,
        message_thread_id=thread_id,
        is_topic_message=thread_id is not None,
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_message=message,
        effective_chat=chat,
        message=message,
    )


@pytest.mark.asyncio
async def test_allowed_add_callback_shows_request_instructions(monkeypatch):
    update, query = _make_callback_update(bot.CB_ALLOWED_ADD, user_id=1)
    edits: list[tuple[str, object]] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: True)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )

    async def _safe_edit(_query, text: str, **kwargs):
        edits.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(bot, "safe_edit", _safe_edit)

    await bot.callback_handler(update, SimpleNamespace(user_data={}))

    assert edits
    assert "Select Group Members" in edits[-1][0]
    assert query.answers
    assert query.answers[-1] == (None, False)


def test_allowed_picker_uses_20_per_page(monkeypatch):
    original_allowed = set(bot.config.allowed_users)
    bot.config.allowed_users = {1}
    monkeypatch.setattr(
        bot,
        "_group_member_candidates",
        lambda _chat_id: [(idx, f"User {idx}") for idx in range(1, 46)],
    )
    text, entries, page, page_count = bot._build_allowed_picker_text(
        chat_id=-1001,
        page=0,
        selected_ids=set(),
    )
    assert "Page: `1/3`" in text
    assert len(entries) == 20
    assert page == 0
    assert page_count == 3
    bot.config.allowed_users = original_allowed


@pytest.mark.asyncio
async def test_allowed_batch_role_callback_queues_single_token(monkeypatch):
    update, query = _make_callback_update(bot.CB_ALLOWED_ADD_CREATE, user_id=1)
    edits: list[tuple[str, object]] = []
    queued_targets: list[bot._PendingAllowedAddTarget] = []
    request = bot._PendingAllowedAuthRequest(
        token="ABCDEFGH",
        action="add_batch",
        requested_by=1,
        target_user_id=0,
        target_name="",
        target_scope=bot.SCOPE_CREATE_SESSIONS,
        bind_thread_id=None,
        bind_window_id=None,
        bind_chat_id=None,
        created_at=0.0,
        expires_at=9999999999.0,
        batch_add_targets=(),
    )
    context = SimpleNamespace(
        bot=object(),
        user_data={
            bot.STATE_KEY: bot.STATE_ALLOWED_PICK_ROLE,
            bot.ALLOWED_PICK_CHAT_KEY: -1001,
            bot.ALLOWED_PICK_SELECTED_IDS_KEY: [2, 3],
            bot.ALLOWED_PICK_THREAD_KEY: 77,
            bot.ALLOWED_PICK_WINDOW_KEY: "@77",
        },
    )

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: True)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot,
        "_group_member_candidates",
        lambda _chat_id: [(2, "Alice"), (3, "Bob")],
    )

    def _queue_batch(*, requested_by: int, targets: list[bot._PendingAllowedAddTarget]):
        _ = requested_by
        queued_targets.extend(targets)
        return True, "", request

    monkeypatch.setattr(bot, "_queue_allowed_add_batch_request", _queue_batch)

    async def _notify(**_kwargs):
        return (1, 1)

    monkeypatch.setattr(bot, "_notify_allowed_auth_token", _notify)
    monkeypatch.setattr(bot, "_build_allowed_overview_text", lambda _uid: "overview")
    monkeypatch.setattr(bot, "_build_allowed_overview_keyboard", lambda _uid: "kb")

    async def _safe_edit(_query, text: str, **kwargs):
        edits.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(bot, "safe_edit", _safe_edit)

    await bot.callback_handler(update, context)

    assert len(queued_targets) == 2
    assert {t.user_id for t in queued_targets} == {2, 3}
    assert all(t.scope == bot.SCOPE_CREATE_SESSIONS for t in queued_targets)
    assert edits
    assert edits[-1][0] == "overview"


@pytest.mark.asyncio
async def test_allowed_command_request_add_queues_token(monkeypatch):
    update = _make_command_update(text="/allowed request_add 20 Alice", user_id=1)
    replies: list[str] = []
    request = bot._PendingAllowedAuthRequest(
        token="ABCDEFGH",
        action="add",
        requested_by=1,
        target_user_id=20,
        target_name="Alice",
        target_scope=bot.SCOPE_SINGLE_SESSION,
        bind_thread_id=77,
        bind_window_id="@77",
        bind_chat_id=-1001,
        created_at=0.0,
        expires_at=9999999999.0,
    )

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: True)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_window_for_thread",
        lambda *_args, **_kwargs: "@77",
    )
    monkeypatch.setattr(
        bot,
        "_queue_allowed_add_request",
        lambda **_kwargs: (True, "", request),
    )

    async def _notify(**_kwargs):
        return (1, 1)

    monkeypatch.setattr(bot, "_notify_allowed_auth_token", _notify)

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await commands.allowed_command(update, SimpleNamespace(bot=object(), user_data={}))

    assert replies
    assert "Pending add request created" in replies[-1]
    assert "/allowed approve <token>" in replies[-1]


@pytest.mark.asyncio
async def test_allowed_command_approve_applies_token(monkeypatch):
    update = _make_command_update(text="/allowed approve ABCDEFGH", user_id=1)
    replies: list[str] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: True)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot,
        "_apply_allowed_auth_request_token",
        lambda _token, *, acting_user_id: (True, "Added `20` with scope: single session"),
    )
    monkeypatch.setattr(
        bot,
        "_build_allowed_overview_text",
        lambda _uid: "overview",
    )
    monkeypatch.setattr(
        bot,
        "_build_allowed_overview_keyboard",
        lambda _uid: "kb",
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await commands.allowed_command(update, SimpleNamespace(bot=object(), user_data={}))

    assert replies[0].startswith("✅ Added `20`")
    assert replies[-1] == "overview"


def test_allowed_auth_token_round_trip_add(tmp_path, monkeypatch):
    original_allowed = set(bot.config.allowed_users)
    original_env = bot.os.environ.get("ALLOWED_USERS")
    monkeypatch.setattr(bot.config, "config_dir", tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bot, "_ALLOWED_USERS_META_FILE", tmp_path / "allowed_users_meta.json")
    if original_env is None:
        monkeypatch.delenv("ALLOWED_USERS", raising=False)
    else:
        monkeypatch.setenv("ALLOWED_USERS", original_env)

    bot.config.allowed_users = {10}
    bot._PENDING_ALLOWED_AUTH_REQUESTS.clear()
    ok, err = bot._save_allowed_users_meta(
        names={10: "Admin"},
        admins={10},
        scopes={10: bot.SCOPE_CREATE_SESSIONS},
    )
    assert ok is True
    assert err == ""

    ok, err, request = bot._queue_allowed_add_request(
        requested_by=10,
        new_user_id=20,
        name="Alice",
        scope=bot.SCOPE_CREATE_SESSIONS,
    )
    assert ok is True
    assert err == ""
    assert request is not None

    ok, message = bot._apply_allowed_auth_request_token(
        request.token,
        acting_user_id=10,
    )
    assert ok is True
    assert "Added `20`" in message
    assert 20 in bot.config.allowed_users
    bot.config.allowed_users = original_allowed


def test_allowed_auth_token_round_trip_remove(tmp_path, monkeypatch):
    original_allowed = set(bot.config.allowed_users)
    original_env = bot.os.environ.get("ALLOWED_USERS")
    monkeypatch.setattr(bot.config, "config_dir", tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bot, "_ALLOWED_USERS_META_FILE", tmp_path / "allowed_users_meta.json")
    if original_env is None:
        monkeypatch.delenv("ALLOWED_USERS", raising=False)
    else:
        monkeypatch.setenv("ALLOWED_USERS", original_env)

    bot.config.allowed_users = {10, 20}
    bot._PENDING_ALLOWED_AUTH_REQUESTS.clear()
    ok, err = bot._save_allowed_users_meta(
        names={10: "Admin", 20: "Target"},
        admins={10},
        scopes={10: bot.SCOPE_CREATE_SESSIONS, 20: bot.SCOPE_SINGLE_SESSION},
    )
    assert ok is True
    assert err == ""

    ok, err, request = bot._queue_allowed_remove_request(
        requested_by=10,
        target_user_id=20,
    )
    assert ok is True
    assert err == ""
    assert request is not None

    ok, message = bot._apply_allowed_auth_request_token(
        request.token,
        acting_user_id=10,
    )
    assert ok is True
    assert "Removed `20`" in message
    assert 20 not in bot.config.allowed_users
    bot.config.allowed_users = original_allowed
