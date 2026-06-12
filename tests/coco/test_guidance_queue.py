"""Tests for steer/queued message helper state."""

import asyncio
import contextlib
from types import SimpleNamespace

import pytest
from telegram.error import RetryAfter

import coco.bot as bot
import coco.handlers.message_queue as mq


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
    mq.clear_queued_topic_inputs(user_id, thread_id)

    assert mq.queued_topic_input_count(user_id, thread_id) == 0

    assert mq.enqueue_queued_topic_input(user_id, thread_id, "first", -100, 1) == 1
    assert mq.enqueue_queued_topic_input(user_id, thread_id, "second", -100, 2) == 2
    assert mq.queued_topic_input_count(user_id, thread_id) == 2

    assert mq.pop_queued_topic_input(user_id, thread_id) == ("first", -100, 1)
    assert mq.pop_queued_topic_input(user_id, thread_id) == ("second", -100, 2)
    assert mq.pop_queued_topic_input(user_id, thread_id) is None
    assert mq.queued_topic_input_count(user_id, thread_id) == 0


def test_is_progress_active_uses_topic_key():
    user_id = 33
    thread_id = 44
    key = (user_id, thread_id)
    mq._progress_msg_info[key] = (123, "@9", "working")
    try:
        assert mq.is_progress_active(user_id, thread_id) is True
        assert mq.is_progress_active(user_id, thread_id + 1) is False
    finally:
        mq.clear_progress_msg_info(user_id, thread_id)


def test_get_progress_text_uses_topic_key():
    user_id = 35
    thread_id = 46
    key = (user_id, thread_id)
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
    skey = (user_id, thread_id)
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
    skey = (user_id, thread_id)
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
    skey = (user_id, thread_id)
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
    skey = (user_id, thread_id)
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
    skey = (user_id, thread_id)
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
    skey = (user_id, thread_id)
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
    skey = (user_id, thread_id)
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
    skey = (user_id, thread_id)
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
    skey = (user_id, thread_id)
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
    skey = (user_id, thread_id)
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
    skey = (user_id, thread_id)
    mq._status_msg_info[skey] = (7013, "@17", "old status")

    class _Bot:
        async def edit_message_text(self, **_kwargs):
            raise Exception("cannot edit")

        async def send_chat_action(self, **_kwargs):
            return True

    observed_before_send: list[tuple[int, str, str] | None] = []

    async def _fake_send_status(_bot, _user_id, tid, wid, text):
        observed_before_send.append(mq._status_msg_info.get((user_id, tid)))
        mq._status_msg_info[(user_id, tid)] = (7014, wid, text)

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
    skey = (user_id, thread_id)
    mq._progress_msg_info[skey] = (7015, "@18", "old progress")

    class _Bot:
        async def edit_message_text(self, **_kwargs):
            raise Exception("cannot edit")

    async def _noop_clear_status(*_args, **_kwargs):
        return None

    observed_before_send: list[tuple[int, str, str] | None] = []

    async def _fake_send_progress(_bot, _user_id, tid, wid, accumulated_text, **_kwargs):
        observed_before_send.append(mq._progress_msg_info.get((user_id, tid)))
        mq._progress_msg_info[(user_id, tid)] = (7016, wid, accumulated_text)

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
    skey = (user_id, thread_id)
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
    skey = (user_id, thread_id)
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
    skey = (user_id, thread_id)
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

        assert mq._queued_delivery_topic_counts[user_id] == {thread_id: 1}
        assert await mq.get_pending_delivery_topics(user_id) == {thread_id}
    finally:
        while not queue.empty():
            queue.get_nowait()
            queue.task_done()
        mq._message_queues.pop(user_id, None)
        mq._queue_locks.pop(user_id, None)
        mq._queued_delivery_topic_counts.pop(user_id, None)
        mq._progress_text_cache.pop((user_id, thread_id), None)


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
        assert mq._queued_delivery_topic_counts[user_id] == {thread_id: 1}
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
        assert mq._queued_delivery_topic_counts[user_id] == {queued_thread_id: 1}
        assert await mq.get_pending_delivery_topics(user_id) == {queued_thread_id}
        assert queue.get_nowait() is different_topic
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
            queued_thread_id: 1,
            retry_thread_id: 1,
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
    skey = (user_id, thread_id)
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
    skey = (user_id, thread_id)
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
    skey = (user_id, thread_id)
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

    async def _is_in_progress(_uid: int, _tid: int | None, _wid: str) -> bool:
        return True

    async def _send_topic_text_to_window(
        *,
        user_id: int,
        thread_id: int | None,
        chat_id: int | None = None,
        window_id: str,
        text: str,
        steer: bool = False,
    ):
        _ = user_id, thread_id, chat_id, text, steer
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


    async def _is_window_in_progress(_uid: int, _tid: int | None, _wid: str) -> bool:
        return False

    async def _send_topic_text_to_window(
        *,
        user_id: int,
        thread_id: int | None,
        chat_id: int | None = None,
        window_id: str,
        text: str,
        steer: bool = False,
    ):
        _ = user_id, thread_id, chat_id, text, steer
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


