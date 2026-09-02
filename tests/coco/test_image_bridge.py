"""Tests for Codex image bridge helpers."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import coco.bot as bot

_REMOTE_ATTACHMENT_REJECTION_TEXT = (
    "❌ Attachments to remote sessions are not supported yet. "
    "Send this attachment in a local topic."
)


def test_pick_image_prompt_prefers_caption():
    assert bot._pick_image_prompt("Check this chart") == "Check this chart"
    assert bot._pick_image_prompt("  A  ") == "A"


def test_pick_image_prompt_default_when_caption_missing():
    assert bot._pick_image_prompt(None) == "Please analyze this image."
    assert bot._pick_image_prompt("   ") == "Please analyze this image."


def test_build_codex_image_resume_cmd():
    cmd = bot._build_codex_image_resume_cmd(
        "/usr/bin/codex",
        "session-123",
        Path("/tmp/image.png"),
        "Inspect this",
        model_slug="gpt-5.6-luna",
        reasoning_effort="max",
        service_tier="fast",
    )
    assert cmd == [
        "/usr/bin/codex",
        "exec",
        "resume",
        "session-123",
        "--skip-git-repo-check",
        "--model",
        "gpt-5.6-luna",
        "--config",
        'model_reasoning_effort="max"',
        "--config",
        'service_tier="fast"',
        "-i",
        "/tmp/image.png",
        "Inspect this",
    ]


def test_tail_command_output_truncates_tail():
    raw = ("x" * 40).encode()
    tail = bot._tail_command_output(raw, limit=20)
    assert tail.startswith("… ")
    assert tail.endswith("x" * 18)


class _FakePhotoFile:
    def __init__(self, *, download_started: asyncio.Event, release_download: asyncio.Event):
        self.download_started = download_started
        self.release_download = release_download
        self.paths: list[Path] = []

    async def download_to_drive(self, path: Path) -> None:
        self.download_started.set()
        await self.release_download.wait()
        path.write_bytes(b"JPEGDATA")
        self.paths.append(path)


class _FakePhoto:
    file_unique_id = "photo-123"

    def __init__(self, tg_file: _FakePhotoFile) -> None:
        self._tg_file = tg_file

    async def get_file(self) -> _FakePhotoFile:
        return self._tg_file


_PENDING_PHOTO_CALLER_ID = 1147817421
_PENDING_PHOTO_CONTROL_OWNER_ID = 9001
_PENDING_PHOTO_CHAT_ID = -100123
_PENDING_PHOTO_THREAD_ID = 77


class _ImmediatePhotoFile:
    def __init__(self, payload: bytes = b"JPEGDATA") -> None:
        self.payload = payload
        self.paths: list[Path] = []

    async def download_to_drive(self, path: Path) -> None:
        path.write_bytes(self.payload)
        self.paths.append(path)


class _ImmediatePhoto:
    file_unique_id = "pending-photo-123"

    def __init__(self, tg_file: _ImmediatePhotoFile) -> None:
        self._tg_file = tg_file

    async def get_file(self) -> _ImmediatePhotoFile:
        return self._tg_file


class _PendingPhotoChat:
    type = "supergroup"
    id = _PENDING_PHOTO_CHAT_ID
    is_forum = True

    async def send_action(self, _action: str) -> None:
        return None


def _pending_photo_update(
    photo: object,
    *,
    caption: str | None = "Use this image",
):
    chat = _PendingPhotoChat()
    message = SimpleNamespace(
        photo=[photo],
        caption=caption,
        message_thread_id=None,
        chat=chat,
        chat_id=chat.id,
        message_id=123,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=_PENDING_PHOTO_CALLER_ID),
        effective_message=message,
        effective_chat=chat,
        message=message,
    )
    return update


def _pending_photo_context(
    ownership: bot.TopicOwnership,
    *,
    created_at: float | None = None,
    owner_user_id: int = _PENDING_PHOTO_CALLER_ID,
    thread_id: int = _PENDING_PHOTO_THREAD_ID,
):
    return SimpleNamespace(
        bot=object(),
        user_data={
            "_coco_dashboard_steer": {
                "owner_user_id": owner_user_id,
                "chat_id": _PENDING_PHOTO_CHAT_ID,
                "thread_id": thread_id,
                "created_at": (
                    bot.time.monotonic()
                    if created_at is None
                    else created_at
                ),
                "ownership": {
                    "window_id": ownership.window_id,
                    "codex_thread_id": ownership.codex_thread_id,
                    "machine_id": ownership.machine_id,
                    "cwd": ownership.cwd,
                },
            }
        },
    )


def _install_pending_photo_routing(
    monkeypatch,
    *,
    target_ownership: bot.TopicOwnership,
    ownership_current: bool = True,
):
    target_binding = SimpleNamespace(
        window_id=target_ownership.window_id,
        codex_thread_id=target_ownership.codex_thread_id,
        machine_id=target_ownership.machine_id,
        cwd=target_ownership.cwd,
    )
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(bot.config, "session_provider", "other")
    monkeypatch.setattr(
        bot,
        "_ensure_default_coco_general_control",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(
            _PENDING_PHOTO_CONTROL_OWNER_ID,
            bot.GENERAL_TOPIC_THREAD_ID,
            _PENDING_PHOTO_CHAT_ID,
        ),
    )
    monkeypatch.setattr(
        bot,
        "_can_coco_control_target",
        lambda *, caller_user_id, target_user_id, **_kwargs: int(caller_user_id)
        == int(target_user_id),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_for_thread",
        lambda user_id, thread_id, **_kwargs: (
            target_ownership.window_id
            if (int(user_id), int(thread_id))
            == (_PENDING_PHOTO_CALLER_ID, _PENDING_PHOTO_THREAD_ID)
            else pytest.fail("photo must resolve the pending target topic")
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda user_id, thread_id, **_kwargs: (
            target_binding
            if (int(user_id), int(thread_id))
            == (_PENDING_PHOTO_CALLER_ID, _PENDING_PHOTO_THREAD_ID)
            else pytest.fail("photo must validate the pending target binding")
        ),
    )
    monkeypatch.setattr(
        bot,
        "capture_topic_ownership",
        lambda user_id, thread_id, _chat_id: (
            target_ownership
            if (int(user_id), int(thread_id))
            == (_PENDING_PHOTO_CALLER_ID, _PENDING_PHOTO_THREAD_ID)
            else pytest.fail("photo must capture the pending target ownership")
        ),
    )
    monkeypatch.setattr(
        bot,
        "is_topic_ownership_current",
        lambda *_args, **_kwargs: ownership_current,
    )
    monkeypatch.setattr(bot, "_local_machine_identity", lambda: ("local-node", "Local"))
    monkeypatch.setattr(bot, "clear_status_msg_info", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bot, "note_run_started", lambda **_kwargs: None)
    async def _set_eyes_reaction(_message):
        return None

    monkeypatch.setattr(bot, "_set_eyes_reaction", _set_eyes_reaction)
    return target_binding


@pytest.mark.asyncio
async def test_photo_handler_pending_steer_routes_local_self_target_and_consumes_once(
    monkeypatch,
    tmp_path,
):
    """A General photo steers the caller's local topic despite another owner."""
    tg_file = _ImmediatePhotoFile()
    update = _pending_photo_update(_ImmediatePhoto(tg_file))
    target_ownership = bot.TopicOwnership(
        window_id="@photo-target",
        codex_thread_id="codex-photo-target",
        machine_id="local-node",
        cwd="/workspace/photo-target",
    )
    context = _pending_photo_context(target_ownership)
    _install_pending_photo_routing(
        monkeypatch,
        target_ownership=target_ownership,
    )
    monkeypatch.setattr(bot, "_IMAGES_DIR", tmp_path, raising=False)
    sent: list[dict[str, object]] = []
    replies: list[str] = []

    async def _send_topic_text_to_window(**kwargs):
        sent.append(kwargs)
        return True, ""

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        _send_topic_text_to_window,
    )
    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.photo_handler(update, context)

    assert tg_file.paths
    assert replies == []
    assert len(sent) == 1
    assert sent[0]["user_id"] == _PENDING_PHOTO_CALLER_ID
    assert sent[0]["thread_id"] == _PENDING_PHOTO_THREAD_ID
    assert sent[0]["window_id"] == target_ownership.window_id
    assert sent[0]["topic_ownership"] == target_ownership
    assert "_coco_dashboard_steer" not in context.user_data


