"""Tests for /coco control-topic command behavior."""

from pathlib import Path
from types import SimpleNamespace

import pytest

import coco.bot as bot


def _make_update(text: str, *, thread_id: int = 77, user_id: int = 1147817421):
    chat = SimpleNamespace(type="supergroup", id=-100123)
    message = SimpleNamespace(
        text=text,
        message_thread_id=thread_id,
        chat=chat,
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_message=message,
        effective_chat=chat,
        message=message,
    )


@pytest.mark.asyncio
async def test_coco_command_requires_named_topic(monkeypatch):
    update = _make_update("/coco", thread_id=None)
    replies: list[tuple[str, object]] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)

    async def _safe_reply(_message, text: str, **kwargs):
        replies.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.coco_command(update, SimpleNamespace(user_data={}))

    assert replies == [("❌ Use `/coco` inside a named topic.", None)]


@pytest.mark.asyncio
async def test_coco_command_shows_confirmation_panel(monkeypatch, tmp_path):
    update = _make_update("/coco")
    replies: list[tuple[str, object]] = []
    binding = SimpleNamespace(
        cwd="",
        display_name="",
        window_id="",
    )

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(bot.config, "browse_root", tmp_path)
    monkeypatch.setattr(bot.session_manager, "set_group_chat_id", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bot.session_manager, "ensure_topic_binding", lambda *_args, **_kwargs: binding)
    monkeypatch.setattr(bot.session_manager, "get_coco_control_topic", lambda: None)
    monkeypatch.setattr(bot.session_manager, "is_coco_control_topic", lambda *_args, **_kwargs: False)

    async def _safe_reply(_message, text: str, **kwargs):
        replies.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.coco_command(update, SimpleNamespace(user_data={}))

    assert len(replies) == 1
    text, markup = replies[0]
    assert "CoCo Control Topic" in text
    assert "Current CoCo topic: `(none)`" in text
    assert str(tmp_path / "_coco" / "chat-100123-thread-77") in text
    assert markup is not None
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert "Set As CoCo" in labels
    assert "Cancel" in labels


@pytest.mark.asyncio
async def test_coco_command_topics_lists_other_topics(monkeypatch):
    update = _make_update("/coco topics")
    replies: list[str] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(bot.session_manager, "set_group_chat_id", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bot.session_manager, "is_coco_control_topic", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        bot.session_manager,
        "iter_topic_bindings",
        lambda: iter(
            [
                (1147817421, -100123, 77, SimpleNamespace(display_name="coco-control", cwd="/env/_coco/ctl")),
                (1147817421, -100123, 88, SimpleNamespace(display_name="fmwblog", cwd="/env/fmwblog")),
                (1147817421, -100123, 99, SimpleNamespace(display_name="bottleshot", cwd="/env/bottleshot")),
            ]
        ),
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.coco_command(update, SimpleNamespace(user_data={}))

    assert len(replies) == 1
    assert "CoCo control topic inventory" in replies[0]
    assert "thread `88`" in replies[0]
    assert "`fmwblog`" in replies[0]
    assert "thread `99`" in replies[0]


@pytest.mark.asyncio
async def test_coco_command_steer_sends_to_target_topic(monkeypatch):
    update = _make_update("/coco steer 88 Focus on the PDF bug")
    replies: list[str] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(bot.session_manager, "set_group_chat_id", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bot.session_manager, "is_coco_control_topic", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        bot.session_manager,
        "iter_topic_bindings",
        lambda: iter(
            [
                (1147817421, -100123, 77, SimpleNamespace(display_name="coco-control", cwd="/env/_coco/ctl")),
                (1147817421, -100123, 88, SimpleNamespace(display_name="fmwblog", cwd="/env/fmwblog")),
            ]
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_window_for_thread",
        lambda _uid, tid, **_kwargs: "@88" if tid == 88 else "@77",
    )
    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        lambda **kwargs: _async_result((True, f"sent:{kwargs['thread_id']}:{kwargs['text']}")),
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.coco_command(update, SimpleNamespace(user_data={}))

    assert replies == ["✅ Steered topic `88` (`fmwblog`)."]


async def _async_result(result):
    return result
