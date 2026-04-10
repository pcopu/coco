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
