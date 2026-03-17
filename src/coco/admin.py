"""Local root-only auth management CLI for CoCo."""

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from .bootstrap import build_admin_meta_payload, resolve_group_ids
from .utils import env_alias

SCOPE_SINGLE_SESSION = "single_session"
SCOPE_CREATE_SESSIONS = "create_sessions"
_VALID_SCOPES = {SCOPE_SINGLE_SESSION, SCOPE_CREATE_SESSIONS}
_DEFAULT_AUTH_ENV_NAME = "auth.env"
_DEFAULT_AUTH_META_NAME = "allowed_users_meta.json"
_DEFAULT_SERVICE_ENV_NAME = "codex.env"
_DEFAULT_GROUP_REQUESTS_NAME = "group_allow_requests.json"
_FILE_MODE = 0o640
_DIR_MODE = 0o750
_GROUP_REQUEST_TTL_SECONDS = 600.0
_TOKEN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _select_existing_path(*candidates: Path) -> Path:
    for path in candidates:
        try:
            if path.exists():
                return path
        except OSError:
            continue
    return candidates[0]


def _default_auth_dir() -> Path:
    return _select_existing_path(
        Path("/etc/coco/auth"),
        Path("/etc/codex/auth"),
    )


def _default_service_env_file() -> Path:
    return _select_existing_path(
        Path("/etc/coco") / "coco.env",
        Path("/etc/codex") / _DEFAULT_SERVICE_ENV_NAME,
    )


def _default_group_requests_file() -> Path:
    return _select_existing_path(
        Path("/var/lib/coco") / _DEFAULT_GROUP_REQUESTS_NAME,
        Path("/var/lib/codex") / _DEFAULT_GROUP_REQUESTS_NAME,
    )


@dataclass(frozen=True)
class AuthPaths:
    auth_dir: Path
    auth_env_file: Path
    auth_meta_file: Path
    service_env_file: Path
    group_requests_file: Path


def _parse_user_tokens(tokens: list[str]) -> set[int]:
    users: set[int] = set()
    for token in tokens:
        for item in token.split(","):
            raw = item.strip()
            if not raw:
                continue
            users.add(int(raw))
    return users


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
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


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


def _write_json(path: Path, payload: dict[str, object]) -> None:
    _atomic_write_text(path, f"{json.dumps(payload, indent=2)}\n")


