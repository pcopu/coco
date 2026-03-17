"""Tests for /looper command behavior."""

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


@pytest.mark.asyncio
async def test_looper_start_parses_options_and_shows_example(monkeypatch):
    update = _make_update(
        '/looper start plans/ship.md done --every 15m --limit 1h --instructions "focus tests first"'
    )
    replies: list[str] = []
    set_skills_calls: list[list[str]] = []
    start_calls: list[dict[str, object]] = []

    skill = SkillDefinition(
        name="looper",
        description="loop helper",
        skill_md_path=Path("/tmp/apps/looper/SKILL.md"),
        source_root=Path("/tmp/apps"),
        folder_name="looper",
    )

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
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

    def _start_looper(**kwargs):
        start_calls.append(kwargs)
        return SimpleNamespace(
            plan_path="plans/ship.md",
            keyword="done",
            interval_seconds=900,
            started_at=100.0,
            deadline_at=3700.0,
            instructions="focus tests first",
        )

    monkeypatch.setattr(bot, "start_looper", _start_looper)
    monkeypatch.setattr(
        bot,
        "build_looper_prompt",
        lambda **_kwargs: "example loop prompt",
    )
    monkeypatch.setattr(bot.session_manager, "discover_skill_catalog", lambda: {"looper": skill})
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_thread_skills",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_thread_skills",
        lambda _uid, _tid, names, **_kwargs: set_skills_calls.append(list(names)),
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.looper_command(update, SimpleNamespace(user_data={}))

    assert start_calls
    call = start_calls[0]
    assert call["plan_path"] == "plans/ship.md"
    assert call["keyword"] == "done"
    assert call["interval_seconds"] == 900
    assert call["limit_seconds"] == 3600
    assert call["instructions"] == "focus tests first"

    assert set_skills_calls == [["looper"]]
    assert replies
    assert "Looper started" in replies[-1]
    assert "Example nudge" in replies[-1]
    assert "example loop prompt" in replies[-1]


@pytest.mark.asyncio
async def test_looper_stop_when_already_off(monkeypatch):
    update = _make_update("/looper stop")
    replies: list[str] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
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

    monkeypatch.setattr(bot, "stop_looper", lambda **_kwargs: None)

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.looper_command(update, SimpleNamespace(user_data={}))

    assert replies
    assert "already off" in replies[-1].lower()


@pytest.mark.asyncio
async def test_looper_start_accepts_spaced_duration_units(monkeypatch):
    update = _make_update("/looper start plans/a.md done --every 10 minutes --limit 1 hour")
    replies: list[str] = []
    start_calls: list[dict[str, object]] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
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

    def _start_looper(**kwargs):
        start_calls.append(kwargs)
        return SimpleNamespace(
            plan_path="plans/a.md",
            keyword="done",
            interval_seconds=600,
            started_at=100.0,
            deadline_at=3700.0,
            instructions="",
        )

    monkeypatch.setattr(bot, "start_looper", _start_looper)
    monkeypatch.setattr(bot, "build_looper_prompt", lambda **_kwargs: "prompt")
    monkeypatch.setattr(bot.session_manager, "discover_skill_catalog", lambda: {})

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.looper_command(update, SimpleNamespace(user_data={}))

    assert start_calls
    assert start_calls[0]["interval_seconds"] == 600
    assert start_calls[0]["limit_seconds"] == 3600
    assert replies
    assert "Looper started" in replies[-1]
