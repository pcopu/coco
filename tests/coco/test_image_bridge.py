"""Tests for Codex image bridge helpers."""

from pathlib import Path

import coco.bot as bot


def test_pick_image_prompt_prefers_caption():
    assert bot._pick_image_prompt("Check this chart") == "Check this chart"
    assert bot._pick_image_prompt("  A  ") == "A"


def test_pick_image_prompt_default_when_caption_missing():
    assert bot._pick_image_prompt(None) == "Please analyze this image."
    assert bot._pick_image_prompt("   ") == "Please analyze this image."


def test_build_codex_image_resume_cmd():
    cmd = bot._build_codex_image_resume_cmd(
        "/usr/bin/codex",
        "session-123",
        Path("/tmp/image.png"),
        "Inspect this",
    )
    assert cmd == [
        "/usr/bin/codex",
        "exec",
        "resume",
        "session-123",
        "--skip-git-repo-check",
        "-i",
        "/tmp/image.png",
        "Inspect this",
    ]


def test_tail_command_output_truncates_tail():
    raw = ("x" * 40).encode()
    tail = bot._tail_command_output(raw, limit=20)
    assert tail.startswith("… ")
    assert tail.endswith("x" * 18)
