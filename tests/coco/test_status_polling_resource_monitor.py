"""Tests for resource monitor delivery in status polling."""

from types import SimpleNamespace

import pytest

import coco.handlers.status_polling as status_polling


@pytest.mark.asyncio
async def test_emit_due_resource_monitor_notifications_sends_to_private_admin_chat(
    monkeypatch,
):
    sent: list[tuple[int, int | None, str]] = []

    monkeypatch.setattr(
        status_polling.resource_monitor,
        "collect_due_notifications",
        lambda **_kwargs: ["📊 *Weekly resource average*\n\nCPU: `30.0%`"],
    )
    monkeypatch.setattr(status_polling.config, "allowed_users", {111, 222})

    async def _safe_send(_bot, chat_id, text, *, message_thread_id=None, **_kwargs):
        sent.append((chat_id, message_thread_id, text))

    monkeypatch.setattr(status_polling, "safe_send", _safe_send)

    await status_polling._emit_due_resource_monitor_notifications(SimpleNamespace())

    assert sent == [
        (111, None, "📊 *Weekly resource average*\n\nCPU: `30.0%`"),
        (222, None, "📊 *Weekly resource average*\n\nCPU: `30.0%`"),
    ]


@pytest.mark.asyncio
async def test_emit_due_reset_credit_notifications_uses_resource_alert_recipients(
    monkeypatch,
):
    sent: list[tuple[int, int | None, str]] = []
    collect_calls: list[dict[str, object]] = []

    def _collect(**kwargs):
        collect_calls.append(kwargs)
        return ["⏰ *Codex reset credit*\n\nExpires in 1 hour."]

    monkeypatch.setattr(
        status_polling.reset_credit_monitor,
        "collect_due_notifications",
        _collect,
    )
    monkeypatch.setattr(status_polling.config, "allowed_users", {111, 222})
    acknowledged: list[list[str]] = []
    monkeypatch.setattr(
        status_polling.reset_credit_monitor,
        "acknowledge_notifications",
        lambda notices: acknowledged.append(list(notices)),
    )

    async def _safe_send(_bot, chat_id, text, *, message_thread_id=None, **_kwargs):
        sent.append((chat_id, message_thread_id, text))
        return SimpleNamespace(message_id=1)

    monkeypatch.setattr(status_polling, "safe_send", _safe_send)

    await status_polling._emit_due_reset_credit_notifications(SimpleNamespace())

    assert sent == [
        (111, None, "⏰ *Codex reset credit*\n\nExpires in 1 hour."),
        (222, None, "⏰ *Codex reset credit*\n\nExpires in 1 hour."),
    ]
    assert acknowledged == [["⏰ *Codex reset credit*\n\nExpires in 1 hour."]]
    assert collect_calls == [{"force_refresh": True, "require_fresh": True}]


@pytest.mark.asyncio
async def test_partial_reset_credit_delivery_is_acknowledged(monkeypatch):
    notice = "⏰ *Codex reset credit*\n\nExpires in 1 hour."
    monkeypatch.setattr(
        status_polling.reset_credit_monitor,
        "collect_due_notifications",
        lambda **_kwargs: [notice],
    )
    monkeypatch.setattr(status_polling.config, "allowed_users", {111, 222})
    acknowledged: list[list[str]] = []
    monkeypatch.setattr(
        status_polling.reset_credit_monitor,
        "acknowledge_notifications",
        lambda notices: acknowledged.append(list(notices)),
    )

    async def _safe_send(_bot, chat_id, text, *, message_thread_id=None, **_kwargs):
        if chat_id == 111:
            return SimpleNamespace(message_id=1)
        return None

    monkeypatch.setattr(status_polling, "safe_send", _safe_send)

    await status_polling._emit_due_reset_credit_notifications(SimpleNamespace())

    assert acknowledged == [[notice]]


