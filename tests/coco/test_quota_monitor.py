"""Tests for durable Codex quota threshold alerts."""

import importlib
import json


def _monitor():
    return importlib.import_module("coco.handlers.quota_monitor")


def _limits(*, primary_used: float, secondary_used: float = 0, reset: int = 1000):
    return {
        "primary": {
            "usedPercent": primary_used,
            "resetsAt": reset,
            "windowDurationMins": 300,
        },
        "secondary": {
            "usedPercent": secondary_used,
            "resetsAt": reset + 500,
            "windowDurationMins": 10080,
        },
    }


def test_first_observation_below_threshold_reports_crossed_remaining_marks(
    monkeypatch, tmp_path
):
    monitor = _monitor()
    monkeypatch.setenv("COCO_DIR", str(tmp_path))

    notices = monitor.collect_due_notifications(
        _limits(primary_used=32),
    )

    assert len(notices) == 1
    assert "Codex quota alert" in notices[0]
    assert "Primary (5 hours)" in notices[0]
    assert "68% remaining" in notices[0]
    assert "90%, 80%, and 70%" in notices[0]


def test_thresholds_are_sent_once_after_successful_delivery(monkeypatch, tmp_path):
    monitor = _monitor()
    monkeypatch.setenv("COCO_DIR", str(tmp_path))

    first = monitor.collect_due_notifications(_limits(primary_used=11))
    assert len(first) == 1
    assert "90%" in first[0]
    monitor.acknowledge_notifications(first)

    assert monitor.collect_due_notifications(_limits(primary_used=11)) == []

    second = monitor.collect_due_notifications(_limits(primary_used=21))
    assert len(second) == 1
    assert "80%" in second[0]
    assert "90%" not in second[0].split("Crossed: ", 1)[1]


def test_allowance_refresh_starts_a_new_threshold_cycle(monkeypatch, tmp_path):
    monitor = _monitor()
    monkeypatch.setenv("COCO_DIR", str(tmp_path))

    first = monitor.collect_due_notifications(_limits(primary_used=11, reset=1000))
    monitor.acknowledge_notifications(first)

    assert (
        monitor.collect_due_notifications(_limits(primary_used=0, reset=2000)) == []
    )
    repeated = monitor.collect_due_notifications(_limits(primary_used=11, reset=2000))

    assert len(repeated) == 1
    assert "90%" in repeated[0]


def test_primary_and_secondary_windows_are_tracked_independently(monkeypatch, tmp_path):
    monitor = _monitor()
    monkeypatch.setenv("COCO_DIR", str(tmp_path))

    notices = monitor.collect_due_notifications(
        _limits(primary_used=11, secondary_used=21)
    )

    assert len(notices) == 2
    assert "Primary (5 hours)" in notices[0]
    assert "Secondary (7 days)" in notices[1]
    assert "80%" in notices[1]


def test_acknowledgement_is_durable(monkeypatch, tmp_path):
    monitor = _monitor()
    monkeypatch.setenv("COCO_DIR", str(tmp_path))

    notices = monitor.collect_due_notifications(_limits(primary_used=11))
    monitor.acknowledge_notifications(notices)

    state = json.loads((tmp_path / "quota_monitor_state.json").read_text())
    assert state["windows"]["primary"]["sent_thresholds"] == [90]
