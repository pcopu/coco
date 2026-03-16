"""Tests for /approvals helper behavior."""

from pathlib import Path
import shlex

import coco.bot as bot


def test_normalize_approval_mode_aliases():
    assert bot._normalize_approval_mode("default") == bot.APPROVAL_MODE_INHERIT
    assert bot._normalize_approval_mode("onrequest") == bot.APPROVAL_MODE_ON_REQUEST
    assert bot._normalize_approval_mode("agent") == bot.APPROVAL_MODE_FULL_AUTO
    assert bot._normalize_approval_mode("full_auto") == bot.APPROVAL_MODE_FULL_AUTO
    assert bot._normalize_approval_mode("yolo") == bot.APPROVAL_MODE_DANGEROUS
    assert bot._normalize_approval_mode("invalid") is None


def test_strip_codex_policy_flags_keeps_non_policy_args():
    args = [
        "codex",
        "--search",
        "-a",
        "on-request",
        "--full-auto",
        "--dangerously-bypass-approvals-and-sandbox",
        "--profile",
        "dev",
    ]
    cleaned = bot._strip_codex_policy_flags(args)
    assert cleaned == ["codex", "--search", "--profile", "dev"]


def test_build_assistant_args_for_never(monkeypatch):
    monkeypatch.setattr(
        bot.config,
        "assistant_command",
        "codex --search --ask-for-approval on-request",
    )
    args = bot._build_assistant_args_for_approval_mode("never")
    assert "--ask-for-approval" in args
    idx = args.index("--ask-for-approval")
    assert args[idx + 1] == "never"
    assert "--search" in args


def test_build_assistant_args_for_dangerous_strips_sandbox(monkeypatch):
    monkeypatch.setattr(
        bot.config,
        "assistant_command",
        "codex --search --sandbox read-only -a on-request",
    )
    args = bot._build_assistant_args_for_approval_mode("dangerous")
    assert "--dangerously-bypass-approvals-and-sandbox" in args
    assert "--sandbox" not in args
    assert "-a" not in args
    assert "--ask-for-approval" not in args


def test_build_assistant_args_for_inherit_uses_app_default(monkeypatch):
    monkeypatch.setattr(
        bot.config,
        "assistant_command",
        "codex --search --ask-for-approval on-request",
    )
    monkeypatch.setattr(bot.session_manager, "get_default_approval_mode", lambda: "never")
    args = bot._build_assistant_args_for_approval_mode("inherit")
    assert "--ask-for-approval" in args
    idx = args.index("--ask-for-approval")
    assert args[idx + 1] == "never"


def test_build_assistant_launch_command_appends_resume(monkeypatch):
    monkeypatch.setattr(
        bot.config,
        "assistant_command",
        "codex --search",
    )
    monkeypatch.setattr(bot.config, "session_provider", "codex")
    command = bot._build_assistant_launch_command(
        "on-request",
        resume_session_id="session-123",
    )
    parts = shlex.split(command)
    assert parts[-2:] == ["resume", "session-123"]
    assert "--ask-for-approval" in parts
    assert "on-request" in parts


def test_get_window_approval_mode_falls_back_to_app_default(monkeypatch):
    monkeypatch.setattr(bot.session_manager, "get_window_approval_mode", lambda _wid: "")
    monkeypatch.setattr(bot.session_manager, "get_default_approval_mode", lambda: "never")
    assert bot._get_window_approval_mode("@1") == bot.APPROVAL_MODE_NEVER


def test_build_approvals_keyboard_marks_current(monkeypatch):
    monkeypatch.setattr(bot.config, "assistant_command", "codex -a on-request")
    monkeypatch.setattr(bot.session_manager, "_save_state", lambda: None)
    bot.session_manager.window_states.clear()
    monkeypatch.setattr(bot.session_manager, "get_default_approval_mode", lambda: "")

    markup = bot._build_approvals_keyboard(
        "@1",
        defaults_view=False,
        can_use_dangerous=True,
    )
    labels = [button.text for row in markup.inline_keyboard for button in row]
    callbacks = {
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    }

    assert "Defaults" in labels
    assert "✅ Inherit" in labels
    assert "Agent" in labels
    assert f"{bot.CB_APPROVAL_SET}{bot.APPROVAL_MODE_NEVER}" in callbacks
    assert bot.CB_APPROVAL_OPEN_DEFAULTS in callbacks
    assert bot.CB_APPROVAL_REFRESH in callbacks


def test_build_approvals_keyboard_defaults_panel_and_no_dangerous(monkeypatch):
    monkeypatch.setattr(bot.config, "assistant_command", "codex -a on-request")
    monkeypatch.setattr(bot.session_manager, "get_default_approval_mode", lambda: "")

    markup = bot._build_approvals_keyboard(
        "@1",
        defaults_view=True,
        can_use_dangerous=False,
    )
    labels = [button.text for row in markup.inline_keyboard for button in row]
    callbacks = {
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    }

    assert "Session" in labels
    assert "✅ On Request" in labels
    assert "Dangerous" not in labels
    assert bot.CB_APPROVAL_OPEN_WINDOW in callbacks
    assert bot.CB_APPROVAL_REFRESH_DEFAULT in callbacks
    assert f"{bot.CB_APPROVAL_SET_DEFAULT}{bot.APPROVAL_MODE_NEVER}" in callbacks
    assert f"{bot.CB_APPROVAL_SET_DEFAULT}{bot.APPROVAL_MODE_DANGEROUS}" not in callbacks


def test_probe_workspace_write_access_success(tmp_path):
    checked_path, can_write, write_error = bot._probe_workspace_write_access(str(tmp_path))
    assert checked_path == str(tmp_path)
    assert can_write is True
    assert write_error is None


def test_probe_workspace_write_access_failure(monkeypatch, tmp_path):
    def _raise_permission_denied(self: Path, _text: str, *, encoding: str) -> int:
        raise PermissionError("permission denied")

    monkeypatch.setattr(Path, "write_text", _raise_permission_denied)
    checked_path, can_write, write_error = bot._probe_workspace_write_access(str(tmp_path))

    assert checked_path == str(tmp_path)
    assert can_write is False
    assert write_error is not None


def test_build_approvals_text_includes_runtime_write_status(monkeypatch):
    monkeypatch.setattr(bot.session_manager, "get_display_name", lambda _wid: "demo")
    monkeypatch.setattr(bot, "_get_app_default_approval_mode", lambda: bot.APPROVAL_MODE_NEVER)
    monkeypatch.setattr(bot, "_get_window_approval_override", lambda _wid: bot.APPROVAL_MODE_INHERIT)
    monkeypatch.setattr(bot, "_get_window_approval_mode", lambda _wid: bot.APPROVAL_MODE_NEVER)
    monkeypatch.setattr(
        bot,
        "_probe_workspace_write_access",
        lambda _workspace_dir: ("/tmp/demo", False, "permission denied"),
    )

    text = bot._build_approvals_text(
        1147817421,
        "@1",
        workspace_dir="/tmp/demo",
        defaults_view=False,
    )
    assert "App default: `never`" in text
    assert "Window override: `inherit (use app default)`" in text
    assert "Effective mode: `never`" in text
    assert "Workspace path: `/tmp/demo`" in text
    assert "Runtime write check: `not writable`" in text
    assert "Write error: `permission denied`" in text
    assert "Panel: `session override`" in text
