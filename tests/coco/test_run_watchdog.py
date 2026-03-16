"""Tests for no-response watchdog checkpoint tracking."""

import pytest

import coco.handlers.run_watchdog as watchdog


@pytest.fixture(autouse=True)
def _isolated_retry_state(tmp_path, monkeypatch):
    monkeypatch.setattr(
        watchdog,
        "_RUN_RETRY_STATE_FILE",
        tmp_path / "run_watchdog_retry_state.json",
    )
    watchdog.reset_run_watchdog_for_tests()
    yield
    watchdog.reset_run_watchdog_for_tests()


def _clear() -> None:
    watchdog.reset_run_watchdog_for_tests()


def test_start_requires_expected_response_and_text():
    _clear()
    watchdog.note_run_started(
        user_id=1,
        thread_id=10,
        window_id="@1",
        source="slash",
        expect_response=False,
        pending_text="/status",
        now=0.0,
    )
    assert watchdog.get_due_run_checks(
        user_id=1,
        thread_id=10,
        window_id="@1",
        now=600.0,
    ) == []
    _clear()


def test_due_checkpoints_fire_once_at_expanding_intervals_with_resend_policy():
    _clear()
    watchdog.note_run_started(
        user_id=2,
        thread_id=20,
        window_id="@2",
        source="user_input",
        expect_response=True,
        pending_text="hello",
        now=0.0,
    )

    assert watchdog.get_due_run_checks(
        user_id=2,
        thread_id=20,
        window_id="@2",
        now=29.0,
    ) == []

    due_30 = watchdog.get_due_run_checks(
        user_id=2,
        thread_id=20,
        window_id="@2",
        now=30.0,
    )
    assert [item.checkpoint_seconds for item in due_30] == [30]
    assert due_30[0].resend_text == "hello"
    assert due_30[0].auto_retry_allowed is True
    assert due_30[0].auto_retry_reason == "eligible"
    assert due_30[0].retry_count == 0
    assert due_30[0].max_auto_retries == 2

    retry_count, retry_limit = watchdog.note_auto_retry_attempt(
        user_id=2,
        thread_id=20,
        window_id="@2",
        now=30.1,
    )
    assert (retry_count, retry_limit) == (1, 2)

    due_60 = watchdog.get_due_run_checks(
        user_id=2,
        thread_id=20,
        window_id="@2",
        now=60.0,
    )
    assert [item.checkpoint_seconds for item in due_60] == [60]
    assert due_60[0].auto_retry_allowed is True
    assert due_60[0].auto_retry_reason == "eligible"
    assert due_60[0].retry_count == 1

    retry_count, retry_limit = watchdog.note_auto_retry_attempt(
        user_id=2,
        thread_id=20,
        window_id="@2",
        now=60.1,
    )
    assert (retry_count, retry_limit) == (2, 2)

    due_180 = watchdog.get_due_run_checks(
        user_id=2,
        thread_id=20,
        window_id="@2",
        now=180.0,
    )
    assert [item.checkpoint_seconds for item in due_180] == [180]
    assert due_180[0].auto_retry_allowed is False
    assert due_180[0].auto_retry_reason == "checkpoint"
    assert due_180[0].retry_count == 2

    due_300 = watchdog.get_due_run_checks(
        user_id=2,
        thread_id=20,
        window_id="@2",
        now=300.0,
    )
    assert [item.checkpoint_seconds for item in due_300] == [300]
    assert due_300[0].auto_retry_allowed is False
    assert due_300[0].auto_retry_reason == "checkpoint"
    assert due_300[0].retry_count == 2

    due_600 = watchdog.get_due_run_checks(
        user_id=2,
        thread_id=20,
        window_id="@2",
        now=600.0,
    )
    assert [item.checkpoint_seconds for item in due_600] == [600]

    due_1200 = watchdog.get_due_run_checks(
        user_id=2,
        thread_id=20,
        window_id="@2",
        now=1200.0,
    )
    assert [item.checkpoint_seconds for item in due_1200] == [1200]

    due_1800 = watchdog.get_due_run_checks(
        user_id=2,
        thread_id=20,
        window_id="@2",
        now=1800.0,
    )
    assert [item.checkpoint_seconds for item in due_1800] == [1800]

    due_3600 = watchdog.get_due_run_checks(
        user_id=2,
        thread_id=20,
        window_id="@2",
        now=3600.0,
    )
    assert [item.checkpoint_seconds for item in due_3600] == [3600]

    assert watchdog.get_due_run_checks(
        user_id=2,
        thread_id=20,
        window_id="@2",
        now=3601.0,
    ) == []
    _clear()


def test_retry_count_persists_for_same_text_across_restart():
    _clear()
    watchdog.note_run_started(
        user_id=6,
        thread_id=60,
        window_id="@6",
        source="user_input",
        expect_response=True,
        pending_text="big request",
        now=0.0,
    )
    watchdog.get_due_run_checks(
        user_id=6,
        thread_id=60,
        window_id="@6",
        now=30.0,
    )
    watchdog.note_auto_retry_attempt(
        user_id=6,
        thread_id=60,
        window_id="@6",
        now=30.1,
    )
    watchdog.get_due_run_checks(
        user_id=6,
        thread_id=60,
        window_id="@6",
        now=60.0,
    )
    watchdog.note_auto_retry_attempt(
        user_id=6,
        thread_id=60,
        window_id="@6",
        now=60.1,
    )

    watchdog.reset_run_watchdog_for_tests(clear_persisted=False)

    watchdog.note_run_started(
        user_id=6,
        thread_id=60,
        window_id="@6",
        source="user_input",
        expect_response=True,
        pending_text="big request",
        now=100.0,
    )
    due_30 = watchdog.get_due_run_checks(
        user_id=6,
        thread_id=60,
        window_id="@6",
        now=130.0,
    )
    assert [item.checkpoint_seconds for item in due_30] == [30]
    assert due_30[0].auto_retry_allowed is False
    assert due_30[0].auto_retry_reason == "retry_cap"
    assert due_30[0].retry_count == 2


