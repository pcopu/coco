"""Unit tests for Config — env var loading, validation, and user access."""

from pathlib import Path

import pytest

import coco.config as config_mod
from coco.config import Config


@pytest.fixture
def _base_env(monkeypatch, tmp_path):
    # chdir to tmp_path so load_dotenv won't find the real .env in repo root
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test:token")
    monkeypatch.setenv("ALLOWED_USERS", "12345")
    monkeypatch.setenv("COCO_DIR", str(tmp_path))


@pytest.mark.usefixtures("_base_env")
class TestConfigValid:
    def test_valid_config(self):
        cfg = Config()
        assert cfg.telegram_bot_token == "test:token"
        assert cfg.allowed_users == {12345}

    def test_custom_monitor_poll_interval(self, monkeypatch):
        monkeypatch.setenv("MONITOR_POLL_INTERVAL", "5.0")
        cfg = Config()
        assert cfg.monitor_poll_interval == 5.0

    def test_runtime_mode_defaults_to_app_server_only(self):
        cfg = Config()
        assert cfg.runtime_mode == "app_server_only"

    def test_runtime_mode_env_override(self, monkeypatch):
        monkeypatch.setenv("COCO_RUNTIME_MODE", "app_server_only")
        cfg = Config()
        assert cfg.runtime_mode == "app_server_only"

    def test_runtime_mode_invalid_forces_app_server_only(self, monkeypatch):
        monkeypatch.setenv("COCO_RUNTIME_MODE", "fast-mode")
        cfg = Config()
        assert cfg.runtime_mode == "app_server_only"

    def test_codex_sandbox_mode_defaults_to_danger_full_access(self):
        cfg = Config()
        assert cfg.codex_sandbox_mode == "danger-full-access"

    def test_codex_sandbox_mode_env_override(self, monkeypatch):
        monkeypatch.setenv("CODEX_SANDBOX_MODE", "workspace-write")
        cfg = Config()
        assert cfg.codex_sandbox_mode == "workspace-write"

    def test_is_user_allowed_true(self):
        cfg = Config()
        assert cfg.is_user_allowed(12345) is True

    def test_is_user_allowed_false(self):
        cfg = Config()
        assert cfg.is_user_allowed(99999) is False

    def test_show_user_messages_default_false(self):
        cfg = Config()
        assert cfg.show_user_messages is False

    def test_show_user_messages_true(self, monkeypatch):
        monkeypatch.setenv("SHOW_USER_MESSAGES", "true")
        cfg = Config()
        assert cfg.show_user_messages is True

    def test_browse_root_defaults_to_home_env(self, monkeypatch, tmp_path):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        (tmp_path / "env").mkdir()
        cfg = Config()
        assert cfg.browse_root == (tmp_path / "env").resolve()

    def test_browse_root_falls_back_to_cwd_when_home_env_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        cfg = Config()
        assert cfg.browse_root == tmp_path.resolve()

    def test_apps_paths_defaults_include_repo_and_config_dir(self):
        cfg = Config()
        assert len(cfg.apps_paths) >= 1
        assert cfg.config_dir / "apps" in cfg.apps_paths

    def test_apps_paths_env_override(self, monkeypatch, tmp_path):
        custom_a = tmp_path / "apps-a"
        custom_b = tmp_path / "apps-b"
        monkeypatch.setenv("COCO_APPS_PATHS", f"{custom_a},{custom_b}")
        cfg = Config()
        assert cfg.apps_paths == [custom_a.resolve(), custom_b.resolve()]

    def test_apps_paths_legacy_env_override_still_supported(self, monkeypatch, tmp_path):
        custom_a = tmp_path / "legacy-a"
        custom_b = tmp_path / "legacy-b"
        monkeypatch.setenv("COCO_SKILLS_PATHS", f"{custom_a},{custom_b}")
        cfg = Config()
        assert cfg.apps_paths == [custom_a.resolve(), custom_b.resolve()]
        assert cfg.skills_paths == cfg.apps_paths

    def test_codex_skills_paths_default(self):
        cfg = Config()
        assert cfg.codex_skills_paths == [(Path.home() / ".codex" / "skills").resolve()]

    def test_codex_skills_paths_env_override(self, monkeypatch, tmp_path):
        custom_a = tmp_path / "codex-a"
        custom_b = tmp_path / "codex-b"
        monkeypatch.setenv("COCO_CODEX_SKILLS_PATHS", f"{custom_a},{custom_b}")
        cfg = Config()
        assert cfg.codex_skills_paths == [custom_a.resolve(), custom_b.resolve()]

    def test_auth_env_file_overrides_allowed_users(self, monkeypatch, tmp_path):
        auth_env = tmp_path / "auth.env"
        auth_env.write_text("ALLOWED_USERS=999,1000\n", encoding="utf-8")
        monkeypatch.setenv("COCO_AUTH_ENV_FILE", str(auth_env))
        monkeypatch.setenv("ALLOWED_USERS", "12345")
        cfg = Config()
        assert cfg.allowed_users == {999, 1000}

    def test_auth_meta_file_env_override(self, monkeypatch, tmp_path):
        meta_path = tmp_path / "auth" / "allowed_users_meta.json"
        monkeypatch.setenv("COCO_AUTH_META_FILE", str(meta_path))
        cfg = Config()
        assert cfg.auth_meta_file == meta_path

    def test_rpc_defaults(self):
        cfg = Config()
        assert cfg.rpc_listen_host == "127.0.0.1"
        assert cfg.rpc_port == 8787
        assert cfg.rpc_advertise_host == ""
        assert cfg.controller_rpc_host == ""
        assert cfg.controller_rpc_port == 8787

    def test_agent_role_allows_missing_telegram_auth(self, monkeypatch):
        monkeypatch.setenv("COCO_NODE_ROLE", "agent")
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("ALLOWED_USERS", raising=False)

        cfg = Config()

        assert cfg.node_role == "agent"
        assert cfg.telegram_bot_token == ""
        assert cfg.allowed_users == set()

    def test_coco_env_namespace_controls_runtime(self, monkeypatch, tmp_path):
        coco_auth = tmp_path / "coco-auth.env"
        coco_auth.write_text("ALLOWED_USERS=202\n", encoding="utf-8")
        monkeypatch.setenv("COCO_NODE_ROLE", "agent")
        monkeypatch.setenv("COCO_RPC_PORT", "9900")
        monkeypatch.setenv("COCO_MACHINE_ID", "coco-node")
        monkeypatch.setenv("COCO_AUTH_ENV_FILE", str(coco_auth))
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("ALLOWED_USERS", raising=False)

        cfg = Config()

        assert cfg.node_role == "agent"
        assert cfg.rpc_port == 9900
        assert cfg.machine_id == "coco-node"
        assert cfg.auth_env_file == coco_auth


