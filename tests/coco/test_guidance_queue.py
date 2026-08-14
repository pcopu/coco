"""Tests for steer/queued message helper state."""

import asyncio
import contextlib
from types import SimpleNamespace

import pytest
from telegram.error import RetryAfter

import coco.bot as bot
import coco.codex_app_server as cas
import coco.handlers.message_queue as mq
import coco.session as session_module
from coco.session import SessionManager


_TEST_TOPIC_OWNERSHIP = mq.TopicOwnership(
    window_id="@test-owner",
    codex_thread_id="test-codex-thread",
    machine_id="test-machine",
    cwd="/test/workspace",
)


def _enqueue_test_topic_input(*args, capture_current: bool = False, **kwargs):
    """Seed queue fixtures with explicit ownership unless a test exercises capture."""
    if "topic_ownership" not in kwargs and not capture_current:
        kwargs["topic_ownership"] = _TEST_TOPIC_OWNERSHIP
    return mq.enqueue_queued_topic_input(*args, **kwargs)


@pytest.fixture(autouse=True)
def _keep_synthetic_queue_owner_current(monkeypatch):
    original_bot_current = bot.is_topic_ownership_current
    original_queue_current = mq.is_topic_ownership_current
    original_queue_capture = mq.capture_topic_ownership

    def _bot_is_current(user_id, thread_id, chat_id, ownership):
        if ownership == _TEST_TOPIC_OWNERSHIP:
            return True
        return original_bot_current(user_id, thread_id, chat_id, ownership)

    def _queue_is_current(user_id, thread_id, chat_id, ownership):
        if ownership == _TEST_TOPIC_OWNERSHIP:
            return True
        return original_queue_current(user_id, thread_id, chat_id, ownership)

    def _queue_capture(user_id, thread_id, chat_id=None):
        return (
            original_queue_capture(user_id, thread_id, chat_id)
            or _TEST_TOPIC_OWNERSHIP
        )

    monkeypatch.setattr(bot, "is_topic_ownership_current", _bot_is_current)
    monkeypatch.setattr(mq, "is_topic_ownership_current", _queue_is_current)
    monkeypatch.setattr(mq, "capture_topic_ownership", _queue_capture)
    monkeypatch.setattr(
        bot,
        "capture_topic_ownership",
        lambda *_args, **_kwargs: _TEST_TOPIC_OWNERSHIP,
    )


def test_extract_command_args():
    assert bot._extract_command_args("/q") == ""
    assert bot._extract_command_args("/q next step") == "next step"
    assert (
        bot._extract_command_args("/q@Terminex_bot next step with spaces")
        == "next step with spaces"
    )


def test_queued_topic_input_fifo_and_count():
    user_id = 11
    thread_id = 22
    mq.clear_queued_topic_inputs(user_id, thread_id, -100)

    assert mq.queued_topic_input_count(user_id, thread_id, -100) == 0

    assert _enqueue_test_topic_input(user_id, thread_id, "first", -100, 1) == 1
    assert _enqueue_test_topic_input(user_id, thread_id, "second", -100, 2) == 2
    assert mq.queued_topic_input_count(user_id, thread_id, -100) == 2

    assert mq.pop_queued_topic_input(user_id, thread_id, -100) == ("first", -100, 1)
    assert mq.pop_queued_topic_input(user_id, thread_id, -100) == ("second", -100, 2)
    assert mq.pop_queued_topic_input(user_id, thread_id, -100) is None
    assert mq.queued_topic_input_count(user_id, thread_id, -100) == 0


def test_is_progress_active_uses_topic_key():
    user_id = 33
    thread_id = 44
    key = (user_id, 0, thread_id)
    mq._progress_msg_info[key] = (123, "@9", "working")
    try:
        assert mq.is_progress_active(user_id, thread_id) is True
        assert mq.is_progress_active(user_id, thread_id + 1) is False
    finally:
        mq.clear_progress_msg_info(user_id, thread_id)


def test_get_progress_text_uses_topic_key():
    user_id = 35
    thread_id = 46
    key = (user_id, 0, thread_id)
    mq._progress_msg_info[key] = (321, "@7", "overview line")
    try:
        assert mq.get_progress_text(user_id, thread_id) == "overview line"
        assert mq.get_progress_text(user_id, thread_id + 1) == ""
    finally:
        mq.clear_progress_msg_info(user_id, thread_id)


@pytest.mark.asyncio
async def test_is_window_in_progress_ignores_stale_progress_when_app_server_idle(
    monkeypatch,
):
    monkeypatch.setattr(bot, "_codex_app_server_enabled", lambda: True)
    monkeypatch.setattr(bot, "is_progress_active", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_thread_id",
        lambda _wid: "thread-1",
    )
    monkeypatch.setattr(
        bot.codex_app_server_client,
        "is_turn_in_progress",
        lambda _thread_id: False,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_active_turn_id",
        lambda _wid: "",
    )

    assert await bot._is_window_in_progress(1, 2, "@1") is False


@pytest.mark.asyncio
async def test_is_window_in_progress_accepts_active_codex_turn_when_app_server_enabled(
    monkeypatch,
):
    monkeypatch.setattr(bot, "_codex_app_server_enabled", lambda: True)
    monkeypatch.setattr(bot, "is_progress_active", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_thread_id",
        lambda _wid: "thread-2",
    )
    monkeypatch.setattr(
        bot.codex_app_server_client,
        "is_turn_in_progress",
        lambda _thread_id: False,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_active_turn_id",
        lambda _wid: "turn-9",
    )

    assert await bot._is_window_in_progress(1, 2, "@2") is True


def test_merge_progress_text_token_stream_is_compact():
    merged = ""
    for chunk in ["Ready", " to", " help", " in", " `/", "srv", "/c", "od", "ex", "`"]:
        merged = mq._merge_progress_text(merged, chunk)
    assert merged == "Ready to help in `/srv/codex`"


def test_merge_progress_text_prefers_new_snapshot_when_prefix_matches():
    existing = "Ready to h"
    merged = mq._merge_progress_text(existing, "Ready to help")
    assert merged == "Ready to help"


def test_render_progress_message_keeps_working_view_compact():
    long_text = "x" * (mq.PROGRESS_PREVIEW_MAX_LENGTH + 200)
    rendered = mq._render_progress_message(long_text)
    assert rendered.startswith("⏳ Working…\n\n… ")
    assert len(rendered) < len(long_text)


@pytest.mark.asyncio
async def test_progress_update_ignores_message_not_modified(monkeypatch):
    user_id = 91
    thread_id = 92
    skey = (user_id, 0, thread_id)
    mq._progress_msg_info[skey] = (7001, "@7", "Ready")
    calls = {"send_new": 0}

    class _Bot:
        async def edit_message_text(self, **_kwargs):
            raise Exception(
                "Message is not modified: specified new message content and reply markup are exactly the same"
            )

    monkeypatch.setattr(
        mq.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, **_kwargs: -100900,
    )

    async def _noop_clear_status(*_args, **_kwargs):
        return None

    async def _unexpected_send(*_args, **_kwargs):
        calls["send_new"] += 1

    monkeypatch.setattr(mq, "_do_clear_status_message", _noop_clear_status)
    monkeypatch.setattr(mq, "_do_send_progress_message", _unexpected_send)

    try:
        await mq._process_progress_update_task(
            _Bot(),
            user_id,
            mq.MessageTask(
                task_type="progress_update",
                text=" to help",
                window_id="@7",
                thread_id=thread_id,
            ),
        )
        assert calls["send_new"] == 0
        assert mq._progress_msg_info[skey][2] == "Ready to help"
    finally:
        mq.clear_progress_msg_info(user_id, thread_id)


@pytest.mark.asyncio
async def test_progress_finalize_ignores_message_not_modified(monkeypatch):
    user_id = 93
    thread_id = 94
    skey = (user_id, 0, thread_id)
    mq._progress_msg_info[skey] = (7002, "@8", "Still running")

    class _Bot:
        async def edit_message_text(self, **_kwargs):
            raise Exception(
                "Message is not modified: specified new message content and reply markup are exactly the same"
            )

    monkeypatch.setattr(
        mq.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, **_kwargs: -100901,
    )

    await mq._process_progress_finalize_task(
        _Bot(),
        user_id,
        mq.MessageTask(
            task_type="progress_finalize",
            window_id="@8",
            thread_id=thread_id,
        ),
    )

    assert skey not in mq._progress_msg_info


@pytest.mark.asyncio
async def test_progress_finalize_clears_empty_placeholder(monkeypatch):
    user_id = 95
    thread_id = 96
    skey = (user_id, 0, thread_id)
    mq._progress_msg_info[skey] = (7003, "@9", "")

    class _Bot:
        def __init__(self) -> None:
            self.deleted: list[tuple[int, int]] = []

        async def delete_message(self, *, chat_id: int, message_id: int):
            self.deleted.append((chat_id, message_id))
            return True

    bot_obj = _Bot()

    monkeypatch.setattr(
        mq.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, **_kwargs: -100902,
    )

    await mq._process_progress_finalize_task(
        bot_obj,
        user_id,
        mq.MessageTask(
            task_type="progress_finalize",
            window_id="@9",
            thread_id=thread_id,
        ),
    )

    assert skey not in mq._progress_msg_info
    assert bot_obj.deleted == [(-100902, 7003)]


@pytest.mark.asyncio
async def test_progress_finalize_compact_mode_hides_body(monkeypatch):
    user_id = 97
    thread_id = 98
    skey = (user_id, 0, thread_id)
    mq._progress_msg_info[skey] = (7004, "@10", "Long progress body")
    edits: list[str] = []

    class _Bot:
        async def edit_message_text(self, **kwargs):
            edits.append(kwargs["text"])
            return True

    monkeypatch.setattr(
        mq.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, **_kwargs: -100903,
    )
    monkeypatch.setattr(mq, "convert_markdown", lambda text: text)

    await mq._process_progress_finalize_task(
        _Bot(),
        user_id,
        mq.MessageTask(
            task_type="progress_finalize",
            window_id="@10",
            thread_id=thread_id,
            finalize_mode="compact",
        ),
    )

    assert edits == ["✅ Process Complete"]
    assert skey not in mq._progress_msg_info


@pytest.mark.asyncio
async def test_progress_finalize_clears_tracking_when_all_edits_fail(monkeypatch):
    user_id = 99
    thread_id = 100
    skey = (user_id, 0, thread_id)
    mq._progress_msg_info[skey] = (7005, "@11", "Working on it")
    mq._progress_text_cache[skey] = ("@11", "Working on it")
    edit_attempts: list[int] = []

    class _Bot:
        async def edit_message_text(self, **_kwargs):
            edit_attempts.append(1)
            raise Exception("message to edit not found")

    monkeypatch.setattr(
        mq.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, **_kwargs: -100904,
    )

    await mq._process_progress_finalize_task(
        _Bot(),
        user_id,
        mq.MessageTask(
            task_type="progress_finalize",
            window_id="@11",
            thread_id=thread_id,
        ),
    )

    assert len(edit_attempts) == 2
    assert skey not in mq._progress_msg_info
    assert skey not in mq._progress_text_cache


@pytest.mark.asyncio
async def test_convert_status_to_content_deletes_orphan_when_all_edits_fail(monkeypatch):
    user_id = 101
    thread_id = 102
    skey = (user_id, 0, thread_id)
    mq._status_msg_info[skey] = (7006, "@12", "waiting")
    deleted: list[tuple[int, int]] = []
    edit_attempts: list[int] = []

    class _Bot:
        async def edit_message_text(self, **_kwargs):
            edit_attempts.append(1)
            raise Exception("message can't be edited")

        async def delete_message(self, *, chat_id: int, message_id: int):
            deleted.append((chat_id, message_id))
            return True

    monkeypatch.setattr(
        mq.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, **_kwargs: -100905,
    )

    converted = await mq._convert_status_to_content(
        _Bot(),
        user_id,
        thread_id,
        "@12",
        "real content",
    )

    assert converted is None
    assert len(edit_attempts) == 2
    assert deleted == [(-100905, 7006)]
    assert skey not in mq._status_msg_info


@pytest.mark.asyncio
async def test_do_send_status_message_preserves_old_tracking_when_delete_and_send_fail(monkeypatch):
    user_id = 103
    thread_id = 104
    skey = (user_id, 0, thread_id)
    mq._status_msg_info[skey] = (7007, "@13", "old status")

    class _Bot:
        async def delete_message(self, *, chat_id: int, message_id: int):
            raise Exception("delete failed")

        async def send_chat_action(self, **_kwargs):
            return True

    async def _send_none(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        mq.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, **_kwargs: -100906,
    )
    monkeypatch.setattr(mq, "send_with_fallback", _send_none)

    await mq._do_send_status_message(
        _Bot(),
        user_id,
        thread_id,
        "@13",
        "new status",
    )

    assert mq._status_msg_info[skey] == (7007, "@13", "old status")


@pytest.mark.asyncio
async def test_do_send_progress_message_preserves_old_tracking_when_delete_and_send_fail(monkeypatch):
    user_id = 105
    thread_id = 106
    skey = (user_id, 0, thread_id)
    mq._progress_msg_info[skey] = (7008, "@14", "old progress")

    class _Bot:
        async def delete_message(self, *, chat_id: int, message_id: int):
            raise Exception("delete failed")

    async def _send_none(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        mq.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, **_kwargs: -100907,
    )
    monkeypatch.setattr(mq, "send_with_fallback", _send_none)

    await mq._do_send_progress_message(
        _Bot(),
        user_id,
        thread_id,
        "@14",
        "new progress",
    )

    assert mq._progress_msg_info[skey] == (7008, "@14", "old progress")


@pytest.mark.asyncio
async def test_do_send_status_message_does_not_clobber_newer_tracking_on_send_failure(monkeypatch):
    user_id = 107
    thread_id = 108
    skey = (user_id, 0, thread_id)
    mq._status_msg_info[skey] = (7009, "@15", "old status")

    class _Bot:
        async def delete_message(self, *, chat_id: int, message_id: int):
            return True

        async def send_chat_action(self, **_kwargs):
            return True

    async def _send_none(*_args, **_kwargs):
        mq._status_msg_info[skey] = (7010, "@15", "newer status")
        return None

    monkeypatch.setattr(
        mq.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, **_kwargs: -100908,
    )
    monkeypatch.setattr(mq, "send_with_fallback", _send_none)

    await mq._do_send_status_message(
        _Bot(),
        user_id,
        thread_id,
        "@15",
        "replacement status",
    )

    assert mq._status_msg_info[skey] == (7010, "@15", "newer status")


@pytest.mark.asyncio
async def test_do_send_progress_message_does_not_clobber_newer_tracking_on_send_failure(monkeypatch):
    user_id = 109
    thread_id = 110
    skey = (user_id, 0, thread_id)
    mq._progress_msg_info[skey] = (7011, "@16", "old progress")

    class _Bot:
        async def delete_message(self, *, chat_id: int, message_id: int):
            return True

    async def _send_none(*_args, **_kwargs):
        mq._progress_msg_info[skey] = (7012, "@16", "newer progress")
        return None

    monkeypatch.setattr(
        mq.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, **_kwargs: -100909,
    )
    monkeypatch.setattr(mq, "send_with_fallback", _send_none)

    await mq._do_send_progress_message(
        _Bot(),
        user_id,
        thread_id,
        "@16",
        "replacement progress",
    )

    assert mq._progress_msg_info[skey] == (7012, "@16", "newer progress")


@pytest.mark.asyncio
async def test_status_update_edit_fallback_does_not_preemptively_drop_tracking(monkeypatch):
    user_id = 111
    thread_id = 112
    skey = (user_id, 0, thread_id)
    mq._status_msg_info[skey] = (7013, "@17", "old status")

    class _Bot:
        async def edit_message_text(self, **_kwargs):
            raise Exception("cannot edit")

        async def send_chat_action(self, **_kwargs):
            return True

    observed_before_send: list[tuple[int, str, str] | None] = []

    async def _fake_send_status(_bot, _user_id, tid, wid, text, *, chat_id=None):
        observed_before_send.append(mq._status_msg_info.get((user_id, 0, tid)))
        mq._status_msg_info[(user_id, 0, tid)] = (7014, wid, text)

    monkeypatch.setattr(
        mq.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, **_kwargs: -100910,
    )
    monkeypatch.setattr(mq, "_do_send_status_message", _fake_send_status)

    await mq._process_status_update_task(
        _Bot(),
        user_id,
        mq.MessageTask(
            task_type="status_update",
            text="new status",
            window_id="@17",
            thread_id=thread_id,
        ),
    )

    assert observed_before_send == [(7013, "@17", "old status")]
    assert mq._status_msg_info[skey] == (7014, "@17", "new status")


@pytest.mark.asyncio
async def test_progress_update_edit_fallback_does_not_preemptively_drop_tracking(monkeypatch):
    user_id = 113
    thread_id = 114
    skey = (user_id, 0, thread_id)
    mq._progress_msg_info[skey] = (7015, "@18", "old progress")

    class _Bot:
        async def edit_message_text(self, **_kwargs):
            raise Exception("cannot edit")

    async def _noop_clear_status(*_args, **_kwargs):
        return None

    observed_before_send: list[tuple[int, str, str] | None] = []

    async def _fake_send_progress(_bot, _user_id, tid, wid, accumulated_text, **_kwargs):
        observed_before_send.append(mq._progress_msg_info.get((user_id, 0, tid)))
        mq._progress_msg_info[(user_id, 0, tid)] = (7016, wid, accumulated_text)

    monkeypatch.setattr(
        mq.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, **_kwargs: -100911,
    )
    monkeypatch.setattr(mq, "_do_clear_status_message", _noop_clear_status)
    monkeypatch.setattr(mq, "_do_send_progress_message", _fake_send_progress)

    await mq._process_progress_update_task(
        _Bot(),
        user_id,
        mq.MessageTask(
            task_type="progress_update",
            text=" plus more",
            window_id="@18",
            thread_id=thread_id,
        ),
    )

    assert observed_before_send == [(7015, "@18", "old progress")]
    assert mq._progress_msg_info[skey] == (7016, "@18", "old progress plus more")


@pytest.mark.asyncio
async def test_do_clear_status_message_preserves_tracking_when_delete_fails(monkeypatch):
    user_id = 115
    thread_id = 116
    skey = (user_id, 0, thread_id)
    mq._status_msg_info[skey] = (7017, "@19", "status text")

    class _Bot:
        async def delete_message(self, *, chat_id: int, message_id: int):
            raise Exception("delete failed")

    monkeypatch.setattr(
        mq.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, **_kwargs: -100912,
    )

    await mq._do_clear_status_message(_Bot(), user_id, thread_id)

    assert mq._status_msg_info[skey] == (7017, "@19", "status text")


@pytest.mark.asyncio
async def test_do_clear_progress_message_preserves_tracking_when_delete_fails(monkeypatch):
    user_id = 117
    thread_id = 118
    skey = (user_id, 0, thread_id)
    mq._progress_msg_info[skey] = (7018, "@20", "progress text")
    mq._progress_text_cache[skey] = ("@20", "progress text")

    class _Bot:
        async def delete_message(self, *, chat_id: int, message_id: int):
            raise Exception("delete failed")

    monkeypatch.setattr(
        mq.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, **_kwargs: -100913,
    )

    await mq._do_clear_progress_message(_Bot(), user_id, thread_id)

    assert mq._progress_msg_info[skey] == (7018, "@20", "progress text")
    assert mq._progress_text_cache[skey] == ("@20", "progress text")


@pytest.mark.asyncio
async def test_do_clear_progress_message_does_not_clear_newer_cache_after_successful_delete(monkeypatch):
    user_id = 119
    thread_id = 120
    skey = (user_id, 0, thread_id)
    old = (7019, "@21", "old progress")
    newer = (7020, "@21", "newer progress")
    mq._progress_msg_info[skey] = old
    mq._progress_text_cache[skey] = ("@21", "old progress")

    class _Bot:
        async def delete_message(self, *, chat_id: int, message_id: int):
            mq._progress_msg_info[skey] = newer
            mq._progress_text_cache[skey] = ("@21", "newer progress")
            return True

    monkeypatch.setattr(
        mq.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, **_kwargs: -100914,
    )

    await mq._do_clear_progress_message(_Bot(), user_id, thread_id)

    assert mq._progress_msg_info[skey] == newer
    assert mq._progress_text_cache[skey] == ("@21", "newer progress")


@pytest.mark.asyncio
async def test_do_clear_progress_message_clears_default_scope_cache(monkeypatch):
    user_id = 121
    thread_id = 122
    skey = (user_id, 0, thread_id)
    mq._progress_msg_info[skey] = (7021, "@22", "old progress")
    mq._progress_text_cache[skey] = ("@22", "old progress")

    class _Bot:
        async def delete_message(self, **_kwargs):
            return True

    monkeypatch.setattr(
        mq.session_manager,
        "resolve_chat_id",
        lambda *_args, **_kwargs: -100915,
    )

    await mq._do_clear_progress_message(_Bot(), user_id, thread_id)

    assert skey not in mq._progress_msg_info
    assert skey not in mq._progress_text_cache


@pytest.mark.asyncio
async def test_enqueue_progress_update_coalesces_trailing_pending_updates():
    user_id = 201
    thread_id = 202
    queue = asyncio.Queue()
    mq._message_queues[user_id] = queue
    mq._queue_locks[user_id] = asyncio.Lock()

    await queue.put(
        mq.MessageTask(
            task_type="progress_update",
            text="Ready",
            window_id="@2",
            thread_id=thread_id,
        )
    )
    await queue.put(
        mq.MessageTask(
            task_type="progress_update",
            text=" to",
            window_id="@2",
            thread_id=thread_id,
        )
    )

    await mq.enqueue_progress_update(
        bot=object(),  # type: ignore[arg-type]
        user_id=user_id,
        window_id="@2",
        progress_text=" help",
        thread_id=thread_id,
    )

    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    try:
        assert len(items) == 1
        only = items[0]
        assert only.task_type == "progress_update"
        assert only.text == "Ready to help"
        assert only.window_id == "@2"
        assert only.thread_id == thread_id
    finally:
        mq._message_queues.pop(user_id, None)
        mq._queue_locks.pop(user_id, None)


@pytest.mark.asyncio
async def test_enqueue_progress_update_keeps_non_progress_tail():
    user_id = 203
    thread_id = 204
    queue = asyncio.Queue()
    mq._message_queues[user_id] = queue
    mq._queue_locks[user_id] = asyncio.Lock()

    await queue.put(
        mq.MessageTask(
            task_type="progress_update",
            text="Ready",
            window_id="@3",
            thread_id=thread_id,
        )
    )
    await queue.put(
        mq.MessageTask(
            task_type="status_update",
            text="status",
            window_id="@3",
            thread_id=thread_id,
        )
    )

    await mq.enqueue_progress_update(
        bot=object(),  # type: ignore[arg-type]
        user_id=user_id,
        window_id="@3",
        progress_text=" now",
        thread_id=thread_id,
    )

    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    try:
        assert [item.task_type for item in items] == [
            "progress_update",
            "status_update",
            "progress_update",
        ]
        assert items[0].text == "Ready"
        assert items[2].text == " now"
    finally:
        mq._message_queues.pop(user_id, None)
        mq._queue_locks.pop(user_id, None)


@pytest.mark.asyncio
async def test_progress_update_coalescing_keeps_pending_topic_index_single():
    user_id = 205
    thread_id = 206
    queue: asyncio.Queue[mq.MessageTask] = asyncio.Queue()
    mq._message_queues[user_id] = queue
    mq._queue_locks[user_id] = asyncio.Lock()

    try:
        await mq.enqueue_progress_update(
            bot=object(),  # type: ignore[arg-type]
            user_id=user_id,
            window_id="@4",
            progress_text="Ready",
            thread_id=thread_id,
        )
        await mq.enqueue_progress_update(
            bot=object(),  # type: ignore[arg-type]
            user_id=user_id,
            window_id="@4",
            progress_text=" to help",
            thread_id=thread_id,
        )

        assert mq._queued_delivery_topic_counts[user_id] == {(0, thread_id): 1}
        assert await mq.get_pending_delivery_topics(user_id) == {thread_id}
    finally:
        while not queue.empty():
            queue.get_nowait()
            queue.task_done()
        mq._message_queues.pop(user_id, None)
        mq._queue_locks.pop(user_id, None)
        mq._queued_delivery_topic_counts.pop(user_id, None)
        mq._progress_text_cache.pop((user_id, 0, thread_id), None)


@pytest.mark.asyncio
async def test_merge_content_tasks_removes_merged_tasks_from_pending_topic_index():
    user_id = 207
    thread_id = 208
    queue: asyncio.Queue[mq.MessageTask] = asyncio.Queue()
    lock = asyncio.Lock()
    mq._message_queues[user_id] = queue
    first = mq.MessageTask(
        task_type="content",
        window_id="@5",
        thread_id=thread_id,
        parts=["hello"],
        delivery_generation=0,
        topic_ownership=_TEST_TOPIC_OWNERSHIP,
    )
    mergeable = mq.MessageTask(
        task_type="content",
        window_id="@5",
        thread_id=thread_id,
        parts=[" world"],
    )
    retained = mq.MessageTask(
        task_type="status_update",
        window_id="@5",
        thread_id=thread_id,
        text="working",
    )

    try:
        mq._put_queued_task(user_id, queue, mergeable)
        mq._put_queued_task(user_id, queue, retained)

        merged, merge_count = await mq._merge_content_tasks(
            queue,
            first,
            lock,
            user_id=user_id,
        )

        assert merge_count == 1
        assert merged.parts == ["hello", " world"]
        assert mq._queued_delivery_topic_counts[user_id] == {(0, thread_id): 1}
        assert await mq.get_pending_delivery_topics(user_id) == {thread_id}
        assert queue.get_nowait() is retained
    finally:
        while not queue.empty():
            queue.get_nowait()
            queue.task_done()
        mq._message_queues.pop(user_id, None)
        mq._queued_delivery_topic_counts.pop(user_id, None)


