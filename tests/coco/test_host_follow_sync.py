"""Tests for one-way host-follow sync and takeover routing."""

import asyncio
import contextlib
from types import SimpleNamespace

import pytest

import coco.bot as bot
import coco.handlers.message_queue as mq
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
    manager = SessionManager()
    monkeypatch.setattr(mq, "session_manager", manager)
    return manager


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
    async def _shadow_passthrough(**_kwargs):
        return False

    monkeypatch.setattr(
        bot,
        "_handle_shadow_transcript_message_for_topic",
        _shadow_passthrough,
    )
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

    async def _enqueue_progress_finalize(_bot, user_id, window_id, thread_id=None, *, compact=False, chat_id=None):
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
async def test_handle_new_message_host_follow_final_preserves_voice_mode_binding(
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
    mgr.set_topic_response_mode(1, 10, response_mode="voice")

    monkeypatch.setattr(bot, "_codex_app_server_enabled", lambda: True)
    monkeypatch.setattr(bot, "session_manager", mgr)
    monkeypatch.setattr(bot, "note_run_activity", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "note_run_completed", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "build_response_parts", lambda text, *_args, **_kwargs: [text])
    monkeypatch.setattr(bot, "consume_looper_completion_keyword", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "queued_topic_input_count", lambda *_args, **_kwargs: 0)

    async def _enqueue_progress_finalize(*_args, **_kwargs):
        return None

    async def _enqueue_content_message(**_kwargs):
        return None

    async def _update_offset(**_kwargs):
        return None

    monkeypatch.setattr(bot, "enqueue_progress_finalize", _enqueue_progress_finalize)
    monkeypatch.setattr(bot, "enqueue_content_message", _enqueue_content_message)
    monkeypatch.setattr(bot, "_update_user_read_offset_for_window", _update_offset)

    await bot.handle_new_message(
        NewMessage(
            session_id="thread-1",
            text="host final voice answer",
            is_complete=True,
            content_type="text",
            role="assistant",
            source="transcript",
        ),
        SimpleNamespace(),
    )

    assert mgr.get_topic_response_mode(1, 10) == "voice"


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
    assert bot._turn_has_final_text.get("thread-1") is True


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

    async def _enqueue_progress_finalize(_bot, user_id, window_id, thread_id=None, *, compact=False, chat_id=None):
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
    assert delivered[0]["image_data"] is None


@pytest.mark.asyncio
async def test_handle_new_message_extracts_hidden_image_attachments(
    monkeypatch, mgr: SessionManager, tmp_path
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    preview = workspace / "preview.webp"
    preview.write_bytes(b"WEBP-preview")

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

    delivered: list[dict[str, object]] = []

    async def _enqueue_progress_finalize(*_args, **_kwargs):
        return None

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
                "Attached the preview image.\n"
                '<telegram-attachment path="preview.webp" />'
            ),
            is_complete=True,
            content_type="text",
            role="assistant",
            source="app_server",
        ),
        SimpleNamespace(),
    )

    assert delivered[0]["text"] == "Attached the preview image."
    assert delivered[0]["image_data"] == [("image/webp", b"WEBP-preview")]
    assert delivered[0]["document_data"] is None


@pytest.mark.asyncio
async def test_handle_new_message_extracts_hidden_video_attachments(
    monkeypatch, mgr: SessionManager, tmp_path
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    clip = workspace / "clip.mp4"
    clip.write_bytes(b"MP4-preview")

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

    delivered: list[dict[str, object]] = []

    async def _enqueue_progress_finalize(*_args, **_kwargs):
        return None

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
                "Attached the preview video.\n"
                '<telegram-attachment path="clip.mp4" />'
            ),
            is_complete=True,
            content_type="text",
            role="assistant",
            source="app_server",
        ),
        SimpleNamespace(),
    )

    assert delivered[0]["text"] == "Attached the preview video."
    assert delivered[0]["video_data"] == [("video/mp4", b"MP4-preview")]
    assert delivered[0]["image_data"] is None
    assert delivered[0]["document_data"] is None


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

    async def _read_attachments(
        machine_id: str,
        *,
        workspace_dir: str,
        paths: list[str],
    ):
        assert machine_id == "remote-node"
        assert workspace_dir == "/srv/demo"
        assert paths == ["report.md"]
        return {
            "documents": [("report.md", b"# Remote report\n")],
            "images": [],
        }

    monkeypatch.setattr("coco.agent_rpc.agent_rpc_client.read_attachments", _read_attachments)

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
    assert delivered[0]["image_data"] is None


