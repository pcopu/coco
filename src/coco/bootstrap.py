"""Quick-start bootstrap helpers for CoCo setup."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from .utils import atomic_write_json, coco_dir

SCOPE_CREATE_SESSIONS = "create_sessions"


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        suffix=".tmp",
        prefix=f".{path.name}.",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        Path(tmp_path).replace(path)
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except OSError:
            pass


def _upsert_env_key(path: Path, key: str, value: str) -> None:
    try:
        content = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError as exc:
        raise RuntimeError(f"Failed reading env file {path}: {exc}") from exc

    lines = content.splitlines()
    replacement = f"{key}={value}"
    updated = False
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        left, sep, _right = line.partition("=")
        if sep and left.strip() == key:
            lines[idx] = replacement
            updated = True
            break

    if not updated:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(replacement)

    new_content = "\n".join(lines)
    if lines:
        new_content += "\n"
    _atomic_write_text(path, new_content)


def build_admin_meta_payload(*, admin_user: int, admin_name: str = "") -> dict[str, object]:
    """Build default metadata payload for one bootstrap admin."""
    payload: dict[str, object] = {
        "names": {},
        "admins": [admin_user],
        "scopes": {str(admin_user): SCOPE_CREATE_SESSIONS},
    }
    if admin_name.strip():
        payload["names"] = {str(admin_user): admin_name.strip()}
    return payload


def resolve_group_ids(
    group_ids: list[int],
    *,
    allow_all_groups: bool,
) -> list[int]:
    """Normalize group ids and enforce the secure default."""
    resolved = sorted(set(group_ids))
    if resolved:
        return resolved
    if allow_all_groups:
        return []
    raise ValueError(
        "At least one --group-id is required unless --allow-all-groups is set."
    )


def write_local_bootstrap(
    *,
    config_dir: Path,
    bot_token: str,
    admin_user: int,
    admin_name: str,
    group_ids: list[int],
    allow_all_groups: bool,
    browse_root: str,
) -> tuple[Path, Path]:
    """Write local quick-start CoCo config files."""
    resolved_groups = resolve_group_ids(group_ids, allow_all_groups=allow_all_groups)
    env_path = config_dir / ".env"
    meta_path = config_dir / "allowed_users_meta.json"

    _upsert_env_key(env_path, "TELEGRAM_BOT_TOKEN", bot_token.strip())
    _upsert_env_key(env_path, "ALLOWED_USERS", str(admin_user))
    _upsert_env_key(
        env_path,
        "ALLOWED_GROUP_IDS",
        ",".join(str(group_id) for group_id in resolved_groups),
    )
    if browse_root.strip():
        _upsert_env_key(env_path, "BROWSE_ROOT", browse_root.strip())

    atomic_write_json(
        meta_path,
        build_admin_meta_payload(admin_user=admin_user, admin_name=admin_name),
    )
    return env_path, meta_path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coco init",
        description="Write a local quick-start CoCo config.",
    )
    parser.add_argument(
        "--config-dir",
        default=str(coco_dir()),
        help="Target config directory (default: ~/.coco).",
    )
    parser.add_argument("--bot-token", required=True, help="Telegram bot token from BotFather.")
    parser.add_argument("--admin-user", required=True, type=int, help="Telegram user ID to grant admin access.")
    parser.add_argument("--admin-name", default="", help="Optional display name for the admin user.")
    parser.add_argument(
        "--group-id",
        action="append",
        type=int,
        default=[],
        help="Allowed Telegram supergroup ID. Repeat for multiple groups.",
    )
    parser.add_argument(
        "--allow-all-groups",
        action="store_true",
        help="Leave group allowlisting open. Not recommended.",
    )
    parser.add_argument(
        "--browse-root",
        default="",
        help="Optional browse root to persist in the local env file.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        config_dir = Path(args.config_dir).expanduser()
        env_path, meta_path = write_local_bootstrap(
            config_dir=config_dir,
            bot_token=args.bot_token,
            admin_user=args.admin_user,
            admin_name=args.admin_name,
            group_ids=list(args.group_id),
            allow_all_groups=bool(args.allow_all_groups),
            browse_root=args.browse_root,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"env_file: {env_path}")
    print(f"meta_file: {meta_path}")
    print(f"admin_user: {args.admin_user}")
    if args.group_id:
        print("allowed_group_ids:", ",".join(str(value) for value in sorted(set(args.group_id))))
    else:
        print("allowed_group_ids: (open)")
    print("next: run `coco`")
    return 0