@pytest.mark.asyncio
async def test_merge_content_tasks_does_not_merge_across_topics():
    user_id = 212
    first_thread_id = 213
    queued_thread_id = 214
    queue: asyncio.Queue[mq.MessageTask] = asyncio.Queue()
    lock = asyncio.Lock()
    mq._message_queues[user_id] = queue
    first = mq.MessageTask(
        task_type="content",
        window_id="@8",
        thread_id=first_thread_id,
        parts=["first"],
    )
    different_topic = mq.MessageTask(
        task_type="content",
        window_id="@8",
        thread_id=queued_thread_id,
        parts=["second"],
    )

    try:
        mq._put_queued_task(user_id, queue, different_topic)

        merged, merge_count = await mq._merge_content_tasks(
            queue,
            first,
            lock,
            user_id=user_id,
        )

        assert merged is first
        assert merge_count == 0
        assert mq._queued_delivery_topic_counts[user_id] == {(0, queued_thread_id): 1}
        assert await mq.get_pending_delivery_topics(user_id) == {queued_thread_id}
        assert queue.get_nowait() is different_topic
    finally:
        while not queue.empty():
            queue.get_nowait()
            queue.task_done()
        mq._message_queues.pop(user_id, None)
        mq._queued_delivery_topic_counts.pop(user_id, None)


@pytest.mark.asyncio
async def test_merge_content_tasks_does_not_consume_newer_delivery_generation():
    user_id = 215
    thread_id = 216
    queue: asyncio.Queue[mq.MessageTask] = asyncio.Queue()
    lock = asyncio.Lock()
    mq._message_queues[user_id] = queue
    first = mq.MessageTask(
        task_type="content",
        window_id="@8",
        thread_id=thread_id,
        parts=["stale"],
        delivery_generation=0,
    )
    newer = mq.MessageTask(
        task_type="content",
        window_id="@8",
        thread_id=thread_id,
        parts=["fresh"],
        delivery_generation=1,
    )

    try:
        mq._put_queued_task(user_id, queue, newer)

        merged, merge_count = await mq._merge_content_tasks(
            queue,
            first,
            lock,
            user_id=user_id,
        )

        assert merged is first
        assert merge_count == 0
        assert queue.get_nowait() is newer
    finally:
        while not queue.empty():
            queue.get_nowait()
            queue.task_done()
        mq._message_queues.pop(user_id, None)
        mq._queued_delivery_topic_counts.pop(user_id, None)


@pytest.mark.asyncio
async def test_requeue_task_front_tracks_retried_task_and_preserves_existing_counts():
    user_id = 209
    retry_thread_id = 210
    queued_thread_id = 211
    queue: asyncio.Queue[mq.MessageTask] = asyncio.Queue()
    lock = asyncio.Lock()
    mq._message_queues[user_id] = queue
    retried = mq.MessageTask(
        task_type="content",
        window_id="@6",
        thread_id=retry_thread_id,
        parts=["retry"],
    )
    already_queued = mq.MessageTask(
        task_type="progress_finalize",
        window_id="@7",
        thread_id=queued_thread_id,
    )

    try:
        mq._put_queued_task(user_id, queue, already_queued)

        await mq._requeue_task_front(
            queue,
            lock,
            user_id=user_id,
            task=retried,
        )

        assert mq._queued_delivery_topic_counts[user_id] == {
            (0, queued_thread_id): 1,
            (0, retry_thread_id): 1,
        }
        assert await mq.get_pending_delivery_topics(user_id) == {
            queued_thread_id,
            retry_thread_id,
        }
        assert queue.get_nowait() is retried
        assert queue.get_nowait() is already_queued
    finally:
        while not queue.empty():
            queue.get_nowait()
            queue.task_done()
        mq._message_queues.pop(user_id, None)
        mq._queued_delivery_topic_counts.pop(user_id, None)


@pytest.mark.asyncio
async def test_pending_delivery_topics_uses_enqueue_index_without_queue_scan():
    user_id = 401
    thread_id = 402
    queue: asyncio.Queue[mq.MessageTask] = asyncio.Queue()
    mq._message_queues[user_id] = queue
    mq._queue_locks[user_id] = asyncio.Lock()

    class _NoIter:
        def __iter__(self):
            raise AssertionError('pending delivery lookup should not scan queue internals')

    try:
        await mq.enqueue_content_message(
            object(),  # type: ignore[arg-type]
            user_id,
            '@401',
            ['hello'],
            thread_id=thread_id,
        )
        original_queue = queue._queue
        queue._queue = _NoIter()  # type: ignore[assignment]
        try:
            assert await mq.get_pending_delivery_topics(user_id) == {thread_id}
        finally:
            queue._queue = original_queue
    finally:
        while not queue.empty():
            queue.get_nowait()
            queue.task_done()
        mq._message_queues.pop(user_id, None)
        mq._queue_locks.pop(user_id, None)
        mq._queued_delivery_topic_counts.pop(user_id, None)


@pytest.mark.asyncio
async def test_message_queue_worker_retries_content_after_retry_after(monkeypatch):
    user_id = 301
    queue: asyncio.Queue[mq.MessageTask] = asyncio.Queue()
    mq._message_queues[user_id] = queue
    mq._queue_locks[user_id] = asyncio.Lock()
    attempts: list[str] = []
    current_time = {"value": 100.0}

    async def _fake_process_content_task(_bot, _user_id: int, _task: mq.MessageTask):
        attempts.append("content")
        if len(attempts) == 1:
            raise RetryAfter(1)

    async def _fake_sleep(seconds: float):
        current_time["value"] += seconds

    monkeypatch.setattr(mq, "_process_content_task", _fake_process_content_task)
    monkeypatch.setattr(mq.time, "monotonic", lambda: current_time["value"])
    monkeypatch.setattr(mq.asyncio, "sleep", _fake_sleep)

    worker = asyncio.create_task(mq._message_queue_worker(object(), user_id))  # type: ignore[arg-type]
    await queue.put(
        mq.MessageTask(
            task_type="content",
            window_id="@301",
            thread_id=301,
            parts=["hello"],
            topic_ownership=_TEST_TOPIC_OWNERSHIP,
        )
    )

    try:
        await asyncio.wait_for(queue.join(), timeout=1)
        assert attempts == ["content", "content"]
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        mq._message_queues.pop(user_id, None)
        mq._queue_locks.pop(user_id, None)
        mq._active_delivery_topics.pop(user_id, None)
        mq._flood_until.pop(user_id, None)


@pytest.mark.asyncio
async def test_message_queue_worker_retries_merged_content_after_retry_after(monkeypatch):
    user_id = 303
    thread_id = 304
    queue: asyncio.Queue[mq.MessageTask] = asyncio.Queue()
    mq._message_queues[user_id] = queue
    mq._queue_locks[user_id] = asyncio.Lock()
    attempts: list[list[str]] = []
    current_time = {"value": 300.0}

    async def _fake_process_content_task(_bot, _user_id: int, task: mq.MessageTask):
        attempts.append(list(task.parts))
        if len(attempts) == 1:
            raise RetryAfter(1)

    async def _fake_sleep(seconds: float):
        current_time["value"] += seconds

    monkeypatch.setattr(mq, "_process_content_task", _fake_process_content_task)
    monkeypatch.setattr(mq.time, "monotonic", lambda: current_time["value"])
    monkeypatch.setattr(mq.asyncio, "sleep", _fake_sleep)

    worker = asyncio.create_task(mq._message_queue_worker(object(), user_id))  # type: ignore[arg-type]
    mq._put_queued_task(
        user_id,
        queue,
        mq.MessageTask(
            task_type="content",
            window_id="@303",
            thread_id=thread_id,
            parts=["hello"],
        ),
    )
    mq._put_queued_task(
        user_id,
        queue,
        mq.MessageTask(
            task_type="content",
            window_id="@303",
            thread_id=thread_id,
            parts=[" world"],
        ),
    )

    try:
        await asyncio.wait_for(queue.join(), timeout=1)
        assert attempts == [["hello", " world"], ["hello", " world"]]
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        mq._message_queues.pop(user_id, None)
        mq._queue_locks.pop(user_id, None)
        mq._active_delivery_topics.pop(user_id, None)
        mq._queued_delivery_topic_counts.pop(user_id, None)
        mq._flood_until.pop(user_id, None)


@pytest.mark.asyncio
async def test_message_queue_worker_retries_progress_finalize_after_long_retry_after(monkeypatch):
    user_id = 302
    queue: asyncio.Queue[mq.MessageTask] = asyncio.Queue()
    mq._message_queues[user_id] = queue
    mq._queue_locks[user_id] = asyncio.Lock()
    attempts: list[str] = []
    current_time = {"value": 200.0}

    async def _fake_finalize(_bot, _user_id: int, _task: mq.MessageTask):
        attempts.append("finalize")
        if len(attempts) == 1:
            raise RetryAfter(mq.FLOOD_CONTROL_MAX_WAIT + 5)

    async def _fake_sleep(seconds: float):
        current_time["value"] += seconds

    monkeypatch.setattr(mq, "_process_progress_finalize_task", _fake_finalize)
    monkeypatch.setattr(mq.time, "monotonic", lambda: current_time["value"])
    monkeypatch.setattr(mq.asyncio, "sleep", _fake_sleep)

    worker = asyncio.create_task(mq._message_queue_worker(object(), user_id))  # type: ignore[arg-type]
    await queue.put(
        mq.MessageTask(
            task_type="progress_finalize",
            window_id="@302",
            thread_id=302,
            topic_ownership=_TEST_TOPIC_OWNERSHIP,
        )
    )

    try:
        await asyncio.wait_for(queue.join(), timeout=1)
        assert attempts == ["finalize", "finalize"]
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        mq._message_queues.pop(user_id, None)
        mq._queue_locks.pop(user_id, None)
        mq._active_delivery_topics.pop(user_id, None)
        mq._flood_until.pop(user_id, None)


@pytest.mark.asyncio
async def test_enqueue_status_clear_survives_active_flood_control(monkeypatch):
    user_id = 303
    current_time = {"value": 300.0}
    monkeypatch.setattr(mq.time, "monotonic", lambda: current_time["value"])

    queue = asyncio.Queue()
    mq._message_queues[user_id] = queue
    mq._queue_locks[user_id] = asyncio.Lock()
    mq._flood_until[user_id] = current_time["value"] + 30.0

    try:
        await mq.enqueue_status_update(
            object(),  # type: ignore[arg-type]
            user_id,
            "@303",
            None,
            thread_id=303,
        )

        item = queue.get_nowait()
        assert item.task_type == "status_clear"
        assert item.thread_id == 303
    finally:
        mq._message_queues.pop(user_id, None)
        mq._queue_locks.pop(user_id, None)
        mq._flood_until.pop(user_id, None)


@pytest.mark.asyncio
async def test_sync_queued_topic_dock_edits_existing_message_in_place(monkeypatch):
    user_id = 304
    thread_id = 304
    skey = (user_id, 0, thread_id)
    mq._queued_topic_inputs[skey] = [("second item", -100304, 1)]
    mq._queue_dock_msg_info[skey] = (55, "⏳ Queue\n1. first item")
    events: list[tuple[str, int]] = []

    class _Bot:
        async def edit_message_text(self, **kwargs):
            events.append(("edit", kwargs["message_id"]))

        async def delete_message(self, **kwargs):
            events.append(("delete", kwargs["message_id"]))

    async def _unexpected_send(*_args, **_kwargs):
        raise AssertionError("queue dock refresh should edit in place, not send a replacement")

    monkeypatch.setattr(mq.session_manager, "resolve_chat_id", lambda *_args, **_kwargs: -100304)
    monkeypatch.setattr(mq, "send_with_fallback", _unexpected_send)

    try:
        await mq.sync_queued_topic_dock(_Bot(), user_id, thread_id, window_id="@304")  # type: ignore[arg-type]
        assert events == [("edit", 55)]
        assert mq._queue_dock_msg_info[skey] == (55, "⏳ Queue\n1. second item")
    finally:
        mq._queued_topic_inputs.pop(skey, None)
        mq._queue_dock_msg_info.pop(skey, None)


@pytest.mark.asyncio
async def test_sync_queued_topic_dock_preserves_tracking_when_empty_delete_fails(monkeypatch):
    user_id = 305
    thread_id = 305
    skey = (user_id, 0, thread_id)
    mq._queued_topic_inputs.pop(skey, None)
    mq._queue_dock_msg_info[skey] = (56, "⏳ Queue\n1. item")

    class _Bot:
        async def delete_message(self, **_kwargs):
            raise Exception("delete failed")

    monkeypatch.setattr(mq.session_manager, "resolve_chat_id", lambda *_args, **_kwargs: -100305)

    try:
        await mq.sync_queued_topic_dock(_Bot(), user_id, thread_id, window_id="@305")  # type: ignore[arg-type]
        assert mq._queue_dock_msg_info[skey] == (56, "⏳ Queue\n1. item")
    finally:
        mq._queue_dock_msg_info.pop(skey, None)


@pytest.mark.asyncio
async def test_clear_queued_topic_dock_preserves_tracking_when_delete_fails(monkeypatch):
    user_id = 306
    thread_id = 306
    skey = (user_id, 0, thread_id)
    mq._queue_dock_msg_info[skey] = (57, "⏳ Queue\n1. item")

    class _Bot:
        async def delete_message(self, **_kwargs):
            raise Exception("delete failed")

    monkeypatch.setattr(mq.session_manager, "resolve_chat_id", lambda *_args, **_kwargs: -100306)

    try:
        await mq.clear_queued_topic_dock(_Bot(), user_id, thread_id)  # type: ignore[arg-type]
        assert mq._queue_dock_msg_info[skey] == (57, "⏳ Queue\n1. item")
    finally:
        mq._queue_dock_msg_info.pop(skey, None)


@pytest.mark.asyncio
async def test_steer_message_keeps_progress_block_active(monkeypatch):
    events: list[tuple[str, str | None]] = []

    class _Chat:
        type = "supergroup"
        id = -100123

        async def send_action(self, *_args, **_kwargs):
            return None

    class _Message:
        def __init__(self) -> None:
            self.text = "steer this"
            self.chat = _Chat()
            self.message_thread_id = 777
            self.message_id = 888

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1147817421),
        effective_message=_Message(),
        effective_chat=_Chat(),
        message=_Message(),
    )
    context = SimpleNamespace(bot=object(), user_data={})

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "runtime_mode", "hybrid")
    monkeypatch.setattr(bot, "_codex_app_server_enabled", lambda: False)
    monkeypatch.setattr(
        bot.session_manager, "set_group_chat_id", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_for_thread",
        lambda _uid, _tid, **_kwargs: "@32",
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda _uid, _tid, **_kwargs: SimpleNamespace(
            codex_thread_id="thread-32",
            cwd="/tmp",
        ),
    )

    async def _is_in_progress(_uid: int, _tid: int | None, _wid: str, **_kwargs) -> bool:
        return True

    async def _send_topic_text_to_window(
        *,
        user_id: int,
        thread_id: int | None,
        chat_id: int | None = None,
        window_id: str,
        text: str,
        steer: bool = False,
        topic_ownership=None,
    ):
        _ = user_id, thread_id, chat_id, text, steer, topic_ownership
        events.append(("send_to_window", window_id))
        return True, ""

    async def _unexpected_status_update(*_args, **_kwargs):
        events.append(("status_update", None))

    async def _unexpected_progress_clear(*_args, **_kwargs):
        events.append(("progress_clear", None))

    async def _set_eyes(_message):
        events.append(("eyes", None))

    monkeypatch.setattr(bot, "_is_window_in_progress", _is_in_progress)
    monkeypatch.setattr(
        bot.session_manager, "send_topic_text_to_window", _send_topic_text_to_window
    )
    monkeypatch.setattr(bot, "enqueue_status_update", _unexpected_status_update)
    monkeypatch.setattr(bot, "enqueue_progress_clear", _unexpected_progress_clear)
    monkeypatch.setattr(
        bot, "note_run_activity", lambda **_kwargs: events.append(("run_activity", None))
    )
    monkeypatch.setattr(
        bot, "note_run_started", lambda **_kwargs: events.append(("run_started", None))
    )
    monkeypatch.setattr(bot, "_set_eyes_reaction", _set_eyes)

    await bot.text_handler(update, context)

    event_names = [name for name, _ in events]
    assert "send_to_window" in event_names
    assert event_names.count("run_activity") == 1
    assert "run_started" not in event_names
    assert "status_update" not in event_names
    assert "progress_clear" not in event_names


@pytest.mark.asyncio
async def test_text_handler_unbound_topic_app_server_only_skips_legacy_window_listing(
    monkeypatch,
):
    replies: list[tuple[str, object | None]] = []

    class _Chat:
        type = "supergroup"
        id = -100123

        async def send_action(self, *_args, **_kwargs):
            return None

    class _Message:
        def __init__(self) -> None:
            self.text = "new task"
            self.chat = _Chat()
            self.message_thread_id = 777

    message = _Message()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1147817421),
        effective_message=message,
        effective_chat=message.chat,
        message=message,
    )
    context = SimpleNamespace(bot=object(), user_data={})

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_codex_app_server_enabled", lambda: True)
    monkeypatch.setattr(bot.config, "runtime_mode", "app_server_only")
    monkeypatch.setattr(bot, "_can_user_create_sessions", lambda _uid: True)
    monkeypatch.setattr(
        bot,
        "_sorted_machine_choices",
        lambda: [SimpleNamespace(machine_id="local", display_name="Local", status="online")],
    )
    monkeypatch.setattr(bot, "_local_machine_identity", lambda: ("local", "Local"))
    monkeypatch.setattr(
        bot.session_manager, "set_group_chat_id", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_for_thread",
        lambda _uid, _tid, **_kwargs: None,
    )

    async def _list_windows():
        raise AssertionError("legacy list_windows should not run")

    monkeypatch.setattr(bot, "resolve_browse_root", lambda _root: "/tmp")
    monkeypatch.setattr(
        bot,
        "build_directory_browser",
        lambda *_args, **_kwargs: ("browse", "keyboard", ["a"]),
    )

    async def _safe_reply(_message, text: str, reply_markup=None, **_kwargs):
        replies.append((text, reply_markup))

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.text_handler(update, context)

    assert replies == [("browse", "keyboard")]
    assert context.user_data[bot.STATE_KEY] == bot.STATE_BROWSING_DIRECTORY
    assert context.user_data["_pending_thread_id"] == 777
    assert context.user_data["_pending_thread_text"] == "new task"


@pytest.mark.asyncio
async def test_text_handler_bound_topic_app_server_only_skips_legacy_window_lookup(
    monkeypatch,
):
    events: list[str] = []

    class _Chat:
        type = "supergroup"
        id = -100123

        async def send_action(self, *_args, **_kwargs):
            events.append("typing")
            return None

    class _Message:
        def __init__(self) -> None:
            self.text = "ship it"
            self.chat = _Chat()
            self.message_thread_id = 777
            self.message_id = 1

    message = _Message()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1147817421),
        effective_message=message,
        effective_chat=message.chat,
        message=message,
    )
    context = SimpleNamespace(bot=object(), user_data={})

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_codex_app_server_enabled", lambda: True)
    monkeypatch.setattr(bot.config, "runtime_mode", "app_server_only")
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
        ),
    )

    async def _find_window_by_id(*_args, **_kwargs):
        raise AssertionError("legacy lookup should not run in app_server_only bound flow")


    async def _is_window_in_progress(_uid: int, _tid: int | None, _wid: str, **_kwargs) -> bool:
        return False

    async def _send_topic_text_to_window(
        *,
        user_id: int,
        thread_id: int | None,
        chat_id: int | None = None,
        window_id: str,
        text: str,
        steer: bool = False,
        topic_ownership=None,
    ):
        _ = user_id, thread_id, chat_id, text, steer, topic_ownership
        events.append(f"send:{window_id}")
        return True, "ok"

    async def _enqueue_status_update(*_args, **_kwargs):
        events.append("status")

    async def _enqueue_progress_clear(*_args, **_kwargs):
        events.append("progress_clear")

    async def _enqueue_progress_start(*_args, **_kwargs):
        events.append("progress_start")

    async def _set_eyes(_message):
        events.append("eyes")

    monkeypatch.setattr(bot, "_is_window_in_progress", _is_window_in_progress)
    monkeypatch.setattr(
        bot.session_manager, "send_topic_text_to_window", _send_topic_text_to_window
    )
    monkeypatch.setattr(bot, "enqueue_status_update", _enqueue_status_update)
    monkeypatch.setattr(bot, "enqueue_progress_clear", _enqueue_progress_clear)
    monkeypatch.setattr(bot, "enqueue_progress_start", _enqueue_progress_start)
    monkeypatch.setattr(bot, "note_run_started", lambda **_kwargs: events.append("started"))
    monkeypatch.setattr(bot, "_set_eyes_reaction", _set_eyes)

    await bot.text_handler(update, context)

    assert "send:@900000" in events
    assert "status" in events
    assert "progress_clear" in events
    assert "progress_start" in events
    assert "started" in events
    assert "eyes" in events
    assert events.index("eyes") < events.index("send:@900000")


@pytest.mark.asyncio
async def test_cancel_topic_delivery_discards_only_target_topic_tasks():
    user_id = 7711
    canceled_thread_id = 111
    retained_thread_id = 222
    queue: asyncio.Queue[mq.MessageTask] = asyncio.Queue()
    lock = asyncio.Lock()
    mq._message_queues[user_id] = queue
    mq._queue_locks[user_id] = lock

    canceled = mq.MessageTask(
        task_type="content",
        parts=["late child update"],
        thread_id=canceled_thread_id,
    )
    retained = mq.MessageTask(
        task_type="content",
        parts=["other topic"],
        thread_id=retained_thread_id,
    )
    mq._put_queued_task(user_id, queue, canceled)
    mq._put_queued_task(user_id, queue, retained)

    try:
        removed = await mq.cancel_topic_delivery(user_id, canceled_thread_id)

        assert removed == 1
        assert queue.get_nowait() is retained
        queue.task_done()
        assert await mq.get_pending_delivery_topics(user_id) == {retained_thread_id}
    finally:
        mq._message_queues.pop(user_id, None)
        mq._queue_locks.pop(user_id, None)
        mq._queued_delivery_topic_counts.pop(user_id, None)


@pytest.mark.asyncio
async def test_cancel_topic_delivery_is_scoped_to_chat():
    user_id = 7716
    thread_id = 77
    queue: asyncio.Queue[mq.MessageTask] = asyncio.Queue()
    mq._message_queues[user_id] = queue
    mq._queue_locks[user_id] = asyncio.Lock()
    canceled = mq.MessageTask(
        task_type="content", parts=["chat one"], thread_id=thread_id, chat_id=-1001
    )
    retained = mq.MessageTask(
        task_type="content", parts=["chat two"], thread_id=thread_id, chat_id=-1002
    )
    mq._put_queued_task(user_id, queue, canceled)
    mq._put_queued_task(user_id, queue, retained)

    try:
        assert await mq.get_pending_delivery_topics(user_id, -1001) == {thread_id}
        assert await mq.get_pending_delivery_topics(user_id, -1002) == {thread_id}
        removed = await mq.cancel_topic_delivery(user_id, thread_id, chat_id=-1001)

        assert removed == 1
        assert await mq.get_pending_delivery_topics(user_id, -1001) == set()
        assert await mq.get_pending_delivery_topics(user_id, -1002) == {thread_id}
        assert queue.get_nowait() is retained
        queue.task_done()
    finally:
        while not queue.empty():
            queue.get_nowait()
            queue.task_done()
        mq._message_queues.pop(user_id, None)
        mq._queue_locks.pop(user_id, None)
        mq._queued_delivery_topic_counts.pop(user_id, None)
        mq._topic_delivery_generations.pop((user_id, -1001, thread_id), None)


def test_discard_queued_topic_inputs_before_generation_preserves_later_guidance():
    """An interrupt cleanup cutoff must not remove guidance added after click."""
    user_id = 7718
    thread_id = 78
    chat_id = -10078
    mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)
    mq._queued_topic_input_generations.pop((user_id, chat_id, thread_id), None)

    try:
        mq.enqueue_queued_topic_input(user_id, thread_id, "captured", chat_id, 1)
        cutoff = mq.get_queued_topic_input_generation(user_id, thread_id, chat_id)
        mq.enqueue_queued_topic_input(user_id, thread_id, "added later", chat_id, 2)

        removed = mq.discard_queued_topic_inputs_before_generation(
            user_id,
            thread_id,
            chat_id,
            generation_cutoff=cutoff,
        )

        assert removed == 1
        assert mq.get_queued_topic_input_snapshot(user_id, thread_id, chat_id) == [
            ("added later", chat_id, 2)
        ]
    finally:
        mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)
        mq._queued_topic_input_generations.pop((user_id, chat_id, thread_id), None)


@pytest.mark.asyncio
async def test_cancel_topic_delivery_generation_cutoff_preserves_later_delivery():
    """A successful interrupt removes only delivery queued at click time."""
    user_id = 7719
    thread_id = 79
    chat_id = -10079
    queue: asyncio.Queue[mq.MessageTask] = asyncio.Queue()
    mq._message_queues[user_id] = queue
    mq._queue_locks[user_id] = asyncio.Lock()
    mq._topic_delivery_generations[(user_id, chat_id, thread_id)] = 0

    try:
        captured = mq.MessageTask(
            task_type="content", parts=["captured"], thread_id=thread_id, chat_id=chat_id
        )
        mq._put_queued_task(user_id, queue, captured)
        cutoff = mq.get_topic_delivery_generation(user_id, thread_id, chat_id)
        later = mq.MessageTask(
            task_type="content", parts=["added later"], thread_id=thread_id, chat_id=chat_id
        )
        mq._put_queued_task(user_id, queue, later)

        removed = await mq.cancel_topic_delivery(
            user_id,
            thread_id,
            chat_id=chat_id,
            generation_cutoff=cutoff,
        )

        assert removed == 1
        retained = queue.get_nowait()
        assert retained is later
        assert mq.is_task_delivery_current(user_id, retained) is True
        queue.task_done()
    finally:
        while not queue.empty():
            queue.get_nowait()
            queue.task_done()
        mq._message_queues.pop(user_id, None)
        mq._queue_locks.pop(user_id, None)
        mq._queued_delivery_topic_counts.pop(user_id, None)
        mq._topic_delivery_generations.pop((user_id, chat_id, thread_id), None)


