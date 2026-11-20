"""Real child-process harness; assertions observe Telegram, RPC and persisted files."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import time

from .telegram_peer import ADMIN, CHAT, MEMBER, TOKEN, TelegramPeer


REPO = Path(__file__).resolve().parents[2]
SUPPORT = Path(__file__).resolve().parent


def unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def eventually(predicate, *, timeout=8, description="condition", diagnostics=lambda: ""):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(f"Timed out waiting for {description}\n{diagnostics()}")


class Controller:
    def __init__(self, root: Path, guard: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        for name in ("home", "coco", "codex", "sessions", "workspace", "tmp", "apps", "skills"):
            (root / name).mkdir(exist_ok=True)
        self.peer = TelegramPeer(root)
        self.rpc_port = unused_port()
        self.proc: subprocess.Popen | None = None
        self.log = None
        self.peer.start()
        source = Path(os.environ.get("COCO_E2E_TEST_SOURCE", str(REPO / "src"))).resolve()
        shutil.copyfile(guard, root / "guard.so")
        assistant = root / "codex-peer"
        assistant.write_text(f"#!{sys.executable}\n" + (SUPPORT / "codex_peer.py").read_text())
        assistant.chmod(0o700)
        (root / "coco/allowed_users_meta.json").write_text(json.dumps({
            "names": {str(ADMIN): "Admin", str(MEMBER): "Member"}, "admins": [ADMIN],
            "scopes": {str(ADMIN): "create_sessions", str(MEMBER): "create_sessions"},
        }))
        self.env = {
            "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TZ": "UTC",
            "HOME": str(root / "home"), "COCO_DIR": str(root / "coco"),
            "CODEX_HOME": str(root / "codex"), "SESSIONS_PATH": str(root / "sessions"),
            "TMPDIR": str(root / "tmp"), "XDG_CACHE_HOME": str(root / "home/.cache"),
            "XDG_CONFIG_HOME": str(root / "home/.config"), "XDG_DATA_HOME": str(root / "home/.local/share"),
            "PYTHONPATH": str(source), "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1", "PYTHONNOUSERSITE": "1",
            "TELEGRAM_BOT_TOKEN": TOKEN, "ALLOWED_USERS": f"{ADMIN},{MEMBER}",
            "ALLOWED_GROUP_IDS": str(CHAT), "BROWSE_ROOT": str(root / "workspace"),
            "ASSISTANT_COMMAND": str(assistant), "COCO_MACHINE_ID": "e2e-controller",
            "COCO_MACHINE_NAME": "E2E Controller", "COCO_RPC_LISTEN_HOST": "127.0.0.1",
            "COCO_RPC_ADVERTISE_HOST": "127.0.0.1",
            "COCO_RPC_PORT": str(self.rpc_port), "COCO_CLUSTER_SHARED_SECRET": "e2e-isolated-secret",
            "COCO_UPDATE_CHECK_ENABLED": "false", "COCO_CODEX_APP_SERVER_SYSTEMD_RUN": "false",
            "COCO_APPS_PATHS": str(root / "apps"), "COCO_CODEX_SKILLS_PATHS": str(root / "skills"),
            "COCO_TELEGRAM_MEMORY_LOG_PATH": str(root / "memory.jsonl"),
            "COCO_TELEGRAM_BASE_URL": self.peer.url + "/bot",
            "COCO_TELEGRAM_BASE_FILE_URL": self.peer.url + "/file/bot",
            "MONITOR_POLL_INTERVAL": "0.1", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_ALLOW_PROTOCOL": "file",
            "LD_PRELOAD": str(root / "guard.so"), "COCO_E2E_ROOT": str(root),
            "COCO_E2E_SOURCE": str(source), "COCO_E2E_VENV": str(REPO / ".venv"),
            "COCO_E2E_PORTS": f"{self.peer.port},{self.rpc_port}",
            "COCO_E2E_DENIAL_LOG": str(root / "denials.log"),
        }

    def launch(self, *, module="coco.main", args=()):
        assert self.proc is None or self.proc.poll() is not None
        self.log = (self.root / "controller.log").open("ab")
        self.proc = subprocess.Popen(
            [sys.executable, str(SUPPORT / "sandbox.py"), module, *args],
            cwd=self.root / "workspace", env=self.env, stdout=self.log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        return self.proc

    def start(self):
        mark = len(self.peer.requests)
        self.launch()
        eventually(lambda: self.requests("getUpdates", since=mark), description="controller polling loopback Telegram",
                   diagnostics=self.diagnostics, timeout=10)
        assert self.proc.poll() is None, self.diagnostics()
        return self

    def diagnostics(self):
        log = self.root / "controller.log"
        return f"pid={self.proc.pid if self.proc else None}, artifacts={self.root}\n" + (
            log.read_text(errors="replace")[-14000:] if log.exists() else ""
        )

    def requests(self, method=None, *, since=0, topic=None, text=None):
        return [r for r in self.peer.requests[since:] if
                (method is None or r["method"] == method) and
                (topic is None or r["data"].get("message_thread_id") == topic or
                 (topic == 1 and r["data"].get("chat_id") == CHAT and not r["data"].get("message_thread_id"))) and
                (text is None or text in re.sub(r"\\([_*\[\]()~`>#+\-=|{}.!])", r"\1", str(r["data"].get("text", ""))))]

    def wait_request(self, method, *, since=0, topic=None, text=None, timeout=8):
        return eventually(lambda: [r for r in self.requests(method, since=since, topic=topic, text=text) if r.get("ok")],
                          description=f"Telegram {method} topic={topic} text={text!r}",
                          diagnostics=self.diagnostics, timeout=timeout)[-1]

    def codex(self, method=None, *, direction="in"):
        path = self.root / "codex.jsonl"
        if not path.exists():
            return []
        result = []
        for line in path.read_text().splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue  # writer may still be completing the final line
            if entry["direction"] == direction and (method is None or entry["payload"].get("method") == method):
                result.append(entry)
        return result

    def wait_codex(self, method, *, count=1, timeout=8):
        return eventually(lambda: self.codex(method) if len(self.codex(method)) >= count else None,
                          description=f"Codex {method} count={count}", timeout=timeout,
                          diagnostics=self.diagnostics)[-1]["payload"]

    def rules(self, *rules):
        path = self.root / "peer-rules.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(rules))
        temporary.replace(path)

    def emit(self, method: str, params: dict, *, request_id=None):
        payload = {"method": method, "params": params}
        if request_id is not None:
            payload["id"] = request_id
        with (self.root / "peer-events.jsonl").open("a") as stream:
            stream.write(json.dumps(payload) + "\n")

    def button(self, label: str, *, since=0, topic=10, user=ADMIN):
        def find():
            for request in reversed(self.peer.requests[since:]):
                data = request["data"]
                if request["method"] not in {"sendMessage", "editMessageText", "editMessageReplyMarkup"}:
                    continue
                mid = data.get("message_id")
                message = self.peer.messages.get(mid) if mid else next(
                    (m for m in reversed(list(self.peer.messages.values())) if
                     m.get("reply_markup") == data.get("reply_markup") and m.get("message_thread_id") == topic), None)
                if not message or message.get("message_thread_id") != topic:
                    continue
                markup = data.get("reply_markup") or {}
                for row in markup.get("inline_keyboard", []):
                    for button in row:
                        if label in button["text"]:
                            return button, message
            return None
        button, message = eventually(find, description=f"button {label!r}", diagnostics=self.diagnostics)
        self.peer.callback(button["callback_data"], message, user=user)
        return button, message

    def bind(self, *, topic=10, user=ADMIN):
        mark = len(self.peer.requests)
        self.peer.message("/start", topic=topic, user=user)
        self.button("Select", since=mark, topic=topic, user=user)
        self.wait_request("editMessageText", since=mark, text="Bound to this topic")
        return self.binding(topic=topic, user=user)

    def state(self):
        return json.loads((self.root / "coco/state.json").read_text())

    def binding(self, *, topic=10, user=ADMIN, chat_id=CHAT):
        return self.state().get("topic_bindings_v2", {}).get(str(user), {}).get(f"{chat_id}:{topic}")

    def complete(self, turn: dict, text="completed by peer"):
        params = turn["params"]
        tid = params["threadId"]
        # Read the peer's wire reply, not CoCo's internal state.
        reply = eventually(lambda: next((e["payload"] for e in self.codex(direction="out")
                           if e["payload"].get("id") == turn["id"]), None),
                           description="peer turn response", diagnostics=self.diagnostics)
        turn_id = reply["result"]["turn"]["id"]
        self.emit("item/completed", {"threadId": tid, "turnId": turn_id,
                  "item": {"id": f"final-{turn_id}", "type": "agentMessage", "text": text}})
        self.emit("turn/completed", {"threadId": tid, "turn": {"id": turn_id, "status": "completed"}})

    def rpc(self, method, params=None, *, secret=None, raw=None):
        request = {"id": "e2e-request", "secret": secret if secret is not None else self.env["COCO_CLUSTER_SHARED_SECRET"],
                   "method": method, "params": params or {}}
        with socket.create_connection(("127.0.0.1", self.rpc_port), timeout=3) as sock:
            sock.settimeout(15)
            sock.sendall(raw if raw is not None else (json.dumps(request) + "\n").encode())
            with sock.makefile("rb") as reader:
                response = json.loads(reader.readline())
        with (self.root / "rpc.jsonl").open("a") as stream:
            stream.write(json.dumps({"request": request, "response": response}) + "\n")
        return response

    def start_agent(self):
        self.env["COCO_NODE_ROLE"] = "agent"
        self.launch()
        def ready():
            try:
                return self.rpc("agent/ping").get("ok")
            except (ConnectionError, OSError):
                return False
        eventually(ready, description="agent RPC listener", diagnostics=self.diagnostics)
        return self

    def stop(self):
        proc = self.proc
        if proc is None:
            return
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT if self.env.get("COCO_NODE_ROLE") == "agent" else signal.SIGTERM)
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                # This dedicated group was created by launch; never touch any other group.
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=3)
        # Detect/clean only descendants in the worker-created process group.
        try:
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            pass
        else:
            os.killpg(proc.pid, signal.SIGKILL)
            raise AssertionError(f"Controller leaked a descendant process: group {proc.pid}")
        if self.log:
            self.log.close()
            self.log = None

    def close(self):
        try:
            self.stop()
        finally:
            self.peer.close()
        assert not self.peer.unknown, f"Unimplemented Telegram methods: {self.peer.unknown}"
