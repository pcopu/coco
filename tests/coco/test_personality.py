"""Tests for daily personality research and digest generation."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

import coco.handlers.personality as personality


@pytest.fixture(autouse=True)
def _isolated_personality_state(tmp_path, monkeypatch):
    monkeypatch.setattr(
        personality,
        "_PERSONALITY_STATE_FILE",
        tmp_path / "personality_state.json",
    )
    monkeypatch.setenv("COCO_PERSONALITY_RESEARCH_BACKEND", "heuristic")
    personality.reset_personality_state_for_tests()
    yield
    personality.reset_personality_state_for_tests()


def _write_memory_entries(path, entries: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry))
            handle.write("\n")


def test_generate_digest_summarizes_yesterday_sessions(tmp_path, monkeypatch):
    memory_path = tmp_path / "TELEGRAM_CHAT_MEMORY.jsonl"
    _write_memory_entries(
        memory_path,
        [
            {
                "ts_utc": "2026-03-18T08:00:00+00:00",
                "direction": "in",
                "chat_id": -100321,
                "thread_id": 77,
                "from_user_id": 12345,
                "text": "Instagram login is still broken and annoying",
            },
            {
                "ts_utc": "2026-03-18T08:02:00+00:00",
                "direction": "out_send",
                "chat_id": -100321,
                "thread_id": 77,
                "text": "I’m checking the login flow and verifying the saved session.",
            },
            {
                "ts_utc": "2026-03-18T08:05:00+00:00",
                "direction": "out_send",
                "chat_id": -100321,
                "thread_id": 77,
                "text": "The login worked and the session is usable now.",
            },
            {
                "ts_utc": "2026-03-18T12:45:00+00:00",
                "direction": "in",
                "chat_id": -100321,
                "thread_id": 77,
                "from_user_id": 12345,
                "text": "Love how fast the VNC handoff was today",
            },
            {
                "ts_utc": "2026-03-18T12:47:00+00:00",
                "direction": "out_send",
                "chat_id": -100321,
                "thread_id": 77,
                "text": "Fresh VNC is up on port 5901.",
            },
        ],
    )
    monkeypatch.setenv("COCO_TELEGRAM_MEMORY_LOG_PATH", str(memory_path))

    auth_meta = tmp_path / "allowed_users_meta.json"
    auth_meta.write_text(
        json.dumps(
            {
                "names": {"12345": "Morgan"},
                "admins": [12345],
                "scopes": {"12345": "create_sessions"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(personality.config, "auth_meta_file", auth_meta)

    digest = personality.generate_personality_digest(
        user_id=12345,
        chat_id=-100321,
        thread_id=77,
        target_date="2026-03-18",
    )

    assert digest is not None
    assert digest.session_count == 2
    assert digest.success_count >= 1
    assert digest.failure_count >= 1
    assert "Morgan" in digest.message_text
    assert "Instagram" in digest.message_text
    assert "VNC" in digest.message_text


def test_claim_due_delivery_only_sends_once_after_nine(tmp_path, monkeypatch):
    memory_path = tmp_path / "TELEGRAM_CHAT_MEMORY.jsonl"
    _write_memory_entries(
        memory_path,
        [
            {
                "ts_utc": "2026-03-18T18:00:00+00:00",
                "direction": "in",
                "chat_id": -100321,
                "thread_id": 77,
                "from_user_id": 12345,
                "text": "Please fix the broken login again",
            },
            {
                "ts_utc": "2026-03-18T18:05:00+00:00",
                "direction": "out_send",
                "chat_id": -100321,
                "thread_id": 77,
                "text": "I found the issue and fixed the login selectors.",
            },
        ],
    )
    monkeypatch.setenv("COCO_TELEGRAM_MEMORY_LOG_PATH", str(memory_path))

    now = datetime(2026, 3, 19, 9, 5, tzinfo=UTC)
    first = personality.claim_due_personality_delivery(
        user_id=12345,
        chat_id=-100321,
        thread_id=77,
        now=now,
    )
    second = personality.claim_due_personality_delivery(
        user_id=12345,
        chat_id=-100321,
        thread_id=77,
        now=now,
    )

    assert first is not None
    assert second is None


def test_generate_digest_prefers_external_research_when_configured(tmp_path, monkeypatch):
    memory_path = tmp_path / "TELEGRAM_CHAT_MEMORY.jsonl"
    _write_memory_entries(
        memory_path,
        [
            {
                "ts_utc": "2026-03-18T18:00:00+00:00",
                "direction": "in",
                "chat_id": -100321,
                "thread_id": 77,
                "from_user_id": 12345,
                "text": "Please fix the broken Instagram login again",
            },
            {
                "ts_utc": "2026-03-18T18:05:00+00:00",
                "direction": "out_send",
                "chat_id": -100321,
                "thread_id": 77,
                "text": "I found the issue and fixed the login selectors.",
            },
        ],
    )
    monkeypatch.setenv("COCO_TELEGRAM_MEMORY_LOG_PATH", str(memory_path))
    monkeypatch.setenv("COCO_PERSONALITY_RESEARCH_BACKEND", "external")

    def _fake_external(**_kwargs):
        return {
            "message_text": "Hey Morgan, external research says you hate flaky Instagram logins.",
            "session_count": 1,
            "success_count": 1,
            "failure_count": 1,
            "focus_terms": ["Instagram"],
            "positive_terms": [],
            "negative_terms": ["Instagram"],
        }

    monkeypatch.setattr(
        personality,
        "_run_external_personality_research",
        _fake_external,
    )

    digest = personality.generate_personality_digest(
        user_id=12345,
        chat_id=-100321,
        thread_id=77,
        target_date="2026-03-18",
    )

    assert digest is not None
    assert digest.message_text == (
        "Hey Morgan, external research says you hate flaky Instagram logins."
    )
