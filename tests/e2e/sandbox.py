"""Linux child launcher: fail closed if kernel filesystem/TCP policy is unavailable.

No CoCo objects are replaced. CoCo runs through its ordinary module entry point.
The environment is supplied from an allowlist by the harness; Landlock is
inherited across exec, including the scripted Codex and temporary Git helpers.
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import runpy
import sys


def confine() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    abi = libc.syscall(444, 0, 0, 1)
    if abi < 4:
        raise RuntimeError(f"E2E requires Linux Landlock ABI >= 4; got {abi}")

    class Ruleset(ctypes.Structure):
        _fields_ = [("fs", ctypes.c_uint64), ("net", ctypes.c_uint64)]

    class PathRule(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("access", ctypes.c_uint64), ("parent", ctypes.c_int32)]

    class NetRule(ctypes.Structure):
        _fields_ = [("access", ctypes.c_uint64), ("port", ctypes.c_uint64)]

    fs_all = (1 << 15) - 1  # v3 adds TRUNCATE, v4 adds TCP policy
    read = (1 << 2) | (1 << 3)
    legacy_ipc = bool(os.environ.get("COCO_E2E_LEGACY_IPC"))
    ruleset = Ruleset(fs_all, 1 if legacy_ipc else 3)
    fd = libc.syscall(444, ctypes.byref(ruleset), ctypes.sizeof(ruleset), 0)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset")
    try:
        root = Path(os.environ["COCO_E2E_ROOT"]).resolve()
        for path, access in [
            (root, fs_all),
            (Path(os.environ["COCO_E2E_SOURCE"]), read),
            (Path(os.environ["COCO_E2E_VENV"]), read),
            (Path(__file__).resolve(), 1 << 2),
            (Path("/usr"), read | 1),
            (Path("/lib"), read | 1),
            (Path("/lib64"), read | 1),
            (Path("/etc/ssl"), read),
            (Path("/etc/ld.so.cache"), 1 << 2),
            (Path("/etc/mime.types"), 1 << 2),
            (Path("/dev/null"), (1 << 1) | (1 << 2)),
            (Path("/dev/urandom"), 1 << 2),
            (Path("/proc/meminfo"), 1 << 2),
            (Path("/proc/cpuinfo"), 1 << 2),
            # Existing multiprocessing.Barrier tests create unnamed semaphores
            # here. E2E application children never receive this allowance.
            *(([(Path("/dev/shm"), fs_all)]) if legacy_ipc else []),
        ]:
            if not path.exists():
                continue
            path_fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = PathRule(access, path_fd)
                if libc.syscall(445, fd, 1, ctypes.byref(rule), 0) != 0:
                    raise OSError(ctypes.get_errno(), f"landlock_add_rule {path}")
            finally:
                os.close(path_fd)
        ports = os.environ["COCO_E2E_PORTS"].split(",") + (["0"] if legacy_ipc else [])
        for port in ports:
            rule = NetRule(1 if legacy_ipc else 3, int(port))
            if libc.syscall(445, fd, 2, ctypes.byref(rule), 0) != 0:
                raise OSError(ctypes.get_errno(), "landlock_add_net_rule")
        if libc.prctl(38, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "PR_SET_NO_NEW_PRIVS")
        if libc.syscall(446, fd, 0) != 0:
            raise OSError(ctypes.get_errno(), "landlock_restrict_self")
    finally:
        os.close(fd)


if __name__ == "__main__":
    confine()
    Path(os.environ["COCO_E2E_ROOT"], "sandbox-ready").touch()
    module = sys.argv[1]
    sys.argv = [module, *sys.argv[2:]]
    runpy.run_module(module, run_name="__main__", alter_sys=True)
