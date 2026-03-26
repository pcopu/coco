"""Tests for generic auto research app state and daily digest generation."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

import coco.handlers.autoresearch as autoresearch


@pytest.fixture(autouse=True)
def _isolated_autoresearch_state(tmp_path, monkeypatch):
    monkeypatch.setattr(
        autoresearch,
        "_AUTORESEARCH_STATE_FILE",
        tmp_path / "autoresearch_state.json",
    )
    monkeypatch.setenv("COCO_AUTORESEARCH_RESEARCH_BACKEND", "heuristic")
    autoresearch.reset_autoresearch_state_for_tests()
    yield
    autoresearch.reset_autoresearch_state_for_tests()


def _write_memory_entries(path, entries: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry))
            handle.write("\n")


def test_set_outcome_and_generate_digest(tmp_path, monkeypatch):
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
                "text": "I want Coco to help me close more inbound leads",
            },
            {
                "ts_utc": "2026-03-18T08:10:00+00:00",
                "direction": "in",
                "chat_id": -100321,
                "thread_id": 77,
                "from_user_id": 12345,
                "text": "This reply draft was great for sales follow-up",
            },
            {
                "ts_utc": "2026-03-18T08:12:00+00:00",
                "direction": "out_send",
                "chat_id": -100321,
                "thread_id": 77,
                "text": "I drafted a tighter follow-up and queued it for review.",
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
    monkeypatch.setattr(autoresearch.config, "auth_meta_file", auth_meta)

    state = autoresearch.set_autoresearch_outcome(
        user_id=12345,
        thread_id=77,
        outcome="Close more inbound leads",
    )
    digest = autoresearch.generate_autoresearch_digest(
        user_id=12345,
        chat_id=-100321,
        thread_id=77,
        target_date="2026-03-18",
        outcome=state.outcome,
    )

    assert digest is not None
    assert digest.outcome == "Close more inbound leads"
    assert "Close more inbound leads" in digest.message_text
    assert "Morgan" in digest.message_text
    assert "sales" in digest.message_text.lower() or "follow-up" in digest.message_text.lower()


def test_claim_due_delivery_requires_outcome(tmp_path, monkeypatch):
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
                "text": "Please help me make my follow-ups more consistent",
            },
        ],
    )
    monkeypatch.setenv("COCO_TELEGRAM_MEMORY_LOG_PATH", str(memory_path))

    assert (
        autoresearch.claim_due_autoresearch_delivery(
            user_id=12345,
            chat_id=-100321,
            thread_id=77,
            now=datetime(2026, 3, 19, 9, 5, tzinfo=UTC),
        )
        is None
    )