@pytest.mark.asyncio
async def test_photo_handler_pending_steer_rejects_remote_target_before_download(
    monkeypatch,
):
    """A remote pending target is rejected and cannot arm the following text."""
    class _UnexpectedPhoto:
        file_unique_id = "remote-pending-photo"

        async def get_file(self):
            raise AssertionError("remote pending photo must not be downloaded")

    update = _pending_photo_update(_UnexpectedPhoto())
    target_ownership = bot.TopicOwnership(
        window_id="@remote-photo-target",
        codex_thread_id="codex-remote-photo-target",
        machine_id="remote-node",
        cwd="/remote/photo-target",
    )
    context = _pending_photo_context(target_ownership)
    _install_pending_photo_routing(
        monkeypatch,
        target_ownership=target_ownership,
    )
    replies: list[str] = []

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.photo_handler(update, context)

    assert replies == [_REMOTE_ATTACHMENT_REJECTION_TEXT]
    assert "_coco_dashboard_steer" not in context.user_data


@pytest.mark.asyncio
async def test_photo_handler_pending_steer_expired_target_fails_closed_before_download(
    monkeypatch,
):
    """Expired dashboard photo intents use the existing exact UX and are consumed."""
    class _UnexpectedPhoto:
        file_unique_id = "expired-pending-photo"

        async def get_file(self):
            raise AssertionError("expired pending photo must not be downloaded")

    update = _pending_photo_update(_UnexpectedPhoto())
    target_ownership = bot.TopicOwnership(
        window_id="@expired-photo-target",
        codex_thread_id="codex-expired-photo-target",
        machine_id="local-node",
        cwd="/workspace/expired-photo-target",
    )
    context = _pending_photo_context(
        target_ownership,
        created_at=bot.time.monotonic() - bot._COCO_DASHBOARD_STEER_TTL_SECONDS - 1,
    )
    _install_pending_photo_routing(
        monkeypatch,
        target_ownership=target_ownership,
    )
    replies: list[str] = []

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.photo_handler(update, context)

    assert replies == [bot._COCO_DASHBOARD_STEER_EXPIRED_TEXT]
    assert "_coco_dashboard_steer" not in context.user_data


