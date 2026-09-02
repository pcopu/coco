from __future__ import annotations

from types import SimpleNamespace

import pytest

import coco.bot as bot


@pytest.mark.asyncio
async def test_controller_rpc_routes_chief_of_staff_without_transport_metadata(monkeypatch):
    calls: list[dict[str, object]] = []

    async def _handle(params: dict[str, object], *, bot):
        calls.append(params)
        return {"accepted": True, "status": "started"}

    monkeypatch.setattr(bot, "_handle_chief_of_staff_enqueue", _handle)
    result = await bot._handle_controller_rpc_request(
        {
            "method": "chief_of_staff/enqueue",
            "params": {
                "directive_id": "dir_voice_001",
                "control_chat_id": -1003841129251,
                "prompt": "Do the thing",
            },
        },
        bot=SimpleNamespace(),
    )

    assert result == {"accepted": True, "status": "started"}
    assert calls[0]["directive_id"] == "dir_voice_001"


@pytest.mark.asyncio
async def test_chief_of_staff_enqueue_fails_closed_without_control_binding(monkeypatch):
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: None,
    )

    result = await bot._handle_chief_of_staff_enqueue(
        {
            "directive_id": "dir_voice_001",
            "control_chat_id": -1003841129251,
            "prompt": "Do the thing",
        },
        bot=SimpleNamespace(),
    )

    assert result == {"accepted": False, "status": "control_unavailable"}


@pytest.mark.asyncio
async def test_chief_of_staff_enqueue_returns_busy_without_side_effect(monkeypatch):
    control = SimpleNamespace(
        user_id=1147817421,
        thread_id=1,
        chat_id=-1003841129251,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: control,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_for_thread",
        lambda *_args, **_kwargs: "@900038",
    )
    monkeypatch.setattr(bot, "capture_topic_ownership", lambda *_args, **_kwargs: object())

    async def _busy(*_args, **_kwargs):
        return True

    monkeypatch.setattr(bot, "_is_window_in_progress", _busy)

    async def _unexpected_send(**_kwargs):
        raise AssertionError("busy directives must remain in the durable external inbox")

    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        _unexpected_send,
    )

    result = await bot._handle_chief_of_staff_enqueue(
        {
            "directive_id": "dir_voice_001",
            "control_chat_id": -1003841129251,
            "prompt": "Do the thing",
        },
        bot=SimpleNamespace(),
    )

    assert result == {"accepted": False, "status": "busy", "retry_after": 30}


@pytest.mark.asyncio
async def test_chief_of_staff_enqueue_dispatches_idle_control_topic(monkeypatch):
    control = SimpleNamespace(
        user_id=1147817421,
        thread_id=1,
        chat_id=-1003841129251,
    )
    ownership = object()
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: control,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_for_thread",
        lambda *_args, **_kwargs: "@900038",
    )
    monkeypatch.setattr(
        bot,
        "capture_topic_ownership",
        lambda *_args, **_kwargs: ownership,
    )

    async def _idle(*_args, **_kwargs):
        return False

    monkeypatch.setattr(bot, "_is_window_in_progress", _idle)
    sent: list[dict[str, object]] = []

    async def _send(**kwargs):
        sent.append(kwargs)
        kwargs["dispatch_state"].started_new_turn = True
        kwargs["dispatch_state"].transport_dispatch_started = True
        return True, ""

    monkeypatch.setattr(bot.session_manager, "send_topic_text_to_window", _send)
    started: list[dict[str, object]] = []
    monkeypatch.setattr(bot, "note_run_started", lambda **kwargs: started.append(kwargs))

    result = await bot._handle_chief_of_staff_enqueue(
        {
            "directive_id": "dir_voice_001",
            "control_chat_id": -1003841129251,
            "prompt": "Do the thing",
        },
        bot=SimpleNamespace(),
    )

    assert result == {"accepted": True, "status": "started"}
    assert sent[0]["text"] == "Do the thing"
    assert sent[0]["force_new_turn"] is True
    assert sent[0]["topic_ownership"] is ownership
    assert started[0]["source"] == "chief_of_staff"
