"""Tests for direct shell CLI topic app management."""

from pathlib import Path
from types import SimpleNamespace

import coco.app_cli as app_cli
from coco.session import TopicBinding
from coco.skills import SkillDefinition


def _make_skill(name: str, *, icon: str = "") -> SkillDefinition:
    return SkillDefinition(
        name=name,
        description=f"{name} description",
        skill_md_path=Path(f"/tmp/{name}/SKILL.md"),
        source_root=Path("/tmp"),
        folder_name=name,
        icon=icon,
    )


def test_app_cli_enable_updates_topic_state(monkeypatch, capsys):
    catalog = {"demo": _make_skill("demo")}
    enabled_names: list[str] = []

    monkeypatch.setattr(app_cli.session_manager, "discover_skill_catalog", lambda: catalog)
    monkeypatch.setattr(
        app_cli.session_manager,
        "resolve_thread_skills",
        lambda *_args, **_kwargs: [catalog[name] for name in enabled_names],
    )
    monkeypatch.setattr(
        app_cli.session_manager,
        "set_thread_skills",
        lambda _uid, _tid, names, **_kwargs: enabled_names.__setitem__(slice(None), list(names)),
    )

    code = app_cli.main(
        [
            "enable",
            "demo",
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
    assert enabled_names == ["demo"]
    assert "Enabled app `demo`" in out


def test_app_cli_looper_start_uses_current_workspace_topic(monkeypatch, tmp_path, capsys):
    catalog = {"looper": _make_skill("looper", icon="🔁")}
    enabled_names: list[str] = []
    start_calls: list[dict[str, object]] = []

    monkeypatch.setattr(app_cli, "_current_working_directory", lambda: tmp_path)
    monkeypatch.setattr(
        app_cli.session_manager,
        "iter_topic_bindings",
        lambda: [
            (
                1147817421,
                -100123,
                77,
                TopicBinding(
                    chat_id=-100123,
                    thread_id=77,
                    window_id="@77",
                    cwd=str(tmp_path),
                ),
            )
        ],
    )
    monkeypatch.setattr(
        app_cli.session_manager,
        "resolve_window_for_thread",
        lambda _uid, _tid, **_kwargs: "@77",
    )
    monkeypatch.setattr(app_cli.session_manager, "discover_skill_catalog", lambda: catalog)
    monkeypatch.setattr(
        app_cli.session_manager,
        "resolve_thread_skills",
        lambda *_args, **_kwargs: [catalog[name] for name in enabled_names],
    )
    monkeypatch.setattr(
        app_cli.session_manager,
        "set_thread_skills",
        lambda _uid, _tid, names, **_kwargs: enabled_names.__setitem__(slice(None), list(names)),
    )

    def _start_looper(**kwargs):
        start_calls.append(kwargs)
        return SimpleNamespace(
            plan_path=kwargs["plan_path"],
            keyword=kwargs["keyword"],
            instructions=kwargs["instructions"],
            interval_seconds=int(kwargs["interval_seconds"]),
            started_at=100.0,
            deadline_at=0.0,
        )

    monkeypatch.setattr(app_cli, "start_looper", _start_looper)
    monkeypatch.setattr(app_cli, "build_looper_prompt", lambda **_kwargs: "example loop prompt")

    code = app_cli.main(["looper", "start", "plans/ship.md", "done", "--every", "15m"])

    out = capsys.readouterr().out
    assert code == 0
    assert start_calls
    assert start_calls[0]["plan_path"] == "plans/ship.md"
    assert start_calls[0]["keyword"] == "done"
    assert start_calls[0]["interval_seconds"] == 900
    assert enabled_names == ["looper"]
    assert "Looper started" in out
    assert "example loop prompt" in out


def test_app_cli_autoresearch_set_outcome_and_run(monkeypatch, capsys):
    set_calls: list[dict[str, object]] = []
    run_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        app_cli,
        "set_autoresearch_outcome",
        lambda **kwargs: set_calls.append(kwargs) or SimpleNamespace(outcome=kwargs["outcome"]),
    )
    monkeypatch.setattr(
        app_cli.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, chat_id=None: chat_id if chat_id is not None else -100123,
    )
    monkeypatch.setattr(
        app_cli,
        "run_autoresearch_now",
        lambda **kwargs: run_calls.append(kwargs) or "digest text",
    )

    code_set = app_cli.main(
        [
            "autoresearch",
            "set-outcome",
            "Close more inbound leads",
            "--user-id",
            "1147817421",
            "--chat-id",
            "-100123",
            "--thread-id",
            "77",
        ]
    )
    code_run = app_cli.main(
        [
            "autoresearch",
            "run",
            "--user-id",
            "1147817421",
            "--chat-id",
            "-100123",
            "--thread-id",
            "77",
        ]
    )

    out = capsys.readouterr().out
    assert code_set == 0
    assert code_run == 0
    assert set_calls == [
        {
            "user_id": 1147817421,
            "thread_id": 77,
            "outcome": "Close more inbound leads",
        }
    ]
    assert run_calls == [
        {
            "user_id": 1147817421,
            "chat_id": -100123,
            "thread_id": 77,
        }
    ]
    assert "Auto research outcome updated." in out
    assert "digest text" in out
