"""Attachment bytes cross real HTTP, filesystem and JSON-RPC boundaries."""

import base64
import io
import json
from pathlib import Path
import re
import zipfile

import pytest

from .harness import eventually


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aM9sAAAAASUVORK5CYII=")


@pytest.mark.parametrize("kind,filename,mime,payload", [
    ("photo", "image.jpg", "image/jpeg", PNG),
    ("document", "report.pdf", "application/pdf", b"%PDF-1.7\nE2E report\n"),
    ("document", "photo.png", "image/png", PNG),
    ("document", "recording.wav", "audio/wav", b"RIFF-test-fixture"),
    ("video", "clip.mp4", "video/mp4", b"mp4-test-fixture"),
], ids=["photo", "pdf", "image-document", "audio-document", "video"])
def test_incoming_attachment_reaches_bound_codex_with_local_bytes(controller, kind, filename, mime, payload):
    binding = controller.bind()
    controller.peer.files["upload"] = payload
    media = {"file_id": "upload", "file_unique_id": "unique", "file_size": len(payload),
             "file_name": filename, "mime_type": mime, "width": 1, "height": 1, "duration": 1}
    controller.peer.message(caption="analyze this attachment", **{kind: [media] if kind == "photo" else media})
    turn = controller.wait_codex("turn/start")
    controller.wait_request("sendMessage", text="E2E answer")
    assert turn["params"]["threadId"] == binding["codex_thread_id"]
    assert "analyze this attachment" in json.dumps(turn["params"]["input"])
    assert len(controller.requests("getFile")) == 1
    assert len(controller.requests("downloadFile")) == 1
    files = [p for folder in ("documents", "videos", "images") for p in (controller.root / "coco" / folder).glob("*") if p.is_file()]
    assert any(p.read_bytes() == payload for p in files)
    if kind == "photo":
        images = [entry for entry in turn["params"]["input"] if entry["type"] == "localImage"]
        assert len(images) == 1
        assert Path(images[0]["path"]).is_relative_to(controller.root)
        assert Path(images[0]["path"]).read_bytes() == payload
    elif mime == "image/png":
        # Image documents currently enter as a path-bearing text prompt; photos
        # use the native localImage item. Preserve and prove both supported routes.
        assert "image attached:" in json.dumps(turn["params"]["input"])
        assert any(str(path) in json.dumps(turn["params"]["input"]) for path in files)


@pytest.mark.parametrize("filename,payload,method,field", [
    ("plot.png", PNG, "sendPhoto", "photo"),
    ("animation.gif", b"GIF89a-test", "sendAnimation", "animation"),
    ("clip.mp4", b"mp4-test", "sendVideo", "video"),
    ("report.pdf", b"%PDF-test", "sendDocument", "document"),
], ids=["photo", "gif", "video", "document"])
def test_outgoing_attachment_is_uploaded_to_its_topic(controller, filename, payload, method, field):
    controller.bind()
    (controller.root / "workspace" / filename).write_bytes(payload)
    controller.rules({"method": "turn/start", "reply": f'Result attached\n<telegram-attachment path="{filename}" />'})
    controller.peer.message("produce an attachment")
    upload = controller.wait_request(method, topic=10)
    assert bytes.fromhex(upload["data"][field]["bytes_hex"]) == payload
    assert not controller.requests("sendMessage", text="telegram-attachment")


def test_media_rejection_falls_back_to_document_and_permanent_failure_is_visible(controller):
    controller.bind()
    (controller.root / "workspace/clip.mp4").write_bytes(b"exact-video-bytes")
    controller.rules({"method": "turn/start", "reply": '<telegram-attachment path="clip.mp4" />'})
    controller.peer.fail_next("sendVideo", description="Bad Request: invalid video")
    controller.peer.message("send video with fallback")
    upload = controller.wait_request("sendDocument", topic=10)
    assert bytes.fromhex(upload["data"]["document"]["bytes_hex"]) == b"exact-video-bytes"
    mark = len(controller.peer.requests)
    controller.peer.fail_next("sendVideo", description="Bad Request: invalid video")
    controller.peer.fail_next("sendDocument", description="Bad Request: file rejected")
    controller.peer.message("show the upload failure")
    controller.wait_request("sendMessage", since=mark, topic=10, text="Attachment upload failed")
    assert len(controller.requests("sendDocument", since=mark)) == 1
    assert len(controller.codex("turn/start")) == 2


def test_outgoing_paths_cannot_escape_workspace_even_via_symlink(controller):
    controller.bind()
    secret = controller.root / "private.txt"
    secret.write_text("DO NOT SEND THIS")
    (controller.root / "workspace/link.txt").symlink_to(secret)
    controller.rules({"method": "turn/start", "reply": (
        'public answer\n<telegram-attachment path="../private.txt" />\n'
        '<telegram-attachment path="link.txt" />\n'
        f'<telegram-attachment path="{secret}" />'
    )})
    controller.peer.message("return attachments safely")
    controller.wait_request("sendMessage", text="public answer")
    controller.peer.message("/status")
    controller.wait_request("sendMessage", text="Codex status")
    assert not controller.requests("sendDocument")
    assert not controller.requests("sendMessage", text="DO NOT SEND THIS")
    assert secret.read_text() == "DO NOT SEND THIS"


@pytest.mark.parametrize("entry", ["../escape.txt", "/absolute.txt"])
def test_archive_traversal_is_rejected_before_dispatch(controller, entry):
    controller.bind()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(entry, "cannot escape")
    controller.peer.files["zip"] = buffer.getvalue()
    controller.peer.message(document={"file_id": "zip", "file_unique_id": "zip", "file_name": "bundle.zip",
                            "mime_type": "application/zip", "file_size": len(buffer.getvalue())})
    controller.wait_request("sendMessage", text="Archive extraction failed")
    assert not controller.codex("turn/start")
    assert not list((controller.root / "coco/documents").rglob("escape.txt"))


def test_long_final_reply_is_split_without_dropping_content(controller):
    controller.bind()
    lines = [f"line{index:04d} " + "readable words " * 8 for index in range(90)]
    controller.rules({"method": "turn/start", "reply": "\n".join(lines)})
    mark = len(controller.peer.requests)
    controller.peer.message("send a long report")
    controller.wait_request("sendMessage", since=mark, topic=10, text="line0089")
    replies = controller.requests("sendMessage", since=mark, topic=10)
    content = "\n".join(row["data"]["text"] for row in replies)
    assert len(replies) >= 3
    assert all(len(row["data"]["text"]) <= 4096 for row in replies)
    assert re.findall(r"line\d{4}", content) == [f"line{i:04d}" for i in range(90)]


def test_telegram_rate_limit_retries_delivery_without_repeating_codex(controller):
    controller.bind()
    controller.rules({"method": "turn/start", "hold": True})
    controller.peer.message("one turn only")
    turn = controller.wait_codex("turn/start")
    controller.wait_request("sendMessage", topic=10, text="⏳ Working")
    controller.peer.fail_next("sendMessage", code=429, description="Too Many Requests: retry after 1", retry_after=1,
                              match={"text": "retry this delivery"})
    mark = len(controller.peer.requests)
    controller.complete(turn, "retry this delivery")
    eventually(lambda: len(controller.requests("sendMessage", since=mark, text="retry this delivery")) >= 2,
               diagnostics=controller.diagnostics)
    assert len(controller.codex("turn/start")) == 1