@pytest.mark.asyncio
async def test_photo_handler_pending_steer_stale_target_fails_closed_before_download(
    monkeypatch,
):
    """A rebound dashboard target gets the normal stale-target response."""
    class _UnexpectedPhoto:
        file_unique_id = "stale-pending-photo"

        async def get_file(self):
            raise AssertionError("stale pending photo must not be downloaded")

    update = _pending_photo_update(_UnexpectedPhoto())
    target_ownership = bot.TopicOwnership(
        window_id="@stale-photo-target",
        codex_thread_id="codex-stale-photo-target",
        machine_id="local-node",
        cwd="/workspace/stale-photo-target",
    )
    context = _pending_photo_context(target_ownership)
    _install_pending_photo_routing(
        monkeypatch,
        target_ownership=target_ownership,
        ownership_current=False,
    )
    replies: list[str] = []

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.photo_handler(update, context)

    assert replies == ["❌ That dashboard target changed. Refresh /coco and try again."]
    assert "_coco_dashboard_steer" not in context.user_data


@pytest.mark.asyncio
async def test_photo_handler_rejects_remote_general_before_download(monkeypatch):
    """A controller must not pass its local image path to a remote General session."""
    chat_id = -100123
    owner_user_id = 100
    chat = SimpleNamespace(type="supergroup", id=chat_id, is_forum=True)

    class _UnexpectedPhoto:
        file_unique_id = "remote-general-photo"

        async def get_file(self):
            raise AssertionError("remote General photo must not be downloaded")

    message = SimpleNamespace(
        photo=[_UnexpectedPhoto()],
        caption="Inspect this",
        message_thread_id=None,
        chat=chat,
        chat_id=chat_id,
        message_id=123,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=owner_user_id),
        effective_message=message,
        effective_chat=chat,
        message=message,
    )
    replies: list[str] = []
    remote_ownership = bot.TopicOwnership(
        window_id="@remote-general",
        codex_thread_id="codex-remote-general",
        machine_id="remote-node",
        cwd="/remote/workspace",
    )

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: True)
    monkeypatch.setattr(bot, "_can_coco_control_target", lambda **_kwargs: True)
    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(bot.session_manager, "set_group_chat_id", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(owner_user_id, 1, chat_id),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_for_thread",
        lambda *_args, **_kwargs: remote_ownership.window_id,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda *_args, **_kwargs: SimpleNamespace(
            window_id=remote_ownership.window_id,
            codex_thread_id=remote_ownership.codex_thread_id,
            machine_id=remote_ownership.machine_id,
            cwd=remote_ownership.cwd,
        ),
    )
    monkeypatch.setattr(bot, "capture_topic_ownership", lambda *_args, **_kwargs: remote_ownership)
    monkeypatch.setattr(bot, "_local_machine_identity", lambda: ("controller-node", "Controller"))

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.photo_handler(update, SimpleNamespace(bot=object(), user_data={}))

    assert replies == [_REMOTE_ATTACHMENT_REJECTION_TEXT]


@pytest.mark.asyncio
async def test_photo_handler_activates_default_general_control_before_lookup(monkeypatch):
    user_id = 1147817421
    chat_id = -100123
    events: list[str] = []

    class _Chat:
        type = "supergroup"
        id = chat_id
        is_forum = True

    message = SimpleNamespace(
        photo=[object()],
        message_thread_id=None,
        chat=_Chat(),
        chat_id=chat_id,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_message=message,
        effective_chat=message.chat,
        message=message,
    )

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot,
        "_ensure_default_coco_general_control",
        lambda **_kwargs: events.append("activate"),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(user_id, 1, chat_id),
    )

    def _lookup(*_args, **_kwargs):
        events.append("lookup")
        return None

    monkeypatch.setattr(bot.session_manager, "get_window_for_thread", _lookup)

    async def _safe_reply(_message, _text: str, **_kwargs):
        return None

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.photo_handler(update, SimpleNamespace(bot=object(), user_data={}))

    assert events[:2] == ["activate", "lookup"]


