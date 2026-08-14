"""Security regressions for the local CoCo control workspace."""

from pathlib import Path
from types import SimpleNamespace
import tempfile

import pytest

import coco.bot as bot


def test_default_control_workspace_rejects_symlink_escape(tmp_path, monkeypatch):
    monkeypatch.setattr(bot.config, "config_dir", tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "_coco").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match="symlink"):
        bot._default_coco_control_workspace(
            user_id=10,
            thread_id=1,
            chat_id=-100123,
        )

    assert not (outside / "chat-100123" / "control").exists()


def test_control_workspace_allows_symlinked_config_root(tmp_path, monkeypatch):
    real_config_root = tmp_path / "real-config"
    real_config_root.mkdir()
    config_link = tmp_path / "config-link"
    config_link.symlink_to(real_config_root, target_is_directory=True)
    monkeypatch.setattr(bot.config, "config_dir", config_link)
    trusted: list[Path] = []
    monkeypatch.setattr(
        bot,
        "_ensure_codex_project_trust",
        lambda path: (trusted.append(path) is None, ""),
    )

    workspace = bot._ensure_local_coco_control_workspace(
        bot._default_coco_control_workspace(
            user_id=10,
            thread_id=1,
            chat_id=-100123,
        )
    )

    expected = real_config_root / "_coco" / "chat-100123" / "control"
    assert workspace == expected
    assert expected.is_dir()
    assert trusted == [expected]


@pytest.mark.parametrize("component", ["_coco", "chat-100123", "control"])
def test_default_control_workspace_rejects_non_directory_component(
    tmp_path,
    monkeypatch,
    component,
):
    monkeypatch.setattr(bot.config, "config_dir", tmp_path)
    target = tmp_path / "_coco" / "chat-100123" / "control"
    if component == "_coco":
        target = tmp_path / component
    elif component == "chat-100123":
        (tmp_path / "_coco").mkdir()
        target = tmp_path / "_coco" / component
    else:
        (tmp_path / "_coco" / "chat-100123").mkdir(parents=True)
    target.write_text("not a directory", encoding="utf-8")

    with pytest.raises(RuntimeError, match="not a directory"):
        bot._default_coco_control_workspace(
            user_id=10,
            thread_id=1,
            chat_id=-100123,
        )


def test_control_workspace_creation_is_idempotent_and_trusted(tmp_path, monkeypatch):
    trusted: list[Path] = []
    monkeypatch.setattr(bot.config, "config_dir", tmp_path)
    monkeypatch.setattr(
        bot,
        "_ensure_codex_project_trust",
        lambda path: (trusted.append(path) is None, ""),
    )

    first = bot._ensure_local_coco_control_workspace(
        bot._default_coco_control_workspace(
            user_id=10,
            thread_id=1,
            chat_id=-100123,
        )
    )
    second = bot._ensure_local_coco_control_workspace(
        bot._default_coco_control_workspace(
            user_id=10,
            thread_id=1,
            chat_id=-100123,
        )
    )

    expected = tmp_path / "_coco" / "chat-100123" / "control"
    assert first == second == expected
    assert expected.is_dir()
    assert trusted == [expected, expected]


def test_control_workspace_trust_uses_custom_codex_home(tmp_path, monkeypatch):
    config_dir = tmp_path / "coco-config"
    codex_home = tmp_path / "codex-home"
    monkeypatch.setattr(bot.config, "config_dir", config_dir)
    monkeypatch.setattr(bot.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    workspace = bot._ensure_local_coco_control_workspace(
        bot._default_coco_control_workspace(
            user_id=10,
            thread_id=1,
            chat_id=-100123,
        )
    )

    config_text = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert f'[projects."{workspace}"]' in config_text
    assert not (tmp_path / "home" / ".codex" / "config.toml").exists()


def test_control_workspace_fails_closed_when_write_probe_fails_and_cleans_up(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(bot.config, "config_dir", tmp_path)
    trusted: list[Path] = []
    monkeypatch.setattr(
        bot,
        "_ensure_codex_project_trust",
        lambda path: (trusted.append(path) is None, ""),
    )
    probe_paths: list[Path] = []
    real_mkstemp = tempfile.mkstemp

    def _mkstemp(*args, **kwargs):
        file_descriptor, raw_path = real_mkstemp(*args, **kwargs)
        probe_paths.append(Path(raw_path))
        return file_descriptor, raw_path

    monkeypatch.setattr(
        bot,
        "tempfile",
        SimpleNamespace(mkstemp=_mkstemp),
        raising=False,
    )

    def _fdopen_fails(*_args, **_kwargs):
        raise OSError("simulated write failure")

    monkeypatch.setattr(bot.os, "fdopen", _fdopen_fails)

    workspace = bot._default_coco_control_workspace(
        user_id=10,
        thread_id=1,
        chat_id=-100123,
    )
    with pytest.raises(RuntimeError, match="not writable"):
        bot._ensure_local_coco_control_workspace(workspace)

    assert trusted == []
    assert len(probe_paths) == 1
    assert not probe_paths[0].exists()