def test_activity_clears_pending_state_and_retry_counter():
    _clear()
    watchdog.note_run_started(
        user_id=3,
        thread_id=30,
        window_id="@3",
        source="user_input",
        expect_response=True,
        pending_text="check",
        now=0.0,
    )
    watchdog.get_due_run_checks(
        user_id=3,
        thread_id=30,
        window_id="@3",
        now=30.0,
    )
    watchdog.note_auto_retry_attempt(
        user_id=3,
        thread_id=30,
        window_id="@3",
        now=30.1,
    )
    watchdog.note_run_activity(
        user_id=3,
        thread_id=30,
        window_id="@3",
        source="assistant_text",
        now=35.0,
    )
    assert watchdog.get_due_run_checks(
        user_id=3,
        thread_id=30,
        window_id="@3",
        now=300.0,
    ) == []

    watchdog.reset_run_watchdog_for_tests(clear_persisted=False)
    watchdog.note_run_started(
        user_id=3,
        thread_id=30,
        window_id="@3",
        source="user_input",
        expect_response=True,
        pending_text="check",
        now=500.0,
    )
    due_30 = watchdog.get_due_run_checks(
        user_id=3,
        thread_id=30,
        window_id="@3",
        now=530.0,
    )
    assert [item.checkpoint_seconds for item in due_30] == [30]
    assert due_30[0].auto_retry_allowed is True
    assert due_30[0].retry_count == 0


def test_completion_clears_pending_state():
    _clear()
    watchdog.note_run_started(
        user_id=4,
        thread_id=40,
        window_id="@4",
        source="user_input",
        expect_response=True,
        pending_text="check",
        now=0.0,
    )
    watchdog.note_run_completed(
        user_id=4,
        thread_id=40,
        reason="done",
        now=10.0,
    )
    assert watchdog.get_due_run_checks(
        user_id=4,
        thread_id=40,
        window_id="@4",
        now=300.0,
    ) == []
    _clear()


def test_window_change_clears_stale_state():
    _clear()
    watchdog.note_run_started(
        user_id=5,
        thread_id=50,
        window_id="@5",
        source="user_input",
        expect_response=True,
        pending_text="check",
        now=0.0,
    )
    assert watchdog.get_due_run_checks(
        user_id=5,
        thread_id=50,
        window_id="@6",
        now=300.0,
    ) == []
    _clear()


def test_payload_too_large_blocks_auto_retry():
    _clear()
    long_text = "x" * (watchdog.RUN_AUTO_RESEND_MAX_TEXT_CHARS + 1)
    watchdog.note_run_started(
        user_id=7,
        thread_id=70,
        window_id="@7",
        source="user_input",
        expect_response=True,
        pending_text=long_text,
        now=0.0,
    )

    due_30 = watchdog.get_due_run_checks(
        user_id=7,
        thread_id=70,
        window_id="@7",
        now=30.0,
    )
    assert [item.checkpoint_seconds for item in due_30] == [30]
    assert due_30[0].auto_retry_allowed is False
    assert due_30[0].auto_retry_reason == "payload_too_large"
    assert due_30[0].resend_text_len == len(long_text)


def test_immediate_auto_retry_candidate_reflects_pending_state():
    _clear()
    watchdog.note_run_started(
        user_id=9,
        thread_id=90,
        window_id="@9",
        source="user_input",
        expect_response=True,
        pending_text="retry me now",
        now=0.0,
    )

    candidate = watchdog.get_immediate_auto_retry_candidate(
        user_id=9,
        thread_id=90,
        window_id="@9",
        now=12.0,
    )

    assert candidate is not None
    assert candidate.resend_text == "retry me now"
    assert candidate.elapsed_seconds == 12.0
    assert candidate.auto_retry_allowed is True
    assert candidate.auto_retry_reason == "eligible"
    assert candidate.retry_count == 0


def test_successful_auto_retry_blocks_followup_duplicate_retry():
    _clear()
    watchdog.note_run_started(
        user_id=8,
        thread_id=80,
        window_id="@8",
        source="user_input",
        expect_response=True,
        pending_text="hello again",
        now=0.0,
    )

    due_30 = watchdog.get_due_run_checks(
        user_id=8,
        thread_id=80,
        window_id="@8",
        now=30.0,
    )
    assert [item.checkpoint_seconds for item in due_30] == [30]
    assert due_30[0].auto_retry_allowed is True
    watchdog.note_auto_retry_attempt(
        user_id=8,
        thread_id=80,
        window_id="@8",
        now=30.1,
    )
    watchdog.note_auto_retry_result(
        user_id=8,
        thread_id=80,
        window_id="@8",
        send_success=True,
    )

    due_60 = watchdog.get_due_run_checks(
        user_id=8,
        thread_id=80,
        window_id="@8",
        now=60.0,
    )
    assert [item.checkpoint_seconds for item in due_60] == [60]
    assert due_60[0].auto_retry_allowed is False
    assert due_60[0].auto_retry_reason == "already_sent"
