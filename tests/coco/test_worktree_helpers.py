"""Tests for /worktree helper logic."""

from pathlib import Path

import pytest

import coco.bot as bot
from coco.handlers.callback_data import (
    CB_WORKTREE_FOLD_MENU,
    CB_WORKTREE_NEW,
    CB_WORKTREE_REFRESH,
)


def test_parse_git_worktree_porcelain():
    text = (
        "worktree /repo/main\n"
        "HEAD 1111111\n"
        "branch refs/heads/main\n"
        "\n"
        "worktree /repo/main-wt-auth\n"
        "HEAD 2222222\n"
        "branch refs/heads/wt/auth-fix\n"
        "\n"
        "worktree /repo/detached\n"
        "HEAD 3333333\n"
        "detached\n"
    )
    entries = bot._parse_git_worktree_porcelain(text)
    assert len(entries) == 3
    assert entries[0]["path"] == "/repo/main"
    assert entries[0]["branch"] == "main"
    assert entries[1]["branch"] == "wt/auth-fix"
    assert entries[2]["branch"] == "(detached)"


def test_resolve_worktree_selector_matches_name_branch_and_path():
    entries = [
        {"path": "/repo/main", "branch": "main"},
        {"path": "/repo/main-wt-auth", "branch": "wt/auth-fix"},
    ]
    by_name = bot._resolve_worktree_selector(entries, "main-wt-auth")
    by_branch = bot._resolve_worktree_selector(entries, "wt/auth-fix")
    by_path = bot._resolve_worktree_selector(entries, "/repo/main")
    missing = bot._resolve_worktree_selector(entries, "unknown")

    assert by_name == entries[1]
    assert by_branch == entries[1]
    assert by_path == entries[0]
    assert missing is None


def test_sanitize_worktree_name():
    assert bot._sanitize_worktree_name(" auth fix ") == "auth-fix"
    assert bot._sanitize_worktree_name("###") != ""


def test_worktree_panel_keyboard_has_expected_callbacks():
    markup = bot._build_worktree_panel_keyboard()
    callback_data = {
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    }
    assert CB_WORKTREE_NEW in callback_data
    assert CB_WORKTREE_FOLD_MENU in callback_data
    assert CB_WORKTREE_REFRESH in callback_data


def test_pick_worktree_path_avoids_conflicts(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    conflict = tmp_path / "repo-wt-auth-fix"
    conflict.mkdir()

    picked = bot._pick_worktree_path(repo_root, "auth-fix")
    assert picked != conflict
    assert picked.name.startswith("repo-wt-auth-fix")


def test_fold_worktrees_requires_primary_worktree(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "_is_primary_worktree", lambda _path: False)
    ok, msg = bot._fold_worktrees_into_branch(
        target_cwd=Path(tmp_path),
        selectors=["feature-a"],
    )
    assert ok is False
    assert "primary repository worktree" in msg


@pytest.mark.asyncio
async def test_build_worktree_handoff_prompt_omits_assistant_progress(monkeypatch):
    async def _get_recent_messages(_window_id):
        return (
            [
                {
                    "role": "user",
                    "text": "Investigate the billing page",
                    "content_type": "text",
                },
                {
                    "role": "assistant",
                    "text": "First I will inspect the router",
                    "content_type": "progress",
                },
                {
                    "role": "assistant",
                    "text": "The bug is in the billing page redirect.",
                    "content_type": "text",
                },
            ],
            3,
        )

    monkeypatch.setattr(bot.session_manager, "get_recent_messages", _get_recent_messages)

    prompt = await bot._build_worktree_handoff_prompt("@1", 77)

    assert "Investigate the billing page" in prompt
    assert "The bug is in the billing page redirect." in prompt
    assert "First I will inspect the router" not in prompt
