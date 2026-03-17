"""Tests for one-way host-follow sync and takeover routing."""

import asyncio
from types import SimpleNamespace

import pytest

import coco.bot as bot
from coco.session import (
    TOPIC_SYNC_MODE_HOST_FOLLOW_FINAL,
    TOPIC_SYNC_MODE_TELEGRAM_LIVE,
    SessionManager,
)
from coco.session_monitor import NewMessage


@pytest.fixture
def mgr(monkeypatch) -> SessionManager:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    return SessionManager()


@pytest.mark.asyncio
async def test_handle_new_message_consumes_expected_transcript_user_echo(
    monkeypatch, mgr: SessionManager
):
    mgr.bind_topic_to_codex_thread(
        user_id=1,
        thread_id=10,
        codex_thread_id="thread-1",
        window_id="@1",
        cwd="/tmp/demo",
        display_name="demo",
    )
    mgr.register_expected_transcript_user_echo("@1", "expected transcript text")

    monkeypatch.setattr(bot, "_codex_app_server_enabled", lambda: True)
    monkeypatch.setattr(bot, "session_manager", mgr)

    events: list[str] = []

    async def _unexpected_enqueue_content_message(*_args, **_kwargs):
        events.append("content")

    monkeypatch.setattr(bot, "enqueue_content_message", _unexpected_enqueue_content_message)

    await bot.handle_new_message(
        NewMessage(
            session_id="thread-1",
            text="expected transcript text",
            is_complete=True,
            content_type="text",
            role="user",
            source="transcript",
        ),
        SimpleNamespace(),
    )

    assert mgr.get_topic_sync_mode(1, 10) == TOPIC_SYNC_MODE_TELEGRAM_LIVE
    assert events == []


@pytest.mark.asyncio
async def test_handle_new_message_switches_topic_into_host_follow_final(
    monkeypatch, mgr: SessionManager
):
    mgr.bind_topic_to_codex_thread(
        user_id=1,
        thread_id=10,
        codex_thread_id="thread-1",
        window_id="@1",
        cwd="/tmp/demo",
        display_name="demo",
    )

    monkeypatch.setattr(bot, "_codex_app_server_enabled", lambda: True)
    monkeypatch.setattr(bot, "session_manager", mgr)

    await bot.handle_new_message(
        NewMessage(
            session_id="thread-1",
            text="host typed locally",
            is_complete=True,
            content_type="text",
            role="user",
            source="transcript",
        ),
        SimpleNamespace(),
    )

    assert mgr.get_topic_sync_mode(1, 10) == TOPIC_SYNC_MODE_HOST_FOLLOW_FINAL
    assert mgr.is_window_external_turn_active("@1") is True


@pytest.mark.asyncio
async def test_handle_new_message_routes_only_final_text_in_host_follow_mode(
    monkeypatch, mgr: SessionManager
):
    mgr.bind_topic_to_codex_thread(
        user_id=1,
        thread_id=10,
        codex_thread_id="thread-1",
        window_id="@1",
        cwd="/tmp/demo",
        display_name="demo",
    )
    mgr.set_topic_sync_mode(1, 10, TOPIC_SYNC_MODE_HOST_FOLLOW_FINAL)
    mgr.set_window_external_turn_active("@1", True)

    monkeypatch.setattr(bot, "_codex_app_server_enabled", lambda: True)
    monkeypatch.setattr(bot, "session_manager", mgr)
    monkeypatch.setattr(bot, "note_run_activity", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "note_run_completed", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "build_response_parts", lambda text, *_args, **_kwargs: [text])
    monkeypatch.setattr(bot, "consume_looper_completion_keyword", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "queued_topic_input_count", lambda *_args, **_kwargs: 0)

    finalized: list[tuple[int, str, int | None, bool]] = []
    delivered: list[str] = []

    async def _enqueue_progress_finalize(_bot, user_id, window_id, thread_id=None, *, compact=False):
        finalized.append((user_id, window_id, thread_id, compact))

    async def _enqueue_content_message(*, text: str, **_kwargs):
        delivered.append(text)

    async def _update_offset(**_kwargs):
        return None

    monkeypatch.setattr(bot, "enqueue_progress_finalize", _enqueue_progress_finalize)
    monkeypatch.setattr(bot, "enqueue_content_message", _enqueue_content_message)
    monkeypatch.setattr(bot, "_update_user_read_offset_for_window", _update_offset)

    await bot.handle_new_message(
        NewMessage(
            session_id="thread-1",
            text="host final answer",
            is_complete=True,
            content_type="text",
            role="assistant",
            source="transcript",
        ),
        SimpleNamespace(),
    )

    assert finalized == [(1, "@1", 10, True)]
    assert delivered == ["host final answer"]
    assert mgr.is_window_external_turn_active("@1") is False


