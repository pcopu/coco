"""Integration tests for MonitorState — real file I/O with tmp_path."""

import json
from pathlib import Path

import pytest

from coco.monitor_state import MonitorState, TrackedSession

pytestmark = pytest.mark.integration


class TestMonitorStateIntegration:
    def test_save_load_round_trip(self, tmp_path):
        state_file = tmp_path / "state.json"
        session = TrackedSession(
            session_id="ses-001",
            file_path="/tmp/test.jsonl",
            last_byte_offset=1024,
        )
        state = MonitorState(state_file=state_file)
        state.update_session(session)
        state.save()

        loaded = MonitorState(state_file=state_file)
        loaded.load()
        result = loaded.get_session("ses-001")
        assert result is not None
        assert result.session_id == "ses-001"
        assert result.file_path == "/tmp/test.jsonl"
        assert result.last_byte_offset == 1024

    def test_corrupt_file_recovery(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text("{{{not json at all!!!")
        state = MonitorState(state_file=state_file)
        state.load()
        assert state.tracked_sessions == {}

    @pytest.mark.parametrize(
        "payload",
        [[], {"tracked_sessions": []}, {"tracked_sessions": {"bad": None}}],
    )
    def test_structurally_invalid_state_recovers(self, tmp_path, payload):
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps(payload), encoding="utf-8")

        state = MonitorState(state_file=state_file)
        state.load()

        assert state.tracked_sessions == {}

    def test_tracked_session_fields_are_normalized(self):
        tracked = TrackedSession.from_dict(
            {
                "session_id": None,
                "file_path": 7,
                "last_byte_offset": "bad",
            }
        )

        assert tracked.session_id == ""
        assert tracked.file_path == ""
        assert tracked.last_byte_offset == 0

    def test_state_read_error_recovers(self, monkeypatch, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text("{}", encoding="utf-8")
        original_read_text = Path.read_text

        def _read_text(path, *args, **kwargs):
            if path == state_file:
                raise OSError("disk failed")
            return original_read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _read_text)
        state = MonitorState(state_file=state_file)

        state.load()

        assert state.tracked_sessions == {}

    def test_dirty_tracking_with_save_if_dirty(self, tmp_path):
        state_file = tmp_path / "state.json"
        state = MonitorState(state_file=state_file)
        state.save_if_dirty()
        assert not state_file.exists()

        state.update_session(
            TrackedSession(session_id="ses-dirty", file_path="/tmp/d.jsonl")
        )
        state.save_if_dirty()
        assert state_file.exists()

    def test_remove_session_and_save(self, tmp_path):
        state_file = tmp_path / "state.json"
        state = MonitorState(state_file=state_file)
        state.update_session(
            TrackedSession(session_id="keep", file_path="/tmp/keep.jsonl")
        )
        state.update_session(
            TrackedSession(session_id="drop", file_path="/tmp/drop.jsonl")
        )
        state.save()

        state.remove_session("drop")
        state.save()

        reloaded = MonitorState(state_file=state_file)
        reloaded.load()
        assert reloaded.get_session("keep") is not None
        assert reloaded.get_session("drop") is None
        assert len(reloaded.tracked_sessions) == 1
