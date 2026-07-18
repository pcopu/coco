"""Shared helpers for updating source and uv-tool CoCo installations."""

from __future__ import annotations

import shutil
from pathlib import Path

from .utils import env_alias


DEFAULT_COCO_INSTALL_SPEC = "git+https://github.com/pcopu/coco.git"
_COCO_INSTALL_SPEC_ENV = "COCO_INSTALL_SPEC"
_COCO_UV_BIN_ENV = "COCO_UV_BIN"


def resolve_uv_binary() -> str:
    """Find uv even when a service has a minimal PATH."""
    configured = env_alias(_COCO_UV_BIN_ENV).strip()
    if configured:
        return str(Path(configured).expanduser())

    discovered = shutil.which("uv")
    if discovered:
        return discovered

    for candidate in (Path.home() / ".local/bin/uv", Path.home() / ".cargo/bin/uv"):
        if candidate.is_file() and candidate.stat().st_mode & 0o111:
            return str(candidate)
    return ""


def resolve_coco_tool_update_argv() -> list[str]:
    """Build safe argv to refresh an installation created by install.sh."""
    uv_binary = resolve_uv_binary()
    if not uv_binary:
        return []
    install_spec = env_alias(_COCO_INSTALL_SPEC_ENV).strip() or DEFAULT_COCO_INSTALL_SPEC
    return [uv_binary, "tool", "install", "--force", install_spec]
