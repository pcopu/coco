"""Tests for looper state and keyword completion behavior."""

import pytest

import coco.handlers.looper as looper


@pytest.fixture(autouse=True)
def _isolated_looper_state(tmp_path, monkeypatch):
    monkeypatch.setattr(looper, "_LOOPER_STATE_FILE", tmp_path / "looper_state.json")
    looper.reset_looper_state_for_tests()
    yield
    looper.reset_looper_state_for_tests()


def test_start_claim_prompt_and_keyword_stop():
    state = looper.start_looper(
        user_id=1,
        thread_id=99,
        window_id="@9",
        plan_path="plans/ship.md",
        keyword="DONE",
        interval_seconds=10 * 60,
        limit_seconds=60 * 60,
        instructions="Focus on tests first",
        now=0.0,
    )

    assert state.keyword == "done"
    assert state.interval_seconds == 600
    assert state.deadline_at == 3600.0

    assert (
        looper.claim_due_looper_prompt(
            user_id=1,
            thread_id=99,
            window_id="@9",
            now=599.0,
        )
        is None
    )

    due = looper.claim_due_looper_prompt(
        user_id=1,
        thread_id=99,
        window_id="@9",
        now=600.0,
    )
    assert due is not None
    assert "plans/ship.md" in due.prompt_text
    assert "done" in due.prompt_text
    assert "Focus on tests first" in due.prompt_text

    assert (
        looper.consume_looper_completion_keyword(
            user_id=1,
            thread_id=99,
            window_id="@9",
            assistant_text="still-working",
        )
        is None
    )

    stopped = looper.consume_looper_completion_keyword(
        user_id=1,
        thread_id=99,
        window_id="@9",
        assistant_text="done",
    )
    assert stopped is not None
    assert looper.get_looper_state(user_id=1, thread_id=99) is None


def test_time_limit_expiry_stops_loop():
    looper.start_looper(
        user_id=2,
        thread_id=77,
        window_id="@7",
        plan_path="docs/plan.md",
        keyword="ship",
        interval_seconds=120,
        limit_seconds=3600,
        instructions="",
        now=10.0,
    )

    assert (
        looper.stop_looper_if_expired(
            user_id=2,
            thread_id=77,
            window_id="@7",
            now=3599.0,
        )
        is None
    )

    expired = looper.stop_looper_if_expired(
        user_id=2,
        thread_id=77,
        window_id="@7",
        now=3700.0,
    )
    assert expired is not None
    assert expired.plan_path == "docs/plan.md"
    assert looper.get_looper_state(user_id=2, thread_id=77) is None


def test_random_interval_range_reschedules_with_sampled_values(monkeypatch):
    sampled: list[tuple[int, int]] = []
    values = iter([1500, 4500])

    def _randint(low: int, high: int) -> int:
        sampled.append((low, high))
        return next(values)

    monkeypatch.setattr(looper.random, "randint", _randint)

    state = looper.start_looper(
        user_id=3,
        thread_id=55,
        window_id="@5",
        plan_path="plans/random.md",
        keyword="done",
        interval_seconds=1500,
        interval_max_seconds=4500,
        now=100.0,
    )

    assert state.next_prompt_at == 1600.0
    due = looper.claim_due_looper_prompt(
        user_id=3,
        thread_id=55,
        window_id="@5",
        now=1600.0,
    )
    assert due is not None
    assert looper.get_looper_state(user_id=3, thread_id=55).next_prompt_at == 6100.0
    assert sampled == [(1500, 4500), (1500, 4500)]


def test_same_user_thread_in_two_chats_keeps_completion_state_isolated():
    looper.start_looper(
        user_id=4,
        chat_id=-100401,
        thread_id=88,
        window_id="@chat-a",
        plan_path="plans/a.md",
        keyword="done",
        now=100.0,
    )
    looper.start_looper(
        user_id=4,
        chat_id=-100402,
        thread_id=88,
        window_id="@chat-b",
        plan_path="plans/b.md",
        keyword="ship",
        now=100.0,
    )

    stopped = looper.consume_looper_completion_keyword(
        user_id=4,
        chat_id=-100401,
        thread_id=88,
        window_id="@chat-a",
        assistant_text="done",
    )

    assert stopped is not None
    assert looper.get_looper_state(user_id=4, chat_id=-100401, thread_id=88) is None
    remaining = looper.get_looper_state(user_id=4, chat_id=-100402, thread_id=88)
    assert remaining is not None
    assert remaining.keyword == "ship"


