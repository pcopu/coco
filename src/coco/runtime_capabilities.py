"""Shared local runtime capability discovery for CoCo services."""

from __future__ import annotations

import logging
from typing import Any

from .transcription import resolve_transcription_runtime
from .tts import get_default_tts_speed, get_default_tts_voice
from .tts_runtime import is_tts_runtime_available

logger = logging.getLogger(__name__)


def get_local_runtime_capabilities(*, controller_capable: bool = False) -> list[str]:
    """Return the machine-level runtime capabilities available on this node."""
    capabilities: list[str] = []
    if controller_capable:
        capabilities.append("controller")
    capabilities.append("monitor")

    try:
        resolve_transcription_runtime("compatible")
    except Exception:
        logger.debug("Local transcription capability unavailable", exc_info=True)
    else:
        capabilities.append("transcription")

    try:
        if is_tts_runtime_available():
            capabilities.append("tts")
    except Exception:
        logger.debug("Local TTS capability unavailable", exc_info=True)

    return capabilities


def get_transcription_runtime_summary(profile: str = "compatible") -> dict[str, Any]:
    """Return one compact summary of local transcription capability."""
    runtime = resolve_transcription_runtime(profile)
    return {
        "mode": profile or "compatible",
        "device": runtime.device,
        "compute_type": runtime.compute_type,
        "model_name": runtime.model_name,
    }


def get_tts_runtime_summary() -> dict[str, Any]:
    """Return one compact summary of local TTS capability."""
    return {
        "available": bool(is_tts_runtime_available()),
        "default_voice": get_default_tts_voice(),
        "default_speed": float(get_default_tts_speed()),
    }
