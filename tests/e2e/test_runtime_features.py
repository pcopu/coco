"""Topic configuration and service ingress exercised through public protocols."""

import json
from datetime import UTC, datetime, timedelta

from .harness import eventually
from .telegram_peer import ADMIN, CHAT, MEMBER


def install_skill(controller, category, name):
    folder = controller.root / category / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: E2E {name} guidance\n---\nUse the {name} workflow.\n"
    )
    return folder


def seed_yesterday_memory(controller):
    """Supply historical visible chat at the documented JSONL storage boundary."""
    timestamp = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    rows = [
        {"direction": "in", "from_user_id": ADMIN, "chat_id": CHAT, "thread_id": 10,
         "ts_utc": timestamp, "text": "Deployment deployment deployment was broken"},
        {"direction": "out_send", "chat_id": CHAT, "thread_id": 10,
         "ts_utc": timestamp, "text": "Deployment fixed and verified"},
        {"direction": "in", "from_user_id": MEMBER, "chat_id": CHAT, "thread_id": 20,
         "ts_utc": timestamp, "text": "SECRETTOPIC SECRETTOPIC SECRETTOPIC"},
    ]
    with (controller.root / "memory.jsonl").open("a") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")


def test_enabled_apps_and_native_skills_are_scoped_and_persisted(controller):
    app = install_skill(controller, "apps", "release-check")
    skill = install_skill(controller, "skills", "review-check")
    controller.bind(topic=10)
    controller.bind(topic=20)
    controller.peer.message("/apps enable release-check")
    controller.wait_request("sendMessage", text="Enabled app")
    controller.peer.message("/skills enable review-check")
    controller.wait_request("sendMessage", text="Enabled Codex skill")
    controller.stop()
    controller.start()
    controller.peer.message("use my saved workflows", topic=10)
    turn = controller.wait_codex("turn/start")
    controller.wait_request("sendMessage", topic=10, text="E2E answer")
    inputs = turn["params"]["input"]
    assert {"type": "skill", "name": "review-check", "path": str(skill)} in inputs
    assert str(app / "SKILL.md") in json.dumps(inputs)
    controller.peer.message("other topic has no workflows", topic=20)
    other = controller.wait_codex("turn/start", count=2)
    assert "release-check" not in json.dumps(other)
    assert "review-check" not in json.dumps(other)
    mark = len(controller.peer.requests)
    controller.peer.message("/apps clear")
    controller.peer.message("/skills clear")
    controller.wait_request("sendMessage", since=mark, text="Cleared Codex skills")
    controller.peer.message("work without saved workflows")
    cleared = controller.wait_codex("turn/start", count=3)
    assert "release-check" not in json.dumps(cleared)
    assert "review-check" not in json.dumps(cleared)


def test_model_reasoning_selection_controls_next_turn_and_status(controller):
    controller.bind()
    catalog = controller.root / "home/.codex/models_cache.json"
    catalog.parent.mkdir(exist_ok=True)
    catalog.write_text(json.dumps({"models": [{"slug": "e2e-model", "visibility": "list",
        "default_reasoning_level": "medium", "supported_reasoning_levels": [{"effort": "high"}]}]}))
    controller.peer.message("/model")
    controller.button("e2e-model")
    controller.button("high")
    controller.wait_request("answerCallbackQuery", text="high")
    controller.peer.message("use selected reasoning")
    turn = controller.wait_codex("turn/start")
    controller.wait_request("sendMessage", text="E2E answer")
    assert turn["params"]["model"] == "e2e-model"
    assert turn["params"]["effort"] == "high"
    controller.peer.message("/status")
    status = controller.wait_request("sendMessage", text="Codex status")
    assert "E2E Controller" in status["data"]["text"]
    assert "90%" in status["data"]["text"]
    assert "used 10%" in status["data"]["text"]


def test_chief_of_staff_ingress_auth_busy_and_explicit_retry(controller):
    controller.bind(topic=10)
    controller.peer.message("initialize control", topic=1)
    controller.wait_request("sendMessage", topic=1, text="E2E answer")
    control_turn = controller.wait_codex("turn/start")
    controller.peer.message("/coco doctor", topic=1)
    controller.wait_request("sendMessage", topic=1, text="Control health is good")
    assert len(controller.codex("turn/start")) == 1
    controller.rules({"method": "turn/start", "hold": True})
    directive = {"method": "chief_of_staff/enqueue", "params": {
        "directive_id": "e2e-directive", "prompt": "external authorized directive", "control_chat_id": CHAT}}
    assert controller.rpc("controller/request", directive, secret="wrong")["error"] == "unauthorized"
    rejected = controller.rpc("controller/request", {**directive, "params": {}})
    assert rejected["result"]["status"] == "invalid_request"
    response = controller.rpc("controller/request", directive)
    assert response["result"] == {"accepted": True, "status": "started"}
    turn = controller.wait_codex("turn/start", count=2)
    assert turn["params"]["threadId"] == control_turn["params"]["threadId"]
    assert "external authorized directive" in json.dumps(turn)
    response = controller.rpc("controller/request", directive)
    assert response["result"]["status"] == "busy"
    assert len(controller.codex("turn/start")) == 2
    controller.complete(turn, "directive complete")
    controller.wait_request("sendMessage", topic=1, text="directive complete")
    assert not controller.requests("sendMessage", topic=10, text="directive complete")
    controller.rules({"method": "turn/start", "reply": "next directive complete"})
    directive["params"]["directive_id"] = "second-directive"
    assert controller.rpc("controller/request", directive)["result"]["status"] == "started"
    controller.wait_request("sendMessage", topic=1, text="next directive complete")
    assert len(controller.codex("turn/start")) == 3