@pytest.mark.asyncio
async def test_reset_credit_delivery_continues_after_one_recipient_errors(monkeypatch):
    notice = "⏰ *Codex reset credit*\n\nExpires in 1 hour."
    monkeypatch.setattr(
        status_polling.reset_credit_monitor,
        "collect_due_notifications",
        lambda **_kwargs: [notice],
    )
    monkeypatch.setattr(status_polling.config, "allowed_users", {111, 222})
    acknowledged: list[list[str]] = []
    monkeypatch.setattr(
        status_polling.reset_credit_monitor,
        "acknowledge_notifications",
        lambda notices: acknowledged.append(list(notices)),
    )

    async def _safe_send(_bot, chat_id, text, *, message_thread_id=None, **_kwargs):
        if chat_id == 111:
            raise RuntimeError("Chat not found")
        return SimpleNamespace(message_id=1)

    monkeypatch.setattr(status_polling, "safe_send", _safe_send)

    await status_polling._emit_due_reset_credit_notifications(SimpleNamespace())

    assert acknowledged == [[notice]]


@pytest.mark.asyncio
async def test_emit_due_quota_notifications_uses_private_alert_recipients(monkeypatch):
    notice = "📉 *Codex quota alert*\n\nPrimary: `89% remaining`"
    monkeypatch.setattr(status_polling.config, "allowed_users", {111, 222})
    monkeypatch.setattr(
        status_polling.codex_app_server_client,
        "read_rate_limits",
        lambda: _async_result({"rateLimits": {"primary": {"usedPercent": 11}}}),
    )
    monkeypatch.setattr(
        status_polling.quota_monitor,
        "collect_due_notifications",
        lambda _limits: [notice],
    )
    acknowledged: list[list[str]] = []
    monkeypatch.setattr(
        status_polling.quota_monitor,
        "acknowledge_notifications",
        lambda notices: acknowledged.append(list(notices)),
    )
    sent: list[tuple[int, int | None]] = []

    async def _safe_send(_bot, chat_id, _text, *, message_thread_id=None, **_kwargs):
        sent.append((chat_id, message_thread_id))
        return SimpleNamespace(message_id=1)

    monkeypatch.setattr(status_polling, "safe_send", _safe_send)

    await status_polling._emit_due_quota_notifications(SimpleNamespace())

    assert sent == [(111, None), (222, None)]
    assert acknowledged == [[notice]]


@pytest.mark.asyncio
async def test_quota_partial_delivery_is_acknowledged(monkeypatch):
    notice = "📉 *Codex quota alert*"
    monkeypatch.setattr(status_polling.config, "allowed_users", {111, 222})
    monkeypatch.setattr(
        status_polling.codex_app_server_client,
        "read_rate_limits",
        lambda: _async_result({"rateLimits": {"primary": {"usedPercent": 11}}}),
    )
    monkeypatch.setattr(
        status_polling.quota_monitor,
        "collect_due_notifications",
        lambda _limits: [notice],
    )
    acknowledged: list[list[str]] = []
    monkeypatch.setattr(
        status_polling.quota_monitor,
        "acknowledge_notifications",
        lambda notices: acknowledged.append(list(notices)),
    )

    async def _safe_send(_bot, chat_id, _text, *, message_thread_id=None, **_kwargs):
        if chat_id == 111:
            raise RuntimeError("Chat not found")
        return SimpleNamespace(message_id=1)

    monkeypatch.setattr(status_polling, "safe_send", _safe_send)

    await status_polling._emit_due_quota_notifications(SimpleNamespace())

    assert acknowledged == [[notice]]


def _async_result(value):
    async def _result():
        return value

    return _result()


def test_quota_monitor_checks_at_most_hourly():
    assert status_polling.QUOTA_CHECK_INTERVAL == 60 * 60


@pytest.mark.asyncio
async def test_codex_account_monitor_checks_resets_and_quota_together(monkeypatch):
    calls: list[str] = []

    async def _reset(_bot):
        calls.append("reset")

    async def _quota(_bot):
        calls.append("quota")

    monkeypatch.setattr(status_polling, "_emit_due_reset_credit_notifications", _reset)
    monkeypatch.setattr(status_polling, "_emit_due_quota_notifications", _quota)

    await status_polling._emit_due_codex_account_notifications(SimpleNamespace())

    assert calls == ["reset", "quota"]
