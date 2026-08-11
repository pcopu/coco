"""Unit tests for SessionMonitor JSONL reading and offset handling."""

import json
from types import SimpleNamespace

import pytest

import coco.session as session_module
import coco.session_monitor as session_monitor_module
from coco.monitor_state import TrackedSession
from coco.session_monitor import SessionInfo, SessionMonitor


class TestReadNewLinesOffsetRecovery:
    """Tests for _read_new_lines offset corruption recovery."""

    @pytest.fixture
    def monitor(self, tmp_path):
        """Create a SessionMonitor with temp state file."""
        return SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

    @pytest.mark.asyncio
    async def test_mid_line_offset_recovery(self, monitor, tmp_path, make_jsonl_entry):
        """Recover from corrupted offset pointing mid-line."""
        # Create JSONL file with two valid lines
        jsonl_file = tmp_path / "session.jsonl"
        entry1 = make_jsonl_entry(msg_type="assistant", content="first message")
        entry2 = make_jsonl_entry(msg_type="assistant", content="second message")
        jsonl_file.write_text(
            json.dumps(entry1) + "\n" + json.dumps(entry2) + "\n",
            encoding="utf-8",
        )

        # Calculate offset pointing into the middle of line 1
        line1_bytes = len(json.dumps(entry1).encode("utf-8")) // 2
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=line1_bytes,  # Mid-line (corrupted)
        )

        # Read should recover and return empty (offset moved to next line)
        result = await monitor._read_new_lines(session, jsonl_file)

        # Should return empty list (recovery skips to next line, no new content yet)
        assert result == []

        # Offset should now point to start of line 2
        line1_full = len(json.dumps(entry1).encode("utf-8")) + 1  # +1 for newline
        assert session.last_byte_offset == line1_full

    @pytest.mark.asyncio
    async def test_valid_offset_reads_normally(
        self, monitor, tmp_path, make_jsonl_entry
    ):
        """Normal reading when offset points to line start."""
        jsonl_file = tmp_path / "session.jsonl"
        entry1 = make_jsonl_entry(msg_type="assistant", content="first")
        entry2 = make_jsonl_entry(msg_type="assistant", content="second")
        jsonl_file.write_text(
            json.dumps(entry1) + "\n" + json.dumps(entry2) + "\n",
            encoding="utf-8",
        )

        # Offset at 0 should read both lines
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=0,
        )

        result = await monitor._read_new_lines(session, jsonl_file)

        assert len(result) == 2
        assert session.last_byte_offset == jsonl_file.stat().st_size

    @pytest.mark.asyncio
    async def test_truncation_detection(self, monitor, tmp_path, make_jsonl_entry):
        """Detect file truncation and reset offset."""
        jsonl_file = tmp_path / "session.jsonl"
        entry = make_jsonl_entry(msg_type="assistant", content="content")
        jsonl_file.write_text(json.dumps(entry) + "\n", encoding="utf-8")

        # Set offset beyond file size (simulates truncation)
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=9999,  # Beyond file size
        )

        result = await monitor._read_new_lines(session, jsonl_file)

        # Should reset offset to 0 and read the line
        assert session.last_byte_offset == jsonl_file.stat().st_size
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_valid_non_object_record_does_not_block_later_entries(
        self, monitor, tmp_path, make_jsonl_entry
    ):
        jsonl_file = tmp_path / "session.jsonl"
        entry = make_jsonl_entry(msg_type="assistant", content="after invalid shape")
        jsonl_file.write_text(
            "7\n[1]\n" + json.dumps(entry) + "\n",
            encoding="utf-8",
        )
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=0,
        )

        result = await monitor._read_new_lines(session, jsonl_file)

        assert result == [entry]
        assert session.last_byte_offset == jsonl_file.stat().st_size

    @pytest.mark.asyncio
    async def test_backlog_is_drained_in_bounded_batches_without_duplicates(
        self,
        monitor,
        monkeypatch,
        tmp_path,
        make_jsonl_entry,
    ):
        jsonl_file = tmp_path / "session.jsonl"
        entries = [
            make_jsonl_entry(msg_type="assistant", content=f"message-{index}")
            for index in range(3)
        ]
        encoded_lines = [
            (json.dumps(entry) + "\n").encode("utf-8")
            for entry in entries
        ]
        jsonl_file.write_bytes(b"".join(encoded_lines))
        monkeypatch.setattr(
            session_monitor_module,
            "SESSION_MONITOR_MAX_BATCH_BYTES",
            len(encoded_lines[0]),
            raising=False,
        )
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=0,
        )

        batches: list[list[dict]] = []
        while session.last_byte_offset < jsonl_file.stat().st_size:
            previous_offset = session.last_byte_offset
            batch = await monitor._read_new_lines(session, jsonl_file)
            assert session.last_byte_offset > previous_offset
            batches.append(batch)

        assert [len(batch) for batch in batches] == [1, 1, 1]
        assert [entry for batch in batches for entry in batch] == entries

    @pytest.mark.asyncio
    async def test_oversized_complete_record_is_skipped_before_json_parse(
        self,
        monitor,
        monkeypatch,
        tmp_path,
        make_jsonl_entry,
    ):
        jsonl_file = tmp_path / "session.jsonl"
        max_bytes = 128
        entry = make_jsonl_entry(msg_type="assistant", content="x" * 512)
        encoded_record = (json.dumps(entry) + "\n").encode("utf-8")
        assert len(encoded_record) > max_bytes
        jsonl_file.write_bytes(encoded_record)
        monkeypatch.setattr(
            session_monitor_module,
            "SESSION_MONITOR_MAX_BATCH_BYTES",
            max_bytes,
        )
        monkeypatch.setattr(
            session_monitor_module.TranscriptParser,
            "parse_line",
            lambda _line: pytest.fail("oversized record reached the JSON parser"),
        )
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=0,
        )

        drain_offsets: list[int] = []
        while session.last_byte_offset < len(encoded_record):
            result = await monitor._read_new_lines(session, jsonl_file)
            assert result == []
            drain_offsets.append(
                session.pending_record_drain_offset
                if session.pending_record_drain_offset is not None
                else session.last_byte_offset
            )

        assert drain_offsets
        assert all(
            current - previous <= max_bytes
            for previous, current in zip([0, *drain_offsets], drain_offsets)
        )
        assert session.last_byte_offset == len(encoded_record)
        assert session.pending_record_start_offset is None
        assert session.pending_record_drain_offset is None

    @pytest.mark.asyncio
    async def test_oversized_partial_record_keeps_last_safe_offset(
        self,
        monitor,
        monkeypatch,
        tmp_path,
    ):
        jsonl_file = tmp_path / "session.jsonl"
        max_bytes = 128
        partial_record = b'{"type":"assistant","payload":"' + (b"x" * 512)
        jsonl_file.write_bytes(partial_record)
        monkeypatch.setattr(
            session_monitor_module,
            "SESSION_MONITOR_MAX_BATCH_BYTES",
            max_bytes,
        )
        monkeypatch.setattr(
            session_monitor_module.TranscriptParser,
            "parse_line",
            lambda _line: pytest.fail("oversized partial record reached the JSON parser"),
        )
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=0,
        )

        result = await monitor._read_new_lines(session, jsonl_file)

        assert result == []
        assert session.last_byte_offset == 0
        assert session.pending_record_start_offset == 0
        assert session.pending_record_drain_offset == max_bytes

        result = await monitor._read_new_lines(session, jsonl_file)

        assert result == []
        assert session.last_byte_offset == 0
        assert session.pending_record_start_offset == 0
        assert session.pending_record_drain_offset == max_bytes * 2


