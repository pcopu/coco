"""Tests for /resume lifecycle command behavior (menu-only)."""

from types import SimpleNamespace

import pytest

import coco.bot as bot


def _make_update(*, text: str = "/resume", thread_id: int = 77, user_id: int = 1147817421):
    chat = SimpleNamespace(type="supergroup", id=-100123)
    message = SimpleNamespace(text=text, message_thread_id=thread_id, chat=chat)
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_message=message,
        effective_chat=chat,
        message=message,
    )
async def test_resume_command_with_text_args_shows_menu_only_notice(monkeypatch):
    update = _make_update(text="/resume rollback 3")
    replies: list[tuple[str, object | None]] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "session_provider", "codex")
    monkeypatch.setattr(bot, "_codex_app_server_preferred", lambda: True)
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_window_for_thread",
        lambda _uid, _tid, **_kwargs: "@1",
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_state",
        lambda _wid: SimpleNamespace(codex_thread_id="th_current", codex_active_turn_id=""),
    )
    monkeypatch.setattr(bot.codex_app_server_client, "get_active_turn_id", lambda _tid: "turn_1")
    monkeypatch.setattr(bot.session_manager, "get_display_name", lambda _wid: "demo")

    async def _thread_rollback(*, thread_id: str, num_turns: int | None = None, turn_id: str | None = None):
        raise AssertionError("text rollback path should be disabled")

    monkeypatch.setattr(bot.codex_app_server_client, "thread_rollback", _thread_rollback)

    async def _thread_list(*, cursor: str | None = None, limit: int = 20):
        _ = cursor, limit
        return {"threads": [{"id": "th_current"}, {"id": "th_other"}]}

    monkeypatch.setattr(bot.codex_app_server_client, "thread_list", _thread_list)

    async def _safe_reply(_message, text: str, **kwargs):
        replies.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.resume_command(update, SimpleNamespace(user_data={}))

    assert replies
    text, markup = replies[0]
    assert "Text subcommands are disabled" in text
    assert "Session Lifecycle" in text
    assert markup is not None
async def test_resume_command_without_args_shows_interactive_panel(monkeypatch):
    update = _make_update(text="/resume")
    replies: list[tuple[str, object | None]] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "session_provider", "codex")
    monkeypatch.setattr(bot, "_codex_app_server_preferred", lambda: True)
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_window_for_thread",
        lambda _uid, _tid, **_kwargs: "@1",
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_state",
        lambda _wid: SimpleNamespace(codex_thread_id="th_current", codex_active_turn_id=""),
    )
    monkeypatch.setattr(bot.codex_app_server_client, "get_active_turn_id", lambda _tid: "turn_1")
    monkeypatch.setattr(bot.session_manager, "get_display_name", lambda _wid: "demo")

    async def _thread_list(*, cursor: str | None = None, limit: int = 20):
        _ = cursor, limit
        return {"threads": [{"id": "th_current"}, {"id": "th_other"}]}

    monkeypatch.setattr(bot.codex_app_server_client, "thread_list", _thread_list)

    async def _safe_reply(_message, text: str, **kwargs):
        replies.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    context = SimpleNamespace(user_data={})
    await bot.resume_command(update, context)

    assert replies
    text, markup = replies[0]
    assert "Session Lifecycle" in text
    assert "Recent resumable threads" in text
    assert markup is not None
async def test_resume_command_without_args_loads_all_threads_via_pagination(monkeypatch):
    update = _make_update(text="/resume")
    replies: list[tuple[str, object | None]] = []
    calls: list[tuple[str | None, int]] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "session_provider", "codex")
    monkeypatch.setattr(bot, "_codex_app_server_preferred", lambda: True)
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_window_for_thread",
        lambda _uid, _tid, **_kwargs: "@1",
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_state",
        lambda _wid: SimpleNamespace(codex_thread_id="th_current", codex_active_turn_id=""),
    )
    monkeypatch.setattr(bot.codex_app_server_client, "get_active_turn_id", lambda _tid: "turn_1")
    monkeypatch.setattr(bot.session_manager, "get_display_name", lambda _wid: "demo")

    async def _thread_list(*, cursor: str | None = None, limit: int = 20):
        calls.append((cursor, limit))
        if cursor is None:
            return {
                "threads": [{"id": "th_current"}, {"id": "th_other"}],
                "nextCursor": "cursor-2",
            }
        if cursor == "cursor-2":
            return {"threads": [{"id": "th_third"}]}
        return {"threads": []}

    monkeypatch.setattr(bot.codex_app_server_client, "thread_list", _thread_list)

    async def _safe_reply(_message, text: str, **kwargs):
        replies.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    context = SimpleNamespace(user_data={})
    await bot.resume_command(update, context)

    assert replies
    picker = context.user_data.get(bot.SESSION_PICKER_THREADS_KEY)
    assert isinstance(picker, dict)
    assert picker.get("items") == ["th_current", "th_other", "th_third"]
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_resume_command_uses_remote_machine_thread_listing(monkeypatch):
    update = _make_update(text="/resume")
    replies: list[tuple[str, object | None]] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "session_provider", "codex")
    monkeypatch.setattr(bot, "_codex_app_server_preferred", lambda: True)
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_window_for_thread",
        lambda _uid, _tid, **_kwargs: "@1",
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_state",
        lambda _wid: SimpleNamespace(codex_thread_id="th_remote", codex_active_turn_id=""),
    )
    monkeypatch.setattr(bot.session_manager, "get_window_machine_id", lambda _wid: "remote-node")
    monkeypatch.setattr(bot.codex_app_server_client, "get_active_turn_id", lambda _tid: "turn_1")
    monkeypatch.setattr(bot.session_manager, "get_display_name", lambda _wid: "demo")

    async def _list_threads(_machine_id: str, *, max_items: int = 300):
        assert _machine_id == "remote-node"
        assert max_items == 300
        return ["th_remote", "th_other"], ""

    monkeypatch.setattr("coco.agent_rpc.agent_rpc_client.list_threads", _list_threads)

    async def _safe_reply(_message, text: str, **kwargs):
        replies.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    context = SimpleNamespace(user_data={})
    await bot.resume_command(update, context)

    assert replies
    picker = context.user_data.get(bot.SESSION_PICKER_THREADS_KEY)
    assert isinstance(picker, dict)
    assert picker.get("items") == ["th_remote", "th_other"]
