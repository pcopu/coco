"""Real synthesis HTTP, WAV decoding/resampling/OPUS encoding, Telegram upload."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
import struct
import threading
import wave

import pytest
import soundfile as sf


@pytest.fixture
def speech_peer():
    audio = io.BytesIO()
    with wave.open(audio, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(i * math.tau * 440 / 24000)))
                                 for i in range(6000)))
    state = {"requests": [], "fail": False, "audio": audio.getvalue()}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, status, body, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            assert self.path == "/v1/health"
            self.respond(200, b'{"status":"ok"}', "application/json")

        def do_POST(self):
            assert self.path == "/v1/audio/speech"
            state["requests"].append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.respond(503 if state["fail"] else 200,
                         b"deliberate speech outage" if state["fail"] else state["audio"], "audio/wav")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["port"] = server.server_port
    yield state
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    assert not thread.is_alive()


@pytest.mark.parametrize("failure", [False, True], ids=["opus-upload", "speech-failure-text-fallback"])
def test_voice_mode_delivers_opus_or_visible_text_fallback(controller_factory, speech_peer, failure):
    controller = controller_factory(start=False)
    controller.env["COCO_TTS_BASE_URL"] = f"http://127.0.0.1:{speech_peer['port']}"
    controller.env["COCO_E2E_PORTS"] += f",{speech_peer['port']}"
    controller.start()
    controller.bind()
    controller.peer.message("/voice on")
    controller.wait_request("sendMessage", text="ON")
    controller.rules({"method": "turn/start", "reply": "Speak this exact answer."})
    speech_peer["fail"] = failure
    mark = len(controller.peer.requests)
    controller.peer.message("/q please answer aloud")
    if failure:
        controller.wait_request("sendMessage", since=mark, topic=10, text="Speak this exact answer")
        assert not controller.requests("sendVoice", since=mark)
    else:
        sent = controller.wait_request("sendVoice", since=mark, topic=10)
        payload = bytes.fromhex(sent["data"]["voice"]["bytes_hex"])
        assert payload.startswith(b"OggS") and b"OpusHead" in payload
        samples, sample_rate = sf.read(io.BytesIO(payload))
        assert sample_rate == 48000 and len(samples) > 5000
    assert len(speech_peer["requests"]) == 1
    assert speech_peer["requests"][0]["input"] == "Speak this exact answer."
    assert len(controller.codex("turn/start")) == 1


def test_transcription_failure_with_empty_local_model_is_visible_without_dispatch(controller_factory):
    controller = controller_factory(start=False)
    model = controller.root / "empty-speech-model"
    model.mkdir()
    controller.env["COCO_TRANSCRIPTION_MODEL"] = str(model)
    controller.start()
    controller.bind()
    controller.peer.files["voice"] = b"unusable voice payload"
    controller.peer.message(voice={"file_id": "voice", "file_unique_id": "voice", "duration": 1,
                                   "mime_type": "audio/ogg", "file_size": 22})
    controller.wait_request("sendMessage", text="transcri", timeout=12)
    # A visible failure must complete without forwarding the original audio or
    # creating a Codex turn; the model path prevents any model download attempt.
    controller.wait_request("sendMessage", text="failed", timeout=12)
    assert len(controller.requests("getFile")) == 1
    assert not controller.codex("turn/start")
