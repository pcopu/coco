"""Tests for no-response watchdog checkpoint tracking."""

from types import SimpleNamespace

import pytest

import coco.bot as bot
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


def test_retry_count_persists_from_epoch_timestamp_across_restart_when_monotonic_resets():
    _clear()
    watchdog.note_run_started(
        user_id=16,
        thread_id=160,
        window_id="@16",
        source="user_input",
        expect_response=True,
        pending_text="carry retry state",
        now=50000.0,
        persisted_now=1_700_000_000.0,
    )
    watchdog.get_due_run_checks(
        user_id=16,
        thread_id=160,
        window_id="@16",
        now=50030.0,
    )
    watchdog.note_auto_retry_attempt(
        user_id=16,
        thread_id=160,
        window_id="@16",
        now=50030.1,
        persisted_now=1_700_000_030.1,
    )

    watchdog.reset_run_watchdog_for_tests(clear_persisted=False)

    watchdog.note_run_started(
        user_id=16,
        thread_id=160,
        window_id="@16",
        source="user_input",
        expect_response=True,
        pending_text="carry retry state",
        now=1000.0,
        persisted_now=1_700_000_060.0,
    )
    due_30 = watchdog.get_due_run_checks(
        user_id=16,
        thread_id=160,
        window_id="@16",
        now=1030.0,
    )
    assert [item.checkpoint_seconds for item in due_30] == [30]
    assert due_30[0].auto_retry_allowed is True
    assert due_30[0].retry_count == 1


def test_legacy_monotonic_retry_state_is_discarded_after_restart():
    _clear()
    fingerprint = watchdog._fingerprint_text("carry retry state")
    retry_key = watchdog._retry_key((17, 170), fingerprint)
    watchdog._RUN_RETRY_STATE_FILE.write_text(
        (
            "{\n"
            f'  "{retry_key}": {{"count": 2, "updated_at": 50030.1}}\n'
            "}\n"
        ),
        encoding="utf-8",
    )

    watchdog.reset_run_watchdog_for_tests(clear_persisted=False)

    watchdog.note_run_started(
        user_id=17,
        thread_id=170,
        window_id="@17",
        source="user_input",
        expect_response=True,
        pending_text="carry retry state",
        now=1000.0,
        persisted_now=1_700_000_060.0,
    )
    due_30 = watchdog.get_due_run_checks(
        user_id=17,
        thread_id=170,
        window_id="@17",
        now=1030.0,
    )
    assert [item.checkpoint_seconds for item in due_30] == [30]
    assert due_30[0].auto_retry_allowed is True
    assert due_30[0].retry_count == 0


def test_activity_resets_silence_clock_without_clearing_state():
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
    watchdog.note_run_activity(
        user_id=3,
        thread_id=30,
        window_id="@3",
        source="assistant_progress",
        now=20.0,
    )

    assert watchdog.get_due_run_checks(
        user_id=3,
        thread_id=30,
        window_id="@3",
        now=49.0,
    ) == []

    due_30 = watchdog.get_due_run_checks(
        user_id=3,
        thread_id=30,
        window_id="@3",
        now=50.0,
    )

    assert [item.checkpoint_seconds for item in due_30] == [30]
    assert due_30[0].elapsed_seconds == 30.0


def test_post_activity_check_never_auto_resends():
    _clear()
    watchdog.note_run_started(
        user_id=3,
        thread_id=31,
        window_id="@3",
        source="user_input",
        expect_response=True,
        pending_text="check",
        now=0.0,
    )
    watchdog.note_run_activity(
        user_id=3,
        thread_id=31,
        window_id="@3",
        source="assistant_progress",
        now=5.0,
    )

    due_30 = watchdog.get_due_run_checks(
        user_id=3,
        thread_id=31,
        window_id="@3",
        now=35.0,
    )

    assert [item.checkpoint_seconds for item in due_30] == [30]
    assert due_30[0].auto_retry_allowed is False


def test_non_response_start_does_not_clear_existing_watch():
    _clear()
    watchdog.note_run_started(
        user_id=3,
        thread_id=32,
        window_id="@3",
        source="user_input",
        expect_response=True,
        pending_text="check",
        now=0.0,
    )
    watchdog.note_run_started(
        user_id=3,
        thread_id=32,
        window_id="@3",
        source="slash:status",
        expect_response=False,
        pending_text="/status",
        now=10.0,
    )

    due_30 = watchdog.get_due_run_checks(
        user_id=3,
        thread_id=32,
        window_id="@3",
        now=30.0,
    )

    assert [item.checkpoint_seconds for item in due_30] == [30]


