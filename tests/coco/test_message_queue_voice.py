from types import SimpleNamespace

import pytest

import coco.handlers.message_queue as mq


@pytest.mark.asyncio
async def test_process_content_task_sends_voice_note_when_topic_prefers_voice(monkeypatch):
    text_sends: list[tuple[int, str, dict[str, object]]] = []
    voice_sends: list[tuple[int, str, bytes, dict[str, object]]] = []

    monkeypatch.setattr(
        mq.session_manager,
        "resolve_chat_id",
        lambda _user_id, _thread_id: -100123,
    )
    monkeypatch.setattr(
        mq.session_manager,
        "get_topic_response_mode",
        lambda _user_id, _thread_id, *, chat_id=None: "voice",
    )

    async def _send_with_fallback(_bot, chat_id, text, **kwargs):
        text_sends.append((chat_id, text, kwargs))
        return SimpleNamespace(message_id=321)

    async def _send_voice(_bot, chat_id, media_type, raw_bytes, **kwargs):
        voice_sends.append((chat_id, media_type, raw_bytes, kwargs))

    async def _synthesize_voice_note(text: str):
        assert text == "Spoken reply"
        return "audio/mpeg", b"voice-bytes"

    async def _check_status(*_args, **_kwargs):
        return None

    monkeypatch.setattr(mq, "send_with_fallback", _send_with_fallback)
    monkeypatch.setattr(mq, "send_voice", _send_voice)
    monkeypatch.setattr(mq, "synthesize_voice_note", _synthesize_voice_note)
    monkeypatch.setattr(mq, "_check_and_send_status", _check_status)

    task = mq.MessageTask(
        task_type="content",
        window_id="@1",
        parts=["Spoken reply"],
        text="Spoken reply",
        content_type="text",
        thread_id=77,
    )

    await mq._process_content_task(object(), 1, task)

    assert text_sends == []
    assert voice_sends == [
        (-100123, "audio/mpeg", b"voice-bytes", {"message_thread_id": 77})
    ]


@pytest.mark.asyncio
async def test_process_content_task_uses_topic_response_mode_instead_of_window_mode(
    monkeypatch,
):
    text_sends: list[tuple[int, str, dict[str, object]]] = []
    voice_sends: list[tuple[int, str, bytes, dict[str, object]]] = []

    monkeypatch.setattr(
        mq.session_manager,
        "resolve_chat_id",
        lambda _user_id, _thread_id: -100123,
    )
    monkeypatch.setattr(
        mq.session_manager,
        "get_topic_response_mode",
        lambda _user_id, _thread_id, *, chat_id=None: "voice",
    )
    monkeypatch.setattr(
        mq.session_manager,
        "get_window_topic_response_mode",
        lambda _window_id: "text",
    )

    async def _send_with_fallback(_bot, chat_id, text, **kwargs):
        text_sends.append((chat_id, text, kwargs))
        return SimpleNamespace(message_id=321)

    async def _send_voice(_bot, chat_id, media_type, raw_bytes, **kwargs):
        voice_sends.append((chat_id, media_type, raw_bytes, kwargs))

    async def _synthesize_voice_note(text: str):
        assert text == "Topic-scoped voice reply"
        return "audio/ogg", b"voice-bytes"

    async def _check_status(*_args, **_kwargs):
        return None

    monkeypatch.setattr(mq, "send_with_fallback", _send_with_fallback)
    monkeypatch.setattr(mq, "send_voice", _send_voice)
    monkeypatch.setattr(mq, "synthesize_voice_note", _synthesize_voice_note)
    monkeypatch.setattr(mq, "_check_and_send_status", _check_status)

    task = mq.MessageTask(
        task_type="content",
        window_id="@1",
        parts=["Topic-scoped voice reply"],
        text="Topic-scoped voice reply",
        content_type="text",
        thread_id=77,
    )

    await mq._process_content_task(object(), 1, task)

    assert text_sends == []
    assert voice_sends == [
        (-100123, "audio/ogg", b"voice-bytes", {"message_thread_id": 77})
    ]
