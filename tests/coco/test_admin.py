"""Tests for local root-only auth admin CLI."""

import json

import coco.admin as admin


def test_main_requires_root(monkeypatch, capsys):
    monkeypatch.setattr(admin.os, "geteuid", lambda: 1000)
    code = admin.main(["show"])
    captured = capsys.readouterr()
    assert code == 1
    assert "must run as root" in captured.err


def test_set_users_writes_auth_files(tmp_path, monkeypatch):
    monkeypatch.setattr(admin.os, "geteuid", lambda: 0)
    auth_dir = tmp_path / "auth"

    code = admin.main(
        [
            "--auth-dir",
            str(auth_dir),
            "--no-immutable",
            "set-users",
            "9,3",
        ]
    )
    assert code == 0

    env_text = (auth_dir / "auth.env").read_text(encoding="utf-8")
    assert "ALLOWED_USERS=3,9" in env_text

    payload = json.loads((auth_dir / "allowed_users_meta.json").read_text(encoding="utf-8"))
    assert payload["admins"] == [3]
    assert payload["scopes"]["3"] == admin.SCOPE_CREATE_SESSIONS
    assert payload["scopes"]["9"] == admin.SCOPE_SINGLE_SESSION


def test_add_and_remove_user_updates_state(tmp_path, monkeypatch):
    monkeypatch.setattr(admin.os, "geteuid", lambda: 0)
    auth_dir = tmp_path / "auth"

    code = admin.main(
        [
            "--auth-dir",
            str(auth_dir),
            "--no-immutable",
            "set-users",
            "3",
        ]
    )
    assert code == 0

    code = admin.main(
        [
            "--auth-dir",
            str(auth_dir),
            "--no-immutable",
            "add-user",
            "20",
            "--name",
            "Alice",
            "--scope",
            admin.SCOPE_CREATE_SESSIONS,
            "--admin",
        ]
    )
    assert code == 0

    payload = json.loads((auth_dir / "allowed_users_meta.json").read_text(encoding="utf-8"))
    assert payload["names"]["20"] == "Alice"
    assert 20 in payload["admins"]
    assert payload["scopes"]["20"] == admin.SCOPE_CREATE_SESSIONS

    code = admin.main(
        [
            "--auth-dir",
            str(auth_dir),
            "--no-immutable",
            "remove-user",
            "20",
        ]
    )
    assert code == 0
    env_text = (auth_dir / "auth.env").read_text(encoding="utf-8")
    assert "ALLOWED_USERS=3" in env_text


def test_parser_prefers_coco_env_defaults(monkeypatch):
    monkeypatch.setenv("COCO_AUTH_DIR", "/tmp/coco-auth")
    monkeypatch.setenv("COCO_SERVICE_ENV_FILE", "/tmp/coco.env")
    monkeypatch.setenv("COCO_GROUP_REQUESTS_FILE", "/tmp/coco.json")

    parser = admin._build_parser()
    args = parser.parse_args(["show"])

    assert args.auth_dir == "/tmp/coco-auth"
    assert args.service_env_file == "/tmp/coco.env"
    assert args.group_requests_file == "/tmp/coco.json"


def test_parser_help_is_coco_only():
    parser = admin._build_parser()
    help_text = " ".join(parser.format_help().split())

    assert "Local root-only auth manager for CoCo." in help_text
    assert "default: /etc/coco/auth" in help_text
    assert "default: /etc/coco/coco.env" in help_text
    assert "default: /var/lib/coco/group_allow_requests.json" in help_text


def test_default_paths_prefer_coco_when_present(monkeypatch):
    def _exists(self):
        return str(self) in {
            "/etc/coco/auth",
            "/etc/coco/coco.env",
            "/var/lib/coco/group_allow_requests.json",
        }

    monkeypatch.setattr(admin.Path, "exists", _exists)

    assert admin._default_auth_dir() == admin.Path("/etc/coco/auth")
    assert admin._default_service_env_file() == admin.Path("/etc/coco/coco.env")
    assert admin._default_group_requests_file() == admin.Path(
        "/var/lib/coco/group_allow_requests.json"
    )


def test_default_paths_fall_back_past_permission_errors(monkeypatch):
    def _exists(self):
        raw = str(self)
        if raw in {
            "/etc/coco/auth",
            "/etc/coco/coco.env",
            "/var/lib/coco/group_allow_requests.json",
        }:
            raise PermissionError("blocked")
        return raw in {
            "/etc/codex/auth",
            "/etc/codex/codex.env",
            "/var/lib/codex/group_allow_requests.json",
        }

    monkeypatch.setattr(admin.Path, "exists", _exists)

    assert admin._default_auth_dir() == admin.Path("/etc/codex/auth")
    assert admin._default_service_env_file() == admin.Path("/etc/codex/codex.env")
    assert admin._default_group_requests_file() == admin.Path(
        "/var/lib/codex/group_allow_requests.json"
    )


def test_load_allowed_groups_ignores_permission_error(monkeypatch):
    target = admin.Path("/etc/codex/codex.env")

    def _is_file(self):
        if str(self) == str(target):
            raise PermissionError("blocked")
        return False

    monkeypatch.setattr(admin.Path, "is_file", _is_file)

    assert admin._load_allowed_groups(target) == set()


def test_bootstrap_writes_service_env_and_auth_files(tmp_path, monkeypatch):
    monkeypatch.setattr(admin.os, "geteuid", lambda: 0)
    auth_dir = tmp_path / "auth"
    service_env = tmp_path / "coco.env"

    code = admin.main(
        [
            "--auth-dir",
            str(auth_dir),
            "--service-env-file",
            str(service_env),
            "--no-immutable",
            "bootstrap",
            "--bot-token",
            "123:ABC",
            "--admin-user",
            "7",
            "--admin-name",
            "Owner",
            "--group-id",
            "-100123",
        ]
    )

    assert code == 0

    auth_env = auth_dir / "auth.env"
    auth_meta = auth_dir / "allowed_users_meta.json"
    auth_env_text = auth_env.read_text(encoding="utf-8")
    service_env_text = service_env.read_text(encoding="utf-8")
    auth_meta_payload = json.loads(auth_meta.read_text(encoding="utf-8"))

    assert "ALLOWED_USERS=7" in auth_env_text
    assert "TELEGRAM_BOT_TOKEN=123:ABC" in service_env_text
    assert "ALLOWED_GROUP_IDS=-100123" in service_env_text
    assert f"COCO_AUTH_ENV_FILE={auth_env}" in service_env_text
    assert f"COCO_AUTH_META_FILE={auth_meta}" in service_env_text
    assert auth_meta_payload["admins"] == [7]
    assert auth_meta_payload["scopes"]["7"] == admin.SCOPE_CREATE_SESSIONS
