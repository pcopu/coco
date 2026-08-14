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
            "_apps_pending_user_id": 1147817421,
            "_apps_pending_chat_id": -100321,
            "_apps_pending_ownership": {
                "window_id": "@77",
                "codex_thread_id": "codex-77",
                "machine_id": "local",
                "cwd": "/tmp/project",
            },
        },
    )

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(bot, "is_topic_ownership_current", lambda *_args, **_kwargs: True)

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

    state = autoresearch.get_autoresearch_state(
        user_id=1147817421,
        chat_id=-100321,
        thread_id=77,
    )
    assert state is not None
    assert state.outcome == "Close more inbound leads"
    assert context.user_data[bot.STATE_KEY] == ""
    assert replies[0] == "✅ Auto research outcome updated."
    assert replies[-1] == "autoresearch panel"


@pytest.mark.asyncio
async def test_autoresearch_outcome_reply_from_other_chat_does_not_mutate_state(monkeypatch):
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1147817421),
        effective_message=SimpleNamespace(
            text="cross-chat outcome",
            chat=_Chat(),
            chat_id=_Chat.id,
            message_thread_id=77,
        ),
        effective_chat=_Chat(),
        message=SimpleNamespace(
            text="cross-chat outcome",
            chat=_Chat(),
            chat_id=_Chat.id,
            message_thread_id=77,
        ),
    )
    context = SimpleNamespace(
        bot=object(),
        user_data={
            bot.STATE_KEY: bot.STATE_APPS_AUTORESEARCH_OUTCOME,
            bot.APPS_PENDING_THREAD_KEY: 77,
            "_apps_pending_user_id": 1147817421,
            "_apps_pending_chat_id": -100999,
            "_apps_pending_ownership": {
                "window_id": "@77",
                "codex_thread_id": "codex-77",
                "machine_id": "local",
                "cwd": "/tmp/project",
            },
        },
    )
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot,
        "set_autoresearch_outcome",
        lambda **kwargs: calls.append(kwargs),
    )
    async def _safe_reply(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.text_handler(update, context)

    assert calls == []
    assert bot.STATE_KEY not in context.user_data


@pytest.mark.asyncio
async def test_general_admin_outcome_reply_writes_canonical_control_owner(monkeypatch):
    owner_user_id = 100
    admin_user_id = 200
    chat_id = _Chat.id
    message = SimpleNamespace(
        text="canonical outcome",
        chat=_Chat(),
        chat_id=chat_id,
        message_thread_id=1,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=admin_user_id),
        effective_message=message,
        effective_chat=_Chat(),
        message=message,
    )
    context = SimpleNamespace(
        bot=object(),
        user_data={
            bot.STATE_KEY: bot.STATE_APPS_AUTORESEARCH_OUTCOME,
            bot.APPS_PENDING_THREAD_KEY: 1,
            "_apps_pending_user_id": owner_user_id,
            "_apps_pending_chat_id": chat_id,
            "_apps_pending_ownership": {
                "window_id": "@control",
                "codex_thread_id": "control-thread",
                "machine_id": "local",
                "cwd": "/tmp/control",
            },
        },
    )
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot,
        "_coco_control_owner_user_id",
        lambda _user_id, _chat_id: owner_user_id,
    )
    monkeypatch.setattr(bot, "_can_coco_control_target", lambda **_kwargs: True)
    monkeypatch.setattr(bot, "is_topic_ownership_current", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        bot,
        "set_autoresearch_outcome",
        lambda **kwargs: calls.append(kwargs),
    )

    async def _safe_reply(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.text_handler(update, context)

    assert calls == [
        {
            "user_id": owner_user_id,
            "chat_id": chat_id,
            "thread_id": 1,
            "outcome": "canonical outcome",
        }
    ]


@pytest.mark.asyncio
async def test_revoked_general_admin_cannot_consume_autoresearch_prompt(monkeypatch):
    owner_user_id = 100
    admin_user_id = 200
    chat_id = _Chat.id
    message = SimpleNamespace(
        text="revoked outcome",
        chat=_Chat(),
        chat_id=chat_id,
        message_thread_id=1,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=admin_user_id),
        effective_message=message,
        effective_chat=_Chat(),
        message=message,
    )
    context = SimpleNamespace(
        bot=object(),
        user_data={
            bot.STATE_KEY: bot.STATE_APPS_AUTORESEARCH_OUTCOME,
            bot.APPS_PENDING_THREAD_KEY: 1,
            bot.APPS_PENDING_USER_KEY: owner_user_id,
            bot.APPS_PENDING_CHAT_KEY: chat_id,
            bot.APPS_PENDING_OWNERSHIP_KEY: {
                "window_id": "@control",
                "codex_thread_id": "control-thread",
                "machine_id": "local",
                "cwd": "/tmp/control",
            },
        },
    )
    outcome_calls: list[dict[str, object]] = []
    auth_calls: list[dict[str, object]] = []
    routing_calls: list[tuple[object, ...]] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(
        bot,
        "_ensure_default_coco_general_control",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(owner_user_id, 1, chat_id),
    )
    monkeypatch.setattr(
        bot,
        "_coco_control_owner_user_id",
        lambda _user_id, _chat_id: owner_user_id,
    )
    monkeypatch.setattr(bot, "is_topic_ownership_current", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        bot,
        "_can_coco_control_target",
        lambda **kwargs: auth_calls.append(kwargs) or False,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *args, **_kwargs: routing_calls.append(args),
    )
    monkeypatch.setattr(
        bot,
        "set_autoresearch_outcome",
        lambda **kwargs: outcome_calls.append(kwargs),
    )
    replies: list[str] = []

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.text_handler(update, context)

    assert outcome_calls == []
    assert routing_calls == []
    assert auth_calls == [
        {
            "caller_user_id": admin_user_id,
            "target_user_id": owner_user_id,
            "chat_id": chat_id,
        }
    ]
    assert context.user_data.get(bot.STATE_KEY) is None
    assert replies == [f"❌ {bot._COCO_CONTROL_PERMISSION_DENIED_TEXT}"]