@pytest.mark.asyncio
async def test_photo_handler_waits_for_pending_general_migration(monkeypatch):
    chat_id = -100123
    chat = SimpleNamespace(type="supergroup", id=chat_id, is_forum=True)
    message = SimpleNamespace(
        photo=[object()],
        message_thread_id=None,
        chat=chat,
        chat_id=chat_id,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=999),
        effective_message=message,
        effective_chat=chat,
        message=message,
    )
    replies: list[str] = []
    routing_users: list[int] = []
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda uid, *_args, **_kwargs: routing_users.append(uid),
    )
    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(100, 77, chat_id),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_for_thread",
        lambda *_args, **_kwargs: pytest.fail("photo must not reach a General session"),
    )

    async def _reply(_message, text, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _reply)

    await bot.photo_handler(update, SimpleNamespace(bot=object(), user_data={}))

    assert replies and "migration is still pending" in replies[0]
    assert routing_users == []


@pytest.mark.asyncio
async def test_photo_handler_rejects_unconfigured_general_before_lookup_or_download(
    monkeypatch,
):
    chat_id = -100123
    chat = SimpleNamespace(type="supergroup", id=chat_id, is_forum=True)

    class _UnexpectedPhoto:
        file_unique_id = "must-not-download"

        async def get_file(self):
            raise AssertionError("unconfigured General photo must not be downloaded")

    message = SimpleNamespace(
        photo=[_UnexpectedPhoto()],
        message_thread_id=1,
        chat=chat,
        chat_id=chat_id,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=999),
        effective_message=message,
        effective_chat=chat,
        message=message,
    )
    replies: list[str] = []
    routing_users: list[int] = []
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda uid, *_args, **_kwargs: routing_users.append(uid),
    )
    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_for_thread",
        lambda *_args, **_kwargs: pytest.fail(
            "unconfigured General photo must not resolve a caller session"
        ),
    )

    async def _reply(_message, text, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _reply)

    await bot.photo_handler(update, SimpleNamespace(bot=object(), user_data={}))

    assert replies == [bot._COCO_CONTROL_UNCONFIGURED_TEXT]
    assert routing_users == []


@pytest.mark.asyncio
async def test_single_session_user_cannot_upload_photo_to_general_control(monkeypatch):
    chat_id = -100123
    chat = SimpleNamespace(type="supergroup", id=chat_id, is_forum=True)

    class _UnexpectedPhoto:
        file_unique_id = "should-not-download"

        async def get_file(self):
            raise AssertionError("unauthorized General photo must not be downloaded")

    message = SimpleNamespace(
        photo=[_UnexpectedPhoto()],
        message_thread_id=None,
        chat=chat,
        chat_id=chat_id,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=999),
        effective_message=message,
        effective_chat=chat,
        message=message,
    )
    replies: list[str] = []
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: False)
    monkeypatch.setattr(bot, "_get_user_scope", lambda _uid: bot.SCOPE_SINGLE_SESSION)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(bot.session_manager, "set_group_chat_id", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(100, 1, chat_id),
    )

    async def _reply(_message, text, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _reply)

    await bot.photo_handler(update, SimpleNamespace(bot=object(), user_data={}))

    assert replies == [
        "❌ Only the CoCo control owner or an admin can control another user's topic."
    ]


