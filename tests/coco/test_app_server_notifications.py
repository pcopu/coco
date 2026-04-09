"""Tests for app-server notification fanout in Telegram bridge."""

import pytest

import coco.bot as bot
import coco.handlers.run_watchdog as run_watchdog


@pytest.mark.asyncio
async def test_error_notification_routes_to_thread_bindings(monkeypatch):
    sent: list[tuple[int, int | None, str]] = []

    monkeypatch.setattr(
        bot.session_manager,
        "find_users_for_codex_thread",
        lambda _thread_id: [(10, None, "@1", 111), (20, None, "@2", 222)],
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_chat_id",
        lambda user_id, _thread_id, **_kwargs: -100000 - user_id,
    )

    async def _safe_send(_bot, chat_id, text, *, message_thread_id=None, **_kwargs):
        sent.append((chat_id, message_thread_id, text))

    monkeypatch.setattr(bot, "safe_send", _safe_send)

    await bot._handle_codex_app_server_notification(
        "error",
        {
            "threadId": "thr-1",
            "turnId": "turn-9",
            "willRetry": True,
            "error": {
                "message": "network timeout",
                "additionalDetails": "upstream 504",
            },
        },
        bot=object(),
    )

    assert len(sent) == 2
    assert all("Codex app-server error" in text for _chat, _tid, text in sent)
    assert all("Will retry: yes" in text for _chat, _tid, text in sent)