@pytest.mark.asyncio
async def test_successful_forwarded_clear_disarms_existing_watch(monkeypatch):
    user_id = 3
    thread_id = 33
    window_id = "@3"
    watchdog.note_run_started(
        user_id=user_id,
        thread_id=thread_id,
        window_id=window_id,
        source="user_input",
        expect_response=True,
        pending_text="do not replay after clear",
        now=0.0,
    )

    async def _send_action(_action):
        return None

    chat = SimpleNamespace(
        id=-1003,
        type="supergroup",
        send_action=_send_action,
    )
    message = SimpleNamespace(
        text="/clear",
        message_thread_id=thread_id,
        chat=chat,
    )
    update = SimpleNamespace(
        effective_chat=chat,
        effective_user=SimpleNamespace(id=user_id),
        effective_message=message,
        message=message,
    )
    monkeypatch.setattr(bot, "_is_chat_allowed", lambda _chat: True)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _user_id: True)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_window_for_thread",
        lambda *_args, **_kwargs: window_id,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda *_args, **_kwargs: SimpleNamespace(
            codex_thread_id="thread-3",
            cwd="/tmp/project",
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_display_name",
        lambda _window_id: "topic",
    )

    async def _send_to_window(_window_id, _text):
        return True, "ok"

    monkeypatch.setattr(bot.session_manager, "send_to_window", _send_to_window)
    monkeypatch.setattr(
        bot.session_manager,
        "clear_window_session",
        lambda _window_id: None,
    )

    async def _safe_reply(_message, _text, **_kwargs):
        return None

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.forward_command_handler(update, SimpleNamespace())

    assert watchdog.get_due_run_checks(
        user_id=user_id,
        thread_id=thread_id,
        window_id=window_id,
        now=30.0,
    ) == []


def test_clear_run_watch_state_honors_window_ownership():
    watchdog.note_run_started(
        user_id=3,
        thread_id=34,
        window_id="@new",
        source="user_input",
        expect_response=True,
        pending_text="still owned by the new window",
        now=0.0,
    )

    assert (
        watchdog.clear_run_watch_state(
            3,
            34,
            window_id="@old",
        )
        is False
    )
    due_30 = watchdog.get_due_run_checks(
        user_id=3,
        thread_id=34,
        window_id="@new",
        now=30.0,
    )
    assert [item.checkpoint_seconds for item in due_30] == [30]

    assert (
        watchdog.clear_run_watch_state(
            3,
            34,
            window_id="@new",
        )
        is True
    )
    assert watchdog.get_due_run_checks(
        user_id=3,
        thread_id=34,
        window_id="@new",
        now=60.0,
    ) == []


def test_completion_after_activity_remains_terminal():
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
    watchdog.note_run_activity(
        user_id=4,
        thread_id=40,
        window_id="@4",
        source="assistant_progress",
        now=10.0,
    )
    due_30 = watchdog.get_due_run_checks(
        user_id=4,
        thread_id=40,
        window_id="@4",
        now=40.0,
    )
    assert [item.checkpoint_seconds for item in due_30] == [30]

    watchdog.note_run_completed(
        user_id=4,
        thread_id=40,
        reason="done",
        now=41.0,
    )
    assert watchdog.get_due_run_checks(
        user_id=4,
        thread_id=40,
        window_id="@4",
        now=300.0,
    ) == []


def test_transport_uncertainty_suppresses_replay_without_known_turn_id():
    _clear()
    watchdog.note_run_started(
        user_id=5,
        thread_id=50,
        window_id="@5",
        source="user_input",
        expect_response=True,
        pending_text="run once",
        now=0.0,
    )

    watchdog.note_transport_reset_uncertainty(
        window_ids={"@5"},
        reason="request_timeout:turn/start",
        now=5.0,
    )

    due_30 = watchdog.get_due_run_checks(
        user_id=5,
        thread_id=50,
        window_id="@5",
        now=35.0,
    )
    assert [item.checkpoint_seconds for item in due_30] == [30]
    assert due_30[0].auto_retry_allowed is False
    assert due_30[0].auto_retry_reason == "transport_uncertain"
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