@pytest.mark.asyncio
async def test_progress_and_status_tracking_are_scoped_to_chat(monkeypatch):
    user_id = 7717
    thread_id = 77
    sent_id = 8000

    async def _send(*_args, **_kwargs):
        nonlocal sent_id
        sent_id += 1
        return SimpleNamespace(message_id=sent_id)

    class _Bot:
        async def delete_message(self, **_kwargs):
            raise AssertionError("one chat must not delete the other chat's message")

    monkeypatch.setattr(mq, "send_with_fallback", _send)
    try:
        for chat_id in (-1001, -1002):
            await mq._do_send_progress_message(
                _Bot(), user_id, thread_id, "@7", "working", chat_id=chat_id
            )
            await mq._do_send_status_message(
                _Bot(), user_id, thread_id, "@7", "status", chat_id=chat_id
            )

        assert (user_id, -1001, thread_id) in mq._progress_msg_info
        assert (user_id, -1002, thread_id) in mq._progress_msg_info
        assert (user_id, -1001, thread_id) in mq._status_msg_info
        assert (user_id, -1002, thread_id) in mq._status_msg_info
    finally:
        for chat_id in (-1001, -1002):
            mq._progress_msg_info.pop((user_id, chat_id, thread_id), None)
            mq._status_msg_info.pop((user_id, chat_id, thread_id), None)


@pytest.mark.asyncio
async def test_cancel_topic_delivery_preserves_task_enqueued_after_generation_advance(monkeypatch):
    user_id = 7714
    thread_id = 444
    queue: asyncio.Queue[mq.MessageTask] = asyncio.Queue()
    mq._message_queues[user_id] = queue
    mq._queue_locks[user_id] = asyncio.Lock()
    stale = mq.MessageTask(task_type="content", parts=["stale"], thread_id=thread_id)
    fresh = mq.MessageTask(task_type="content", parts=["fresh"], thread_id=thread_id)
    mq._put_queued_task(user_id, queue, stale)
    original_inspect = mq._inspect_queue

    def _inspect_after_concurrent_enqueue(target_queue):
        mq._put_queued_task(user_id, target_queue, fresh)
        return original_inspect(target_queue)

    monkeypatch.setattr(mq, "_inspect_queue", _inspect_after_concurrent_enqueue)

    try:
        removed = await mq.cancel_topic_delivery(user_id, thread_id)

        assert removed == 1
        assert queue.get_nowait() is fresh
        queue.task_done()
    finally:
        while not queue.empty():
            queue.get_nowait()
            queue.task_done()
        mq._message_queues.pop(user_id, None)
        mq._queue_locks.pop(user_id, None)
        mq._queued_delivery_topic_counts.pop(user_id, None)
        mq._topic_delivery_generations.pop((user_id, 0, thread_id), None)


@pytest.mark.asyncio
async def test_cancel_topic_delivery_invalidates_already_dequeued_task():
    user_id = 7712
    thread_id = 333
    queue: asyncio.Queue[mq.MessageTask] = asyncio.Queue()
    mq._message_queues[user_id] = queue
    mq._queue_locks[user_id] = asyncio.Lock()
    task = mq.MessageTask(task_type="content", parts=["stale"], thread_id=thread_id)
    mq._put_queued_task(user_id, queue, task)
    dequeued = queue.get_nowait()
    mq._untrack_queued_task(user_id, dequeued)

    try:
        assert mq.is_task_delivery_current(user_id, dequeued) is True

        await mq.cancel_topic_delivery(user_id, thread_id)

        assert mq.is_task_delivery_current(user_id, dequeued) is False
    finally:
        queue.task_done()
        mq._message_queues.pop(user_id, None)
        mq._queue_locks.pop(user_id, None)
        mq._queued_delivery_topic_counts.pop(user_id, None)
        mq._topic_delivery_generations.pop((user_id, 0, thread_id), None)


@pytest.mark.asyncio
async def test_worker_rechecks_delivery_generation_after_content_merge(monkeypatch):
    user_id = 7713
    thread_id = 334
    queue: asyncio.Queue[mq.MessageTask] = asyncio.Queue()
    mq._message_queues[user_id] = queue
    mq._queue_locks[user_id] = asyncio.Lock()
    delivered: list[mq.MessageTask] = []

    async def _merge(_queue, task, _lock, *, user_id):
        await mq.cancel_topic_delivery(user_id, thread_id)
        return task, 0

    async def _process(_bot, _user_id, task):
        delivered.append(task)

    monkeypatch.setattr(mq, "_merge_content_tasks", _merge)
    monkeypatch.setattr(mq, "_process_content_task", _process)
    mq._put_queued_task(
        user_id,
        queue,
        mq.MessageTask(task_type="content", parts=["stale"], thread_id=thread_id),
    )
    worker = asyncio.create_task(mq._message_queue_worker(object(), user_id))

    try:
        await asyncio.wait_for(queue.join(), timeout=0.5)
        assert delivered == []
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        mq._message_queues.pop(user_id, None)
        mq._queue_locks.pop(user_id, None)
        mq._queued_delivery_topic_counts.pop(user_id, None)
        mq._topic_delivery_generations.pop((user_id, 0, thread_id), None)


@pytest.mark.asyncio
async def test_content_task_stops_between_parts_after_topic_cancellation(monkeypatch):
    user_id = 7715
    thread_id = 445
    sent_parts: list[str] = []
    mq._topic_delivery_generations[(user_id, 0, thread_id)] = 0
    task = mq.MessageTask(
        task_type="content",
        window_id="@9",
        thread_id=thread_id,
        parts=["first", "late second"],
        delivery_generation=0,
        topic_ownership=_TEST_TOPIC_OWNERSHIP,
    )

    monkeypatch.setattr(mq.session_manager, "resolve_chat_id", lambda *_args: -100123)
    monkeypatch.setattr(mq, "_convert_status_to_content", lambda *_args, **_kwargs: None)

    async def _convert(*_args, **_kwargs):
        return None

    async def _send(_bot, _chat_id, text, **_kwargs):
        sent_parts.append(text)
        if text == "first":
            await mq.cancel_topic_delivery(user_id, thread_id)
        return SimpleNamespace(message_id=len(sent_parts))

    async def _status(*_args, **_kwargs):
        return None

    monkeypatch.setattr(mq, "_convert_status_to_content", _convert)
    monkeypatch.setattr(mq, "send_with_fallback", _send)
    monkeypatch.setattr(mq, "_check_and_send_status", _status)

    try:
        await mq._process_content_task(object(), user_id, task)
        assert sent_parts == ["first"]
    finally:
        mq._topic_delivery_generations.pop((user_id, 0, thread_id), None)


@pytest.mark.asyncio
async def test_text_handler_auto_queues_when_host_turn_is_active(monkeypatch):
    user_id = 1147817421
    thread_id = 777
    chat_id = -100123
    mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)

    class _Chat:
        type = "supergroup"
        id = chat_id

        async def send_action(self, *_args, **_kwargs):
            raise AssertionError("typing indicator should not run while host turn is active")

    class _Message:
        def __init__(self) -> None:
            self.text = "take over after host finishes"
            self.chat = _Chat()
            self.message_thread_id = thread_id
            self.message_id = 99

    message = _Message()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_message=message,
        effective_chat=message.chat,
        message=message,
    )
    context = SimpleNamespace(bot=object(), user_data={})
    events: list[str] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_chat_allowed", lambda _chat: True)
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
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_mention_only",
        lambda _wid: False,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "is_window_external_turn_active",
        lambda _wid: True,
    )

    async def _unexpected_send_topic_text_to_window(**_kwargs):
        raise AssertionError("message should be queued until the host turn completes")

    async def _sync_queue_dock(*_args, **_kwargs):
        events.append("dock")

    async def _set_hourglass(_message):
        events.append("hourglass")

    async def _safe_reply(_message, text: str, **_kwargs):
        events.append(f"safe_reply:{text}")

    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        _unexpected_send_topic_text_to_window,
    )
    monkeypatch.setattr(bot, "sync_queued_topic_dock", _sync_queue_dock)
    monkeypatch.setattr(bot, "_set_hourglass_reaction", _set_hourglass)
    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    try:
        await bot.text_handler(update, context)
        assert mq.queued_topic_input_count(user_id, thread_id, -100123) == 1
        assert "dock" in events
        assert "hourglass" in events
        assert not any(item.startswith("safe_reply:") for item in events)
    finally:
        mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)


@pytest.mark.asyncio
async def test_forward_topic_text_queues_when_send_discovers_external_writer(monkeypatch):
    user_id = 1147817421
    thread_id = 778
    chat_id = -100123
    mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)
    events: list[str] = []

    class _Chat:
        id = chat_id

    message = SimpleNamespace(
        chat=_Chat(),
        chat_id=chat_id,
        message_id=100,
    )
    context = SimpleNamespace(bot=object(), user_data={})

    monkeypatch.setattr(
        bot.session_manager,
        "get_window_for_thread",
        lambda _uid, _tid, **_kwargs: "@900000",
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda _uid, _tid, **_kwargs: SimpleNamespace(
            codex_thread_id="thread-old",
            cwd="/tmp/demo",
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_topic_response_mode",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "is_coco_control_topic",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_mention_only",
        lambda _wid: False,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "is_window_external_turn_active",
        lambda _wid: False,
    )

    async def _is_window_in_progress(*_args, **_kwargs):
        return False

    async def _send_topic_text_to_window(**_kwargs):
        return False, "Latest Codex run has an active writer."

    async def _noop_async(*_args, **_kwargs):
        return None

    async def _set_hourglass(_message):
        events.append("hourglass")

    async def _sync_dock(*_args, **_kwargs):
        events.append("dock")

    async def _safe_reply(_message, text: str, **_kwargs):
        events.append(f"safe_reply:{text}")

    async def _dispatch_retry(*_args, **_kwargs):
        events.append("dispatch_retry")

    monkeypatch.setattr(bot, "_is_window_in_progress", _is_window_in_progress)
    monkeypatch.setattr(bot, "_start_ingress_ack", lambda _message: [])
    monkeypatch.setattr(bot, "enqueue_status_update", _noop_async)
    monkeypatch.setattr(bot, "enqueue_progress_clear", _noop_async)
    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        _send_topic_text_to_window,
    )
    monkeypatch.setattr(bot, "_set_hourglass_reaction", _set_hourglass)
    monkeypatch.setattr(bot, "sync_queued_topic_dock", _sync_dock)
    monkeypatch.setattr(bot, "safe_reply", _safe_reply)
    monkeypatch.setattr(bot, "_dispatch_next_queued_input", _dispatch_retry)

    try:
        await bot._forward_topic_text_message(
            message=message,
            context=context,
            user_id=user_id,
            thread_id=thread_id,
            chat_id=chat_id,
            text="keep this exact question",
        )

        assert mq.get_queued_topic_input_snapshot(user_id, thread_id, chat_id) == [
            ("keep this exact question", chat_id, 100)
        ]
        assert events == ["hourglass", "dock", "dispatch_retry"]
    finally:
        mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)


@pytest.mark.asyncio
async def test_forward_topic_text_does_not_queue_uncertain_result_during_external_race(
    monkeypatch,
):
    user_id = 1147817421
    thread_id = 779
    chat_id = -100123
    mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)
    events: list[str] = []

    message = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        chat_id=chat_id,
        message_id=101,
    )
    context = SimpleNamespace(bot=object(), user_data={})

    monkeypatch.setattr(
        bot.session_manager,
        "get_window_for_thread",
        lambda _uid, _tid, **_kwargs: "@900000",
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda _uid, _tid, **_kwargs: SimpleNamespace(
            codex_thread_id="thread-old",
            cwd="/tmp/demo",
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_topic_response_mode",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "is_coco_control_topic",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_mention_only",
        lambda _wid: False,
    )
    external_checks = iter([False, True])
    monkeypatch.setattr(
        bot.session_manager,
        "is_window_external_turn_active",
        lambda _wid: next(external_checks),
    )

    async def _not_in_progress(*_args, **_kwargs):
        return False

    async def _send_uncertain(**_kwargs):
        return (
            False,
            "the outcome is uncertain and the request will not be replayed automatically",
        )

    async def _noop_async(*_args, **_kwargs):
        return None

    async def _safe_reply(_message, text: str, **_kwargs):
        events.append(f"safe_reply:{text}")

    async def _unexpected_dispatch(*_args, **_kwargs):
        raise AssertionError("uncertain send must not schedule a replay")

    monkeypatch.setattr(bot, "_is_window_in_progress", _not_in_progress)
    monkeypatch.setattr(bot, "_start_ingress_ack", lambda _message: [])
    monkeypatch.setattr(bot, "enqueue_status_update", _noop_async)
    monkeypatch.setattr(bot, "enqueue_progress_clear", _noop_async)
    monkeypatch.setattr(bot.session_manager, "send_topic_text_to_window", _send_uncertain)
    monkeypatch.setattr(bot, "sync_queued_topic_dock", _noop_async)
    monkeypatch.setattr(bot, "safe_reply", _safe_reply)
    monkeypatch.setattr(bot, "_dispatch_next_queued_input", _unexpected_dispatch)

    try:
        await bot._forward_topic_text_message(
            message=message,
            context=context,
            user_id=user_id,
            thread_id=thread_id,
            chat_id=chat_id,
            text="do not replay me",
        )

        assert mq.get_queued_topic_input_snapshot(user_id, thread_id, chat_id) == []
        assert events
        assert "will not be replayed automatically" in events[0]
    finally:
        mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)


@pytest.mark.asyncio
async def test_text_handler_mentions_only_skips_non_mention_text(monkeypatch):
    events: list[str] = []

    class _Chat:
        type = "supergroup"
        id = -100123

        async def send_action(self, *_args, **_kwargs):
            events.append("typing")
            return None

    class _Message:
        def __init__(self) -> None:
            self.text = "ship it"
            self.chat = _Chat()
            self.message_thread_id = 777
            self.message_id = 1

    message = _Message()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1147817421),
        effective_message=message,
        effective_chat=message.chat,
        message=message,
    )
    context = SimpleNamespace(
        bot=SimpleNamespace(username="Terminex_bot"),
        user_data={},
    )

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_chat_allowed", lambda _chat: True)
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
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_mention_only",
        lambda _wid: True,
    )

    async def _is_window_in_progress(_uid: int, _tid: int | None, _wid: str, **_kwargs) -> bool:
        events.append("checked_progress")
        return False

    async def _send_topic_text_to_window(**_kwargs):
        events.append("send")
        return True, ""

    monkeypatch.setattr(bot, "_is_window_in_progress", _is_window_in_progress)
    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        _send_topic_text_to_window,
    )

    await bot.text_handler(update, context)

    assert "send" not in events
    assert "typing" not in events
    assert "checked_progress" not in events


@pytest.mark.asyncio
async def test_text_handler_mentions_only_allows_bot_mentions(monkeypatch):
    events: list[str] = []

    class _Chat:
        type = "supergroup"
        id = -100123

        async def send_action(self, *_args, **_kwargs):
            events.append("typing")
            return None

    class _Message:
        def __init__(self) -> None:
            self.text = "hey @Terminex_bot ship it"
            self.chat = _Chat()
            self.message_thread_id = 777
            self.message_id = 1

    message = _Message()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1147817421),
        effective_message=message,
        effective_chat=message.chat,
        message=message,
    )
    context = SimpleNamespace(
        bot=SimpleNamespace(username="Terminex_bot"),
        user_data={},
    )

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_chat_allowed", lambda _chat: True)
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
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_mention_only",
        lambda _wid: True,
    )

    async def _is_window_in_progress(_uid: int, _tid: int | None, _wid: str, **_kwargs) -> bool:
        return False

    async def _send_topic_text_to_window(
        *,
        user_id: int,
        thread_id: int | None,
        chat_id: int | None = None,
        window_id: str,
        text: str,
        steer: bool = False,
        topic_ownership=None,
    ):
        _ = user_id, thread_id, chat_id, text, steer, topic_ownership
        events.append(f"send:{window_id}")
        return True, "ok"

    async def _enqueue_status_update(*_args, **_kwargs):
        events.append("status")

    async def _enqueue_progress_clear(*_args, **_kwargs):
        events.append("progress_clear")

    async def _enqueue_progress_start(*_args, **_kwargs):
        events.append("progress_start")

    async def _set_eyes(_message):
        events.append("eyes")

    monkeypatch.setattr(bot, "_is_window_in_progress", _is_window_in_progress)
    monkeypatch.setattr(
        bot.session_manager, "send_topic_text_to_window", _send_topic_text_to_window
    )
    monkeypatch.setattr(bot, "enqueue_status_update", _enqueue_status_update)
    monkeypatch.setattr(bot, "enqueue_progress_clear", _enqueue_progress_clear)
    monkeypatch.setattr(bot, "enqueue_progress_start", _enqueue_progress_start)
    monkeypatch.setattr(bot, "note_run_started", lambda **_kwargs: events.append("started"))
    monkeypatch.setattr(bot, "_set_eyes_reaction", _set_eyes)

    await bot.text_handler(update, context)

    assert "typing" in events
    assert "send:@900000" in events
    assert "status" in events


@pytest.mark.asyncio
async def test_forward_topic_text_message_coco_control_tell_routes_to_target(monkeypatch):
    events: list[str] = []

    class _Chat:
        id = -100123

        async def send_action(self, _action):
            events.append("typing")
            return None

    message = SimpleNamespace(
        chat=_Chat(),
        chat_id=-100123,
        message_id=321,
    )
    context = SimpleNamespace(bot=object(), user_data={})

    monkeypatch.setattr(
        bot.session_manager,
        "get_window_for_thread",
        lambda _uid, tid, **_kwargs: "@ctl" if tid == 77 else "@88",
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda _uid, tid, **_kwargs: SimpleNamespace(
            codex_thread_id=f"thread-{tid}",
            cwd=f"/tmp/{tid}",
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_topic_response_mode",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_mention_only",
        lambda _wid: False,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "is_window_external_turn_active",
        lambda _wid: False,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "is_coco_control_topic",
        lambda _uid, tid, *, chat_id=None: tid == 77 and chat_id == -100123,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "iter_topic_bindings",
        lambda: iter(
            [
                (1147817421, -100123, 77, SimpleNamespace(display_name="ccbot-codex")),
                (1147817421, -100123, 88, SimpleNamespace(display_name="bottleshot")),
            ]
        ),
    )

    async def _unexpected_in_progress(*_args, **_kwargs):
        raise AssertionError("control routing should bypass normal in-progress detection")

    async def _send_topic_text_to_window(**kwargs):
        events.append(f"send:{kwargs['thread_id']}:{kwargs['text']}:{kwargs.get('steer')}")
        return True, "ok"

    async def _noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bot, "_is_window_in_progress", _unexpected_in_progress)
    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        _send_topic_text_to_window,
    )
    monkeypatch.setattr(bot, "safe_reply", _noop_async)
    monkeypatch.setattr(bot, "enqueue_status_update", _noop_async)
    monkeypatch.setattr(bot, "enqueue_progress_clear", _noop_async)
    monkeypatch.setattr(bot, "enqueue_progress_start", _noop_async)
    monkeypatch.setattr(bot, "_set_eyes_reaction", _noop_async)
    monkeypatch.setattr(bot, "note_run_started", lambda **_kwargs: None)

    await bot._forward_topic_text_message(
        message=message,
        context=context,
        user_id=1147817421,
        thread_id=77,
        chat_id=-100123,
        text="tell bottleshot to focus on the PDF bug",
    )

    assert "typing" in events
    assert "send:88:focus on the PDF bug:True" in events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "caller_user_id",
        "target_user_id",
        "caller_is_admin",
        "text",
        "expected_action",
        "send_success",
    ),
    [
        (999, 999, False, "tell mine to focus on my topic", "steer", True),
        (42, 999, True, "tell mine to focus on my topic", "steer", True),
        (999, 999, False, "queue for mine: focus on my topic", "queue", True),
        (999, 999, False, "tell mine to fail on my topic", "steer", False),
    ],
)
async def test_general_control_actions_route_authorized_topics(
    monkeypatch,
    caller_user_id,
    target_user_id,
    caller_is_admin,
    text,
    expected_action,
    send_success,
):
    chat_id = -100123
    target_thread_id = 88
    events: list[str] = []
    replies: list[str] = []
    activity_calls: list[dict] = []
    started_calls: list[dict] = []

    class _Chat:
        id = chat_id

        async def send_action(self, _action):
            events.append("typing")

    message = SimpleNamespace(
        chat=_Chat(),
        chat_id=chat_id,
        message_id=321,
    )
    context = SimpleNamespace(bot=object(), user_data={})

    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: caller_is_admin)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(100, 1, chat_id),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "is_coco_control_topic",
        lambda uid, tid, *, chat_id=None: (
            (uid, tid) == (100, 1) and chat_id == -100123
        ),
    )
    monkeypatch.setattr(bot.session_manager, "set_group_chat_id", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "iter_topic_bindings",
        lambda: iter(
            [
                (100, chat_id, 1, SimpleNamespace(display_name="coco-control")),
                (
                    target_user_id,
                    chat_id,
                    target_thread_id,
                    SimpleNamespace(display_name="mine"),
                ),
            ]
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_for_thread",
        lambda uid, tid, **_kwargs: (
            "@mine"
            if (uid, tid) == (target_user_id, target_thread_id)
            else "@control"
            if (uid, tid) == (100, 1)
            else pytest.fail("control routing resolved an unexpected session")
        ),
    )
    monkeypatch.setattr(
        bot,
        "capture_topic_ownership",
        lambda uid, tid, _chat_id: bot.TopicOwnership(
            window_id="@mine" if (uid, tid) == (target_user_id, target_thread_id) else "@control",
            codex_thread_id=(
                "codex-mine"
                if (uid, tid) == (target_user_id, target_thread_id)
                else "codex-control"
            ),
            machine_id="local-node",
            cwd=(
                "/workspace/mine"
                if (uid, tid) == (target_user_id, target_thread_id)
                else "/workspace/control"
            ),
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda uid, tid, **_kwargs: (
            SimpleNamespace(
                window_id="@mine" if (uid, tid) == (target_user_id, target_thread_id) else "@control",
                codex_thread_id="codex-topic",
                cwd="/workspace/topic",
            )
            if (uid, tid) in {
                (target_user_id, target_thread_id),
                (100, 1),
            }
            else pytest.fail("must resolve the target or control topic")
        ),
    )
    async def _noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bot, "safe_reply", _noop_async)
    monkeypatch.setattr(bot, "_set_eyes_reaction", _noop_async)
    monkeypatch.setattr(bot, "_set_hourglass_reaction", _noop_async)
    monkeypatch.setattr(bot, "sync_queued_topic_dock", _noop_async)
    monkeypatch.setattr(
        bot,
        "enqueue_queued_topic_input",
        lambda uid, tid, *_args, **_kwargs: (
            events.append(f"queue:{uid}:{tid}") or 1
        ),
    )

    async def _send(**kwargs):
        events.append(
            f"send:{kwargs['user_id']}:{kwargs['thread_id']}:{kwargs.get('steer')}"
        )
        if send_success and kwargs.get("dispatch_state") is not None:
            kwargs["dispatch_state"].mark_turn_started()
        return send_success, "ok" if send_success else "failed"

    monkeypatch.setattr(bot.session_manager, "send_topic_text_to_window", _send)
    monkeypatch.setattr(
        bot,
        "note_run_activity",
        lambda **kwargs: activity_calls.append(kwargs),
    )
    monkeypatch.setattr(
        bot,
        "note_run_started",
        lambda **kwargs: started_calls.append(kwargs),
    )

    await bot._forward_topic_text_message(
        message=message,
        context=context,
        user_id=caller_user_id,
        thread_id=1,
        chat_id=chat_id,
        text=text,
    )

    if expected_action == "steer":
        assert f"send:{target_user_id}:88:True" in events
    else:
        assert f"queue:{target_user_id}:88" in events
    assert "typing" in events if expected_action == "steer" else True
    if expected_action == "steer" and send_success:
        assert activity_calls == []
        assert started_calls == [
            {
                "user_id": target_user_id,
                "thread_id": target_thread_id,
                "chat_id": chat_id,
                "window_id": "@mine",
                "source": "steer_input",
                "pending_text": "focus on my topic",
                "expect_response": True,
            }
        ]
    else:
        assert activity_calls == []
        assert started_calls == []
    assert replies == []


@pytest.mark.asyncio
async def test_forward_general_message_activates_default_coco_control(monkeypatch):
    events: list[str] = []
    activated: list[tuple[int, int, int | None]] = []
    message = SimpleNamespace(
        chat=SimpleNamespace(id=-100123),
        chat_id=-100123,
        message_id=655,
    )
    context = SimpleNamespace(bot=object(), user_data={})
    binding = SimpleNamespace(
        window_id="@general",
        codex_thread_id="thread-general",
        cwd="/tmp/general",
    )

    def _activate_general(*, user_id: int, thread_id: int, chat_id: int | None):
        activated.append((user_id, thread_id, chat_id))
        return binding

    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", _activate_general)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(1147817421, 1, -100123),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_for_thread",
        lambda _uid, tid, **_kwargs: "@general" if tid == 1 else None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda _uid, _tid, **_kwargs: binding,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_topic_response_mode",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "is_coco_control_topic",
        lambda _uid, tid, *, chat_id=None: tid == 1 and chat_id == -100123,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_mention_only",
        lambda _wid: False,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "is_window_external_turn_active",
        lambda _wid: False,
    )

    async def _not_in_progress(*_args, **_kwargs):
        return False

    async def _send(**kwargs):
        events.append(f"send:{kwargs['window_id']}:{kwargs['text']}")
        return True, "ok"

    async def _noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bot, "capture_topic_ownership", lambda *_args: object())
    monkeypatch.setattr(bot, "_is_window_in_progress", _not_in_progress)
    monkeypatch.setattr(bot, "_start_ingress_ack", lambda _message: [])
    monkeypatch.setattr(bot.session_manager, "send_topic_text_to_window", _send)
    monkeypatch.setattr(bot, "enqueue_status_update", _noop_async)
    monkeypatch.setattr(bot, "enqueue_progress_clear", _noop_async)
    monkeypatch.setattr(bot, "enqueue_progress_start", _noop_async)
    monkeypatch.setattr(bot, "_set_eyes_reaction", _noop_async)
    monkeypatch.setattr(bot, "note_run_started", lambda **_kwargs: None)

    await bot._forward_topic_text_message(
        message=message,
        context=context,
        user_id=1147817421,
        thread_id=1,
        chat_id=-100123,
        text="show me the active topics",
    )

    assert activated == [(1147817421, 1, -100123)]
    assert events == ["send:@general:show me the active topics"]


