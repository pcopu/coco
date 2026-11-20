#!/usr/bin/env python3
"""Run CoCo tests with disposable state, without inheriting operator credentials.

E2E children have additional kernel/network guards. Legacy tests run against a
temporary source snapshot under the same guards, so even an accidental real
updater invocation cannot act on the working checkout. No test is filtered from
the selected suites. Artifacts are kept for inspection.
"""

from __future__ import annotations

import argparse
import io
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))
from e2e.harness import Controller, SUPPORT  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=("e2e", "legacy"))
    parser.add_argument("--artifacts", type=Path, default=Path(tempfile.gettempdir()) / "coco-e2e-runs")
    parser.add_argument("--source-ref", help="Test source from a Git revision in a disposable snapshot")
    parser.add_argument("--fault", choices=("allow-every-user", "allow-remote-ingress"),
                        help="Deliberately break a temporary source snapshot; selected E2E must fail")
    args, pytest_args = parser.parse_known_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix=f"{args.suite}-", dir=args.artifacts)).resolve()
    guard = root / "compiled-guard.so"
    subprocess.run(["gcc", "-shared", "-fPIC", "-Wall", "-Werror", "-O2", "-o", str(guard),
                    str(SUPPORT / "guard.c"), "-ldl"], check=True)
    harness = Controller(root / "runner", guard)
    proc = None
    try:
        env = dict(harness.env)
        source = REPO / "src"
        alternate = root / "source-snapshot"
        alternate.mkdir()
        if args.source_ref:
            archive = subprocess.check_output(["git", "archive", args.source_ref, "src"], cwd=REPO)
            with tarfile.open(fileobj=io.BytesIO(archive)) as files:
                files.extractall(alternate, filter="data")
        else:
            shutil.copytree(source, alternate / "src", ignore=shutil.ignore_patterns("__pycache__", "*.bak*"))
        source = alternate / "src"
        if args.fault:
            target = source / "coco/bot.py"
            content = target.read_text()
            if args.fault == "allow-every-user":
                before = "return user_id is not None and config.is_user_allowed(user_id)"
                after = "return user_id is not None"
            else:
                before = '"""Reject local Telegram media before sending a path to a remote session."""'
                after = before + "\n    return None  # intentional temporary E2E negative control"
            assert content.count(before) == 1, "fault target must match exactly once"
            target.write_text(content.replace(before, after))
        # Freeze application code for this run, even if the working copy changes.
        env["COCO_E2E_TEST_SOURCE"] = str(source)
        # Disable bytecode/cache writes in the actual source checkout.
        command = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider"]
        cwd = harness.root / "workspace"
        if args.suite == "legacy":
            snapshot = root / "runner" / "checkout"
            snapshot.mkdir()
            for directory in ("tests", "apps", "doc"):
                shutil.copytree(REPO / directory, snapshot / directory,
                                ignore=shutil.ignore_patterns("__pycache__", "*.bak*"))
            for name in ("pyproject.toml", "README.md", "README_CN.md", "AGENTS.md"):
                shutil.copyfile(REPO / name, snapshot / name)
            shutil.copytree(source, snapshot / "src")
            env["PYTHONPATH"] = str(snapshot / "src")
            env["COCO_E2E_SOURCE"] = str(snapshot / "src")
            # Legacy tests assert configuration defaults and supply their own fixtures.
            for key in ("BROWSE_ROOT", "MONITOR_POLL_INTERVAL", "COCO_APPS_PATHS", "COCO_CODEX_SKILLS_PATHS",
                        "SESSIONS_PATH", "COCO_MACHINE_ID", "COCO_MACHINE_NAME", "ALLOWED_GROUP_IDS", "CODEX_HOME",
                        "COCO_RPC_PORT", "COCO_RPC_ADVERTISE_HOST", "COCO_CLUSTER_SHARED_SECRET"):
                env.pop(key, None)
            env["COCO_E2E_LEGACY_IPC"] = "1"
            env["COCO_E2E_DYNAMIC_PORTS"] = str(harness.root / "legacy-listeners.txt")
            command = [sys.executable, str(SUPPORT / "sandbox.py"), "pytest", "-p", "no:cacheprovider"]
            command += ["tests/coco", "tests/integration"]
            cwd = snapshot
        else:
            # The pytest host must run loopback emulators. Only application children
            # receive the LD/Landlock policy; the host imports no application code.
            env.pop("LD_PRELOAD")
            command += [str(REPO / "tests/e2e")]
        command += [f"--basetemp={root / 'runner' / 'pytest'}", *pytest_args]
        print(f"Artifacts: {root}\nCommand: {' '.join(command)}", flush=True)
        with (root / "pytest.log").open("wb") as log:
            proc = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, start_new_session=True)
            assert proc.stdout is not None
            for data in iter(proc.stdout.readline, b""):
                log.write(data)
                log.flush()
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
            code = proc.wait()
        return code
    finally:
        try:
            if proc is not None:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait(timeout=3)
                # Only inspect the process group this runner created.
                deadline = time.monotonic() + 1
                while True:
                    try:
                        os.killpg(proc.pid, 0)
                    except ProcessLookupError:
                        break
                    if time.monotonic() >= deadline:
                        os.killpg(proc.pid, signal.SIGKILL)
                        raise RuntimeError(f"pytest leaked descendants in owned process group {proc.pid}")
                    time.sleep(0.02)
        finally:
            harness.close()


if __name__ == "__main__":
    raise SystemExit(main())