def test_plugin_inventory_and_admin_toggle_are_persisted_without_dispatch(controller):
    manifest = controller.root / "home/.codex/plugins/cache/e2e/example/1/.codex-plugin/plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"name": "example", "version": "1", "description": "test plugin"}))
    controller.peer.message("/plugins")
    controller.wait_request("sendMessage", text="example")
    controller.peer.message("/plugins enable example", user=MEMBER)
    controller.wait_request("sendMessage", text="Only admins")
    assert not (controller.root / "home/.codex/config.toml").exists()
    controller.peer.message("/plugins enable example")
    controller.wait_request("sendMessage", text="Enabled")
    config = controller.root / "home/.codex/config.toml"
    eventually(lambda: config.exists())
    assert "example@e2e" in config.read_text()
    assert "enabled = true" in config.read_text()
    assert not controller.codex("turn/start")


def test_guided_looper_configuration_starts_and_stops_only_this_topic(controller):
    install_skill(controller, "apps", "looper")
    (controller.root / "workspace/plan.md").write_text("# Plan\nFinish the E2E task.\n")
    controller.bind()
    controller.peer.message("/apps")
    controller.button("looper")
    controller.button("Configure")
    controller.wait_request("editMessageText", text="plan.md")
    controller.button("Start")
    controller.wait_request("answerCallbackQuery", text="Looper started")
    controller.wait_request("editMessageText", text="running")
    # Stop through the same delivered action sheet, before the first timer nudge.
    mark = len(controller.peer.requests)
    controller.button("Stop")
    controller.wait_request("answerCallbackQuery", since=mark, text="Looper stopped")
    controller.wait_request("editMessageText", since=mark, text="stopped")
    assert not controller.codex("turn/start")
    controller.button("Start", since=mark)
    controller.wait_request("answerCallbackQuery", since=mark, text="Looper started")
    controller.rules({"method": "turn/start", "reply": "done"})
    controller.peer.message("finish the configured plan")
    controller.wait_request("sendMessage", text="Looper stopped after completion keyword match")
    assert len(controller.codex("turn/start")) == 1


def test_autoresearch_outcome_is_consumed_as_configuration_not_a_turn(controller):
    install_skill(controller, "apps", "autoresearch")
    controller.bind()
    controller.peer.message("/apps")
    controller.button("autoresearch")
    controller.button("Set Outcome")
    controller.wait_request("answerCallbackQuery", text="outcome")
    controller.peer.message("Reduce flaky deployments")
    controller.wait_request("sendMessage", text="Reduce flaky deployments")
    assert not controller.codex("turn/start")
    controller.stop()
    controller.start()
    mark = len(controller.peer.requests)
    controller.peer.message("/apps")
    controller.button("autoresearch", since=mark)
    controller.wait_request("editMessageText", since=mark, text="Reduce flaky deployments")
    seed_yesterday_memory(controller)
    controller.button("Run Now", since=mark)
    digest = controller.wait_request("sendMessage", topic=10, text="yesterday I reviewed")
    assert "Deployment" in digest["data"]["text"]
    assert "SECRETTOPIC" not in digest["data"]["text"]
    assert not controller.codex("turn/start")
    controller.button("Schedule Daily", since=mark)
    controller.wait_request("answerCallbackQuery", text="Daily auto research enabled")
    controller.button("Stop Daily", since=mark)
    controller.wait_request("answerCallbackQuery", text="Daily auto research stopped")


def test_personality_digest_uses_only_topic_memory_and_is_not_repeated_after_restart(controller_factory):
    controller = controller_factory(start=False)
    # Configure local wall time near noon through the ordinary process timezone,
    # so the daily scheduler is eligible at any UTC test time; no clock is patched.
    offset = 12 - datetime.now(UTC).hour
    controller.env["TZ"] = f"UTC{-offset:+d}"
    install_skill(controller, "apps", "personality")
    controller.start()
    controller.bind(topic=10)
    controller.bind(topic=20, user=MEMBER)
    seed_yesterday_memory(controller)
    controller.peer.message("/apps enable personality")
    controller.wait_request("sendMessage", text="Enabled app")
    digest = controller.wait_request("sendMessage", topic=10, text="yesterday I learned", timeout=12)
    assert "Deployment" in digest["data"]["text"]
    assert "SECRETTOPIC" not in digest["data"]["text"]
    assert not controller.requests("sendMessage", topic=20, text="yesterday I learned")
    controller.stop()
    controller.start()
    poll_mark = len(controller.peer.requests)
    controller.peer.message("/status")
    controller.wait_request("sendMessage", text="Codex status")
    eventually(lambda: len(controller.requests("getUpdates", since=poll_mark)) >= 12,
               description="scheduler polling continued after restart", diagnostics=controller.diagnostics)
    assert len(controller.requests("sendMessage", topic=10, text="yesterday I learned")) == 1
    assert not controller.codex("turn/start")
