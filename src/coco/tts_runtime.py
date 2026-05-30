"""Managed local TTS server lifecycle for CoCo."""

from __future__ import annotations

import asyncio
import logging
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .tts import _tts_base_url
from .utils import env_alias

logger = logging.getLogger(__name__)

_tts_server_process: subprocess.Popen[bytes] | None = None


def _tts_server_model() -> str:
    return env_alias("COCO_TTS_MODEL", default="supertonic-3").strip() or "supertonic-3"


def _tts_server_log_level() -> str:
    return env_alias("COCO_TTS_LOG_LEVEL", default="warning").strip() or "warning"


def _tts_server_start_timeout() -> float:
    raw = env_alias("COCO_TTS_START_TIMEOUT_SECONDS", default="60").strip()
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 60.0


def _tts_server_poll_interval() -> float:
    raw = env_alias("COCO_TTS_POLL_INTERVAL_SECONDS", default="0.25").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.25


def _tts_command_override() -> str:
    return env_alias("COCO_TTS_COMMAND", default="").strip()


def _parsed_tts_base_url():
    return urlparse(_tts_base_url())


def _is_local_managed_base_url() -> bool:
    parsed = _parsed_tts_base_url()
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").strip().lower()
    return host in {"127.0.0.1", "localhost"}


def _healthcheck_url() -> str:
    parsed = _parsed_tts_base_url()
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 7788
    scheme = parsed.scheme or "http"
    return f"{scheme}://{host}:{port}/v1/health"


def _resolve_tts_command() -> list[str]:
    override = _tts_command_override()
    if override:
        return shlex.split(override)

    virtual_env = env_alias("VIRTUAL_ENV", default="").strip()
    if virtual_env:
        venv_supertonic = Path(virtual_env) / "bin" / "supertonic"
    else:
        venv_supertonic = Path(sys.prefix) / "bin" / "supertonic"
    if venv_supertonic.exists():
        binary = str(venv_supertonic)
    else:
        binary = shutil.which("supertonic") or "supertonic"

    parsed = _parsed_tts_base_url()
    host = parsed.hostname or "127.0.0.1"
    port = str(parsed.port or 7788)
    return [
        binary,
        "serve",
        "--host",
        host,
        "--port",
        port,
        "--model",
        _tts_server_model(),
        "--log-level",
        _tts_server_log_level(),
    ]


def is_tts_runtime_available() -> bool:
    """Return whether this machine can satisfy local TTS requests."""
    if not _tts_base_url():
        return False

    if not _is_local_managed_base_url():
        return True

    command = _resolve_tts_command()
    if not command:
        return False
    binary = command[0].strip()
    if not binary:
        return False
    if "/" in binary:
        return Path(binary).exists()
    return shutil.which(binary) is not None


async def is_tts_server_healthy() -> bool:
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(_healthcheck_url())
            response.raise_for_status()
            payload = response.json()
    except Exception:
        return False
    return str(payload.get("status", "")).strip().lower() == "ok"


async def ensure_tts_server_started() -> None:
    global _tts_server_process

    if not _is_local_managed_base_url():
        return

    if await is_tts_server_healthy():
        return

    if _tts_server_process is None or _tts_server_process.poll() is not None:
        command = _resolve_tts_command()
        logger.info("Starting managed TTS server: %s", command)
        _tts_server_process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    deadline = time.monotonic() + _tts_server_start_timeout()
    while time.monotonic() < deadline:
        proc = _tts_server_process
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(
                f"managed TTS server exited before becoming healthy (code {proc.returncode})"
            )
        if await is_tts_server_healthy():
            return
        await asyncio.sleep(_tts_server_poll_interval())

    raise RuntimeError("managed TTS server did not become healthy before timeout")


async def stop_tts_server() -> None:
    global _tts_server_process

    proc = _tts_server_process
    _tts_server_process = None
    if proc is None:
        return
    if proc.poll() is not None:
        return

    proc.terminate()
    try:
        await asyncio.to_thread(proc.wait, 5.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        await asyncio.to_thread(proc.wait, 5.0)
