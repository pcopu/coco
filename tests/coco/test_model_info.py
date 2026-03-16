"""Tests for /model info rendering."""

import json

import pytest

import coco.bot as bot
from coco.session import SessionManager
from coco.handlers.callback_data import (
    CB_MODEL_EFFORT_SET,
    CB_MODEL_REFRESH,
    CB_MODEL_SET,
)


def test_build_model_info_text_is_compact_and_uses_greeting(tmp_path, monkeypatch):
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()

    (codex_dir / "config.toml").write_text(
        'model = "gpt-5.3-codex"\nmodel_reasoning_effort = "xhigh"\n',
        encoding="utf-8",
    )

    payload = {
        "fetched_at": "2026-02-21T06:17:01Z",
        "client_version": "0.104.0",
        "models": [
            {
                "slug": "gpt-5.3-codex",
                "visibility": "list",
                "priority": 0,
                "default_reasoning_level": "medium",
                "supported_reasoning_levels": [
                    {"effort": "low"},
                    {"effort": "medium"},
                    {"effort": "high"},
                    {"effort": "xhigh"},
                ],
            },
            {
                "slug": "gpt-5.1-codex-mini",
                "visibility": "list",
                "priority": 10,
                "default_reasoning_level": "medium",
                "supported_reasoning_levels": [
                    {"effort": "medium"},
                    {"effort": "high"},
                ],
            },
            {
                "slug": "hidden-model",
                "visibility": "hide",
                "priority": 20,
                "default_reasoning_level": "minimal",
                "supported_reasoning_levels": [
                    {"effort": "minimal"},
                    {"effort": "low"},
                ],
            },
        ],
    }
    (codex_dir / "models_cache.json").write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(bot.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(bot, "_pick_model_greeting", lambda: "Dry greeting sample.")

    text = bot._build_model_info_text()

    assert text.splitlines()[0] == "🤖 Dry greeting sample."
    assert "Topic model: `gpt-5.3-codex`" in text
    assert "Topic reasoning: `xhigh`" in text
    assert "Dry greeting sample." in text
    assert "Stored per topic." in text
    assert "Model options (`--model`):" not in text
    assert "Reasoning options (`model_reasoning_effort`):" not in text


def test_build_model_info_text_reports_missing_cache(tmp_path, monkeypatch):
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text(
        'model = "gpt-5.3-codex"\nmodel_reasoning_effort = "high"\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(bot.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(bot, "_pick_model_greeting", lambda: "Dry greeting sample.")

    text = bot._build_model_info_text()
    assert "Model cache not found" in text
    assert "Topic model: `gpt-5.3-codex`" in text
    assert "Topic reasoning: `high`" in text
    assert "Dry greeting sample." in text


@pytest.mark.asyncio
async def test_model_callback_updates_topic_binding_not_global_config(monkeypatch):
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    mgr = SessionManager()
    mgr.bind_topic_to_codex_thread(
        user_id=1147817421,
        thread_id=77,
        chat_id=-100123,
        codex_thread_id="thread-1",
        window_id="@1",
        cwd="/tmp/proj",
        display_name="proj",
    )
    mgr.set_topic_model_selection(
        1147817421,
        77,
        chat_id=-100123,
        model_slug="gpt-5.3-codex",
        reasoning_effort="xhigh",
    )

    catalog = {
        "current_model": "global-default",
        "current_effort": "medium",
        "models": [
            {
                "slug": "gpt-5.3-codex",
                "default_effort": "medium",
                "levels": ["medium", "high", "xhigh"],
            },
            {
                "slug": "gpt-5.4",
                "default_effort": "high",
                "levels": ["high"],
            },
        ],
        "reasoning_options": ["medium", "high", "xhigh"],
    }

    class _FakeQuery:
        def __init__(self) -> None:
            self.data = f"{CB_MODEL_SET}gpt-5.4"
            self.message = type(
                "Msg",
                (),
                {
                    "message_thread_id": 77,
                    "chat": type("Chat", (), {"type": "supergroup", "id": -100123})(),
                    "chat_id": -100123,
                },
            )()
            self.answers: list[tuple[str | None, bool]] = []

        async def answer(self, text: str | None = None, show_alert: bool = False):
            self.answers.append((text, show_alert))

    query = _FakeQuery()
    update = type(
        "Update",
        (),
        {
            "callback_query": query,
            "effective_user": type("User", (), {"id": 1147817421})(),
            "effective_chat": query.message.chat,
            "effective_message": query.message,
        },
    )()
    context = type("Context", (), {"user_data": {}})()
    edits: list[str] = []

    monkeypatch.setattr(bot, "session_manager", mgr)
    monkeypatch.setattr(bot, "_is_chat_allowed", lambda _chat: True)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_load_codex_model_catalog", lambda: catalog)
    monkeypatch.setattr(
        bot,
        "_set_codex_config_value",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not write global config")),
    )

    async def _safe_edit(_query, text: str, **_kwargs):
        edits.append(text)

    monkeypatch.setattr(bot, "safe_edit", _safe_edit)

    await bot.callback_handler(update, context)

    binding = mgr.resolve_topic_binding(1147817421, 77, chat_id=-100123)
    assert binding is not None
    assert binding.model_slug == "gpt-5.4"
    assert binding.reasoning_effort == "high"
    assert edits
    assert "Topic model: `gpt-5.4`" in edits[-1]


def test_build_model_keyboard_has_select_buttons():
    catalog = {
        "current_model": "gpt-5.3-codex",
        "current_effort": "high",
        "models": [
            {
                "slug": "gpt-5.3-codex",
                "default_effort": "medium",
                "levels": ["low", "medium", "high", "xhigh"],
            },
            {
                "slug": "gpt-5.1-codex-mini",
                "default_effort": "medium",
                "levels": ["medium", "high"],
            },
        ],
        "reasoning_options": ["low", "medium", "high", "xhigh"],
    }

    markup = bot._build_model_keyboard(catalog)
    assert markup is not None
    buttons = [button for row in markup.inline_keyboard for button in row]
    callback_data = {button.callback_data for button in buttons}
    labels = {button.text for button in buttons}

    assert f"{CB_MODEL_SET}gpt-5.3-codex" in callback_data
    assert f"{CB_MODEL_SET}gpt-5.1-codex-mini" in callback_data
    assert f"{CB_MODEL_EFFORT_SET}high" in callback_data
    assert CB_MODEL_REFRESH in callback_data
    assert "✅ gpt-5.3-codex" in labels
    assert "✅ high" in labels


def test_set_codex_config_value_updates_existing_key(tmp_path, monkeypatch):
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    config_path = codex_dir / "config.toml"
    config_path.write_text(
        'model = "gpt-5.2-codex"\nmodel_reasoning_effort = "medium"\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(bot.Path, "home", lambda: tmp_path)

    ok, err = bot._set_codex_config_value("model", "gpt-5.3-codex")
    assert ok is True
    assert err == ""
    updated = config_path.read_text(encoding="utf-8")
    assert 'model = "gpt-5.3-codex"' in updated
    assert updated.count("model = ") == 1
    assert 'model_reasoning_effort = "medium"' in updated


def test_ensure_codex_project_trust_appends_missing_table(tmp_path, monkeypatch):
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    config_path = codex_dir / "config.toml"
    config_path.write_text('model = "gpt-5.3-codex"\n', encoding="utf-8")

    monkeypatch.setattr(bot.Path, "home", lambda: tmp_path)

    ok, err = bot._ensure_codex_project_trust(bot.Path("/srv/codex/projects"))
    assert ok is True
    assert err == ""
    updated = config_path.read_text(encoding="utf-8")
    assert '[projects."/srv/codex/projects"]' in updated
    assert 'trust_level = "trusted"' in updated


def test_ensure_codex_project_trust_updates_existing_level(tmp_path, monkeypatch):
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    config_path = codex_dir / "config.toml"
    config_path.write_text(
        '[projects."/srv/codex/projects"]\ntrust_level = "untrusted"\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(bot.Path, "home", lambda: tmp_path)

    ok, err = bot._ensure_codex_project_trust(
        bot.Path("/srv/codex/projects"),
        trust_level="trusted",
    )
    assert ok is True
    assert err == ""
    updated = config_path.read_text(encoding="utf-8")
    assert 'trust_level = "trusted"' in updated
    assert 'trust_level = "untrusted"' not in updated


def test_model_greeting_pool_uses_funny_one_liners_only():
    assert len(bot.MODEL_GREETING_MESSAGES) == len(bot._MODEL_GREETING_ENDINGS)
    assert all(":" not in msg for msg in bot.MODEL_GREETING_MESSAGES)
