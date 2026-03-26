"""Tests for autoresearch external backend wiring."""

from __future__ import annotations

import json

import pytest

import coco.handlers.autoresearch as autoresearch


@pytest.fixture(autouse=True)
def _isolated_autoresearch_state(tmp_path, monkeypatch):
    monkeypatch.setattr(
        autoresearch,
        "_AUTORESEARCH_STATE_FILE",
        tmp_path / "autoresearch_state.json",
    )
    autoresearch.reset_autoresearch_state_for_tests()
    yield
    autoresearch.reset_autoresearch_state_for_tests()


def _write_memory_entries(path, entries: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry))
            handle.write("\n")


def test_generate_digest_prefers_external_research_when_configured(tmp_path, monkeypatch):
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
                "ts_utc": "2026-03-18T08:12:00+00:00",
                "direction": "out_send",
                "chat_id": -100321,
                "thread_id": 77,
                "text": "I drafted a tighter follow-up and queued it for review.",
            },
        ],
    )
    monkeypatch.setenv("COCO_TELEGRAM_MEMORY_LOG_PATH", str(memory_path))
    monkeypatch.setenv("COCO_AUTORESEARCH_RESEARCH_BACKEND", "external")

    def _fake_external(**kwargs):
        assert kwargs["app_slug"] == "autoresearch"
        assert kwargs["bundle_payload"]["outcome"] == "Close more inbound leads"
        return {
            "message_text": "Hey Morgan, yesterday Coco helped you push inbound leads forward with tighter follow-up drafts.",
            "session_count": 1,
            "success_count": 1,
            "failure_count": 0,
            "focus_terms": ["follow-up", "sales"],
            "positive_terms": ["follow-up"],
            "negative_terms": [],
        }

    monkeypatch.setattr(autoresearch.research_backend, "run_external_research", _fake_external)

    digest = autoresearch.generate_autoresearch_digest(
        user_id=12345,
        chat_id=-100321,
        thread_id=77,
        target_date="2026-03-18",
        outcome="Close more inbound leads",
    )

    assert digest is not None
    assert digest.message_text == (
        "Hey Morgan, yesterday Coco helped you push inbound leads forward with tighter follow-up drafts."
    )
