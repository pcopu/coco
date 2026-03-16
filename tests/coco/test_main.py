"""Tests for coco.main bootstrap behavior."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import coco.main as main_mod


def _install_runtime_modules(
    monkeypatch,
    *,
    node_role: str,
    session_provider: str = "codex",
    runtime_mode: str = "app_server_only",
    app: object | None = None,
) -> None:
    config_module = ModuleType("coco.config")
    config_module.config = SimpleNamespace(
        node_role=node_role,
        allowed_users={1},
        session_provider=session_provider,
        sessions_path=Path("/tmp/sessions"),
        assistant_command="codex",
        runtime_mode=runtime_mode,
    )
    controller_calls: list[str] = []
    agent_calls: list[str] = []

    controller_runtime_module = ModuleType("coco.controller_runtime")
    controller_runtime_module.run_controller = lambda: controller_calls.append("run_controller")
    agent_runtime_module = ModuleType("coco.agent_runtime")
    agent_runtime_module.run_agent = lambda: agent_calls.append("run_agent")

    monkeypatch.setitem(sys.modules, "coco.config", config_module)
    monkeypatch.setitem(sys.modules, "coco.controller_runtime", controller_runtime_module)
    monkeypatch.setitem(sys.modules, "coco.agent_runtime", agent_runtime_module)
    monkeypatch.setattr(main_mod, "_TEST_RUNTIME_CALLS", (controller_calls, agent_calls), raising=False)


def test_main_skips_legacy_bootstrap_in_app_server_only(monkeypatch):
    _install_runtime_modules(
        monkeypatch,
        node_role="controller",
    )
    monkeypatch.setattr(main_mod.sys, "argv", ["coco"])

    main_mod.main()

    controller_calls, agent_calls = main_mod._TEST_RUNTIME_CALLS
    assert controller_calls == ["run_controller"]
    assert agent_calls == []


def test_main_runs_without_legacy_bootstrap(monkeypatch):
    _install_runtime_modules(
        monkeypatch,
        node_role="controller",
    )
    monkeypatch.setattr(main_mod.sys, "argv", ["coco"])

    main_mod.main()

    controller_calls, agent_calls = main_mod._TEST_RUNTIME_CALLS
    assert controller_calls == ["run_controller"]
    assert agent_calls == []


def test_main_runs_agent_runtime_in_agent_mode(monkeypatch):
    _install_runtime_modules(
        monkeypatch,
        node_role="agent",
    )
    monkeypatch.setattr(main_mod.sys, "argv", ["coco"])

    main_mod.main()

    controller_calls, agent_calls = main_mod._TEST_RUNTIME_CALLS
    assert controller_calls == []
    assert agent_calls == ["run_agent"]


def test_main_enables_debug_for_coco_loggers(monkeypatch):
    _install_runtime_modules(
        monkeypatch,
        node_role="controller",
    )
    monkeypatch.setattr(main_mod.sys, "argv", ["coco"])
    coco_logger = logging.getLogger("coco")
    previous_coco = coco_logger.level

    try:
        main_mod.main()
        assert coco_logger.level == logging.DEBUG
    finally:
        coco_logger.setLevel(previous_coco)


def test_main_uses_coco_dir_in_bootstrap_error_path(monkeypatch, capsys, tmp_path):
    config_module = ModuleType("coco.config")

    def _raise_config(name: str):
        if name == "config":
            raise ValueError("missing token")
        raise AttributeError(name)

    config_module.__getattr__ = _raise_config  # type: ignore[attr-defined]

    utils_module = ModuleType("coco.utils")
    utils_module.coco_dir = lambda: tmp_path

    monkeypatch.setitem(sys.modules, "coco.config", config_module)
    monkeypatch.setitem(sys.modules, "coco.utils", utils_module)

    with pytest.raises(SystemExit):
        main_mod.main()

    out = capsys.readouterr().out
    assert str(tmp_path / ".env") in out
