"""Tests for host resource usage watcher state and notifications."""

from pathlib import Path

import coco.handlers.resource_monitor as resource_monitor


def _sample(*, cpu=None, ram, disk, gpu=None):
    return resource_monitor.ResourceSample(
        cpu_percent=cpu,
        ram_percent=ram,
        disk_percent=disk,
        gpu_percent=gpu,
    )


def test_emergency_alert_fires_once_per_threshold_crossing(monkeypatch, tmp_path):
    monkeypatch.setenv("COCO_DIR", str(tmp_path))
    resource_monitor.reset_resource_monitor_for_tests(clear_persisted=True)

    first = resource_monitor.collect_due_notifications(
        now=1_000.0,
        sample=_sample(cpu=91.0, ram=40.0, disk=50.0),
        force_sample=True,
    )
    second = resource_monitor.collect_due_notifications(
        now=1_100.0,
        sample=_sample(cpu=95.0, ram=42.0, disk=50.0),
        force_sample=True,
    )
    third = resource_monitor.collect_due_notifications(
        now=1_200.0,
        sample=_sample(cpu=50.0, ram=42.0, disk=50.0),
        force_sample=True,
    )
    fourth = resource_monitor.collect_due_notifications(
        now=1_300.0,
        sample=_sample(cpu=93.0, ram=42.0, disk=50.0),
        force_sample=True,
    )

    assert len(first) == 1
    assert "Resource alert" in first[0]
    assert "CPU: `91.0%`" in first[0]
    assert second == []
    assert third == []
    assert len(fourth) == 1
    assert "CPU: `93.0%`" in fourth[0]


def test_weekly_average_summary_uses_recent_samples(monkeypatch, tmp_path):
    monkeypatch.setenv("COCO_DIR", str(tmp_path))
    resource_monitor.reset_resource_monitor_for_tests(clear_persisted=True)

    notifications1 = resource_monitor.collect_due_notifications(
        now=10_000.0,
        sample=_sample(cpu=20.0, ram=40.0, disk=60.0, gpu=10.0),
        force_sample=True,
    )
    notifications2 = resource_monitor.collect_due_notifications(
        now=10_000.0 + resource_monitor.WEEKLY_SUMMARY_INTERVAL_SECONDS,
        sample=_sample(cpu=40.0, ram=50.0, disk=70.0, gpu=30.0),
        force_sample=True,
    )

    assert notifications1 == []
    assert len(notifications2) == 1
    text = notifications2[0]
    assert "Weekly resource average" in text
    assert "CPU: `30.0%`" in text
    assert "RAM: `45.0%`" in text
    assert "Disk: `65.0%`" in text
    assert "GPU: `20.0%`" in text


def test_state_file_persists_under_coco_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("COCO_DIR", str(tmp_path))
    resource_monitor.reset_resource_monitor_for_tests(clear_persisted=True)

    resource_monitor.collect_due_notifications(
        now=500.0,
        sample=_sample(cpu=25.0, ram=35.0, disk=45.0),
        force_sample=True,
    )

    assert (Path(tmp_path) / "resource_monitor_state.json").is_file()
