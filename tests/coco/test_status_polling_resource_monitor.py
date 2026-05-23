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