def test_legacy_looper_state_key_loads_into_chat_zero(tmp_path):
    payload = {
        "4:88": {
            "window_id": "@legacy",
            "plan_path": "plans/legacy.md",
            "keyword": "done",
            "instructions": "",
            "interval_seconds": 600,
            "started_at": 100.0,
            "next_prompt_at": 700.0,
        }
    }
    looper._LOOPER_STATE_FILE.write_text(__import__("json").dumps(payload), encoding="utf-8")

    state = looper.get_looper_state(user_id=4, thread_id=88)

    assert state is not None
    assert looper.get_looper_state(user_id=4, chat_id=1, thread_id=88) is None


def test_pruning_keeps_active_chat_scope_only():
    for chat_id, keyword in ((-100401, "done"), (-100402, "ship")):
        looper.start_looper(
            user_id=4,
            chat_id=chat_id,
            thread_id=88,
            window_id=f"@{chat_id}",
            plan_path="plans/plan.md",
            keyword=keyword,
            now=100.0,
        )

    looper.prune_looper_topics({(4, -100401, 88)})

    assert looper.get_looper_state(user_id=4, chat_id=-100401, thread_id=88) is not None
    assert looper.get_looper_state(user_id=4, chat_id=-100402, thread_id=88) is None


def test_pruning_migrates_unique_legacy_state_to_active_chat():
    payload = {
        "4:88": {
            "window_id": "@legacy",
            "plan_path": "plans/legacy.md",
            "keyword": "done",
            "instructions": "",
            "interval_seconds": 600,
            "started_at": 100.0,
            "next_prompt_at": 700.0,
        }
    }
    looper._LOOPER_STATE_FILE.write_text(__import__("json").dumps(payload), encoding="utf-8")

    looper.prune_looper_topics({(4, -100401, 88)})

    assert looper.get_looper_state(user_id=4, chat_id=-100401, thread_id=88) is not None
    assert looper.get_looper_state(user_id=4, thread_id=88) is None
    persisted = __import__("json").loads(looper._LOOPER_STATE_FILE.read_text(encoding="utf-8"))
    assert list(persisted) == ["4:-100401:88"]


def test_pruning_discards_superseded_legacy_state_when_scoped_state_exists():
    payload = {
        "4:88": {
            "window_id": "@legacy",
            "plan_path": "plans/legacy.md",
            "keyword": "done",
            "instructions": "",
            "interval_seconds": 600,
            "started_at": 100.0,
            "next_prompt_at": 700.0,
        },
        "4:-100401:88": {
            "window_id": "@authoritative",
            "plan_path": "plans/current.md",
            "keyword": "ship",
            "instructions": "",
            "interval_seconds": 900,
            "started_at": 200.0,
            "next_prompt_at": 1100.0,
        },
    }
    looper._LOOPER_STATE_FILE.write_text(__import__("json").dumps(payload), encoding="utf-8")

    looper.prune_looper_topics({(4, -100401, 88)})

    state = looper.get_looper_state(user_id=4, chat_id=-100401, thread_id=88)
    assert state is not None
    assert state.window_id == "@authoritative"
    assert looper.get_looper_state(user_id=4, thread_id=88) is None
    persisted = __import__("json").loads(looper._LOOPER_STATE_FILE.read_text(encoding="utf-8"))
    assert list(persisted) == ["4:-100401:88"]

    looper.clear_looper_state(4, thread_id=88, chat_id=-100401)
    looper.prune_looper_topics({(4, -100401, 88)})
    assert looper.get_looper_state(user_id=4, thread_id=88) is None


def test_pruning_retains_ambiguous_legacy_state_without_cross_claiming():
    payload = {
        "4:88": {
            "window_id": "@legacy",
            "plan_path": "plans/legacy.md",
            "keyword": "done",
            "instructions": "",
            "interval_seconds": 600,
            "started_at": 100.0,
            "next_prompt_at": 700.0,
        }
    }
    looper._LOOPER_STATE_FILE.write_text(__import__("json").dumps(payload), encoding="utf-8")

    looper.prune_looper_topics({(4, -100401, 88), (4, -100402, 88)})

    assert looper.get_looper_state(user_id=4, thread_id=88) is not None
    assert looper.get_looper_state(user_id=4, chat_id=-100401, thread_id=88) is None
    assert looper.get_looper_state(user_id=4, chat_id=-100402, thread_id=88) is None
    persisted = __import__("json").loads(looper._LOOPER_STATE_FILE.read_text(encoding="utf-8"))
    assert list(persisted) == ["4:88"]
