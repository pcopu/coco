from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from coco.handlers import topic_send


@pytest.mark.asyncio
async def test_send_message_to_topic_with_image_file_uses_send_photo(monkeypatch, tmp_path):
    image_path = tmp_path / "image.jpg"
    image_path.write_bytes(b"JPEGDATA")
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        topic_send.session_manager,
        "resolve_chat_id",
        lambda _user_id, _thread_id, chat_id=None: chat_id if chat_id is not None else -100123,
    )

    async def _fake_send_photo(bot, chat_id, image_data, **kwargs):
        calls.append(
            {
                "bot": bot,
                "chat_id": chat_id,
                "image_data": image_data,
                "kwargs": kwargs,
            }
        )

    monkeypatch.setattr(topic_send, "send_photo", _fake_send_photo)

    ok, error = await topic_send.send_message_to_topic(
        SimpleNamespace(),
        user_id=1147817421,
        thread_id=77,
        chat_id=-100123,
        text="hello image",
        image_file=str(image_path),
    )

    assert ok is True
    assert error == ""
    assert len(calls) == 1
    assert calls[0]["chat_id"] == -100123
    assert calls[0]["kwargs"]["caption"] == "hello image"
    assert calls[0]["kwargs"]["message_thread_id"] == 77
    assert calls[0]["image_data"] == [("image/jpeg", b"JPEGDATA")]


@pytest.mark.asyncio
async def test_send_message_to_topic_with_video_file_uses_send_video(monkeypatch, tmp_path):
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"MP4DATA")
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        topic_send.session_manager,
        "resolve_chat_id",
        lambda _user_id, _thread_id, chat_id=None: chat_id if chat_id is not None else -100123,
    )

    async def _fake_send_video(bot, chat_id, media_type, raw_bytes, **kwargs):
        calls.append(
            {
                "bot": bot,
                "chat_id": chat_id,
                "media_type": media_type,
                "raw_bytes": raw_bytes,
                "kwargs": kwargs,
            }
        )

    monkeypatch.setattr(topic_send, "send_video", _fake_send_video)

    ok, error = await topic_send.send_message_to_topic(
        SimpleNamespace(),
        user_id=1147817421,
        thread_id=77,
        chat_id=-100123,
        text="hello video",
        video_file=str(video_path),
    )

    assert ok is True
    assert error == ""
    assert len(calls) == 1
    assert calls[0]["chat_id"] == -100123
    assert calls[0]["media_type"] == "video/mp4"
    assert calls[0]["raw_bytes"] == b"MP4DATA"
    assert calls[0]["kwargs"]["caption"] == "hello video"
    assert calls[0]["kwargs"]["message_thread_id"] == 77
