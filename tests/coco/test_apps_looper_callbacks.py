"""Tests for /apps callback-driven Looper panel flow."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram import InlineKeyboardMarkup

import coco.bot as bot
from coco.handlers.callback_data import (
    CB_APPS_AUTORESEARCH_OUTCOME,
    CB_APPS_OPEN,
    CB_APPS_LOOPER_INTERVAL,
    CB_APPS_LOOPER_START,
    CB_APPS_REFRESH,
    CB_APPS_TOGGLE,
)
from coco.session import SessionManager
from coco.session import TopicOwnership
from coco.skills import SkillDefinition


class _FakeQuery:
    def __init__(self, *, data: str, message) -> None:
        self.data = data
        self.message = message
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False):
        self.answers.append((text, show_alert))


def _make_callback_update(
    data: str,
    *,
    thread_id: int = 77,
    user_id: int = 1147817421,
    chat_id: int = -100321,
):
    chat = SimpleNamespace(type="supergroup", id=chat_id)
    message = SimpleNamespace(message_thread_id=thread_id, chat=chat)
    query = _FakeQuery(data=data, message=message)
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=chat,
        effective_message=message,
    )
    return update, query


def _make_skill(name: str, *, icon: str = "") -> SkillDefinition:
    return SkillDefinition(
        name=name,
        description=f"{name} description",
        skill_md_path=Path(f"/tmp/{name}/SKILL.md"),
        source_root=Path("/tmp"),
        folder_name=name,
        icon=icon,
    )


def test_apps_keyboard_uses_icon_and_routes_by_config_support():
    catalog = {
        "autoresearch": _make_skill("autoresearch", icon="🔎"),
        "demo": _make_skill("demo", icon="📦"),
        "looper": _make_skill("looper", icon="🔁"),
    }
    keyboard = bot._build_apps_panel_keyboard(enabled_names=[], catalog=catalog)
    rows = keyboard.inline_keyboard
    assert rows[0][0].text == "🔎 autoresearch"
    assert rows[0][0].callback_data == f"{CB_APPS_OPEN}autoresearch"
    assert rows[1][0].text == "📦 demo"
    assert rows[1][0].callback_data == f"{CB_APPS_TOGGLE}demo"
    assert rows[2][0].text == "🔁 looper"
    assert rows[2][0].callback_data == f"{CB_APPS_OPEN}looper"

    enabled_keyboard = bot._build_apps_panel_keyboard(
        enabled_names=["demo"],
        catalog=catalog,
    )
    enabled_labels = [row[0].text for row in enabled_keyboard.inline_keyboard[:-1]]
    assert "✅ demo" in enabled_labels


def test_looper_panel_keyboard_includes_disable_app_button():
    keyboard = bot._build_looper_panel_keyboard(
        config_data={
            "plan_path": "plans/ship.md",
            "keyword": "done",
            "instructions": "",
            "interval_seconds": 900,
            "limit_seconds": 0,
            "candidates": ["plans/ship.md"],
        },
        active_state=None,
    )
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    assert "🚫 Disable App" in labels


@pytest.mark.asyncio
async def test_apps_refresh_callback_edits_overview(monkeypatch):
    update, query = _make_callback_update(CB_APPS_REFRESH)
    edits: list[tuple[str, object]] = []
    keyboard = InlineKeyboardMarkup([])

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "runtime_mode", "hybrid")
    monkeypatch.setattr(bot, "_codex_app_server_enabled", lambda: False)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot,
        "_build_apps_panel_payload_for_topic",
        lambda **_kwargs: ("apps panel", keyboard, {}, []),
    )

    async def _safe_edit(_query, text: str, **kwargs):
        edits.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(bot, "safe_edit", _safe_edit)

    await bot.callback_handler(update, SimpleNamespace(user_data={}))

    assert edits == [("apps panel", keyboard)]
    assert query.answers
    assert query.answers[-1] == ("Refreshed", False)


@pytest.mark.asyncio
async def test_general_apps_callback_admin_mutates_control_owner_without_shadow_binding(
    monkeypatch,
):
    owner_user_id = 100
    admin_user_id = 200
    chat_id = -100321001
    manager = SessionManager()
    manager.set_coco_control_topic(owner_user_id, 1, chat_id=chat_id)
    manager.bind_topic_to_codex_thread(
        user_id=owner_user_id,
        thread_id=1,
        chat_id=chat_id,
        codex_thread_id="control-thread",
        window_id="@control",
        cwd="/tmp/control",
        display_name="coco-control",
    )
    catalog = {"demo": _make_skill("demo", icon="📦")}
    update, query = _make_callback_update(
        f"{CB_APPS_TOGGLE}demo",
        thread_id=1,
        user_id=admin_user_id,
        chat_id=chat_id,
    )

    monkeypatch.setattr(bot, "session_manager", manager)
    monkeypatch.setattr(bot, "_is_chat_allowed", lambda _chat: True)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda uid: uid == admin_user_id)
    monkeypatch.setattr(manager, "discover_skill_catalog", lambda: catalog)

    async def _safe_edit(_query, _text: str, **_kwargs):
        return None

    monkeypatch.setattr(bot, "safe_edit", _safe_edit)

    await bot.callback_handler(update, type("Context", (), {"user_data": {}})())

    assert manager.get_thread_skills(owner_user_id, 1, chat_id=chat_id) == ["demo"]
    assert manager.get_thread_skills(admin_user_id, 1, chat_id=chat_id) == []
    assert manager.resolve_topic_binding(admin_user_id, 1, chat_id=chat_id) is None


@pytest.mark.asyncio
async def test_apps_open_callback_shows_action_sheet(monkeypatch):
    update, query = _make_callback_update(f"{CB_APPS_OPEN}looper")
    edits: list[tuple[str, object]] = []
    keyboard = InlineKeyboardMarkup([])

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "runtime_mode", "hybrid")
    monkeypatch.setattr(bot, "_codex_app_server_enabled", lambda: False)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot,
        "_build_app_actions_payload_for_topic",
        lambda **_kwargs: (True, "looper actions", keyboard, "looper"),
    )

    async def _safe_edit(_query, text: str, **kwargs):
        edits.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(bot, "safe_edit", _safe_edit)

    await bot.callback_handler(update, SimpleNamespace(user_data={}))

    assert edits == [("looper actions", keyboard)]
    assert query.answers
    assert query.answers[-1] == ("App actions", False)


@pytest.mark.asyncio
async def test_apps_open_callback_shows_autoresearch_panel(monkeypatch):
    update, query = _make_callback_update(f"{CB_APPS_OPEN}autoresearch")
    edits: list[tuple[str, object]] = []
    keyboard = InlineKeyboardMarkup([])

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "runtime_mode", "hybrid")
    monkeypatch.setattr(bot, "_codex_app_server_enabled", lambda: False)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )

    async def _build_autoresearch_panel_payload_for_topic(**_kwargs):
        return True, "autoresearch panel", keyboard, ""

    monkeypatch.setattr(
        bot,
        "_build_autoresearch_panel_payload_for_topic",
        _build_autoresearch_panel_payload_for_topic,
    )

    async def _safe_edit(_query, text: str, **kwargs):
        edits.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(bot, "safe_edit", _safe_edit)

    await bot.callback_handler(update, SimpleNamespace(user_data={}))

    assert edits == [("autoresearch panel", keyboard)]
    assert query.answers
    assert query.answers[-1] == ("Auto research", False)


@pytest.mark.asyncio
async def test_apps_configure_callback_shows_autoresearch_panel(monkeypatch):
    update, query = _make_callback_update(f"{bot.CB_APPS_CONFIGURE}autoresearch")
    edits: list[tuple[str, object]] = []
    keyboard = InlineKeyboardMarkup([])

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "runtime_mode", "hybrid")
    monkeypatch.setattr(bot, "_codex_app_server_enabled", lambda: False)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )

    async def _build_autoresearch_panel_payload_for_topic(**_kwargs):
        return True, "autoresearch panel", keyboard, ""

    monkeypatch.setattr(
        bot,
        "_build_autoresearch_panel_payload_for_topic",
        _build_autoresearch_panel_payload_for_topic,
    )

    async def _safe_edit(_query, text: str, **kwargs):
        edits.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(bot, "safe_edit", _safe_edit)

    await bot.callback_handler(update, SimpleNamespace(user_data={}))

    assert edits == [("autoresearch panel", keyboard)]
    assert query.answers
    assert query.answers[-1] == ("Auto research config", False)


@pytest.mark.asyncio
async def test_autoresearch_run_now_callback_sends_digest_without_enabling_schedule(monkeypatch):
    update, query = _make_callback_update("am:ar:run")
    edits: list[tuple[str, object]] = []
    sent: list[tuple[int, int | None, str]] = []
    keyboard = InlineKeyboardMarkup([])
    enabled_names: list[str] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "runtime_mode", "hybrid")
    monkeypatch.setattr(bot, "_codex_app_server_enabled", lambda: False)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot,
        "get_autoresearch_state",
        lambda **_kwargs: SimpleNamespace(outcome="Close more inbound leads"),
    )
    monkeypatch.setattr(
        bot,
        "run_autoresearch_now",
        lambda **_kwargs: "digest text",
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, chat_id=None: chat_id if chat_id is not None else -100321,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "discover_skill_catalog",
        lambda: {"autoresearch": _make_skill("autoresearch", icon="🔎")},
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_thread_skills",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_thread_skills",
        lambda _uid, _tid, names, **_kwargs: enabled_names.__setitem__(
            slice(None), list(names)
        ),
    )

    async def _build_autoresearch_panel_payload_for_topic(**_kwargs):
        return True, "autoresearch panel", keyboard, ""

    monkeypatch.setattr(
        bot,
        "_build_autoresearch_panel_payload_for_topic",
        _build_autoresearch_panel_payload_for_topic,
    )

    async def _safe_edit(_query, text: str, **kwargs):
        edits.append((text, kwargs.get("reply_markup")))

    async def _safe_send(_bot, chat_id: int, text: str, *, message_thread_id=None, **_kwargs):
        sent.append((chat_id, message_thread_id, text))

    monkeypatch.setattr(bot, "safe_edit", _safe_edit)
    monkeypatch.setattr(bot, "safe_send", _safe_send)

    await bot.callback_handler(update, SimpleNamespace(user_data={}))

    assert enabled_names == []
    assert sent == [(-100321, 77, "digest text")]
    assert edits == [("autoresearch panel", keyboard)]
    assert query.answers
    assert query.answers[-1] == ("Running auto research...", False)


@pytest.mark.asyncio
async def test_autoresearch_schedule_callback_enables_daily_delivery(monkeypatch):
    update, query = _make_callback_update("am:ar:sched")
    edits: list[tuple[str, object]] = []
    keyboard = InlineKeyboardMarkup([])
    enabled_names: list[str] = []
    catalog = {"autoresearch": _make_skill("autoresearch", icon="🔎")}

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "runtime_mode", "hybrid")
    monkeypatch.setattr(bot, "_codex_app_server_enabled", lambda: False)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot,
        "get_autoresearch_state",
        lambda **_kwargs: SimpleNamespace(outcome="Close more inbound leads"),
    )
    monkeypatch.setattr(bot.session_manager, "discover_skill_catalog", lambda: catalog)
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_thread_skills",
        lambda *_args, **_kwargs: [catalog[name] for name in enabled_names],
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_thread_skills",
        lambda _uid, _tid, names, **_kwargs: enabled_names.__setitem__(
            slice(None), list(names)
        ),
    )

    async def _build_autoresearch_panel_payload_for_topic(**_kwargs):
        return True, "autoresearch panel", keyboard, ""

    monkeypatch.setattr(
        bot,
        "_build_autoresearch_panel_payload_for_topic",
        _build_autoresearch_panel_payload_for_topic,
    )

    async def _safe_edit(_query, text: str, **kwargs):
        edits.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(bot, "safe_edit", _safe_edit)

    await bot.callback_handler(update, SimpleNamespace(user_data={}))

    assert enabled_names == ["autoresearch"]
    assert edits == [("autoresearch panel", keyboard)]
    assert query.answers
    assert query.answers[-1] == ("Daily auto research enabled", False)


@pytest.mark.asyncio
async def test_autoresearch_stop_callback_disables_daily_delivery(monkeypatch):
    update, query = _make_callback_update("am:ar:stop")
    edits: list[tuple[str, object]] = []
    keyboard = InlineKeyboardMarkup([])
    enabled_names: list[str] = ["autoresearch"]
    catalog = {"autoresearch": _make_skill("autoresearch", icon="🔎")}

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "runtime_mode", "hybrid")
    monkeypatch.setattr(bot, "_codex_app_server_enabled", lambda: False)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(bot.session_manager, "discover_skill_catalog", lambda: catalog)
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_thread_skills",
        lambda *_args, **_kwargs: [catalog[name] for name in enabled_names],
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_thread_skills",
        lambda _uid, _tid, names, **_kwargs: enabled_names.__setitem__(
            slice(None), list(names)
        ),
    )

    async def _build_autoresearch_panel_payload_for_topic(**_kwargs):
        return True, "autoresearch panel", keyboard, ""

    monkeypatch.setattr(
        bot,
        "_build_autoresearch_panel_payload_for_topic",
        _build_autoresearch_panel_payload_for_topic,
    )

    async def _safe_edit(_query, text: str, **kwargs):
        edits.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(bot, "safe_edit", _safe_edit)

    await bot.callback_handler(update, SimpleNamespace(user_data={}))

    assert enabled_names == []
    assert edits == [("autoresearch panel", keyboard)]
    assert query.answers
    assert query.answers[-1] == ("Daily auto research stopped", False)


@pytest.mark.asyncio
async def test_apps_toggle_callback_nonconfig_updates_overview(monkeypatch):
    update, query = _make_callback_update(f"{CB_APPS_TOGGLE}demo")
    edits: list[tuple[str, object]] = []
    enabled_names: list[str] = []
    catalog = {"demo": _make_skill("demo", icon="📦")}

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "runtime_mode", "hybrid")
    monkeypatch.setattr(bot, "_codex_app_server_enabled", lambda: False)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(bot.session_manager, "discover_skill_catalog", lambda: catalog)
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_thread_skills",
        lambda *_args, **_kwargs: [catalog[name] for name in enabled_names],
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_thread_skills",
        lambda _uid, _tid, names, **_kwargs: enabled_names.__setitem__(
            slice(None), list(names)
        ),
    )

    async def _safe_edit(_query, text: str, **kwargs):
        edits.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(bot, "safe_edit", _safe_edit)

    await bot.callback_handler(update, SimpleNamespace(user_data={}))

    assert enabled_names == ["demo"]
    assert edits
    assert edits[-1][0].startswith("🧩 *Topic Apps*")
    keyboard = edits[-1][1]
    assert isinstance(keyboard, InlineKeyboardMarkup)
    assert keyboard.inline_keyboard[0][0].text == "✅ demo"
    assert query.answers
    assert query.answers[-1] == ("Enabled demo", False)


@pytest.mark.asyncio
async def test_looper_interval_custom_callback_sets_text_input_state(monkeypatch):
    update, query = _make_callback_update(f"{CB_APPS_LOOPER_INTERVAL}custom")
    edits: list[tuple[str, object]] = []
    keyboard = InlineKeyboardMarkup([])
    context = SimpleNamespace(user_data={})

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )

    async def _build_looper_panel_payload_for_topic(**_kwargs):
        return True, "looper panel", keyboard, "@77"

    monkeypatch.setattr(
        bot,
        "_build_looper_panel_payload_for_topic",
        _build_looper_panel_payload_for_topic,
    )

    async def _safe_edit(_query, text: str, **kwargs):
        edits.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(bot, "safe_edit", _safe_edit)

    await bot.callback_handler(update, context)

    assert edits == [("looper panel", keyboard)]
    assert context.user_data[bot.STATE_KEY] == bot.STATE_APPS_LOOPER_INTERVAL
    assert context.user_data[bot.APPS_PENDING_THREAD_KEY] == 77
    assert context.user_data[bot.APPS_PENDING_WINDOW_ID_KEY] == "@77"
    assert query.answers
    assert query.answers[-1][0] == "Send interval like `10m` or `1h`."
    assert query.answers[-1][1] is True


@pytest.mark.asyncio
async def test_apps_pending_prompt_persists_full_topic_scope_and_ownership(monkeypatch):
    update, query = _make_callback_update(f"{CB_APPS_LOOPER_INTERVAL}custom")
    keyboard = InlineKeyboardMarkup([])
    context = SimpleNamespace(user_data={})
    ownership = TopicOwnership(
        window_id="@77",
        codex_thread_id="codex-77",
        machine_id="local",
        cwd="/tmp/project",
    )

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(bot, "capture_topic_ownership", lambda *_args, **_kwargs: ownership)
    async def _build_looper_panel_payload_for_topic(**_kwargs):
        return True, "looper panel", keyboard, "@77"

    async def _safe_edit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bot, "_build_looper_panel_payload_for_topic", _build_looper_panel_payload_for_topic)
    monkeypatch.setattr(bot, "safe_edit", _safe_edit)

    await bot.callback_handler(update, context)

    assert context.user_data["_apps_pending_user_id"] == 1147817421
    assert context.user_data["_apps_pending_chat_id"] == -100321
    assert context.user_data[bot.APPS_PENDING_THREAD_KEY] == 77
    assert context.user_data["_apps_pending_ownership"] == {
        "window_id": "@77",
        "codex_thread_id": "codex-77",
        "machine_id": "local",
        "cwd": "/tmp/project",
    }
    assert query.answers


@pytest.mark.asyncio
async def test_general_admin_apps_prompt_persists_canonical_owner_scope(monkeypatch):
    owner_user_id = 100
    admin_user_id = 200
    chat_id = -100321002
    manager = SessionManager()
    manager.set_coco_control_topic(owner_user_id, 1, chat_id=chat_id)
    manager.bind_topic_to_codex_thread(
        user_id=owner_user_id,
        thread_id=1,
        chat_id=chat_id,
        codex_thread_id="control-thread",
        window_id="@control",
        cwd="/tmp/control",
        display_name="coco-control",
    )
    update, query = _make_callback_update(
        CB_APPS_AUTORESEARCH_OUTCOME,
        thread_id=1,
        user_id=admin_user_id,
        chat_id=chat_id,
    )
    context = SimpleNamespace(user_data={})
    keyboard = InlineKeyboardMarkup([])
    monkeypatch.setattr(bot, "session_manager", manager)
    monkeypatch.setattr(bot, "_is_chat_allowed", lambda _chat: True)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda uid: uid == admin_user_id)
    monkeypatch.setattr(bot, "capture_topic_ownership", lambda *_args, **_kwargs: TopicOwnership(
        window_id="@control",
        codex_thread_id="control-thread",
        machine_id="local",
        cwd="/tmp/control",
    ))
    async def _build_autoresearch_panel_payload_for_topic(**_kwargs):
        return True, "autoresearch panel", keyboard, ""

    async def _safe_edit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        bot,
        "_build_autoresearch_panel_payload_for_topic",
        _build_autoresearch_panel_payload_for_topic,
    )
    monkeypatch.setattr(bot, "safe_edit", _safe_edit)

    await bot.callback_handler(update, context)

    assert context.user_data["_apps_pending_user_id"] == owner_user_id
    assert context.user_data["_apps_pending_chat_id"] == chat_id
    assert context.user_data[bot.APPS_PENDING_THREAD_KEY] == 1
    assert query.answers


@pytest.mark.asyncio
async def test_revoked_general_admin_cannot_consume_looper_prompt(monkeypatch):
    owner_user_id = 100
    admin_user_id = 200
    chat_id = -100321
    chat = SimpleNamespace(type="supergroup", id=chat_id)
    message = SimpleNamespace(
        text="10m",
        chat=chat,
        chat_id=chat_id,
        message_thread_id=1,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=admin_user_id),
        effective_message=message,
        effective_chat=chat,
        message=message,
    )
    context = SimpleNamespace(
        bot=object(),
        user_data={
            bot.STATE_KEY: bot.STATE_APPS_LOOPER_INTERVAL,
            bot.APPS_PENDING_THREAD_KEY: 1,
            bot.APPS_PENDING_WINDOW_ID_KEY: "@control",
            bot.APPS_PENDING_USER_KEY: owner_user_id,
            bot.APPS_PENDING_CHAT_KEY: chat_id,
            bot.APPS_PENDING_OWNERSHIP_KEY: {
                "window_id": "@control",
                "codex_thread_id": "control-thread",
                "machine_id": "local",
                "cwd": "/tmp/control",
            },
            bot.APPS_LOOPER_CONFIG_KEY: {
                "plan_path": "plans/ship.md",
                "keyword": "done",
                "instructions": "",
                "interval_seconds": 900,
                "limit_seconds": 0,
                "candidates": [],
            },
        },
    )
    auth_calls: list[dict[str, object]] = []
    routing_calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(
        bot,
        "_ensure_default_coco_general_control",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(owner_user_id, 1, chat_id),
    )
    monkeypatch.setattr(
        bot,
        "_coco_control_owner_user_id",
        lambda _user_id, _chat_id: owner_user_id,
    )
    monkeypatch.setattr(bot, "is_topic_ownership_current", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        bot,
        "_can_coco_control_target",
        lambda **kwargs: auth_calls.append(kwargs) or False,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *args, **_kwargs: routing_calls.append(args),
    )
    replies: list[str] = []

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.text_handler(update, context)

    assert routing_calls == []
    assert auth_calls == [
        {
            "caller_user_id": admin_user_id,
            "target_user_id": owner_user_id,
            "chat_id": chat_id,
        }
    ]
    assert context.user_data.get(bot.STATE_KEY) is None
    assert replies == [f"❌ {bot._COCO_CONTROL_PERMISSION_DENIED_TEXT}"]


@pytest.mark.asyncio
async def test_looper_start_callback_uses_panel_config(monkeypatch):
    update, query = _make_callback_update(CB_APPS_LOOPER_START)
    edits: list[tuple[str, object]] = []
    start_calls: list[dict[str, object]] = []
    keyboard = InlineKeyboardMarkup([])
    context = SimpleNamespace(
        user_data={
            bot.APPS_LOOPER_CONFIG_KEY: {
                "plan_path": "plans/ship.md",
                "keyword": "done",
                "instructions": "focus tests first",
                "interval_seconds": 900,
                "limit_seconds": 3600,
            }
        }
    )

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "runtime_mode", "hybrid")
    monkeypatch.setattr(bot, "_codex_app_server_enabled", lambda: False)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_window_for_thread",
        lambda _uid, _tid, **_kwargs: "@77",
    )
    monkeypatch.setattr(
        bot,
        "_resolve_workspace_dir_for_window",
        lambda **_kwargs: "/tmp/project",
    )

    async def _build_looper_panel_payload_for_topic(**_kwargs):
        return True, "looper panel", keyboard, "@77"

    monkeypatch.setattr(
        bot,
        "_build_looper_panel_payload_for_topic",
        _build_looper_panel_payload_for_topic,
    )
    monkeypatch.setattr(bot.session_manager, "discover_skill_catalog", lambda: {})

    def _start_looper(**kwargs):
        start_calls.append(kwargs)
        deadline = 0.0
        if int(kwargs["limit_seconds"]) > 0:
            deadline = 100.0 + int(kwargs["limit_seconds"])
        return SimpleNamespace(
            plan_path=kwargs["plan_path"],
            keyword=kwargs["keyword"],
            instructions=kwargs["instructions"],
            interval_seconds=int(kwargs["interval_seconds"]),
            started_at=100.0,
            deadline_at=deadline,
        )

    monkeypatch.setattr(bot, "start_looper", _start_looper)

    async def _safe_edit(_query, text: str, **kwargs):
        edits.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(bot, "safe_edit", _safe_edit)

    await bot.callback_handler(update, context)

    assert start_calls
    call = start_calls[0]
    assert call["plan_path"] == "plans/ship.md"
    assert call["keyword"] == "done"
    assert call["interval_seconds"] == 900
    assert call["limit_seconds"] == 3600
    assert call["instructions"] == "focus tests first"
    assert edits
    assert edits[-1] == ("looper panel", keyboard)
    assert context.user_data[bot.STATE_KEY] == ""
    assert query.answers
    assert query.answers[-1] == ("Looper started", False)


@pytest.mark.asyncio
async def test_looper_disable_callback_stops_and_disables_app(monkeypatch):
    update, query = _make_callback_update("am:loop:disable")
    edits: list[tuple[str, object]] = []
    keyboard = InlineKeyboardMarkup([])
    enabled_names: list[str] = ["looper"]
    stop_calls: list[tuple[int, int, str]] = []
    catalog = {"looper": _make_skill("looper", icon="🔁")}

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "runtime_mode", "hybrid")
    monkeypatch.setattr(bot, "_codex_app_server_enabled", lambda: False)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(bot.session_manager, "discover_skill_catalog", lambda: catalog)
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_thread_skills",
        lambda *_args, **_kwargs: [catalog[name] for name in enabled_names],
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_thread_skills",
        lambda _uid, _tid, names, **_kwargs: enabled_names.__setitem__(
            slice(None), list(names)
        ),
    )

    def _stop_looper(
        *, user_id: int, chat_id: int | None, thread_id: int, reason: str
    ):
        stop_calls.append((user_id, thread_id, reason))
        return True

    monkeypatch.setattr(bot, "stop_looper", _stop_looper)

    async def _build_looper_panel_payload_for_topic(**_kwargs):
        return True, "looper panel", keyboard, "@77"

    monkeypatch.setattr(
        bot,
        "_build_looper_panel_payload_for_topic",
        _build_looper_panel_payload_for_topic,
    )

    async def _safe_edit(_query, text: str, **kwargs):
        edits.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(bot, "safe_edit", _safe_edit)

    await bot.callback_handler(update, SimpleNamespace(user_data={}))

    assert stop_calls == [(1147817421, 77, "manual_disable")]
    assert enabled_names == []
    assert edits == [("looper panel", keyboard)]
    assert query.answers
    assert query.answers[-1] == ("Looper app disabled", False)
