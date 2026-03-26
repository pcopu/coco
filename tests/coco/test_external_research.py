"""Tests for the shared external research runner."""

from __future__ import annotations

import json
from pathlib import Path

import coco.handlers.research_backend as research_backend


def test_run_external_research_defaults_to_codex_exec(tmp_path, monkeypatch):
    bundle_root = tmp_path / "bundles"
    monkeypatch.setenv("COCO_PERSONALITY_RESEARCH_BUNDLE_DIR", str(bundle_root))
    monkeypatch.setattr(research_backend.config, "assistant_command", "codex --model gpt-5.4")

    captured: dict[str, object] = {}

    def _fake_run(
        argv,
        *,
        cwd,
        env,
        input,
        capture_output,
        text,
        check,
        timeout,
    ):
        captured["argv"] = list(argv)
        captured["cwd"] = cwd
        captured["env"] = env
        captured["input"] = input

        output_path = Path(env["COCO_RESEARCH_OUTPUT_JSON"])
        output_path.write_text(
            json.dumps(
                {
                    "message_text": "Hey Morgan, external research found strong friction around Instagram login.",
                    "session_count": 1,
                }
            ),
            encoding="utf-8",
        )

        class _Result:
            returncode = 0
            stderr = ""

        return _Result()

    monkeypatch.setattr(research_backend.subprocess, "run", _fake_run)

    payload = research_backend.run_external_research(
        app_slug="personality",
        env_prefix="COCO_PERSONALITY",
        target_date="2026-03-18",
        user_id=12345,
        chat_id=-100321,
        thread_id=77,
        bundle_payload={
            "target_date": "2026-03-18",
            "sessions": [],
        },
        program_markdown="# Personality Research\n\nReturn a short digest.",
    )

    assert payload == {
        "message_text": "Hey Morgan, external research found strong friction around Instagram login.",
        "session_count": 1,
    }
    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[0] == "codex"
    assert "exec" in argv
    assert argv.index("exec") > 0
    assert "--skip-git-repo-check" in argv
    assert "--output-schema" in argv
    assert "-o" in argv
    assert captured["cwd"] == str(bundle_root / "2026-03-18" / "12345_77")
    assert "Personality Research" in str(captured["input"])
    assert '"target_date": "2026-03-18"' in str(captured["input"])
