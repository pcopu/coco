from types import SimpleNamespace
import io

import numpy as np

import httpx
import pytest

import coco.tts as tts


class _FakeResponse:
    def __init__(self, *, content: bytes, status_code: int = 200, text: str = "") -> None:
        self.content = content
        self.status_code = status_code
        self.text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", "http://127.0.0.1:7788/v1/audio/speech")
            response = httpx.Response(self.status_code, request=request, text=self.text)
            raise httpx.HTTPStatusError("boom", request=request, response=response)


@pytest.mark.asyncio
async def test_synthesize_voice_note_uses_supertone_openai_endpoint(monkeypatch):
    captured: dict[str, object] = {}
    runtime_calls: list[str] = []
    wav_bytes = b"wav-bytes"

    class _FakeClient:
        def __init__(self, *, base_url: str, timeout: float) -> None:
            captured["base_url"] = base_url
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, path: str, json: dict[str, object]):
            captured["path"] = path
            captured["json"] = json
            return _FakeResponse(content=wav_bytes)

    monkeypatch.setattr(tts.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(tts, "_tts_base_url", lambda: "http://127.0.0.1:7788")
    monkeypatch.setattr(tts, "_tts_voice", lambda: "M1")
    monkeypatch.setattr(tts, "_tts_language", lambda: "en")
    monkeypatch.setattr(tts, "_tts_speed", lambda: 1.4)

    captured_audio: dict[str, object] = {}

    def _fake_prepare_voice_note_audio(raw_bytes: bytes) -> bytes:
        captured_audio["raw_bytes"] = raw_bytes
        return b"opus-bytes"

    async def _fake_ensure_started() -> None:
        runtime_calls.append("ensure")

    monkeypatch.setattr(tts, "_prepare_voice_note_audio", _fake_prepare_voice_note_audio)
    monkeypatch.setattr(tts, "_ensure_tts_runtime_started", _fake_ensure_started)

    media_type, audio_bytes = await tts.synthesize_voice_note("hello world")

    assert runtime_calls == ["ensure"]
    assert media_type == "audio/ogg"
    assert audio_bytes == b"opus-bytes"
    assert captured_audio["raw_bytes"] == wav_bytes
    assert captured["base_url"] == "http://127.0.0.1:7788"
    assert captured["path"] == "/v1/audio/speech"
    assert captured["json"] == {
        "model": "supertonic-3",
        "voice": "M1",
        "input": "hello world",
        "response_format": "wav",
        "language": "en",
        "speed": 1.4,
    }


@pytest.mark.asyncio
async def test_synthesize_voice_note_raises_tts_error_on_http_failure(monkeypatch):
    class _FakeClient:
        def __init__(self, *, base_url: str, timeout: float) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, path: str, json: dict[str, object]):
            return _FakeResponse(content=b"", status_code=503, text="unavailable")

    monkeypatch.setattr(tts.httpx, "AsyncClient", _FakeClient)

    with pytest.raises(tts.TtsError) as excinfo:
        await tts.synthesize_voice_note("hello world")

    assert "unavailable" in str(excinfo.value)


def test_trim_leading_silence_removes_initial_quiet_frames():
    audio = np.array([0.0, 0.0, 0.0005, 0.002, 0.01, 0.02], dtype=np.float32)
    trimmed = tts._trim_leading_silence(audio, threshold=0.005)
    assert np.allclose(trimmed, np.array([0.01, 0.02], dtype=np.float32))


def test_prepare_voice_note_audio_reencodes_to_opus(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_sf_read(file_obj):
        assert isinstance(file_obj, io.BytesIO)
        return np.array([0.0, 0.0, 0.02, 0.03], dtype=np.float32), 44100

    def _fake_resample(audio: np.ndarray, *, source_rate: int, target_rate: int):
        captured["resample"] = {
            "audio": audio.tolist(),
            "source_rate": source_rate,
            "target_rate": target_rate,
        }
        return np.array([0.02, 0.03], dtype=np.float32)

    def _fake_sf_write(file_obj, data, samplerate, *, format, subtype):
        captured["write"] = {
            "data": data.tolist(),
            "samplerate": samplerate,
            "format": format,
            "subtype": subtype,
        }
        file_obj.write(b"opus-data")

    monkeypatch.setattr(tts.sf, "read", _fake_sf_read)
    monkeypatch.setattr(tts, "_resample_audio", _fake_resample)
    monkeypatch.setattr(tts.sf, "write", _fake_sf_write)

    result = tts._prepare_voice_note_audio(b"wav-bytes")

    assert result == b"opus-data"
    assert captured["resample"]["source_rate"] == 44100
    assert captured["resample"]["target_rate"] == 48000
    assert np.allclose(captured["resample"]["audio"], [0.02, 0.03])
    assert captured["write"]["samplerate"] == 48000
    assert captured["write"]["format"] == "OGG"
    assert captured["write"]["subtype"] == "OPUS"
    assert np.allclose(captured["write"]["data"], [0.02, 0.03])