@pytest.mark.asyncio
async def test_text_handler_rejects_unconfigured_general_without_caller_routing(
    monkeypatch,
):
    chat_id = -100123
    message = SimpleNamespace(
        text="stale General prompt",
        message_thread_id=1,
        chat=SimpleNamespace(type="supergroup", id=chat_id, is_forum=True),
        chat_id=chat_id,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=999),
        effective_message=message,
        effective_chat=message.chat,
        message=message,
    )
    context = SimpleNamespace(bot=object(), user_data={})
    replies: list[str] = []
    routing_users: list[int] = []

    monkeypatch.setattr(bot, "_is_chat_allowed", lambda _chat: True)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda uid, *_args, **_kwargs: routing_users.append(uid),
    )
    async def _forward(**_kwargs):
        return None

    monkeypatch.setattr(bot, "_forward_topic_text_message", _forward)

    async def _reply(_message, text, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _reply)

    await bot.text_handler(update, context)

    assert replies == [bot._COCO_CONTROL_UNCONFIGURED_TEXT]
    assert routing_users == []


@pytest.mark.asyncio
async def test_text_handler_general_admin_routes_canonical_owner(monkeypatch):
    chat_id = -100123
    owner_user_id = 100
    admin_user_id = 200
    message = SimpleNamespace(
        text="admin General prompt",
        message_thread_id=1,
        chat=SimpleNamespace(type="supergroup", id=chat_id, is_forum=True),
        chat_id=chat_id,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=admin_user_id),
        effective_message=message,
        effective_chat=message.chat,
        message=message,
    )
    context = SimpleNamespace(bot=object(), user_data={})
    routing_users: list[int] = []

    monkeypatch.setattr(bot, "_is_chat_allowed", lambda _chat: True)
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
        lambda uid, *_args, **_kwargs: routing_users.append(uid),
    )

    async def _forward(**_kwargs):
        return None

    monkeypatch.setattr(bot, "_forward_topic_text_message", _forward)

    await bot.text_handler(update, context)

    assert routing_users == [owner_user_id]


@pytest.mark.asyncio
async def test_text_handler_allows_pending_dashboard_steer_for_caller_owned_topic(
    monkeypatch,
):
    """A member's pending dashboard steer must pass the General pre-auth gate."""
    caller_user_id = 999
    control_owner_user_id = 100
    target_thread_id = 88
    chat_id = -100123
    context = SimpleNamespace(
        bot=object(),
        user_data={
            "_coco_dashboard_steer": {
                "owner_user_id": caller_user_id,
                "chat_id": chat_id,
                "thread_id": target_thread_id,
                "created_at": bot.time.monotonic(),
                "ownership": {
                    "window_id": "@88",
                    "codex_thread_id": "codex-88",
                    "machine_id": "node-a",
                    "cwd": "/workspace/88",
                },
            }
        },
    )
    chat = SimpleNamespace(type="supergroup", id=chat_id, is_forum=True)
    message = SimpleNamespace(
        text="steer my selected topic",
        message_thread_id=1,
        chat=chat,
        chat_id=chat_id,
        message_id=1,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=caller_user_id),
        effective_message=message,
        effective_chat=chat,
        message=message,
    )
    sends: list[dict] = []
    replies: list[str] = []

    monkeypatch.setattr(bot, "_is_chat_allowed", lambda _chat: True)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: False)
    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(control_owner_user_id, 1, chat_id),
    )
    monkeypatch.setattr(bot.session_manager, "set_group_chat_id", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda uid, tid, **_kwargs: SimpleNamespace(window_id="@88")
        if (uid, tid) == (caller_user_id, target_thread_id)
        else None,
    )
    monkeypatch.setattr(bot, "is_topic_ownership_current", lambda *_args: True)

    async def _send(**kwargs):
        sends.append(kwargs)
        return True, "ok"

    async def _reply(_message, text, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot.session_manager, "send_topic_text_to_window", _send)
    monkeypatch.setattr(bot, "safe_reply", _reply)

    await bot.text_handler(update, context)

    assert sends and sends[0]["user_id"] == caller_user_id
    assert sends[0]["thread_id"] == target_thread_id
    assert sends[0]["steer"] is True
    assert replies == [f"✅ Steered topic `{target_thread_id}`."]
    assert "_coco_dashboard_steer" not in context.user_data


@pytest.mark.asyncio
async def test_dashboard_steer_button_routes_the_next_general_message(monkeypatch):
    sends: list[dict] = []
    replies: list[str] = []
    activity_calls: list[dict] = []
    context = SimpleNamespace(
        bot=object(),
        user_data={
            "_coco_dashboard_steer": {
                "owner_user_id": 200,
                "chat_id": -100123,
                "thread_id": 88,
                "created_at": bot.time.monotonic(),
                "ownership": {
                    "window_id": "@88",
                    "codex_thread_id": "codex-88",
                    "machine_id": "node-a",
                    "cwd": "/workspace/88",
                },
            }
        },
    )
    message = SimpleNamespace(chat_id=-100123, message_id=1)
    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: True)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(100, 1, -100123),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda uid, tid, **_kwargs: SimpleNamespace(window_id="@88")
        if (uid, tid) == (200, 88)
        else None,
    )
    monkeypatch.setattr(
        bot,
        "capture_topic_ownership",
        lambda *_args, **_kwargs: bot.TopicOwnership(
            window_id="@88",
            codex_thread_id="codex-88",
            machine_id="node-a",
            cwd="/workspace/88",
        ),
    )
    monkeypatch.setattr(bot, "is_topic_ownership_current", lambda *_args: True)

    async def _send(**kwargs):
        sends.append(kwargs)
        return True, "ok"

    async def _reply(_message, text, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot.session_manager, "send_topic_text_to_window", _send)
    monkeypatch.setattr(bot, "safe_reply", _reply)
    monkeypatch.setattr(
        bot,
        "note_run_activity",
        lambda **kwargs: activity_calls.append(kwargs),
    )

    await bot._forward_topic_text_message(
        message=message,
        context=context,
        user_id=999,
        thread_id=1,
        chat_id=-100123,
        text="ship the focused fix",
    )

    assert sends[0]["user_id"] == 200
    assert sends[0]["thread_id"] == 88
    assert sends[0]["steer"] is True
    assert replies == ["✅ Steered topic `88`."]
    assert activity_calls == [
        {
            "user_id": 200,
            "thread_id": 88,
            "chat_id": -100123,
            "window_id": "@88",
            "source": "steer_input",
        }
    ]
    assert "_coco_dashboard_steer" not in context.user_data


@pytest.mark.asyncio
async def test_dashboard_steer_initializes_watchdog_when_transport_starts_new_turn(
    monkeypatch,
):
    """A steer fallback to turn/start must retain pending-response tracking."""
    sends: list[dict] = []
    activity_calls: list[dict] = []
    started_calls: list[dict] = []
    context = SimpleNamespace(
        bot=object(),
        user_data={
            "_coco_dashboard_steer": {
                "owner_user_id": 200,
                "chat_id": -100123,
                "thread_id": 88,
                "created_at": bot.time.monotonic(),
                "ownership": {
                    "window_id": "@88",
                    "codex_thread_id": "codex-88",
                    "machine_id": "node-a",
                    "cwd": "/workspace/88",
                },
            }
        },
    )
    message = SimpleNamespace(chat_id=-100123, message_id=1)
    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: True)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(100, 1, -100123),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda uid, tid, **_kwargs: SimpleNamespace(window_id="@88")
        if (uid, tid) == (200, 88)
        else None,
    )
    monkeypatch.setattr(
        bot,
        "capture_topic_ownership",
        lambda *_args, **_kwargs: bot.TopicOwnership(
            window_id="@88",
            codex_thread_id="codex-88",
            machine_id="node-a",
            cwd="/workspace/88",
        ),
    )
    monkeypatch.setattr(bot, "is_topic_ownership_current", lambda *_args: True)

    async def _send(**kwargs):
        sends.append(kwargs)
        dispatch_state = kwargs.get("dispatch_state")
        if dispatch_state is not None:
            dispatch_state.mark_turn_started()
        return True, "ok"

    async def _reply(_message, _text, **_kwargs):
        return None

    monkeypatch.setattr(bot.session_manager, "send_topic_text_to_window", _send)
    monkeypatch.setattr(bot, "safe_reply", _reply)
    monkeypatch.setattr(
        bot,
        "note_run_activity",
        lambda **kwargs: activity_calls.append(kwargs),
    )
    monkeypatch.setattr(
        bot,
        "note_run_started",
        lambda **kwargs: started_calls.append(kwargs),
    )

    await bot._forward_topic_text_message(
        message=message,
        context=context,
        user_id=999,
        thread_id=1,
        chat_id=-100123,
        text="ship the focused fix",
    )

    assert sends[0]["steer"] is True
    assert activity_calls == []
    assert started_calls == [
        {
            "user_id": 200,
            "thread_id": 88,
            "chat_id": -100123,
            "window_id": "@88",
            "source": "steer_input",
            "pending_text": "ship the focused fix",
            "expect_response": True,
        }
    ]
    assert "_coco_dashboard_steer" not in context.user_data


@pytest.mark.asyncio
async def test_dashboard_steer_allows_single_session_caller_to_steer_own_topic(
    monkeypatch,
):
    sends: list[dict] = []
    replies: list[str] = []
    context = SimpleNamespace(
        bot=object(),
        user_data={
            "_coco_dashboard_steer": {
                "owner_user_id": 999,
                "chat_id": -100123,
                "thread_id": 88,
                "created_at": bot.time.monotonic(),
                "ownership": {
                    "window_id": "@88",
                    "codex_thread_id": "codex-88",
                    "machine_id": "node-a",
                    "cwd": "/workspace/88",
                },
            }
        },
    )
    message = SimpleNamespace(chat_id=-100123, message_id=1)
    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: False)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(100, 1, -100123),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda uid, tid, **_kwargs: SimpleNamespace(window_id="@88")
        if (uid, tid) == (999, 88)
        else None,
    )
    monkeypatch.setattr(bot, "is_topic_ownership_current", lambda *_args: True)

    async def _send(**kwargs):
        sends.append(kwargs)
        return True, "ok"

    async def _reply(_message, text, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot.session_manager, "send_topic_text_to_window", _send)
    monkeypatch.setattr(bot, "safe_reply", _reply)

    await bot._forward_topic_text_message(
        message=message,
        context=context,
        user_id=999,
        thread_id=1,
        chat_id=-100123,
        text="ship the focused fix",
    )

    assert sends[0]["user_id"] == 999
    assert sends[0]["thread_id"] == 88
    assert sends[0]["steer"] is True
    assert replies == ["✅ Steered topic `88`."]
    assert "_coco_dashboard_steer" not in context.user_data


@pytest.mark.asyncio
async def test_dashboard_steer_waits_for_matching_general_message(monkeypatch):
    pending = {
        "owner_user_id": 200,
        "chat_id": -100123,
        "thread_id": 88,
        "created_at": bot.time.monotonic(),
    }
    context = SimpleNamespace(
        bot=object(),
        user_data={"_coco_dashboard_steer": dict(pending)},
    )
    message = SimpleNamespace(chat_id=-100123, message_id=1)
    replies: list[str] = []
    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(bot.session_manager, "get_window_for_thread", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bot, "_can_user_create_sessions", lambda _user_id: False)

    async def _reply(_message, text, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _reply)

    await bot._forward_topic_text_message(
        message=message,
        context=context,
        user_id=999,
        thread_id=55,
        chat_id=-100123,
        text="ordinary named-topic message",
    )

    assert context.user_data["_coco_dashboard_steer"] == pending
    assert replies and "single-session access" in replies[0]


@pytest.mark.asyncio
async def test_dashboard_steer_expired_intent_is_cleared_without_general_routing(
    monkeypatch,
):
    clock = {"now": 100.0}
    context = SimpleNamespace(
        bot=object(),
        user_data={
            "_coco_dashboard_steer": {
                "owner_user_id": 200,
                "chat_id": -100123,
                "thread_id": 88,
                "created_at": clock["now"]
                - bot._COCO_DASHBOARD_SNAPSHOT_TTL_SECONDS
                - 1,
            }
        },
    )
    message = SimpleNamespace(chat_id=-100123, message_id=1)
    replies: list[str] = []
    sends: list[dict] = []
    monkeypatch.setattr(bot.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        bot,
        "_ensure_default_coco_general_control",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(100, 1, -100123),
    )

    async def _send(**kwargs):
        sends.append(kwargs)
        return True, "ok"

    async def _reply(_message, text, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot.session_manager, "send_topic_text_to_window", _send)
    monkeypatch.setattr(bot, "safe_reply", _reply)

    await bot._forward_topic_text_message(
        message=message,
        context=context,
        user_id=999,
        thread_id=bot.GENERAL_TOPIC_THREAD_ID,
        chat_id=-100123,
        text="do not route this after expiry",
    )

    assert sends == []
    assert replies == ["❌ That dashboard steer expired. Refresh /coco and try again."]
    assert "_coco_dashboard_steer" not in context.user_data


@pytest.mark.asyncio
async def test_text_handler_expired_own_dashboard_steer_cannot_bypass_general_owner(
    monkeypatch,
):
    clock = {"now": 100.0}
    chat_id = -100123
    caller_user_id = 999
    context = SimpleNamespace(
        bot=object(),
        user_data={
            "_coco_dashboard_steer": {
                "owner_user_id": caller_user_id,
                "chat_id": chat_id,
                "thread_id": 88,
                "created_at": clock["now"]
                - bot._COCO_DASHBOARD_SNAPSHOT_TTL_SECONDS
                - 1,
            }
        },
    )
    chat = SimpleNamespace(type="supergroup", id=chat_id, is_forum=True)
    message = SimpleNamespace(
        text="expired steer must not become a General prompt",
        message_thread_id=bot.GENERAL_TOPIC_THREAD_ID,
        chat=chat,
        chat_id=chat_id,
        message_id=1,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=caller_user_id),
        effective_message=message,
        effective_chat=chat,
        message=message,
    )
    replies: list[str] = []
    forwarded = False
    monkeypatch.setattr(bot.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(bot, "_is_chat_allowed", lambda _chat: True)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: False)
    monkeypatch.setattr(
        bot,
        "_ensure_default_coco_general_control",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(100, 1, chat_id),
    )

    async def _forward(**_kwargs):
        nonlocal forwarded
        forwarded = True

    async def _reply(_message, text, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "_forward_topic_text_message", _forward)
    monkeypatch.setattr(bot, "safe_reply", _reply)

    await bot.text_handler(update, context)

    assert forwarded is False
    assert replies == ["❌ That dashboard steer expired. Refresh /coco and try again."]
    assert "_coco_dashboard_steer" not in context.user_data


@pytest.mark.asyncio
async def test_dashboard_steer_rejects_target_rebound_after_click(monkeypatch):
    pending = {
        "owner_user_id": 200,
        "chat_id": -100123,
        "thread_id": 88,
        "created_at": bot.time.monotonic(),
        "ownership": {
            "window_id": "@old",
            "codex_thread_id": "codex-old",
            "machine_id": "node-a",
            "cwd": "/workspace/old",
        },
    }
    context = SimpleNamespace(
        bot=object(),
        user_data={"_coco_dashboard_steer": pending},
    )
    message = SimpleNamespace(chat_id=-100123, message_id=1)
    replies: list[str] = []
    sends: list[dict] = []
    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: True)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(100, 1, -100123),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda *_args, **_kwargs: SimpleNamespace(window_id="@new"),
    )
    monkeypatch.setattr(
        bot,
        "capture_topic_ownership",
        lambda *_args, **_kwargs: bot.TopicOwnership(
            window_id="@new",
            codex_thread_id="codex-new",
            machine_id="node-b",
            cwd="/workspace/new",
        ),
    )

    async def _send(**kwargs):
        sends.append(kwargs)
        return True, ""

    async def _reply(_message, text, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot.session_manager, "send_topic_text_to_window", _send)
    monkeypatch.setattr(bot, "safe_reply", _reply)

    await bot._forward_topic_text_message(
        message=message,
        context=context,
        user_id=999,
        thread_id=1,
        chat_id=-100123,
        text="steer the selected target",
    )

    assert sends == []
    assert replies == ["❌ That dashboard target changed. Refresh /coco and try again."]
    assert "_coco_dashboard_steer" not in context.user_data


@pytest.mark.asyncio
async def test_general_message_waits_when_legacy_control_migration_is_deferred(
    monkeypatch,
):
    replies: list[str] = []
    message = SimpleNamespace(chat_id=-100123, message_id=1)
    context = SimpleNamespace(bot=object(), user_data={})
    monkeypatch.setattr(
        bot,
        "_ensure_default_coco_general_control",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(100, 77, -100123),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_for_thread",
        lambda *_args, **_kwargs: pytest.fail("must not create a competing General session"),
    )

    async def _reply(_message, text, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _reply)
    await bot._forward_topic_text_message(
        message=message,
        context=context,
        user_id=999,
        thread_id=1,
        chat_id=-100123,
        text="hello General",
    )

    assert replies and "migration is still pending" in replies[0]


def test_coco_control_target_parser_rejects_ambiguous_names(monkeypatch):
    monkeypatch.setattr(
        bot.session_manager,
        "iter_topic_bindings",
        lambda: iter(
            [
                (100, -100123, 88, SimpleNamespace(display_name="duplicate")),
                (200, -100123, 99, SimpleNamespace(display_name="duplicate")),
            ]
        ),
    )

    action = bot._parse_coco_control_action(
        user_id=100,
        thread_id=1,
        chat_id=-100123,
        text="tell duplicate to inspect the failure",
    )

    assert action == ("ambiguous", 0, 0, "duplicate", "")


@pytest.mark.asyncio
async def test_forward_topic_text_message_coco_control_queue_routes_to_target_queue(monkeypatch):
    events: list[str] = []

    class _Chat:
        id = -100123

        async def send_action(self, _action):
            events.append("typing")
            return None

    message = SimpleNamespace(
        chat=_Chat(),
        chat_id=-100123,
        message_id=654,
    )
    context = SimpleNamespace(bot=object(), user_data={})

    monkeypatch.setattr(
        bot.session_manager,
        "get_window_for_thread",
        lambda _uid, tid, **_kwargs: "@ctl" if tid == 77 else "@88",
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda _uid, tid, **_kwargs: SimpleNamespace(
            codex_thread_id=f"thread-{tid}",
            cwd=f"/tmp/{tid}",
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_topic_response_mode",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_mention_only",
        lambda _wid: False,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "is_window_external_turn_active",
        lambda _wid: False,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "is_coco_control_topic",
        lambda _uid, tid, *, chat_id=None: tid == 77 and chat_id == -100123,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "iter_topic_bindings",
        lambda: iter(
            [
                (1147817421, -100123, 77, SimpleNamespace(display_name="ccbot-codex")),
                (1147817421, -100123, 88, SimpleNamespace(display_name="bottleshot")),
            ]
        ),
    )

    async def _unexpected_send(**_kwargs):
        raise AssertionError("queue control routing should not send immediately")

    async def _noop_async(*_args, **_kwargs):
        return None

    def _enqueue(
        user_id, thread_id, text, source_chat_id, source_message_id, **_kwargs
    ):
        events.append(f"queue:{user_id}:{thread_id}:{text}:{source_chat_id}:{source_message_id}")
        return 1

    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        _unexpected_send,
    )
    monkeypatch.setattr(bot, "enqueue_queued_topic_input", _enqueue)
    monkeypatch.setattr(bot, "sync_queued_topic_dock", _noop_async)
    monkeypatch.setattr(bot, "_set_hourglass_reaction", _noop_async)
    monkeypatch.setattr(bot, "safe_reply", _noop_async)
    monkeypatch.setattr(bot, "_is_window_in_progress", _noop_async)

    await bot._forward_topic_text_message(
        message=message,
        context=context,
        user_id=1147817421,
        thread_id=77,
        chat_id=-100123,
        text="queue for bottleshot: focus on the PDF bug after this turn",
    )

    assert "queue:1147817421:88:focus on the PDF bug after this turn:-100123:654" in events


@pytest.mark.asyncio
async def test_q_enqueues_internal_queue_and_updates_dock_when_in_progress(monkeypatch):
    events: list[str] = []
    telemetry: list[tuple[str, dict[str, object]]] = []

    class _Chat:
        type = "supergroup"
        id = -100321

    class _Message:
        def __init__(self) -> None:
            self.text = "/q next task"
            self.chat = _Chat()
            self.chat_id = self.chat.id
            self.message_thread_id = 777
            self.message_id = 888

    message = _Message()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1147817421),
        effective_message=message,
        effective_chat=message.chat,
        message=message,
    )
    context = SimpleNamespace(bot=object(), user_data={})

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "runtime_mode", "hybrid")
    monkeypatch.setattr(
        bot.session_manager, "set_group_chat_id", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_window_for_thread",
        lambda _uid, _tid, **_kwargs: "@77",
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda _uid, _tid, **_kwargs: SimpleNamespace(
            codex_thread_id="thread-77",
            cwd="/tmp/project",
        ),
    )
    async def _find_window_by_id(_wid: str):
        return SimpleNamespace(
            window_id="@77",
            window_name="coco-codex",
            cwd="/tmp/project",
        )

    async def _is_window_in_progress(*_args, **_kwargs):
        return True

    monkeypatch.setattr(bot, "_is_window_in_progress", _is_window_in_progress)
    monkeypatch.setattr(bot, "_codex_app_server_enabled", lambda: True)

    async def _set_hourglass(_message):
        events.append("hourglass")

    async def _set_eyes(_message):
        events.append("eyes")

    def _enqueue(
        _uid: int,
        _tid: int,
        _text: str,
        _chat_id: int,
        _msg_id: int,
        **_kwargs,
    ):
        events.append("internal_queue")
        return 1

    async def _sync_dock(
        _bot, _uid: int, _tid: int, *, window_id: str | None = None, chat_id=None
    ):
        events.append(f"dock_sync:{window_id}")

    monkeypatch.setattr(bot, "_set_hourglass_reaction", _set_hourglass)
    monkeypatch.setattr(bot, "_set_eyes_reaction", _set_eyes)
    monkeypatch.setattr(bot, "enqueue_queued_topic_input", _enqueue)
    monkeypatch.setattr(bot, "sync_queued_topic_dock", _sync_dock)
    monkeypatch.setattr(
        bot,
        "emit_telemetry",
        lambda event, **fields: telemetry.append((event, fields)),
    )

    await bot.queue_command(update, context)

    assert events == ["internal_queue", "hourglass", "dock_sync:@77"]
    assert telemetry
    assert [event for event, _fields in telemetry] == ["queue.q_internal_enqueued"]
    assert telemetry[0][1]["queue_size"] == 1
    assert telemetry[0][1]["used_native_queue"] is False
    assert telemetry[0][1]["native_attempts"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("thread_id", "expected_user_id"),
    [
        (1, 42),
        (77, 999),
    ],
)
async def test_q_uses_the_owner_for_general_and_sender_for_named_topics(
    monkeypatch,
    thread_id,
    expected_user_id,
):
    owner_user_id = 42
    sender_user_id = 999
    chat_id = -100321
    observed_user_ids: list[tuple[str, int]] = []

    message = SimpleNamespace(
        text="/q next task",
        chat=SimpleNamespace(type="supergroup", id=chat_id),
        chat_id=chat_id,
        message_thread_id=thread_id,
        message_id=888,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=sender_user_id),
        effective_message=message,
        effective_chat=message.chat,
        message=message,
    )
    context = SimpleNamespace(bot=object(), user_data={})

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    # Cross-owner General queueing remains available to the admin path; a
    # single-session sender is covered by the denial regressions.
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: True)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(owner_user_id, 1, chat_id),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda uid, *_args, **_kwargs: observed_user_ids.append(("group", uid)),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_window_for_thread",
        lambda uid, *_args, **_kwargs: (
            observed_user_ids.append(("window", uid)) or "@control"
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda uid, *_args, **_kwargs: (
            observed_user_ids.append(("binding", uid))
            or SimpleNamespace(codex_thread_id="control-thread", cwd="/control")
        ),
    )
    monkeypatch.setattr(
        bot,
        "capture_topic_ownership",
        lambda uid, *_args, **_kwargs: (
            observed_user_ids.append(("ownership", uid)) or object()
        ),
    )
    monkeypatch.setattr(
        bot,
        "queued_topic_input_count",
        lambda uid, *_args, **_kwargs: (
            observed_user_ids.append(("count", uid)) or 0
        ),
    )
    monkeypatch.setattr(
        bot,
        "_is_queued_topic_drain_active",
        lambda uid, *_args, **_kwargs: (
            observed_user_ids.append(("drain", uid)) or False
        ),
    )

    async def _in_progress(uid, *_args, **_kwargs):
        observed_user_ids.append(("progress", uid))
        return True

    def _enqueue(uid, *_args, **_kwargs):
        observed_user_ids.append(("enqueue", uid))
        return 1

    async def _dock(_bot, uid, *_args, **_kwargs):
        observed_user_ids.append(("dock", uid))

    async def _hourglass(_message):
        return None

    monkeypatch.setattr(bot, "_is_window_in_progress", _in_progress)
    monkeypatch.setattr(bot, "enqueue_queued_topic_input", _enqueue)
    monkeypatch.setattr(bot, "sync_queued_topic_dock", _dock)
    monkeypatch.setattr(bot, "_set_hourglass_reaction", _hourglass)

    await bot.queue_command(update, context)

    assert observed_user_ids
    assert {uid for _operation, uid in observed_user_ids} == {expected_user_id}


