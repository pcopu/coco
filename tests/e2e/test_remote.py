"""Two real CoCo processes communicate over their authenticated loopback RPC."""

import pytest

from .harness import eventually


@pytest.fixture
def cluster(controller_factory):
    controller = controller_factory(start=False)
    agent = controller_factory(start=False)
    ports = f"{controller.peer.port},{controller.rpc_port},{agent.rpc_port},{agent.peer.port}"
    controller.env["COCO_E2E_PORTS"] = agent.env["COCO_E2E_PORTS"] = ports
    controller.env["COCO_NODE_OFFLINE_TIMEOUT"] = "1"
    agent.env.update(COCO_MACHINE_ID="e2e-agent", COCO_MACHINE_NAME="E2E Agent",
                     COCO_CONTROLLER_RPC_HOST="127.0.0.1", COCO_CONTROLLER_RPC_PORT=str(controller.rpc_port),
                     COCO_NODE_HEARTBEAT_INTERVAL="1")
    controller.start()
    agent.start_agent()
    eventually(lambda: "e2e-agent" in (controller.root / "coco/nodes.json").read_text(),
               description="agent heartbeat registered", diagnostics=agent.diagnostics)
    return controller, agent


def bind_remote(controller, *, topic=10):
    mark = len(controller.peer.requests)
    controller.peer.message("/start", topic=topic)
    controller.button("E2E Agent", since=mark, topic=topic)
    controller.button("Select", since=mark, topic=topic)
    controller.wait_request("editMessageText", since=mark, text="Bound to this topic")
    return controller.binding(topic=topic)


def test_remote_heartbeat_folder_bind_turn_and_reply_cross_both_processes(cluster):
    controller, agent = cluster
    binding = bind_remote(controller)
    assert binding["machine_id"] == "e2e-agent"
    controller.peer.message("run this on the remote machine")
    turn = agent.wait_codex("turn/start")
    assert turn["params"]["threadId"] == binding["codex_thread_id"]
    controller.wait_request("sendMessage", topic=10, text="E2E answer")
    assert not controller.codex("turn/start")
    assert not agent.peer.requests, "Agent must not use Telegram"
    controller.peer.message("/goal remote objective")
    controller.wait_request("sendMessage", topic=10, text="remote objective")
    assert agent.wait_codex("thread/goal/set")["params"]["objective"] == "remote objective"


def test_remote_ultrafast_topic_setting_reaches_agent_turn(cluster):
    controller, agent = cluster
    binding = bind_remote(controller)
    controller.peer.message("/fast ultrafast")
    controller.wait_request("sendMessage", text="Fast mode is now")
    assert controller.binding(topic=10)["service_tier"] == "ultrafast"

    controller.peer.message("run ultrafast on the remote machine")
    turn = agent.wait_codex("turn/start")
    controller.wait_request("sendMessage", topic=10, text="E2E answer")
    assert turn["params"]["threadId"] == binding["codex_thread_id"]
    assert turn["params"]["serviceTier"] == "ultrafast"


def test_cluster_rejects_bad_auth_malformed_frames_and_stale_transport(cluster):
    controller, agent = cluster
    binding = bind_remote(controller)
    assert agent.rpc("agent/ping", secret="wrong")["error"] == "unauthorized"
    assert agent.rpc("unused", raw=b"not json\n")["error"] == "invalid_json"
    assert agent.rpc("unused", raw=b"[]\n")["error"] == "invalid_request"
    assert agent.rpc("agent/unknown")["error"] == "unknown_method"
    bad = agent.rpc("agent/send_inputs", {"window_id": binding["window_id"],
                    "thread_id": binding["codex_thread_id"], "cwd": binding["cwd"],
                    "inputs": [{"type": "text", "text": "STALE MUTATION"}],
                    "expected_transport_epoch": "stale-epoch", "expected_transport_epoch_started_at": 1})
    assert bad["ok"] is False
    assert not agent.codex("turn/start")
    controller.peer.message("valid after rejected frames")
    controller.wait_request("sendMessage", text="E2E answer")
    assert len(agent.codex("turn/start")) == 1


@pytest.mark.parametrize("kind", ["document", "photo", "video"])
def test_remote_attachments_are_read_on_agent_and_local_ingress_is_rejected(cluster, kind):
    controller, agent = cluster
    bind_remote(controller)
    (agent.root / "workspace/remote.txt").write_text("remote artifact bytes")
    agent.rules({"method": "turn/start", "reply": '<telegram-attachment path="remote.txt" />'})
    controller.peer.message("send remote artifact")
    sent = controller.wait_request("sendDocument", topic=10)
    assert bytes.fromhex(sent["data"]["document"]["bytes_hex"]) == b"remote artifact bytes"
    controller.peer.files["local"] = b"local file"
    media = {"file_id": "local", "file_unique_id": "local", "file_name": "local.txt",
             "mime_type": "text/plain", "file_size": 10, "width": 1, "height": 1, "duration": 1}
    controller.peer.message(**{kind: [media] if kind == "photo" else media})
    controller.wait_request("sendMessage", text="local topic", timeout=3)
    assert not controller.requests("getFile")
    assert len(agent.codex("turn/start")) == 1


def test_agent_offline_and_recovery_are_observable_without_replaying_work(cluster):
    controller, agent = cluster
    bind_remote(controller)
    controller.peer.message("finish before disconnect")
    controller.wait_request("sendMessage", text="E2E answer")
    mark = len(controller.peer.requests)
    agent.stop()
    controller.wait_request("sendMessage", since=mark, text="offline", timeout=12)
    agent.start_agent()
    controller.wait_request("sendMessage", since=mark, text="online", timeout=12)
    assert len(agent.codex("turn/start")) == 1
    controller.peer.message("work during replacement confirmation")
    controller.wait_request("sendMessage", since=mark, text="not dispatched")
    assert len(agent.codex("turn/start")) == 1
    eventually(lambda: "remote_agent_transport_epoch_changed" in controller.diagnostics(), timeout=10,
               description="replacement epoch confirmed by subsequent heartbeat", diagnostics=controller.diagnostics)
    controller.peer.message("explicit work after recovery")
    eventually(lambda: len(agent.codex("turn/start")) == 2, diagnostics=controller.diagnostics)
