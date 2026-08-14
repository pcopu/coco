"""Small shared helper for persisting Codex project trust."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import tempfile
from collections.abc import Iterator
from typing import BinaryIO

from .utils import codex_config_path

if os.name == "nt":
    try:
        import msvcrt as _msvcrt
    except ImportError:
        _msvcrt = None
    _fcntl = None
else:
    try:
        import fcntl as _fcntl
    except ImportError:
        _fcntl = None
    _msvcrt = None

if _fcntl is not None and all(
    hasattr(_fcntl, attribute) for attribute in ("flock", "LOCK_EX", "LOCK_UN")
):
    _LOCK_BACKEND = "fcntl"
elif _msvcrt is not None and all(
    hasattr(_msvcrt, attribute)
    for attribute in ("locking", "LK_LOCK", "LK_UNLCK")
):
    _LOCK_BACKEND = "msvcrt"
else:
    _LOCK_BACKEND = "unavailable"


def _acquire_config_lock(lock_file: BinaryIO) -> None:
    """Acquire the configured platform lock or fail closed."""
    if _LOCK_BACKEND == "fcntl":
        if _fcntl is None:
            raise OSError("fcntl lock backend is unavailable")
        _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_EX)  # type: ignore[attr-defined]
        return
    if _LOCK_BACKEND == "msvcrt":
        if _msvcrt is None:
            raise OSError("msvcrt lock backend is unavailable")
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        _msvcrt.locking(lock_file.fileno(), _msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
        return
    raise OSError("no supported inter-process lock backend is available")


def _release_config_lock(lock_file: BinaryIO) -> None:
    """Release a platform lock previously acquired by this process."""
    if _LOCK_BACKEND == "fcntl":
        if _fcntl is None:
            return
        _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_UN)  # type: ignore[attr-defined]
    elif _LOCK_BACKEND == "msvcrt":
        if _msvcrt is None:
            return
        lock_file.seek(0)
        _msvcrt.locking(lock_file.fileno(), _msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]


@contextmanager
def _config_lock(config_path: Path) -> Iterator[None]:
    """Hold a stable sibling lock while updating one Codex config.

    The config itself is replaced atomically, so locking its inode would not
    serialize the next writer.  A sibling lock file keeps the lock identity
    stable across replacements and also coordinates separate processes.
    """
    lock_path = config_path.with_name(f".{config_path.name}.lock")
    with lock_path.open("a+b") as lock_file:
        _acquire_config_lock(lock_file)
        try:
            yield
        finally:
            try:
                _release_config_lock(lock_file)
            except OSError:
                pass


def _fsync_parent_directory(directory: Path) -> None:
    """Best-effort fsync of the directory containing an atomically replaced file."""
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        directory_fd = os.open(str(directory), directory_flags)
    except OSError:
        return
    try:
        try:
            os.fsync(directory_fd)
        except OSError:
            # The replacement is already atomic; directory fsync is a
            # durability improvement unavailable on some filesystems.
            pass
    finally:
        try:
            os.close(directory_fd)
        except OSError:
            pass


def _resolve_config_target(config_path: Path) -> Path:
    """Resolve symlink-managed configs without replacing the symlink itself."""
    if not config_path.is_symlink():
        return config_path
    return config_path.resolve(strict=True)


def _atomic_write_text(
    target_config: Path,
    content: str,
    *,
    mode: int | None,
) -> None:
    """Write text through a same-directory fsynced temp file and replace."""
    file_descriptor = -1
    temporary_path: Path | None = None
    try:
        file_descriptor, raw_temporary_path = tempfile.mkstemp(
            dir=str(target_config.parent),
            prefix=f".{target_config.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(raw_temporary_path)
        if mode is not None:
            fchmod = getattr(os, "fchmod", None)
            if fchmod is not None:
                try:
                    fchmod(file_descriptor, mode)
                except (AttributeError, NotImplementedError):
                    os.chmod(temporary_path, mode)
            else:
                os.chmod(temporary_path, mode)
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary_file:
            file_descriptor = -1
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(str(temporary_path), str(target_config))
        _fsync_parent_directory(target_config.parent)
    except BaseException:
        if file_descriptor >= 0:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
        raise
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def ensure_codex_project_trust(
    project_path: Path,
    *,
    trust_level: str = "trusted",
    config_path: Path | None = None,
) -> tuple[bool, str]:
    """Ensure one exact project path is trusted in Codex config."""
    target_config = config_path if config_path is not None else codex_config_path()
    try:
        target_config.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"Failed to read config: {exc}"

    try:
        try:
            write_target = _resolve_config_target(target_config)
            lock_target = write_target.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            return False, f"Failed to resolve config: {exc}"
        with _config_lock(lock_target):
            try:
                try:
                    target_stat = write_target.stat()
                except FileNotFoundError:
                    content = ""
                    config_mode = None
                else:
                    if not stat.S_ISREG(target_stat.st_mode):
                        return False, "Failed to read config: config target is not a regular file"
                    content = write_target.read_text(encoding="utf-8")
                    config_mode = stat.S_IMODE(target_stat.st_mode)
            except OSError as exc:
                return False, f"Failed to read config: {exc}"

            project_key = json.dumps(str(project_path), ensure_ascii=False)
            section_header = f"[projects.{project_key}]"
            desired_line = f"trust_level = {json.dumps(trust_level)}"
            lines = content.splitlines()
            start: int | None = None
            end = len(lines)
            for index, line in enumerate(lines):
                if line.strip() != section_header:
                    continue
                start = index
                for next_index in range(index + 1, len(lines)):
                    maybe_header = lines[next_index].strip()
                    if maybe_header.startswith("[") and maybe_header.endswith("]"):
                        end = next_index
                        break
                break

            if start is None:
                if lines and lines[-1].strip():
                    lines.append("")
                lines.extend((section_header, desired_line))
            else:
                updated = False
                for index in range(start + 1, end):
                    stripped = lines[index].strip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    left, separator, _right = lines[index].partition("=")
                    if separator and left.strip() == "trust_level":
                        indent = lines[index][: len(lines[index]) - len(lines[index].lstrip())]
                        lines[index] = f"{indent}{desired_line}"
                        updated = True
                        break
                if not updated:
                    lines.insert(start + 1, desired_line)

            new_content = "\n".join(lines)
            if lines:
                new_content += "\n"
            if new_content == content:
                return True, ""
            try:
                _atomic_write_text(write_target, new_content, mode=config_mode)
            except OSError as exc:
                return False, f"Failed to write config: {exc}"
            return True, ""
    except OSError as exc:
        return False, f"Failed to lock config: {exc}"