@pytest.mark.asyncio
async def test_handle_new_message_delivers_transcript_tool_result_images_in_telegram_live(
    monkeypatch, mgr: SessionManager
):
    mgr.bind_topic_to_codex_thread(
        user_id=1,
        thread_id=10,
        codex_thread_id="thread-1",
        window_id="@1",
        cwd="/tmp/demo",
        display_name="demo",
    )

    monkeypatch.setattr(bot, "_codex_app_server_enabled", lambda: True)
    monkeypatch.setattr(bot, "session_manager", mgr)
    monkeypatch.setattr(bot, "note_run_activity", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "build_response_parts", lambda text, *_args, **_kwargs: [text])

    delivered: list[dict[str, object]] = []

    async def _enqueue_content_message(**kwargs):
        delivered.append(kwargs)

    async def _update_offset(**_kwargs):
        return None

    monkeypatch.setattr(bot, "enqueue_content_message", _enqueue_content_message)
    monkeypatch.setattr(bot, "_update_user_read_offset_for_window", _update_offset)

    await bot.handle_new_message(
        NewMessage(
            session_id="thread-1",
            text="  ⎿  Wrote 1 lines",
            is_complete=True,
            content_type="tool_result",
            role="assistant",
            source="transcript",
            image_data=[("image/png", b"png-bytes")],
        ),
        SimpleNamespace(),
    )

    assert len(delivered) == 1
    assert delivered[0]["content_type"] == "tool_result"
    assert delivered[0]["image_data"] == [("image/png", b"png-bytes")]


@pytest.mark.asyncio
async def test_handle_new_message_delivers_native_transcript_progress_in_telegram_live(
    monkeypatch, mgr: SessionManager
):
    mgr.bind_topic_to_codex_thread(
        user_id=1,
        thread_id=10,
        codex_thread_id="thread-1",
        window_id="@1",
        cwd="/tmp/demo",
        display_name="demo",
    )

    monkeypatch.setattr(bot, "_codex_app_server_enabled", lambda: True)
    monkeypatch.setattr(bot, "session_manager", mgr)
    monkeypatch.setattr(bot, "note_run_activity", lambda **_kwargs: None)

    progress_updates: list[dict[str, object]] = []

    async def _enqueue_progress_update(**kwargs):
        progress_updates.append(kwargs)

    monkeypatch.setattr(bot, "enqueue_progress_update", _enqueue_progress_update)

    await bot.handle_new_message(
        NewMessage(
            session_id="thread-1",
            text="web search: site:support.google.com sender guidelines",
            is_complete=True,
            content_type="progress",
            role="assistant",
            source="transcript",
            event_type="response_item:web_search_call",
        ),
        SimpleNamespace(),
    )

    assert len(progress_updates) == 1
    assert progress_updates[0]["progress_text"] == (
        "web search: site:support.google.com sender guidelines"
    )


