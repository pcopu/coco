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
async def test_general_looper_status_admin_reads_canonical_control_owner_state(
    monkeypatch,
):
    owner_user_id = 100
    admin_user_id = 200
    chat_id = -100123
    update = _make_update("/looper status", thread_id=1, user_id=admin_user_id)
    replies: list[str] = []
    observed: list[tuple[str, int]] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda uid: uid == admin_user_id)
    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(owner_user_id, 1, chat_id),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda uid, *_args, **_kwargs: observed.append(("group", uid)),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_window_for_thread",
        lambda uid, *_args, **_kwargs: observed.append(("window", uid)) or "@control",
    )
    monkeypatch.setattr(
        bot,
        "get_looper_state",
        lambda *, user_id, **_kwargs: (
            observed.append(("state", user_id))
            or SimpleNamespace(interval_seconds=600, deadline_at=0, started_at=0)
        ),
    )
    monkeypatch.setattr(bot, "_build_looper_overview_text", lambda **_kwargs: "owner status")

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.looper_command(update, SimpleNamespace(user_data={}))

    assert observed == [("group", owner_user_id), ("window", owner_user_id), ("state", owner_user_id)]
    assert replies == ["owner status"]


@pytest.mark.asyncio
async def test_general_looper_start_and_stop_admin_mutate_canonical_control_owner(
    monkeypatch,
):
    owner_user_id = 100
    admin_user_id = 200
    chat_id = -100123
    observed: list[tuple[str, int]] = []
    replies: list[str] = []
    stopped = SimpleNamespace(plan_path="plans/a.md", prompt_count=2)

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda uid: uid == admin_user_id)
    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(owner_user_id, 1, chat_id),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda uid, *_args, **_kwargs: observed.append(("group", uid)),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_window_for_thread",
        lambda uid, *_args, **_kwargs: observed.append(("window", uid)) or "@control",
    )
    monkeypatch.setattr(bot.session_manager, "discover_skill_catalog", lambda: {})
    monkeypatch.setattr(bot, "build_looper_prompt", lambda **_kwargs: "prompt")

    def _start_looper(**kwargs):
        observed.append(("start", int(kwargs["user_id"])))
        return SimpleNamespace(
            plan_path="plans/a.md",
            keyword="done",
            interval_seconds=600,
            interval_max_seconds=0,
            started_at=0.0,
            deadline_at=0.0,
            instructions="",
            runner_command="",
            trigger_on_user_message=False,
        )

    def _stop_looper(**kwargs):
        observed.append(("stop", int(kwargs["user_id"])))
        return stopped

    monkeypatch.setattr(bot, "start_looper", _start_looper)
    monkeypatch.setattr(bot, "stop_looper", _stop_looper)

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.looper_command(
        _make_update("/looper start plans/a.md done", thread_id=1, user_id=admin_user_id),
        SimpleNamespace(user_data={}),
    )
    await bot.looper_command(
        _make_update("/looper stop", thread_id=1, user_id=admin_user_id),
        SimpleNamespace(user_data={}),
    )

    assert ("start", owner_user_id) in observed
    assert ("stop", owner_user_id) in observed
    assert ("start", admin_user_id) not in observed
    assert ("stop", admin_user_id) not in observed


@pytest.mark.asyncio
async def test_general_looper_denies_single_session_user_before_state_or_mutation(
    monkeypatch,
):
    owner_user_id = 100
    caller_user_id = 999
    chat_id = -100123
    update = _make_update("/looper status", thread_id=1, user_id=caller_user_id)
    replies: list[str] = []
    calls: list[str] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: False)
    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(owner_user_id, 1, chat_id),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: calls.append("group"),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_window_for_thread",
        lambda *_args, **_kwargs: calls.append("window") or "@control",
    )
    monkeypatch.setattr(
        bot,
        "get_looper_state",
        lambda **_kwargs: calls.append("state"),
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.looper_command(update, SimpleNamespace(user_data={}))

    assert calls == []
    assert replies == [
        "❌ Only the CoCo control owner or an admin can control another user's topic."
    ]


@pytest.mark.asyncio
async def test_named_looper_status_remains_caller_scoped(monkeypatch):
    caller_user_id = 999
    update = _make_update("/looper status", thread_id=77, user_id=caller_user_id)
    observed: list[tuple[str, int]] = []
    replies: list[str] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda uid, *_args, **_kwargs: observed.append(("group", uid)),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_window_for_thread",
        lambda uid, *_args, **_kwargs: observed.append(("window", uid)) or "@77",
    )
    monkeypatch.setattr(
        bot,
        "get_looper_state",
        lambda *, user_id, **_kwargs: observed.append(("state", user_id)) or None,
    )
    monkeypatch.setattr(bot, "_build_looper_overview_text", lambda **_kwargs: "named status")

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.looper_command(update, SimpleNamespace(user_data={}))

    assert observed == [("group", caller_user_id), ("window", caller_user_id), ("state", caller_user_id)]
    assert replies == ["named status"]


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


@pytest.mark.asyncio
async def test_looper_start_accepts_random_interval_range_and_on_reply(monkeypatch):
    update = _make_update("/looper start plans/a.md done --every 25m-75m --on-reply")
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
            interval_seconds=1500,
            interval_max_seconds=4500,
            started_at=100.0,
            deadline_at=0.0,
            instructions="",
            runner_command="",
            trigger_on_user_message=True,
        )

    monkeypatch.setattr(bot, "start_looper", _start_looper)
    monkeypatch.setattr(bot, "build_looper_prompt", lambda **_kwargs: "prompt")
    monkeypatch.setattr(bot.session_manager, "discover_skill_catalog", lambda: {})

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.looper_command(update, SimpleNamespace(user_data={}))

    assert start_calls
    assert start_calls[0]["interval_seconds"] == 1500
    assert start_calls[0]["interval_max_seconds"] == 4500
    assert start_calls[0]["trigger_on_user_message"] is True
    assert replies
    assert "Looper started" in replies[-1]


@pytest.mark.asyncio
async def test_looper_start_accepts_runner_mode(monkeypatch):
    update = _make_update('/looper start --runner "python tools/nudge.py" --every-random 25m 75m --on-reply')
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
            plan_path="",
            keyword="done",
            interval_seconds=1500,
            interval_max_seconds=4500,
            started_at=100.0,
            deadline_at=0.0,
            instructions="",
            runner_command="python tools/nudge.py",
            trigger_on_user_message=True,
        )

    monkeypatch.setattr(bot, "start_looper", _start_looper)
    monkeypatch.setattr(bot, "build_looper_prompt", lambda **_kwargs: "prompt")
    monkeypatch.setattr(bot.session_manager, "discover_skill_catalog", lambda: {})

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.looper_command(update, SimpleNamespace(user_data={}))

    assert start_calls
    assert start_calls[0]["runner_command"] == "python tools/nudge.py"
    assert start_calls[0]["interval_seconds"] == 1500
    assert start_calls[0]["interval_max_seconds"] == 4500
    assert start_calls[0]["trigger_on_user_message"] is True
    assert replies
    assert "Looper started" in replies[-1]
