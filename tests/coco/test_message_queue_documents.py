from types import SimpleNamespace

import pytest

import coco.handlers.message_queue as mq


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
        document_data=[("report.pdf", b"%PDF-1.7")],
    )

    await mq._process_content_task(object(), 1, task)

    assert text_sends == [(-100123, "Report attached", {"message_thread_id": 77})]
    assert document_sends == [
        (-100123, [("report.pdf", b"%PDF-1.7")], {"message_thread_id": 77})
    ]
