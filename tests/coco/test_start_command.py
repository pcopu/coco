"""Tests for /start command behavior across runtime transport modes."""

from types import SimpleNamespace

import pytest

import coco.bot as bot
from coco.handlers import commands


def _make_update(*, text: str = "/start", thread_id: int | None = 777):
    class _Chat:
        type = "supergroup"
        id = -100123

    message = SimpleNamespace(
        text=text,
        chat=_Chat(),
        message_thread_id=thread_id,
        is_topic_message=thread_id is not None,
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=1147817421),
        effective_message=message,
        effective_chat=message.chat,
        message=message,
    )


@pytest.mark.asyncio
async def test_start_command_app_server_only_skips_legacy_window_listing(monkeypatch):
    replies: list[tuple[str, object | None]] = []
    update = _make_update()
    context = SimpleNamespace(bot=object(), user_data={})

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_get_thread_id", lambda _update: 777)
    monkeypatch.setattr(bot, "clear_browse_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager, "set_group_chat_id", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_for_thread",
        lambda _uid, _tid, **_kwargs: None,
    )
    monkeypatch.setattr(bot, "_can_user_create_sessions", lambda _uid: True)
    monkeypatch.setattr(bot, "_codex_app_server_enabled", lambda: True)
    monkeypatch.setattr(bot.config, "runtime_mode", "app_server_only")
    monkeypatch.setattr(
        bot,
        "_sorted_machine_choices",
        lambda: [SimpleNamespace(machine_id="local", display_name="Local", status="online")],
    )
    monkeypatch.setattr(bot, "_local_machine_identity", lambda: ("local", "Local"))

    monkeypatch.setattr(bot, "resolve_browse_root", lambda _root: "/tmp")
    monkeypatch.setattr(
        bot,
        "build_directory_browser",
        lambda *_args, **_kwargs: ("browse", "keyboard", ["a"]),
    )

    async def _safe_reply(_message, text: str, reply_markup=None, **_kwargs):
        replies.append((text, reply_markup))

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await commands.start_command(update, context)

    assert replies == [("browse", "keyboard")]
    assert context.user_data[bot.STATE_KEY] == bot.STATE_BROWSING_DIRECTORY
    assert context.user_data["_pending_thread_id"] == 777


@pytest.mark.asyncio
async def test_start_command_app_server_only_accepts_existing_non_legacy_binding(
    monkeypatch,
):
    replies: list[str] = []
    update = _make_update()
    context = SimpleNamespace(bot=object(), user_data={})

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_get_thread_id", lambda _update: 777)
    monkeypatch.setattr(bot, "clear_browse_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager, "set_group_chat_id", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_for_thread",
        lambda _uid, _tid, **_kwargs: "@900000",
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda _uid, _tid, **_kwargs: SimpleNamespace(
            codex_thread_id="thread-1",
            cwd="/tmp/demo",
            display_name="demo",
        ),
    )
    monkeypatch.setattr(bot, "_codex_app_server_enabled", lambda: True)
    monkeypatch.setattr(bot.config, "runtime_mode", "app_server_only")
    monkeypatch.setattr(
        bot,
        "_sorted_machine_choices",
        lambda: [SimpleNamespace(machine_id="local", display_name="Local", status="online")],
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await commands.start_command(update, context)

    assert len(replies) == 1
    assert "already bound" in replies[0]


@pytest.mark.asyncio
async def test_folder_command_aliases_start_command(monkeypatch):
    update = _make_update(text="/folder")
    context = SimpleNamespace(bot=object(), user_data={})
    calls: list[tuple[object, object]] = []

    async def _start_command(_update, _context):
        calls.append((_update, _context))

    monkeypatch.setattr(commands, "start_command", _start_command)

    await commands.folder_command(update, context)

    assert calls == [(update, context)]


@pytest.mark.asyncio
async def test_start_command_shows_machine_picker_when_multiple_nodes_available(monkeypatch):
    replies: list[tuple[str, object | None]] = []
    update = _make_update()
    context = SimpleNamespace(bot=object(), user_data={})

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_get_thread_id", lambda _update: 777)
    monkeypatch.setattr(bot, "clear_browse_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager, "set_group_chat_id", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_for_thread",
        lambda _uid, _tid, **_kwargs: None,
    )
    monkeypatch.setattr(bot, "_can_user_create_sessions", lambda _uid: True)
    monkeypatch.setattr(bot, "_sorted_machine_choices", lambda: [
        SimpleNamespace(machine_id="local", display_name="Local", status="online"),
        SimpleNamespace(machine_id="remote", display_name="Remote", status="online"),
    ])

    async def _safe_reply(_message, text: str, reply_markup=None, **_kwargs):
        replies.append((text, reply_markup))

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await commands.start_command(update, context)

    assert len(replies) == 1
    assert "Select Machine" in replies[0][0]
    assert context.user_data[bot.STATE_KEY] == bot.STATE_PICKING_MACHINE
