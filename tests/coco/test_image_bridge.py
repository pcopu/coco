"""Tests for Codex image bridge helpers."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import coco.bot as bot


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
    )
    assert cmd == [
        "/usr/bin/codex",
        "exec",
        "resume",
        "session-123",
        "--skip-git-repo-check",
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
