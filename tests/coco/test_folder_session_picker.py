"""Tests for folder-change session resume picker flow."""

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import coco.bot as bot
import coco.session as session_mod
from coco.handlers.callback_data import (
    CB_DIR_CONFIRM,
    CB_DIR_SESSION_RESUME,
)
from coco.session import CodexSessionSummary, SessionManager


class _FakeQuery:
    def __init__(self, *, data: str, message) -> None:
        self.data = data
        self.message = message
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False):
        self.answers.append((text, show_alert))


def _make_callback_update(data: str, *, thread_id: int = 77, user_id: int = 1147817421):
    chat = SimpleNamespace(type="supergroup", id=-100123)
    message = SimpleNamespace(message_thread_id=thread_id, chat=chat, chat_id=chat.id)
    query = _FakeQuery(data=data, message=message)
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=chat,
        effective_message=message,
    )
    return update, query


@pytest.fixture
def mgr(monkeypatch) -> SessionManager:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    return SessionManager()


@pytest.mark.asyncio
async def test_folder_confirm_with_prior_codex_sessions_shows_resume_picker(
    monkeypatch, mgr: SessionManager, tmp_path: Path
):
    selected_path = tmp_path / "demo"
    selected_path.mkdir()
    update, query = _make_callback_update(CB_DIR_CONFIRM)
    edits: list[tuple[str, object | None]] = []
    context = SimpleNamespace(
        bot=object(),
        user_data={
            bot.STATE_KEY: bot.STATE_BROWSING_DIRECTORY,
            bot.BROWSE_PATH_KEY: str(selected_path),
            bot.BROWSE_ROOT_KEY: str(tmp_path),
            "_pending_thread_id": 77,
        },
    )

    monkeypatch.setattr(bot, "_is_chat_allowed", lambda _chat: True)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_can_user_create_sessions", lambda _uid: True)
    monkeypatch.setattr(bot, "_get_thread_id", lambda _update: 77)
    monkeypatch.setattr(bot, "session_manager", mgr)

    def _unexpected_allocate_window_id():
        raise AssertionError("should not create a fresh session before user chooses")

    monkeypatch.setattr(mgr, "allocate_virtual_window_id", _unexpected_allocate_window_id)
    monkeypatch.setattr(
        mgr,
        "list_codex_session_summaries_for_cwd",
        lambda cwd, *, limit=100: [
            CodexSessionSummary(
                thread_id="thread-1",
                file_path=selected_path / "session-1.jsonl",
                created_at=datetime(
                    2026, 3, 1, 8, 15, tzinfo=timezone.utc
                ).timestamp(),
                last_active_at=datetime(
                    2026, 3, 6, 18, 45, tzinfo=timezone.utc
                ).timestamp(),
            )
        ],
    )

    async def _safe_edit(_query, text: str, **kwargs):
        edits.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(bot, "safe_edit", _safe_edit)

    await bot.callback_handler(update, context)

    assert edits
    text, markup = edits[-1]
    assert "Past Codex sessions for this folder" in text
    assert "Created:" in text
    assert "Last active:" in text
    assert markup is not None
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert "Start Fresh" in labels
    assert any(label.startswith("Resume ") for label in labels)
    picker = context.user_data.get(bot.DIR_SESSION_PICKER_KEY)
    assert isinstance(picker, dict)
    assert picker.get("selected_path") == str(selected_path)
    assert query.answers[-1] == ("Choose a previous session or start fresh.", False)


@pytest.mark.asyncio
async def test_folder_session_resume_callback_binds_selected_thread(
    monkeypatch, mgr: SessionManager, tmp_path: Path
):
    selected_path = tmp_path / "demo"
    selected_path.mkdir()
    sessions_root = tmp_path / "sessions"
    sessions_dir = sessions_root / "2026" / "03"
    sessions_dir.mkdir(parents=True)
    transcript = sessions_dir / "session-1.jsonl"
    transcript.write_text(
        "\n".join(
            [
                '{"type":"session_meta","timestamp":"2026-03-01T08:15:00Z","payload":{"id":"thread-1","cwd":"'
                + str(selected_path.resolve())
                + '"}}',
                '{"type":"turn_context","timestamp":"2026-03-06T18:45:00Z","payload":{"turn_id":"turn-1","model":"gpt-5.4","effort":"high"}}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    update, query = _make_callback_update(f"{CB_DIR_SESSION_RESUME}0")
    edits: list[tuple[str, object | None]] = []
    renamed_topics: list[tuple[int, int, str]] = []
    context = SimpleNamespace(
        bot=SimpleNamespace(),
        user_data={
            "_pending_thread_id": 77,
            bot.DIR_SESSION_PICKER_KEY: {
                "thread_id": 77,
                "chat_id": -100123,
                "selected_path": str(selected_path),
                "root_path": str(tmp_path),
                "items": [
                    {
                        "thread_id": "thread-1",
                        "created_at": datetime(
                            2026, 3, 1, 8, 15, tzinfo=timezone.utc
                        ).timestamp(),
                        "last_active_at": datetime(
                            2026, 3, 6, 18, 45, tzinfo=timezone.utc
                        ).timestamp(),
                    }
                ],
                "page": 0,
            },
        },
    )

    async def _edit_forum_topic(*, chat_id: int, message_thread_id: int, name: str):
        renamed_topics.append((chat_id, message_thread_id, name))

    context.bot.edit_forum_topic = _edit_forum_topic

    monkeypatch.setattr(bot, "_is_chat_allowed", lambda _chat: True)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_can_user_create_sessions", lambda _uid: True)
    monkeypatch.setattr(bot, "_get_thread_id", lambda _update: 77)
    monkeypatch.setattr(bot, "session_manager", mgr)
    monkeypatch.setattr(mgr, "allocate_virtual_window_id", lambda: "@1")
    monkeypatch.setattr(session_mod.config, "session_provider", "codex")
    monkeypatch.setattr(session_mod.config, "sessions_path", sessions_root)

    mgr.bind_topic_to_codex_thread(
        user_id=1147817421,
        thread_id=77,
        chat_id=-100123,
        codex_thread_id="thread-old",
        window_id="@old",
        cwd=str(selected_path),
        display_name="demo",
    )
    mgr.set_topic_model_selection(
        1147817421,
        77,
        chat_id=-100123,
        model_slug="gpt-5.3-codex",
        reasoning_effort="xhigh",
    )

    async def _thread_resume(*, thread_id: str):
        assert thread_id == "thread-1"
        return {"thread": {"id": "thread-1"}}

    monkeypatch.setattr(bot.codex_app_server_client, "thread_resume", _thread_resume)

    async def _safe_edit(_query, text: str, **kwargs):
        edits.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(bot, "safe_edit", _safe_edit)

    await bot.callback_handler(update, context)

    binding = mgr.resolve_topic_binding(1147817421, 77, chat_id=-100123)
    assert binding is not None
    assert binding.codex_thread_id == "thread-1"
    assert binding.cwd == str(selected_path)
    assert binding.display_name == "demo"
    assert binding.model_slug == "gpt-5.4"
    assert binding.reasoning_effort == "high"
    assert renamed_topics == [(-100123, 77, "demo")]
    assert edits
    assert "Resumed app-server session" in edits[-1][0]
    assert "Model inherited from resumed session" in edits[-1][0]
    assert query.answers[-1] == ("Resumed", False)