def test_resolve_codex_session_files_prefers_tracked_file_path(tmp_path):
    monitor = SessionMonitor(
        projects_path=tmp_path / "projects",
        state_file=tmp_path / "monitor_state.json",
    )
    jsonl_file = tmp_path / "session.jsonl"
    jsonl_file.write_text("{}\n", encoding="utf-8")
    monitor.state.update_session(
        TrackedSession(
            session_id="session-1",
            file_path=str(jsonl_file),
            last_byte_offset=0,
        )
    )

    class _NoGlobProjects:
        def exists(self) -> bool:
            return True

        def glob(self, _pattern: str):
            raise AssertionError("tracked session resolution should not scan")

    monitor.projects_path = _NoGlobProjects()  # type: ignore[assignment]

    sessions = monitor._resolve_codex_session_files_sync({"session-1"})

    assert sessions == [SessionInfo(session_id="session-1", file_path=jsonl_file)]


@pytest.mark.asyncio
async def test_check_for_updates_updates_tracked_path_after_fallback_resolution(tmp_path):
    session_id = "12345678-1234-1234-1234-123456789abc"
    projects_path = tmp_path / "projects"
    projects_path.mkdir()
    stale_path = tmp_path / "missing" / f"old-{session_id}.jsonl"
    resolved_path = projects_path / f"new-{session_id}.jsonl"
    resolved_path.write_text("", encoding="utf-8")

    monitor = SessionMonitor(
        projects_path=projects_path,
        state_file=tmp_path / "monitor_state.json",
    )
    monitor.state.update_session(
        TrackedSession(
            session_id=session_id,
            file_path=str(stale_path),
            last_byte_offset=0,
        )
    )

    assert await monitor.check_for_updates({session_id}) == []

    tracked = monitor.state.get_session(session_id)
    assert tracked is not None
    assert tracked.file_path == str(resolved_path)


