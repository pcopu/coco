"""Telegram -> real controller -> Codex stdio -> Telegram scenarios."""

import json

from .telegram_peer import ADMIN, CHAT


def test_controller_boots_and_polls_only_configured_telegram_endpoint(controller_factory):
    controller = controller_factory(start=False)
    controller.start()
    assert controller.requests("getMe")
    commands = controller.requests("setMyCommands")[-1]["data"]["commands"]
    assert {"start", "resume", "status", "apps", "update"} <= {c["command"] for c in commands}
    initialized = controller.wait_codex("initialize")
    assert initialized["params"]["capabilities"]["experimentalApi"] is True
    summary = controller.wait_request("sendMessage", text="Weekly resource average")
    assert "RAM:" in summary["data"]["text"] and "Disk:" in summary["data"]["text"]
    assert (controller.root / "coco/resource_monitor_state.json").exists()
    assert (controller.root / "sandbox-ready").exists()
    assert not (controller.root / "denials.log").exists(), controller.diagnostics()


def test_bound_topic_round_trip_persists_only_visible_chat(controller):
    controller.bind()
    mark = len(controller.peer.requests)
    controller.peer.message("Please report the result")
    turn = controller.wait_codex("turn/start")
    reply = controller.wait_request("sendMessage", since=mark, topic=10, text="E2E answer")
    assert reply["data"]["chat_id"] == CHAT
    assert turn["params"]["threadId"]
    assert "Please report the result" in json.dumps(turn["params"]["input"])
    memory = [json.loads(line) for line in (controller.root / "memory.jsonl").read_text().splitlines()]
    assert any(row.get("text") == "Please report the result" and row["direction"] == "in" for row in memory)
    assert any("E2E answer" in row.get("text", "") and row["direction"] == "out_send" for row in memory)
    assert all("jsonrpc" not in row.get("text", "") for row in memory)
    assert (controller.root / "coco/state.json").exists()
    assert not (controller.root / "denials.log").exists(), controller.diagnostics()


def test_unauthorized_user_and_disallowed_group_never_dispatch(controller):
    controller.bind()
    controller.peer.message("steal this session", user=777777)
    controller.wait_request("sendMessage", text="not authorized")
    mark = len(controller.peer.requests)
    controller.peer.message("wrong group", user=ADMIN, chat_id=CHAT - 1)
    controller.wait_request("sendMessage", since=mark, text="not allowed")
    assert not controller.codex("turn/start")
    controller.peer.message("legitimate message")
    controller.wait_request("sendMessage", text="E2E answer")
    assert len(controller.codex("turn/start")) == 1