@pytest.mark.asyncio
async def test_handle_new_message_extracts_hidden_document_attachments(
    monkeypatch, mgr: SessionManager, tmp_path
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    report = workspace / "report.md"
    report.write_text("# Report\n", encoding="utf-8")

    mgr.bind_topic_to_codex_thread(
        user_id=1,
        thread_id=10,
        codex_thread_id="thread-1",
        window_id="@1",
        cwd=str(workspace),
        display_name="demo",
    )
    mgr.get_window_state("@1").cwd = str(workspace)

    monkeypatch.setattr(bot, "_codex_app_server_enabled", lambda: True)
    monkeypatch.setattr(bot, "session_manager", mgr)
    monkeypatch.setattr(bot, "note_run_activity", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "note_run_completed", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "consume_looper_completion_keyword", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "queued_topic_input_count", lambda *_args, **_kwargs: 0)

    finalized: list[tuple[int, str, int | None, bool]] = []
    delivered: list[dict[str, object]] = []

    async def _enqueue_progress_finalize(_bot, user_id, window_id, thread_id=None, *, compact=False):
        finalized.append((user_id, window_id, thread_id, compact))

    async def _enqueue_content_message(**kwargs):
        delivered.append(kwargs)

    async def _update_offset(**_kwargs):
        return None

    monkeypatch.setattr(bot, "enqueue_progress_finalize", _enqueue_progress_finalize)
    monkeypatch.setattr(bot, "enqueue_content_message", _enqueue_content_message)
    monkeypatch.setattr(bot, "_update_user_read_offset_for_window", _update_offset)

    await bot.handle_new_message(
        NewMessage(
            session_id="thread-1",
            text=(
                "Attached the markdown report.\n"
                '<telegram-attachment path="report.md" />'
            ),
            is_complete=True,
            content_type="text",
            role="assistant",
            source="app_server",
        ),
        SimpleNamespace(),
    )

    assert finalized == [(1, "@1", 10, True)]
    assert len(delivered) == 1
    assert delivered[0]["text"] == "Attached the markdown report."
    assert delivered[0]["document_data"] == [("report.md", b"# Report\n")]


@pytest.mark.asyncio
async def test_handle_new_message_extracts_hidden_remote_document_attachments(
    monkeypatch, mgr: SessionManager
):
    mgr.bind_topic_to_codex_thread(
        user_id=1,
        thread_id=10,
        codex_thread_id="thread-1",
        window_id="@1",
        cwd="/srv/demo",
        display_name="demo",
        machine_id="remote-node",
        machine_display_name="Remote Node",
    )

    monkeypatch.setattr(bot, "_codex_app_server_enabled", lambda: True)
    monkeypatch.setattr(bot, "session_manager", mgr)
    monkeypatch.setattr(bot, "note_run_activity", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "note_run_completed", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "build_response_parts", lambda text, *_args, **_kwargs: [text])
    monkeypatch.setattr(bot, "consume_looper_completion_keyword", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "_resolve_workspace_dir_for_window", lambda **_kwargs: "/srv/demo")
    monkeypatch.setattr(bot, "queued_topic_input_count", lambda *_args, **_kwargs: 0)

    async def _read_documents(
        machine_id: str,
        *,
        workspace_dir: str,
        paths: list[str],
    ):
        assert machine_id == "remote-node"
        assert workspace_dir == "/srv/demo"
        assert paths == ["report.md"]
        return [("report.md", b"# Remote report\n")]

    monkeypatch.setattr("coco.agent_rpc.agent_rpc_client.read_documents", _read_documents)

    delivered: list[dict[str, object]] = []

    async def _enqueue_content_message(**kwargs):
        delivered.append(kwargs)

    async def _enqueue_progress_finalize(*_args, **_kwargs):
        return None

    async def _update_offset(**_kwargs):
        return None

    monkeypatch.setattr(bot, "enqueue_content_message", _enqueue_content_message)
    monkeypatch.setattr(bot, "enqueue_progress_finalize", _enqueue_progress_finalize)
    monkeypatch.setattr(bot, "_update_user_read_offset_for_window", _update_offset)

    await bot.handle_new_message(
        NewMessage(
            session_id="thread-1",
            text=(
                "Final answer\n"
                '<telegram-attachment path="report.md" />'
            ),
            is_complete=True,
            content_type="text",
            role="assistant",
            source="app_server",
        ),
        SimpleNamespace(),
    )

    assert delivered[0]["text"] == "Final answer"
    assert delivered[0]["document_data"] == [("report.md", b"# Remote report\n")]


@pytest.mark.asyncio
async def test_handle_new_message_final_text_does_not_dispatch_queue_before_completion(
    monkeypatch, mgr: SessionManager
):
    mgr.bind_topic_to_codex_thread(
        user_id=1,
        thread_id=10,
        codex_thread_id="thread-1",
        window_id="@1",
        cwd="/tmp/demo",
        display_name="demo",
    )

    monkeypatch.setattr(bot, "_codex_app_server_enabled", lambda: True)
    monkeypatch.setattr(bot, "session_manager", mgr)
    monkeypatch.setattr(bot, "note_run_activity", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "note_run_completed", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "consume_looper_completion_keyword", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "build_response_parts", lambda text, *_args, **_kwargs: [text])
    monkeypatch.setattr(bot, "queued_topic_input_count", lambda *_args, **_kwargs: 1)

    dispatched: list[dict[str, object]] = []

    async def _enqueue_progress_finalize(*_args, **_kwargs):
        return None

    async def _enqueue_content_message(**_kwargs):
        return None

    async def _update_offset(**_kwargs):
        return None

    async def _dispatch_next(**kwargs):
        dispatched.append(kwargs)

    monkeypatch.setattr(bot, "enqueue_progress_finalize", _enqueue_progress_finalize)
    monkeypatch.setattr(bot, "enqueue_content_message", _enqueue_content_message)
    monkeypatch.setattr(bot, "_update_user_read_offset_for_window", _update_offset)
    monkeypatch.setattr(bot, "_dispatch_next_queued_input", _dispatch_next)

    await bot.handle_new_message(
        NewMessage(
            session_id="thread-1",
            text="final answer before turn completed",
            is_complete=True,
            content_type="text",
            role="assistant",
            source="app_server",
        ),
        SimpleNamespace(),
    )

    assert dispatched == []


@pytest.mark.asyncio
async def test_handle_new_message_task_complete_dispatches_waiting_queue(
    monkeypatch, mgr: SessionManager
):
    mgr.bind_topic_to_codex_thread(
        user_id=1,
        thread_id=10,
        codex_thread_id="thread-1",
        window_id="@1",
        cwd="/tmp/demo",
        display_name="demo",
    )
    mgr.set_topic_sync_mode(1, 10, TOPIC_SYNC_MODE_HOST_FOLLOW_FINAL)
    mgr.set_window_external_turn_active("@1", True)

    monkeypatch.setattr(bot, "_codex_app_server_enabled", lambda: True)
    monkeypatch.setattr(bot, "session_manager", mgr)
    monkeypatch.setattr(bot, "queued_topic_input_count", lambda *_args, **_kwargs: 1)

    dispatched: list[dict[str, object]] = []

    async def _dispatch_next(**kwargs):
        dispatched.append(kwargs)

    monkeypatch.setattr(bot, "_dispatch_next_queued_input", _dispatch_next)

    await bot.handle_new_message(
        NewMessage(
            session_id="thread-1",
            text="",
            is_complete=True,
            content_type="lifecycle",
            role="system",
            source="transcript",
            event_type="task_complete",
        ),
        SimpleNamespace(),
    )

    assert mgr.is_window_external_turn_active("@1") is False
    assert len(dispatched) == 1
    assert dispatched[0]["thread_id"] == 10
    assert dispatched[0]["window_id"] == "@1"


@pytest.mark.asyncio
async def test_post_init_starts_shadow_session_monitor_when_app_server_is_enabled(
    monkeypatch,
):
    class _FakeBot:
        def __init__(self) -> None:
            self.rate_limiter = SimpleNamespace(_base_limiter=None)

        async def delete_my_commands(self):
            return None

        async def set_my_commands(self, _commands):
            return None

    app = SimpleNamespace(bot=_FakeBot())

    class _FakeMonitor:
        def __init__(self) -> None:
            self.callback = None
            self.started = False

        def set_message_callback(self, callback):
            self.callback = callback

        def start(self):
            self.started = True

        def stop(self):
            self.started = False

    fake_monitor = _FakeMonitor()
    fake_task = asyncio.create_task(asyncio.sleep(0))

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bot, "SessionMonitor", lambda: fake_monitor)
    monkeypatch.setattr(bot, "_pop_restart_notice_target", lambda: None)
    monkeypatch.setattr(bot, "_startup_notice_targets", lambda _target: [])
    monkeypatch.setattr(bot, "_codex_app_server_preferred", lambda: True)
    monkeypatch.setattr(bot, "_ensure_codex_trust_for_runtime", lambda: None)
    monkeypatch.setattr(bot.session_manager, "resolve_stale_ids", _noop)
    monkeypatch.setattr(
        bot.session_manager,
        "validate_codex_topic_bindings",
        lambda: {"checked": 0, "invalid": 0, "repaired": 0},
    )
    monkeypatch.setattr(bot.codex_app_server_client, "set_handlers", _noop)
    monkeypatch.setattr(bot.codex_app_server_client, "ensure_started", _noop)
    monkeypatch.setattr(bot, "status_poll_loop", lambda _bot: asyncio.sleep(0))

    class _FakeControllerRpcServer:
        async def start(self, *, host: str, port: int):
            self.host = host
            self.port = port

        def bound_address(self):
            return ("127.0.0.1", 8787)

    monkeypatch.setattr(bot, "ControllerRpcServer", lambda **_kwargs: _FakeControllerRpcServer())

    def _create_task(coro):
        coro.close()
        return fake_task

    monkeypatch.setattr(bot.asyncio, "create_task", _create_task)

    bot.session_monitor = None
    await bot.post_init(app)

    assert bot.session_monitor is fake_monitor
    assert fake_monitor.started is True

    fake_task.cancel()
