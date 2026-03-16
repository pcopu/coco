"""Tests for /apps callback-driven Looper panel flow."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram import InlineKeyboardMarkup

import coco.bot as bot
from coco.handlers.callback_data import (
    CB_APPS_OPEN,
    CB_APPS_LOOPER_INTERVAL,
    CB_APPS_LOOPER_START,
    CB_APPS_REFRESH,
    CB_APPS_TOGGLE,
)
from coco.skills import SkillDefinition


class _FakeQuery:
    def __init__(self, *, data: str, message) -> None:
        self.data = data
        self.message = message
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False):
        self.answers.append((text, show_alert))


def _make_callback_update(data: str, *, thread_id: int = 77, user_id: int = 1147817421):
    chat = SimpleNamespace(type="supergroup", id=-100321)
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
        "demo": _make_skill("demo", icon="📦"),
        "looper": _make_skill("looper", icon="🔁"),
    }
    keyboard = bot._build_apps_panel_keyboard(enabled_names=[], catalog=catalog)
    rows = keyboard.inline_keyboard
    assert rows[0][0].text == "📦 demo"
    assert rows[0][0].callback_data == f"{CB_APPS_TOGGLE}demo"
    assert rows[1][0].text == "🔁 looper"
    assert rows[1][0].callback_data == f"{CB_APPS_OPEN}looper"

    enabled_keyboard = bot._build_apps_panel_keyboard(
        enabled_names=["demo"],
        catalog=catalog,
    )
    assert enabled_keyboard.inline_keyboard[0][0].text == "✅ demo"


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
