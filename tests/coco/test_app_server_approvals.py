"""Tests for app-server interactive approval bridge and inheritance hooks."""

from types import SimpleNamespace

import pytest

import coco.bot as bot


@pytest.mark.asyncio
async def test_app_server_request_auto_accepts_in_agent_mode(monkeypatch):
    telemetry: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        bot.session_manager,
        "find_users_for_codex_thread",
        lambda _thread_id: [(1147817421, None, "@1", 7)],
    )
    monkeypatch.setattr(bot, "_get_window_approval_mode", lambda _wid: bot.APPROVAL_MODE_FULL_AUTO)
    monkeypatch.setattr(
        bot,
        "emit_telemetry",
        lambda event, **fields: telemetry.append((event, fields)),
    )

    result = await bot._handle_codex_app_server_request(
        "item/commandExecution/requestApproval",
        {"threadId": "th_1", "itemId": "it_1", "turnId": "turn_1"},
        bot=object(),
    )

    assert result == {"decision": bot.APP_SERVER_APPROVAL_DECISION_ACCEPT_SESSION}
    assert [name for name, _fields in telemetry] == [
        "approval.request.received",
        "approval.request.finalized",
    ]
    assert telemetry[-1][1]["reason"] == "mode_auto_accept"
    assert telemetry[-1][1]["decision"] == bot.APP_SERVER_APPROVAL_DECISION_ACCEPT_SESSION


@pytest.mark.asyncio
async def test_app_server_request_declines_when_no_admin_bound(monkeypatch):
    sent: list[str] = []
    telemetry: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        bot.session_manager,
        "find_users_for_codex_thread",
        lambda _thread_id: [(55, None, "@1", 8)],
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, **_kwargs: -100,
    )
    monkeypatch.setattr(bot, "_get_window_approval_mode", lambda _wid: bot.APPROVAL_MODE_ON_REQUEST)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: False)

    async def _safe_send(_bot, _chat_id, text, **_kwargs):
        sent.append(text)

    monkeypatch.setattr(bot, "safe_send", _safe_send)
    monkeypatch.setattr(
        bot,
        "emit_telemetry",
        lambda event, **fields: telemetry.append((event, fields)),
    )

    result = await bot._handle_codex_app_server_request(
        "item/fileChange/requestApproval",
        {"threadId": "th_1", "itemId": "it_1", "turnId": "turn_1"},
        bot=object(),
    )

    assert result == {"decision": bot.APP_SERVER_APPROVAL_DECISION_DECLINE}
    assert sent
    assert "no admin is available" in sent[0]
    assert telemetry[-1][0] == "approval.request.finalized"
    assert telemetry[-1][1]["reason"] == "no_admin_targets"
    assert telemetry[-1][1]["decision"] == bot.APP_SERVER_APPROVAL_DECISION_DECLINE


@pytest.mark.asyncio
async def test_app_server_request_accepts_after_callback_decision(monkeypatch):
    telemetry: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        bot.session_manager,
        "find_users_for_codex_thread",
        lambda _thread_id: [(1147817421, None, "@1", 9)],
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, **_kwargs: -100,
    )
    monkeypatch.setattr(bot, "_get_window_approval_mode", lambda _wid: bot.APPROVAL_MODE_ON_REQUEST)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: True)

    async def _safe_send(_bot, _chat_id, _text, **kwargs):
        markup = kwargs.get("reply_markup")
        assert markup is not None
        callback_data = markup.inline_keyboard[0][0].callback_data
        assert callback_data is not None
        parsed = bot._parse_app_server_approval_callback(callback_data)
        assert parsed is not None
        token, _action = parsed
        assert bot._resolve_pending_app_server_approval(
            token,
            bot.APP_SERVER_APPROVAL_DECISION_ACCEPT,
        )

    monkeypatch.setattr(bot, "safe_send", _safe_send)
    monkeypatch.setattr(
        bot,
        "emit_telemetry",
        lambda event, **fields: telemetry.append((event, fields)),
    )

    result = await bot._handle_codex_app_server_request(
        "item/commandExecution/requestApproval",
        {
            "threadId": "th_2",
            "itemId": "it_2",
            "turnId": "turn_2",
            "command": "ls -la",
            "cwd": "/tmp",
        },
        bot=object(),
    )

    assert result == {"decision": bot.APP_SERVER_APPROVAL_DECISION_ACCEPT}
    assert any(name == "approval.request.prompt_sent" for name, _fields in telemetry)
    assert telemetry[-1][0] == "approval.request.finalized"
    assert telemetry[-1][1]["decision"] == bot.APP_SERVER_APPROVAL_DECISION_ACCEPT


