"""Tests for /apps and /skills command behavior."""

from pathlib import Path
from types import SimpleNamespace

import pytest

import coco.bot as bot
from coco.skills import SkillDefinition


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


def _demo_skill() -> SkillDefinition:
    return SkillDefinition(
        name="demo",
        description="Demo skill",
        skill_md_path=Path("/tmp/demo/SKILL.md"),
        source_root=Path("/tmp"),
        folder_name="demo",
    )


@pytest.mark.asyncio
async def test_apps_command_lists_catalog(monkeypatch):
    update = _make_update("/apps")
    replies: list[str] = []
    catalog = {"demo": _demo_skill()}

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(bot.session_manager, "discover_skill_catalog", lambda: catalog)
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_thread_skills",
        lambda *_args, **_kwargs: [catalog["demo"]],
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.apps_command(update, SimpleNamespace(user_data={}))

    assert replies
    assert "Topic Apps" in replies[0]
    assert "`demo`" in replies[0]


@pytest.mark.asyncio
async def test_apps_command_enable_updates_topic_state(monkeypatch):
    update = _make_update("/apps enable demo")
    replies: list[str] = []
    catalog = {"demo": _demo_skill()}
    enabled_names: list[str] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
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
            slice(None),
            list(names),
        ),
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.apps_command(update, SimpleNamespace(user_data={}))

    assert enabled_names == ["demo"]
    assert any("Enabled app `demo`" in text for text in replies)


@pytest.mark.asyncio
async def test_apps_command_disable_removes_skill(monkeypatch):
    update = _make_update("/apps disable demo")
    replies: list[str] = []
    catalog = {"demo": _demo_skill()}
    enabled_names: list[str] = ["demo"]

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
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
            slice(None),
            list(names),
        ),
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.apps_command(update, SimpleNamespace(user_data={}))

    assert enabled_names == []
    assert any("Disabled app `demo`" in text for text in replies)


@pytest.mark.asyncio
async def test_skills_command_lists_codex_catalog(monkeypatch):
    update = _make_update("/skills")
    replies: list[str] = []
    catalog = {"demo": _demo_skill()}

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "discover_codex_skill_catalog",
        lambda: catalog,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_thread_codex_skills",
        lambda *_args, **_kwargs: [catalog["demo"]],
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.skills_command(update, SimpleNamespace(user_data={}))

    assert replies
    assert "Codex Skills" in replies[0]
    assert "`demo`" in replies[0]


@pytest.mark.asyncio
async def test_skills_command_enable_updates_topic_state(monkeypatch):
    update = _make_update("/skills enable demo")
    replies: list[str] = []
    catalog = {"demo": _demo_skill()}
    enabled_names: list[str] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "discover_codex_skill_catalog",
        lambda: catalog,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_thread_codex_skills",
        lambda *_args, **_kwargs: [catalog[name] for name in enabled_names],
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_thread_codex_skills",
        lambda _uid, _tid, names, **_kwargs: enabled_names.__setitem__(
            slice(None),
            list(names),
        ),
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.skills_command(update, SimpleNamespace(user_data={}))

    assert enabled_names == ["demo"]
    assert any("Enabled Codex skill `demo`" in text for text in replies)


@pytest.mark.asyncio
async def test_skills_command_disable_removes_skill(monkeypatch):
    update = _make_update("/skills disable demo")
    replies: list[str] = []
    catalog = {"demo": _demo_skill()}
    enabled_names: list[str] = ["demo"]

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "discover_codex_skill_catalog",
        lambda: catalog,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_thread_codex_skills",
        lambda *_args, **_kwargs: [catalog[name] for name in enabled_names],
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_thread_codex_skills",
        lambda _uid, _tid, names, **_kwargs: enabled_names.__setitem__(
            slice(None),
            list(names),
        ),
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.skills_command(update, SimpleNamespace(user_data={}))

    assert enabled_names == []
    assert any("Disabled Codex skill `demo`" in text for text in replies)
