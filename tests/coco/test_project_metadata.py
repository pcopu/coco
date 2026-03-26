"""Tests for user-facing project branding metadata."""

from __future__ import annotations

import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_pyproject_uses_coco_branding() -> None:
    payload = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = payload["project"]
    scripts = project["scripts"]
    wheel_packages = payload["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    coverage_sources = payload["tool"]["coverage"]["run"]["source"]

    assert project["name"] == "coco"
    assert (
        project["description"]
        == "Telegram operations overlay for OpenAI Codex, derived from ccbot."
    )
    assert scripts["coco"] == "coco.main:main"
    assert scripts["coco-admin"] == "coco.admin:main"
    assert sorted(scripts) == ["coco", "coco-admin"]
    assert wheel_packages == ["src/coco"]
    assert coverage_sources == ["coco"]


def test_readme_uses_coco_title() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert readme.startswith("# CoCo: Orchestrate Codex across machines through Telegram.\n")
    assert "uv run coco" in readme
    assert "sudo coco-admin show" in readme
    assert "doc/multi-machine-setup.md" in readme


def test_chinese_readme_uses_coco_cli_and_admin_surface() -> None:
    readme = (REPO_ROOT / "README_CN.md").read_text(encoding="utf-8")
    assert "uv run coco" in readme
    assert "sudo coco-admin show" in readme
    assert "COCO_DIR" in readme