@pytest.mark.asyncio
async def test_q_uses_internal_queue_when_app_server_turn_is_active(monkeypatch):
    events: list[str] = []
    telemetry: list[tuple[str, dict[str, object]]] = []

    class _Chat:
        type = "supergroup"
        id = -100321

    class _Message:
        def __init__(self) -> None:
            self.text = "/q next task"
            self.chat = _Chat()
            self.chat_id = self.chat.id
            self.message_thread_id = 777
            self.message_id = 888

    message = _Message()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1147817421),
        effective_message=message,
        effective_chat=message.chat,
        message=message,
    )
    context = SimpleNamespace(bot=object(), user_data={})

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "runtime_mode", "hybrid")
    monkeypatch.setattr(
        bot.session_manager, "set_group_chat_id", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_window_for_thread",
        lambda _uid, _tid, **_kwargs: "@77",
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda _uid, _tid, **_kwargs: SimpleNamespace(
            codex_thread_id="thread-77",
            cwd="/tmp/project",
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "is_window_external_turn_active",
        lambda _wid: False,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_active_turn_id",
        lambda _wid: "turn-77",
    )

    async def _is_window_in_progress(*_args, **_kwargs):
        return True

    async def _set_hourglass(_message):
        events.append("hourglass")

    def _enqueue(
        _uid: int,
        _tid: int,
        _text: str,
        _chat_id: int,
        _msg_id: int,
        **_kwargs,
    ):
        events.append("internal_queue")
        return 1

    async def _sync_dock(_bot, _uid: int, _tid: int, *, window_id: str | None = None, **_kwargs):
        events.append(f"dock_sync:{window_id}")

    monkeypatch.setattr(bot, "_is_window_in_progress", _is_window_in_progress)
    monkeypatch.setattr(bot, "_codex_app_server_enabled", lambda: True)
    monkeypatch.setattr(bot, "_set_hourglass_reaction", _set_hourglass)
    monkeypatch.setattr(bot, "enqueue_queued_topic_input", _enqueue)
    monkeypatch.setattr(bot, "sync_queued_topic_dock", _sync_dock)
    monkeypatch.setattr(
        bot,
        "emit_telemetry",
        lambda event, **fields: telemetry.append((event, fields)),
    )

    await bot.queue_command(update, context)

    assert events == ["internal_queue", "hourglass", "dock_sync:@77"]
    assert telemetry == [
        (
            "queue.q_internal_enqueued",
            {
                "user_id": 1147817421,
                "thread_id": 777,
                "window_id": "@77",
                "queue_size": 1,
                "used_native_queue": False,
                "native_attempts": 0,
                "native_error": "",
                "text_len": len("next task"),
            },
        )
    ]


@pytest.mark.asyncio
async def test_q_does_not_attempt_native_queue_when_turn_is_active(monkeypatch):
    events: list[str] = []
    telemetry: list[tuple[str, dict[str, object]]] = []

    class _Chat:
        type = "supergroup"
        id = -100321

    class _Message:
        def __init__(self) -> None:
            self.text = "/q next task"
            self.chat = _Chat()
            self.chat_id = self.chat.id
            self.message_thread_id = 777
            self.message_id = 888

    message = _Message()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1147817421),
        effective_message=message,
        effective_chat=message.chat,
        message=message,
    )
    context = SimpleNamespace(bot=object(), user_data={})

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "runtime_mode", "hybrid")
    monkeypatch.setattr(
        bot.session_manager, "set_group_chat_id", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_window_for_thread",
        lambda _uid, _tid, **_kwargs: "@77",
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda _uid, _tid, **_kwargs: SimpleNamespace(
            codex_thread_id="thread-77",
            cwd="/tmp/project",
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "is_window_external_turn_active",
        lambda _wid: False,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_active_turn_id",
        lambda _wid: "turn-77",
    )

    async def _is_window_in_progress(*_args, **_kwargs):
        return True

    async def _set_hourglass(_message):
        events.append("hourglass")

    async def _unexpected_send_topic_text_to_window(**_kwargs):
        raise AssertionError("/q should not steer or send immediately while a turn is active")

    def _enqueue(
        _uid: int,
        _tid: int,
        _text: str,
        _chat_id: int,
        _msg_id: int,
        **_kwargs,
    ):
        events.append("internal_queue")
        return 1

    async def _sync_dock(_bot, _uid: int, _tid: int, *, window_id: str | None = None, **_kwargs):
        events.append(f"dock_sync:{window_id}")

    monkeypatch.setattr(bot, "_is_window_in_progress", _is_window_in_progress)
    monkeypatch.setattr(bot, "_codex_app_server_enabled", lambda: True)
    monkeypatch.setattr(bot, "_set_hourglass_reaction", _set_hourglass)
    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        _unexpected_send_topic_text_to_window,
    )
    monkeypatch.setattr(bot, "enqueue_queued_topic_input", _enqueue)
    monkeypatch.setattr(bot, "sync_queued_topic_dock", _sync_dock)
    monkeypatch.setattr(
        bot,
        "emit_telemetry",
        lambda event, **fields: telemetry.append((event, fields)),
    )

    await bot.queue_command(update, context)

    assert events == ["internal_queue", "hourglass", "dock_sync:@77"]
    assert telemetry == [
        (
            "queue.q_internal_enqueued",
            {
                "user_id": 1147817421,
                "thread_id": 777,
                "window_id": "@77",
                "queue_size": 1,
                "used_native_queue": False,
                "native_attempts": 0,
                "native_error": "",
                "text_len": len("next task"),
            },
        )
    ]


@pytest.mark.asyncio
async def test_q_immediate_send_forces_new_turn_semantics(monkeypatch):
    captured: dict[str, object] = {}

    class _Chat:
        type = "supergroup"
        id = -100321

        async def send_action(self, _action):
            return None

    class _Message:
        def __init__(self) -> None:
            self.text = "/q next task"
            self.chat = _Chat()
            self.chat_id = self.chat.id
            self.message_thread_id = 777
            self.message_id = 888

    message = _Message()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1147817421),
        effective_message=message,
        effective_chat=message.chat,
        message=message,
    )
    context = SimpleNamespace(bot=object(), user_data={})

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(
        bot.session_manager, "set_group_chat_id", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_window_for_thread",
        lambda _uid, _tid, **_kwargs: "@77",
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda _uid, _tid, **_kwargs: SimpleNamespace(
            codex_thread_id="thread-77",
            cwd="/tmp/project",
        ),
    )

    async def _is_window_in_progress(*_args, **_kwargs):
        return False

    async def _send_topic_text_to_window(
        *,
        user_id: int,
        thread_id: int | None,
        chat_id: int | None = None,
        window_id: str,
        text: str,
        steer: bool = False,
        force_new_turn: bool = False,
        topic_ownership=None,
    ):
        captured.update(
            {
                "user_id": user_id,
                "thread_id": thread_id,
                "chat_id": chat_id,
                "window_id": window_id,
                "text": text,
                "steer": steer,
                "force_new_turn": force_new_turn,
                "topic_ownership": topic_ownership,
            }
        )
        return True, ""

    monkeypatch.setattr(bot, "_is_window_in_progress", _is_window_in_progress)
    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        _send_topic_text_to_window,
    )
    monkeypatch.setattr(bot, "note_run_started", lambda **_kwargs: None)

    async def _noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bot, "sync_queued_topic_dock", _noop_async)
    monkeypatch.setattr(bot, "_set_eyes_reaction", _noop_async)
    monkeypatch.setattr(bot, "enqueue_status_update", _noop_async)
    monkeypatch.setattr(bot, "enqueue_progress_clear", _noop_async)
    monkeypatch.setattr(bot, "emit_telemetry", lambda *_args, **_kwargs: None)

    await bot.queue_command(update, context)

    assert captured["window_id"] == "@77"
    assert captured["text"] == "next task"
    assert captured["steer"] is False
    assert captured["force_new_turn"] is True


@pytest.mark.asyncio
async def test_q_immediate_active_writer_result_is_queued(monkeypatch):
    events: list[str] = []

    class _Chat:
        type = "supergroup"
        id = -100321

    message = SimpleNamespace(
        text="/q preserve this task",
        chat=_Chat(),
        chat_id=-100321,
        message_thread_id=777,
        message_id=889,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1147817421),
        effective_message=message,
        effective_chat=message.chat,
        message=message,
    )
    context = SimpleNamespace(bot=object(), user_data={})

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(
        bot.session_manager, "set_group_chat_id", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_window_for_thread",
        lambda _uid, _tid, **_kwargs: "@77",
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda _uid, _tid, **_kwargs: SimpleNamespace(
            codex_thread_id="thread-77",
            cwd="/tmp/project",
        ),
    )

    async def _is_window_in_progress(*_args, **_kwargs):
        return False

    async def _send_topic_text_to_window(**_kwargs):
        return False, "Latest Codex run already has an active writer."

    def _enqueue(
        _uid: int,
        _tid: int,
        text: str,
        _chat_id: int,
        _msg_id: int,
        **_kwargs,
    ):
        events.append(f"queue:{text}")
        return 1

    async def _hourglass(_message):
        events.append("hourglass")

    async def _dock(*_args, **_kwargs):
        events.append("dock")

    async def _safe_reply(*_args, **_kwargs):
        events.append("safe_reply")

    async def _dispatch_retry(*_args, **_kwargs):
        events.append("dispatch_retry")

    monkeypatch.setattr(bot, "_is_window_in_progress", _is_window_in_progress)
    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        _send_topic_text_to_window,
    )
    monkeypatch.setattr(bot, "enqueue_queued_topic_input", _enqueue)
    monkeypatch.setattr(bot, "_set_hourglass_reaction", _hourglass)
    monkeypatch.setattr(bot, "sync_queued_topic_dock", _dock)
    monkeypatch.setattr(bot, "safe_reply", _safe_reply)
    monkeypatch.setattr(bot, "_dispatch_next_queued_input", _dispatch_retry)
    monkeypatch.setattr(bot, "emit_telemetry", lambda *_args, **_kwargs: None)

    await bot.queue_command(update, context)

    assert events == [
        "queue:preserve this task",
        "hourglass",
        "dock",
        "dispatch_retry",
    ]


@pytest.mark.asyncio
async def test_dispatch_next_q_updates_dock_posts_marker_and_reacts(monkeypatch):
    mq.clear_queued_topic_inputs(1147817421, 777)
    _enqueue_test_topic_input(1147817421, 777, "first queued task", -100321, 111)
    _enqueue_test_topic_input(1147817421, 777, "second queued task", -100321, 222)

    events: list[str] = []

    class _FakeBot:
        async def set_message_reaction(self, *, chat_id: int, message_id: int, reaction):
            events.append(f"reaction:{chat_id}:{message_id}:{reaction}")

    monkeypatch.setattr(bot, "get_message_queue", lambda _uid: None)
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, **_kwargs: -100321,
    )

    async def _sync_dock(_bot, _uid: int, _tid: int, *, window_id: str | None = None, **_kwargs):
        events.append(f"dock_sync:{mq.queued_topic_input_count(1147817421, 777, -100321)}:{window_id}")

    async def _send_topic_text_to_window(
        *,
        user_id: int,
        thread_id: int | None,
        chat_id: int | None = None,
        window_id: str,
        text: str,
        steer: bool = False,
        force_new_turn: bool = False,
        dispatch_state=None,
    ):
        _ = user_id, thread_id, chat_id, steer, dispatch_state
        events.append(f"send_to_window:{window_id}:{text}:force_new_turn={force_new_turn}")
        return True, ""

    async def _safe_send(_bot, _chat_id, text, **_kwargs):
        events.append(f"safe_send:{text}")

    monkeypatch.setattr(bot, "sync_queued_topic_dock", _sync_dock)
    monkeypatch.setattr(
        bot.session_manager, "send_topic_text_to_window", _send_topic_text_to_window
    )
    monkeypatch.setattr(bot, "safe_send", _safe_send)
    monkeypatch.setattr(
        bot,
        "note_run_started",
        lambda **_kwargs: events.append("run_started"),
    )

    await bot._dispatch_next_queued_input(
        bot=_FakeBot(),
        user_id=1147817421,
        thread_id=777,
        window_id="@77",
        chat_id=-100321,
    )

    assert events[0] == "dock_sync:1:@77"
    assert events[1] == "send_to_window:@77:first queued task:force_new_turn=True"
    assert "run_started" in events
    assert not any(event.startswith("safe_send:") for event in events)
    assert any(ev.startswith("reaction:-100321:111") for ev in events)
    assert mq.queued_topic_input_count(1147817421, 777, -100321) == 1

    mq.clear_queued_topic_inputs(1147817421, 777)


@pytest.mark.asyncio
async def test_dispatch_next_q_requeues_when_send_fails(monkeypatch):
    mq.clear_queued_topic_inputs(1147817421, 888)
    _enqueue_test_topic_input(1147817421, 888, "first queued task", -100321, 333)

    sync_counts: list[int] = []
    sent_text: list[str] = []

    class _FakeBot:
        async def set_message_reaction(self, **_kwargs):
            raise AssertionError("reaction should not be set on send failure")

    monkeypatch.setattr(bot, "get_message_queue", lambda _uid: None)
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, **_kwargs: -100321,
    )

    async def _sync_dock(
        _bot, _uid: int, _tid: int, *, window_id: str | None = None, chat_id=None
    ):
        _ = window_id
        sync_counts.append(mq.queued_topic_input_count(1147817421, 888, -100321))

    async def _send_topic_text_to_window(
        *,
        user_id: int,
        thread_id: int | None,
        chat_id: int | None = None,
        window_id: str,
        text: str,
        steer: bool = False,
        force_new_turn: bool = False,
        dispatch_state=None,
    ):
        _ = (
            user_id,
            thread_id,
            chat_id,
            window_id,
            text,
            steer,
            force_new_turn,
            dispatch_state,
        )
        return False, "boom"

    async def _safe_send(_bot, _chat_id, text, **_kwargs):
        sent_text.append(text)

    monkeypatch.setattr(bot, "sync_queued_topic_dock", _sync_dock)
    monkeypatch.setattr(
        bot.session_manager, "send_topic_text_to_window", _send_topic_text_to_window
    )
    monkeypatch.setattr(bot, "safe_send", _safe_send)

    await bot._dispatch_next_queued_input(
        bot=_FakeBot(),
        user_id=1147817421,
        thread_id=888,
        window_id="@88",
        chat_id=-100321,
    )

    assert sync_counts == [0, 1]
    assert mq.queued_topic_input_count(1147817421, 888, -100321) == 1
    assert sent_text
    assert "Failed to send queued" in sent_text[0]

    mq.clear_queued_topic_inputs(1147817421, 888)


@pytest.mark.asyncio
async def test_dispatch_next_q_requeues_active_writer_exception(monkeypatch):
    user_id = 1147817421
    thread_id = 889
    chat_id = -100321
    mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)
    _enqueue_test_topic_input(
        user_id,
        thread_id,
        "preserve after exception",
        chat_id,
        334,
    )
    sent_text: list[str] = []

    class _FakeBot:
        async def set_message_reaction(self, **_kwargs):
            raise AssertionError("reaction should not be set on send failure")

    monkeypatch.setattr(bot, "get_message_queue", lambda _uid: None)
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, **_kwargs: chat_id,
    )

    async def _noop_sync(*_args, **_kwargs):
        return None

    async def _send_topic_text_to_window(**_kwargs):
        raise bot.CodexAppServerError(
            "thread thread-live already has an active writer"
        )

    async def _safe_send(_bot, _chat_id, text, **_kwargs):
        sent_text.append(text)

    monkeypatch.setattr(bot, "sync_queued_topic_dock", _noop_sync)
    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        _send_topic_text_to_window,
    )
    monkeypatch.setattr(bot, "safe_send", _safe_send)

    try:
        await bot._dispatch_next_queued_input(
            bot=_FakeBot(),
            user_id=user_id,
            thread_id=thread_id,
            window_id="@89",
            chat_id=chat_id,
            active_writer_retries_remaining=0,
        )

        assert mq.get_queued_topic_input_snapshot(user_id, thread_id, chat_id) == [
            ("preserve after exception", chat_id, 334)
        ]
        assert sent_text
        assert "active writer" in sent_text[0]
    finally:
        mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)


@pytest.mark.asyncio
async def test_dispatch_next_q_retries_active_writer_then_succeeds(monkeypatch):
    user_id = 1147817421
    thread_id = 891
    chat_id = -100321
    mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)
    _enqueue_test_topic_input(
        user_id,
        thread_id,
        "retry after writer closes",
        chat_id,
        336,
    )
    send_calls = 0
    sleep_calls: list[float] = []
    sent_text: list[str] = []

    class _FakeBot:
        async def set_message_reaction(self, **_kwargs):
            return None

    async def _not_in_progress(*_args, **_kwargs):
        return False

    async def _noop_sync(*_args, **_kwargs):
        return None

    async def _send_topic_text_to_window(**_kwargs):
        nonlocal send_calls
        send_calls += 1
        if send_calls == 1:
            return False, "thread already has an active writer"
        return True, "ok"

    async def _sleep(delay: float):
        sleep_calls.append(delay)

    async def _safe_send(_bot, _chat_id, text, **_kwargs):
        sent_text.append(text)

    monkeypatch.setattr(bot, "_is_window_in_progress", _not_in_progress)
    monkeypatch.setattr(bot, "get_message_queue", lambda _uid: None)
    monkeypatch.setattr(bot, "sync_queued_topic_dock", _noop_sync)
    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        _send_topic_text_to_window,
    )
    monkeypatch.setattr(bot.asyncio, "sleep", _sleep)
    monkeypatch.setattr(bot, "safe_send", _safe_send)
    monkeypatch.setattr(bot, "note_run_started", lambda **_kwargs: None)

    try:
        await bot._dispatch_next_queued_input(
            bot=_FakeBot(),
            user_id=user_id,
            thread_id=thread_id,
            window_id="@91",
            chat_id=chat_id,
        )

        assert send_calls == 2
        assert sleep_calls == [bot.QUEUE_ACTIVE_WRITER_RETRY_DELAY_SECONDS]
        assert mq.get_queued_topic_input_snapshot(user_id, thread_id, chat_id) == []
        assert sent_text == []
    finally:
        mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)


@pytest.mark.asyncio
async def test_dispatch_next_q_requeues_active_writer_conflict_after_dispatch_marker(
    monkeypatch,
):
    """A definite writer conflict must replay A before dispatching queued B."""
    user_id = 1147817421
    thread_id = 893
    chat_id = -100321
    mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)
    _enqueue_test_topic_input(user_id, thread_id, "A", chat_id, 338)
    _enqueue_test_topic_input(user_id, thread_id, "B", chat_id, 339)

    sent: list[str] = []
    retry_sleeps: list[float] = []
    conflict_returned = False

    class _FakeBot:
        async def set_message_reaction(self, **_kwargs):
            return None

    async def _not_in_progress(*_args, **_kwargs):
        return False

    async def _noop_sync(*_args, **_kwargs):
        return None

    async def _send_topic_text_to_window(**kwargs):
        nonlocal conflict_returned
        text = kwargs["text"]
        sent.append(text)
        if text == "A" and not conflict_returned:
            conflict_returned = True
            dispatch_state = kwargs["dispatch_state"]
            dispatch_state.transport_dispatch_started = True
            return False, "thread already has an active writer"
        return True, "ok"

    async def _sleep(delay: float):
        retry_sleeps.append(delay)

    monkeypatch.setattr(bot, "_is_window_in_progress", _not_in_progress)
    monkeypatch.setattr(bot, "get_message_queue", lambda _uid: None)
    monkeypatch.setattr(bot, "sync_queued_topic_dock", _noop_sync)
    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        _send_topic_text_to_window,
    )
    monkeypatch.setattr(bot.asyncio, "sleep", _sleep)
    monkeypatch.setattr(bot, "note_run_started", lambda **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, **_kwargs: chat_id,
    )

    try:
        await bot._dispatch_next_queued_input(
            bot=_FakeBot(),
            user_id=user_id,
            thread_id=thread_id,
            window_id="@93",
            chat_id=chat_id,
        )

        # The active-writer retry must own A again; B cannot overtake it.
        assert sent == ["A", "A"]
        assert retry_sleeps == [bot.QUEUE_ACTIVE_WRITER_RETRY_DELAY_SECONDS]
        assert mq.get_queued_topic_input_snapshot(user_id, thread_id, chat_id) == [
            ("B", chat_id, 339)
        ]

        await bot._dispatch_next_queued_input(
            bot=_FakeBot(),
            user_id=user_id,
            thread_id=thread_id,
            window_id="@93",
            chat_id=chat_id,
        )

        assert sent == ["A", "A", "B"]
        assert mq.get_queued_topic_input_snapshot(user_id, thread_id, chat_id) == []
    finally:
        mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)


@pytest.mark.asyncio
async def test_dispatch_next_q_does_not_requeue_uncertain_false_result(monkeypatch):
    user_id = 1147817421
    thread_id = 892
    chat_id = -100321
    mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)
    _enqueue_test_topic_input(
        user_id,
        thread_id,
        "possibly delivered remotely",
        chat_id,
        337,
    )
    sent_text: list[str] = []

    monkeypatch.setattr(bot, "get_message_queue", lambda _uid: None)

    async def _noop_sync(*_args, **_kwargs):
        return None

    async def _send_topic_text_to_window(**_kwargs):
        return (
            False,
            "Remote Codex returned a different thread; "
            "the request will not be replayed automatically.",
        )

    async def _safe_send(_bot, _chat_id, text, **_kwargs):
        sent_text.append(text)

    monkeypatch.setattr(bot, "sync_queued_topic_dock", _noop_sync)
    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        _send_topic_text_to_window,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, **_kwargs: chat_id,
    )
    monkeypatch.setattr(bot, "safe_send", _safe_send)

    try:
        await bot._dispatch_next_queued_input(
            bot=object(),
            user_id=user_id,
            thread_id=thread_id,
            window_id="@92",
            chat_id=chat_id,
        )

        assert mq.get_queued_topic_input_snapshot(user_id, thread_id, chat_id) == []
        assert sent_text
        assert "will not be replayed automatically" in sent_text[0]
    finally:
        mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)


@pytest.mark.asyncio
async def test_dispatch_next_q_does_not_replay_uncertain_exception(monkeypatch):
    user_id = 1147817421
    thread_id = 890
    chat_id = -100321
    mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)
    _enqueue_test_topic_input(
        user_id,
        thread_id,
        "do not replay uncertain send",
        chat_id,
        335,
    )

    monkeypatch.setattr(bot, "get_message_queue", lambda _uid: None)

    async def _noop_sync(*_args, **_kwargs):
        return None

    async def _send_topic_text_to_window(**kwargs):
        kwargs["dispatch_state"].transport_dispatch_started = True
        raise RuntimeError("remote delivery outcome is uncertain")

    monkeypatch.setattr(bot, "sync_queued_topic_dock", _noop_sync)
    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        _send_topic_text_to_window,
    )

    try:
        with pytest.raises(RuntimeError, match="delivery outcome is uncertain"):
            await bot._dispatch_next_queued_input(
                bot=object(),
                user_id=user_id,
                thread_id=thread_id,
                window_id="@90",
                chat_id=chat_id,
            )

        assert mq.get_queued_topic_input_snapshot(user_id, thread_id, chat_id) == []
    finally:
        mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)


@pytest.mark.asyncio
async def test_dispatch_next_q_defers_when_window_still_in_progress(monkeypatch):
    mq.clear_queued_topic_inputs(1147817421, 999)
    _enqueue_test_topic_input(1147817421, 999, "first queued task", -100321, 444)

    sync_counts: list[int] = []
    events: list[str] = []

    class _FakeBot:
        async def set_message_reaction(self, **_kwargs):
            raise AssertionError("reaction should not be set while dispatch is deferred")

    async def _is_window_in_progress(*_args, **_kwargs):
        return True

    async def _sync_dock(
        _bot, _uid: int, _tid: int, *, window_id: str | None = None, chat_id=None
    ):
        _ = window_id
        sync_counts.append(mq.queued_topic_input_count(1147817421, 999, -100321))

    async def _unexpected_send_topic_text_to_window(**_kwargs):
        raise AssertionError("queued item should not send while the turn is still active")

    monkeypatch.setattr(bot, "_is_window_in_progress", _is_window_in_progress)
    monkeypatch.setattr(bot, "sync_queued_topic_dock", _sync_dock)
    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        _unexpected_send_topic_text_to_window,
    )
    monkeypatch.setattr(
        bot,
        "emit_telemetry",
        lambda event, **fields: events.append(f"{event}:{fields.get('thread_id')}"),
    )

    await bot._dispatch_next_queued_input(
        bot=_FakeBot(),
        user_id=1147817421,
        thread_id=999,
        window_id="@99",
        chat_id=-100321,
    )

    assert sync_counts == [1]
    assert mq.queued_topic_input_count(1147817421, 999, -100321) == 1
    assert "queue.dispatch.deferred_active_turn:999" in events

    mq.clear_queued_topic_inputs(1147817421, 999)


