"""Tests for /apps callback-driven autoresearch panel flow."""

from types import SimpleNamespace

import pytest
from telegram import InlineKeyboardMarkup

import coco.bot as bot
import coco.handlers.autoresearch as autoresearch


class _Chat:
    type = "supergroup"
    id = -100321


class _Message:
    def __init__(self, text: str = "Close more inbound leads") -> None:
        self.text = text
        self.chat = _Chat()
        self.chat_id = _Chat.id
        self.message_thread_id = 77
        self.message_id = 900


@pytest.mark.asyncio
async def test_autoresearch_outcome_text_capture_updates_panel(monkeypatch, tmp_path):
    monkeypatch.setattr(
        autoresearch,
        "_AUTORESEARCH_STATE_FILE",
        tmp_path / "autoresearch_state.json",
    )
    autoresearch.reset_autoresearch_state_for_tests()
    keyboard = InlineKeyboardMarkup([])
    replies: list[str] = []

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1147817421),
        effective_message=_Message(),
        effective_chat=_Chat(),
        message=_Message(),
    )
    context = SimpleNamespace(
        bot=object(),
        user_data={
            bot.STATE_KEY: bot.STATE_APPS_AUTORESEARCH_OUTCOME,
            bot.APPS_PENDING_THREAD_KEY: 77,
        },
    )

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )

    async def _build_autoresearch_panel_payload_for_topic(**_kwargs):
        return True, "autoresearch panel", keyboard, ""

    monkeypatch.setattr(
        bot,
        "_build_autoresearch_panel_payload_for_topic",
        _build_autoresearch_panel_payload_for_topic,
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.text_handler(update, context)

    state = autoresearch.get_autoresearch_state(user_id=1147817421, thread_id=77)
    assert state is not None
    assert state.outcome == "Close more inbound leads"
    assert context.user_data[bot.STATE_KEY] == ""
    assert replies[0] == "✅ Auto research outcome updated."
    assert replies[-1] == "autoresearch panel"
