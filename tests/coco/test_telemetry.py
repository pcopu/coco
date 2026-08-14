"""Tests for structured telemetry event encoding."""

import json
import logging

import pytest

from coco import telemetry
from coco.telemetry import _MAX_STRING_CHARS, emit_telemetry


@pytest.fixture(autouse=True)
def clear_recent_failures():
    recent_failures = getattr(telemetry, "_recent_failures", None)
    if recent_failures is not None:
        recent_failures.clear()
    yield
    if recent_failures is not None:
        recent_failures.clear()


def _get_recent_failures(limit=5):
    return telemetry.get_recent_failures(limit)


def test_emit_telemetry_logs_json_payload(caplog):
    with caplog.at_level(logging.INFO, logger="coco.telemetry"):
        emit_telemetry("queue.q_native_result", success=True, attempts=2, text_len=44)

    assert caplog.records
    payload = json.loads(caplog.records[-1].message)
    assert payload["event"] == "queue.q_native_result"
    assert payload["success"] is True
    assert payload["attempts"] == 2
    assert payload["text_len"] == 44
    assert "ts" in payload


def test_emit_telemetry_truncates_long_string(caplog):
    long_value = "x" * (_MAX_STRING_CHARS + 25)
    with caplog.at_level(logging.INFO, logger="coco.telemetry"):
        emit_telemetry("watchdog.check_fired", resend_err=long_value)

    payload = json.loads(caplog.records[-1].message)
    resend_err = str(payload["resend_err"])
    assert resend_err.startswith("x" * _MAX_STRING_CHARS)
    assert resend_err.endswith(f"...[{len(long_value)} chars]")


def test_emit_telemetry_records_failure_event():
    emit_telemetry("worker.failure", reason="backend unavailable")

    failures = _get_recent_failures()

    assert len(failures) == 1
    assert failures[0]["event"] == "worker.failure"
    assert failures[0]["reason"] == "backend unavailable"
    assert isinstance(failures[0]["ts"], float)


def test_emit_telemetry_ignores_normal_event():
    emit_telemetry("worker.started", status="ok")

    assert _get_recent_failures() == []


def test_recent_failures_are_bounded_fifo():
    for index in range(telemetry._MAX_RECENT_FAILURES + 3):
        emit_telemetry(f"worker.failure.{index}")

    failures = _get_recent_failures(limit=telemetry._MAX_RECENT_FAILURES + 10)

    assert len(failures) == telemetry._MAX_RECENT_FAILURES
    assert [item["event"] for item in failures] == [
        f"worker.failure.{index}"
        for index in range(3, telemetry._MAX_RECENT_FAILURES + 3)
    ]


def test_recent_failures_return_safe_copies():
    emit_telemetry("worker.error", context={"attempts": [1, 2]})

    failures = _get_recent_failures()
    failures[0]["event"] = "tampered"
    failures[0]["context"]["attempts"].append("tampered")

    fresh_failures = _get_recent_failures()
    assert fresh_failures[0]["event"] == "worker.error"
    assert fresh_failures[0]["context"] == {"attempts": [1, 2]}


def test_recent_failures_bound_event_and_field_names():
    long_event = "worker.failure." + ("x" * (_MAX_STRING_CHARS + 25))
    long_key = "field_" + ("y" * (_MAX_STRING_CHARS + 25))

    emit_telemetry(long_event, **{long_key: "value"})

    failure = _get_recent_failures()[0]
    retained_event = failure["event"]
    retained_key = next(key for key in failure if key.startswith("field_"))
    assert len(retained_event) < len(long_event)
    assert len(retained_key) < len(long_key)