@pytest.mark.asyncio
async def test_dispatch_next_q_concurrent_drains_are_single_flight_and_fifo(monkeypatch):
    user_id = 1147817421
    thread_id = 1000
    chat_id = -100321
    mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)
    _enqueue_test_topic_input(user_id, thread_id, "A", chat_id, 1)
    _enqueue_test_topic_input(user_id, thread_id, "B", chat_id, 2)

    sent: list[str] = []
    active_sends = 0
    max_active_sends = 0
    first_send_started = asyncio.Event()
    release_first_send = asyncio.Event()

    class _FakeBot:
        async def set_message_reaction(self, **_kwargs):
            return None

    async def _not_in_progress(*_args, **_kwargs):
        return False

    async def _noop_sync(*_args, **_kwargs):
        return None

    async def _send_topic_text_to_window(**kwargs):
        nonlocal active_sends, max_active_sends
        text = kwargs["text"]
        sent.append(text)
        active_sends += 1
        max_active_sends = max(max_active_sends, active_sends)
        try:
            if text == "A":
                first_send_started.set()
                await release_first_send.wait()
            return True, ""
        finally:
            active_sends -= 1

    monkeypatch.setattr(bot, "_is_window_in_progress", _not_in_progress)
    monkeypatch.setattr(bot, "get_message_queue", lambda _uid: None)
    monkeypatch.setattr(bot, "sync_queued_topic_dock", _noop_sync)
    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        _send_topic_text_to_window,
    )
    monkeypatch.setattr(bot, "note_run_started", lambda **_kwargs: None)

    first_drain = asyncio.create_task(
        bot._dispatch_next_queued_input(
            bot=_FakeBot(),
            user_id=user_id,
            thread_id=thread_id,
            window_id="@1000",
            chat_id=chat_id,
        )
    )
    second_drain: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(first_send_started.wait(), timeout=1)
        second_drain = asyncio.create_task(
            bot._dispatch_next_queued_input(
                bot=_FakeBot(),
                user_id=user_id,
                thread_id=thread_id,
                window_id="@1000",
                chat_id=chat_id,
            )
        )
        await asyncio.sleep(0)

        assert sent == ["A"]
        assert max_active_sends == 1
        assert mq.get_queued_topic_input_snapshot(user_id, thread_id, chat_id) == [
            ("B", chat_id, 2)
        ]

        release_first_send.set()
        await asyncio.gather(first_drain, second_drain)
        assert sent == ["A"]
        assert mq.get_queued_topic_input_snapshot(user_id, thread_id, chat_id) == [
            ("B", chat_id, 2)
        ]
    finally:
        release_first_send.set()
        drains = [first_drain]
        if second_drain is not None:
            drains.append(second_drain)
        await asyncio.gather(*drains, return_exceptions=True)
        mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)


@pytest.mark.asyncio
async def test_dispatch_next_q_completion_wakeup_drains_pending_fifo_after_reaction_tail(
    monkeypatch,
):
    user_id = 1147817421
    thread_id = 1002
    chat_id = -100321
    mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)
    _enqueue_test_topic_input(user_id, thread_id, "A", chat_id, 1)
    _enqueue_test_topic_input(user_id, thread_id, "B", chat_id, 2)

    sent: list[str] = []
    active_sends = 0
    max_active_sends = 0
    first_reaction_started = asyncio.Event()
    release_first_reaction = asyncio.Event()

    class _FakeBot:
        async def set_message_reaction(self, *, message_id: int, **_kwargs):
            if message_id == 1:
                first_reaction_started.set()
                await release_first_reaction.wait()

    async def _not_in_progress(*_args, **_kwargs):
        return False

    async def _noop_sync(*_args, **_kwargs):
        return None

    async def _send_topic_text_to_window(**kwargs):
        nonlocal active_sends, max_active_sends
        active_sends += 1
        max_active_sends = max(max_active_sends, active_sends)
        try:
            sent.append(kwargs["text"])
            return True, ""
        finally:
            active_sends -= 1

    monkeypatch.setattr(bot, "_is_window_in_progress", _not_in_progress)
    monkeypatch.setattr(bot, "get_message_queue", lambda _uid: None)
    monkeypatch.setattr(bot, "sync_queued_topic_dock", _noop_sync)
    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        _send_topic_text_to_window,
    )
    monkeypatch.setattr(bot, "note_run_started", lambda **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, **_kwargs: chat_id,
    )

    first_drain = asyncio.create_task(
        bot._dispatch_next_queued_input(
            bot=_FakeBot(),
            user_id=user_id,
            thread_id=thread_id,
            window_id="@1002",
            chat_id=chat_id,
        )
    )
    second_drain: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(first_reaction_started.wait(), timeout=1)

        # Model a transcript/completion callback arriving while the owner is
        # still in its reaction/dock tail. This wakeup must not be dropped.
        second_drain = asyncio.create_task(
            bot._dispatch_next_queued_input(
                bot=_FakeBot(),
                user_id=user_id,
                thread_id=thread_id,
                window_id="@1002",
                chat_id=chat_id,
                preserve_coalesced_wakeup=True,
            )
        )
        await asyncio.sleep(0)

        assert sent == ["A"]
        assert mq.get_queued_topic_input_snapshot(user_id, thread_id, chat_id) == [
            ("B", chat_id, 2)
        ]

        release_first_reaction.set()
        await asyncio.gather(first_drain, second_drain)

        assert sent == ["A", "B"]
        assert max_active_sends == 1
        assert mq.get_queued_topic_input_snapshot(user_id, thread_id, chat_id) == []
    finally:
        release_first_reaction.set()
        drains = [first_drain]
        if second_drain is not None:
            drains.append(second_drain)
        await asyncio.gather(*drains, return_exceptions=True)
        mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)


@pytest.mark.asyncio
async def test_dispatch_next_q_active_writer_requeue_coalesces_concurrent_retry(
    monkeypatch,
):
    user_id = 1147817421
    thread_id = 1001
    chat_id = -100321
    mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)
    _enqueue_test_topic_input(user_id, thread_id, "A", chat_id, 1)
    _enqueue_test_topic_input(user_id, thread_id, "B", chat_id, 2)

    sent: list[str] = []
    retry_sleeps: list[float] = []
    first_retry_waiting = asyncio.Event()
    release_retry = asyncio.Event()
    original_sleep = asyncio.sleep

    async def _send_topic_text_to_window(**kwargs):
        sent.append(kwargs["text"])
        return False, "thread already has an active writer"

    async def _sleep(delay: float):
        retry_sleeps.append(delay)
        if len(retry_sleeps) == 1:
            first_retry_waiting.set()
        await release_retry.wait()

    async def _not_in_progress(*_args, **_kwargs):
        return False

    async def _noop_sync(*_args, **_kwargs):
        return None

    async def _safe_send(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bot, "_is_window_in_progress", _not_in_progress)
    monkeypatch.setattr(bot, "get_message_queue", lambda _uid: None)
    monkeypatch.setattr(bot, "sync_queued_topic_dock", _noop_sync)
    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        _send_topic_text_to_window,
    )
    monkeypatch.setattr(bot.asyncio, "sleep", _sleep)
    monkeypatch.setattr(bot, "safe_send", _safe_send)
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, **_kwargs: chat_id,
    )

    first_drain = asyncio.create_task(
        bot._dispatch_next_queued_input(
            bot=object(),
            user_id=user_id,
            thread_id=thread_id,
            window_id="@1001",
            chat_id=chat_id,
            active_writer_retries_remaining=1,
        )
    )
    second_drain: asyncio.Task[None] | None = None
    try:
        await first_retry_waiting.wait()
        second_drain = asyncio.create_task(
            bot._dispatch_next_queued_input(
                bot=object(),
                user_id=user_id,
                thread_id=thread_id,
                window_id="@1001",
                chat_id=chat_id,
                active_writer_retries_remaining=1,
            )
        )
        await original_sleep(0)

        assert sent == ["A"]
        assert retry_sleeps == [bot.QUEUE_ACTIVE_WRITER_RETRY_DELAY_SECONDS]
        assert mq.get_queued_topic_input_snapshot(user_id, thread_id, chat_id) == [
            ("A", chat_id, 1),
            ("B", chat_id, 2),
        ]

        release_retry.set()
        await asyncio.gather(first_drain, second_drain)
        assert sent == ["A", "A"]
        assert retry_sleeps == [bot.QUEUE_ACTIVE_WRITER_RETRY_DELAY_SECONDS]
        assert mq.get_queued_topic_input_snapshot(user_id, thread_id, chat_id) == [
            ("A", chat_id, 1),
            ("B", chat_id, 2),
        ]
    finally:
        release_retry.set()
        drains = [first_drain]
        if second_drain is not None:
            drains.append(second_drain)
        await asyncio.gather(*drains, return_exceptions=True)
        mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)


@pytest.mark.asyncio
async def test_dispatch_next_q_uncertain_owner_exception_preserves_completion_wakeup(
    monkeypatch,
):
    """A completion wakeup must drain the next item after an uncertain A send."""
    user_id = 1147817421
    thread_id = 1003
    chat_id = -100321
    mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)
    _enqueue_test_topic_input(user_id, thread_id, "A", chat_id, 1)
    _enqueue_test_topic_input(user_id, thread_id, "B", chat_id, 2)

    sent: list[str] = []
    first_send_started = asyncio.Event()
    release_first_send = asyncio.Event()

    class _FakeBot:
        async def set_message_reaction(self, **_kwargs):
            return None

    async def _not_in_progress(*_args, **_kwargs):
        return False

    async def _noop_sync(*_args, **_kwargs):
        return None

    async def _send_topic_text_to_window(**kwargs):
        text = kwargs["text"]
        sent.append(text)
        if text == "A":
            first_send_started.set()
            await release_first_send.wait()
            kwargs["dispatch_state"].transport_dispatch_started = True
            raise RuntimeError("remote delivery outcome is uncertain")
        return True, ""

    monkeypatch.setattr(bot, "_is_window_in_progress", _not_in_progress)
    monkeypatch.setattr(bot, "get_message_queue", lambda _uid: None)
    monkeypatch.setattr(bot, "sync_queued_topic_dock", _noop_sync)
    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        _send_topic_text_to_window,
    )
    monkeypatch.setattr(bot, "note_run_started", lambda **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, **_kwargs: chat_id,
    )

    first_drain = asyncio.create_task(
        bot._dispatch_next_queued_input(
            bot=_FakeBot(),
            user_id=user_id,
            thread_id=thread_id,
            window_id="@1003",
            chat_id=chat_id,
        )
    )
    second_drain: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(first_send_started.wait(), timeout=1)
        second_drain = asyncio.create_task(
            bot._dispatch_next_queued_input(
                bot=_FakeBot(),
                user_id=user_id,
                thread_id=thread_id,
                window_id="@1003",
                chat_id=chat_id,
                preserve_coalesced_wakeup=True,
            )
        )
        await asyncio.sleep(0)

        assert sent == ["A"]
        assert mq.get_queued_topic_input_snapshot(user_id, thread_id, chat_id) == [
            ("B", chat_id, 2)
        ]

        release_first_send.set()
        results = await asyncio.gather(
            first_drain,
            second_drain,
            return_exceptions=True,
        )

        assert isinstance(results[0], RuntimeError)
        assert sent == ["A", "B"]
        assert mq.get_queued_topic_input_snapshot(user_id, thread_id, chat_id) == []
    finally:
        release_first_send.set()
        drains = [first_drain]
        if second_drain is not None:
            drains.append(second_drain)
        await asyncio.gather(*drains, return_exceptions=True)
        mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)


@pytest.mark.asyncio
async def test_q_queues_behind_owned_drain_after_writer_becomes_idle(monkeypatch):
    """A /q arriving during an owned retry cannot overtake its requeued A."""
    user_id = 1147817421
    thread_id = 1004
    chat_id = -100321
    mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)
    _enqueue_test_topic_input(user_id, thread_id, "A", chat_id, 1)

    sent: list[str] = []
    writer_active = False
    retry_sleep_started = asyncio.Event()
    release_retry = asyncio.Event()
    original_sleep = asyncio.sleep

    class _FakeBot:
        async def set_message_reaction(self, **_kwargs):
            return None

    class _Chat:
        type = "supergroup"
        id = chat_id

    message = SimpleNamespace(
        text="/q B",
        chat=_Chat(),
        chat_id=chat_id,
        message_thread_id=thread_id,
        message_id=2,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_message=message,
        effective_chat=message.chat,
        message=message,
    )
    context = SimpleNamespace(bot=_FakeBot(), user_data={})

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    async def _is_window_in_progress(*_args, **_kwargs):
        return writer_active

    async def _send_topic_text_to_window(**kwargs):
        nonlocal writer_active
        text = kwargs["text"]
        sent.append(text)
        if text == "A" and sent.count("A") == 1:
            writer_active = True
            return False, "thread already has an active writer"
        writer_active = False
        return True, ""

    async def _sleep(delay: float):
        nonlocal writer_active
        if delay == bot.QUEUE_ACTIVE_WRITER_RETRY_DELAY_SECONDS:
            writer_active = False
            retry_sleep_started.set()
            await release_retry.wait()
            return
        await original_sleep(delay)

    async def _noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bot, "_is_window_in_progress", _is_window_in_progress)
    monkeypatch.setattr(bot, "get_message_queue", lambda _uid: None)
    monkeypatch.setattr(bot, "sync_queued_topic_dock", _noop_async)
    monkeypatch.setattr(bot, "_set_hourglass_reaction", _noop_async)
    monkeypatch.setattr(bot, "_set_eyes_reaction", _noop_async)
    monkeypatch.setattr(bot, "note_run_started", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "emit_telemetry", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bot, "safe_reply", _noop_async)
    monkeypatch.setattr(bot, "safe_send", _noop_async)
    monkeypatch.setattr(bot.asyncio, "sleep", _sleep)
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, **_kwargs: chat_id,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        _send_topic_text_to_window,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_window_for_thread",
        lambda _uid, _tid, **_kwargs: "@1004",
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda _uid, _tid, **_kwargs: SimpleNamespace(
            codex_thread_id="thread-1004",
            cwd="/tmp/project",
        ),
    )

    first_drain = asyncio.create_task(
        bot._dispatch_next_queued_input(
            bot=_FakeBot(),
            user_id=user_id,
            thread_id=thread_id,
            window_id="@1004",
            chat_id=chat_id,
            active_writer_retries_remaining=1,
        )
    )
    try:
        await asyncio.wait_for(retry_sleep_started.wait(), timeout=1)
        await bot.queue_command(update, context)

        assert sent == ["A"]
        assert mq.get_queued_topic_input_snapshot(user_id, thread_id, chat_id) == [
            ("A", chat_id, 1),
            ("B", chat_id, 2),
        ]

        release_retry.set()
        await first_drain
        assert sent == ["A", "A", "B"]
        assert mq.get_queued_topic_input_snapshot(user_id, thread_id, chat_id) == []
    finally:
        release_retry.set()
        await asyncio.gather(first_drain, return_exceptions=True)
        mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)


@pytest.mark.asyncio
async def test_forward_topic_text_queues_behind_owned_drain_after_writer_becomes_idle(
    monkeypatch,
):
    """Normal text arriving during an owned retry cannot overtake its requeued A."""
    user_id = 1147817421
    thread_id = 1005
    chat_id = -100321
    mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)
    _enqueue_test_topic_input(user_id, thread_id, "A", chat_id, 1)

    sent: list[str] = []
    writer_active = False
    retry_sleep_started = asyncio.Event()
    release_retry = asyncio.Event()
    original_sleep = asyncio.sleep

    class _FakeBot:
        async def set_message_reaction(self, **_kwargs):
            return None

    message = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        chat_id=chat_id,
        message_id=3,
    )
    context = SimpleNamespace(bot=_FakeBot(), user_data={})

    async def _is_window_in_progress(*_args, **_kwargs):
        return writer_active

    async def _send_topic_text_to_window(**kwargs):
        nonlocal writer_active
        text = kwargs["text"]
        sent.append(text)
        if text == "A" and sent.count("A") == 1:
            writer_active = True
            return False, "thread already has an active writer"
        writer_active = False
        return True, ""

    async def _sleep(delay: float):
        nonlocal writer_active
        if delay == bot.QUEUE_ACTIVE_WRITER_RETRY_DELAY_SECONDS:
            writer_active = False
            retry_sleep_started.set()
            await release_retry.wait()
            return
        await original_sleep(delay)

    async def _noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bot, "_is_window_in_progress", _is_window_in_progress)
    monkeypatch.setattr(bot, "get_message_queue", lambda _uid: None)
    monkeypatch.setattr(bot, "sync_queued_topic_dock", _noop_async)
    monkeypatch.setattr(bot, "_start_ingress_ack", lambda _message: [])
    monkeypatch.setattr(bot, "enqueue_status_update", _noop_async)
    monkeypatch.setattr(bot, "enqueue_progress_clear", _noop_async)
    monkeypatch.setattr(bot, "enqueue_progress_start", _noop_async)
    monkeypatch.setattr(bot, "_set_hourglass_reaction", _noop_async)
    monkeypatch.setattr(bot, "_set_eyes_reaction", _noop_async)
    monkeypatch.setattr(bot, "note_run_started", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "emit_telemetry", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bot, "safe_reply", _noop_async)
    monkeypatch.setattr(bot.asyncio, "sleep", _sleep)
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, **_kwargs: chat_id,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        _send_topic_text_to_window,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_for_thread",
        lambda _uid, _tid, **_kwargs: "@1005",
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda _uid, _tid, **_kwargs: SimpleNamespace(
            codex_thread_id="thread-1005",
            cwd="/tmp/project",
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_topic_response_mode",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "is_coco_control_topic",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_mention_only",
        lambda _wid: False,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "is_window_external_turn_active",
        lambda _wid: False,
    )

    first_drain = asyncio.create_task(
        bot._dispatch_next_queued_input(
            bot=_FakeBot(),
            user_id=user_id,
            thread_id=thread_id,
            window_id="@1005",
            chat_id=chat_id,
            active_writer_retries_remaining=1,
        )
    )
    try:
        await asyncio.wait_for(retry_sleep_started.wait(), timeout=1)
        await bot._forward_topic_text_message(
            message=message,
            context=context,
            user_id=user_id,
            thread_id=thread_id,
            chat_id=chat_id,
            text="C",
        )

        assert sent == ["A"]
        assert mq.get_queued_topic_input_snapshot(user_id, thread_id, chat_id) == [
            ("A", chat_id, 1),
            ("C", chat_id, 3),
        ]

        release_retry.set()
        await first_drain
        assert sent == ["A", "A", "C"]
        assert mq.get_queued_topic_input_snapshot(user_id, thread_id, chat_id) == []
    finally:
        release_retry.set()
        await asyncio.gather(first_drain, return_exceptions=True)
        mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)


@pytest.mark.asyncio
async def test_dispatch_next_q_restores_fifo_once_when_cancelled_before_send(
    monkeypatch,
):
    """Cancellation in the pre-send dock barrier must not lose the popped item."""
    user_id = 1147817421
    thread_id = 1100
    chat_id = -100321
    mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)
    _enqueue_test_topic_input(user_id, thread_id, "A", chat_id, 1)
    _enqueue_test_topic_input(user_id, thread_id, "B", chat_id, 2)

    sync_started = asyncio.Event()
    release_sync = asyncio.Event()
    prepend_calls: list[tuple[str, int, int]] = []
    sent: list[str] = []

    async def _sync_dock(*_args, **_kwargs):
        # The queue item must already be owned/popped before this await.
        assert mq.get_queued_topic_input_snapshot(user_id, thread_id, chat_id) == [
            ("B", chat_id, 2)
        ]
        sync_started.set()
        await release_sync.wait()

    async def _send_topic_text_to_window(**kwargs):
        sent.append(kwargs["text"])
        raise AssertionError("send must not begin after pre-dispatch cancellation")

    original_prepend = bot.prepend_queued_topic_input

    def _prepend(*args, **kwargs):
        prepend_calls.append((args[2], args[3], args[4]))
        return original_prepend(*args, **kwargs)

    async def _not_in_progress(*_args, **_kwargs):
        return False

    monkeypatch.setattr(bot, "_is_window_in_progress", _not_in_progress)
    monkeypatch.setattr(bot, "get_message_queue", lambda _uid: None)
    monkeypatch.setattr(bot, "sync_queued_topic_dock", _sync_dock)
    monkeypatch.setattr(bot.session_manager, "send_topic_text_to_window", _send_topic_text_to_window)
    monkeypatch.setattr(bot, "prepend_queued_topic_input", _prepend)

    drain = asyncio.create_task(
        bot._dispatch_next_queued_input(
            bot=object(),
            user_id=user_id,
            thread_id=thread_id,
            window_id="@1100",
            chat_id=chat_id,
        )
    )
    try:
        await asyncio.wait_for(sync_started.wait(), timeout=1)
        drain.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain

        release_sync.set()
        assert sent == []
        assert prepend_calls == [("A", chat_id, 1)]
        assert mq.get_queued_topic_input_snapshot(user_id, thread_id, chat_id) == [
            ("A", chat_id, 1),
            ("B", chat_id, 2),
        ]
    finally:
        release_sync.set()
        await asyncio.gather(drain, return_exceptions=True)
        mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)


@pytest.mark.asyncio
async def test_dispatch_next_q_restores_once_when_cancelled_waiting_on_session_send_lock(
    monkeypatch,
):
    """Cancellation while waiting for the real send lock is still pre-write."""
    user_id = 1147817421
    thread_id = 1104
    chat_id = -100321
    window_id = "@1104"
    mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)
    _enqueue_test_topic_input(user_id, thread_id, "A", chat_id, 1)
    _enqueue_test_topic_input(user_id, thread_id, "B", chat_id, 2)

    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    manager = SessionManager()
    monkeypatch.setattr(
        manager,
        "_local_machine_identity",
        lambda: ("local-test-machine", "Local test machine"),
    )
    manager.set_group_chat_id(user_id, thread_id, chat_id)
    state = manager.get_window_state(window_id)
    state.cwd = "/tmp/project"
    state.window_name = "lock-window"
    state.codex_thread_id = "codex-thread-1104"
    manager.bind_topic_to_codex_thread(
        user_id=user_id,
        thread_id=thread_id,
        chat_id=chat_id,
        codex_thread_id=state.codex_thread_id,
        cwd=state.cwd,
        window_id=window_id,
    )

    class _TrackingLock(asyncio.Lock):
        def __init__(self) -> None:
            super().__init__()
            self.waiting = asyncio.Event()

        async def acquire(self) -> bool:
            if self.locked():
                self.waiting.set()
            return await super().acquire()

    lock = _TrackingLock()
    await lock.acquire()
    manager._window_send_locks[window_id] = lock

    app_server_calls = 0
    dispatch_states: list[bot.TopicSendDispatchState] = []
    prepend_calls: list[tuple[str, int, int]] = []

    async def _unexpected_app_server_send(**_kwargs):
        nonlocal app_server_calls
        app_server_calls += 1
        raise AssertionError("turn transport must not begin while the lock is held")

    async def _not_in_progress(*_args, **_kwargs):
        return False

    async def _noop_async(*_args, **_kwargs):
        return None

    state_type = bot.TopicSendDispatchState

    def _capture_dispatch_state():
        dispatch_state = state_type()
        dispatch_states.append(dispatch_state)
        return dispatch_state

    original_prepend = bot.prepend_queued_topic_input

    def _prepend(*args, **kwargs):
        prepend_calls.append((args[2], args[3], args[4]))
        return original_prepend(*args, **kwargs)

    monkeypatch.setattr(bot, "session_manager", manager)
    monkeypatch.setattr(manager, "_codex_app_server_mode_enabled", lambda: True)
    monkeypatch.setattr(
        manager,
        "_send_inputs_via_codex_app_server",
        _unexpected_app_server_send,
    )
    monkeypatch.setattr(bot, "TopicSendDispatchState", _capture_dispatch_state)
    monkeypatch.setattr(bot, "prepend_queued_topic_input", _prepend)
    monkeypatch.setattr(
        bot,
        "is_topic_ownership_current",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(bot, "_is_window_in_progress", _not_in_progress)
    monkeypatch.setattr(bot, "get_message_queue", lambda _uid: None)
    monkeypatch.setattr(bot, "sync_queued_topic_dock", _noop_async)

    drain_key = bot._queued_topic_drain_key(
        user_id,
        thread_id,
        chat_id,
    )
    bot._queued_topic_drains.discard(drain_key)
    bot._queued_topic_drain_wakeups.pop(drain_key, None)
    drain = asyncio.create_task(
        bot._dispatch_next_queued_input(
            bot=object(),
            user_id=user_id,
            thread_id=thread_id,
            window_id=window_id,
            chat_id=chat_id,
        )
    )
    try:
        await asyncio.wait_for(lock.waiting.wait(), timeout=1)
        drain.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain

        assert app_server_calls == 0
        assert len(dispatch_states) == 1
        assert prepend_calls == [("A", chat_id, 1)]
        assert mq.get_queued_topic_input_snapshot(user_id, thread_id, chat_id) == [
            ("A", chat_id, 1),
            ("B", chat_id, 2),
        ]
        assert dispatch_states[0].transport_dispatch_started is False
    finally:
        if lock.locked():
            lock.release()
        await asyncio.gather(drain, return_exceptions=True)
        bot._queued_topic_drains.discard(drain_key)
        bot._queued_topic_drain_wakeups.pop(drain_key, None)
        mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)


