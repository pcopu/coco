"""Regression tests for durable, concurrent Codex trust updates."""

from __future__ import annotations

import multiprocessing
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from threading import BrokenBarrierError

import pytest

import coco.codex_trust as codex_trust


def test_windows_without_lock_backend_loads_fail_closed() -> None:
    script = """
import importlib
import os
import sys

os.name = "nt"
sys.modules["fcntl"] = None
sys.modules["msvcrt"] = None
module = importlib.import_module("coco.codex_trust")
assert module._LOCK_BACKEND == "unavailable"
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_unavailable_lock_backend_does_not_write_unlocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    original = 'model = "gpt-5.3-codex"\n'
    config_path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(codex_trust, "_LOCK_BACKEND", "unavailable")

    ok, error = codex_trust.ensure_codex_project_trust(
        Path("/projects/unlocked"), config_path=config_path
    )

    assert ok is False
    assert "lock backend" in error
    assert config_path.read_text(encoding="utf-8") == original


def test_relative_symlink_config_updates_target_and_preserves_link(
    tmp_path: Path,
) -> None:
    target_config = tmp_path / "real" / "config.toml"
    target_config.parent.mkdir()
    target_config.write_text('model = "gpt-5.3-codex"\n', encoding="utf-8")
    config_path = tmp_path / "codex" / "config.toml"
    config_path.parent.mkdir()
    relative_target = os.path.relpath(target_config, config_path.parent)
    config_path.symlink_to(relative_target)

    ok, error = codex_trust.ensure_codex_project_trust(
        Path("/projects/relative"), config_path=config_path
    )

    assert (ok, error) == (True, "")
    assert config_path.is_symlink()
    assert os.readlink(config_path) == relative_target
    assert '[projects."/projects/relative"]' in target_config.read_text(
        encoding="utf-8"
    )


def test_absolute_symlink_config_updates_target_and_preserves_link(
    tmp_path: Path,
) -> None:
    target_config = tmp_path / "outside" / "config.toml"
    target_config.parent.mkdir()
    target_config.write_text('model = "gpt-5.3-codex"\n', encoding="utf-8")
    config_path = tmp_path / "codex" / "config.toml"
    config_path.parent.mkdir()
    config_path.symlink_to(target_config)

    ok, error = codex_trust.ensure_codex_project_trust(
        Path("/projects/absolute"), config_path=config_path
    )

    assert (ok, error) == (True, "")
    assert config_path.is_symlink()
    assert os.readlink(config_path) == str(target_config)
    assert '[projects."/projects/absolute"]' in target_config.read_text(
        encoding="utf-8"
    )


def test_dangling_symlink_config_fails_without_replacing_link(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "codex" / "config.toml"
    config_path.parent.mkdir()
    missing_target = "missing-config.toml"
    config_path.symlink_to(missing_target)

    ok, error = codex_trust.ensure_codex_project_trust(
        Path("/projects/dangling"), config_path=config_path
    )

    assert ok is False
    assert error
    assert config_path.is_symlink()
    assert os.readlink(config_path) == missing_target
    assert not (config_path.parent / missing_target).exists()


def test_symlink_loop_fails_without_replacing_link(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    other_path = tmp_path / "other.toml"
    config_path.symlink_to(other_path.name)
    other_path.symlink_to(config_path.name)

    ok, error = codex_trust.ensure_codex_project_trust(
        Path("/projects/loop"), config_path=config_path
    )

    assert ok is False
    assert error
    assert config_path.is_symlink()
    assert other_path.is_symlink()
    assert os.readlink(config_path) == other_path.name
    assert os.readlink(other_path) == config_path.name


def test_default_config_uses_codex_home_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom_home = tmp_path / "custom-codex-home"
    monkeypatch.setenv("CODEX_HOME", str(custom_home))

    ok, error = codex_trust.ensure_codex_project_trust(Path("/projects/env"))

    assert (ok, error) == (True, "")
    assert '[projects."/projects/env"]' in (
        custom_home / "config.toml"
    ).read_text(encoding="utf-8")
    assert not (tmp_path / ".codex" / "config.toml").exists()


def test_default_config_falls_back_to_home_codex_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(codex_trust.Path, "home", lambda: tmp_path)

    ok, error = codex_trust.ensure_codex_project_trust(Path("/projects/default"))

    assert (ok, error) == (True, "")
    assert '[projects."/projects/default"]' in (
        tmp_path / ".codex" / "config.toml"
    ).read_text(encoding="utf-8")


def test_explicit_config_path_takes_precedence_over_codex_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom_home = tmp_path / "custom-codex-home"
    explicit_config = tmp_path / "explicit" / "config.toml"
    monkeypatch.setenv("CODEX_HOME", str(custom_home))

    ok, error = codex_trust.ensure_codex_project_trust(
        Path("/projects/explicit"), config_path=explicit_config
    )

    assert (ok, error) == (True, "")
    assert '[projects."/projects/explicit"]' in explicit_config.read_text(
        encoding="utf-8"
    )
    assert not (custom_home / "config.toml").exists()


def _trust_worker(
    config_path: str,
    project_path: str,
    read_barrier: object,
    result_queue: object,
) -> None:
    """Force both legacy writers to read before either one writes."""
    config = Path(config_path)
    original_read_text = codex_trust.Path.read_text

    def _read_text(self: Path, *args: object, **kwargs: object) -> str:
        content = original_read_text(self, *args, **kwargs)
        if self == config:
            try:
                read_barrier.wait(timeout=2.0)  # type: ignore[attr-defined]
            except (BrokenBarrierError, TimeoutError):
                # With the fixed helper, the second process cannot read until
                # the first has committed.  A broken barrier is expected then.
                pass
            if project_path.endswith("-a"):
                time.sleep(0.01)
            else:
                time.sleep(0.20)
        return content

    codex_trust.Path.read_text = _read_text  # type: ignore[method-assign]
    ok, error = codex_trust.ensure_codex_project_trust(
        Path(project_path), config_path=config
    )
    result_queue.put((ok, error))  # type: ignore[attr-defined]


def test_concurrent_process_updates_preserve_both_projects(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('model = "gpt-5.3-codex"\n', encoding="utf-8")
    context = multiprocessing.get_context("spawn")
    read_barrier = context.Barrier(2)
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_trust_worker,
            args=(str(config_path), "/projects-a", read_barrier, result_queue),
        ),
        context.Process(
            target=_trust_worker,
            args=(str(config_path), "/projects-b", read_barrier, result_queue),
        ),
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10.0)

    assert all(process.exitcode == 0 for process in processes)
    assert [result_queue.get(timeout=1.0) for _ in processes] == [
        (True, ""),
        (True, ""),
    ]
    updated = config_path.read_text(encoding="utf-8")
    assert '[projects."/projects-a"]' in updated
    assert '[projects."/projects-b"]' in updated


def _alias_trust_worker(
    config_path: str,
    project_path: str,
    read_barrier: object,
    result_queue: object,
) -> None:
    """Force aliased writers to read before either one writes."""
    config = Path(config_path).resolve()
    original_read_text = codex_trust.Path.read_text

    def _read_text(self: Path, *args: object, **kwargs: object) -> str:
        content = original_read_text(self, *args, **kwargs)
        if self == config:
            try:
                read_barrier.wait(timeout=2.0)  # type: ignore[attr-defined]
            except (BrokenBarrierError, TimeoutError):
                # With the fixed helper, the second process cannot read until
                # the first has committed.  A broken barrier is expected then.
                pass
            if project_path.endswith("-a"):
                time.sleep(0.01)
            else:
                time.sleep(0.20)
        return content

    codex_trust.Path.read_text = _read_text  # type: ignore[method-assign]
    ok, error = codex_trust.ensure_codex_project_trust(
        Path(project_path), config_path=Path(config_path)
    )
    result_queue.put((ok, error))  # type: ignore[attr-defined]


def test_concurrent_alias_updates_preserve_both_projects(tmp_path: Path) -> None:
    target_config = tmp_path / "outside" / "config.toml"
    target_config.parent.mkdir()
    target_config.write_text('model = "gpt-5.3-codex"\n', encoding="utf-8")

    real_home = tmp_path / "codex-real"
    real_home.mkdir()
    real_config = real_home / "config.toml"
    real_config.symlink_to(target_config)
    aliased_home = tmp_path / "codex-alias"
    aliased_home.symlink_to(real_home, target_is_directory=True)
    aliased_config = aliased_home / "config.toml"

    context = multiprocessing.get_context("spawn")
    read_barrier = context.Barrier(2)
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_alias_trust_worker,
            args=(str(aliased_config), "/projects-a", read_barrier, result_queue),
        ),
        context.Process(
            target=_alias_trust_worker,
            args=(str(target_config), "/projects-b", read_barrier, result_queue),
        ),
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10.0)

    assert all(process.exitcode == 0 for process in processes)
    assert [result_queue.get(timeout=1.0) for _ in processes] == [
        (True, ""),
        (True, ""),
    ]
    updated = target_config.read_text(encoding="utf-8")
    assert '[projects."/projects-a"]' in updated
    assert '[projects."/projects-b"]' in updated


def test_replace_failure_preserves_original_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    original = 'model = "gpt-5.3-codex"\n'
    config_path.write_text(original, encoding="utf-8")

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(codex_trust.os, "replace", fail_replace)

    ok, error = codex_trust.ensure_codex_project_trust(
        Path("/projects"), config_path=config_path
    )

    assert ok is False
    assert "replace failed" in error
    assert config_path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob(f".{config_path.name}.*.tmp")) == []


def test_write_failure_preserves_original_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    original = 'model = "gpt-5.3-codex"\n'
    config_path.write_text(original, encoding="utf-8")

    def fail_fsync(_file_descriptor: int) -> None:
        raise OSError("fsync failed")

    monkeypatch.setattr(codex_trust.os, "fsync", fail_fsync)

    ok, error = codex_trust.ensure_codex_project_trust(
        Path("/projects"), config_path=config_path
    )

    assert ok is False
    assert "fsync failed" in error
    assert config_path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob(f".{config_path.name}.*.tmp")) == []


def test_repeated_trust_update_is_idempotent(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('model = "gpt-5.3-codex"\n', encoding="utf-8")

    first = codex_trust.ensure_codex_project_trust(
        Path("/projects"), config_path=config_path
    )
    first_content = config_path.read_text(encoding="utf-8")
    second = codex_trust.ensure_codex_project_trust(
        Path("/projects"), config_path=config_path
    )
    second_content = config_path.read_text(encoding="utf-8")

    assert first == (True, "")
    assert second == (True, "")
    assert second_content == first_content
    assert second_content.count('[projects."/projects"]') == 1


def test_update_preserves_existing_config_permissions(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('model = "gpt-5.3-codex"\n', encoding="utf-8")
    config_path.chmod(0o640)

    ok, error = codex_trust.ensure_codex_project_trust(
        Path("/projects"), config_path=config_path
    )

    assert (ok, error) == (True, "")
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o640


def test_update_uses_chmod_when_fchmod_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('model = "gpt-5.3-codex"\n', encoding="utf-8")
    config_path.chmod(0o640)

    def unsupported_fchmod(_file_descriptor: int, _mode: int) -> None:
        raise NotImplementedError

    monkeypatch.setattr(codex_trust.os, "fchmod", unsupported_fchmod)

    ok, error = codex_trust.ensure_codex_project_trust(
        Path("/projects/no-fchmod"), config_path=config_path
    )

    assert (ok, error) == (True, "")
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o640
