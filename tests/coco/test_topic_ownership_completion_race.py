"""Regression tests for completion delivery after a topic unbind/rebind race."""

import asyncio
from types import SimpleNamespace

import pytest

import coco.bot as bot
import coco.handlers.message_queue as mq
from coco.session import SessionManager
from coco.session_monitor import NewMessage


@pytest.fixture
def mgr(monkeypatch) -> SessionManager:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    return SessionManager()


def _bind_topic(
    mgr: SessionManager,
    *,
    user_id: int,
    thread_id: int,
    chat_id: int,
    codex_thread_id: str,
) -> None:
    mgr.bind_topic_to_codex_thread(
        user_id=user_id,
        thread_id=thread_id,
        chat_id=chat_id,
        codex_thread_id=codex_thread_id,
        window_id="@1",
        cwd="/tmp/demo",
        display_name="demo",
    )


@pytest.mark.asyncio
async def test_turn_completed_drops_stale_output_when_later_entry_captures_none(
    monkeypatch,
    mgr: SessionManager,
):
    """A completion captured after unbind must not be delivered to a new B binding."""
    user_id = 992001
    telegram_thread_id = 21
    chat_id = -100021
    codex_thread_id = "codex-A"
    _bind_topic(
        mgr,
        user_id=user_id,
        thread_id=telegram_thread_id,
        chat_id=chat_id,
        codex_thread_id=codex_thread_id,
    )
    monkeypatch.setattr(bot, "session_manager", mgr)
    monkeypatch.setattr(mq, "session_manager", mgr)
    monkeypatch.setattr(
        mgr,
        "set_codex_turn_for_thread",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot,
        "_find_codex_thread_bindings_for_source",
        lambda *_args, **_kwargs: [
            (user_id, chat_id, "@1", telegram_thread_id),
            (user_id, chat_id, "@1", telegram_thread_id),
        ],
    )
    monkeypatch.setattr(bot, "note_run_completed", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "get_progress_text", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(
        bot,
        "build_response_parts",
        lambda text, *_args, **_kwargs: [text],
    )
    monkeypatch.setattr(bot, "queued_topic_input_count", lambda *_args, **_kwargs: 0)

    original_capture = mq.capture_topic_ownership
    capture_calls = 0

    def _capture_and_rebind_after_unbind(*args, **kwargs):
        nonlocal capture_calls
        capture_calls += 1
        ownership = original_capture(*args, **kwargs)
        if capture_calls == 2:
            # The first loop entry was awaiting progress finalization while
            # this topic was unbound.  The later stale entry sees no owner;
            # model a new /resume binding immediately after that capture.
            assert ownership is None
            _bind_topic(
                mgr,
                user_id=user_id,
                thread_id=telegram_thread_id,
                chat_id=chat_id,
                codex_thread_id="codex-B",
            )
        return ownership

    monkeypatch.setattr(
        bot, "capture_topic_ownership", _capture_and_rebind_after_unbind
    )

    first_finalize_started = asyncio.Event()
    release_first_finalize = asyncio.Event()
    finalized: list[tuple[int, int | None]] = []

    async def _enqueue_finalize(
        _bot,
        user_id,
        window_id,
        thread_id=None,
        *,
        compact=False,
        chat_id=None,
    ):
        _ = window_id, compact, chat_id
        if not finalized:
            first_finalize_started.set()
            await release_first_finalize.wait()
        finalized.append((user_id, thread_id))

    monkeypatch.setattr(bot, "enqueue_progress_finalize", _enqueue_finalize)

    delivered: list[dict[str, object]] = []

    async def _enqueue_content(**kwargs):
        binding = mgr.resolve_topic_binding(
            user_id,
            telegram_thread_id,
            chat_id=chat_id,
        )
        delivered.append(
            {
                "text": kwargs["text"],
                "captured_ownership": kwargs.get("topic_ownership"),
                "current_codex_thread_id": (
                    binding.codex_thread_id if binding is not None else None
                ),
            }
        )

    monkeypatch.setattr(bot, "enqueue_content_message", _enqueue_content)
    monkeypatch.setattr(bot, "_dispatch_next_queued_input", lambda **_kwargs: None)

    bot._turn_has_final_text[codex_thread_id] = False
    handling = asyncio.create_task(
        bot._handle_codex_app_server_notification(
            "turn/completed",
            {
                "threadId": codex_thread_id,
                "turn": {"status": "completed"},
            },
            bot=object(),
        )
    )
    await asyncio.wait_for(first_finalize_started.wait(), timeout=1)

    mgr.unbind_topic(user_id, telegram_thread_id, chat_id=chat_id)
    release_first_finalize.set()
    await handling

    # A missing owner snapshot is stale even though the topic is now rebound
    # to B; no output from A may be enqueued.
    assert delivered == []


@pytest.mark.asyncio
async def test_handle_new_message_drops_stale_output_when_later_entry_captures_none(
    monkeypatch,
    mgr: SessionManager,
):
    """Transcript-monitor completion must also fence a missing topic owner."""
    user_id = 992002
    telegram_thread_id = 22
    chat_id = -100022
    codex_thread_id = "codex-A-transcript"
    _bind_topic(
        mgr,
        user_id=user_id,
        thread_id=telegram_thread_id,
        chat_id=chat_id,
        codex_thread_id=codex_thread_id,
    )
    monkeypatch.setattr(bot, "session_manager", mgr)
    monkeypatch.setattr(mq, "session_manager", mgr)
    monkeypatch.setattr(bot, "_codex_app_server_enabled", lambda: True)
    monkeypatch.setattr(
        bot,
        "_find_codex_thread_bindings_for_source",
        lambda *_args, **_kwargs: [
            (user_id, chat_id, "@1", telegram_thread_id),
            (user_id, chat_id, "@1", telegram_thread_id),
        ],
    )
    monkeypatch.setattr(
        bot, "_resolve_workspace_dir_for_window", lambda **_kwargs: "/tmp/demo"
    )
    monkeypatch.setattr(bot, "note_run_activity", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "note_run_completed", lambda **_kwargs: None)
    monkeypatch.setattr(
        bot, "build_response_parts", lambda text, *_args, **_kwargs: [text]
    )
    monkeypatch.setattr(
        bot, "consume_looper_completion_keyword", lambda **_kwargs: None
    )
    monkeypatch.setattr(bot, "queued_topic_input_count", lambda *_args, **_kwargs: 0)

    async def _enqueue_finalize(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bot, "enqueue_progress_finalize", _enqueue_finalize)

    async def _update_offset(**_kwargs):
        return None

    monkeypatch.setattr(bot, "_update_user_read_offset_for_window", _update_offset)

    async def _shadow_passthrough(**_kwargs):
        return False

    monkeypatch.setattr(
        bot, "_handle_shadow_transcript_message_for_topic", _shadow_passthrough
    )

    original_capture = mq.capture_topic_ownership
    capture_calls = 0

    def _capture_and_rebind_after_unbind(*args, **kwargs):
        nonlocal capture_calls
        capture_calls += 1
        ownership = original_capture(*args, **kwargs)
        if capture_calls == 2:
            assert ownership is None
            _bind_topic(
                mgr,
                user_id=user_id,
                thread_id=telegram_thread_id,
                chat_id=chat_id,
                codex_thread_id="codex-B-transcript",
            )
        return ownership

    monkeypatch.setattr(
        bot, "capture_topic_ownership", _capture_and_rebind_after_unbind
    )

    first_attachment_started = asyncio.Event()
    release_first_attachment = asyncio.Event()
    extraction_calls = 0

    async def _extract_attachments(text, *, workspace_dir, window_id):
        nonlocal extraction_calls
        _ = workspace_dir, window_id
        extraction_calls += 1
        if extraction_calls == 1:
            first_attachment_started.set()
            await release_first_attachment.wait()
        return text, None, None, None

    monkeypatch.setattr(
        bot, "_extract_telegram_attachments_for_window", _extract_attachments
    )

    delivered: list[dict[str, object]] = []

    async def _enqueue_content(**kwargs):
        binding = mgr.resolve_topic_binding(
            user_id,
            telegram_thread_id,
            chat_id=chat_id,
        )
        delivered.append(
            {
                "text": kwargs["text"],
                "captured_ownership": kwargs.get("topic_ownership"),
                "current_codex_thread_id": (
                    binding.codex_thread_id if binding is not None else None
                ),
            }
        )

    monkeypatch.setattr(bot, "enqueue_content_message", _enqueue_content)

    handling = asyncio.create_task(
        bot.handle_new_message(
            NewMessage(
                session_id=codex_thread_id,
                text="late transcript answer from A",
                is_complete=True,
                content_type="text",
                role="assistant",
                source="transcript",
            ),
            SimpleNamespace(),
        )
    )
    await asyncio.wait_for(first_attachment_started.wait(), timeout=1)

    mgr.unbind_topic(user_id, telegram_thread_id, chat_id=chat_id)
    release_first_attachment.set()
    await handling

    # A missing owner snapshot is stale even though the topic is now rebound
    # to B; no output from A may be enqueued.
    assert delivered == []