@pytest.mark.asyncio
async def test_photo_handler_clears_status_for_routed_general_owner(monkeypatch, tmp_path):
    """General photo status cleanup must use the canonical control owner."""
    caller_user_id = 200
    control_owner_user_id = 100
    chat_id = -100123

    class _Chat:
        type = "supergroup"
        id = chat_id
        is_forum = True

        async def send_action(self, _action: str) -> None:
            return None

    class _PhotoFile:
        async def download_to_drive(self, path: Path) -> None:
            path.write_bytes(b"JPEGDATA")

    class _Photo:
        file_unique_id = "general-owner-photo"

        async def get_file(self) -> _PhotoFile:
            return _PhotoFile()

    chat = _Chat()
    message = SimpleNamespace(
        photo=[_Photo()],
        caption="Inspect this image",
        message_thread_id=None,
        chat=chat,
        chat_id=chat_id,
        message_id=1,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=caller_user_id),
        effective_message=message,
        effective_chat=chat,
        message=message,
    )
    context = SimpleNamespace(bot=object(), user_data={})
    clear_calls: list[tuple[int, int | None, int | None]] = []
    sends: list[dict] = []
    binding = SimpleNamespace(
        window_id="@general",
        codex_thread_id="codex-general",
        machine_id="node-a",
        cwd="/workspace/general",
    )
    ownership = bot.TopicOwnership(
        window_id=binding.window_id,
        codex_thread_id=binding.codex_thread_id,
        machine_id=binding.machine_id,
        cwd=binding.cwd,
    )

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(bot.config, "session_provider", "other")
    monkeypatch.setattr(bot, "_local_machine_identity", lambda: ("node-a", "Node A"))
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: True)
    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(bot.session_manager, "set_group_chat_id", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(control_owner_user_id, 1, chat_id),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_for_thread",
        lambda uid, tid, **_kwargs: (
            "@general"
            if (uid, tid) == (control_owner_user_id, 1)
            else pytest.fail("General photo must route through the control owner")
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda uid, tid, **_kwargs: (
            binding
            if (uid, tid) == (control_owner_user_id, 1)
            else pytest.fail("General photo binding must use the control owner")
        ),
    )
    monkeypatch.setattr(
        bot,
        "capture_topic_ownership",
        lambda uid, tid, _chat_id: (
            ownership
            if (uid, tid) == (control_owner_user_id, 1)
            else pytest.fail("General photo ownership must use the control owner")
        ),
    )
    monkeypatch.setattr(
        bot,
        "clear_status_msg_info",
        lambda uid, tid, chat: clear_calls.append((uid, tid, chat)),
    )

    async def _send(**kwargs):
        sends.append(kwargs)
        return True, "ok"

    async def _reply(_message, _text, **_kwargs):
        return None

    async def _reaction(_message):
        return None

    monkeypatch.setattr(bot.session_manager, "send_topic_text_to_window", _send)
    monkeypatch.setattr(bot, "safe_reply", _reply)
    monkeypatch.setattr(bot, "note_run_started", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "_set_eyes_reaction", _reaction)
    monkeypatch.setattr(bot, "_IMAGES_DIR", tmp_path)

    await bot.photo_handler(update, context)

    assert clear_calls == [(control_owner_user_id, 1, chat_id)]
    assert sends and sends[0]["user_id"] == control_owner_user_id


@pytest.mark.asyncio
async def test_photo_handler_drops_image_fallback_after_topic_rebind_during_download(
    monkeypatch,
    tmp_path,
):
    """A photo accepted for owner A must not schedule a fallback into owner B."""
    user_id = 1147817421
    thread_id = 77
    chat_id = -100123
    owner_a = SimpleNamespace(
        window_id="@photo-a",
        codex_thread_id="photo-thread-a",
        machine_id="machine-a",
        cwd="/workspace/a",
    )
    owner_b = SimpleNamespace(
        window_id="@photo-b",
        codex_thread_id="photo-thread-b",
        machine_id="machine-b",
        cwd="/workspace/b",
    )
    current_owner = {"value": owner_a}
    capture_called = asyncio.Event()
    download_started = asyncio.Event()
    release_download = asyncio.Event()
    dispatched: list[tuple[str, str]] = []
    scheduled: list[asyncio.Task] = []

    class _FakeChat:
        type = "supergroup"
        id = chat_id

        async def send_action(self, _action: str) -> None:
            return None

    async def _set_reaction(_message):
        return None

    tg_file = _FakePhotoFile(
        download_started=download_started,
        release_download=release_download,
    )
    photo = _FakePhoto(tg_file)
    chat = _FakeChat()
    message = SimpleNamespace(
        photo=[photo],
        caption="Inspect this image",
        message_thread_id=thread_id,
        chat=chat,
        chat_id=chat_id,
        message_id=991,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_message=message,
        effective_chat=chat,
        message=message,
    )
    context = SimpleNamespace(bot=object(), user_data={})
    session_manager = bot.session_manager

    def _capture_topic_ownership(*_args, **_kwargs):
        # The snapshot must be taken before Telegram's potentially slow file
        # download starts.
        assert not download_started.is_set()
        capture_called.set()
        return owner_a

    async def _fake_send_topic_text_to_window(
        *,
        window_id: str,
        text: str,
        topic_ownership=None,
        **_kwargs,
    ):
        if topic_ownership is None:
            # Model the vulnerable call: a send without an ingress snapshot
            # resolves whatever owner is canonical now (owner B).
            dispatched.append((current_owner["value"].window_id, text))
            return True, ""
        if topic_ownership != current_owner["value"]:
            return False, "stale topic owner; request was not sent"
        dispatched.append((window_id, text))
        return True, ""

    async def _fake_run_photo_bridge_task(
        *,
        bot: object,
        user_id: int,
        thread_id: int,
        chat_id: int | None,
        window_id: str,
        image_path: Path,
        prompt: str,
        topic_ownership=None,
    ) -> None:
        fallback_text = f"{prompt}\n\n(image attached: {image_path})"
        await session_manager.send_topic_text_to_window(
            user_id=user_id,
            thread_id=thread_id,
            chat_id=chat_id,
            window_id=window_id,
            text=fallback_text,
            topic_ownership=topic_ownership,
        )

    monkeypatch.setattr(bot, "_IMAGES_DIR", tmp_path)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(bot.config, "session_provider", "codex")
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_for_thread",
        lambda *_args, **_kwargs: owner_a.window_id,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda *_args, **_kwargs: owner_a,
    )
    monkeypatch.setattr(bot, "capture_topic_ownership", _capture_topic_ownership)
    monkeypatch.setattr(bot, "clear_status_msg_info", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bot, "_set_eyes_reaction", _set_reaction)
    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        _fake_send_topic_text_to_window,
    )
    monkeypatch.setattr(bot, "_run_photo_bridge_task", _fake_run_photo_bridge_task)

    original_create_task = asyncio.create_task

    def _track_create_task(coro):
        task = original_create_task(coro)
        scheduled.append(task)
        return task

    monkeypatch.setattr(bot.asyncio, "create_task", _track_create_task)

    photo_task = original_create_task(bot.photo_handler(update, context))
    await asyncio.wait_for(download_started.wait(), timeout=1)

    current_owner["value"] = owner_b
    release_download.set()
    await asyncio.wait_for(photo_task, timeout=1)
    await asyncio.wait_for(asyncio.gather(*scheduled), timeout=1)

    assert tg_file.paths
    assert not any(window_id == owner_b.window_id for window_id, _text in dispatched)
    assert capture_called.is_set()