@pytest.mark.asyncio
async def test_handle_new_message_extracts_hidden_remote_image_attachments(
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

    async def _read_attachments(
        machine_id: str,
        *,
        workspace_dir: str,
        paths: list[str],
    ):
        assert machine_id == "remote-node"
        assert workspace_dir == "/srv/demo"
        assert paths == ["preview.webp"]
        return {
            "documents": [],
            "images": [("image/webp", b"remote-webp")],
        }

    monkeypatch.setattr("coco.agent_rpc.agent_rpc_client.read_attachments", _read_attachments)

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
                '<telegram-attachment path="preview.webp" />'
            ),
            is_complete=True,
            content_type="text",
            role="assistant",
            source="app_server",
        ),
        SimpleNamespace(),
    )

    assert delivered[0]["text"] == "Final answer"
    assert delivered[0]["image_data"] == [("image/webp", b"remote-webp")]
    assert delivered[0]["document_data"] is None


@pytest.mark.asyncio
async def test_handle_new_message_extracts_hidden_remote_video_attachments(
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

    async def _read_attachments(
        machine_id: str,
        *,
        workspace_dir: str,
        paths: list[str],
    ):
        assert machine_id == "remote-node"
        assert workspace_dir == "/srv/demo"
        assert paths == ["clip.mp4"]
        return {
            "documents": [],
            "images": [],
            "videos": [("video/mp4", b"REMOTE-MP4")],
        }

    monkeypatch.setattr("coco.agent_rpc.agent_rpc_client.read_attachments", _read_attachments)

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
                '<telegram-attachment path="clip.mp4" />'
            ),
            is_complete=True,
            content_type="text",
            role="assistant",
            source="app_server",
        ),
        SimpleNamespace(),
    )

    assert delivered[0]["text"] == "Final answer"
    assert delivered[0]["video_data"] == [("video/mp4", b"REMOTE-MP4")]
    assert delivered[0]["image_data"] is None
    assert delivered[0]["document_data"] is None


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
async def test_handle_new_message_drops_late_final_after_topic_rebind_before_enqueue(
    monkeypatch, mgr: SessionManager
):
    """A final from Codex A must not enter Telegram after the topic selects B."""
    user_id = 991002
    telegram_thread_id = 10
    chat_id = -100010
    mgr.bind_topic_to_codex_thread(
        user_id=user_id,
        thread_id=telegram_thread_id,
        chat_id=chat_id,
        codex_thread_id="codex-A",
        window_id="@1",
        cwd="/tmp/demo",
        display_name="demo",
    )

    monkeypatch.setattr(bot, "_codex_app_server_enabled", lambda: True)
    monkeypatch.setattr(bot, "session_manager", mgr)
    monkeypatch.setattr(mq, "session_manager", mgr)
    monkeypatch.setattr(bot, "note_run_activity", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "note_run_completed", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "build_response_parts", lambda text, *_args: [text])
    monkeypatch.setattr(bot, "consume_looper_completion_keyword", lambda **_kwargs: None)

    async def _shadow_passthrough(**_kwargs):
        return False

    monkeypatch.setattr(bot, "_handle_shadow_transcript_message_for_topic", _shadow_passthrough)
    monkeypatch.setattr(bot, "queued_topic_input_count", lambda *_args, **_kwargs: 0)

    attachment_started = asyncio.Event()
    release_attachment = asyncio.Event()

    async def _extract_attachments(text, *, workspace_dir, window_id):
        _ = workspace_dir, window_id
        attachment_started.set()
        await release_attachment.wait()
        return text, None, None, None

    monkeypatch.setattr(bot, "_extract_telegram_attachments_for_window", _extract_attachments)

    async def _enqueue_progress_finalize(*_args, **_kwargs):
        return None

    async def _update_offset(**_kwargs):
        return None

    monkeypatch.setattr(bot, "enqueue_progress_finalize", _enqueue_progress_finalize)
    monkeypatch.setattr(bot, "_update_user_read_offset_for_window", _update_offset)

    sent: list[str] = []

    async def _send(_bot, _chat_id, text, **_kwargs):
        sent.append(text)
        return SimpleNamespace(message_id=len(sent))

    monkeypatch.setattr(mq, "send_with_fallback", _send)

    handling = asyncio.create_task(
        bot.handle_new_message(
            NewMessage(
                session_id="codex-A",
                text="late answer from A",
                is_complete=True,
                content_type="text",
                role="assistant",
                source="app_server",
            ),
            SimpleNamespace(),
        )
    )
    await asyncio.wait_for(attachment_started.wait(), timeout=1)

    # Model an explicit /resume selection while A's callback is awaiting I/O.
    mgr.bind_topic_to_codex_thread(
        user_id=user_id,
        thread_id=telegram_thread_id,
        chat_id=chat_id,
        codex_thread_id="codex-B",
        window_id="@1",
        cwd="/tmp/demo",
        display_name="demo",
    )
    release_attachment.set()
    await handling

    observed: list[str] = []
    try:
        queue = mq.get_message_queue(user_id)
        if queue is not None:
            await asyncio.wait_for(queue.join(), timeout=1)
        observed = list(sent)
    finally:
        worker = mq._queue_workers.pop(user_id, None)
        if worker is not None:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        mq._message_queues.pop(user_id, None)
        mq._queue_locks.pop(user_id, None)
        mq._active_delivery_topics.pop(user_id, None)
        mq._queued_delivery_topic_counts.pop(user_id, None)
        mq._topic_delivery_generations.pop((user_id, chat_id, telegram_thread_id), None)

    assert observed == []


