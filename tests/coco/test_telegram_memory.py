"""Tests for Telegram visible-memory logging."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import coco.bot as bot
from coco.telegram_memory import (
    log_incoming_message,
    log_outgoing_edit,
    log_outgoing_send,
)


def _read_entries(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_telegram_memory_appends_entries():
    path = Path(os.environ["COCO_TELEGRAM_MEMORY_LOG_PATH"])

    log_outgoing_send(
        text="hello from bot",
        chat_id=-1001,
        thread_id=10,
        message_id=21,
        source="unit_test",
    )
    log_outgoing_edit(
        text="edited text",
        chat_id=-1001,
        thread_id=10,
        message_id=21,
        source="unit_test",
    )
    log_incoming_message(
        kind="message",
        text="/status",
        chat_id=-1001,
        thread_id=10,
        message_id=22,
        from_user_id=1147817421,
        sender_chat_id=None,
        chat_type="supergroup",
    )

    entries = _read_entries(path)
    assert len(entries) == 3
    assert entries[0]["direction"] == "out_send"
    assert entries[1]["direction"] == "out_edit"
    assert entries[2]["direction"] == "in"
    assert entries[2]["text"] == "/status"


def test_telegram_memory_ignores_blank_text():
    path = Path(os.environ["COCO_TELEGRAM_MEMORY_LOG_PATH"])

    log_outgoing_send(
        text="   ",
        chat_id=-1001,
        thread_id=10,
        message_id=21,
        source="unit_test",
    )
    log_incoming_message(
        kind="message",
        text="",
        chat_id=-1001,
        thread_id=10,
        message_id=22,
        from_user_id=1,
        sender_chat_id=None,
        chat_type="supergroup",
    )

    entries = _read_entries(path)
    assert entries == []


def test_telegram_memory_prefers_coco_log_path(monkeypatch, tmp_path):
    legacy_path = tmp_path / "legacy.jsonl"
    coco_path = tmp_path / "coco.jsonl"
    monkeypatch.setenv("COCO_TELEGRAM_MEMORY_LOG_PATH", str(legacy_path))
    monkeypatch.setenv("COCO_TELEGRAM_MEMORY_LOG_PATH", str(coco_path))

    log_outgoing_send(
        text="hello from coco",
        chat_id=-1001,
        thread_id=10,
        message_id=21,
        source="unit_test",
    )

    assert coco_path.exists()
    assert not legacy_path.exists()
    entries = _read_entries(coco_path)
    assert len(entries) == 1
    assert entries[0]["text"] == "hello from coco"


@pytest.mark.asyncio
async def test_inbound_update_probe_logs_visible_caption(monkeypatch):
    captured: list[dict[str, object]] = []

    def _capture(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(bot, "log_incoming_message", _capture)

    msg = SimpleNamespace(
        text=None,
        caption="nvidia done",
        chat=SimpleNamespace(type="supergroup"),
        chat_id=-100123,
        message_thread_id=198,
        from_user=SimpleNamespace(id=1147817421),
        sender_chat=None,
        message_id=999,
    )
    update = SimpleNamespace(
        effective_message=msg,
        channel_post=None,
        message=msg,
        edited_channel_post=None,
        edited_message=None,
    )

    await bot.inbound_update_probe(update, SimpleNamespace())

    assert len(captured) == 1
    assert captured[0]["kind"] == "message"
    assert captured[0]["text"] == "nvidia done"
    assert captured[0]["chat_id"] == -100123
