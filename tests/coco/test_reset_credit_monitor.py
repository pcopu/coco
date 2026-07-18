"""Tests for persistent Codex reset-credit expiry reminders."""

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import coco.handlers.reset_credit_monitor as reset_credit_monitor


def _credit(credit_id: str, expires_at: datetime) -> reset_credit_monitor.ResetCredit:
    return reset_credit_monitor.ResetCredit(
        credit_id=credit_id,
        title="Full reset (Weekly + 5 hr)",
        expires_at=expires_at,
    )


def test_new_credit_is_persisted_without_sending_an_early_reminder(monkeypatch, tmp_path):
    monkeypatch.setenv("COCO_DIR", str(tmp_path))
    expiry = datetime(2026, 7, 14, 19, 40, tzinfo=UTC)

    notifications = reset_credit_monitor.collect_due_notifications(
        now=expiry - timedelta(days=3),
        fetch_credits=lambda: [_credit("credit-1", expiry)],
        force_refresh=True,
        timezone_name="UTC",
    )

    assert notifications == []
    state = json.loads((tmp_path / "reset_credit_monitor_state.json").read_text())
    assert state["credits"]["credit-1"]["expires_at"] == "2026-07-14T19:40:00+00:00"


def test_credit_sends_each_due_reminder_once(monkeypatch, tmp_path):
    monkeypatch.setenv("COCO_DIR", str(tmp_path))
    expiry = datetime(2026, 7, 14, 19, 40, tzinfo=UTC)
    def fetch():
        return [_credit("credit-1", expiry)]

    reset_credit_monitor.collect_due_notifications(
        now=expiry - timedelta(days=3),
        fetch_credits=fetch,
        force_refresh=True,
        timezone_name="UTC",
    )

    at_24h = reset_credit_monitor.collect_due_notifications(
        now=expiry - timedelta(hours=24),
        fetch_credits=fetch,
        timezone_name="UTC",
    )
    assert len(at_24h) == 1
    assert "24 hours" in at_24h[0]
    reset_credit_monitor.acknowledge_notifications(at_24h)
    at_24h_again = reset_credit_monitor.collect_due_notifications(
        now=expiry - timedelta(hours=23, minutes=59),
        fetch_credits=fetch,
        timezone_name="UTC",
    )
    morning = reset_credit_monitor.collect_due_notifications(
        now=datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
        fetch_credits=fetch,
        timezone_name="UTC",
    )
    assert len(morning) == 1
    assert "expires today" in morning[0]
    reset_credit_monitor.acknowledge_notifications(morning)
    at_1h = reset_credit_monitor.collect_due_notifications(
        now=expiry - timedelta(hours=1),
        fetch_credits=fetch,
        timezone_name="UTC",
    )
    assert len(at_1h) == 1
    assert "1 hour" in at_1h[0]
    reset_credit_monitor.acknowledge_notifications(at_1h)
    at_1h_again = reset_credit_monitor.collect_due_notifications(
        now=expiry - timedelta(minutes=30),
        fetch_credits=fetch,
        timezone_name="UTC",
    )

    assert at_24h_again == []
    assert at_1h_again == []


def test_daily_refresh_adds_new_credits(monkeypatch, tmp_path):
    monkeypatch.setenv("COCO_DIR", str(tmp_path))
    start = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    first_expiry = start + timedelta(days=10)
    second_expiry = start + timedelta(days=12)
    fetched = [[_credit("credit-1", first_expiry)], [_credit("credit-1", first_expiry), _credit("credit-2", second_expiry)]]

    def fetch():
        return fetched.pop(0)

    reset_credit_monitor.collect_due_notifications(
        now=start,
        fetch_credits=fetch,
        force_refresh=True,
        timezone_name="UTC",
    )
    reset_credit_monitor.collect_due_notifications(
        now=start + timedelta(hours=23),
        fetch_credits=fetch,
        timezone_name="UTC",
    )
    assert len(fetched) == 1

    reset_credit_monitor.collect_due_notifications(
        now=start + timedelta(days=1),
        fetch_credits=fetch,
        timezone_name="UTC",
    )

    state = json.loads((tmp_path / "reset_credit_monitor_state.json").read_text())
    assert set(state["credits"]) == {"credit-1", "credit-2"}
    assert fetched == []


def test_fetch_failure_keeps_existing_reminders(monkeypatch, tmp_path):
    monkeypatch.setenv("COCO_DIR", str(tmp_path))
    expiry = datetime(2026, 7, 14, 19, 40, tzinfo=UTC)
    reset_credit_monitor.collect_due_notifications(
        now=expiry - timedelta(days=2),
        fetch_credits=lambda: [_credit("credit-1", expiry)],
        force_refresh=True,
        timezone_name="UTC",
    )

    def fail():
        raise reset_credit_monitor.ResetCreditFetchError("offline")

    notifications = reset_credit_monitor.collect_due_notifications(
        now=expiry - timedelta(hours=24),
        fetch_credits=fail,
        force_refresh=True,
        timezone_name="UTC",
    )

    assert len(notifications) == 1
    assert "24 hours" in notifications[0]
    state = json.loads((tmp_path / "reset_credit_monitor_state.json").read_text())
    assert "credit-1" in state["credits"]