@pytest.mark.asyncio
async def test_content_delivery_stops_split_output_after_topic_rebind(
    monkeypatch, mgr: SessionManager
):
    """A queued split response must re-check topic ownership before each send."""
    user_id = 991001
    telegram_thread_id = 11
    chat_id = -100011
    mgr.bind_topic_to_codex_thread(
        user_id=user_id,
        thread_id=telegram_thread_id,
        chat_id=chat_id,
        codex_thread_id="codex-A",
        window_id="@1",
        cwd="/tmp/demo",
        display_name="demo",
    )
    monkeypatch.setattr(mq, "session_manager", mgr)

    queue: asyncio.Queue[mq.MessageTask] = asyncio.Queue()
    mq._message_queues[user_id] = queue
    mq._queue_locks[user_id] = asyncio.Lock()
    first_send_started = asyncio.Event()
    release_first_send = asyncio.Event()
    sent_parts: list[str] = []

    async def _send(_bot, _chat_id, text, **_kwargs):
        sent_parts.append(text)
        if text == "first part from A":
            first_send_started.set()
            await release_first_send.wait()
        return SimpleNamespace(message_id=len(sent_parts))

    async def _status(*_args, **_kwargs):
        return None

    monkeypatch.setattr(mq, "send_with_fallback", _send)
    monkeypatch.setattr(mq, "_check_and_send_status", _status)

    await mq.enqueue_content_message(
        object(),  # type: ignore[arg-type]
        user_id,
        "@1",
        ["first part from A", "late second part from A"],
        thread_id=telegram_thread_id,
        chat_id=chat_id,
    )
    worker = asyncio.create_task(mq._message_queue_worker(object(), user_id))

    try:
        await asyncio.wait_for(first_send_started.wait(), timeout=1)
        mgr.bind_topic_to_codex_thread(
            user_id=user_id,
            thread_id=telegram_thread_id,
            chat_id=chat_id,
            codex_thread_id="codex-B",
            window_id="@1",
            cwd="/tmp/demo",
            display_name="demo",
        )
        release_first_send.set()
        await asyncio.wait_for(queue.join(), timeout=1)

        assert sent_parts == ["first part from A"]
    finally:
        release_first_send.set()
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        mq._message_queues.pop(user_id, None)
        mq._queue_locks.pop(user_id, None)
        mq._active_delivery_topics.pop(user_id, None)
        mq._queued_delivery_topic_counts.pop(user_id, None)
        mq._topic_delivery_generations.pop((user_id, chat_id, telegram_thread_id), None)


