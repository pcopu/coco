"""Tests for topic capability discovery CLI."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import coco.topic_cli as topic_cli
from coco.session import TopicBinding, WindowState
from coco.skills import SkillDefinition


def _make_skill(name: str, description: str) -> SkillDefinition:
    return SkillDefinition(
        name=name,
        description=description,
        skill_md_path=Path(f"/tmp/{name}/SKILL.md"),
        source_root=Path("/tmp"),
        folder_name=name,
        icon="",
    )


def test_topic_cli_renders_text_summary(monkeypatch, capsys):
    catalog = {
        "looper": _make_skill("looper", "Periodic plan nudges."),
        "personality": _make_skill("personality", "Daily fit note."),
    }
    binding = TopicBinding(
        chat_id=-100123,
        thread_id=77,
        window_id="@77",
        cwd="/repo/project",
        display_name="fmw-x",
        model_slug="gpt-5.4",
        reasoning_effort="medium",
        service_tier="flex",
    )
    looper_state = SimpleNamespace(
        plan_path="plans/ship.md",
        keyword="done",
        instructions="",
        interval_seconds=900,
        prompt_count=2,
        deadline_at=0.0,
    )

    monkeypatch.setattr(
        topic_cli.app_cli,
        "_resolve_topic_target",
        lambda **_kwargs: topic_cli.app_cli.TopicTarget(
            user_id=1147817421,
            chat_id=-100123,
            thread_id=77,
            binding=binding,
        ),
    )
    monkeypatch.setattr(topic_cli.session_manager, "discover_skill_catalog", lambda: catalog)
    monkeypatch.setattr(
        topic_cli.session_manager,
        "resolve_thread_skills",
        lambda *_args, **_kwargs: [catalog["looper"]],
    )
    monkeypatch.setattr(
        topic_cli.session_manager,
        "resolve_topic_binding",
        lambda *_args, **_kwargs: binding,
    )
    monkeypatch.setattr(
        topic_cli.session_manager,
        "get_window_state",
        lambda _wid: WindowState(cwd="/repo/project", window_name="fmw-x", mention_only=True),
    )
    monkeypatch.setattr(topic_cli.session_manager, "get_window_mention_only", lambda _wid: True)
    monkeypatch.setattr(
        topic_cli.session_manager,
        "get_topic_model_selection",
        lambda *_args, **_kwargs: ("gpt-5.4", "medium"),
    )
    monkeypatch.setattr(
        topic_cli.session_manager,
        "get_topic_service_tier_selection",
        lambda *_args, **_kwargs: "flex",
    )
    monkeypatch.setattr(topic_cli, "get_looper_state", lambda **_kwargs: looper_state)
    monkeypatch.setattr(topic_cli, "get_autoresearch_state", lambda *_args, **_kwargs: None)

    code = topic_cli.main(
        [
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
    assert "Topic: user=1147817421 chat=-100123 thread=77" in out
    assert "Workspace: `/repo/project`" in out
    assert "Display: `fmw-x`" in out
    assert "Mention-only: `ON`" in out
    assert "Model: `gpt-5.4`" in out
    assert "Fast mode: `OFF` (`flex`)" in out
    assert "Enabled apps: `looper`" in out
    assert "- `looper`: enabled; actions: status, start, stop, enable, disable" in out


def test_topic_cli_json_includes_capabilities_and_state(monkeypatch, capsys):
    catalog = {
        "looper": _make_skill("looper", "Periodic plan nudges."),
        "autoresearch": _make_skill("autoresearch", "Daily research digests."),
        "personality": _make_skill("personality", "Daily fit note."),
    }
    binding = TopicBinding(
        chat_id=-100123,
        thread_id=77,
        window_id="@77",
        cwd="/repo/project",
        display_name="fmw-x",
        model_slug="gpt-5.4",
        reasoning_effort="medium",
        service_tier="fast",
    )
    looper_state = SimpleNamespace(
        plan_path="plans/ship.md",
        keyword="done",
        instructions="focus tests",
        interval_seconds=900,
        prompt_count=2,
        deadline_at=0.0,
    )
    autoresearch_state = SimpleNamespace(
        outcome="Close more inbound leads",
        last_researched_for_date="2026-04-09",
        last_delivered_for_date="2026-04-09",
        last_digest_text="digest text",
        last_digest_generated_at=1.0,
        last_session_count=3,
    )

    monkeypatch.setattr(
        topic_cli.app_cli,
        "_resolve_topic_target",
        lambda **_kwargs: topic_cli.app_cli.TopicTarget(
            user_id=1147817421,
            chat_id=-100123,
            thread_id=77,
            binding=binding,
        ),
    )
    monkeypatch.setattr(topic_cli.session_manager, "discover_skill_catalog", lambda: catalog)
    monkeypatch.setattr(
        topic_cli.session_manager,
        "resolve_thread_skills",
        lambda *_args, **_kwargs: [catalog["looper"], catalog["autoresearch"]],
    )
    monkeypatch.setattr(
        topic_cli.session_manager,
        "resolve_topic_binding",
        lambda *_args, **_kwargs: binding,
    )
    monkeypatch.setattr(
        topic_cli.session_manager,
        "get_window_state",
        lambda _wid: WindowState(
            cwd="/repo/project",
            window_name="fmw-x",
            mention_only=False,
            codex_thread_id="thread_123",
            codex_active_turn_id="turn_456",
        ),
    )
    monkeypatch.setattr(topic_cli.session_manager, "get_window_mention_only", lambda _wid: False)
    monkeypatch.setattr(
        topic_cli.session_manager,
        "get_topic_model_selection",
        lambda *_args, **_kwargs: ("gpt-5.4", "medium"),
    )
    monkeypatch.setattr(
        topic_cli.session_manager,
        "get_topic_service_tier_selection",
        lambda *_args, **_kwargs: "fast",
    )
    monkeypatch.setattr(
        topic_cli,
        "resolve_transcription_runtime",
        lambda profile="": SimpleNamespace(
            profile="compatible",
            device="cpu",
            compute_type="int8",
            model_name="base",
            gpu_available=False,
        ),
    )
    monkeypatch.setattr(topic_cli, "get_looper_state", lambda **_kwargs: looper_state)
    monkeypatch.setattr(
        topic_cli,
        "get_autoresearch_state",
        lambda *_args, **_kwargs: autoresearch_state,
    )

    code = topic_cli.main(
        [
            "--json",
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
    payload = json.loads(out)
    assert payload["topic"] == {
        "user_id": 1147817421,
        "chat_id": -100123,
        "thread_id": 77,
        "window_id": "@77",
        "cwd": "/repo/project",
        "display_name": "fmw-x",
    }
    assert payload["session"] == {
        "bound": True,
        "mention_only": False,
        "model": "gpt-5.4",
        "reasoning_effort": "medium",
        "service_tier": "fast",
        "fast_mode": True,
        "codex_thread_id": "thread_123",
        "active_turn_id": "turn_456",
    }
    assert "mentions" in payload["commands"]["available"]
    assert payload["commands"]["examples"]["looper"] == "coco looper start plans/ship.md done --every 15m"
    assert payload["transcription"] == {
        "mode": "compatible",
        "device": "cpu",
        "compute_type": "int8",
        "model_name": "base",
    }
    apps = {app["name"]: app for app in payload["apps"]}
    assert apps["looper"]["enabled"] is True
    assert apps["looper"]["state"]["plan_path"] == "plans/ship.md"
    assert apps["autoresearch"]["enabled"] is True
    assert apps["autoresearch"]["state"]["outcome"] == "Close more inbound leads"
    assert apps["personality"]["enabled"] is False


def test_topic_cli_send_text_dispatches_to_bound_topic(monkeypatch, capsys):
    calls: list[dict[str, object]] = []
    binding = TopicBinding(chat_id=-100123, thread_id=77, window_id="@77")

    monkeypatch.setattr(
        topic_cli.app_cli,
        "_resolve_topic_target",
        lambda **_kwargs: topic_cli.app_cli.TopicTarget(
            user_id=1147817421,
            chat_id=-100123,
            thread_id=77,
            binding=binding,
        ),
    )
    monkeypatch.setattr(
        topic_cli,
        "_send_text_to_current_topic",
        lambda **kwargs: calls.append(kwargs) or (True, ""),
    )

    code = topic_cli.main(
        [
            "send",
            "--text",
            "hello from cron",
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
    assert calls == [
        {
            "target": topic_cli.app_cli.TopicTarget(
                user_id=1147817421,
                chat_id=-100123,
                thread_id=77,
                binding=binding,
            ),
            "text": "hello from cron",
        }
    ]
    assert "Sent message to topic." in out


def test_topic_cli_send_text_file_returns_nonzero_on_failure(monkeypatch, capsys, tmp_path):
    binding = TopicBinding(chat_id=-100123, thread_id=77, window_id="@77")
    message_path = tmp_path / "message.md"
    message_path.write_text("file text", encoding="utf-8")

    monkeypatch.setattr(
        topic_cli.app_cli,
        "_resolve_topic_target",
        lambda **_kwargs: topic_cli.app_cli.TopicTarget(
            user_id=1147817421,
            chat_id=-100123,
            thread_id=77,
            binding=binding,
        ),
    )
    monkeypatch.setattr(
        topic_cli,
        "_send_text_to_current_topic",
        lambda **_kwargs: (False, "telegram failed"),
    )

    code = topic_cli.main(
        [
            "send",
            "--text-file",
            str(message_path),
            "--user-id",
            "1147817421",
            "--chat-id",
            "-100123",
            "--thread-id",
            "77",
        ]
    )

    err = capsys.readouterr().err
    assert code == 1
    assert "telegram failed" in err


def test_topic_cli_send_text_with_image_url_dispatches_to_bound_topic(monkeypatch, capsys):
    calls: list[dict[str, object]] = []
    binding = TopicBinding(chat_id=-100123, thread_id=77, window_id="@77")

    monkeypatch.setattr(
        topic_cli.app_cli,
        "_resolve_topic_target",
        lambda **_kwargs: topic_cli.app_cli.TopicTarget(
            user_id=1147817421,
            chat_id=-100123,
            thread_id=77,
            binding=binding,
        ),
    )
    monkeypatch.setattr(
        topic_cli,
        "_send_message_to_current_topic",
        lambda **kwargs: calls.append(kwargs) or (True, ""),
    )

    code = topic_cli.main(
        [
            "send",
            "--text",
            "hello from cron",
            "--image-url",
            "https://example.com/image.jpg",
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
    assert calls == [
        {
            "target": topic_cli.app_cli.TopicTarget(
                user_id=1147817421,
                chat_id=-100123,
                thread_id=77,
                binding=binding,
            ),
            "text": "hello from cron",
            "image_url": "https://example.com/image.jpg",
            "image_file": "",
        }
    ]
    assert "Sent message to topic." in out
