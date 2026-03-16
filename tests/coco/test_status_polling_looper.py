"""Tests for looper dispatch in status polling."""

from types import SimpleNamespace

import pytest

import coco.handlers.status_polling as status_polling
from coco.handlers.looper import DueLooperPrompt


@pytest.mark.asyncio
async def test_emit_due_looper_prompt_sends_and_marks_run_started(monkeypatch):
    events: list[str] = []

    due = DueLooperPrompt(
        user_id=1,
        thread_id=10,
        window_id="@1",
        prompt_text="Continue loop",
        plan_path="plans/demo.md",
        keyword="done",
        instructions="",
        interval_seconds=600,
        prompt_count=2,
        deadline_at=0.0,
    )

    monkeypatch.setattr(status_polling, "get_interactive_window", lambda _u, _t: None)
    monkeypatch.setattr(
        status_polling,
        "stop_looper_if_expired",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        status_polling,
        "claim_due_looper_prompt",
        lambda **_kwargs: due,
    )

    async def _send_topic_text_to_window(
        *,
        user_id: int,
        thread_id: int | None,
        window_id: str,
        text: str,
        steer: bool = False,
    ):
        _ = user_id, thread_id, window_id, text, steer
        events.append("send")
        return True, ""

    monkeypatch.setattr(
        status_polling.session_manager,
        "send_topic_text_to_window",
        _send_topic_text_to_window,
    )
    monkeypatch.setattr(
        status_polling,
        "note_run_started",
        lambda **_kwargs: events.append("run_started"),
    )

    async def _unexpected_safe_send(*_args, **_kwargs):
        raise AssertionError("safe_send should not be called on successful looper send")

    monkeypatch.setattr(status_polling, "safe_send", _unexpected_safe_send)
    monkeypatch.setattr(status_polling, "emit_telemetry", lambda *_args, **_kwargs: None)

    await status_polling._emit_due_looper_prompt(
        bot=SimpleNamespace(),
        user_id=1,
        thread_id=10,
        window_id="@1",
    )

    assert events == ["send", "run_started"]


@pytest.mark.asyncio
async def test_emit_due_looper_prompt_notifies_when_time_limit_expires(monkeypatch):
    sent: list[str] = []

    monkeypatch.setattr(status_polling, "get_interactive_window", lambda _u, _t: None)
    monkeypatch.setattr(
        status_polling,
        "stop_looper_if_expired",
        lambda **_kwargs: SimpleNamespace(plan_path="plans/demo.md", keyword="done"),
    )

    def _unexpected_claim(**_kwargs):
        raise AssertionError("claim_due_looper_prompt should not run when loop expired")

    monkeypatch.setattr(status_polling, "claim_due_looper_prompt", _unexpected_claim)
    monkeypatch.setattr(
        status_polling.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid: -100,
    )

    async def _safe_send(_bot, _chat_id, text: str, **_kwargs):
        sent.append(text)

    monkeypatch.setattr(status_polling, "safe_send", _safe_send)

    await status_polling._emit_due_looper_prompt(
        bot=SimpleNamespace(),
        user_id=1,
        thread_id=10,
        window_id="@1",
    )

    assert sent
    assert "time limit reached" in sent[0].lower()
