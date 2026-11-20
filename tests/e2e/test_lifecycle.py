"""User-visible session lifecycle and persisted topic settings."""

import json

from .harness import eventually
from .telegram_peer import ADMIN, MEMBER


def test_restart_command_reexecs_isolated_controller_and_resumes_binding(controller):
    binding = controller.bind()
    pid = controller.proc.pid
    mark = len(controller.peer.requests)
    controller.peer.message("/restart")
    controller.wait_codex("initialize", count=2, timeout=12)
    eventually(lambda: controller.requests("getUpdates", since=mark), diagnostics=controller.diagnostics)
    assert controller.proc.pid == pid and controller.proc.poll() is None
    controller.peer.message("continue after command restart")
    turn = controller.wait_codex("turn/start")
    controller.wait_request("sendMessage", since=mark, text="E2E answer")
    assert turn["params"]["threadId"] == binding["codex_thread_id"]
    assert len(controller.codex("thread/start")) == 1


def test_topic_settings_and_binding_survive_controller_restart(controller):
    original = controller.bind()
    controller.peer.message("/fast on")
    controller.wait_request("sendMessage", text="Fast mode is now")
    controller.peer.message("/voice on")
    controller.wait_request("sendMessage", text="voice")
    # Avoid invoking the optional speech service when testing persisted settings.
    controller.peer.message("/voice off")
    controller.wait_request("sendMessage", text="OFF")
    controller.peer.message("/goal ship the tested behavior")
    controller.wait_request("sendMessage", text="ship the tested behavior")
    assert controller.wait_codex("thread/goal/set")["params"]["threadId"] == original["codex_thread_id"]
    controller.stop()
    mark = len(controller.peer.requests)
    controller.start()
    controller.peer.message("continue after restart")
    turn = controller.wait_codex("turn/start")
    controller.wait_request("sendMessage", since=mark, topic=10, text="E2E answer")
    assert turn["params"]["threadId"] == original["codex_thread_id"]
    assert turn["params"]["serviceTier"] == "fast"
    assert len(controller.codex("thread/start")) == 1
    controller.peer.message("/goal")
    controller.wait_request("sendMessage", since=mark, topic=10, text="ship the tested behavior")


def test_ultrafast_topic_setting_survives_restart_and_reaches_turn(controller):
    original = controller.bind()
    controller.peer.message("/fast ultrafast")
    controller.wait_request("sendMessage", text="Fast mode is now")
    assert controller.binding(topic=10)["service_tier"] == "ultrafast"

    controller.peer.message("use ultrafast locally")
    first = controller.wait_codex("turn/start")
    controller.wait_request("sendMessage", text="E2E answer")
    assert first["params"]["threadId"] == original["codex_thread_id"]
    assert first["params"]["serviceTier"] == "ultrafast"

    controller.stop()
    controller.start()
    controller.peer.message("use ultrafast after restart")
    second = controller.wait_codex("turn/start", count=2)
    controller.wait_request("sendMessage", text="E2E answer")
    assert second["params"]["threadId"] == original["codex_thread_id"]
    assert second["params"]["serviceTier"] == "ultrafast"


def test_fork_rollback_and_unbind_change_only_target_topic(controller):
    first = controller.bind(topic=10)
    second = controller.bind(topic=20)
    controller.peer.message("/resume", topic=10)
    controller.button("Fork", topic=10)
    controller.wait_codex("thread/fork")
    controller.wait_request("answerCallbackQuery", text="Forked")
    fork = controller.binding(topic=10)
    assert fork["codex_thread_id"] != first["codex_thread_id"]
    assert controller.binding(topic=20)["codex_thread_id"] == second["codex_thread_id"]
    controller.peer.message("message after fork", topic=10)
    assert controller.wait_codex("turn/start")["params"]["threadId"] == fork["codex_thread_id"]
    controller.wait_request("sendMessage", topic=10, text="E2E answer")
    mark = len(controller.peer.requests)
    controller.peer.message("/resume", topic=10)
    controller.button("Rollback", since=mark, topic=10)
    rollback = controller.wait_codex("thread/rollback")
    assert rollback["params"]["threadId"] == fork["codex_thread_id"]
    assert rollback["params"]["numTurns"] == 1
    controller.wait_request("answerCallbackQuery", text="Rolled back")
    controller.peer.message("/unbind", topic=10)
    controller.wait_request("sendMessage", text="unbound")
    assert controller.binding(topic=10) is None
    assert controller.binding(topic=20)["codex_thread_id"] == second["codex_thread_id"]
    assert not controller.codex("thread/archive"), "Unbind must preserve the Codex session"


def test_general_is_reserved_and_shared_without_aliasing_named_topics(controller):
    named = controller.bind(topic=10)
    mark = len(controller.peer.requests)
    for command in ("/start", "/resume", "/unbind"):
        controller.peer.message(command, topic=1)
    eventually(lambda: len(controller.requests("sendMessage", since=mark, text="permanent control")) == 3,
               diagnostics=controller.diagnostics)
    assert len(controller.codex("thread/start")) == 1
    assert controller.binding(topic=10) == named
    controller.peer.message("hello General", topic=1)
    turn = controller.wait_codex("turn/start")
    controller.wait_request("sendMessage", topic=1, text="E2E answer")
    assert turn["params"]["threadId"] != named["codex_thread_id"]
    mark = len(controller.peer.requests)
    controller.peer.message("same General as second user", topic=1, user=MEMBER)
    controller.wait_request("sendMessage", since=mark, text="owner or an admin")
    assert len(controller.codex("turn/start")) == 1
    assert controller.binding(topic=1, user=MEMBER) is None
    # Provision a second admin through the external auth configuration boundary.
    metadata = controller.root / "coco/allowed_users_meta.json"
    values = json.loads(metadata.read_text())
    values["admins"].append(MEMBER)
    metadata.write_text(json.dumps(values))
    controller.peer.message("same General as second admin", topic=1, user=MEMBER)
    second = controller.wait_codex("turn/start", count=2)
    assert second["params"]["threadId"] == turn["params"]["threadId"]
    assert controller.binding(topic=1, user=MEMBER) is None


def test_two_users_and_topics_keep_turn_settings_and_replies_separate(controller):
    a = controller.bind(topic=10, user=ADMIN)
    b = controller.bind(topic=20, user=MEMBER)
    controller.peer.message("/fast on", topic=10)
    controller.wait_request("sendMessage", topic=10, text="Fast mode is now")
    controller.rules({"method": "turn/start", "contains": "first user", "reply": "alpha answer"},
                     {"method": "turn/start", "contains": "second user", "reply": "beta answer"})
    mark = len(controller.peer.requests)
    controller.peer.message("first user", topic=10, user=ADMIN)
    controller.peer.message("second user", topic=20, user=MEMBER)
    alpha = controller.wait_request("sendMessage", since=mark, topic=10, text="alpha answer")
    beta = controller.wait_request("sendMessage", since=mark, topic=20, text="beta answer")
    turns = [r["payload"]["params"] for r in controller.codex("turn/start")]
    assert {t["threadId"] for t in turns} == {a["codex_thread_id"], b["codex_thread_id"]}
    assert next(t for t in turns if t["threadId"] == a["codex_thread_id"])["serviceTier"] == "fast"
    assert next(t for t in turns if t["threadId"] == b["codex_thread_id"])["serviceTier"] == "default"
    assert "beta" not in json.dumps(alpha) and "alpha" not in json.dumps(beta)