def test_required_fresh_refresh_suppresses_cached_reminder_on_fetch_failure(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("COCO_DIR", str(tmp_path))
    expiry = datetime(2026, 7, 14, 19, 40, tzinfo=UTC)
    reset_credit_monitor.collect_due_notifications(
        now=expiry - timedelta(days=2),
        fetch_credits=lambda: [_credit("possibly-used-credit", expiry)],
        force_refresh=True,
        timezone_name="UTC",
    )

    def fail():
        raise reset_credit_monitor.ResetCreditFetchError("offline")

    notifications = reset_credit_monitor.collect_due_notifications(
        now=expiry - timedelta(hours=1),
        fetch_credits=fail,
        force_refresh=True,
        require_fresh=True,
        timezone_name="UTC",
    )

    assert notifications == []


def test_corrupt_state_is_recovered(monkeypatch, tmp_path):
    monkeypatch.setenv("COCO_DIR", str(tmp_path))
    path = Path(tmp_path) / "reset_credit_monitor_state.json"
    path.write_text("not json", encoding="utf-8")
    expiry = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

    notifications = reset_credit_monitor.collect_due_notifications(
        now=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        fetch_credits=lambda: [_credit("credit-1", expiry)],
        force_refresh=True,
        timezone_name="UTC",
    )

    assert notifications == []
    assert json.loads(path.read_text())["credits"]["credit-1"]


def test_successful_refresh_removes_credits_that_are_no_longer_available(monkeypatch, tmp_path):
    monkeypatch.setenv("COCO_DIR", str(tmp_path))
    start = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    expiry = start + timedelta(days=10)
    reset_credit_monitor.collect_due_notifications(
        now=start,
        fetch_credits=lambda: [_credit("redeemed-credit", expiry)],
        force_refresh=True,
        timezone_name="UTC",
    )

    reset_credit_monitor.collect_due_notifications(
        now=start + timedelta(days=1),
        fetch_credits=lambda: [],
        force_refresh=True,
        timezone_name="UTC",
    )

    state = json.loads((tmp_path / "reset_credit_monitor_state.json").read_text())
    assert "redeemed-credit" not in state["credits"]


def test_morning_reminder_moves_to_previous_day_when_credit_expires_early(monkeypatch, tmp_path):
    monkeypatch.setenv("COCO_DIR", str(tmp_path))
    expiry = datetime(2026, 7, 14, 2, 30, tzinfo=UTC)
    reset_credit_monitor.collect_due_notifications(
        now=expiry - timedelta(days=3),
        fetch_credits=lambda: [_credit("early-credit", expiry)],
        force_refresh=True,
        timezone_name="UTC",
    )
    first = reset_credit_monitor.collect_due_notifications(
        now=expiry - timedelta(hours=24),
        fetch_credits=lambda: [_credit("early-credit", expiry)],
        timezone_name="UTC",
    )
    reset_credit_monitor.acknowledge_notifications(first)

    notifications = reset_credit_monitor.collect_due_notifications(
        now=datetime(2026, 7, 13, 9, 0, tzinfo=UTC),
        fetch_credits=lambda: [_credit("early-credit", expiry)],
        timezone_name="UTC",
    )

    assert len(notifications) == 1
    assert "expires today" in notifications[0]


def test_late_discovery_sends_only_the_latest_due_milestone(monkeypatch, tmp_path):
    monkeypatch.setenv("COCO_DIR", str(tmp_path))
    expiry = datetime(2026, 7, 14, 19, 40, tzinfo=UTC)

    notifications = reset_credit_monitor.collect_due_notifications(
        now=expiry - timedelta(hours=2),
        fetch_credits=lambda: [_credit("late-credit", expiry)],
        force_refresh=True,
        timezone_name="UTC",
    )

    assert len(notifications) == 1
    assert "expires today" in notifications[0]
    assert "24 hours" not in notifications[0]


def test_fetch_uses_local_codex_login_and_keeps_only_available_credits(monkeypatch, tmp_path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "secret-token",
                    "account_id": "account-1",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(reset_credit_monitor, "_auth_file_path", lambda: auth_path)
    captured = SimpleNamespace(request=None, timeout=None)

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps(
                {
                    "credits": [
                        {
                            "id": "available-1",
                            "title": "Full reset",
                            "status": "available",
                            "expires_at": "2026-07-14T19:40:51Z",
                        },
                        {
                            "id": "used-1",
                            "title": "Full reset",
                            "status": "redeemed",
                            "expires_at": "2026-07-15T19:40:51Z",
                        },
                    ]
                }
            ).encode()

    def _urlopen(request, *, timeout):
        captured.request = request
        captured.timeout = timeout
        return _Response()

    monkeypatch.setattr(reset_credit_monitor, "urlopen", _urlopen)

    credits = reset_credit_monitor.fetch_reset_credits(timeout=3.0)

    assert [credit.credit_id for credit in credits] == ["available-1"]
    assert credits[0].expires_at == datetime(2026, 7, 14, 19, 40, 51, tzinfo=UTC)
    assert captured.timeout == 3.0
    assert captured.request.get_header("Authorization") == "Bearer secret-token"
    assert captured.request.get_header("Chatgpt-account-id") == "account-1"