@pytest.mark.asyncio
async def test_tool_result_delivery_stops_after_status_clear_topic_rebind(
    monkeypatch, mgr: SessionManager
):
    """A tool result must not edit A after its awaited status clear loses ownership."""
    user_id = 991003
    telegram_thread_id = 12
    chat_id = -100012
    mgr.bind_topic_to_codex_thread(
        user_id=user_id,
        thread_id=telegram_thread_id,
        chat_id=chat_id,
        codex_thread_id="codex-A",
        window_id="@1",
        cwd="/tmp/demo",
        display_name="demo",
    )
    mgr.set_group_chat_id(user_id, telegram_thread_id, chat_id)
    monkeypatch.setattr(mq, "session_manager", mgr)

    queue: asyncio.Queue[mq.MessageTask] = asyncio.Queue()
    mq._message_queues[user_id] = queue
    mq._queue_locks[user_id] = asyncio.Lock()
    clear_started = asyncio.Event()
    release_clear = asyncio.Event()
    edits: list[dict[str, object]] = []
    sends: list[dict[str, object]] = []

    class _Bot:
        async def delete_message(self, **_kwargs):
            clear_started.set()
            await release_clear.wait()

        async def edit_message_text(self, **kwargs):
            edits.append(kwargs)

        async def send_message(self, **kwargs):
            sends.append(kwargs)
            return SimpleNamespace(message_id=len(sends))

    tool_use_id = "tool-A"
    mq._tool_msg_ids[(tool_use_id, user_id, chat_id, telegram_thread_id)] = 7001
    mq._status_msg_info[(user_id, chat_id, telegram_thread_id)] = (
        7002,
        "@1",
        "working",
    )
    task = mq.MessageTask(
        task_type="content",
        window_id="@1",
        parts=["result from A"],
        tool_use_id=tool_use_id,
        content_type="tool_result",
        thread_id=telegram_thread_id,
        chat_id=chat_id,
        topic_ownership=mq.capture_topic_ownership(
            user_id,
            telegram_thread_id,
            chat_id,
        ),
    )
    mq._put_queued_task(user_id, queue, task)
    worker = asyncio.create_task(mq._message_queue_worker(_Bot(), user_id))

    try:
        await asyncio.wait_for(clear_started.wait(), timeout=1)
        mgr.bind_topic_to_codex_thread(
            user_id=user_id,
            thread_id=telegram_thread_id,
            chat_id=chat_id,
            codex_thread_id="codex-B",
            window_id="@1",
            cwd="/tmp/demo",
            display_name="demo",
        )
        release_clear.set()
        await asyncio.wait_for(queue.join(), timeout=1)

        assert edits == []
        assert sends == []
    finally:
        release_clear.set()
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        mq._message_queues.pop(user_id, None)
        mq._queue_locks.pop(user_id, None)
        mq._active_delivery_topics.pop(user_id, None)
        mq._queued_delivery_topic_counts.pop(user_id, None)
        mq._topic_delivery_generations.pop((user_id, chat_id, telegram_thread_id), None)
        mq._tool_msg_ids.pop((tool_use_id, user_id, chat_id, telegram_thread_id), None)
        mq._status_msg_info.pop((user_id, chat_id, telegram_thread_id), None)


