"""Tests for direct shell CLI slash-command bridging."""

from types import SimpleNamespace

import coco.command_cli as command_cli


def test_command_cli_mentions_on_updates_topic_state(monkeypatch, capsys):
    set_calls: list[tuple[str, bool]] = []

    monkeypatch.setattr(command_cli.bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(command_cli.bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(
        command_cli.bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        command_cli.bot.session_manager,
        "resolve_window_for_thread",
        lambda _uid, _tid, **_kwargs: "@42",
    )
    monkeypatch.setattr(
        command_cli.bot.session_manager,
        "get_window_mention_only",
        lambda _wid: False,
    )
    monkeypatch.setattr(
        command_cli.bot.session_manager,
        "set_window_mention_only",
        lambda wid, mention_only: set_calls.append((wid, mention_only)),
    )

    code = command_cli.main(
        [
            "mentions",
            "on",
            "--user-id",
            "1147817421",
            "--chat-id",
            "-100123",
            "--thread-id",
            "77",
        ]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert set_calls == [("@42", True)]
    assert "Mention-only mode is now `ON`" in out


def test_command_cli_fast_off_updates_topic_service_tier(monkeypatch, capsys):
    set_calls: list[tuple[int, int, int | None, str]] = []

    monkeypatch.setattr(command_cli.bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(command_cli.bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(
        command_cli.bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        command_cli.bot.session_manager,
        "ensure_topic_binding",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        command_cli.bot.session_manager,
        "get_topic_service_tier_selection",
        lambda *_args, **_kwargs: "fast",
    )
    monkeypatch.setattr(
        command_cli.bot.session_manager,
        "set_topic_service_tier_selection",
        lambda user_id, thread_id, *, chat_id=None, service_tier="": set_calls.append(
            (user_id, thread_id, chat_id, service_tier)
        )
        or True,
    )

    code = command_cli.main(
        [
            "fast",
            "off",
            "--user-id",
            "1147817421",
            "--chat-id",
            "-100123",
            "--thread-id",
            "77",
        ]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert set_calls == [(1147817421, 77, -100123, "flex")]
    assert "Fast mode is now `OFF`" in out


def test_command_cli_transcription_reports_status(monkeypatch, capsys):
    monkeypatch.setattr(command_cli.bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(command_cli.bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(
        command_cli.bot,
        "resolve_transcription_runtime",
        lambda profile="": SimpleNamespace(
            profile="compatible",
            device="cpu",
            compute_type="int8",
            model_name="base",
            gpu_available=False,
        ),
    )

    code = command_cli.main(
        [
            "transcription",
            "--user-id",
            "1147817421",
            "--chat-id",
            "-100123",
            "--thread-id",
            "77",
        ]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "Server transcription mode: `COMPATIBLE`" in out
    assert "Resolved here: `cpu / int8 / base`" in out
