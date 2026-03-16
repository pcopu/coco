"""Tests for the final CoCo-only package namespace."""

from __future__ import annotations

import importlib

import pytest


def test_coco_main_imports() -> None:
    module = importlib.import_module("coco.main")
    assert module.__name__ == "coco.main"


def test_legacy_namespace_is_removed() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("cc" "bot.main")