@pytest.mark.asyncio
async def test_tool_result_delivery_stops_plain_fallback_edit_after_awaited_rebind(
    monkeypatch, mgr: SessionManager
):
    """A failed Markdown edit must not fall back after the topic rebinds."""
    user_id = 991005
    telegram_thread_id = 14
    chat_id = -100014
    tool_use_id = "tool-A"
    mgr.bind_topic_to_codex_thread(
        user_id=user_id,
        thread_id=telegram_thread_id,
        chat_id=chat_id,
        codex_thread_id="codex-A",
        window_id="@1",
        cwd="/tmp/demo",
        display_name="demo",
    )
    mgr.set_group_chat_id(user_id, telegram_thread_id, chat_id)
    monkeypatch.setattr(mq, "session_manager", mgr)

    markdown_started = asyncio.Event()
    release_markdown = asyncio.Event()
    calls: list[str] = []
    sent: list[str] = []

    class _Bot:
        async def edit_message_text(self, **kwargs):
            if kwargs.get("parse_mode") == "MarkdownV2":
                calls.append("markdown-edit:start")
                markdown_started.set()
                await release_markdown.wait()
                calls.append("markdown-edit:raise")
                raise RuntimeError("markdown edit failed")
            calls.append("plain-edit")
            raise RuntimeError("plain fallback must not run")

    async def _send(_bot, _chat_id, text, **_kwargs):
        sent.append(text)
        calls.append("send")
        return SimpleNamespace(message_id=len(sent))

    monkeypatch.setattr(mq, "send_with_fallback", _send)
    mq._tool_msg_ids[(tool_use_id, user_id, chat_id, telegram_thread_id)] = 7003

    bot_instance = _Bot()
    try:
        await mq.enqueue_content_message(
            bot_instance,
            user_id,
            "@1",
            ["result from A"],
            tool_use_id=tool_use_id,
            content_type="tool_result",
            text="result from A",
            thread_id=telegram_thread_id,
            chat_id=chat_id,
        )
        await asyncio.wait_for(markdown_started.wait(), timeout=1)

        mgr.bind_topic_to_codex_thread(
            user_id=user_id,
            thread_id=telegram_thread_id,
            chat_id=chat_id,
            codex_thread_id="codex-B",
            window_id="@1",
            cwd="/tmp/demo",
            display_name="demo",
        )
        release_markdown.set()
        queue = mq.get_message_queue(user_id)
        assert queue is not None
        await asyncio.wait_for(queue.join(), timeout=1)

        assert calls == ["markdown-edit:start", "markdown-edit:raise"]
        assert sent == []
    finally:
        release_markdown.set()
        worker = mq._queue_workers.pop(user_id, None)
        if worker is not None:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        mq._message_queues.pop(user_id, None)
        mq._queue_locks.pop(user_id, None)
        mq._active_delivery_topics.pop(user_id, None)
        mq._queued_delivery_topic_counts.pop(user_id, None)
        mq._topic_delivery_generations.pop((user_id, chat_id, telegram_thread_id), None)
        mq._tool_msg_ids.pop((tool_use_id, user_id, chat_id, telegram_thread_id), None)


@pytest.mark.asyncio
async def test_content_delivery_stops_fallback_send_after_awaited_edit_rebind(
    monkeypatch, mgr: SessionManager
):
    """A failed awaited status edit must not fall through to sending stale A content."""
    user_id = 991004
    telegram_thread_id = 13
    chat_id = -100013
    mgr.bind_topic_to_codex_thread(
        user_id=user_id,
        thread_id=telegram_thread_id,
        chat_id=chat_id,
        codex_thread_id="codex-A",
        window_id="@1",
        cwd="/tmp/demo",
        display_name="demo",
    )
    mgr.set_group_chat_id(user_id, telegram_thread_id, chat_id)
    monkeypatch.setattr(mq, "session_manager", mgr)

    queue: asyncio.Queue[mq.MessageTask] = asyncio.Queue()
    mq._message_queues[user_id] = queue
    mq._queue_locks[user_id] = asyncio.Lock()
    edit_started = asyncio.Event()
    release_edit = asyncio.Event()
    sent: list[str] = []

    async def _failed_status_edit(*_args, **_kwargs):
        edit_started.set()
        await release_edit.wait()
        return None

    async def _send(_bot, _chat_id, text, **_kwargs):
        sent.append(text)
        return SimpleNamespace(message_id=len(sent))

    monkeypatch.setattr(mq, "_convert_status_to_content", _failed_status_edit)
    monkeypatch.setattr(mq, "send_with_fallback", _send)

    task = mq.MessageTask(
        task_type="content",
        window_id="@1",
        parts=["late output from A"],
        content_type="text",
        thread_id=telegram_thread_id,
        chat_id=chat_id,
        topic_ownership=mq.capture_topic_ownership(
            user_id,
            telegram_thread_id,
            chat_id,
        ),
    )
    mq._put_queued_task(user_id, queue, task)
    worker = asyncio.create_task(mq._message_queue_worker(object(), user_id))

    try:
        await asyncio.wait_for(edit_started.wait(), timeout=1)
        mgr.bind_topic_to_codex_thread(
            user_id=user_id,
            thread_id=telegram_thread_id,
            chat_id=chat_id,
            codex_thread_id="codex-B",
            window_id="@1",
            cwd="/tmp/demo",
            display_name="demo",
        )
        release_edit.set()
        await asyncio.wait_for(queue.join(), timeout=1)

        assert sent == []
    finally:
        release_edit.set()
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        mq._message_queues.pop(user_id, None)
        mq._queue_locks.pop(user_id, None)
        mq._active_delivery_topics.pop(user_id, None)
        mq._queued_delivery_topic_counts.pop(user_id, None)
        mq._topic_delivery_generations.pop((user_id, chat_id, telegram_thread_id), None)


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
