"""Executable deterministic Codex boundary peer (JSON-RPC over real stdio).

The harness supplies rules/events on disk; CoCo knows only ASSISTANT_COMMAND.
Rules can hold/drop/delay replies, exit the process, emit late notifications,
and ask for approvals. No internal CoCo function is imported or patched.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid

ROOT = Path(os.environ["COCO_E2E_ROOT"])
LOCK = threading.RLock()
STATE_PATH = ROOT / "peer-state.json"
STATE = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {"threads": {}, "goals": {}}


def record(direction, payload):
    with LOCK, (ROOT / "codex.jsonl").open("a") as stream:
        stream.write(json.dumps({"direction": direction, "pid": os.getpid(), "payload": payload,
                                 "time": time.monotonic()}) + "\n")


def write(payload):
    with LOCK:
        if payload.get("method") == "turn/completed":
            params = payload["params"]
            for turn in STATE["threads"].get(params.get("threadId"), {}).get("turns", []):
                if turn["id"] == params["turn"]["id"]:
                    turn["status"] = params["turn"].get("status", "completed")
            persist()
        elif payload.get("method") == "item/completed":
            params = payload["params"]
            for turn in STATE["threads"].get(params.get("threadId"), {}).get("turns", []):
                if turn["id"] == params.get("turnId"):
                    turn["items"].append(params["item"])
            persist()
        record("out", payload)
        print(json.dumps({"jsonrpc": "2.0", **payload}), flush=True)


def notify(method, params):
    write({"method": method, "params": params})


def persist():
    with LOCK:
        temporary = STATE_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(STATE))
        temporary.replace(STATE_PATH)


def finish(thread_id, turn_id, text, delay):
    time.sleep(delay)
    notify("item/completed", {"threadId": thread_id, "turnId": turn_id,
           "item": {"id": f"item-{turn_id}", "type": "agentMessage", "text": text}})
    notify("turn/completed", {"threadId": thread_id, "turn": {"id": turn_id, "status": "completed"}})


def process(request):
    record("in", request)
    method = request.get("method")
    if method is None or "id" not in request:
        return
    params = request.get("params", {})
    rules_path = ROOT / "peer-rules.json"
    rules = json.loads(rules_path.read_text()) if rules_path.exists() else []
    rule = next((r for r in rules if r["method"] == method and
                 r.get("contains", "") in json.dumps(params)), {})
    if rule.get("exit"):
        os._exit(int(rule["exit"]))
    if rule.get("drop"):
        return
    if rule.get("delay"):
        time.sleep(rule["delay"])
    if "error" in rule:
        write({"id": request["id"], "error": rule["error"]})
        return
    result = {}
    thread_id = params.get("threadId", "")
    after_reply = None
    if method == "initialize":
        result = {"userAgent": "codex-e2e/1.0"}
    elif method in {"thread/start", "thread/fork"}:
        thread_id = str(uuid.uuid4())
        original = STATE["threads"].get(params.get("threadId", ""), {})
        thread = {**original, "id": thread_id, "cwd": params.get("cwd", original.get("cwd", str(ROOT / "workspace"))),
                  "createdAt": int(time.time()), "updatedAt": int(time.time()), "preview": "E2E session", "turns": []}
        STATE["threads"][thread_id] = thread
        result = {"thread": thread, "model": "e2e-model", "reasoningEffort": "medium"}
    elif method in {"thread/resume", "thread/read", "thread/rollback"}:
        thread = STATE["threads"].get(thread_id)
        if thread is None:
            write({"id": request["id"], "error": {"code": -32000, "message": "Thread not found"}})
            return
        if method == "thread/rollback":
            thread["turns"] = thread["turns"][:-int(params.get("numTurns", 1))]
        result = {"thread": thread, "model": "e2e-model", "reasoningEffort": "medium"}
    elif method == "thread/list":
        result = {"data": list(STATE["threads"].values()), "nextCursor": None}
    elif method in {"turn/start", "turn/steer"}:
        turn_id = params.get("expectedTurnId") or f"turn-{uuid.uuid4().hex[:12]}"
        turn = {"id": turn_id, "status": "inProgress", "items": []}
        if thread_id in STATE["threads"] and method == "turn/start":
            STATE["threads"][thread_id]["turns"].append(turn)
        result = {"turn": turn, "turnId": turn_id}
        def after_reply():
            if method == "turn/start":
                notify("turn/started", {"threadId": thread_id, "turn": turn})
            if not rule.get("hold"):
                finish(thread_id, turn_id, rule.get("reply", "E2E answer"), rule.get("complete_after", 0.05))
    elif method == "turn/interrupt":
        def after_reply():
            notify("turn/completed", {"threadId": thread_id, "turn": {"id": params.get("turnId"), "status": "interrupted"}})
    elif method == "model/list":
        result = {"data": [{"id": "e2e-model", "model": "e2e-model", "displayName": "E2E Model",
                            "description": "Deterministic model", "isDefault": True,
                            "defaultReasoningEffort": "medium", "supportedReasoningEfforts": [
                                {"reasoningEffort": e, "description": e} for e in ("low", "medium", "high")]}],
                  "nextCursor": None}
    elif method == "account/rateLimits/read":
        result = {"rateLimits": {"primary": {"usedPercent": 10, "windowDurationMins": 300,
                                              "resetsAt": int(time.time()) + 3600}}}
    elif method.startswith("thread/goal/"):
        if method.endswith("set"):
            STATE["goals"][thread_id] = {"id": "goal-e2e", **params, "status": params.get("status", "active")}
        elif method.endswith("clear"):
            STATE["goals"].pop(thread_id, None)
        result = {"goal": STATE["goals"].get(thread_id)}
    elif method in {"config/read", "skills/list", "plugin/list", "mcpServerStatus/list", "account/read"}:
        result = {"data": [], "config": {}, "account": {"type": "chatgpt", "email": "test@example.invalid"}}
    elif method == "thread/archive":
        STATE["threads"].pop(thread_id, None)
    else:
        write({"id": request["id"], "error": {"code": -32601, "message": f"Unsupported peer method: {method}"}})
        return
    persist()
    write({"id": request["id"], "result": rule.get("result", result)})
    if after_reply:
        after_reply()
    for event in rule.get("events", []):
        write(event)


def external_events():
    path = ROOT / "peer-events.jsonl"
    offset = 0
    while True:
        if path.exists():
            with path.open() as stream:
                stream.seek(offset)
                for line in stream:
                    write(json.loads(line))
                offset = stream.tell()
        time.sleep(0.01)


if __name__ == "__main__":
    record("start", {"argv": sys.argv})
    threading.Thread(target=external_events, daemon=True).start()
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except ValueError:
            continue
        threading.Thread(target=process, args=(request,), daemon=True).start()