def _install_ambiguous_photo_topic_bindings(
    monkeypatch,
    *,
    user_id: int,
    thread_id: int,
    chat_ids: tuple[int, int],
) -> dict[tuple[int, int], bot.TopicBinding]:
    """Install two same-owner/thread bindings that differ only by chat scope."""
    bindings: dict[tuple[int, int], bot.TopicBinding] = {}
    per_user: dict[str, bot.TopicBinding] = {}
    for index, chat_id in enumerate(chat_ids):
        binding = bot.TopicBinding(
            chat_id=chat_id,
            thread_id=thread_id,
            window_id=f"@photo-{index}",
            codex_thread_id=f"photo-thread-{index}",
            cwd=f"/workspace/photo-{index}",
            sync_mode=bot.TOPIC_SYNC_MODE_HOST_FOLLOW_FINAL,
            machine_id="local-node",
        )
        bindings[(chat_id, thread_id)] = binding
        per_user[f"{chat_id}:{thread_id}"] = binding

    monkeypatch.setattr(bot.session_manager, "topic_bindings_v2", {user_id: per_user})
    monkeypatch.setattr(bot.session_manager, "_save_state", lambda: None)
    monkeypatch.setattr(bot, "_local_machine_identity", lambda: ("local-node", "Local"))
    monkeypatch.setattr(
        bot.session_manager,
        "_external_turn_active_by_window",
        {},
    )
    return bindings


@pytest.mark.asyncio
async def test_submit_image_app_server_restores_chat_scoped_topic_sync_mode(
    monkeypatch,
    tmp_path,
):
    """App-server image success must restore only the requested chat's topic."""
    user_id = 100
    thread_id = 1
    target_chat_id, other_chat_id = (-100101, -100202)
    bindings = _install_ambiguous_photo_topic_bindings(
        monkeypatch,
        user_id=user_id,
        thread_id=thread_id,
        chat_ids=(target_chat_id, other_chat_id),
    )
    target_binding = bindings[(target_chat_id, thread_id)]
    image_path = tmp_path / "photo.png"
    image_path.write_bytes(b"PNGDATA")

    monkeypatch.setattr(bot.config, "session_provider", "codex")
    monkeypatch.setattr(bot, "_codex_app_server_preferred", lambda: True)
    mark_chat_ids: list[int | None] = []
    original_mark_topic_telegram_live = bot.session_manager.mark_topic_telegram_live

    def _mark_topic_telegram_live(**kwargs):
        mark_chat_ids.append(kwargs["chat_id"])
        original_mark_topic_telegram_live(**kwargs)

    monkeypatch.setattr(
        bot.session_manager,
        "mark_topic_telegram_live",
        _mark_topic_telegram_live,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_active_turn_id",
        lambda _window_id: "",
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_thread_id",
        lambda _window_id: "",
    )

    async def _send_topic_inputs(**kwargs):
        bot.session_manager.mark_topic_telegram_live(
            user_id=kwargs["user_id"],
            thread_id=kwargs["thread_id"],
            chat_id=kwargs["chat_id"],
            window_id=kwargs["window_id"],
        )
        return True, ""

    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_inputs_to_window",
        _send_topic_inputs,
    )

    ok, error, path_fallback_allowed = await bot._submit_image_to_codex_session(
        user_id=user_id,
        thread_id=thread_id,
        chat_id=target_chat_id,
        window_id=target_binding.window_id,
        image_path=image_path,
        prompt="Inspect this image",
        topic_ownership=bot.TopicOwnership(
            window_id=target_binding.window_id,
            codex_thread_id=target_binding.codex_thread_id,
            machine_id=target_binding.machine_id,
            cwd=target_binding.cwd,
        ),
    )

    assert (ok, error, path_fallback_allowed) == (True, "", False)
    assert mark_chat_ids == [target_chat_id]
    assert bindings[(target_chat_id, thread_id)].sync_mode == (
        bot.TOPIC_SYNC_MODE_TELEGRAM_LIVE
    )
    assert bindings[(other_chat_id, thread_id)].sync_mode == (
        bot.TOPIC_SYNC_MODE_HOST_FOLLOW_FINAL
    )


