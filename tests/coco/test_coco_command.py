"""Tests for /coco control-topic command behavior."""

import asyncio
from types import SimpleNamespace

import pytest

import coco.bot as bot
from coco.session import SessionManager


def _make_update(
    text: str,
    *,
    thread_id: int = 1,
    user_id: int = 1147817421,
    is_forum: bool = False,
):
    chat = SimpleNamespace(type="supergroup", id=-100123, is_forum=is_forum)
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


def test_get_thread_id_uses_general_topic_sentinel_for_forum_general():
    update = _make_update("hello", thread_id=None, is_forum=True)

    assert bot._get_thread_id(update) == 1


def test_get_thread_id_keeps_explicit_general_topic_id_without_forum_metadata():
    update = _make_update("hello", thread_id=1, is_forum=False)

    assert bot._get_thread_id(update) == 1


@pytest.mark.asyncio
async def test_startup_migrates_named_control_history_and_notifies_both_topics(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(bot.config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(bot.config, "sessions_path", tmp_path / "sessions")
    monkeypatch.setattr(bot.config, "browse_root", tmp_path)
    manager = SessionManager()
    state = manager.get_window_state("@42")
    state.cwd = "/projects/old-control"
    state.window_name = "old-control"
    state.codex_thread_id = "codex-history-123"
    manager.bind_topic_to_codex_thread(
        user_id=1147817421,
        thread_id=77,
        chat_id=-100123,
        codex_thread_id=state.codex_thread_id,
        cwd=state.cwd,
        display_name=state.window_name,
        window_id="@42",
    )
    manager.coco_control_topic = bot.CocoControlTopic(
        user_id=1147817421,
        thread_id=77,
        chat_id=-100123,
    )
    manager._save_state()
    sends: list[tuple[int, str, int | None]] = []

    monkeypatch.setattr(bot, "session_manager", manager)
    async def _safe_send(_bot, chat_id: int, text: str, **kwargs):
        sends.append((chat_id, text, kwargs.get("message_thread_id")))

    monkeypatch.setattr(bot, "safe_send", _safe_send)

    migration = await bot._migrate_coco_control_to_general(SimpleNamespace())

    assert migration is not None
    general = manager.resolve_topic_binding(1147817421, 1, chat_id=-100123)
    assert general is not None
    assert general.window_id == "@42"
    assert general.codex_thread_id == "codex-history-123"
    assert general.cwd == str(tmp_path / "_coco" / "chat-100123-thread-1")
    assert [thread_id for _chat, _text, thread_id in sends] == [77, 1]
    assert "moved permanently to General" in sends[0][1]
    assert "permanent CoCo control channel" in sends[1][1]


@pytest.mark.asyncio
async def test_startup_normalizes_existing_general_workspace_without_losing_history(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(bot.config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(bot.config, "sessions_path", tmp_path / "sessions")
    monkeypatch.setattr(bot.config, "browse_root", tmp_path)
    manager = SessionManager()
    state = manager.get_window_state("@42")
    state.cwd = "/projects/legacy-control"
    state.codex_thread_id = "codex-history-123"
    manager.bind_topic_to_codex_thread(
        user_id=1147817421,
        thread_id=1,
        chat_id=-100123,
        codex_thread_id=state.codex_thread_id,
        cwd=state.cwd,
        display_name="legacy-control",
        window_id="@42",
    )
    manager.set_coco_control_topic(1147817421, 1, chat_id=-100123)
    monkeypatch.setattr(bot, "session_manager", manager)

    migration = await bot._migrate_coco_control_to_general(SimpleNamespace())
    binding = manager.resolve_topic_binding(1147817421, 1, chat_id=-100123)

    expected = str(tmp_path / "_coco" / "chat-100123-thread-1")
    assert migration is None
    assert binding is not None
    assert binding.cwd == expected
    assert binding.codex_thread_id == "codex-history-123"
    assert manager.get_window_state("@42").cwd == expected


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
async def test_coco_command_makes_forum_general_the_default_control_topic(
    monkeypatch,
    tmp_path,
):
    update = _make_update("/coco", thread_id=None, is_forum=True)
    replies: list[tuple[str, object]] = []

    monkeypatch.setattr(bot.config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(bot.config, "sessions_path", tmp_path / "sessions")
    monkeypatch.setattr(bot.config, "browse_root", tmp_path)
    manager = SessionManager()
    monkeypatch.setattr(bot, "session_manager", manager)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)

    async def _safe_reply(_message, text: str, **kwargs):
        replies.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.coco_command(update, SimpleNamespace(user_data={}))

    assert manager.is_coco_control_topic(
        1147817421,
        1,
        chat_id=-100123,
    )
    binding = manager.resolve_topic_binding(1147817421, 1, chat_id=-100123)
    assert binding is not None
    assert binding.window_id
    assert binding.cwd == str(tmp_path / "_coco" / "chat-100123-thread-1")
    assert len(replies) == 1
    text, markup = replies[0]
    assert "This topic is currently the singleton CoCo control topic." in text
    assert [button.text for row in markup.inline_keyboard for button in row] == [
        "Refresh"
    ]


@pytest.mark.asyncio
async def test_coco_command_rejects_named_topic_as_control(
    monkeypatch,
    tmp_path,
):
    update = _make_update("/coco", thread_id=77, is_forum=True)
    replies: list[tuple[str, object]] = []

    monkeypatch.setattr(bot.config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(bot.config, "sessions_path", tmp_path / "sessions")
    monkeypatch.setattr(bot.config, "browse_root", tmp_path)
    manager = SessionManager()
    manager.set_coco_control_topic(1147817421, 1, chat_id=-100123)
    monkeypatch.setattr(bot, "session_manager", manager)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)

    async def _safe_reply(_message, text: str, **kwargs):
        replies.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.coco_command(update, SimpleNamespace(user_data={}))

    assert manager.is_coco_control_topic(1147817421, 1, chat_id=-100123)
    assert manager.resolve_topic_binding(1147817421, 77, chat_id=-100123) is None
    assert replies == [
        (
            "ℹ️ CoCo control is permanently assigned to General. Use `/coco` there.",
            None,
        )
    ]


@pytest.mark.asyncio
async def test_coco_control_keyboard_never_offers_reassignment(monkeypatch, tmp_path):
    update = _make_update("/coco", thread_id=1, is_forum=True)
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
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda: bot.CocoControlTopic(1147817421, 1, -100123),
    )
    monkeypatch.setattr(bot.session_manager, "is_coco_control_topic", lambda *_args, **_kwargs: True)

    async def _safe_reply(_message, text: str, **kwargs):
        replies.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.coco_command(update, SimpleNamespace(user_data={}))

    assert len(replies) == 1
    text, markup = replies[0]
    assert "CoCo Control Topic" in text
    assert "This topic is currently the singleton CoCo control topic." in text
    assert str(tmp_path / "_coco" / "chat-100123-thread-1") in text
    assert markup is not None
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert labels == ["Refresh"]
    forced_labels = [
        button.text
        for row in bot._build_coco_control_keyboard(is_current=False).inline_keyboard
        for button in row
    ]
    assert forced_labels == ["Refresh"]


@pytest.mark.asyncio
async def test_stale_named_set_callback_cannot_reassign_control(monkeypatch, tmp_path):
    chat = SimpleNamespace(type="supergroup", id=-100123, is_forum=True)
    message = SimpleNamespace(message_thread_id=77, chat=chat)
    answers: list[tuple[str, bool]] = []
    edits: list[str] = []

    async def _answer(text: str, *, show_alert: bool = False):
        answers.append((text, show_alert))

    query = SimpleNamespace(
        data=bot.CB_COCO_SET,
        message=message,
        answer=_answer,
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=chat,
        effective_user=SimpleNamespace(id=1147817421),
        effective_message=message,
    )

    monkeypatch.setattr(bot.config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(bot.config, "sessions_path", tmp_path / "sessions")
    manager = SessionManager()
    manager.set_coco_control_topic(1147817421, 1, chat_id=-100123)
    monkeypatch.setattr(bot, "session_manager", manager)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)

    async def _safe_edit(_query, text: str, **_kwargs):
        edits.append(text)

    monkeypatch.setattr(bot, "safe_edit", _safe_edit)

    await bot.callback_handler(update, SimpleNamespace(user_data={}, bot=None))

    assert manager.is_coco_control_topic(1147817421, 1, chat_id=-100123)
    assert manager.resolve_topic_binding(1147817421, 77, chat_id=-100123) is None
    assert edits == [
        "ℹ️ CoCo control is permanently assigned to General. Use `/coco` there."
    ]
    assert answers == [("CoCo is fixed to General.", True)]


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
        bot,
        "capture_topic_ownership",
        lambda *_args, **_kwargs: SimpleNamespace(
            window_id="@88",
            codex_thread_id="thread-88",
            machine_id="test-machine",
            cwd="/env/fmwblog",
        ),
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


@pytest.mark.asyncio
async def test_coco_command_steer_drops_target_after_rebind_during_send(monkeypatch):
    """A direct steer captured for owner A must not dispatch into owner B."""
    user_id = 1147817421
    target_thread_id = 88
    chat_id = -100123
    owner_a = SimpleNamespace(
        window_id="@target-a",
        codex_thread_id="target-thread-a",
        machine_id="machine-a",
        cwd="/workspace/a",
    )
    owner_b = SimpleNamespace(
        window_id="@target-b",
        codex_thread_id="target-thread-b",
        machine_id="machine-b",
        cwd="/workspace/b",
    )
    current_owner = {"value": owner_a}
    capture_calls: list[tuple[int, int, int | None]] = []
    send_started = asyncio.Event()
    release_send = asyncio.Event()
    dispatched: list[str] = []
    update = _make_update("/coco steer 88 Focus on the PDF bug")
    replies: list[str] = []

    def _capture_topic_ownership(
        capture_user_id: int,
        capture_thread_id: int,
        capture_chat_id: int | None,
    ):
        capture_calls.append((capture_user_id, capture_thread_id, capture_chat_id))
        return owner_a

    async def _send_topic_text_to_window(
        *,
        window_id: str,
        topic_ownership=None,
        **_kwargs,
    ):
        send_started.set()
        await release_send.wait()
        if topic_ownership is None:
            # Model the vulnerable call: without a snapshot, dispatch resolves
            # the rebound canonical target (owner B).
            dispatched.append(current_owner["value"].window_id)
            return True, ""
        if topic_ownership != current_owner["value"]:
            return False, "stale topic owner; request was not sent"
        dispatched.append(window_id)
        return True, ""

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "is_coco_control_topic",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "iter_topic_bindings",
        lambda: iter(
            [
                (
                    user_id,
                    chat_id,
                    77,
                    SimpleNamespace(display_name="coco-control", cwd="/env/_coco/ctl"),
                ),
                (
                    user_id,
                    chat_id,
                    target_thread_id,
                    SimpleNamespace(display_name="fmwblog", cwd="/workspace/a"),
                ),
            ]
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_window_for_thread",
        lambda _uid, tid, **_kwargs: (
            owner_a.window_id if tid == target_thread_id else "@control"
        ),
    )
    monkeypatch.setattr(bot, "capture_topic_ownership", _capture_topic_ownership)
    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        _send_topic_text_to_window,
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    steer_task = asyncio.create_task(
        bot.coco_command(update, SimpleNamespace(user_data={}))
    )
    await asyncio.wait_for(send_started.wait(), timeout=1)
    current_owner["value"] = owner_b
    release_send.set()
    await asyncio.wait_for(steer_task, timeout=1)

    assert not any(window_id == owner_b.window_id for window_id in dispatched)
    assert capture_calls == [(user_id, target_thread_id, chat_id)]


async def _async_result(result):
    return result
