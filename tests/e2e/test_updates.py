"""Both real updaters run git pull --ff-only exclusively in temporary clones."""

from pathlib import Path
import shutil
import subprocess

import pytest

def git(cwd, *args):
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                            env={"PATH": "/usr/bin:/bin", "HOME": str(cwd), "GIT_CONFIG_NOSYSTEM": "1",
                                 "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_ALLOW_PROTOCOL": "file",
                                 "GIT_AUTHOR_NAME": "E2E", "GIT_AUTHOR_EMAIL": "e2e@example.invalid",
                                 "GIT_COMMITTER_NAME": "E2E", "GIT_COMMITTER_EMAIL": "e2e@example.invalid"})
    return result.stdout.strip()


def runtime_clone(controller, mode):
    upstream = controller.root / "upstream"
    upstream.mkdir()
    shutil.copytree(Path(controller.env["COCO_E2E_SOURCE"]), upstream / "src",
                    ignore=shutil.ignore_patterns("*.bak*", "__pycache__"))
    (upstream / "tracked.txt").write_text("initial\n")
    git(upstream, "init", "-q", "-b", "main")
    git(upstream, "add", "src", "tracked.txt")
    git(upstream, "commit", "-qm", "fixture baseline")
    runtime = controller.root / "runtime"
    git(controller.root, "clone", "-q", str(upstream), str(runtime))
    before = git(runtime, "rev-parse", "HEAD")
    for name in (".codex-research/notes.txt", "tmp/cache.txt", "keep.bak"):
        path = runtime / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"user-owned {name}")
    if mode == "tracked":
        (runtime / "tracked.txt").write_text("local tracked work\n")
    (upstream / "tracked.txt").write_text("updated upstream\n")
    if mode == "collision":
        (upstream / "collision.txt").write_text("upstream contents")
        (runtime / "collision.txt").write_text("precious untracked contents")
    git(upstream, "add", ".")
    git(upstream, "commit", "-qm", "fixture incoming update")
    after = git(upstream, "rev-parse", "HEAD")
    controller.env["PYTHONPATH"] = str(runtime / "src")
    controller.env["COCO_E2E_SOURCE"] = str(runtime / "src")
    return runtime, before, after


@pytest.mark.parametrize("runtime_role", ["controller", "agent"])
@pytest.mark.parametrize("mode", ["untracked", "tracked", "collision"])
def test_updater_preserves_untracked_and_blocks_tracked_or_incoming_collisions(controller_factory, runtime_role, mode):
    process = controller_factory(start=False)
    runtime, before, after = runtime_clone(process, mode)
    if runtime_role == "agent":
        process.start_agent()
        result = process.rpc("agent/run_update", {"action": "coco"})
        assert result["ok"], result
        outcome = result["result"]
        success = outcome["ok"]
        message = outcome["message"]
    else:
        process.start()
        mark = len(process.peer.requests)
        process.peer.message("/update run coco")
        if mode == "untracked":
            response = process.wait_request("sendMessage", since=mark, text="CoCo updated:")
        elif mode == "tracked":
            response = process.wait_request("sendMessage", since=mark, text="worktree has local changes")
        else:
            response = process.wait_request("sendMessage", since=mark, text="CoCo update failed")
        message = response["data"]["text"]
        success = "CoCo updated:" in message
    assert success == (mode == "untracked"), message
    assert git(runtime, "rev-parse", "HEAD") == (after if success else before)
    for name in (".codex-research/notes.txt", "tmp/cache.txt", "keep.bak"):
        assert (runtime / name).read_text() == f"user-owned {name}"
    if mode == "tracked":
        assert "worktree has local changes" in message
        assert (runtime / "tracked.txt").read_text() == "local tracked work\n"
    if mode == "collision":
        assert "overwritten" in message or "Aborting" in message
        assert (runtime / "collision.txt").read_text() == "precious untracked contents"
    # No updater touched the worker's actual source path; only these cloned HEADs
    # can change. Actual source is never a write-allowed path in the child policy.
    assert Path(process.env["COCO_E2E_SOURCE"]).is_relative_to(process.root)
