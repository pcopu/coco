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


def test_run_now_generates_digest_and_marks_delivery(tmp_path, monkeypatch):
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
                "text": "Please help me close more inbound leads",
            },
            {
                "ts_utc": "2026-03-18T18:05:00+00:00",
                "direction": "out_send",
                "chat_id": -100321,
                "thread_id": 77,
                "text": "I tightened the follow-up email and highlighted the next step.",
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

    autoresearch.set_autoresearch_outcome(
        user_id=12345,
        chat_id=-100321,
        thread_id=77,
        outcome="Close more inbound leads",
    )
    runner = getattr(autoresearch, "run_autoresearch_now", None)
    assert runner is not None

    digest_text = runner(
        user_id=12345,
        chat_id=-100321,
        thread_id=77,
        now=datetime(2026, 3, 19, 8, 0, tzinfo=UTC),
    )

    state = autoresearch.get_autoresearch_state(
        user_id=12345,
        chat_id=-100321,
        thread_id=77,
    )
    assert digest_text is not None
    assert "Close more inbound leads" in digest_text
    assert state is not None
    assert state.last_researched_for_date == "2026-03-18"
    assert state.last_delivered_for_date == "2026-03-18"


def test_same_user_thread_in_two_chats_can_each_claim_delivery(tmp_path, monkeypatch):
    memory_path = tmp_path / "TELEGRAM_CHAT_MEMORY.jsonl"
    _write_memory_entries(
        memory_path,
        [
            {
                "ts_utc": "2026-03-18T18:00:00+00:00",
                "direction": "in",
                "chat_id": -100501,
                "thread_id": 77,
                "from_user_id": 12345,
                "text": "Help me close more leads",
            },
            {
                "ts_utc": "2026-03-18T18:00:00+00:00",
                "direction": "in",
                "chat_id": -100502,
                "thread_id": 77,
                "from_user_id": 12345,
                "text": "Help me ship more releases",
            },
        ],
    )
    monkeypatch.setenv("COCO_TELEGRAM_MEMORY_LOG_PATH", str(memory_path))
    monkeypatch.setattr(autoresearch._personality, "_local_timezone", lambda _now=None: UTC)

    autoresearch.set_autoresearch_outcome(
        user_id=12345,
        chat_id=-100501,
        thread_id=77,
        outcome="Close more leads",
    )
    autoresearch.set_autoresearch_outcome(
        user_id=12345,
        chat_id=-100502,
        thread_id=77,
        outcome="Ship more releases",
    )

    first = autoresearch.claim_due_autoresearch_delivery(
        user_id=12345,
        chat_id=-100501,
        thread_id=77,
        now=datetime(2026, 3, 19, 9, 5, tzinfo=UTC),
    )
    second = autoresearch.claim_due_autoresearch_delivery(
        user_id=12345,
        chat_id=-100502,
        thread_id=77,
        now=datetime(2026, 3, 19, 9, 5, tzinfo=UTC),
    )

    assert first is not None and "Close more leads" in first
    assert second is not None and "Ship more releases" in second


def test_legacy_autoresearch_state_key_loads_into_chat_zero():
    autoresearch._AUTORESEARCH_STATE_FILE.write_text(
        json.dumps({"12345:77": {"outcome": "legacy outcome"}}),
        encoding="utf-8",
    )

    state = autoresearch.get_autoresearch_state(user_id=12345, thread_id=77)

    assert state is not None
    assert state.outcome == "legacy outcome"
    assert autoresearch.get_autoresearch_state(
        user_id=12345,
        chat_id=-100501,
        thread_id=77,
    ) is None


def test_pruning_keeps_active_chat_scope_only():
    autoresearch.set_autoresearch_outcome(
        user_id=12345,
        chat_id=-100501,
        thread_id=77,
        outcome="goal a",
    )
    autoresearch.set_autoresearch_outcome(
        user_id=12345,
        chat_id=-100502,
        thread_id=77,
        outcome="goal b",
    )

    autoresearch.prune_autoresearch_topics({(12345, -100501, 77)})

    assert autoresearch.get_autoresearch_state(
        user_id=12345,
        chat_id=-100501,
        thread_id=77,
    ) is not None
    assert autoresearch.get_autoresearch_state(
        user_id=12345,
        chat_id=-100502,
        thread_id=77,
    ) is None


def test_pruning_migrates_unique_legacy_state_to_active_chat():
    autoresearch._AUTORESEARCH_STATE_FILE.write_text(
        json.dumps({"12345:77": {"outcome": "legacy outcome"}}),
        encoding="utf-8",
    )

    autoresearch.prune_autoresearch_topics({(12345, -100501, 77)})

    state = autoresearch.get_autoresearch_state(
        user_id=12345,
        chat_id=-100501,
        thread_id=77,
    )
    assert state is not None
    assert state.outcome == "legacy outcome"
    assert autoresearch.get_autoresearch_state(user_id=12345, thread_id=77) is None
    persisted = json.loads(autoresearch._AUTORESEARCH_STATE_FILE.read_text(encoding="utf-8"))
    assert list(persisted) == ["12345:-100501:77"]


def test_pruning_discards_superseded_legacy_state_when_scoped_state_exists():
    autoresearch._AUTORESEARCH_STATE_FILE.write_text(
        json.dumps(
            {
                "12345:77": {"outcome": "legacy outcome"},
                "12345:-100501:77": {"outcome": "authoritative outcome"},
            }
        ),
        encoding="utf-8",
    )

    autoresearch.prune_autoresearch_topics({(12345, -100501, 77)})

    state = autoresearch.get_autoresearch_state(
        user_id=12345,
        chat_id=-100501,
        thread_id=77,
    )
    assert state is not None
    assert state.outcome == "authoritative outcome"
    assert autoresearch.get_autoresearch_state(user_id=12345, thread_id=77) is None
    persisted = json.loads(autoresearch._AUTORESEARCH_STATE_FILE.read_text(encoding="utf-8"))
    assert list(persisted) == ["12345:-100501:77"]

    autoresearch.clear_autoresearch_state(12345, thread_id=77, chat_id=-100501)
    autoresearch.prune_autoresearch_topics({(12345, -100501, 77)})
    assert autoresearch.get_autoresearch_state(user_id=12345, thread_id=77) is None


def test_pruning_retains_ambiguous_legacy_state_without_cross_claiming():
    autoresearch._AUTORESEARCH_STATE_FILE.write_text(
        json.dumps({"12345:77": {"outcome": "legacy outcome"}}),
        encoding="utf-8",
    )

    autoresearch.prune_autoresearch_topics({(12345, -100501, 77), (12345, -100502, 77)})

    state = autoresearch.get_autoresearch_state(user_id=12345, thread_id=77)
    assert state is not None
    assert state.outcome == "legacy outcome"
    assert autoresearch.get_autoresearch_state(
        user_id=12345,
        chat_id=-100501,
        thread_id=77,
    ) is None
    assert autoresearch.get_autoresearch_state(
        user_id=12345,
        chat_id=-100502,
        thread_id=77,
    ) is None
    persisted = json.loads(autoresearch._AUTORESEARCH_STATE_FILE.read_text(encoding="utf-8"))
    assert list(persisted) == ["12345:77"]