@pytest.mark.asyncio
async def test_dispatch_next_q_restores_fifo_when_cancelled_before_app_server_write(
    monkeypatch,
):
    """Cancellation while the app-server write lock is held is pre-write."""
    user_id = 1147817421
    thread_id = 1105
    chat_id = -100321
    window_id = "@1105"
    mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)

    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    manager = SessionManager()
    manager.set_group_chat_id(user_id, thread_id, chat_id)
    state = manager.get_window_state(window_id)
    state.cwd = "/tmp/project"
    state.window_name = "write-window"
    state.codex_thread_id = "codex-thread-1105"
    manager.bind_topic_to_codex_thread(
        user_id=user_id,
        thread_id=thread_id,
        chat_id=chat_id,
        codex_thread_id=state.codex_thread_id,
        cwd=state.cwd,
        window_id=window_id,
    )
    owner = mq.TopicOwnership(
        window_id=window_id,
        codex_thread_id=state.codex_thread_id,
        machine_id=manager.get_window_machine_id(window_id),
        cwd=state.cwd,
    )
    mq.enqueue_queued_topic_input(
        user_id,
        thread_id,
        "A",
        chat_id,
        1,
        topic_ownership=owner,
    )
    mq.enqueue_queued_topic_input(
        user_id,
        thread_id,
        "B",
        chat_id,
        2,
        topic_ownership=owner,
    )

    class _TrackingLock(asyncio.Lock):
        def __init__(self) -> None:
            super().__init__()
            self.waiting = asyncio.Event()

        async def acquire(self) -> bool:
            if self.locked():
                self.waiting.set()
            return await super().acquire()

    class _FakeStdin:
        def __init__(self) -> None:
            self.writes: list[bytes] = []

        def write(self, data: bytes) -> None:
            self.writes.append(data)

        async def drain(self) -> None:
            return None

    stdin = _FakeStdin()
    client = cas.CodexAppServerClient()
    client._proc = SimpleNamespace(returncode=None, stdin=stdin)
    client._initialized = True
    client._transport_generation = 1
    write_lock = _TrackingLock()
    await write_lock.acquire()
    client._write_lock = write_lock

    dispatch_states: list[bot.TopicSendDispatchState] = []
    prepend_calls: list[tuple[str, int, int]] = []
    state_type = bot.TopicSendDispatchState

    def _capture_dispatch_state():
        dispatch_state = state_type()
        dispatch_states.append(dispatch_state)
        return dispatch_state

    original_prepend = bot.prepend_queued_topic_input

    def _prepend(*args, **kwargs):
        prepend_calls.append((args[2], args[3], args[4]))
        return original_prepend(*args, **kwargs)

    async def _not_in_progress(*_args, **_kwargs):
        return False

    async def _noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bot, "session_manager", manager)
    monkeypatch.setattr(session_module, "codex_app_server_client", client)
    monkeypatch.setattr(manager, "_codex_app_server_mode_enabled", lambda: True)
    monkeypatch.setattr(bot, "TopicSendDispatchState", _capture_dispatch_state)
    monkeypatch.setattr(bot, "prepend_queued_topic_input", _prepend)
    monkeypatch.setattr(bot, "is_topic_ownership_current", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(bot, "_is_window_in_progress", _not_in_progress)
    monkeypatch.setattr(bot, "get_message_queue", lambda _uid: None)
    monkeypatch.setattr(bot, "sync_queued_topic_dock", _noop_async)

    drain_key = bot._queued_topic_drain_key(user_id, thread_id, chat_id)
    bot._queued_topic_drains.discard(drain_key)
    bot._queued_topic_drain_wakeups.pop(drain_key, None)
    drain = asyncio.create_task(
        bot._dispatch_next_queued_input(
            bot=object(),
            user_id=user_id,
            thread_id=thread_id,
            window_id=window_id,
            chat_id=chat_id,
        )
    )
    try:
        await asyncio.wait_for(write_lock.waiting.wait(), timeout=1)
        drain.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain

        assert stdin.writes == []
        assert client._pending == {}
        assert client._in_flight_mutation_requests == {}
        assert len(dispatch_states) == 1
        assert dispatch_states[0].transport_dispatch_started is False
        assert prepend_calls == [("A", chat_id, 1)]
        assert mq.get_queued_topic_input_snapshot(user_id, thread_id, chat_id) == [
            ("A", chat_id, 1),
            ("B", chat_id, 2),
        ]
    finally:
        if write_lock.locked():
            write_lock.release()
        await asyncio.gather(drain, return_exceptions=True)
        bot._queued_topic_drains.discard(drain_key)
        bot._queued_topic_drain_wakeups.pop(drain_key, None)
        mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)


@pytest.mark.asyncio
async def test_dispatch_next_q_restores_fifo_once_when_queue_barrier_fails(
    monkeypatch,
):
    """A queue.join exception before send starts must re-prepend exactly once."""
    user_id = 1147817421
    thread_id = 1101
    chat_id = -100321
    mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)
    _enqueue_test_topic_input(user_id, thread_id, "A", chat_id, 1)
    _enqueue_test_topic_input(user_id, thread_id, "B", chat_id, 2)

    prepend_calls: list[tuple[str, int, int]] = []
    sent: list[str] = []

    class _Queue:
        async def join(self):
            assert mq.get_queued_topic_input_snapshot(user_id, thread_id, chat_id) == [
                ("B", chat_id, 2)
            ]
            raise RuntimeError("pre-send queue barrier failed")

    async def _send_topic_text_to_window(**kwargs):
        sent.append(kwargs["text"])
        raise AssertionError("send must not begin after queue barrier failure")

    async def _noop_sync(*_args, **_kwargs):
        return None

    original_prepend = bot.prepend_queued_topic_input

    def _prepend(*args, **kwargs):
        prepend_calls.append((args[2], args[3], args[4]))
        return original_prepend(*args, **kwargs)

    async def _not_in_progress(*_args, **_kwargs):
        return False

    monkeypatch.setattr(bot, "_is_window_in_progress", _not_in_progress)
    monkeypatch.setattr(bot, "get_message_queue", lambda _uid: _Queue())
    monkeypatch.setattr(bot, "sync_queued_topic_dock", _noop_sync)
    monkeypatch.setattr(bot.session_manager, "send_topic_text_to_window", _send_topic_text_to_window)
    monkeypatch.setattr(bot, "prepend_queued_topic_input", _prepend)

    try:
        with pytest.raises(RuntimeError, match="pre-send queue barrier failed"):
            await bot._dispatch_next_queued_input(
                bot=object(),
                user_id=user_id,
                thread_id=thread_id,
                window_id="@1101",
                chat_id=chat_id,
            )

        assert sent == []
        assert prepend_calls == [("A", chat_id, 1)]
        assert mq.get_queued_topic_input_snapshot(user_id, thread_id, chat_id) == [
            ("A", chat_id, 1),
            ("B", chat_id, 2),
        ]
    finally:
        mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)


@pytest.mark.asyncio
async def test_queued_topic_input_is_not_dispatched_after_explicit_rebind(
    monkeypatch,
):
    """Normal queueing captures A's owner and cannot silently execute on B."""
    user_id = 1147817421
    thread_id = 1102
    chat_id = -100321
    mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)
    owner = {
        "chat_id": chat_id,
        "window_id": "@owner-a",
        "codex_thread_id": "codex-thread-a",
        "machine_id": "machine-a",
        "cwd": "/workspace/a",
    }
    warnings: list[str] = []
    sent: list[str] = []

    def _binding(*_args, **_kwargs):
        return SimpleNamespace(**owner)

    async def _not_in_progress(*_args, **_kwargs):
        return False

    async def _sync_dock(*_args, **_kwargs):
        return None

    async def _send_topic_text_to_window(**kwargs):
        sent.append(kwargs["text"])
        return True, ""

    async def _safe_send(_bot, _chat_id, text, **_kwargs):
        warnings.append(text)

    message = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type="supergroup"),
        chat_id=chat_id,
        message_id=1,
    )
    context = SimpleNamespace(bot=object(), user_data={})

    monkeypatch.setattr(
        bot.session_manager,
        "get_window_for_thread",
        lambda _uid, _tid, **_kwargs: owner["window_id"],
    )
    monkeypatch.setattr(bot.session_manager, "resolve_topic_binding", _binding)
    monkeypatch.setattr(bot.session_manager, "_get_persisted_topic_binding", _binding)
    monkeypatch.setattr(bot, "capture_topic_ownership", mq.capture_topic_ownership)
    monkeypatch.setattr(bot.session_manager, "set_topic_response_mode", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bot.session_manager, "is_coco_control_topic", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(bot.session_manager, "get_window_mention_only", lambda _wid: False)
    monkeypatch.setattr(bot.session_manager, "is_window_external_turn_active", lambda _wid: True)
    monkeypatch.setattr(bot, "_set_hourglass_reaction", _sync_dock)
    monkeypatch.setattr(bot, "sync_queued_topic_dock", _sync_dock)
    monkeypatch.setattr(bot, "get_message_queue", lambda _uid: None)
    monkeypatch.setattr(bot, "_is_window_in_progress", _not_in_progress)
    monkeypatch.setattr(bot.session_manager, "send_topic_text_to_window", _send_topic_text_to_window)
    monkeypatch.setattr(bot, "safe_send", _safe_send)

    try:
        await bot._forward_topic_text_message(
            message=message,
            context=context,
            user_id=user_id,
            thread_id=thread_id,
            chat_id=chat_id,
            text="captured under owner A",
        )
        assert mq.queued_topic_input_count(user_id, thread_id, chat_id) == 1

        # Model an explicit /resume rebind before the queued drain wakes up.
        owner.update(
            window_id="@owner-b",
            codex_thread_id="codex-thread-b",
            machine_id="machine-b",
            cwd="/workspace/b",
        )
        await bot._dispatch_next_queued_input(
            bot=object(),
            user_id=user_id,
            thread_id=thread_id,
            window_id="@owner-b",
            chat_id=chat_id,
        )

        assert sent == []
        assert mq.queued_topic_input_count(user_id, thread_id, chat_id) == 0
        assert warnings
        warning = warnings[-1].lower()
        assert "not sent" in warning
        assert "binding" in warning or "rebind" in warning
    finally:
        mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)


@pytest.mark.asyncio
async def test_queued_topic_drain_drops_stale_owner_and_checks_next_item(
    monkeypatch,
):
    """A stale A item is dropped while a later B item is independently checked."""
    user_id = 1147817421
    thread_id = 1103
    chat_id = -100321
    mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)
    owner = {
        "chat_id": chat_id,
        "window_id": "@owner-a",
        "codex_thread_id": "codex-thread-a",
        "machine_id": "machine-a",
        "cwd": "/workspace/a",
    }
    warnings: list[str] = []
    sent: list[str] = []

    def _binding(*_args, **_kwargs):
        return SimpleNamespace(**owner)

    async def _not_in_progress(*_args, **_kwargs):
        return False

    async def _sync_dock(*_args, **_kwargs):
        return None

    async def _send_topic_text_to_window(**kwargs):
        sent.append(kwargs["text"])
        return True, ""

    async def _safe_send(_bot, _chat_id, text, **_kwargs):
        warnings.append(text)

    monkeypatch.setattr(bot.session_manager, "resolve_topic_binding", _binding)
    monkeypatch.setattr(bot.session_manager, "_get_persisted_topic_binding", _binding)
    monkeypatch.setattr(bot.session_manager, "get_window_for_thread", lambda _uid, _tid, **_kwargs: owner["window_id"])
    monkeypatch.setattr(bot.session_manager, "is_window_external_turn_active", lambda _wid: False)
    monkeypatch.setattr(bot, "get_message_queue", lambda _uid: None)
    monkeypatch.setattr(bot, "_is_window_in_progress", _not_in_progress)
    monkeypatch.setattr(bot, "sync_queued_topic_dock", _sync_dock)
    monkeypatch.setattr(bot.session_manager, "send_topic_text_to_window", _send_topic_text_to_window)
    monkeypatch.setattr(bot, "safe_send", _safe_send)

    try:
        _enqueue_test_topic_input(
            user_id,
            thread_id,
            "stale A",
            chat_id,
            1,
            capture_current=True,
        )
        owner.update(
            window_id="@owner-b",
            codex_thread_id="codex-thread-b",
            machine_id="machine-b",
            cwd="/workspace/b",
        )
        _enqueue_test_topic_input(
            user_id,
            thread_id,
            "current B",
            chat_id,
            2,
            capture_current=True,
        )

        await bot._dispatch_next_queued_input(
            bot=object(),
            user_id=user_id,
            thread_id=thread_id,
            window_id="@owner-b",
            chat_id=chat_id,
        )

        assert sent == ["current B"]
        assert warnings
        warning = warnings[-1].lower()
        assert "not sent" in warning
        assert "binding" in warning or "rebind" in warning
        assert mq.queued_topic_input_count(user_id, thread_id, chat_id) == 0
    finally:
        mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)


@pytest.mark.asyncio
async def test_dispatch_next_q_rechecks_owner_after_dock_and_queue_barriers(
    monkeypatch,
):
    """A queued A request must not cross a rebind while pre-send waits run."""
    user_id = 1147817421
    thread_id = 1200
    chat_id = -100321
    owner_a = mq.TopicOwnership(
        window_id="@1200-a",
        codex_thread_id="codex-1200-a",
        machine_id="machine-a",
        cwd="/workspace/a",
    )
    owner_b = mq.TopicOwnership(
        window_id="@1200-b",
        codex_thread_id="codex-1200-b",
        machine_id="machine-b",
        cwd="/workspace/b",
    )
    current_owner = {"value": owner_a}
    mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)
    _enqueue_test_topic_input(
        user_id,
        thread_id,
        "old A request",
        chat_id,
        1,
        topic_ownership=owner_a,
    )

    sync_started = asyncio.Event()
    release_sync = asyncio.Event()
    queue_join_started = asyncio.Event()
    release_queue_join = asyncio.Event()
    sync_calls = 0
    sent: list[str] = []
    warnings: list[str] = []

    class _Queue:
        async def join(self):
            queue_join_started.set()
            await release_queue_join.wait()

    async def _not_in_progress(*_args, **_kwargs):
        return False

    async def _sync_dock(*_args, **_kwargs):
        nonlocal sync_calls
        sync_calls += 1
        if sync_calls == 1:
            sync_started.set()
            await release_sync.wait()

    async def _send_topic_text_to_window(**kwargs):
        sent.append(kwargs["text"])
        return True, ""

    async def _safe_send(_bot, _chat_id, text, **_kwargs):
        warnings.append(text)

    def _binding_matches(
        _user_id,
        _thread_id,
        *,
        chat_id,
        window_id,
        codex_thread_id,
        machine_id,
        cwd,
    ):
        expected = current_owner["value"]
        return (
            chat_id == expected_chat_id
            and window_id == expected.window_id
            and codex_thread_id == expected.codex_thread_id
            and machine_id == expected.machine_id
            and cwd == expected.cwd
        )

    monkeypatch.setattr(bot, "_is_window_in_progress", _not_in_progress)
    monkeypatch.setattr(bot, "get_message_queue", lambda _uid: _Queue())
    monkeypatch.setattr(bot, "sync_queued_topic_dock", _sync_dock)
    expected_chat_id = chat_id
    monkeypatch.setattr(
        bot.session_manager,
        "_topic_binding_ownership_matches",
        _binding_matches,
    )
    monkeypatch.setattr(bot, "safe_send", _safe_send)
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, **_kwargs: chat_id,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        _send_topic_text_to_window,
    )
    monkeypatch.setattr(bot, "note_run_started", lambda **_kwargs: None)

    drain = asyncio.create_task(
        bot._dispatch_next_queued_input(
            bot=object(),
            user_id=user_id,
            thread_id=thread_id,
            window_id=owner_a.window_id,
            chat_id=chat_id,
        )
    )
    try:
        await asyncio.wait_for(sync_started.wait(), timeout=1)
        release_sync.set()
        await asyncio.wait_for(queue_join_started.wait(), timeout=1)

        # /resume rebinds the topic while the drain is still at its final
        # pre-dispatch barrier. The popped A item must not be sent to B.
        current_owner["value"] = owner_b
        release_queue_join.set()
        await asyncio.wait_for(drain, timeout=1)

        assert sent == []
        assert warnings
        assert "not sent" in warnings[-1].lower()
        assert mq.get_queued_topic_input_snapshot(user_id, thread_id, chat_id) == []
    finally:
        release_sync.set()
        release_queue_join.set()
        await asyncio.gather(drain, return_exceptions=True)
        mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)


@pytest.mark.asyncio
async def test_ingress_during_empty_owned_drain_preserves_wakeup(monkeypatch):
    """An ingress item arriving during an empty drain must not be stranded."""
    user_id = 1147817421
    thread_id = 1201
    chat_id = -100321
    owner = mq.TopicOwnership(
        window_id="@1201",
        codex_thread_id="codex-1201",
        machine_id="machine-a",
        cwd="/workspace/a",
    )
    mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)
    sent: list[str] = []
    sync_started = asyncio.Event()
    release_sync = asyncio.Event()
    sync_calls = 0

    async def _not_in_progress(*_args, **_kwargs):
        return False

    async def _sync_dock(*_args, **_kwargs):
        nonlocal sync_calls
        sync_calls += 1
        if sync_calls == 1:
            sync_started.set()
            await release_sync.wait()

    async def _send_topic_text_to_window(**kwargs):
        sent.append(kwargs["text"])
        return True, ""

    async def _noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bot, "_is_window_in_progress", _not_in_progress)
    monkeypatch.setattr(bot, "get_message_queue", lambda _uid: None)
    monkeypatch.setattr(bot, "sync_queued_topic_dock", _sync_dock)
    monkeypatch.setattr(bot, "_set_hourglass_reaction", _noop_async)
    monkeypatch.setattr(bot, "is_topic_ownership_current", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(bot, "note_run_started", lambda **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, **_kwargs: chat_id,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_for_thread",
        lambda _uid, _tid, **_kwargs: owner.window_id,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda _uid, _tid, **_kwargs: SimpleNamespace(
            codex_thread_id=owner.codex_thread_id,
            cwd=owner.cwd,
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_topic_response_mode",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "is_coco_control_topic",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_mention_only",
        lambda _wid: False,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "is_window_external_turn_active",
        lambda _wid: False,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        _send_topic_text_to_window,
    )
    monkeypatch.setattr(mq, "capture_topic_ownership", lambda *_args, **_kwargs: owner)

    owner_drain = asyncio.create_task(
        bot._dispatch_next_queued_input(
            bot=object(),
            user_id=user_id,
            thread_id=thread_id,
            window_id=owner.window_id,
            chat_id=chat_id,
        )
    )
    try:
        await asyncio.wait_for(sync_started.wait(), timeout=1)
        message = SimpleNamespace(
            chat=SimpleNamespace(id=chat_id),
            chat_id=chat_id,
            message_id=2,
        )
        context = SimpleNamespace(bot=object(), user_data={})

        # The owning drain has popped an empty queue and is waiting on dock
        # sync. Normal ingress must enqueue B and preserve a completion wakeup.
        await bot._forward_topic_text_message(
            message=message,
            context=context,
            user_id=user_id,
            thread_id=thread_id,
            chat_id=chat_id,
            text="new ingress B",
        )
        assert mq.get_queued_topic_input_snapshot(user_id, thread_id, chat_id) == [
            ("new ingress B", chat_id, 2)
        ]

        release_sync.set()
        await asyncio.wait_for(owner_drain, timeout=1)
        assert sent == ["new ingress B"]
        assert mq.get_queued_topic_input_snapshot(user_id, thread_id, chat_id) == []
    finally:
        release_sync.set()
        await asyncio.gather(owner_drain, return_exceptions=True)
        mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)
        bot._queued_topic_drain_wakeups.clear()
        bot._queued_topic_drains.clear()


@pytest.mark.asyncio
async def test_dispatch_next_q_restores_pre_dispatch_exception_once_with_owner_fifo(
    monkeypatch,
):
    """A definite pre-send resume failure restores the popped item exactly once."""
    user_id = 1147817421
    thread_id = 1202
    chat_id = -100321
    owner = mq.TopicOwnership(
        window_id="@1202",
        codex_thread_id="codex-1202",
        machine_id="machine-a",
        cwd="/workspace/a",
    )
    mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)
    _enqueue_test_topic_input(
        user_id,
        thread_id,
        "A before resume failure",
        chat_id,
        1,
        topic_ownership=owner,
    )
    _enqueue_test_topic_input(
        user_id,
        thread_id,
        "B after A",
        chat_id,
        2,
        topic_ownership=owner,
    )
    sent: list[str] = []

    async def _not_in_progress(*_args, **_kwargs):
        return False

    async def _noop_sync(*_args, **_kwargs):
        return None

    async def _send_topic_text_to_window(**kwargs):
        sent.append(kwargs["text"])
        # This models the exact host-follow resume failure before any turn
        # input is accepted by app-server.
        raise bot.CodexAppServerError("thread not found: codex-1202")

    async def _safe_send(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bot, "_is_window_in_progress", _not_in_progress)
    monkeypatch.setattr(bot, "get_message_queue", lambda _uid: None)
    monkeypatch.setattr(bot, "sync_queued_topic_dock", _noop_sync)
    monkeypatch.setattr(bot, "is_topic_ownership_current", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(bot, "safe_send", _safe_send)
    monkeypatch.setattr(bot, "note_run_started", lambda **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, **_kwargs: chat_id,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        _send_topic_text_to_window,
    )

    try:
        try:
            await bot._dispatch_next_queued_input(
                bot=object(),
                user_id=user_id,
                thread_id=thread_id,
                window_id=owner.window_id,
                chat_id=chat_id,
            )
        except bot.CodexAppServerError as exc:
            assert "thread not found" in str(exc)

        bucket = mq._queued_topic_inputs[mq._topic_key(user_id, thread_id, chat_id)]
        assert [item.text for item in bucket] == [
            "A before resume failure",
            "B after A",
        ]
        assert [item.topic_ownership for item in bucket] == [owner, owner]
        assert sent == ["A before resume failure"]
    finally:
        mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)


@pytest.mark.asyncio
async def test_dispatch_next_q_post_write_disconnect_is_not_requeued_or_replayed(
    monkeypatch,
):
    """A post-write app-server disconnect must not replay the queued request."""
    user_id = 1147817421
    thread_id = 1203
    chat_id = -100321
    owner = mq.TopicOwnership(
        window_id="@1203",
        codex_thread_id="codex-1203",
        machine_id="machine-a",
        cwd="/workspace/a",
    )
    mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)
    _enqueue_test_topic_input(
        user_id,
        thread_id,
        "possibly delivered request",
        chat_id,
        1,
        topic_ownership=owner,
    )
    send_calls: list[str] = []
    failures: list[str] = []

    async def _not_in_progress(*_args, **_kwargs):
        return False

    async def _noop_sync(*_args, **_kwargs):
        return None

    async def _send_topic_text_to_window(**kwargs):
        send_calls.append(kwargs["text"])
        # The transport reports only a generic disconnect after the write. The
        # queue layer must classify this as post-dispatch uncertainty rather
        # than relying on an explicit "uncertain" marker in the message.
        dispatch_state = kwargs.get("dispatch_state")
        if dispatch_state is not None:
            dispatch_state.transport_dispatch_started = True
        return False, "App-server disconnected"

    async def _safe_send(_bot, _chat_id, text, **_kwargs):
        failures.append(text)

    monkeypatch.setattr(bot, "_is_window_in_progress", _not_in_progress)
    monkeypatch.setattr(bot, "get_message_queue", lambda _uid: None)
    monkeypatch.setattr(bot, "sync_queued_topic_dock", _noop_sync)
    monkeypatch.setattr(bot, "is_topic_ownership_current", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(bot, "safe_send", _safe_send)
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, **_kwargs: chat_id,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        _send_topic_text_to_window,
    )

    try:
        await bot._dispatch_next_queued_input(
            bot=object(),
            user_id=user_id,
            thread_id=thread_id,
            window_id="@1203",
            chat_id=chat_id,
        )

        assert send_calls == ["possibly delivered request"]
        assert mq.get_queued_topic_input_snapshot(user_id, thread_id, chat_id) == []
        assert failures
        assert "App-server disconnected" in failures[0]
    finally:
        mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)


