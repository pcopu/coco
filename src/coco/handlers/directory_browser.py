"""Directory browser UI for session creation.

Provides UIs in Telegram for:
  - Directory browser: navigate directory hierarchies to create new sessions

Key components:
  - DIRS_PER_PAGE: Number of directories shown per page
  - User state keys for tracking browse/picker session
  - build_directory_browser: Build directory browser UI
  - clear_browse_state: Clear browsing state from user_data
"""

from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .callback_data import (
    CB_DIR_CANCEL,
    CB_DIR_CONFIRM,
    CB_DIR_NEW_FOLDER,
    CB_DIR_PAGE,
    CB_DIR_SELECT,
    CB_DIR_UP,
)

# Directories per page in directory browser
DIRS_PER_PAGE = 6

# User state keys
STATE_KEY = "state"
STATE_BROWSING_DIRECTORY = "browsing_directory"
STATE_CREATING_DIRECTORY = "creating_directory"
BROWSE_PATH_KEY = "browse_path"
BROWSE_ROOT_KEY = "browse_root"
BROWSE_PAGE_KEY = "browse_page"
BROWSE_DIRS_KEY = "browse_dirs"  # Cache of subdirs for current path


def clear_browse_state(user_data: dict | None) -> None:
    """Clear directory browsing state keys from user_data."""
    if user_data is not None:
        user_data.pop(STATE_KEY, None)
        user_data.pop(BROWSE_PATH_KEY, None)
        user_data.pop(BROWSE_ROOT_KEY, None)
        user_data.pop(BROWSE_PAGE_KEY, None)
        user_data.pop(BROWSE_DIRS_KEY, None)


def build_directory_browser(
    current_path: str,
    page: int = 0,
    *,
    root_path: str | None = None,
    subdirs_override: list[str] | None = None,
    allow_new_folder: bool = True,
) -> tuple[str, InlineKeyboardMarkup, list[str]]:
    """Build directory browser UI.

    Returns: (text, keyboard, subdirs) where subdirs is the full list for caching.
    """
    root = resolve_browse_root(root_path)
    path = clamp_browse_path(current_path, root)

    if subdirs_override is not None:
        subdirs = sorted(
            name.strip()
            for name in subdirs_override
            if isinstance(name, str) and name.strip()
        )
    else:
        try:
            subdirs = sorted(
                [
                    d.name
                    for d in path.iterdir()
                    if d.is_dir() and not d.name.startswith(".")
                ]
            )
        except (PermissionError, OSError):
            subdirs = []

    total_pages = max(1, (len(subdirs) + DIRS_PER_PAGE - 1) // DIRS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * DIRS_PER_PAGE
    page_dirs = subdirs[start : start + DIRS_PER_PAGE]

    buttons: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(page_dirs), 2):
        row = []
        for j, name in enumerate(page_dirs[i : i + 2]):
            display = name[:12] + "…" if len(name) > 13 else name
            # Use global index (start + i + j) to avoid long dir names in callback_data
            idx = start + i + j
            row.append(
                InlineKeyboardButton(
                    f"📁 {display}", callback_data=f"{CB_DIR_SELECT}{idx}"
                )
            )
        buttons.append(row)

    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(
                InlineKeyboardButton("◀", callback_data=f"{CB_DIR_PAGE}{page - 1}")
            )
        nav.append(
            InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop")
        )
        if page < total_pages - 1:
            nav.append(
                InlineKeyboardButton("▶", callback_data=f"{CB_DIR_PAGE}{page + 1}")
            )
        buttons.append(nav)

    action_row: list[InlineKeyboardButton] = []
    # Allow going up while inside the configured browse root.
    if path != root:
        action_row.append(InlineKeyboardButton("..", callback_data=CB_DIR_UP))
    if allow_new_folder:
        action_row.append(
            InlineKeyboardButton("➕ Folder", callback_data=CB_DIR_NEW_FOLDER)
        )
    action_row.append(InlineKeyboardButton("Select", callback_data=CB_DIR_CONFIRM))
    action_row.append(InlineKeyboardButton("Cancel", callback_data=CB_DIR_CANCEL))
    buttons.append(action_row)

    display_path = str(path).replace(str(Path.home()), "~")
    display_root = str(root).replace(str(Path.home()), "~")
    if not subdirs:
        text = (
            "*Select Working Directory*"
            f"\n\nRoot: `{display_root}`"
            f"\nCurrent: `{display_path}`"
            "\n\n_(No subdirectories)_"
        )
    else:
        text = (
            "*Select Working Directory*"
            f"\n\nRoot: `{display_root}`"
            f"\nCurrent: `{display_path}`"
            "\n\nTap a folder to enter, or select current directory."
        )

    return text, InlineKeyboardMarkup(buttons), subdirs


def resolve_browse_root(root_path: str | Path | None) -> Path:
    """Resolve and validate browser root; fall back to cwd when invalid."""
    candidate = Path(root_path).expanduser() if root_path else Path.cwd()
    try:
        resolved = candidate.resolve()
    except OSError:
        resolved = Path.cwd().resolve()
    if not resolved.exists() or not resolved.is_dir():
        return Path.cwd().resolve()
    return resolved


def is_within_browse_root(path: str | Path, root_path: str | Path) -> bool:
    """Return True when path is equal to or inside root_path."""
    try:
        resolved_path = Path(path).expanduser().resolve()
        resolved_root = Path(root_path).expanduser().resolve()
        resolved_path.relative_to(resolved_root)
        return True
    except (ValueError, OSError):
        return False


def clamp_browse_path(path: str | Path, root_path: str | Path) -> Path:
    """Clamp path to the browse root and ensure it is an existing directory."""
    root = resolve_browse_root(root_path)
    try:
        resolved_path = Path(path).expanduser().resolve()
    except OSError:
        return root
    if not resolved_path.exists() or not resolved_path.is_dir():
        return root
    if not is_within_browse_root(resolved_path, root):
        return root
    return resolved_path