@pytest.mark.asyncio
async def test_submit_image_rejects_remote_topic_before_local_cli_fallback(
    monkeypatch,
    tmp_path,
) -> None:
    user_id = 100
    thread_id = 1
    target_chat_id, other_chat_id = (-100707, -100808)
    bindings = _install_ambiguous_photo_topic_bindings(
        monkeypatch,
        user_id=user_id,
        thread_id=thread_id,
        chat_ids=(target_chat_id, other_chat_id),
    )
    target_binding = bindings[(target_chat_id, thread_id)]
    target_binding.machine_id = "remote-node"
    image_path = tmp_path / "photo.png"
    image_path.write_bytes(b"PNGDATA")

    monkeypatch.setattr(bot.config, "session_provider", "codex")
    monkeypatch.setattr(bot, "_codex_app_server_preferred", lambda: False)
    monkeypatch.setattr(bot, "_local_machine_identity", lambda: ("local-node", "Local"))
    monkeypatch.setattr(
        bot,
        "_resolve_codex_exec_binary",
        lambda: pytest.fail("remote image reached controller-local CLI fallback"),
    )

    ok, error, path_fallback_allowed = await bot._submit_image_to_codex_session(
        user_id=user_id,
        thread_id=thread_id,
        chat_id=target_chat_id,
        window_id=target_binding.window_id,
        image_path=image_path,
        prompt="Inspect this image",
        topic_ownership=bot.TopicOwnership(
            window_id=target_binding.window_id,
            codex_thread_id=target_binding.codex_thread_id,
            machine_id=target_binding.machine_id,
            cwd=target_binding.cwd,
        ),
    )

    assert (ok, error, path_fallback_allowed) == (
        False,
        _REMOTE_ATTACHMENT_REJECTION_TEXT,
        False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dispatch_started", "send_error"),
    [
        (
            False,
            "The topic binding and window cache disagree about the Codex thread.",
        ),
        (
            False,
            "App-server send failed: topic binding changed during recovery",
        ),
        (
            True,
            "The topic's canonical Codex binding changed while its fresh recovery "
            "turn was in flight.",
        ),
    ],
)
async def test_submit_image_does_not_cli_retry_topic_safety_failures(
    monkeypatch,
    tmp_path,
    dispatch_started: bool,
    send_error: str,
):
    """Unsafe targets and possible prior dispatches must never reach CLI resume."""
    user_id = 100
    thread_id = 1
    chat_id = -100505
    bindings = _install_ambiguous_photo_topic_bindings(
        monkeypatch,
        user_id=user_id,
        thread_id=thread_id,
        chat_ids=(chat_id, -100606),
    )
    target_binding = bindings[(chat_id, thread_id)]
    image_path = tmp_path / "photo.png"
    image_path.write_bytes(b"PNGDATA")

    monkeypatch.setattr(bot.config, "session_provider", "codex")
    monkeypatch.setattr(bot, "_codex_app_server_preferred", lambda: True)

    async def _send_topic_inputs(**kwargs):
        if dispatch_started:
            kwargs["dispatch_state"].mark_transport_dispatch_started()
        return False, send_error

    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_inputs_to_window",
        _send_topic_inputs,
    )
    monkeypatch.setattr(
        bot,
        "_resolve_codex_exec_binary",
        lambda: pytest.fail("unsafe image failure reached CLI fallback"),
    )

    ok, error, path_fallback_allowed = await bot._submit_image_to_codex_session(
        user_id=user_id,
        thread_id=thread_id,
        chat_id=chat_id,
        window_id=target_binding.window_id,
        image_path=image_path,
        prompt="Inspect this image",
        topic_ownership=bot.TopicOwnership(
            window_id=target_binding.window_id,
            codex_thread_id=target_binding.codex_thread_id,
            machine_id=target_binding.machine_id,
            cwd=target_binding.cwd,
        ),
    )

    assert (ok, error, path_fallback_allowed) == (False, send_error, False)


