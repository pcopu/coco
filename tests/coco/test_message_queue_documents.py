from types import SimpleNamespace

import pytest

import coco.handlers.message_queue as mq


@pytest.fixture(autouse=True)
def _current_topic_owner(monkeypatch):
    monkeypatch.setattr(
        mq,
        "is_topic_ownership_current",
        lambda *_args, **_kwargs: True,
    )


@pytest.mark.asyncio
async def test_process_content_task_sends_document_attachments(monkeypatch):
    text_sends: list[tuple[int, str, dict[str, object]]] = []
    document_sends: list[tuple[int, list[tuple[str, bytes]], dict[str, object]]] = []

    monkeypatch.setattr(
        mq.session_manager,
        "resolve_chat_id",
        lambda _user_id, _thread_id: -100123,
    )

    async def _send_with_fallback(_bot, chat_id, text, **kwargs):
        text_sends.append((chat_id, text, kwargs))
        return SimpleNamespace(message_id=321)

    async def _send_documents(_bot, chat_id, document_data, **kwargs):
        document_sends.append((chat_id, document_data, kwargs))

    async def _check_status(*_args, **_kwargs):
        return None

    monkeypatch.setattr(mq, "send_with_fallback", _send_with_fallback)
    monkeypatch.setattr(mq, "send_documents", _send_documents)
    monkeypatch.setattr(mq, "_check_and_send_status", _check_status)

    task = mq.MessageTask(
        task_type="content",
        window_id="@1",
        parts=["Report attached"],
        content_type="text",
        thread_id=77,
        topic_ownership=mq.TopicOwnership("@1", "thread-77", "machine", "/tmp"),
        document_data=[("report.pdf", b"%PDF-1.7")],
    )

    await mq._process_content_task(object(), 1, task)

    assert text_sends == [(-100123, "Report attached", {"message_thread_id": 77})]
    assert document_sends == [
        (-100123, [("report.pdf", b"%PDF-1.7")], {"message_thread_id": 77})
    ]


@pytest.mark.asyncio
async def test_process_content_task_sends_video_attachments(monkeypatch):
    text_sends: list[tuple[int, str, dict[str, object]]] = []
    video_sends: list[tuple[int, str, bytes, dict[str, object]]] = []

    monkeypatch.setattr(
        mq.session_manager,
        "resolve_chat_id",
        lambda _user_id, _thread_id: -100123,
    )

    async def _send_with_fallback(_bot, chat_id, text, **kwargs):
        text_sends.append((chat_id, text, kwargs))
        return SimpleNamespace(message_id=321)

    async def _send_video(_bot, chat_id, media_type, raw_bytes, **kwargs):
        video_sends.append((chat_id, media_type, raw_bytes, kwargs))

    async def _check_status(*_args, **_kwargs):
        return None

    monkeypatch.setattr(mq, "send_with_fallback", _send_with_fallback)
    monkeypatch.setattr(mq, "send_video", _send_video)
    monkeypatch.setattr(mq, "_check_and_send_status", _check_status)

    task = mq.MessageTask(
        task_type="content",
        window_id="@1",
        parts=["Video attached"],
        content_type="text",
        thread_id=77,
        topic_ownership=mq.TopicOwnership("@1", "thread-77", "machine", "/tmp"),
        video_data=[("video/mp4", b"MP4DATA")],
    )

    await mq._process_content_task(object(), 1, task)

    assert text_sends == [(-100123, "Video attached", {"message_thread_id": 77})]
    assert video_sends == [
        (-100123, "video/mp4", b"MP4DATA", {"message_thread_id": 77})
    ]
