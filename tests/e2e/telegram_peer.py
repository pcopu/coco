"""A strict Telegram Bot API peer; HTTP requests are the test observations.

Updates enter through getUpdates, including callbacks and file metadata. Responses
are protocol fixtures, never imported CoCo helpers. Unknown methods fail loudly.
"""

from __future__ import annotations

from collections import deque
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
from urllib.parse import parse_qs, unquote


BOT = {"id": 999000, "is_bot": True, "first_name": "E2E", "username": "coco_e2e_bot"}
CHAT = -1001234000
ADMIN = 12345
MEMBER = 23456
TOKEN = "999000:E2E-ONLY-NOT-A-REAL-TOKEN"


def chat(chat_id: int) -> dict:
    if chat_id > 0:
        return {"id": chat_id, "type": "private", "first_name": "Test"}
    return {"id": chat_id, "type": "supergroup", "title": "E2E", "is_forum": True}


class TelegramPeer:
    def __init__(self, root: Path):
        self.root = root
        self.requests: list[dict] = []
        self.updates: list[dict] = []
        self.messages: dict[int, dict] = {}
        self.files: dict[str, bytes] = {}
        self.faults: dict[str, deque[dict]] = {}
        self.unknown: list[str] = []
        self.condition = threading.Condition()
        self.sequence = 0
        self.message_sequence = 1000
        peer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                prefix = f"/file/bot{TOKEN}/"
                path = unquote(self.path)
                if not path.startswith(prefix):
                    self.send_error(404)
                    return
                file_id = path[len(prefix):]
                payload = peer.files.get(file_id)
                if payload is None:
                    self.send_error(404)
                    return
                peer.record("downloadFile", {"file_id": file_id})
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self):
                prefix = f"/bot{TOKEN}/"
                if not self.path.startswith(prefix):
                    self.send_error(403)
                    return
                method = self.path[len(prefix):]
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                content_type = self.headers.get("Content-Type", "")
                if content_type.startswith("application/json"):
                    data = json.loads(raw)
                elif content_type.startswith("multipart/form-data"):
                    mime = BytesParser(policy=default).parsebytes(
                        b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + raw
                    )
                    data = {}
                    for part in mime.iter_parts():
                        name = part.get_param("name", header="content-disposition")
                        value = part.get_payload(decode=True)
                        if part.get_filename():
                            data[name] = {"filename": part.get_filename(), "size": len(value),
                                          "bytes_hex": value.hex()}
                        else:
                            data[name] = value.decode()
                else:
                    data = {key: values[0] for key, values in parse_qs(raw.decode()).items()}
                for key, value in list(data.items()):
                    if isinstance(value, str):
                        try:
                            data[key] = json.loads(value)
                        except (ValueError, TypeError):
                            pass
                response = peer.dispatch(method, data)
                encoded = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                try:
                    self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self.server.server_port

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        assert not self.thread.is_alive(), "Telegram emulator thread leaked"

    def record(self, method: str, data: dict) -> dict:
        with self.condition:
            entry = {"method": method, "data": data, "time": time.monotonic()}
            self.requests.append(entry)
            with (self.root / "telegram.jsonl").open("a") as stream:
                stream.write(json.dumps(entry) + "\n")
            self.condition.notify_all()
            return entry

    def fail_next(self, method: str, *, code=400, description="Bad Request: injected", match=None, **params):
        self.faults.setdefault(method, deque()).append(
            {"match": match or {}, "response": {"ok": False, "error_code": code, "description": description, "parameters": params}}
        )

    def dispatch(self, method: str, data: dict) -> dict:
        entry = self.record(method, data)
        response = self.respond(method, data)
        entry["ok"] = response["ok"]
        with (self.root / "telegram-responses.jsonl").open("a") as stream:
            stream.write(json.dumps({"method": method, "response": response}) + "\n")
        return response

    def respond(self, method: str, data: dict) -> dict:
        faults = self.faults.get(method)
        if faults and all(str(value) in str(data.get(key, "")) for key, value in faults[0]["match"].items()):
            return faults.popleft()["response"]
        result: object = True
        if method == "getMe":
            result = BOT
        elif method == "getUpdates":
            offset = int(data.get("offset", 0))
            with self.condition:
                self.updates = [u for u in self.updates if u["update_id"] >= offset]
                if not self.updates:
                    self.condition.wait(0.15)
                result = list(self.updates)
        elif method == "getChat":
            result = chat(int(data["chat_id"]))
        elif method in {"getChatMember", "getChatAdministrators"}:
            def member(uid):
                return {"user": {"id": uid, "is_bot": False, "first_name": "Test"},
                        "status": "creator" if uid == ADMIN else "member",
                        "is_anonymous": False}
            result = [member(ADMIN)] if method.endswith("Administrators") else member(int(data["user_id"]))
        elif method in {"sendMessage", "sendPhoto", "sendVideo", "sendAnimation", "sendDocument", "sendAudio", "sendVoice"}:
            self.message_sequence += 1
            result = self.make_message(data, self.message_sequence)
            self.messages[self.message_sequence] = result
        elif method == "sendMediaGroup":
            result = []
            for _media in data["media"]:
                self.message_sequence += 1
                result.append(self.make_message(data, self.message_sequence))
        elif method in {"editMessageText", "editMessageCaption", "editMessageReplyMarkup"}:
            mid = int(data["message_id"])
            result = {**self.messages.get(mid, {}), **self.make_message(data, mid)}
            self.messages[mid] = result
        elif method == "getFile":
            file_id = str(data["file_id"])
            result = {"file_id": file_id, "file_unique_id": file_id, "file_path": file_id,
                      "file_size": len(self.files.get(file_id, b""))}
        elif method == "createForumTopic":
            self.message_sequence += 1
            result = {"message_thread_id": self.message_sequence, "name": data["name"], "icon_color": 7322096}
        elif method not in {
            "deleteWebhook", "deleteMyCommands", "setMyCommands", "answerCallbackQuery",
            "deleteMessage", "deleteMessages", "setMessageReaction", "sendChatAction",
            "editForumTopic", "closeForumTopic", "reopenForumTopic", "deleteForumTopic",
            "editGeneralForumTopic", "unpinAllForumTopicMessages", "pinChatMessage", "unpinChatMessage",
        }:
            self.unknown.append(method)
            return {"ok": False, "error_code": 400, "description": f"Unimplemented E2E API: {method}"}
        return {"ok": True, "result": result}

    @staticmethod
    def make_message(data: dict, mid: int) -> dict:
        result = {"message_id": mid, "date": int(time.time()), "from": BOT,
                  "chat": chat(int(data.get("chat_id", CHAT)))}
        for field in ("text", "caption", "message_thread_id", "reply_markup"):
            if field in data:
                result[field] = data[field]
        if result.get("message_thread_id"):
            result["is_topic_message"] = True
        return result

    def push(self, payload: dict) -> int:
        with self.condition:
            self.sequence += 1
            self.updates.append({"update_id": self.sequence, **payload})
            self.condition.notify_all()
            return self.sequence

    def message(self, text: str = "", *, user=ADMIN, topic=10, chat_id=CHAT, **fields) -> int:
        self.message_sequence += 1
        payload = {"message_id": self.message_sequence, "date": int(time.time()),
                   "chat": chat(chat_id), "from": {"id": user, "is_bot": False, "first_name": f"User{user}"}}
        if text:
            payload["text"] = text
            if text.startswith("/"):
                payload["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        if topic is not None:
            payload.update(message_thread_id=topic, is_topic_message=True)
        payload.update(fields)
        return self.push({"message": payload})

    def callback(self, data: str, message: dict, *, user=ADMIN) -> int:
        return self.push({"callback_query": {"id": f"cb-{self.sequence + 1}", "data": data,
                          "chat_instance": "e2e", "from": {"id": user, "is_bot": False, "first_name": "Test"},
                          "message": message}})
