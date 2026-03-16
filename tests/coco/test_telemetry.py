"""Tests for structured telemetry event encoding."""

import json
import logging

from coco.telemetry import _MAX_STRING_CHARS, emit_telemetry


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
