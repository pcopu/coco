"""Tests for directory browser root clamping and actions."""

from pathlib import Path

from coco.handlers.callback_data import CB_DIR_NEW_FOLDER, CB_DIR_UP
from coco.handlers.directory_browser import (
    BROWSE_DIRS_KEY,
    BROWSE_PAGE_KEY,
    BROWSE_PATH_KEY,
    BROWSE_ROOT_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    clamp_browse_path,
    build_directory_browser,
    clear_browse_state,
    is_within_browse_root,
)


def _callback_data_set(keyboard) -> set[str]:
    callbacks: set[str] = set()
    for row in keyboard.inline_keyboard:
        for button in row:
            if button.callback_data:
                callbacks.add(button.callback_data)
    return callbacks


def test_clamp_browse_path_returns_root_when_candidate_is_outside(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    clamped = clamp_browse_path(outside, root)
    assert clamped == root


def test_is_within_browse_root_for_nested_path(tmp_path):
    root = tmp_path / "root"
    nested = root / "one" / "two"
    nested.mkdir(parents=True)

    assert is_within_browse_root(nested, root) is True
    assert is_within_browse_root(tmp_path / "other", root) is False


def test_browser_hides_up_button_at_root_and_shows_new_folder(tmp_path):
    root = tmp_path / "root"
    root.mkdir()

    _text, keyboard, _subdirs = build_directory_browser(str(root), root_path=str(root))
    callbacks = _callback_data_set(keyboard)

    assert CB_DIR_UP not in callbacks
    assert CB_DIR_NEW_FOLDER in callbacks


def test_browser_shows_up_button_below_root(tmp_path):
    root = tmp_path / "root"
    child = root / "child"
    child.mkdir(parents=True)

    _text, keyboard, _subdirs = build_directory_browser(str(child), root_path=str(root))
    callbacks = _callback_data_set(keyboard)

    assert CB_DIR_UP in callbacks
    assert CB_DIR_NEW_FOLDER in callbacks


def test_browser_text_shows_root_and_clamped_current_path(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    text, _keyboard, _subdirs = build_directory_browser(
        str(outside),
        root_path=str(root),
    )
    display_root = str(Path(root))

    assert f"Root: `{display_root}`" in text
    assert f"Current: `{display_root}`" in text


def test_clear_browse_state_removes_all_browse_keys():
    user_data = {
        STATE_KEY: STATE_BROWSING_DIRECTORY,
        BROWSE_PATH_KEY: "/tmp/path",
        BROWSE_ROOT_KEY: "/tmp",
        BROWSE_PAGE_KEY: 1,
        BROWSE_DIRS_KEY: ["one"],
    }

    clear_browse_state(user_data)

    assert STATE_KEY not in user_data
    assert BROWSE_PATH_KEY not in user_data
    assert BROWSE_ROOT_KEY not in user_data
    assert BROWSE_PAGE_KEY not in user_data
    assert BROWSE_DIRS_KEY not in user_data


def test_browser_can_use_supplied_subdirs_and_hide_new_folder(tmp_path):
    root = tmp_path / "root"
    root.mkdir()

    _text, keyboard, subdirs = build_directory_browser(
        str(root),
        root_path=str(root),
        subdirs_override=["beta", "alpha"],
        allow_new_folder=False,
    )
    callbacks = _callback_data_set(keyboard)

    assert subdirs == ["alpha", "beta"]
    assert CB_DIR_NEW_FOLDER not in callbacks
