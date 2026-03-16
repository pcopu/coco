"""Tests for coco.utils: coco_dir, atomic_write_json, read_cwd_from_jsonl."""

import json
from pathlib import Path

import pytest

from coco.utils import atomic_write_json, coco_dir, read_cwd_from_jsonl


class TestCocoDir:
    def test_uses_coco_dir_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("COCO_DIR", "/custom/coco")
        assert coco_dir() == Path("/custom/coco")

    def test_returns_coco_dir_when_env_is_set(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("COCO_DIR", "/custom/coco")
        assert coco_dir() == Path("/custom/coco")

    def test_returns_default_without_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        monkeypatch.delenv("COCO_DIR", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert coco_dir() == tmp_path / ".coco"

    def test_prefers_existing_coco_dir(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        monkeypatch.delenv("COCO_DIR", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        new_dir = tmp_path / ".coco"
        new_dir.mkdir()

        assert coco_dir() == new_dir


class TestAtomicWriteJson:
    def test_writes_valid_json(self, tmp_path: Path):
        target = tmp_path / "data.json"
        atomic_write_json(target, {"key": "value"})
        result = json.loads(target.read_text(encoding="utf-8"))
        assert result == {"key": "value"}

    def test_creates_parent_directories(self, tmp_path: Path):
        target = tmp_path / "a" / "b" / "c" / "data.json"
        atomic_write_json(target, [1, 2, 3])
        assert target.exists()
        assert json.loads(target.read_text(encoding="utf-8")) == [1, 2, 3]

    def test_round_trip(self, tmp_path: Path):
        data = {"users": [{"id": 1, "name": "alice"}, {"id": 2, "name": "bob"}]}
        target = tmp_path / "round_trip.json"
        atomic_write_json(target, data)
        assert json.loads(target.read_text(encoding="utf-8")) == data

    def test_no_temp_files_left_on_success(self, tmp_path: Path):
        target = tmp_path / "clean.json"
        atomic_write_json(target, {"ok": True})
        remaining = list(tmp_path.glob(".*tmp*"))
        assert remaining == []


class TestReadCwdFromJsonl:
    def test_cwd_in_first_entry(self, tmp_path: Path):
        f = tmp_path / "session.jsonl"
        f.write_text(json.dumps({"cwd": "/home/user/project"}) + "\n")
        assert read_cwd_from_jsonl(f) == "/home/user/project"

    def test_cwd_in_second_entry(self, tmp_path: Path):
        f = tmp_path / "session.jsonl"
        lines = [
            json.dumps({"type": "init"}),
            json.dumps({"cwd": "/found/here"}),
        ]
        f.write_text("\n".join(lines) + "\n")
        assert read_cwd_from_jsonl(f) == "/found/here"

    def test_no_cwd_returns_empty(self, tmp_path: Path):
        f = tmp_path / "session.jsonl"
        lines = [
            json.dumps({"type": "init"}),
            json.dumps({"type": "message", "text": "hello"}),
        ]
        f.write_text("\n".join(lines) + "\n")
        assert read_cwd_from_jsonl(f) == ""

    def test_missing_file_returns_empty(self, tmp_path: Path):
        assert read_cwd_from_jsonl(tmp_path / "nonexistent.jsonl") == ""
