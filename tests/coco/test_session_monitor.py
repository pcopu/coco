"""Unit tests for SessionMonitor JSONL reading and offset handling."""

import json
from types import SimpleNamespace

import pytest

import coco.session as session_module
from coco.monitor_state import TrackedSession
from coco.session_monitor import SessionMonitor


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