@pytest.mark.asyncio
async def test_text_handler_auto_queues_when_host_turn_is_active(monkeypatch):
    user_id = 1147817421
    thread_id = 777
    mq.clear_queued_topic_inputs(user_id, thread_id)

    class _Chat:
        type = "supergroup"
        id = -100123

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
        assert mq.queued_topic_input_count(user_id, thread_id) == 1
        assert "dock" in events
        assert "hourglass" in events
        assert not any(item.startswith("safe_reply:") for item in events)
    finally:
        mq.clear_queued_topic_inputs(user_id, thread_id)


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

    async def _is_window_in_progress(_uid: int, _tid: int | None, _wid: str) -> bool:
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

    async def _is_window_in_progress(_uid: int, _tid: int | None, _wid: str) -> bool:
        return False

    async def _send_topic_text_to_window(
        *,
        user_id: int,
        thread_id: int | None,
        chat_id: int | None = None,
        window_id: str,
        text: str,
        steer: bool = False,
    ):
        _ = user_id, thread_id, chat_id, text, steer
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

    def _enqueue(user_id, thread_id, text, source_chat_id, source_message_id):
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

    def _enqueue(_uid: int, _tid: int, _text: str, _chat_id: int, _msg_id: int):
        events.append("internal_queue")
        return 1

    async def _sync_dock(_bot, _uid: int, _tid: int, *, window_id: str | None = None):
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

    def _enqueue(_uid: int, _tid: int, _text: str, _chat_id: int, _msg_id: int):
        events.append("internal_queue")
        return 1

    async def _sync_dock(_bot, _uid: int, _tid: int, *, window_id: str | None = None):
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

    def _enqueue(_uid: int, _tid: int, _text: str, _chat_id: int, _msg_id: int):
        events.append("internal_queue")
        return 1

    async def _sync_dock(_bot, _uid: int, _tid: int, *, window_id: str | None = None):
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
async def test_dispatch_next_q_updates_dock_posts_marker_and_reacts(monkeypatch):
    mq.clear_queued_topic_inputs(1147817421, 777)
    mq.enqueue_queued_topic_input(1147817421, 777, "first queued task", -100321, 111)
    mq.enqueue_queued_topic_input(1147817421, 777, "second queued task", -100321, 222)

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

    async def _sync_dock(_bot, _uid: int, _tid: int, *, window_id: str | None = None):
        events.append(f"dock_sync:{mq.queued_topic_input_count(1147817421, 777)}:{window_id}")

    async def _send_topic_text_to_window(
        *,
        user_id: int,
        thread_id: int | None,
        chat_id: int | None = None,
        window_id: str,
        text: str,
        steer: bool = False,
        force_new_turn: bool = False,
    ):
        _ = user_id, thread_id, chat_id, steer
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
    )

    assert events[0] == "dock_sync:1:@77"
    assert events[1] == "send_to_window:@77:first queued task:force_new_turn=True"
    assert "run_started" in events
    assert not any(event.startswith("safe_send:") for event in events)
    assert any(ev.startswith("reaction:-100321:111") for ev in events)
    assert mq.queued_topic_input_count(1147817421, 777) == 1

    mq.clear_queued_topic_inputs(1147817421, 777)


@pytest.mark.asyncio
async def test_dispatch_next_q_requeues_when_send_fails(monkeypatch):
    mq.clear_queued_topic_inputs(1147817421, 888)
    mq.enqueue_queued_topic_input(1147817421, 888, "first queued task", -100321, 333)

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

    async def _sync_dock(_bot, _uid: int, _tid: int, *, window_id: str | None = None):
        _ = window_id
        sync_counts.append(mq.queued_topic_input_count(1147817421, 888))

    async def _send_topic_text_to_window(
        *,
        user_id: int,
        thread_id: int | None,
        chat_id: int | None = None,
        window_id: str,
        text: str,
        steer: bool = False,
        force_new_turn: bool = False,
    ):
        _ = user_id, thread_id, chat_id, window_id, text, steer, force_new_turn
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
    )

    assert sync_counts == [0, 1]
    assert mq.queued_topic_input_count(1147817421, 888) == 1
    assert sent_text
    assert "Failed to send queued" in sent_text[0]

    mq.clear_queued_topic_inputs(1147817421, 888)


@pytest.mark.asyncio
async def test_dispatch_next_q_defers_when_window_still_in_progress(monkeypatch):
    mq.clear_queued_topic_inputs(1147817421, 999)
    mq.enqueue_queued_topic_input(1147817421, 999, "first queued task", -100321, 444)

    sync_counts: list[int] = []
    events: list[str] = []

    class _FakeBot:
        async def set_message_reaction(self, **_kwargs):
            raise AssertionError("reaction should not be set while dispatch is deferred")

    async def _is_window_in_progress(*_args, **_kwargs):
        return True

    async def _sync_dock(_bot, _uid: int, _tid: int, *, window_id: str | None = None):
        _ = window_id
        sync_counts.append(mq.queued_topic_input_count(1147817421, 999))

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
    )

    assert sync_counts == [1]
    assert mq.queued_topic_input_count(1147817421, 999) == 1
    assert "queue.dispatch.deferred_active_turn:999" in events

    mq.clear_queued_topic_inputs(1147817421, 999)