@pytest.mark.usefixtures("_base_env")
class TestConfigMissingEnv:
    def test_missing_telegram_bot_token(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
            Config()

    def test_missing_allowed_users(self, monkeypatch):
        monkeypatch.delenv("ALLOWED_USERS", raising=False)
        with pytest.raises(ValueError, match="ALLOWED_USERS"):
            Config()

    def test_non_numeric_allowed_users(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_USERS", "abc")
        with pytest.raises(ValueError, match="non-numeric"):
            Config()

    def test_missing_auth_env_file_raises(self, monkeypatch):
        monkeypatch.setenv("COCO_AUTH_ENV_FILE", "/tmp/does-not-exist-auth.env")
        with pytest.raises(ValueError, match="COCO_AUTH_ENV_FILE does not exist"):
            Config()

    def test_missing_coco_auth_env_file_raises(self, monkeypatch):
        monkeypatch.setenv("COCO_AUTH_ENV_FILE", "/tmp/does-not-exist-auth.env")
        with pytest.raises(ValueError, match="COCO_AUTH_ENV_FILE does not exist"):
            Config()

    def test_invalid_rpc_port_raises_coco_message(self, monkeypatch):
        monkeypatch.setenv("COCO_RPC_PORT", "not-a-port")
        with pytest.raises(ValueError, match="COCO_RPC_PORT must be an integer"):
            Config()

    def test_default_machine_id_uses_coco_node_when_hostname_missing(self, monkeypatch):
        monkeypatch.delenv("COCO_MACHINE_ID", raising=False)
        monkeypatch.setattr(config_mod.socket, "gethostname", lambda: "")

        cfg = Config()

        assert cfg.machine_id == "coco-node"

    def test_controller_role_still_requires_telegram_bot_token(self, monkeypatch):
        monkeypatch.setenv("COCO_NODE_ROLE", "controller")
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

        with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
            Config()