@pytest.mark.asyncio
async def test_config_warning_notification_broadcasts_once_per_topic(monkeypatch):
    sent: list[tuple[int, int | None, str]] = []

    monkeypatch.setattr(
        bot.session_manager,
        "iter_topic_window_bindings",
        lambda: iter(
            [
                (1, -100001, 10, "@1"),
                (2, -100002, 20, "@2"),
                (3, -100002, 20, "@9"),
            ]
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_chat_id",
        lambda _user_id, _thread_id, *, chat_id=None: chat_id if chat_id is not None else -100001,
    )

    async def _safe_send(_bot, chat_id, text, *, message_thread_id=None, **_kwargs):
        sent.append((chat_id, message_thread_id, text))

    monkeypatch.setattr(bot, "safe_send", _safe_send)

    await bot._handle_codex_app_server_notification(
        "configWarning",
        {
            "summary": "Unknown key in config",
            "details": "model.foo is ignored",
            "path": "/home/user/.codex/config.toml",
        },
        bot=object(),
    )

    # Dedupe is by (chat_id, thread_id); two users share topic 20 in same chat.
    assert len(sent) == 2
    assert any("Codex config warning" in text for _chat, _tid, text in sent)


@pytest.mark.asyncio
async def test_deprecation_notice_notification_broadcasts(monkeypatch):
    sent: list[tuple[int, int | None, str]] = []

    monkeypatch.setattr(
        bot.session_manager,
        "iter_topic_window_bindings",
        lambda: iter([(1, -100010, 10, "@1")]),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_chat_id",
        lambda _user_id, _thread_id, *, chat_id=None: chat_id if chat_id is not None else -100010,
    )

    async def _safe_send(_bot, chat_id, text, *, message_thread_id=None, **_kwargs):
        sent.append((chat_id, message_thread_id, text))

    monkeypatch.setattr(bot, "safe_send", _safe_send)

    await bot._handle_codex_app_server_notification(
        "deprecationNotice",
        {
            "summary": "Legacy endpoint will be removed soon",
            "details": "Migrate to turn/start",
        },
        bot=object(),
    )

    assert len(sent) == 1
    assert "Codex deprecation notice" in sent[0][2]


@pytest.mark.asyncio
async def test_reasoning_delta_notification_is_ignored(monkeypatch):
    handled = []

    async def _handle_new_message(msg, _bot):
        handled.append(msg)

    monkeypatch.setattr(bot, "handle_new_message", _handle_new_message)

    await bot._handle_codex_app_server_notification(
        "item/reasoning/textDelta",
        {"threadId": "th-1", "delta": "thinking token"},
        bot=object(),
    )

    assert handled == []


@pytest.mark.asyncio
async def test_agent_message_delta_notification_is_ignored(monkeypatch):
    handled = []

    async def _handle_new_message(msg, _bot):
        handled.append(msg)

    monkeypatch.setattr(bot, "handle_new_message", _handle_new_message)

    await bot._handle_codex_app_server_notification(
        "item/agentMessage/delta",
        {"threadId": "th-1", "delta": "progress token"},
        bot=object(),
    )

    assert handled == []


@pytest.mark.asyncio
async def test_item_completed_agent_message_routes_final_text(monkeypatch):
    handled = []

    async def _handle_new_message(msg, _bot):
        handled.append(msg)

    monkeypatch.setattr(bot, "handle_new_message", _handle_new_message)
    bot._turn_has_final_text.pop("th-item", None)

    await bot._handle_codex_app_server_notification(
        "item/completed",
        {
            "threadId": "th-item",
            "item": {
                "type": "agentMessage",
                "id": "msg-1",
                "text": "hello world",
            },
        },
        bot=object(),
    )

    assert len(handled) == 1
    msg = handled[0]
    assert msg.session_id == "th-item"
    assert msg.content_type == "text"
    assert msg.text == "hello world"
    assert bot._turn_has_final_text.get("th-item") is True


@pytest.mark.asyncio
async def test_raw_response_commentary_completed_routes_progress(monkeypatch):
    handled = []

    async def _handle_new_message(msg, _bot):
        handled.append(msg)

    monkeypatch.setattr(bot, "handle_new_message", _handle_new_message)

    await bot._handle_codex_app_server_notification(
        "rawResponseItem/completed",
        {
            "threadId": "th-9",
            "item": {
                "type": "message",
                "role": "assistant",
                "phase": "commentary",
                "content": [{"type": "output_text", "text": "overview update"}],
            },
        },
        bot=object(),
    )

    assert len(handled) == 1
    msg = handled[0]
    assert msg.session_id == "th-9"
    assert msg.content_type == "progress"
    assert msg.text == "overview update"


@pytest.mark.asyncio
async def test_raw_response_unknown_phase_stays_progress(monkeypatch):
    handled = []

    async def _handle_new_message(msg, _bot):
        handled.append(msg)

    monkeypatch.setattr(bot, "handle_new_message", _handle_new_message)
    bot._turn_has_final_text.pop("th-10", None)

    await bot._handle_codex_app_server_notification(
        "rawResponseItem/completed",
        {
            "threadId": "th-10",
            "item": {
                "type": "message",
                "role": "assistant",
                "phase": "tool_preamble",
                "content": [{"type": "output_text", "text": "checking files"}],
            },
        },
        bot=object(),
    )

    assert len(handled) == 1
    msg = handled[0]
    assert msg.session_id == "th-10"
    assert msg.content_type == "progress"
    assert msg.text == "checking files"
    assert bot._turn_has_final_text.get("th-10") is None


@pytest.mark.asyncio
async def test_turn_completed_finalizes_progress_and_clears_active_turn(monkeypatch):
    set_turn_calls: list[tuple[str, str]] = []
    completed: list[dict[str, object]] = []
    finalized: list[tuple[int, str, int | None]] = []
    cleared: list[tuple[int, int | None]] = []

    monkeypatch.setattr(
        bot.session_manager,
        "set_codex_turn_for_thread",
        lambda thread_id, turn_id: set_turn_calls.append((thread_id, turn_id)),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "find_users_for_codex_thread",
        lambda _thread_id: [(10, None, "@1", 111)],
    )
    monkeypatch.setattr(bot, "note_run_completed", lambda **kwargs: completed.append(kwargs))

    async def _enqueue_finalize(_bot, user_id, window_id, thread_id=None, *, compact=False):
        finalized.append((user_id, window_id, thread_id, compact))

    async def _enqueue_clear(_bot, user_id, thread_id=None):
        cleared.append((user_id, thread_id))

    async def _dispatch_next(**_kwargs):
        raise AssertionError("queue dispatch should not run for completed status")

    async def _enqueue_content(**_kwargs):
        raise AssertionError("fallback content should not be sent when final text exists")

    monkeypatch.setattr(bot, "enqueue_progress_finalize", _enqueue_finalize)
    monkeypatch.setattr(bot, "enqueue_progress_clear", _enqueue_clear)
    monkeypatch.setattr(bot, "enqueue_content_message", _enqueue_content)
    monkeypatch.setattr(bot, "queued_topic_input_count", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(bot, "_dispatch_next_queued_input", _dispatch_next)

    bot._turn_has_final_text["th-1"] = True
    await bot._handle_codex_app_server_notification(
        "turn/completed",
        {
            "threadId": "th-1",
            "turn": {"status": "completed"},
        },
        bot=object(),
    )

    assert set_turn_calls == [("th-1", "")]
    assert finalized == [(10, "@1", 111, True)]
    assert cleared == []
    assert completed and completed[0]["reason"] == "turn_completed:completed"


@pytest.mark.asyncio
async def test_turn_completed_failed_clears_progress_and_dispatches_queue(monkeypatch):
    set_turn_calls: list[tuple[str, str]] = []
    finalized: list[tuple[int, str, int | None]] = []
    cleared: list[tuple[int, int | None]] = []
    dispatched: list[dict[str, object]] = []

    monkeypatch.setattr(
        bot.session_manager,
        "set_codex_turn_for_thread",
        lambda thread_id, turn_id: set_turn_calls.append((thread_id, turn_id)),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "find_users_for_codex_thread",
        lambda _thread_id: [(10, -10010, "@1", 111)],
    )
    monkeypatch.setattr(bot, "note_run_completed", lambda **_kwargs: None)

    async def _enqueue_finalize(_bot, user_id, window_id, thread_id=None, *, compact=False):
        finalized.append((user_id, window_id, thread_id, compact))

    async def _enqueue_clear(_bot, user_id, thread_id=None):
        cleared.append((user_id, thread_id))

    async def _dispatch_next(**kwargs):
        dispatched.append(kwargs)

    monkeypatch.setattr(bot, "enqueue_progress_finalize", _enqueue_finalize)
    monkeypatch.setattr(bot, "enqueue_progress_clear", _enqueue_clear)
    monkeypatch.setattr(bot, "queued_topic_input_count", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(bot, "_dispatch_next_queued_input", _dispatch_next)

    await bot._handle_codex_app_server_notification(
        "turn/completed",
        {
            "threadId": "th-2",
            "turn": {"status": "failed"},
        },
        bot=object(),
    )

    assert set_turn_calls == [("th-2", "")]
    assert cleared == [(10, 111)]
    assert finalized == []
    assert len(dispatched) == 1
    assert dispatched[0]["thread_id"] == 111
    assert dispatched[0]["window_id"] == "@1"


@pytest.mark.asyncio
async def test_turn_completed_completed_dispatches_queued_input(monkeypatch):
    set_turn_calls: list[tuple[str, str]] = []
    finalized: list[tuple[int, str, int | None]] = []
    cleared: list[tuple[int, int | None]] = []
    dispatched: list[dict[str, object]] = []

    monkeypatch.setattr(
        bot.session_manager,
        "set_codex_turn_for_thread",
        lambda thread_id, turn_id: set_turn_calls.append((thread_id, turn_id)),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "find_users_for_codex_thread",
        lambda _thread_id: [(10, -10010, "@1", 111)],
    )
    monkeypatch.setattr(bot, "note_run_completed", lambda **_kwargs: None)

    async def _enqueue_finalize(_bot, user_id, window_id, thread_id=None, *, compact=False):
        finalized.append((user_id, window_id, thread_id, compact))

    async def _enqueue_clear(_bot, user_id, thread_id=None):
        cleared.append((user_id, thread_id))

    async def _dispatch_next(**kwargs):
        dispatched.append(kwargs)

    async def _enqueue_content(**_kwargs):
        raise AssertionError("fallback content should not be sent when final text exists")

    monkeypatch.setattr(bot, "enqueue_progress_finalize", _enqueue_finalize)
    monkeypatch.setattr(bot, "enqueue_progress_clear", _enqueue_clear)
    monkeypatch.setattr(bot, "enqueue_content_message", _enqueue_content)
    monkeypatch.setattr(bot, "queued_topic_input_count", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(bot, "_dispatch_next_queued_input", _dispatch_next)

    bot._turn_has_final_text["th-completed-q"] = True
    await bot._handle_codex_app_server_notification(
        "turn/completed",
        {
            "threadId": "th-completed-q",
            "turn": {"status": "completed"},
        },
        bot=object(),
    )

    assert set_turn_calls == [("th-completed-q", "")]
    assert finalized == [(10, "@1", 111, True)]
    assert cleared == []
    assert len(dispatched) == 1
    assert dispatched[0]["thread_id"] == 111
    assert dispatched[0]["window_id"] == "@1"


@pytest.mark.asyncio
async def test_turn_completed_failed_retries_pending_text_after_transient_stream_error(
    monkeypatch,
):
    run_watchdog.reset_run_watchdog_for_tests()
    set_turn_calls: list[tuple[str, str]] = []
    completed: list[dict[str, object]] = []
    cleared: list[tuple[int, int | None]] = []
    progress_started: list[tuple[int, str, int | None]] = []
    dispatched: list[dict[str, object]] = []
    retry_calls: list[dict[str, object]] = []
    sent: list[tuple[int, int | None, str]] = []

    monkeypatch.setattr(
        bot.session_manager,
        "set_codex_turn_for_thread",
        lambda thread_id, turn_id: set_turn_calls.append((thread_id, turn_id)),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "find_users_for_codex_thread",
        lambda _thread_id: [(10, -10010, "@1", 111)],
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_chat_id",
        lambda _user_id, _thread_id, *, chat_id=None: chat_id if chat_id is not None else -10010,
    )
    monkeypatch.setattr(bot, "note_run_completed", lambda **kwargs: completed.append(kwargs))

    async def _enqueue_clear(_bot, user_id, thread_id=None):
        cleared.append((user_id, thread_id))

    async def _enqueue_progress_start(_bot, user_id, window_id, thread_id=None):
        progress_started.append((user_id, window_id, thread_id))

    async def _dispatch_next(**kwargs):
        dispatched.append(kwargs)

    async def _send_topic_text_to_window(**kwargs):
        retry_calls.append(kwargs)
        return True, "Sent via app-server to demo"

    async def _safe_send(_bot, chat_id, text, *, message_thread_id=None, **_kwargs):
        sent.append((chat_id, message_thread_id, text))

    monkeypatch.setattr(bot, "enqueue_progress_clear", _enqueue_clear)
    monkeypatch.setattr(bot, "enqueue_progress_start", _enqueue_progress_start)
    monkeypatch.setattr(bot, "_dispatch_next_queued_input", _dispatch_next)
    monkeypatch.setattr(bot, "queued_topic_input_count", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(bot, "safe_send", _safe_send)
    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        _send_topic_text_to_window,
    )

    run_watchdog.note_run_started(
        user_id=10,
        thread_id=111,
        window_id="@1",
        source="user_input",
        pending_text="retry me",
        expect_response=True,
    )

    await bot._handle_codex_app_server_notification(
        "error",
        {
            "threadId": "th-retry",
            "turnId": "turn-retry",
            "willRetry": False,
            "error": {
                "message": (
                    "stream disconnected before completion: "
                    "An error occurred while processing your request. "
                    "Please include the request ID req-1 in your message."
                ),
            },
        },
        bot=object(),
    )

    await bot._handle_codex_app_server_notification(
        "turn/completed",
        {
            "threadId": "th-retry",
            "turn": {"status": "failed"},
        },
        bot=object(),
    )

    assert set_turn_calls == [("th-retry", "")]
    assert cleared == [(10, 111)]
    assert progress_started == [(10, "@1", 111)]
    assert len(retry_calls) == 1
    assert retry_calls[0]["user_id"] == 10
    assert retry_calls[0]["thread_id"] == 111
    assert retry_calls[0]["chat_id"] == -10010
    assert retry_calls[0]["window_id"] == "@1"
    assert retry_calls[0]["text"] == "retry me"
    assert completed == []
    assert dispatched == []
    assert any("Codex app-server error" in text for _chat, _tid, text in sent)
    assert any("Retrying last message after transient Codex stream failure" in text for _chat, _tid, text in sent)
    run_watchdog.reset_run_watchdog_for_tests()


@pytest.mark.asyncio
async def test_turn_completed_promotes_progress_when_no_final_text(monkeypatch):
    set_turn_calls: list[tuple[str, str]] = []
    finalized: list[tuple[int, str, int | None, bool]] = []
    final_content: list[dict[str, object]] = []

    monkeypatch.setattr(
        bot.session_manager,
        "set_codex_turn_for_thread",
        lambda thread_id, turn_id: set_turn_calls.append((thread_id, turn_id)),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "find_users_for_codex_thread",
        lambda _thread_id: [(10, -10010, "@1", 111)],
    )
    monkeypatch.setattr(bot, "note_run_completed", lambda **_kwargs: None)
    monkeypatch.setattr(
        bot,
        "get_progress_text",
        lambda *_args, **_kwargs: "promoted from progress",
    )

    async def _enqueue_finalize(_bot, user_id, window_id, thread_id=None, *, compact=False):
        finalized.append((user_id, window_id, thread_id, compact))

    async def _enqueue_content(**kwargs):
        final_content.append(kwargs)

    async def _dispatch_next(**_kwargs):
        raise AssertionError("queue dispatch should not run when no queued input exists")

    monkeypatch.setattr(bot, "enqueue_progress_finalize", _enqueue_finalize)
    monkeypatch.setattr(bot, "enqueue_content_message", _enqueue_content)
    monkeypatch.setattr(bot, "queued_topic_input_count", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(bot, "_dispatch_next_queued_input", _dispatch_next)

    bot._turn_has_final_text["th-fallback"] = False
    await bot._handle_codex_app_server_notification(
        "turn/completed",
        {
            "threadId": "th-fallback",
            "turn": {"status": "completed"},
        },
        bot=object(),
    )

    assert set_turn_calls == [("th-fallback", "")]
    assert finalized == [(10, "@1", 111, True)]
    assert len(final_content) == 1
    assert final_content[0]["content_type"] == "text"
    assert final_content[0]["text"] == "promoted from progress"


@pytest.mark.asyncio
async def test_turn_completed_uses_warning_when_progress_empty(monkeypatch):
    finalized: list[tuple[int, str, int | None, bool]] = []
    final_content: list[dict[str, object]] = []

    monkeypatch.setattr(
        bot.session_manager,
        "set_codex_turn_for_thread",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "find_users_for_codex_thread",
        lambda _thread_id: [(10, -10010, "@1", 111)],
    )
    monkeypatch.setattr(bot, "note_run_completed", lambda **_kwargs: None)
    monkeypatch.setattr(
        bot,
        "get_progress_text",
        lambda *_args, **_kwargs: "   ",
    )

    async def _enqueue_finalize(_bot, user_id, window_id, thread_id=None, *, compact=False):
        finalized.append((user_id, window_id, thread_id, compact))

    async def _enqueue_content(**kwargs):
        final_content.append(kwargs)

    monkeypatch.setattr(bot, "enqueue_progress_finalize", _enqueue_finalize)
    monkeypatch.setattr(bot, "enqueue_content_message", _enqueue_content)
    monkeypatch.setattr(bot, "queued_topic_input_count", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(bot, "_dispatch_next_queued_input", lambda **_kwargs: None)

    bot._turn_has_final_text["th-empty"] = False
    await bot._handle_codex_app_server_notification(
        "turn/completed",
        {"threadId": "th-empty", "turn": {"status": "completed"}},
        bot=object(),
    )

    assert finalized == [(10, "@1", 111, True)]
    assert len(final_content) == 1
    assert "without a final assistant response" in final_content[0]["text"]