@pytest.mark.asyncio
async def test_submit_image_cli_resume_restores_chat_scoped_topic_sync_mode(
    monkeypatch,
    tmp_path,
):
    """CLI-resume image success must restore only the requested chat's topic."""
    user_id = 100
    thread_id = 1
    target_chat_id, other_chat_id = (-100303, -100404)
    bindings = _install_ambiguous_photo_topic_bindings(
        monkeypatch,
        user_id=user_id,
        thread_id=thread_id,
        chat_ids=(target_chat_id, other_chat_id),
    )
    target_binding = bindings[(target_chat_id, thread_id)]
    target_binding.model_slug = "gpt-5.6-luna"
    target_binding.reasoning_effort = "max"
    target_binding.service_tier = "fast"
    image_path = tmp_path / "photo.png"
    image_path.write_bytes(b"PNGDATA")

    monkeypatch.setattr(bot.config, "session_provider", "codex")
    monkeypatch.setattr(bot, "_codex_app_server_preferred", lambda: True)
    monkeypatch.setattr(bot, "_resolve_codex_exec_binary", lambda: "/usr/bin/codex")

    async def _app_server_unavailable(**_kwargs):
        raise bot.CodexAppServerError(
            "app-server unavailable",
            request_dispatched=False,
        )

    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_inputs_to_window",
        _app_server_unavailable,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_active_turn_id",
        lambda _window_id: "",
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_thread_id",
        lambda _window_id: "",
    )
    mark_chat_ids: list[int | None] = []
    original_mark_topic_telegram_live = bot.session_manager.mark_topic_telegram_live

    def _mark_topic_telegram_live(**kwargs):
        mark_chat_ids.append(kwargs["chat_id"])
        original_mark_topic_telegram_live(**kwargs)

    monkeypatch.setattr(
        bot.session_manager,
        "mark_topic_telegram_live",
        _mark_topic_telegram_live,
    )

    async def _resolve_session(_window_id):
        return SimpleNamespace(session_id=target_binding.codex_thread_id)

    monkeypatch.setattr(bot.session_manager, "resolve_session_for_window", _resolve_session)
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_state",
        lambda _window_id: SimpleNamespace(cwd=str(tmp_path)),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "register_expected_transcript_user_echo",
        lambda *_args, **_kwargs: None,
    )

    class _CompletedProcess:
        returncode = 0

        async def communicate(self):
            return b"", b""

    subprocess_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def _create_subprocess_exec(*args, **kwargs):
        subprocess_calls.append((args, kwargs))
        return _CompletedProcess()

    monkeypatch.setattr(bot.asyncio, "create_subprocess_exec", _create_subprocess_exec)

    ok, error, path_fallback_allowed = await bot._submit_image_to_codex_session(
        user_id=user_id,
        thread_id=thread_id,
        chat_id=target_chat_id,
        window_id=target_binding.window_id,
        image_path=image_path,
        prompt="Inspect this image",
        topic_ownership=bot.TopicOwnership(
            window_id=target_binding.window_id,
            codex_thread_id=target_binding.codex_thread_id,
            machine_id=target_binding.machine_id,
            cwd=target_binding.cwd,
        ),
    )

    assert (ok, error, path_fallback_allowed) == (True, "", False)
    assert subprocess_calls == [
        (
            (
                "/usr/bin/codex",
                "exec",
                "resume",
                target_binding.codex_thread_id,
                "--skip-git-repo-check",
                "--model",
                "gpt-5.6-luna",
                "--config",
                'model_reasoning_effort="max"',
                "--config",
                'service_tier="fast"',
                "-i",
                str(image_path),
                "Inspect this image",
            ),
            {
                "cwd": str(tmp_path),
                "stdout": bot.asyncio.subprocess.PIPE,
                "stderr": bot.asyncio.subprocess.PIPE,
            },
        )
    ]
    assert mark_chat_ids == [target_chat_id]
    assert bindings[(target_chat_id, thread_id)].sync_mode == (
        bot.TOPIC_SYNC_MODE_TELEGRAM_LIVE
    )
    assert bindings[(other_chat_id, thread_id)].sync_mode == (
        bot.TOPIC_SYNC_MODE_HOST_FOLLOW_FINAL
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("request_dispatched", [None, True])
async def test_submit_image_does_not_cli_retry_indeterminate_app_server_error(
    monkeypatch,
    tmp_path,
    request_dispatched: bool | None,
) -> None:
    user_id = 100
    thread_id = 1
    target_chat_id, other_chat_id = (-100909, -1001001)
    bindings = _install_ambiguous_photo_topic_bindings(
        monkeypatch,
        user_id=user_id,
        thread_id=thread_id,
        chat_ids=(target_chat_id, other_chat_id),
    )
    target_binding = bindings[(target_chat_id, thread_id)]
    image_path = tmp_path / "photo.png"
    image_path.write_bytes(b"PNGDATA")

    monkeypatch.setattr(bot.config, "session_provider", "codex")
    monkeypatch.setattr(bot, "_codex_app_server_preferred", lambda: True)

    async def _indeterminate_failure(**_kwargs: object) -> tuple[bool, str]:
        raise bot.CodexAppServerError(
            "indeterminate app-server failure",
            request_dispatched=request_dispatched,
        )

    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_inputs_to_window",
        _indeterminate_failure,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_active_turn_id",
        lambda _window_id: "",
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_thread_id",
        lambda _window_id: "",
    )
    monkeypatch.setattr(
        bot,
        "_resolve_codex_exec_binary",
        lambda: pytest.fail("indeterminate image failure reached CLI fallback"),
    )

    ok, error, path_fallback_allowed = await bot._submit_image_to_codex_session(
        user_id=user_id,
        thread_id=thread_id,
        chat_id=target_chat_id,
        window_id=target_binding.window_id,
        image_path=image_path,
        prompt="Inspect this image",
        topic_ownership=bot.TopicOwnership(
            window_id=target_binding.window_id,
            codex_thread_id=target_binding.codex_thread_id,
            machine_id=target_binding.machine_id,
            cwd=target_binding.cwd,
        ),
    )

    assert (ok, error, path_fallback_allowed) == (
        False,
        "App-server send failed: indeterminate app-server failure",
        False,
    )