@pytest.mark.asyncio
async def test_app_server_request_timeout_records_telemetry(monkeypatch):
    sent: list[str] = []
    telemetry: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        bot.session_manager,
        "find_users_for_codex_thread",
        lambda _thread_id: [(1147817421, None, "@1", 9)],
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, **_kwargs: -100,
    )
    monkeypatch.setattr(bot, "_get_window_approval_mode", lambda _wid: bot.APPROVAL_MODE_ON_REQUEST)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: True)

    async def _safe_send(_bot, _chat_id, text, **_kwargs):
        sent.append(text)

    async def _wait_for(_awaitable, timeout=None):
        _ = timeout
        raise TimeoutError

    monkeypatch.setattr(bot, "safe_send", _safe_send)
    monkeypatch.setattr(bot.asyncio, "wait_for", _wait_for)
    monkeypatch.setattr(
        bot,
        "emit_telemetry",
        lambda event, **fields: telemetry.append((event, fields)),
    )

    result = await bot._handle_codex_app_server_request(
        "item/commandExecution/requestApproval",
        {"threadId": "th_9", "itemId": "it_9", "turnId": "turn_9"},
        bot=object(),
    )

    assert result == {"decision": bot.APP_SERVER_APPROVAL_DECISION_DECLINE}
    assert any("timed out" in text for text in sent)
    assert telemetry[-1][0] == "approval.request.finalized"
    assert telemetry[-1][1]["reason"] == "timeout"
    assert telemetry[-1][1]["decision"] == bot.APP_SERVER_APPROVAL_DECISION_DECLINE


@pytest.mark.asyncio
async def test_create_worktree_inherits_approval_mode(monkeypatch, tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    wt_path = tmp_path / "repo-wt-demo"
    mode_state: dict[str, str] = {
        "@source": bot.APPROVAL_MODE_FULL_AUTO,
        "@new": bot.APPROVAL_MODE_INHERIT,
    }
    mode_calls: list[tuple[str, str]] = []

    class _FakeBot:
        async def create_forum_topic(self, *, chat_id: int, name: str):
            assert chat_id == -100
            assert name == "demo"
            return SimpleNamespace(message_thread_id=1234)

    monkeypatch.setattr(bot, "_can_user_create_sessions", lambda _uid: True)
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_window_for_thread",
        lambda _uid, _tid, **_kwargs: "@source",
    )
    monkeypatch.setattr(bot, "_codex_app_server_enabled", lambda: True)
    monkeypatch.setattr(bot.config, "runtime_mode", "app_server_only")
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda _uid, _tid, **_kwargs: SimpleNamespace(
            cwd=str(repo_root),
            codex_thread_id="thread-source",
        ),
    )
    monkeypatch.setattr(bot.session_manager, "allocate_virtual_window_id", lambda: "@new")

    monkeypatch.setattr(bot, "_git_repo_root", lambda _cwd: (repo_root, ""))
    monkeypatch.setattr(bot, "_sanitize_worktree_name", lambda _raw: "demo")
    monkeypatch.setattr(bot, "_pick_worktree_path", lambda _root, _slug: wt_path)
    monkeypatch.setattr(bot, "_run_git", lambda *_args, **_kwargs: (True, "", ""))

    async def _ensure_codex_thread_for_window(*, window_id: str, cwd: str):
        assert window_id == "@new"
        assert cwd == str(wt_path)
        return "thread-new", ""

    monkeypatch.setattr(
        bot.session_manager,
        "_ensure_codex_thread_for_window",
        _ensure_codex_thread_for_window,
    )
    monkeypatch.setattr(
        bot,
        "_get_window_approval_mode",
        lambda wid: mode_state.get(wid, bot.APPROVAL_MODE_INHERIT),
    )

    async def _apply_window_approval_mode(window_id: str, mode: str):
        mode_calls.append((window_id, mode))
        mode_state[window_id] = mode
        return True, ""

    monkeypatch.setattr(bot, "_apply_window_approval_mode", _apply_window_approval_mode)
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, **_kwargs: -100,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "bind_topic_to_codex_thread",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(bot.session_manager, "bind_thread", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )
    async def _build_handoff(_source_wid: str, _thread_id: int) -> str:
        return "handoff"

    async def _sleep(_seconds: float) -> None:
        return None

    async def _send_to_window(_wid: str, _text: str):
        return True, ""

    monkeypatch.setattr(bot, "_build_worktree_handoff_prompt", _build_handoff)
    monkeypatch.setattr(bot.asyncio, "sleep", _sleep)
    monkeypatch.setattr(bot.session_manager, "send_to_window", _send_to_window)
    monkeypatch.setattr(bot, "note_run_started", lambda **_kwargs: None)

    sent_messages: list[str] = []

    async def _safe_send(_bot, _chat_id, text, **_kwargs):
        sent_messages.append(text)

    monkeypatch.setattr(bot, "safe_send", _safe_send)

    ok, msg = await bot._create_worktree_from_topic(
        bot=_FakeBot(),
        user_id=1147817421,
        thread_id=77,
        worktree_name="demo",
    )

    assert ok is True
    assert "Created worktree `demo`" in msg
    assert mode_calls == [("@new", bot.APPROVAL_MODE_FULL_AUTO)]
    assert sent_messages
    assert "Approvals: `agent (full-auto)`" in sent_messages[0]
