"""Faults occur at the external peer while the real queues/client run unchanged."""

import json
import time


def test_queued_inputs_dispatch_fifo_once_after_completion(controller):
    controller.bind()
    controller.rules({"method": "turn/start", "hold": True})
    controller.peer.message("first running input")
    first = controller.wait_codex("turn/start")
    controller.peer.message("/q second queued input")
    controller.peer.message("/q third queued input")
    controller.wait_request("editMessageText", text="third queued input")
    assert len(controller.codex("turn/start")) == 1
    assert not controller.codex("turn/steer")
    controller.complete(first, "first finished")
    second = controller.wait_codex("turn/start", count=2)
    assert "second queued input" in json.dumps(second["params"])
    assert "third queued input" not in json.dumps(second["params"])
    controller.complete(second, "second finished")
    third = controller.wait_codex("turn/start", count=3)
    assert "third queued input" in json.dumps(third["params"])
    controller.complete(third, "third finished")
    controller.wait_request("sendMessage", text="third finished")
    assert len(controller.codex("turn/start")) == 3
    assert {r["payload"]["params"]["threadId"] for r in controller.codex("turn/start")} == {first["params"]["threadId"]}


def test_normal_text_steers_active_turn_and_escape_interrupts_it(controller):
    binding = controller.bind()
    controller.rules({"method": "turn/start", "hold": True}, {"method": "turn/steer", "hold": True})
    controller.peer.message("work until instructed")
    controller.wait_codex("turn/start")
    controller.peer.message("adjust the approach")
    steer = controller.wait_codex("turn/steer")
    assert steer["params"]["threadId"] == binding["codex_thread_id"]
    assert "adjust the approach" in json.dumps(steer["params"]["input"])
    controller.peer.message("/esc")
    interrupt = controller.wait_codex("turn/interrupt")
    assert interrupt["params"]["threadId"] == binding["codex_thread_id"]
    assert interrupt["params"]["turnId"] == steer["params"]["expectedTurnId"]
    assert len(controller.codex("turn/start")) == 1


def test_unbinding_discards_queued_input_and_late_output(controller):
    controller.bind()
    controller.rules({"method": "turn/start", "hold": True})
    controller.peer.message("old running input")
    old = controller.wait_codex("turn/start")
    controller.peer.message("/q must never be replayed")
    controller.wait_request("sendMessage", text="must never be replayed")
    controller.peer.message("/unbind")
    controller.wait_request("sendMessage", text="unbound")
    controller.complete(old, "STALE SECRET OUTPUT")
    controller.rules({"method": "turn/start", "reply": "fresh output"})
    controller.bind()
    mark = len(controller.peer.requests)
    controller.peer.message("new session input")
    controller.wait_request("sendMessage", since=mark, text="fresh output")
    turns = controller.codex("turn/start")
    assert len(turns) == 2
    assert turns[0]["payload"]["params"]["threadId"] != turns[1]["payload"]["params"]["threadId"]
    assert not controller.requests("sendMessage", text="STALE SECRET OUTPUT")
    assert all("must never be replayed" not in json.dumps(turn["payload"]) for turn in turns)


def test_process_exit_after_mutation_never_replays_the_input(controller):
    controller.bind()
    controller.rules({"method": "turn/start", "exit": 17})
    controller.peer.message("non-idempotent operation")
    controller.wait_codex("turn/start")
    # The operation was written before the transport died. It must surface
    # uncertainty and wait for explicit user input instead of auto-retrying.
    controller.wait_request("sendMessage", text="disconnected", timeout=12)
    assert len(controller.codex("turn/start")) == 1
    controller.rules({"method": "turn/start", "reply": "explicit recovery answer"})
    controller.peer.message("explicit recovery message")
    controller.wait_request("sendMessage", text="explicit recovery answer")
    assert len(controller.codex("turn/start")) == 2
    assert len(controller.codex("initialize")) == 2


def test_notification_flood_does_not_block_other_topics_or_duplicate_final(controller):
    a = controller.bind(topic=10)
    controller.bind(topic=20)
    controller.rules({"method": "turn/start", "hold": True})
    controller.peer.message("busy topic", topic=10)
    first = controller.wait_codex("turn/start")
    for index in range(400):
        controller.emit("item/agentMessage/delta", {"threadId": a["codex_thread_id"], "delta": f"{index}"})
    controller.rules({"method": "turn/start", "reply": "other topic stays responsive"})
    controller.peer.message("other topic input", topic=20)
    controller.wait_request("sendMessage", topic=20, text="other topic stays responsive")
    controller.complete(first, "flood final")
    controller.wait_request("sendMessage", topic=10, text="flood final")
    assert len(controller.requests("sendMessage", topic=10, text="flood final")) == 1
    assert not controller.requests("sendMessage", topic=20, text="flood final")


def test_lost_turn_response_surfaces_uncertainty_without_automatic_replay(controller):
    controller.bind()
    controller.rules({"method": "turn/start", "drop": True})
    controller.peer.message("operation with lost acknowledgement")
    controller.wait_codex("turn/start")
    controller.wait_request("sendMessage", text="Timed out", timeout=30)
    assert len(controller.codex("turn/start")) == 1
    controller.rules({"method": "turn/start", "reply": "explicit retry succeeds"})
    controller.peer.message("user explicitly requests another operation")
    controller.wait_request("sendMessage", text="explicit retry succeeds")
    assert len(controller.codex("turn/start")) == 2


def test_watchdog_keeps_active_turn_without_replaying_it(controller):
    controller.bind()
    controller.rules({"method": "turn/start", "hold": True})
    started = time.monotonic()
    controller.peer.message("continue this long running operation")
    turn = controller.wait_codex("turn/start")
    controller.wait_request("sendMessage", topic=10, text="⏳ Working")
    mark = len(controller.peer.requests)
    keepalive = controller.wait_request("sendMessage", since=mark, topic=10, timeout=40)
    assert time.monotonic() - started >= 28
    assert len(keepalive["data"]["text"].strip()) <= 4
    assert len(controller.codex("turn/start")) == 1
    assert not controller.codex("turn/steer")
    controller.complete(turn, "long running operation finished")
    controller.wait_request("sendMessage", topic=10, text="long running operation finished")
