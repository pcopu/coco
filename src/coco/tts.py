"""Local text-to-speech helpers for Telegram voice-note delivery."""

from __future__ import annotations

import io

import httpx
import numpy as np
import soundfile as sf

from .utils import env_alias


class TtsError(RuntimeError):
    """Raised when local TTS synthesis cannot complete."""


async def _ensure_tts_runtime_started() -> None:
    from .tts_runtime import ensure_tts_server_started

    await ensure_tts_server_started()


def _tts_base_url() -> str:
    return env_alias("COCO_TTS_BASE_URL", default="http://127.0.0.1:7788").strip()


def _tts_voice() -> str:
    return env_alias("COCO_TTS_VOICE", default="M1").strip() or "M1"


def _tts_language() -> str:
    return env_alias("COCO_TTS_LANGUAGE", default="en").strip() or "en"


def _tts_speed() -> float:
    raw = env_alias("COCO_TTS_SPEED", default="1.4").strip()
    try:
        return float(raw)
    except ValueError:
        return 1.4


def get_default_tts_voice() -> str:
    """Return the configured default TTS voice."""
    return _tts_voice()


def get_default_tts_speed() -> float:
    """Return the configured default TTS speed."""
    return _tts_speed()


def _trim_leading_silence(
    audio: np.ndarray,
    *,
    threshold: float = 0.005,
) -> np.ndarray:
    if audio.size == 0:
        return audio
    if audio.ndim > 1:
        amplitude = np.max(np.abs(audio), axis=1)
    else:
        amplitude = np.abs(audio)
    indices = np.flatnonzero(amplitude >= threshold)
    if indices.size == 0:
        return audio
    return audio[int(indices[0]) :]


def _resample_audio(
    audio: np.ndarray,
    *,
    source_rate: int,
    target_rate: int,
) -> np.ndarray:
    if source_rate == target_rate or audio.size == 0:
        return audio
    if audio.ndim == 1:
        duration = audio.shape[0] / float(source_rate)
        sample_count = max(1, round(duration * target_rate))
        old_x = np.linspace(0.0, duration, num=audio.shape[0], endpoint=False)
        new_x = np.linspace(0.0, duration, num=sample_count, endpoint=False)
        return np.interp(new_x, old_x, audio).astype(audio.dtype, copy=False)
    channels: list[np.ndarray] = []
    for channel_idx in range(audio.shape[1]):
        channel = _resample_audio(
            audio[:, channel_idx],
            source_rate=source_rate,
            target_rate=target_rate,
        )
        channels.append(channel)
    return np.stack(channels, axis=1)


def _prepare_voice_note_audio(raw_wav_bytes: bytes) -> bytes:
    with io.BytesIO(raw_wav_bytes) as wav_buffer:
        audio, sample_rate = sf.read(wav_buffer)
    trimmed = _trim_leading_silence(audio)
    normalized = _resample_audio(trimmed, source_rate=int(sample_rate), target_rate=48000)
    with io.BytesIO() as out_buffer:
        sf.write(out_buffer, normalized, 48000, format="OGG", subtype="OPUS")
        return out_buffer.getvalue()


async def synthesize_voice_note(text: str) -> tuple[str, bytes]:
    """Return one synthesized Telegram-friendly voice-note payload."""
    normalized = text.strip()
    if not normalized:
        raise TtsError("text to synthesize cannot be empty")

    try:
        await _ensure_tts_runtime_started()
    except Exception as exc:
        raise TtsError(f"local TTS runtime unavailable: {exc}") from exc

    payload: dict[str, object] = {
        "model": "supertonic-3",
        "voice": _tts_voice(),
        "input": normalized,
        "response_format": "wav",
        "language": _tts_language(),
        "speed": _tts_speed(),
    }

    try:
        async with httpx.AsyncClient(base_url=_tts_base_url(), timeout=30.0) as client:
            response = await client.post("/v1/audio/speech", json=payload)
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        details = exc.response.text.strip() if exc.response is not None else ""
        raise TtsError(details or str(exc)) from exc
    except httpx.HTTPError as exc:
        raise TtsError(str(exc)) from exc

    try:
        voice_bytes = _prepare_voice_note_audio(response.content)
    except Exception as exc:
        raise TtsError(f"failed to prepare voice note audio: {exc}") from exc

    return "audio/ogg", voice_bytes
