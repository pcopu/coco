"""Host resource usage watcher with weekly summaries and threshold alerts."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from ..utils import atomic_write_json, coco_dir

logger = logging.getLogger(__name__)

SAMPLE_INTERVAL_SECONDS = 5 * 60
WEEKLY_SUMMARY_INTERVAL_SECONDS = 7 * 24 * 60 * 60
ALERT_THRESHOLD_PERCENT = 90.0
STATE_FILE_NAME = "resource_monitor_state.json"


@dataclass
class ResourceSample:
    cpu_percent: float | None
    ram_percent: float | None
    disk_percent: float | None
    gpu_percent: float | None = None


def _state_path() -> Path:
    return coco_dir() / STATE_FILE_NAME


def _default_state() -> dict[str, object]:
    return {
        "samples": [],
        "last_summary_ts": 0.0,
        "last_sample_ts": 0.0,
        "alert_active": {},
        "cpu_prev_total": None,
        "cpu_prev_idle": None,
    }


def _load_state() -> dict[str, object]:
    path = _state_path()
    if not path.is_file():
        return _default_state()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.debug("Failed reading resource monitor state %s: %s", path, exc)
        return _default_state()
    if not isinstance(payload, dict):
        return _default_state()
    state = _default_state()
    state.update(payload)
    return state


def _save_state(state: dict[str, object]) -> None:
    atomic_write_json(_state_path(), state, indent=2)


def reset_resource_monitor_for_tests(*, clear_persisted: bool = True) -> None:
    """Test helper to clear persisted watcher state."""
    if clear_persisted:
        try:
            _state_path().unlink()
        except FileNotFoundError:
            pass


def _trim_samples(samples: list[dict[str, object]], *, now: float) -> list[dict[str, object]]:
    cutoff = now - WEEKLY_SUMMARY_INTERVAL_SECONDS
    return [
        sample
        for sample in samples
        if isinstance(sample, dict) and float(sample.get("ts", 0.0)) >= cutoff
    ]


def _append_sample(
    state: dict[str, object],
    *,
    now: float,
    sample: ResourceSample,
) -> None:
    samples = state.get("samples")
    if not isinstance(samples, list):
        samples = []
    samples.append(
        {
            "ts": now,
            "cpu": sample.cpu_percent,
            "ram": sample.ram_percent,
            "disk": sample.disk_percent,
            "gpu": sample.gpu_percent,
        }
    )
    state["samples"] = _trim_samples(samples, now=now)
    state["last_sample_ts"] = now


def _format_percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}%"


def _collect_alerts(
    state: dict[str, object],
    sample: ResourceSample,
) -> list[str]:
    metrics = {
        "cpu": sample.cpu_percent,
        "ram": sample.ram_percent,
        "disk": sample.disk_percent,
        "gpu": sample.gpu_percent,
    }
    alert_active_raw = state.get("alert_active")
    alert_active = alert_active_raw if isinstance(alert_active_raw, dict) else {}
    newly_active: list[tuple[str, float]] = []
    for key, value in metrics.items():
        was_active = bool(alert_active.get(key))
        is_active = value is not None and value >= ALERT_THRESHOLD_PERCENT
        if is_active and not was_active:
            newly_active.append((key, value))
        alert_active[key] = is_active
    state["alert_active"] = alert_active
    if not newly_active:
        return []
    labels = {"cpu": "CPU", "ram": "RAM", "disk": "Disk", "gpu": "GPU"}
    lines = ["🚨 *Resource alert*", ""]
    for key, value in newly_active:
        lines.append(f"{labels[key]}: `{value:.1f}%`")
    lines.append("")
    lines.append("One or more host resources crossed the `90%` threshold.")
    return ["\n".join(lines)]


def _collect_weekly_summary(
    state: dict[str, object],
    *,
    now: float,
) -> list[str]:
    last_summary_ts = float(state.get("last_summary_ts", 0.0) or 0.0)
    if now - last_summary_ts < WEEKLY_SUMMARY_INTERVAL_SECONDS:
        return []
    samples = state.get("samples")
    if not isinstance(samples, list) or not samples:
        return []

    metrics: dict[str, list[float]] = {"cpu": [], "ram": [], "disk": [], "gpu": []}
    for item in samples:
        if not isinstance(item, dict):
            continue
        for key in metrics:
            value = item.get(key)
            if isinstance(value, (int, float)):
                metrics[key].append(float(value))
    if not any(metrics.values()):
        return []

    labels = {"cpu": "CPU", "ram": "RAM", "disk": "Disk", "gpu": "GPU"}
    lines = ["📊 *Weekly resource average*", ""]
    for key in ("cpu", "ram", "disk", "gpu"):
        values = metrics[key]
        if not values:
            continue
        average = sum(values) / len(values)
        lines.append(f"{labels[key]}: `{average:.1f}%`")
    state["last_summary_ts"] = now
    return ["\n".join(lines)]


def _read_proc_stat() -> tuple[int, int] | None:
    try:
        with Path("/proc/stat").open("r", encoding="utf-8") as handle:
            first_line = handle.readline().strip()
    except OSError:
        return None
    parts = first_line.split()
    if len(parts) < 6 or parts[0] != "cpu":
        return None
    try:
        values = [int(part) for part in parts[1:]]
    except ValueError:
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)
    return total, idle


def _cpu_percent_from_state(state: dict[str, object]) -> float | None:
    current = _read_proc_stat()
    if current is None:
        return None
    current_total, current_idle = current
    prev_total = state.get("cpu_prev_total")
    prev_idle = state.get("cpu_prev_idle")
    state["cpu_prev_total"] = current_total
    state["cpu_prev_idle"] = current_idle
    if not isinstance(prev_total, int) or not isinstance(prev_idle, int):
        return None
    total_delta = current_total - prev_total
    idle_delta = current_idle - prev_idle
    if total_delta <= 0:
        return None
    usage = 100.0 * (1.0 - (idle_delta / total_delta))
    return max(0.0, min(100.0, usage))


def _ram_percent() -> float | None:
    try:
        lines = Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    values: dict[str, int] = {}
    for line in lines:
        key, _, rest = line.partition(":")
        if not key or not rest:
            continue
        number = rest.strip().split()[0]
        try:
            values[key] = int(number)
        except ValueError:
            continue
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if not total or available is None or total <= 0:
        return None
    return max(0.0, min(100.0, ((total - available) / total) * 100.0))


def _disk_percent() -> float | None:
    try:
        usage = shutil.disk_usage("/")
    except OSError:
        return None
    if usage.total <= 0:
        return None
    return max(0.0, min(100.0, (usage.used / usage.total) * 100.0))


def _gpu_percent() -> float | None:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    readings: list[float] = []
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 3:
            continue
        try:
            util = float(parts[0])
            mem_used = float(parts[1])
            mem_total = float(parts[2])
        except ValueError:
            continue
        mem_percent = 0.0 if mem_total <= 0 else (mem_used / mem_total) * 100.0
        readings.append(max(util, mem_percent))
    if not readings:
        return None
    return max(0.0, min(100.0, max(readings)))


def collect_resource_sample(*, state: dict[str, object] | None = None) -> ResourceSample:
    """Collect one host resource sample."""
    mutable_state = state if state is not None else _load_state()
    return ResourceSample(
        cpu_percent=_cpu_percent_from_state(mutable_state),
        ram_percent=_ram_percent(),
        disk_percent=_disk_percent(),
        gpu_percent=_gpu_percent(),
    )


def collect_due_notifications(
    *,
    now: float | None = None,
    sample: ResourceSample | None = None,
    force_sample: bool = False,
) -> list[str]:
    """Persist one sample when due and return due alert/summary texts."""
    timestamp = time.time() if now is None else float(now)
    state = _load_state()
    last_sample_ts = float(state.get("last_sample_ts", 0.0) or 0.0)
    if not force_sample and timestamp - last_sample_ts < SAMPLE_INTERVAL_SECONDS:
        return []

    collected = sample if sample is not None else collect_resource_sample(state=state)
    _append_sample(state, now=timestamp, sample=collected)

    notifications: list[str] = []
    notifications.extend(_collect_alerts(state, collected))
    notifications.extend(_collect_weekly_summary(state, now=timestamp))

    _save_state(state)
    return notifications
