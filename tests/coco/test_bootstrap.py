"""Tests for quick-start CoCo bootstrap helpers."""

from __future__ import annotations

import json

import coco.admin as admin
import coco.bootstrap as bootstrap


def test_bootstrap_main_writes_local_env_and_meta(tmp_path):
    code = bootstrap.main(
        [
            "--config-dir",
            str(tmp_path),
            "--bot-token",
            "123:ABC",
            "--admin-user",
            "9",
            "--admin-name",
            "Owner",
            "--group-id",
            "-100123",
        ]
    )

    assert code == 0
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "TELEGRAM_BOT_TOKEN=123:ABC" in env_text
    assert "ALLOWED_USERS=9" in env_text
    assert "ALLOWED_GROUP_IDS=-100123" in env_text

    payload = json.loads((tmp_path / "allowed_users_meta.json").read_text(encoding="utf-8"))
    assert payload["names"]["9"] == "Owner"
    assert payload["admins"] == [9]
    assert payload["scopes"]["9"] == admin.SCOPE_CREATE_SESSIONS


def test_bootstrap_main_requires_group_id_by_default(tmp_path, capsys):
    code = bootstrap.main(
        [
            "--config-dir",
            str(tmp_path),
            "--bot-token",
            "123:ABC",
            "--admin-user",
            "9",
        ]
    )

    captured = capsys.readouterr()
    assert code == 1
    assert "group" in captured.err.lower()

