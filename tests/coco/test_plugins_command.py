"""Tests for /plugins command behavior."""

from pathlib import Path
from types import SimpleNamespace

import pytest

import coco.bot as bot


def _make_update(text: str, *, thread_id: int = 77, user_id: int = 1147817421):
    chat = SimpleNamespace(type="supergroup", id=-100123)
    message = SimpleNamespace(
        text=text,
        message_thread_id=thread_id,
        chat=chat,
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_message=message,
        effective_chat=chat,
        message=message,
    )


@pytest.mark.asyncio
async def test_plugins_command_lists_installed_plugins(monkeypatch):
    update = _make_update("/plugins")
    replies: list[str] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: True)
    monkeypatch.setattr(
        bot,
        "_load_codex_plugin_inventory",
        lambda: [
            {
                "plugin_id": "github@openai-curated",
                "name": "github",
                "display_name": "GitHub",
                "enabled": True,
                "installed": True,
                "version": "0.1.0",
            }
        ],
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.plugins_command(update, SimpleNamespace(user_data={}))

    assert replies
    assert "Codex Plugins" in replies[0]
    assert "GitHub" in replies[0]
    assert "enabled" in replies[0].lower()


@pytest.mark.asyncio
async def test_plugins_command_enable_updates_codex_config(monkeypatch):
    update = _make_update("/plugins enable github")
    replies: list[str] = []
    calls: list[tuple[str, bool]] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: True)
    monkeypatch.setattr(
        bot,
        "_load_codex_plugin_inventory",
        lambda: [
            {
                "plugin_id": "github@openai-curated",
                "name": "github",
                "display_name": "GitHub",
                "enabled": False,
                "installed": True,
                "version": "0.1.0",
            }
        ],
    )
    monkeypatch.setattr(
        bot,
        "_set_codex_plugin_enabled",
        lambda plugin_id, enabled: calls.append((plugin_id, enabled)) or (True, ""),
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.plugins_command(update, SimpleNamespace(user_data={}))

    assert calls == [("github@openai-curated", True)]
    assert any("Enabled Codex plugin `github@openai-curated`" in text for text in replies)


@pytest.mark.asyncio
async def test_plugins_command_search_lists_marketplace_matches(monkeypatch):
    update = _make_update("/plugins search can")
    replies: list[str] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: True)
    monkeypatch.setattr(
        bot,
        "_search_codex_marketplace_plugins",
        lambda query, limit=12: [
            {
                "plugin_id": "canva@openai-curated",
                "name": "canva",
                "display_name": "Canva",
                "category": "Design",
                "marketplace_name": "openai-curated",
            }
        ],
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.plugins_command(update, SimpleNamespace(user_data={}))

    assert replies
    assert "Marketplace Matches" in replies[0]
    assert "canva@openai-curated" in replies[0]


@pytest.mark.asyncio
async def test_plugins_command_install_copies_marketplace_plugin(monkeypatch):
    update = _make_update("/plugins install canva")
    replies: list[str] = []
    install_calls: list[str] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: True)
    monkeypatch.setattr(
        bot,
        "_load_codex_plugin_inventory",
        lambda: [],
    )
    monkeypatch.setattr(
        bot,
        "_search_codex_marketplace_plugins",
        lambda query, limit=12: [
            {
                "plugin_id": "canva@openai-curated",
                "name": "canva",
                "display_name": "Canva",
                "category": "Design",
                "marketplace_name": "openai-curated",
            }
        ],
    )
    monkeypatch.setattr(
        bot,
        "_install_codex_marketplace_plugin",
        lambda plugin_id: install_calls.append(plugin_id) or (True, ""),
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.plugins_command(update, SimpleNamespace(user_data={}))

    assert install_calls == ["canva@openai-curated"]
    assert any("Installed Codex plugin `canva@openai-curated`" in text for text in replies)


@pytest.mark.asyncio
async def test_plugins_command_marketplace_add_runs_cli(monkeypatch):
    update = _make_update("/plugins marketplace add owner/repo")
    replies: list[str] = []
    calls: list[list[str]] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: True)
    monkeypatch.setattr(
        bot,
        "_run_codex_marketplace_command",
        lambda argv: calls.append(list(argv)) or (True, "ok"),
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.plugins_command(update, SimpleNamespace(user_data={}))

    assert calls == [["add", "owner/repo"]]
    assert any("Marketplace command succeeded" in text for text in replies)


@pytest.mark.asyncio
async def test_plugins_command_install_id_with_source_path(monkeypatch):
    update = _make_update('/plugins install-id chrome@openai-bundled "/tmp/chrome plugin"')
    replies: list[str] = []
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: True)
    monkeypatch.setattr(
        bot,
        "_install_codex_plugin_from_source",
        lambda plugin_id, source_path: calls.append((plugin_id, source_path)) or (True, ""),
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.plugins_command(update, SimpleNamespace(user_data={}))

    assert calls == [("chrome@openai-bundled", "/tmp/chrome plugin")]
    assert any("Installed Codex plugin `chrome@openai-bundled`" in text for text in replies)


@pytest.mark.asyncio
async def test_plugins_command_rejects_non_admin(monkeypatch):
    update = _make_update("/plugins disable github")
    replies: list[str] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: False)

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.plugins_command(update, SimpleNamespace(user_data={}))

    assert replies == ["❌ Only admins can manage Codex plugins."]


def test_main_dispatches_plugins_command_before_runtime_import(monkeypatch):
    from types import ModuleType
    import sys
    import coco.main as main_mod

    command_cli_calls: list[list[str]] = []

    command_cli_module = ModuleType("coco.command_cli")
    command_cli_module.main = lambda argv=None: command_cli_calls.append(list(argv or [])) or 0

    config_module = ModuleType("coco.config")

    def _fail_config(name: str):
        raise AssertionError(f"config import should not happen for plugins cli: {name}")

    config_module.__getattr__ = _fail_config  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "coco.command_cli", command_cli_module)
    monkeypatch.setitem(sys.modules, "coco.config", config_module)
    monkeypatch.setattr(main_mod.sys, "argv", ["coco", "plugins", "list"])

    main_mod.main()

    assert command_cli_calls == [["plugins", "list"]]
