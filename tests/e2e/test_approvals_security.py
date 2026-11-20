"""Approval and callback authority exercised through Telegram and JSON-RPC."""

from .harness import eventually
from .telegram_peer import ADMIN, MEMBER


def server_response(controller, request_id):
    return eventually(lambda: next((row["payload"] for row in controller.codex() if
                      row["payload"].get("id") == request_id and "method" not in row["payload"]), None),
                      description=f"response to server request {request_id}", diagnostics=controller.diagnostics)


def test_approval_rejects_nonadmin_then_accepts_once_for_admin(controller):
    binding = controller.bind()
    controller.emit("item/commandExecution/requestApproval", {
        "threadId": binding["codex_thread_id"], "turnId": "approval-turn", "itemId": "approval-item",
        "command": "printf approved", "cwd": str(controller.root / "workspace"), "reason": "E2E approval",
    }, request_id="approve-1")
    mark = len(controller.peer.requests)
    button, message = controller.button("Accept", topic=10, user=MEMBER)
    controller.wait_request("answerCallbackQuery", since=mark, text="admin")
    assert not [r for r in controller.codex() if r["payload"].get("id") == "approve-1"]
    controller.peer.callback(button["callback_data"], message, user=ADMIN)
    response = server_response(controller, "approve-1")
    assert response["result"] == {"decision": "accept"}
    mark = len(controller.peer.requests)
    controller.peer.callback(button["callback_data"], message, user=ADMIN)
    controller.wait_request("answerCallbackQuery", since=mark, text="already resolved")
    assert len([r for r in controller.codex() if r["payload"].get("id") == "approve-1"]) == 1


def test_approval_without_admin_declines_and_structured_input_reports_defaults(controller):
    binding = controller.bind(topic=20, user=MEMBER)
    controller.emit("item/fileChange/requestApproval", {"threadId": binding["codex_thread_id"],
                    "reason": "Modify file"}, request_id="decline-1")
    assert server_response(controller, "decline-1")["result"] == {"decision": "decline"}
    controller.wait_request("sendMessage", topic=20, text="no admin")
    controller.emit("item/tool/requestUserInput", {"threadId": binding["codex_thread_id"], "questions": [
        {"id": "color", "question": "Which color?", "options": [{"label": "Blue"}, {"label": "Red"}]},
        {"id": "notes", "question": "Any notes?", "options": []},
    ]}, request_id="input-1")
    answer = server_response(controller, "input-1")
    assert answer["result"] == {"answers": {"color": {"answers": ["Blue"]}, "notes": {"answers": []}}}
    controller.wait_request("sendMessage", topic=20, text="auto-selected default")


def test_stale_folder_callback_from_other_topic_cannot_bind(controller):
    mark = len(controller.peer.requests)
    controller.peer.message("/start", topic=10)
    controller.wait_request("sendMessage", since=mark, topic=10, text="Working Directory")
    menu = next(m for m in reversed(list(controller.peer.messages.values())) if m.get("message_thread_id") == 10)
    forged = {**menu, "message_thread_id": 20}
    controller.peer.callback("db:confirm", forged)
    controller.wait_request("answerCallbackQuery", text="topic mismatch")
    assert not controller.codex("thread/start")
    assert controller.binding(topic=10) is None and controller.binding(topic=20) is None
    controller.bind(topic=10)
    controller.peer.message("valid after stale browser", topic=10)
    controller.wait_request("sendMessage", topic=10, text="E2E answer")


def test_nonadmin_cannot_trigger_updater_or_allowlist_mutation(controller):
    controller.bind(topic=20, user=MEMBER)
    mark = len(controller.peer.requests)
    controller.peer.message("/update run coco", topic=20, user=MEMBER)
    controller.wait_request("sendMessage", since=mark, text="admin")
    before = (controller.root / "coco/allowed_users_meta.json").read_bytes()
    controller.peer.message("/allowed add 77777", topic=20, user=MEMBER)
    controller.wait_request("sendMessage", since=mark, text="Only admins")
    assert (controller.root / "coco/allowed_users_meta.json").read_bytes() == before
    assert not controller.codex("turn/start")