@pytest.mark.asyncio
async def test_q_capture_gap_drops_ownerless_input_before_rebound_owner_can_send(
    monkeypatch,
):
    """A queue item captured during an A->unbound->B gap must not target B."""
    user_id = 1147817421
    thread_id = 1104
    chat_id = -100321
    mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)

    owner_a = SimpleNamespace(
        chat_id=chat_id,
        window_id="@owner-a",
        codex_thread_id="codex-thread-a",
        machine_id="machine-a",
        cwd="/workspace/a",
    )
    owner_b = SimpleNamespace(
        chat_id=chat_id,
        window_id="@owner-b",
        codex_thread_id="codex-thread-b",
        machine_id="machine-b",
        cwd="/workspace/b",
    )
    raw_binding = {"value": owner_a}
    active_turn = {"value": True}
    sent: list[str] = []
    warnings: list[str] = []

    def _raw_binding(*_args, **_kwargs):
        return raw_binding["value"]

    async def _is_window_in_progress(*_args, **_kwargs):
        return active_turn["value"]

    async def _send_topic_text_to_window(**kwargs):
        sent.append(kwargs["text"])
        return True, ""

    async def _safe_send(_bot, _chat_id, text, **_kwargs):
        warnings.append(text)

    async def _noop_async(*_args, **_kwargs):
        return None

    original_enqueue = mq.enqueue_queued_topic_input

    def _enqueue_during_binding_gap(*args, **kwargs):
        # The handler already resolved A. The persisted binding disappears
        # before capture, then B is rebound before the drain wakes up.
        raw_binding["value"] = None
        result = original_enqueue(*args, **kwargs)
        assert mq.get_queued_topic_input_snapshot(user_id, thread_id, chat_id) == [
            ("old input from A", chat_id, 1)
        ]
        raw_binding["value"] = owner_b
        return result

    message = SimpleNamespace(
        text="/q old input from A",
        chat=SimpleNamespace(id=chat_id, type="supergroup"),
        chat_id=chat_id,
        message_thread_id=thread_id,
        message_id=1,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_message=message,
        effective_chat=message.chat,
        message=message,
    )
    context = SimpleNamespace(bot=object(), user_data={})

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(
        bot.session_manager, "set_group_chat_id", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_window_for_thread",
        lambda _uid, _tid, **_kwargs: owner_a.window_id,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda _uid, _tid, **_kwargs: owner_a,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "_get_persisted_topic_binding",
        _raw_binding,
    )
    monkeypatch.setattr(
        bot,
        "capture_topic_ownership",
        lambda *_args, **_kwargs: mq.TopicOwnership(
            window_id=owner_a.window_id,
            codex_thread_id=owner_a.codex_thread_id,
            machine_id=owner_a.machine_id,
            cwd=owner_a.cwd,
        ),
    )
    monkeypatch.setattr(bot, "_is_window_in_progress", _is_window_in_progress)
    monkeypatch.setattr(bot, "enqueue_queued_topic_input", _enqueue_during_binding_gap)
    monkeypatch.setattr(bot, "sync_queued_topic_dock", _noop_async)
    monkeypatch.setattr(bot, "_set_hourglass_reaction", _noop_async)
    monkeypatch.setattr(bot, "get_message_queue", lambda _uid: None)
    monkeypatch.setattr(bot, "safe_send", _safe_send)
    monkeypatch.setattr(bot, "note_run_started", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "emit_telemetry", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_chat_id",
        lambda *_args, **_kwargs: chat_id,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        _send_topic_text_to_window,
    )

    try:
        await bot.queue_command(update, context)
        assert mq.queued_topic_input_count(user_id, thread_id, chat_id) == 1

        active_turn["value"] = False
        await bot._dispatch_next_queued_input(
            bot=object(),
            user_id=user_id,
            thread_id=thread_id,
            window_id=owner_b.window_id,
            chat_id=chat_id,
        )

        assert sent == []
        assert mq.queued_topic_input_count(user_id, thread_id, chat_id) == 0
        assert warnings
        warning = warnings[-1].lower()
        assert "not sent" in warning
        assert "binding" in warning or "owner" in warning
    finally:
        mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)


@pytest.mark.asyncio
async def test_ownerless_non_topic_task_remains_deliverable(monkeypatch):
    """Missing topic ownership is valid for ordinary non-topic delivery."""
    user_id = 7718
    queue: asyncio.Queue[mq.MessageTask] = asyncio.Queue()
    mq._message_queues[user_id] = queue
    mq._queue_locks[user_id] = asyncio.Lock()
    delivered: list[mq.MessageTask] = []

    async def _process(_bot, _user_id: int, task: mq.MessageTask):
        delivered.append(task)

    monkeypatch.setattr(mq, "_process_content_task", _process)
    task = mq.MessageTask(task_type="content", parts=["ordinary message"])
    mq._put_queued_task(user_id, queue, task)
    worker = asyncio.create_task(mq._message_queue_worker(object(), user_id))

    try:
        await asyncio.wait_for(queue.join(), timeout=0.5)
        assert delivered == [task]
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        mq._message_queues.pop(user_id, None)
        mq._queue_locks.pop(user_id, None)
        mq._queued_delivery_topic_counts.pop(user_id, None)
        mq._topic_delivery_generations.pop((user_id, 0, 0), None)


@pytest.mark.asyncio
async def test_dispatch_next_q_restores_fifo_once_when_remote_rpc_cancelled_before_write(
    monkeypatch,
):
    """Remote connect cancellation is still before the RPC writer boundary."""
    import coco.agent_rpc as agent_rpc_module
    import coco.cluster_rpc as cluster_rpc_module

    user_id = 1147817421
    thread_id = 1204
    chat_id = -100321
    window_id = _TEST_TOPIC_OWNERSHIP.window_id
    mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)
    _enqueue_test_topic_input(user_id, thread_id, "A", chat_id, 1)
    _enqueue_test_topic_input(user_id, thread_id, "B", chat_id, 2)

    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    manager = SessionManager()
    monkeypatch.setattr(
        manager,
        "_local_machine_identity",
        lambda: ("local-test-machine", "Local test machine"),
    )
    manager.set_group_chat_id(user_id, thread_id, chat_id)
    state = manager.get_window_state(window_id)
    state.cwd = _TEST_TOPIC_OWNERSHIP.cwd
    state.window_name = "remote-window"
    state.codex_thread_id = _TEST_TOPIC_OWNERSHIP.codex_thread_id
    manager.bind_topic_to_codex_thread(
        user_id=user_id,
        thread_id=thread_id,
        chat_id=chat_id,
        codex_thread_id=state.codex_thread_id,
        cwd=state.cwd,
        window_id=window_id,
        machine_id=_TEST_TOPIC_OWNERSHIP.machine_id,
    )

    async def _no_goal_context(*_args, **_kwargs):
        return ""

    monkeypatch.setattr(manager, "_build_coco_operator_context", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(manager, "_build_live_goal_context", _no_goal_context)
    monkeypatch.setattr(manager, "resolve_thread_skills", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(manager, "resolve_thread_codex_skills", lambda *_args, **_kwargs: [])

    remote_client = agent_rpc_module.AgentRpcClient(shared_secret="rpc-secret")
    monkeypatch.setattr(
        remote_client,
        "_resolve_endpoint",
        lambda _machine_id: ("remote.test", 12345),
    )

    connect_started = asyncio.Event()
    release_connect = asyncio.Event()
    writer_writes: list[bytes] = []

    class _Reader:
        async def readline(self):
            return b""

    class _Writer:
        def write(self, data: bytes) -> None:
            writer_writes.append(data)

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    async def _open_connection(_host, _port, **_kwargs):
        connect_started.set()
        await release_connect.wait()
        return _Reader(), _Writer()

    monkeypatch.setattr(
        cluster_rpc_module.asyncio,
        "open_connection",
        _open_connection,
    )
    monkeypatch.setattr(agent_rpc_module, "agent_rpc_client", remote_client)
    monkeypatch.setattr(session_module.node_registry, "get_node", lambda _machine_id: None)

    dispatch_states: list[bot.TopicSendDispatchState] = []
    state_type = bot.TopicSendDispatchState

    def _capture_dispatch_state():
        dispatch_state = state_type()
        dispatch_states.append(dispatch_state)
        return dispatch_state

    prepend_calls: list[tuple[str, int, int]] = []
    original_prepend = bot.prepend_queued_topic_input

    def _prepend(*args, **kwargs):
        prepend_calls.append((args[2], args[3], args[4]))
        return original_prepend(*args, **kwargs)

    async def _not_in_progress(*_args, **_kwargs):
        return False

    async def _noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bot, "session_manager", manager)
    monkeypatch.setattr(bot, "TopicSendDispatchState", _capture_dispatch_state)
    monkeypatch.setattr(bot, "prepend_queued_topic_input", _prepend)
    monkeypatch.setattr(bot, "is_topic_ownership_current", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(bot, "_is_window_in_progress", _not_in_progress)
    monkeypatch.setattr(bot, "get_message_queue", lambda _uid: None)
    monkeypatch.setattr(bot, "sync_queued_topic_dock", _noop_async)

    drain_key = bot._queued_topic_drain_key(user_id, thread_id, chat_id)
    bot._queued_topic_drains.discard(drain_key)
    bot._queued_topic_drain_wakeups.pop(drain_key, None)
    drain = asyncio.create_task(
        bot._dispatch_next_queued_input(
            bot=object(),
            user_id=user_id,
            thread_id=thread_id,
            window_id=window_id,
            chat_id=chat_id,
        )
    )
    try:
        for _ in range(100):
            if connect_started.is_set() or drain.done():
                break
            await asyncio.sleep(0.01)
        assert connect_started.is_set(), (
            "remote drain ended before the RPC connect boundary: "
            f"{drain.exception() if drain.done() else 'still running'}"
        )
        drain.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain

        assert writer_writes == []
        assert len(dispatch_states) == 1
        assert dispatch_states[0].transport_dispatch_started is False
        assert prepend_calls == [("A", chat_id, 1)]
        assert mq.get_queued_topic_input_snapshot(user_id, thread_id, chat_id) == [
            ("A", chat_id, 1),
            ("B", chat_id, 2),
        ]
    finally:
        release_connect.set()
        await asyncio.gather(drain, return_exceptions=True)
        bot._queued_topic_drains.discard(drain_key)
        bot._queued_topic_drain_wakeups.pop(drain_key, None)
        mq.clear_queued_topic_inputs(user_id, thread_id, chat_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "helper_name",
    (
        "status_update",
        "status_clear",
        "progress_update",
        "progress_start",
        "progress_clear",
        "progress_finalize",
    ),
)
async def test_topic_scoped_status_progress_tasks_drop_after_explicit_rebind(
    monkeypatch,
    helper_name,
):
    """Queued A status/progress work must not write after an A->B rebind."""
    user_id = 7720
    thread_id = 7721
    chat_id = -1007721
    owner_a = {
        "window_id": "@progress-a",
        "codex_thread_id": "codex-progress-a",
        "cwd": "/workspace/progress-a",
        "machine_id": "machine-a",
    }
    owner_b = {
        "window_id": "@progress-b",
        "codex_thread_id": "codex-progress-b",
        "cwd": "/workspace/progress-b",
        "machine_id": "machine-b",
    }

    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    manager = SessionManager()
    manager.bind_topic_to_codex_thread(
        user_id=user_id,
        thread_id=thread_id,
        chat_id=chat_id,
        window_id=owner_a["window_id"],
        codex_thread_id=owner_a["codex_thread_id"],
        cwd=owner_a["cwd"],
        machine_id=owner_a["machine_id"],
        display_name="owner A",
    )
    monkeypatch.setattr(mq, "session_manager", manager)

    queue: asyncio.Queue[mq.MessageTask] = asyncio.Queue()
    mq._message_queues[user_id] = queue
    mq._queue_locks[user_id] = asyncio.Lock()
    skey = mq._topic_key(user_id, thread_id, chat_id)
    writes: list[str] = []

    class _Bot:
        async def edit_message_text(self, **_kwargs):
            writes.append("edit")

        async def delete_message(self, **_kwargs):
            writes.append("delete")

        async def send_chat_action(self, **_kwargs):
            writes.append("typing")

    async def _send_with_fallback(*_args, **_kwargs):
        writes.append("send")
        return SimpleNamespace(message_id=8800)

    monkeypatch.setattr(mq, "send_with_fallback", _send_with_fallback)
    bot_obj = _Bot()
    owner_a_snapshot = mq.capture_topic_ownership(user_id, thread_id, chat_id)
    assert owner_a_snapshot is not None

    if helper_name == "status_update":
        mq._status_msg_info[skey] = (8701, owner_a["window_id"], "old status")
        await mq.enqueue_status_update(
            bot_obj,
            user_id,
            owner_a["window_id"],
            "new status",
            thread_id,
            chat_id,
        )
    elif helper_name == "status_clear":
        mq._status_msg_info[skey] = (8702, owner_a["window_id"], "old status")
        await mq.enqueue_status_update(
            bot_obj,
            user_id,
            owner_a["window_id"],
            None,
            thread_id,
            chat_id,
        )
    elif helper_name == "progress_update":
        mq._progress_msg_info[skey] = (8703, owner_a["window_id"], "old progress")
        await mq.enqueue_progress_update(
            bot_obj,
            user_id,
            owner_a["window_id"],
            " + next",
            thread_id,
            chat_id,
        )
    elif helper_name == "progress_start":
        await mq.enqueue_progress_start(
            bot_obj,
            user_id,
            owner_a["window_id"],
            thread_id,
            chat_id,
        )
    elif helper_name == "progress_clear":
        mq._progress_msg_info[skey] = (8704, owner_a["window_id"], "old progress")
        await mq.enqueue_progress_clear(bot_obj, user_id, thread_id, chat_id)
    else:
        mq._progress_msg_info[skey] = (8705, owner_a["window_id"], "old progress")
        await mq.enqueue_progress_finalize(
            bot_obj,
            user_id,
            owner_a["window_id"],
            thread_id,
            chat_id=chat_id,
        )

    assert queue.qsize() == 1

    # This is the explicit lifecycle rebind that must fence work captured for A.
    manager.bind_topic_to_codex_thread(
        user_id=user_id,
        thread_id=thread_id,
        chat_id=chat_id,
        window_id=owner_b["window_id"],
        codex_thread_id=owner_b["codex_thread_id"],
        cwd=owner_b["cwd"],
        machine_id=owner_b["machine_id"],
        display_name="owner B",
    )

    worker = asyncio.create_task(mq._message_queue_worker(bot_obj, user_id))
    try:
        await asyncio.wait_for(queue.join(), timeout=1)
        assert writes == []
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        mq._message_queues.pop(user_id, None)
        mq._queue_locks.pop(user_id, None)
        mq._queue_workers.pop(user_id, None)
        mq._queued_delivery_topic_counts.pop(user_id, None)
        mq._active_delivery_topics.pop(user_id, None)
        mq._topic_delivery_generations.pop(skey, None)
        mq._status_msg_info.pop(skey, None)
        mq._progress_msg_info.pop(skey, None)
        mq._progress_text_cache.pop(skey, None)


@pytest.mark.asyncio
async def test_threadless_progress_task_remains_deliverable(monkeypatch):
    """A helper task without a topic owner still reaches Telegram."""
    user_id = 7722
    queue: asyncio.Queue[mq.MessageTask] = asyncio.Queue()
    mq._message_queues[user_id] = queue
    mq._queue_locks[user_id] = asyncio.Lock()
    writes: list[str] = []

    async def _send_with_fallback(*_args, **_kwargs):
        writes.append("send")
        return SimpleNamespace(message_id=8801)

    monkeypatch.setattr(mq, "send_with_fallback", _send_with_fallback)
    await mq.enqueue_progress_start(
        object(),  # type: ignore[arg-type]
        user_id,
        "@threadless",
    )
    worker = asyncio.create_task(mq._message_queue_worker(object(), user_id))

    try:
        await asyncio.wait_for(queue.join(), timeout=1)
        assert writes == ["send"]
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        mq._message_queues.pop(user_id, None)
        mq._queue_locks.pop(user_id, None)
        mq._queue_workers.pop(user_id, None)
        mq._queued_delivery_topic_counts.pop(user_id, None)
        mq._active_delivery_topics.pop(user_id, None)
        mq._topic_delivery_generations.pop((user_id, 0, 0), None)
        mq._progress_msg_info.pop((user_id, 0, 0), None)
        mq._progress_text_cache.pop((user_id, 0, 0), None)


@pytest.mark.asyncio
async def test_progress_update_captures_owner_before_coalescing_lock_wait(monkeypatch):
    """Progress ownership must be A even if coalescing waits through an A->B rebind."""
    user_id = 7723
    thread_id = 7724
    chat_id = -1007724
    owner_a = {
        "window_id": "@progress-lock-a",
        "codex_thread_id": "codex-progress-lock-a",
        "cwd": "/workspace/progress-lock-a",
        "machine_id": "machine-a",
    }
    owner_b = {
        "window_id": "@progress-lock-b",
        "codex_thread_id": "codex-progress-lock-b",
        "cwd": "/workspace/progress-lock-b",
        "machine_id": "machine-b",
    }

    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    manager = SessionManager()
    manager.bind_topic_to_codex_thread(
        user_id=user_id,
        thread_id=thread_id,
        chat_id=chat_id,
        window_id=owner_a["window_id"],
        codex_thread_id=owner_a["codex_thread_id"],
        cwd=owner_a["cwd"],
        machine_id=owner_a["machine_id"],
        display_name="owner A",
    )
    monkeypatch.setattr(mq, "session_manager", manager)

    queue: asyncio.Queue[mq.MessageTask] = asyncio.Queue()

    class _TrackingLock(asyncio.Lock):
        def __init__(self):
            super().__init__()
            self.waiting = asyncio.Event()

        async def acquire(self):
            if self.locked():
                self.waiting.set()
            return await super().acquire()

    lock = _TrackingLock()
    mq._message_queues[user_id] = queue
    mq._queue_locks[user_id] = lock
    skey = mq._topic_key(user_id, thread_id, chat_id)
    mq._progress_msg_info[skey] = (8706, owner_a["window_id"], "old progress")
    writes: list[str] = []

    class _Bot:
        async def edit_message_text(self, **_kwargs):
            writes.append("edit")

        async def delete_message(self, **_kwargs):
            writes.append("delete")

    async def _send_with_fallback(*_args, **_kwargs):
        writes.append("send")
        return SimpleNamespace(message_id=8802)

    monkeypatch.setattr(mq, "send_with_fallback", _send_with_fallback)
    bot_obj = _Bot()
    enqueue_task: asyncio.Task[None] | None = None
    worker: asyncio.Task[None] | None = None

    await lock.acquire()
    try:
        enqueue_task = asyncio.create_task(
            mq.enqueue_progress_update(
                bot_obj,
                user_id,
                owner_a["window_id"],
                " + stale A progress",
                thread_id,
                chat_id,
            )
        )
        await asyncio.wait_for(lock.waiting.wait(), timeout=1)
        assert not enqueue_task.done()

        # The enqueue coroutine is suspended at the coalescing lock. Rebinding
        # now must not make its A output capture B after the await resumes.
        manager.bind_topic_to_codex_thread(
            user_id=user_id,
            thread_id=thread_id,
            chat_id=chat_id,
            window_id=owner_b["window_id"],
            codex_thread_id=owner_b["codex_thread_id"],
            cwd=owner_b["cwd"],
            machine_id=owner_b["machine_id"],
            display_name="owner B",
        )
        lock.release()
        await asyncio.wait_for(enqueue_task, timeout=1)
        assert queue.qsize() == 1

        worker = asyncio.create_task(mq._message_queue_worker(bot_obj, user_id))
        await asyncio.wait_for(queue.join(), timeout=1)
        assert writes == []
    finally:
        if lock.locked():
            lock.release()
        if enqueue_task is not None:
            await asyncio.gather(enqueue_task, return_exceptions=True)
        if worker is not None:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        while not queue.empty():
            queue.get_nowait()
            queue.task_done()
        mq._message_queues.pop(user_id, None)
        mq._queue_locks.pop(user_id, None)
        mq._queue_workers.pop(user_id, None)
        mq._queued_delivery_topic_counts.pop(user_id, None)
        mq._active_delivery_topics.pop(user_id, None)
        mq._topic_delivery_generations.pop(skey, None)
        mq._progress_msg_info.pop(skey, None)
        mq._progress_text_cache.pop(skey, None)


@pytest.mark.asyncio
async def test_forward_topic_text_drops_prompt_when_owner_rebinds_before_send(
    monkeypatch,
):
    """An immediate text prompt captured for A must not dispatch to rebound B."""
    user_id = 1147817421
    thread_id = 1301
    chat_id = -100321
    owner_a = mq.TopicOwnership(
        window_id="@owner-a-1301",
        codex_thread_id="codex-thread-a-1301",
        machine_id="machine-a",
        cwd="/workspace/a",
    )
    owner_b = mq.TopicOwnership(
        window_id="@owner-b-1301",
        codex_thread_id="codex-thread-b-1301",
        machine_id="machine-b",
        cwd="/workspace/b",
    )
    current_owner = {"value": owner_a}
    capture_started = asyncio.Event()
    progress_check_started = asyncio.Event()
    release_progress_check = asyncio.Event()
    dispatched: list[tuple[str, str]] = []

    message = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        chat_id=chat_id,
        message_id=130101,
    )
    context = SimpleNamespace(bot=object(), user_data={})

    def _resolve_binding(_uid, _tid, **_kwargs):
        owner = current_owner["value"]
        return SimpleNamespace(
            window_id=owner.window_id,
            codex_thread_id=owner.codex_thread_id,
            machine_id=owner.machine_id,
            cwd=owner.cwd,
        )

    def _capture_ownership(*_args, **_kwargs):
        assert current_owner["value"] is owner_a
        capture_started.set()
        return owner_a

    async def _is_window_in_progress(*_args, **_kwargs):
        progress_check_started.set()
        await release_progress_check.wait()
        return False

    async def _send_topic_text_to_window(
        *, window_id, text, topic_ownership=None, **_kwargs
    ):
        # This fake models the vulnerable SessionManager behavior: without the
        # ingress snapshot, it resolves the rebound canonical owner (B).
        if topic_ownership is None:
            dispatched.append((current_owner["value"].window_id, text))
            return True, ""
        if topic_ownership != current_owner["value"]:
            return False, "stale topic owner; request was not sent"
        dispatched.append((window_id, text))
        return True, ""

    async def _noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        bot.session_manager,
        "get_window_for_thread",
        lambda *_args, **_kwargs: owner_a.window_id,
    )
    monkeypatch.setattr(bot.session_manager, "resolve_topic_binding", _resolve_binding)
    monkeypatch.setattr(bot, "capture_topic_ownership", _capture_ownership)
    monkeypatch.setattr(
        bot.session_manager, "set_topic_response_mode", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        bot.session_manager, "is_coco_control_topic", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        bot.session_manager, "get_window_mention_only", lambda _wid: False
    )
    monkeypatch.setattr(
        bot.session_manager, "is_window_external_turn_active", lambda _wid: False
    )
    monkeypatch.setattr(bot, "_is_window_in_progress", _is_window_in_progress)
    monkeypatch.setattr(bot, "_start_ingress_ack", lambda _message: [])
    monkeypatch.setattr(bot, "enqueue_status_update", _noop_async)
    monkeypatch.setattr(bot, "enqueue_progress_clear", _noop_async)
    monkeypatch.setattr(bot, "enqueue_progress_start", _noop_async)
    monkeypatch.setattr(bot, "_set_eyes_reaction", _noop_async)
    monkeypatch.setattr(bot, "note_run_started", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "safe_reply", _noop_async)
    monkeypatch.setattr(bot, "emit_telemetry", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        _send_topic_text_to_window,
    )

    send_task = asyncio.create_task(
        bot._forward_topic_text_message(
            message=message,
            context=context,
            user_id=user_id,
            thread_id=thread_id,
            chat_id=chat_id,
            text="prompt captured for owner A",
        )
    )
    await asyncio.wait_for(
        asyncio.gather(capture_started.wait(), progress_check_started.wait()),
        timeout=1,
    )
    current_owner["value"] = owner_b
    release_progress_check.set()
    await asyncio.wait_for(send_task, timeout=1)

    assert not any(window_id == owner_b.window_id for window_id, _text in dispatched)


@pytest.mark.asyncio
async def test_q_drops_prompt_when_owner_rebinds_before_immediate_send(monkeypatch):
    """A direct /q captured for A must not dispatch to rebound owner B."""
    user_id = 1147817421
    thread_id = 1302
    chat_id = -100321
    owner_a = mq.TopicOwnership(
        window_id="@owner-a-1302",
        codex_thread_id="codex-thread-a-1302",
        machine_id="machine-a",
        cwd="/workspace/a",
    )
    owner_b = mq.TopicOwnership(
        window_id="@owner-b-1302",
        codex_thread_id="codex-thread-b-1302",
        machine_id="machine-b",
        cwd="/workspace/b",
    )
    current_owner = {"value": owner_a}
    capture_started = asyncio.Event()
    progress_check_started = asyncio.Event()
    release_progress_check = asyncio.Event()
    dispatched: list[tuple[str, str]] = []

    message = SimpleNamespace(
        text="/q prompt captured for owner A",
        chat=SimpleNamespace(id=chat_id, type="supergroup"),
        chat_id=chat_id,
        message_thread_id=thread_id,
        message_id=130201,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_message=message,
        effective_chat=message.chat,
        message=message,
    )
    context = SimpleNamespace(bot=object(), user_data={})

    def _resolve_binding(_uid, _tid, **_kwargs):
        owner = current_owner["value"]
        return SimpleNamespace(
            window_id=owner.window_id,
            codex_thread_id=owner.codex_thread_id,
            machine_id=owner.machine_id,
            cwd=owner.cwd,
        )

    def _capture_ownership(*_args, **_kwargs):
        assert current_owner["value"] is owner_a
        capture_started.set()
        return owner_a

    async def _is_window_in_progress(*_args, **_kwargs):
        progress_check_started.set()
        await release_progress_check.wait()
        return False

    async def _send_topic_text_to_window(
        *, window_id, text, topic_ownership=None, **_kwargs
    ):
        if topic_ownership is None:
            dispatched.append((current_owner["value"].window_id, text))
            return True, ""
        if topic_ownership != current_owner["value"]:
            return False, "stale topic owner; request was not sent"
        dispatched.append((window_id, text))
        return True, ""

    async def _noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(
        bot.session_manager, "set_group_chat_id", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_window_for_thread",
        lambda *_args, **_kwargs: owner_a.window_id,
    )
    monkeypatch.setattr(bot.session_manager, "resolve_topic_binding", _resolve_binding)
    monkeypatch.setattr(bot, "capture_topic_ownership", _capture_ownership)
    monkeypatch.setattr(bot, "_is_window_in_progress", _is_window_in_progress)
    monkeypatch.setattr(
        bot.session_manager, "send_topic_text_to_window", _send_topic_text_to_window
    )
    monkeypatch.setattr(
        bot.session_manager, "is_codex_active_writer_error", lambda _error: False
    )
    monkeypatch.setattr(bot, "safe_reply", _noop_async)
    monkeypatch.setattr(bot, "sync_queued_topic_dock", _noop_async)
    monkeypatch.setattr(bot, "_set_eyes_reaction", _noop_async)
    monkeypatch.setattr(bot, "note_run_started", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "emit_telemetry", lambda *_args, **_kwargs: None)

    send_task = asyncio.create_task(bot.queue_command(update, context))
    await asyncio.wait_for(
        asyncio.gather(capture_started.wait(), progress_check_started.wait()),
        timeout=1,
    )
    current_owner["value"] = owner_b
    release_progress_check.set()
    await asyncio.wait_for(send_task, timeout=1)

    assert not any(window_id == owner_b.window_id for window_id, _text in dispatched)