@pytest.mark.asyncio
async def test_check_for_updates_reads_resolved_path_after_stale_mtime_cache(tmp_path):
    session_id = "12345678-1234-1234-1234-123456789abd"
    projects_path = tmp_path / "projects"
    projects_path.mkdir()
    stale_path = tmp_path / "missing" / f"old-{session_id}.jsonl"
    resolved_path = projects_path / f"new-{session_id}.jsonl"
    resolved_path.write_text("", encoding="utf-8")

    monitor = SessionMonitor(
        projects_path=projects_path,
        state_file=tmp_path / "monitor_state.json",
    )
    monitor.state.update_session(
        TrackedSession(
            session_id=session_id,
            file_path=str(stale_path),
            last_byte_offset=50,
        )
    )
    monitor._file_mtimes[session_id] = 9_999_999_999.0

    assert await monitor.check_for_updates({session_id}) == []

    tracked = monitor.state.get_session(session_id)
    assert tracked is not None
    assert tracked.file_path == str(resolved_path)
    assert tracked.last_byte_offset == 0


@pytest.mark.asyncio
async def test_monitor_loop_retries_startup_autodiscover_failure(monkeypatch, tmp_path):
    monitor = SessionMonitor(
        projects_path=tmp_path / "projects",
        state_file=tmp_path / "monitor_state.json",
        poll_interval=0.01,
    )
    monitor._running = True

    events: list[str] = []
    autodiscover_calls = 0

    async def _fake_autodiscover_sessions_for_bound_windows():
        nonlocal autodiscover_calls
        autodiscover_calls += 1
        events.append(f"autodiscover:{autodiscover_calls}")
        if autodiscover_calls == 1:
            raise RuntimeError("startup autodiscover failed")

    async def _fake_cleanup_all_stale_sessions():
        events.append("cleanup")

    async def _fake_load_current_session_map():
        events.append("load_map")
        return {}

    async def _fake_detect_and_cleanup_changes():
        events.append("detect")
        return {}

    async def _fake_check_for_updates(active_session_ids):
        assert active_session_ids == set()
        events.append("check_updates")
        return []

    sleep_calls = 0
    monotonic_values = iter([100.0, 100.5, 101.0])
    last_monotonic = 101.0

    async def _fake_sleep(_seconds: float):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 3:
            monitor._running = False
        return None

    monkeypatch.setattr(
        session_module,
        "session_manager",
        SimpleNamespace(
            autodiscover_sessions_for_bound_windows=(
                _fake_autodiscover_sessions_for_bound_windows
            )
        ),
    )
    monkeypatch.setattr(monitor, "_cleanup_all_stale_sessions", _fake_cleanup_all_stale_sessions)
    monkeypatch.setattr(monitor, "_load_current_session_map", _fake_load_current_session_map)
    monkeypatch.setattr(monitor, "_detect_and_cleanup_changes", _fake_detect_and_cleanup_changes)
    monkeypatch.setattr(monitor, "check_for_updates", _fake_check_for_updates)
    monkeypatch.setattr("coco.session_monitor.asyncio.sleep", _fake_sleep)
    monkeypatch.setattr(
        "coco.session_monitor.time.monotonic",
        lambda: next(monotonic_values, last_monotonic),
    )

    await monitor._monitor_loop()

    assert events == [
        "autodiscover:1",
        "detect",
        "check_updates",
        "detect",
        "check_updates",
        "autodiscover:2",
        "cleanup",
        "load_map",
        "detect",
        "check_updates",
    ]