def _run_chattr(path: Path, flag: str) -> None:
    if shutil.which("chattr") is None:
        raise RuntimeError("`chattr` is required when immutable mode is enabled.")
    result = subprocess.run(
        ["chattr", flag, str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if detail:
            raise RuntimeError(f"chattr {flag} {path} failed: {detail}")
        raise RuntimeError(f"chattr {flag} {path} failed with exit code {result.returncode}")


def _write_protected_file(
    path: Path,
    writer: Callable[[Path], None],
    *,
    use_immutable: bool,
) -> None:
    unlocked = False
    if use_immutable and path.exists():
        _run_chattr(path, "-i")
        unlocked = True

    write_error: Exception | None = None
    try:
        writer(path)
        os.chmod(path, _FILE_MODE)
    except Exception as exc:  # noqa: BLE001
        write_error = exc

    lock_error: Exception | None = None
    if use_immutable and (unlocked or path.exists()):
        try:
            _run_chattr(path, "+i")
        except Exception as exc:  # noqa: BLE001
            lock_error = exc

    if write_error is not None:
        raise write_error
    if lock_error is not None:
        raise lock_error


def _ensure_secure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, _DIR_MODE)


def _safe_is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _load_allowed_users(path: Path) -> set[int]:
    if not _safe_is_file(path):
        return set()
    try:
        values = dotenv_values(path)
    except OSError:
        return set()
    raw = (values.get("ALLOWED_USERS") or "").strip()
    if not raw:
        return set()
    try:
        return _parse_user_tokens([raw])
    except ValueError as exc:
        raise ValueError(
            f"ALLOWED_USERS in {path} contains non-numeric values."
        ) from exc


def _parse_group_tokens(tokens: list[str]) -> set[int]:
    groups: set[int] = set()
    for token in tokens:
        for item in token.split(","):
            raw = item.strip()
            if not raw:
                continue
            groups.add(int(raw))
    return groups


def _load_allowed_groups(path: Path) -> set[int]:
    if not _safe_is_file(path):
        return set()
    try:
        values = dotenv_values(path)
    except OSError:
        return set()
    raw = (values.get("ALLOWED_GROUP_IDS") or "").strip()
    if not raw:
        return set()
    try:
        return _parse_group_tokens([raw])
    except ValueError as exc:
        raise ValueError(
            f"ALLOWED_GROUP_IDS in {path} contains non-numeric values."
        ) from exc


def _save_allowed_groups(
    path: Path,
    *,
    groups: set[int],
    use_immutable: bool,
) -> None:
    groups_csv = ",".join(str(gid) for gid in sorted(groups))
    _write_protected_file(
        path,
        lambda p: _upsert_env_key(p, "ALLOWED_GROUP_IDS", groups_csv),
        use_immutable=use_immutable,
    )


def _normalize_token(raw: str) -> str:
    return "".join(ch for ch in raw.strip().upper() if ch.isalnum())


def _new_token(length: int = 10) -> str:
    return "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(length))


def _load_group_requests(path: Path) -> dict[str, dict[str, object]]:
    if not _safe_is_file(path):
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Failed reading group requests file {path}: {exc}") from exc
    if not isinstance(payload, dict):
        return {}

    requests: dict[str, dict[str, object]] = {}
    for raw_token, raw_entry in payload.items():
        if not isinstance(raw_token, str) or not isinstance(raw_entry, dict):
            continue
        token = _normalize_token(raw_token)
        if not token:
            continue
        chat_id_raw = raw_entry.get("chat_id")
        requested_by_raw = raw_entry.get("requested_by")
        expires_at_raw = raw_entry.get("expires_at")
        try:
            chat_id = int(chat_id_raw)
            requested_by = int(requested_by_raw)
            expires_at = float(expires_at_raw)
        except (TypeError, ValueError):
            continue
        title = raw_entry.get("chat_title")
        created_at_raw = raw_entry.get("created_at")
        try:
            created_at = float(created_at_raw)
        except (TypeError, ValueError):
            created_at = 0.0
        requests[token] = {
            "chat_id": chat_id,
            "requested_by": requested_by,
            "chat_title": title.strip() if isinstance(title, str) else "",
            "created_at": created_at,
            "expires_at": expires_at,
        }
    return requests


def _save_group_requests(path: Path, requests: dict[str, dict[str, object]]) -> None:
    payload: dict[str, dict[str, object]] = {}
    for token, entry in sorted(requests.items()):
        payload[token] = {
            "chat_id": int(entry["chat_id"]),
            "requested_by": int(entry["requested_by"]),
            "chat_title": str(entry.get("chat_title", "")).strip(),
            "created_at": float(entry.get("created_at", 0.0)),
            "expires_at": float(entry["expires_at"]),
        }
    _atomic_write_text(path, f"{json.dumps(payload, indent=2)}\n")


def _queue_group_request(
    path: Path,
    *,
    chat_id: int,
    requested_by: int,
    chat_title: str,
) -> dict[str, object]:
    now = time.time()
    requests = _load_group_requests(path)
    requests = {
        token: entry
        for token, entry in requests.items()
        if float(entry.get("expires_at", 0.0)) > now
    }
    for token, entry in requests.items():
        if int(entry.get("chat_id", 0)) == chat_id:
            return {"token": token, **entry}
    token = _new_token()
    while token in requests:
        token = _new_token()
    entry: dict[str, object] = {
        "chat_id": chat_id,
        "requested_by": requested_by,
        "chat_title": chat_title.strip(),
        "created_at": now,
        "expires_at": now + _GROUP_REQUEST_TTL_SECONDS,
    }
    requests[token] = entry
    _save_group_requests(path, requests)
    return {"token": token, **entry}

def _load_meta(path: Path) -> tuple[dict[int, str], set[int], dict[int, str]]:
    if not path.is_file():
        return {}, set(), {}

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Failed reading metadata file {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Metadata file {path} must contain a JSON object.")

    raw_names = payload.get("names", {})
    names: dict[int, str] = {}
    if isinstance(raw_names, dict):
        for raw_uid, raw_name in raw_names.items():
            try:
                uid = int(raw_uid)
            except (TypeError, ValueError):
                continue
            if isinstance(raw_name, str) and raw_name.strip():
                names[uid] = raw_name.strip()

    raw_admins = payload.get("admins", [])
    admins: set[int] = set()
    if isinstance(raw_admins, list):
        for value in raw_admins:
            try:
                admins.add(int(value))
            except (TypeError, ValueError):
                continue

    raw_scopes = payload.get("scopes", {})
    scopes: dict[int, str] = {}
    if isinstance(raw_scopes, dict):
        for raw_uid, raw_scope in raw_scopes.items():
            try:
                uid = int(raw_uid)
            except (TypeError, ValueError):
                continue
            if isinstance(raw_scope, str) and raw_scope in _VALID_SCOPES:
                scopes[uid] = raw_scope
    return names, admins, scopes


def _normalize_meta(
    allowed_users: set[int],
    names: dict[int, str],
    admins: set[int],
    scopes: dict[int, str],
) -> tuple[dict[int, str], set[int], dict[int, str]]:
    clean_names = {
        uid: name.strip()
        for uid, name in names.items()
        if uid in allowed_users and isinstance(name, str) and name.strip()
    }
    clean_admins = {uid for uid in admins if uid in allowed_users}
    if allowed_users and not clean_admins:
        clean_admins.add(min(allowed_users))

    clean_scopes: dict[int, str] = {}
    for uid in allowed_users:
        scope = scopes.get(uid, "")
        if scope not in _VALID_SCOPES:
            scope = (
                SCOPE_CREATE_SESSIONS
                if uid in clean_admins
                else SCOPE_SINGLE_SESSION
            )
        clean_scopes[uid] = scope
    return clean_names, clean_admins, clean_scopes


def _serialize_meta(
    names: dict[int, str],
    admins: set[int],
    scopes: dict[int, str],
) -> dict[str, object]:
    return {
        "names": {str(uid): names[uid] for uid in sorted(names)},
        "admins": sorted(admins),
        "scopes": {str(uid): scopes[uid] for uid in sorted(scopes)},
    }


def _resolve_paths(args: argparse.Namespace) -> AuthPaths:
    auth_dir = Path(args.auth_dir).expanduser()
    auth_env_file = (
        Path(args.auth_env_file).expanduser()
        if args.auth_env_file
        else auth_dir / _DEFAULT_AUTH_ENV_NAME
    )
    auth_meta_file = (
        Path(args.auth_meta_file).expanduser()
        if args.auth_meta_file
        else auth_dir / _DEFAULT_AUTH_META_NAME
    )
    service_env_file = (
        Path(args.service_env_file).expanduser()
        if args.service_env_file
        else _default_service_env_file()
    )
    group_requests_file = (
        Path(args.group_requests_file).expanduser()
        if args.group_requests_file
        else _default_group_requests_file()
    )
    return AuthPaths(
        auth_dir=auth_dir,
        auth_env_file=auth_env_file,
        auth_meta_file=auth_meta_file,
        service_env_file=service_env_file,
        group_requests_file=group_requests_file,
    )


def _load_state(paths: AuthPaths) -> tuple[set[int], dict[int, str], set[int], dict[int, str]]:
    allowed_users = _load_allowed_users(paths.auth_env_file)
    names, admins, scopes = _load_meta(paths.auth_meta_file)
    names, admins, scopes = _normalize_meta(allowed_users, names, admins, scopes)
    return allowed_users, names, admins, scopes


def _save_state(
    paths: AuthPaths,
    *,
    allowed_users: set[int],
    names: dict[int, str],
    admins: set[int],
    scopes: dict[int, str],
    use_immutable: bool,
) -> None:
    if not allowed_users:
        raise ValueError("Refusing to persist an empty allowlist.")

    _ensure_secure_dir(paths.auth_dir)
    names, admins, scopes = _normalize_meta(allowed_users, names, admins, scopes)
    users_csv = ",".join(str(uid) for uid in sorted(allowed_users))
    payload = _serialize_meta(names, admins, scopes)

    _write_protected_file(
        paths.auth_env_file,
        lambda path: _upsert_env_key(path, "ALLOWED_USERS", users_csv),
        use_immutable=use_immutable,
    )
    _write_protected_file(
        paths.auth_meta_file,
        lambda path: _write_json(path, payload),
        use_immutable=use_immutable,
    )


def _cmd_show(paths: AuthPaths) -> int:
    allowed_users, names, admins, scopes = _load_state(paths)
    allowed_groups = _load_allowed_groups(paths.service_env_file)

    print(f"auth_dir: {paths.auth_dir}")
    print(f"auth_env_file: {paths.auth_env_file}")
    print(f"auth_meta_file: {paths.auth_meta_file}")
    print(f"service_env_file: {paths.service_env_file}")
    print(f"group_requests_file: {paths.group_requests_file}")
    print(f"allowed_group_ids: {','.join(str(gid) for gid in sorted(allowed_groups)) or '(none)'}")
    print(f"allowed_users: {','.join(str(uid) for uid in sorted(allowed_users)) or '(none)'}")
    print(f"admins: {','.join(str(uid) for uid in sorted(admins)) or '(none)'}")
    if not allowed_users:
        return 0

    print("entries:")
    for uid in sorted(allowed_users):
        label = names.get(uid, "")
        scope = scopes.get(uid, SCOPE_SINGLE_SESSION)
        role = "admin" if uid in admins else "member"
        if label:
            print(f"  - {uid}: {label} ({role}, {scope})")
        else:
            print(f"  - {uid}: {role}, {scope}")
    return 0


def _cmd_set_users(paths: AuthPaths, args: argparse.Namespace) -> int:
    allowed_users = _parse_user_tokens(args.users)
    names, admins, scopes = {}, set(), {}
    if paths.auth_meta_file.is_file():
        names, admins, scopes = _load_meta(paths.auth_meta_file)
    _save_state(
        paths,
        allowed_users=allowed_users,
        names=names,
        admins=admins,
        scopes=scopes,
        use_immutable=not args.no_immutable,
    )
    return _cmd_show(paths)


def _cmd_add_user(paths: AuthPaths, args: argparse.Namespace) -> int:
    allowed_users, names, admins, scopes = _load_state(paths)
    allowed_users.add(args.user_id)
    if args.name.strip():
        names[args.user_id] = args.name.strip()
    scopes[args.user_id] = args.scope
    if args.admin:
        admins.add(args.user_id)

    _save_state(
        paths,
        allowed_users=allowed_users,
        names=names,
        admins=admins,
        scopes=scopes,
        use_immutable=not args.no_immutable,
    )
    return _cmd_show(paths)


def _cmd_remove_user(paths: AuthPaths, args: argparse.Namespace) -> int:
    allowed_users, names, admins, scopes = _load_state(paths)
    if args.user_id not in allowed_users:
        raise ValueError(f"User {args.user_id} is not currently allowed.")

    allowed_users.remove(args.user_id)
    names.pop(args.user_id, None)
    admins.discard(args.user_id)
    scopes.pop(args.user_id, None)
    _save_state(
        paths,
        allowed_users=allowed_users,
        names=names,
        admins=admins,
        scopes=scopes,
        use_immutable=not args.no_immutable,
    )
    return _cmd_show(paths)


def _cmd_request_group(paths: AuthPaths, args: argparse.Namespace) -> int:
    entry = _queue_group_request(
        paths.group_requests_file,
        chat_id=args.chat_id,
        requested_by=args.requested_by,
        chat_title=args.chat_title or "",
    )
    print(f"token: {entry['token']}")
    print(f"chat_id: {entry['chat_id']}")
    print(f"requested_by: {entry['requested_by']}")
    print(
        "run: "
        f"sudo codex-admin approve-group {entry['token']}"
    )
    return 0


def _cmd_add_group(paths: AuthPaths, args: argparse.Namespace) -> int:
    groups = _load_allowed_groups(paths.service_env_file)
    groups.add(args.chat_id)
    _save_allowed_groups(
        paths.service_env_file,
        groups=groups,
        use_immutable=not args.no_immutable,
    )
    print("allowed_group_ids:", ",".join(str(gid) for gid in sorted(groups)))
    return 0


def _cmd_remove_group(paths: AuthPaths, args: argparse.Namespace) -> int:
    groups = _load_allowed_groups(paths.service_env_file)
    if args.chat_id not in groups:
        raise ValueError(f"Group {args.chat_id} is not currently allowed.")
    groups.remove(args.chat_id)
    _save_allowed_groups(
        paths.service_env_file,
        groups=groups,
        use_immutable=not args.no_immutable,
    )
    print("allowed_group_ids:", ",".join(str(gid) for gid in sorted(groups)))
    return 0


def _cmd_approve_group(paths: AuthPaths, args: argparse.Namespace) -> int:
    token = _normalize_token(args.token)
    if not token:
        raise ValueError("Token cannot be empty.")

    now = time.time()
    requests = _load_group_requests(paths.group_requests_file)
    request = requests.get(token)
    if request is None:
        raise ValueError("Invalid or expired group approval token.")
    expires_at = float(request.get("expires_at", 0.0))
    if expires_at <= now:
        requests.pop(token, None)
        _save_group_requests(paths.group_requests_file, requests)
        raise ValueError("Invalid or expired group approval token.")

    chat_id = int(request.get("chat_id", 0))
    if chat_id == 0:
        raise ValueError("Request does not contain a valid chat_id.")

    groups = _load_allowed_groups(paths.service_env_file)
    groups.add(chat_id)
    _save_allowed_groups(
        paths.service_env_file,
        groups=groups,
        use_immutable=not args.no_immutable,
    )

    requests.pop(token, None)
    _save_group_requests(paths.group_requests_file, requests)
    print("approved_group:", chat_id)
    print("allowed_group_ids:", ",".join(str(gid) for gid in sorted(groups)))
    return 0


def _cmd_bootstrap(paths: AuthPaths, args: argparse.Namespace) -> int:
    resolved_groups = resolve_group_ids(
        list(args.group_id),
        allow_all_groups=bool(args.allow_all_groups),
    )
    allowed_users = {args.admin_user}
    meta_payload = build_admin_meta_payload(
        admin_user=args.admin_user,
        admin_name=args.admin_name,
    )

    _ensure_secure_dir(paths.auth_dir)
    _write_protected_file(
        paths.auth_env_file,
        lambda path: _upsert_env_key(path, "ALLOWED_USERS", str(args.admin_user)),
        use_immutable=not args.no_immutable,
    )
    _write_protected_file(
        paths.auth_meta_file,
        lambda path: _write_json(path, meta_payload),
        use_immutable=not args.no_immutable,
    )

    group_csv = ",".join(str(gid) for gid in resolved_groups)
    _write_protected_file(
        paths.service_env_file,
        lambda path: _upsert_env_key(path, "TELEGRAM_BOT_TOKEN", args.bot_token.strip()),
        use_immutable=not args.no_immutable,
    )
    _write_protected_file(
        paths.service_env_file,
        lambda path: _upsert_env_key(path, "COCO_AUTH_ENV_FILE", str(paths.auth_env_file)),
        use_immutable=not args.no_immutable,
    )
    _write_protected_file(
        paths.service_env_file,
        lambda path: _upsert_env_key(path, "COCO_AUTH_META_FILE", str(paths.auth_meta_file)),
        use_immutable=not args.no_immutable,
    )
    _write_protected_file(
        paths.service_env_file,
        lambda path: _upsert_env_key(path, "ALLOWED_GROUP_IDS", group_csv),
        use_immutable=not args.no_immutable,
    )
    if args.browse_root.strip():
        _write_protected_file(
            paths.service_env_file,
            lambda path: _upsert_env_key(path, "BROWSE_ROOT", args.browse_root.strip()),
            use_immutable=not args.no_immutable,
        )

    print(f"service_env_file: {paths.service_env_file}")
    print(f"auth_env_file: {paths.auth_env_file}")
    print(f"auth_meta_file: {paths.auth_meta_file}")
    print(f"allowed_users: {','.join(str(uid) for uid in sorted(allowed_users))}")
    print(f"allowed_group_ids: {group_csv or '(open)'}")
    print("next: restart the CoCo service or run `coco` with this env loaded")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coco-admin",
        description="Local root-only auth manager for CoCo.",
    )
    parser.add_argument(
        "--auth-dir",
        default=env_alias("COCO_AUTH_DIR", default=str(_default_auth_dir())),
        help="Base auth directory (default: /etc/coco/auth).",
    )
    parser.add_argument(
        "--auth-env-file",
        default="",
        help="Explicit auth env file path (defaults to <auth-dir>/auth.env).",
    )
    parser.add_argument(
        "--auth-meta-file",
        default="",
        help="Explicit metadata JSON path (defaults to <auth-dir>/allowed_users_meta.json).",
    )
    parser.add_argument(
        "--no-immutable",
        action="store_true",
        help="Skip `chattr +/-i` around writes.",
    )
    parser.add_argument(
        "--service-env-file",
        default=env_alias("COCO_SERVICE_ENV_FILE"),
        help="Env file containing ALLOWED_GROUP_IDS (default: /etc/coco/coco.env).",
    )
    parser.add_argument(
        "--group-requests-file",
        default=env_alias("COCO_GROUP_REQUESTS_FILE"),
        help="Pending group request token file (default: /var/lib/coco/group_allow_requests.json).",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("show", help="Show current allowlist auth state.")

    set_users = subparsers.add_parser(
        "set-users",
        help="Set the full allowed-users list (comma or space separated).",
    )
    set_users.add_argument("users", nargs="+", help="User IDs.")

    add_user = subparsers.add_parser("add-user", help="Add one allowed user.")
    add_user.add_argument("user_id", type=int, help="Telegram user ID.")
    add_user.add_argument(
        "--name",
        default="",
        help="Optional display name.",
    )
    add_user.add_argument(
        "--scope",
        choices=sorted(_VALID_SCOPES),
        default=SCOPE_SINGLE_SESSION,
        help="Scope for this user.",
    )
    add_user.add_argument(
        "--admin",
        action="store_true",
        help="Grant admin role.",
    )

    remove_user = subparsers.add_parser("remove-user", help="Remove one allowed user.")
    remove_user.add_argument("user_id", type=int, help="Telegram user ID.")

    request_group = subparsers.add_parser(
        "request-group",
        help="Create a local group-allow request token (for testing/ops).",
    )
    request_group.add_argument("chat_id", type=int, help="Telegram chat/group ID.")
    request_group.add_argument(
        "--requested-by",
        type=int,
        default=0,
        help="Requester Telegram user ID (for audit trail).",
    )
    request_group.add_argument(
        "--chat-title",
        default="",
        help="Optional group title.",
    )

    add_group = subparsers.add_parser("add-group", help="Add one allowed group ID.")
    add_group.add_argument("chat_id", type=int, help="Telegram chat/group ID.")

    remove_group = subparsers.add_parser(
        "remove-group", help="Remove one allowed group ID."
    )
    remove_group.add_argument("chat_id", type=int, help="Telegram chat/group ID.")

    approve_group = subparsers.add_parser(
        "approve-group",
        help="Approve a pending group request token and add its chat_id.",
    )
    approve_group.add_argument("token", help="One-time approval token from Telegram DM.")

    bootstrap = subparsers.add_parser(
        "bootstrap",
        help="Write bot token, admin allowlist, and allowed groups in one shot.",
    )
    bootstrap.add_argument(
        "--bot-token",
        required=True,
        help="Telegram bot token from BotFather.",
    )
    bootstrap.add_argument(
        "--admin-user",
        required=True,
        type=int,
        help="Telegram user ID to grant admin access.",
    )
    bootstrap.add_argument(
        "--admin-name",
        default="",
        help="Optional display name for the bootstrap admin user.",
    )
    bootstrap.add_argument(
        "--group-id",
        action="append",
        type=int,
        default=[],
        help="Allowed Telegram supergroup ID. Repeat for multiple groups.",
    )
    bootstrap.add_argument(
        "--allow-all-groups",
        action="store_true",
        help="Leave group allowlisting open. Not recommended.",
    )
    bootstrap.add_argument(
        "--browse-root",
        default="",
        help="Optional browse root to persist in the service env file.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if os.geteuid() != 0:
        print("coco-admin must run as root (use sudo).", file=sys.stderr)
        return 1

    try:
        paths = _resolve_paths(args)
        if args.command == "show":
            return _cmd_show(paths)
        if args.command == "set-users":
            return _cmd_set_users(paths, args)
        if args.command == "add-user":
            return _cmd_add_user(paths, args)
        if args.command == "remove-user":
            return _cmd_remove_user(paths, args)
        if args.command == "request-group":
            return _cmd_request_group(paths, args)
        if args.command == "add-group":
            return _cmd_add_group(paths, args)
        if args.command == "remove-group":
            return _cmd_remove_group(paths, args)
        if args.command == "approve-group":
            return _cmd_approve_group(paths, args)
        if args.command == "bootstrap":
            return _cmd_bootstrap(paths, args)
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
