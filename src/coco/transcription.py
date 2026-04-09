"""Local audio transcription helpers for Telegram media ingress."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .utils import env_alias

logger = logging.getLogger(__name__)

TRANSCRIPTION_PROFILES = {"compatible"}


class TranscriptionError(RuntimeError):
    """Raised when local audio transcription cannot complete."""


@dataclass(frozen=True)
class TranscriptionRuntime:
    """Resolved runtime configuration for one transcription profile."""

    profile: str
    model_name: str
    device: str
    compute_type: str
    download_root: str
    gpu_available: bool


@dataclass(frozen=True)
class TranscriptionBootstrapHandle:
    """One reserved first-download notification slot for a model config."""

    cache_key: tuple[str, str, str, str]


_MODEL_CACHE: dict[tuple[str, str, str, str], Any] = {}
_MODEL_LOCK = threading.Lock()
_BOOTSTRAP_PENDING: set[tuple[str, str, str, str]] = set()
_BOOTSTRAP_READY: set[tuple[str, str, str, str]] = set()


def normalize_transcription_profile(raw_value: object) -> str:
    """Normalize one persisted/user-facing transcription profile value."""
    if not isinstance(raw_value, str):
        return ""
    normalized = raw_value.strip().lower()
    return normalized if normalized in TRANSCRIPTION_PROFILES else ""


def get_default_transcription_profile() -> str:
    """Return the default server transcription profile."""
    configured = normalize_transcription_profile(
        env_alias("COCO_TRANSCRIPTION_PROFILE_DEFAULT", default="compatible")
    )
    return configured or "compatible"


def _transcription_model_override() -> str:
    return env_alias("COCO_TRANSCRIPTION_MODEL").strip()


def _transcription_device_override() -> str:
    value = env_alias("COCO_TRANSCRIPTION_DEVICE").strip().lower()
    return value if value in {"cpu", "cuda"} else ""


def _transcription_compute_type_override() -> str:
    return env_alias("COCO_TRANSCRIPTION_COMPUTE_TYPE").strip().lower()


def _transcription_download_root() -> str:
    return env_alias("COCO_TRANSCRIPTION_DOWNLOAD_ROOT").strip()


def _transcription_beam_size() -> int:
    raw = env_alias("COCO_TRANSCRIPTION_BEAM_SIZE", default="1").strip()
    try:
        parsed = int(raw)
    except ValueError:
        logger.warning(
            "COCO_TRANSCRIPTION_BEAM_SIZE=%r is invalid; using 1",
            raw,
        )
        return 1
    return max(1, parsed)


def _transcription_language() -> str | None:
    value = env_alias("COCO_TRANSCRIPTION_LANGUAGE").strip()
    return value or None


def _transcription_vad_filter() -> bool:
    value = env_alias("COCO_TRANSCRIPTION_VAD_FILTER", default="true").strip().lower()
    if value in {"", "1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    logger.warning(
        "COCO_TRANSCRIPTION_VAD_FILTER=%r is invalid; using true",
        value,
    )
    return True


def _cuda_device_count() -> int:
    """Return the number of visible CUDA devices for CTranslate2."""
    try:
        import ctranslate2
    except ImportError:
        return 0

    try:
        count = int(ctranslate2.get_cuda_device_count())
    except Exception:
        logger.debug("Failed to inspect CUDA availability for transcription", exc_info=True)
        return 0
    return max(0, count)


def _supported_compute_types(device: str) -> set[str]:
    """Return supported compute types for one device, lower-cased."""
    try:
        import ctranslate2
    except ImportError:
        return set()

    try:
        raw_values = ctranslate2.get_supported_compute_types(device)
    except Exception:
        logger.debug(
            "Failed to inspect supported compute types for transcription device %s",
            device,
            exc_info=True,
        )
        return set()
    return {
        str(value).strip().lower()
        for value in raw_values
        if isinstance(value, str) and str(value).strip()
    }


def _pick_compute_type(device: str, *, prefer_compatible: bool) -> str:
    supported = _supported_compute_types(device)
    if device == "cuda":
        ordered = ["float16", "int8_float16", "int8", "float32", "int8_float32"]
        fallback = "float16"
    else:
        ordered = ["int8", "int8_float32", "int16", "float32"]
        fallback = "int8" if prefer_compatible else "int8"
    for candidate in ordered:
        if candidate in supported:
            return candidate
    return fallback


def resolve_transcription_runtime(profile: str = "") -> TranscriptionRuntime:
    """Resolve one transcription profile into a concrete local runtime."""
    normalized_profile = normalize_transcription_profile(profile)
    if normalized_profile != "compatible":
        normalized_profile = "compatible"
    gpu_available = _cuda_device_count() > 0

    default_model_name = "base"
    default_device = "cpu"
    default_compute_type = _pick_compute_type("cpu", prefer_compatible=True)

    model_name = _transcription_model_override() or default_model_name
    device_override = _transcription_device_override()
    device = device_override if device_override == "cpu" else default_device
    compute_type_override = _transcription_compute_type_override()
    compute_type = (
        compute_type_override
        if compute_type_override in _supported_compute_types(device)
        else _pick_compute_type(device, prefer_compatible=True)
    )
    return TranscriptionRuntime(
        profile=normalized_profile,
        model_name=model_name,
        device=device,
        compute_type=compute_type,
        download_root=_transcription_download_root(),
        gpu_available=gpu_available,
    )


def _transcription_cache_key(profile: str = "") -> tuple[str, str, str, str]:
    runtime = resolve_transcription_runtime(profile)
    return (
        runtime.model_name,
        runtime.device,
        runtime.compute_type,
        runtime.download_root,
    )


def _model_available_locally(
    model_name: str,
    *,
    download_root: str,
) -> bool:
    model_path = Path(model_name).expanduser()
    if model_path.is_dir():
        return True

    try:
        from faster_whisper.utils import download_model
    except ImportError:
        return False

    kwargs: dict[str, Any] = {"local_files_only": True}
    if download_root:
        kwargs["cache_dir"] = download_root

    try:
        resolved = download_model(model_name, **kwargs)
    except Exception:
        return False
    return Path(resolved).is_dir()


def begin_transcription_bootstrap(profile: str = "") -> TranscriptionBootstrapHandle | None:
    """Reserve first-download notices when a model is not cached locally yet."""
    cache_key = _transcription_cache_key(profile)
    model_name, _device, _compute_type, download_root = cache_key

    with _MODEL_LOCK:
        if cache_key in _MODEL_CACHE:
            return None
        if cache_key in _BOOTSTRAP_READY or cache_key in _BOOTSTRAP_PENDING:
            return None
        if _model_available_locally(model_name, download_root=download_root):
            return None
        _BOOTSTRAP_PENDING.add(cache_key)
        return TranscriptionBootstrapHandle(cache_key=cache_key)


def complete_transcription_bootstrap(
    handle: TranscriptionBootstrapHandle | None,
    *,
    success: bool,
) -> bool:
    """Complete one reserved bootstrap attempt and tell callers whether to notify."""
    if handle is None:
        return False

    with _MODEL_LOCK:
        _BOOTSTRAP_PENDING.discard(handle.cache_key)
        if not success:
            return False
        if handle.cache_key in _BOOTSTRAP_READY:
            return False
        _BOOTSTRAP_READY.add(handle.cache_key)
        return True


def _load_whisper_model(profile: str = "") -> Any:
    model_name, device, compute_type, download_root = _transcription_cache_key(profile)
    cache_key = (model_name, device, compute_type, download_root)

    with _MODEL_LOCK:
        cached = _MODEL_CACHE.get(cache_key)
        if cached is not None:
            return cached

        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:  # pragma: no cover - exercised via callers
            raise TranscriptionError(
                "faster-whisper is not installed. Run `pip install faster-whisper`."
            ) from exc

        try:
            model = WhisperModel(
                model_name,
                device=device,
                compute_type=compute_type,
                download_root=download_root or None,
            )
        except Exception as exc:
            raise TranscriptionError(f"failed to load faster-whisper model: {exc}") from exc

        _MODEL_CACHE[cache_key] = model
        return model


def transcribe_audio_file(audio_path: str | Path, *, profile: str = "") -> str:
    """Return plain text transcript for one local audio file."""
    path = Path(audio_path)
    if not path.is_file():
        raise TranscriptionError(f"audio file not found: {path}")

    model = _load_whisper_model(profile)

    transcribe_kwargs: dict[str, Any] = {
        "beam_size": _transcription_beam_size(),
        "vad_filter": _transcription_vad_filter(),
    }
    language = _transcription_language()
    if language:
        transcribe_kwargs["language"] = language

    try:
        segments, _info = model.transcribe(str(path), **transcribe_kwargs)
        text = " ".join(
            segment.text.strip()
            for segment in segments
            if getattr(segment, "text", "").strip()
        ).strip()
    except TranscriptionError:
        raise
    except Exception as exc:
        raise TranscriptionError(f"faster-whisper transcription failed: {exc}") from exc

    if not text:
        raise TranscriptionError("audio transcription was empty")
    return text
