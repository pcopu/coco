"""Tests for SessionManager pure dict operations."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import coco.session as session_mod
from coco.node_registry import NodeRegistry
from coco.session import (
    CodexSessionSummary,
    TOPIC_SYNC_MODE_HOST_FOLLOW_FINAL,
    TOPIC_SYNC_MODE_TELEGRAM_LIVE,
    SessionManager,
)


@pytest.fixture
def mgr(monkeypatch) -> SessionManager:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    return SessionManager()


class TestThreadBindings:
    def test_bind_and_get(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")
        assert mgr.get_window_for_thread(100, 1) == "@1"

    def test_bind_unbind_get_returns_none(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")
        mgr.unbind_thread(100, 1)
        assert mgr.get_window_for_thread(100, 1) is None

    def test_unbind_nonexistent_returns_none(self, mgr: SessionManager) -> None:
        assert mgr.unbind_thread(100, 999) is None

    def test_iter_topic_window_bindings(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")
        mgr.bind_thread(100, 2, "@2")
        mgr.bind_thread(200, 3, "@3")
        result = set(mgr.iter_topic_window_bindings())
        assert result == {
            (100, None, 1, "@1"),
            (100, None, 2, "@2"),
            (200, None, 3, "@3"),
        }

    def test_get_window_for_thread_handles_binding_without_window_id(
        self, mgr: SessionManager
    ) -> None:
        mgr.resolve_topic_binding = (  # type: ignore[method-assign]
            lambda _user_id, _thread_id, **_kwargs: SimpleNamespace(
                codex_thread_id="thread-1",
                cwd="/tmp/demo",
            )
        )
        assert mgr.get_window_for_thread(100, 1) is None


class TestTopicBindingsV2:
    def test_load_state_backfills_local_machine_into_legacy_topic_binding(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        state_file = tmp_path / "state.json"
        state_file.write_text(
            json.dumps(
                {
                    "state_schema_version": 5,
                    "window_states": {
                        "@1": {
                            "session_id": "session-1",
                            "cwd": str(tmp_path / "workspace"),
                            "window_name": "proj",
                        }
                    },
                    "user_window_offsets": {},
                    "topic_bindings_v2": {
                        "100": {
                            "1": {
                                "transport": "window",
                                "thread_id": 1,
                                "window_id": "@1",
                                "cwd": str(tmp_path / "workspace"),
                                "display_name": "proj",
                            }
                        }
                    },
                    "window_display_names": {"@1": "proj"},
                    "group_chat_ids": {},
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(session_mod.config, "state_file", state_file)
        monkeypatch.setattr(session_mod.config, "machine_id", "local-node")
        monkeypatch.setattr(session_mod.config, "machine_name", "Local Node")
        monkeypatch.setattr(
            session_mod,
            "node_registry",
            NodeRegistry(state_file=tmp_path / "nodes.json"),
        )
        session_mod.node_registry.ensure_local_node(
            machine_id="local-node",
            display_name="Local Node",
            transport="local",
            is_local=True,
        )

        loaded = SessionManager()

        binding = loaded.resolve_topic_binding(100, 1)
        assert binding is not None
        assert binding.machine_id == "local-node"
        assert binding.machine_display_name == "Local Node"

    def test_bind_thread_populates_topic_binding(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="proj")
        binding = mgr.resolve_topic_binding(100, 1)
        assert binding is not None
        assert binding.transport == "window"
        assert binding.window_id == "@1"
        assert binding.display_name == "proj"

    def test_bind_topic_to_codex_thread_resolves_target(self, mgr: SessionManager) -> None:
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=1,
            codex_thread_id="thread-1",
            window_id="@1",
            cwd="/tmp/proj",
            display_name="proj",
        )
        assert mgr.resolve_topic_target(100, 1) == ("codex_thread", "thread-1")
        assert mgr.get_window_for_thread(100, 1) == "@1"
        assert mgr.find_users_for_codex_thread("thread-1") == [(100, None, "@1", 1)]

    def test_find_users_for_codex_thread_with_codex_only_binding(
        self, mgr: SessionManager
    ) -> None:
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=9,
            codex_thread_id="thread-9",
        )
        assert mgr.find_users_for_codex_thread("thread-9") == [
            (100, None, "topic:100:9", 9)
        ]

    def test_unbind_topic_removes_legacy_mapping(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")
        removed = mgr.unbind_topic(100, 1)
        assert removed is not None
        assert removed.window_id == "@1"
        assert mgr.resolve_topic_binding(100, 1) is None
        assert mgr.get_window_for_thread(100, 1) is None

    def test_set_window_codex_thread_id_syncs_topic_binding(
        self, mgr: SessionManager
    ) -> None:
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=1,
            codex_thread_id="thread-old",
            window_id="@1",
            cwd="/tmp/proj",
            display_name="proj",
        )

        mgr.set_window_codex_thread_id("@1", "thread-new")

        binding = mgr.resolve_topic_binding(100, 1)
        assert binding is not None
        assert binding.codex_thread_id == "thread-new"

    def test_topic_model_selection_roundtrip(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="proj")

        changed = mgr.set_topic_model_selection(
            100,
            1,
            model_slug="gpt-5.4",
            reasoning_effort="high",
        )

        binding = mgr.resolve_topic_binding(100, 1)
        assert changed is True
        assert binding is not None
        assert binding.model_slug == "gpt-5.4"
        assert binding.reasoning_effort == "high"
        assert mgr.get_topic_model_selection(100, 1) == ("gpt-5.4", "high")

    def test_topic_service_tier_selection_roundtrip(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="proj")

        changed = mgr.set_topic_service_tier_selection(
            100,
            1,
            service_tier="fast",
        )

        binding = mgr.resolve_topic_binding(100, 1)
        assert changed is True
        assert binding is not None
        assert binding.service_tier == "fast"
        assert mgr.get_topic_service_tier_selection(100, 1) == "fast"

    def test_machine_transcription_profile_selection_roundtrip(
        self, mgr: SessionManager
    ) -> None:
        changed = mgr.set_machine_transcription_profile_selection(
            "local-node",
            transcription_profile="compatible",
        )

        assert changed is True
        assert (
            mgr.get_machine_transcription_profile_selection("local-node")
            == "compatible"
        )

    def test_bind_topic_to_codex_thread_preserves_topic_model_selection(
        self, mgr: SessionManager
    ) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="proj")
        mgr.set_topic_model_selection(
            100,
            1,
            model_slug="gpt-5.4",
            reasoning_effort="high",
        )

        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=1,
            codex_thread_id="thread-1",
            window_id="@1",
            cwd="/tmp/proj",
            display_name="proj",
        )

        binding = mgr.resolve_topic_binding(100, 1)
        assert binding is not None
        assert binding.model_slug == "gpt-5.4"
        assert binding.reasoning_effort == "high"

    def test_topic_sync_mode_defaults_to_telegram_live(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")

        binding = mgr.resolve_topic_binding(100, 1)

        assert binding is not None
        assert binding.sync_mode == TOPIC_SYNC_MODE_TELEGRAM_LIVE
        assert mgr.get_topic_sync_mode(100, 1) == TOPIC_SYNC_MODE_TELEGRAM_LIVE

    def test_set_topic_sync_mode_roundtrip(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")

        changed = mgr.set_topic_sync_mode(
            100,
            1,
            TOPIC_SYNC_MODE_HOST_FOLLOW_FINAL,
        )

        binding = mgr.resolve_topic_binding(100, 1)
        assert changed is True
        assert binding is not None
        assert binding.sync_mode == TOPIC_SYNC_MODE_HOST_FOLLOW_FINAL
        assert mgr.get_topic_sync_mode(100, 1) == TOPIC_SYNC_MODE_HOST_FOLLOW_FINAL


class TestTranscriptEchoState:
    def test_register_and_consume_expected_transcript_echo(
        self, mgr: SessionManager
    ) -> None:
        mgr.register_expected_transcript_user_echo("@1", "hello world")

        assert mgr.consume_expected_transcript_user_echo("@1", "hello world") is True
        assert mgr.consume_expected_transcript_user_echo("@1", "hello world") is False

    def test_window_external_turn_active_roundtrip(self, mgr: SessionManager) -> None:
        assert mgr.is_window_external_turn_active("@1") is False


class TestCodexSessionSummaries:
    def test_list_codex_session_summaries_for_cwd_returns_created_and_last_active(
        self, mgr: SessionManager, monkeypatch, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        sessions_root = tmp_path / "sessions"
        sessions_dir = sessions_root / "2026" / "03"
        sessions_dir.mkdir(parents=True)

        newer_file = sessions_dir / "session-new.jsonl"
        newer_file.write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "timestamp": "2026-03-05T12:00:00Z",
                    "payload": {
                        "id": "thread-new",
                        "cwd": str(workspace.resolve()),
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        newer_last_active = datetime(2026, 3, 6, 13, 30, tzinfo=timezone.utc).timestamp()
        os.utime(newer_file, (newer_last_active, newer_last_active))

        older_file = sessions_dir / "session-old.jsonl"
        older_file.write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "timestamp": "2026-03-01T08:15:00Z",
                    "payload": {
                        "id": "thread-old",
                        "cwd": str(workspace.resolve()),
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        older_last_active = datetime(2026, 3, 2, 9, 45, tzinfo=timezone.utc).timestamp()
        os.utime(older_file, (older_last_active, older_last_active))

        monkeypatch.setattr(session_mod.config, "session_provider", "codex")
        monkeypatch.setattr(session_mod.config, "sessions_path", sessions_root)

        summaries = mgr.list_codex_session_summaries_for_cwd(str(workspace))

        assert summaries == [
            CodexSessionSummary(
                thread_id="thread-new",
                file_path=newer_file,
                created_at=datetime(
                    2026, 3, 5, 12, 0, tzinfo=timezone.utc
                ).timestamp(),
                last_active_at=newer_last_active,
            ),
            CodexSessionSummary(
                thread_id="thread-old",
                file_path=older_file,
                created_at=datetime(
                    2026, 3, 1, 8, 15, tzinfo=timezone.utc
                ).timestamp(),
                last_active_at=older_last_active,
            ),
        ]
        mgr.set_window_external_turn_active("@1", True)
        assert mgr.is_window_external_turn_active("@1") is True
        mgr.set_window_external_turn_active("@1", False)
        assert mgr.is_window_external_turn_active("@1") is False

    def test_get_codex_session_model_selection_for_thread_reads_turn_context(
        self, mgr: SessionManager, monkeypatch, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        sessions_root = tmp_path / "sessions"
        sessions_dir = sessions_root / "2026" / "03"
        sessions_dir.mkdir(parents=True)

        transcript = sessions_dir / "session-1.jsonl"
        transcript.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "session_meta",
                            "timestamp": "2026-03-05T12:00:00Z",
                            "payload": {
                                "id": "thread-1",
                                "cwd": str(workspace.resolve()),
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "turn_context",
                            "timestamp": "2026-03-05T12:05:00Z",
                            "payload": {
                                "turn_id": "turn-1",
                                "model": "gpt-5.4",
                                "effort": "high",
                            },
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(session_mod.config, "session_provider", "codex")
        monkeypatch.setattr(session_mod.config, "sessions_path", sessions_root)

        assert mgr.get_codex_session_model_selection_for_thread(
            "thread-1",
            cwd=str(workspace),
        ) == ("gpt-5.4", "high")


class TestHostFollowTakeover:
    @pytest.mark.asyncio
    async def test_send_topic_text_to_window_resumes_latest_before_telegram_takeover(
        self, mgr: SessionManager, monkeypatch
    ) -> None:
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=1,
            codex_thread_id="thread-old",
            window_id="@1",
            cwd="/tmp/proj",
            display_name="proj",
        )
        mgr.set_topic_sync_mode(100, 1, TOPIC_SYNC_MODE_HOST_FOLLOW_FINAL)
        mgr.set_window_external_turn_active("@1", True)
        mgr.get_window_state("@1").cwd = "/tmp/proj"

        resumed: list[tuple[str, str]] = []
        sent: list[tuple[str, str, bool]] = []

        async def _resume_latest(*, window_id: str, cwd: str) -> str:
            resumed.append((window_id, cwd))
            return "thread-new"

        async def _send_to_window(window_id: str, text: str, *, steer: bool = False):
            sent.append((window_id, text, steer))
            return True, "ok"

        monkeypatch.setattr(
            mgr,
            "resume_latest_codex_session_for_window",
            _resume_latest,
        )
        monkeypatch.setattr(mgr, "send_to_window", _send_to_window)

        ok, msg = await mgr.send_topic_text_to_window(
            user_id=100,
            thread_id=1,
            window_id="@1",
            text="take over from telegram",
        )

        assert ok is True
        assert msg == "ok"
        assert resumed == [("@1", "/tmp/proj")]
        assert sent == [("@1", "take over from telegram", False)]
        assert mgr.get_topic_sync_mode(100, 1) == TOPIC_SYNC_MODE_TELEGRAM_LIVE
        assert mgr.is_window_external_turn_active("@1") is False


@pytest.mark.asyncio
async def test_resume_latest_codex_session_for_window_syncs_topic_model_selection(
    mgr: SessionManager, monkeypatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions_root = tmp_path / "sessions"
    sessions_dir = sessions_root / "2026" / "03"
    sessions_dir.mkdir(parents=True)

    transcript = sessions_dir / "session-1.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "session_meta",
                        "timestamp": "2026-03-05T12:00:00Z",
                        "payload": {
                            "id": "thread-new",
                            "cwd": str(workspace.resolve()),
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "turn_context",
                        "timestamp": "2026-03-05T12:05:00Z",
                        "payload": {
                            "turn_id": "turn-1",
                            "model": "gpt-5.4",
                            "effort": "high",
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    last_active = datetime(2026, 3, 6, 13, 30, tzinfo=timezone.utc).timestamp()
    os.utime(transcript, (last_active, last_active))

    monkeypatch.setattr(session_mod.config, "session_provider", "codex")
    monkeypatch.setattr(session_mod.config, "sessions_path", sessions_root)

    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=1,
        chat_id=-100123,
        codex_thread_id="thread-old",
        window_id="@1",
        cwd=str(workspace),
        display_name="proj",
    )
    mgr.set_topic_model_selection(
        100,
        1,
        chat_id=-100123,
        model_slug="gpt-5.3-codex",
        reasoning_effort="xhigh",
    )

    async def _thread_resume(*, thread_id: str):
        assert thread_id == "thread-new"
        return {"thread": {"id": "thread-new"}}

    monkeypatch.setattr(session_mod.codex_app_server_client, "thread_resume", _thread_resume)

    resumed = await mgr.resume_latest_codex_session_for_window(
        window_id="@1",
        cwd=str(workspace),
    )

    binding = mgr.resolve_topic_binding(100, 1, chat_id=-100123)
    assert resumed == "thread-new"
    assert mgr.consume_window_pending_session_start_reason("@1") == "resume"
    assert binding is not None
    assert binding.codex_thread_id == "thread-new"
    assert binding.model_slug == "gpt-5.4"
    assert binding.reasoning_effort == "high"


class TestRuntimeCapabilityHint:
    def test_runtime_hint_includes_telegram_attachment_protocol(self):
        hint = SessionManager._build_runtime_capability_hint(
            workspace_path="/tmp/demo",
            can_write=True,
            approval_policy="on-request",
        )

        assert "Workspace: /tmp/demo" in hint
        assert "<telegram-attachment path=" in hint
        assert ".pdf" in hint
        assert ".txt" in hint
        assert ".md" in hint
        assert ".png" in hint
        assert ".jpg" in hint
        assert ".jpeg" in hint
        assert ".webp" in hint


class TestGroupChatId:
    """Tests for group chat_id routing (supergroup forum topic support).

    IMPORTANT: These tests protect against regression. The group_chat_ids
    mapping is required for Telegram supergroup forum topics — without it,
    all outbound messages fail with "Message thread not found". This was
    erroneously removed once (26cb81f) and restored in PR #23. Do NOT
    delete these tests or the underlying functionality.
    """

    def test_resolve_with_stored_group_id(self, mgr: SessionManager) -> None:
        """resolve_chat_id returns stored group chat_id for known thread."""
        mgr.set_group_chat_id(100, 1, -1001234567890)
        assert mgr.resolve_chat_id(100, 1) == -1001234567890

    def test_resolve_without_group_id_falls_back_to_user_id(
        self, mgr: SessionManager
    ) -> None:
        """resolve_chat_id falls back to user_id when no group_id stored."""
        assert mgr.resolve_chat_id(100, 1) == 100

    def test_resolve_none_thread_id_falls_back_to_user_id(
        self, mgr: SessionManager
    ) -> None:
        """resolve_chat_id returns user_id when thread_id is None (private chat)."""
        mgr.set_group_chat_id(100, 1, -1001234567890)
        assert mgr.resolve_chat_id(100) == 100

    def test_set_group_chat_id_overwrites(self, mgr: SessionManager) -> None:
        """set_group_chat_id updates the stored value on change."""
        mgr.set_group_chat_id(100, 1, -999)
        mgr.set_group_chat_id(100, 1, -888)
        assert mgr.resolve_chat_id(100, 1) == -888

    def test_multiple_threads_independent(self, mgr: SessionManager) -> None:
        """Different threads for the same user store independent group chat_ids."""
        mgr.set_group_chat_id(100, 1, -111)
        mgr.set_group_chat_id(100, 2, -222)
        assert mgr.resolve_chat_id(100, 1) == -111
        assert mgr.resolve_chat_id(100, 2) == -222

    def test_multiple_users_independent(self, mgr: SessionManager) -> None:
        """Different users store independent group chat_ids."""
        mgr.set_group_chat_id(100, 1, -111)
        mgr.set_group_chat_id(200, 1, -222)
        assert mgr.resolve_chat_id(100, 1) == -111
        assert mgr.resolve_chat_id(200, 1) == -222

    def test_set_group_chat_id_with_none_thread(self, mgr: SessionManager) -> None:
        """set_group_chat_id handles None thread_id (mapped to 0)."""
        mgr.set_group_chat_id(100, None, -999)
        # thread_id=None in resolve falls back to user_id (by design)
        assert mgr.resolve_chat_id(100, None) == 100
        # The stored key is "100:0", only accessible with explicit thread_id=0
        assert mgr.group_chat_ids.get("100:0") == -999


class TestWindowState:
    def test_get_creates_new(self, mgr: SessionManager) -> None:
        state = mgr.get_window_state("@0")
        assert state.session_id == ""
        assert state.cwd == ""

    def test_get_returns_existing(self, mgr: SessionManager) -> None:
        state = mgr.get_window_state("@1")
        state.session_id = "abc"
        assert mgr.get_window_state("@1").session_id == "abc"

    def test_clear_window_session(self, mgr: SessionManager) -> None:
        state = mgr.get_window_state("@1")
        state.session_id = "abc"
        mgr.clear_window_session("@1")
        assert mgr.get_window_state("@1").session_id == ""

    def test_window_approval_mode_roundtrip(self, mgr: SessionManager) -> None:
        assert mgr.get_window_approval_mode("@1") == ""
        mgr.set_window_approval_mode("@1", "on-request")
        assert mgr.get_window_approval_mode("@1") == "on-request"

    def test_clear_window_session_keeps_approval_mode(self, mgr: SessionManager) -> None:
        state = mgr.get_window_state("@1")
        state.session_id = "abc"
        mgr.set_window_approval_mode("@1", "never")
        mgr.clear_window_session("@1")
        assert mgr.get_window_state("@1").session_id == ""
        assert mgr.get_window_approval_mode("@1") == "never"

    def test_default_approval_mode_roundtrip(self, mgr: SessionManager) -> None:
        assert mgr.get_default_approval_mode() == ""
        mgr.set_default_approval_mode("full-auto")
        assert mgr.get_default_approval_mode() == "full-auto"

    def test_window_mention_only_roundtrip(self, mgr: SessionManager) -> None:
        assert mgr.get_window_mention_only("@1") is False
        mgr.set_window_mention_only("@1", True)
        assert mgr.get_window_mention_only("@1") is True

    def test_clear_window_session_keeps_mention_only(self, mgr: SessionManager) -> None:
        state = mgr.get_window_state("@1")
        state.session_id = "abc"
        mgr.set_window_mention_only("@1", True)
        mgr.clear_window_session("@1")
        assert mgr.get_window_state("@1").session_id == ""
        assert mgr.get_window_mention_only("@1") is True


class TestResolveWindowForThread:
    def test_none_thread_id_returns_none(self, mgr: SessionManager) -> None:
        assert mgr.resolve_window_for_thread(100, None) is None

    def test_unbound_thread_returns_none(self, mgr: SessionManager) -> None:
        assert mgr.resolve_window_for_thread(100, 42) is None

    def test_bound_thread_returns_window(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 42, "@3")
        assert mgr.resolve_window_for_thread(100, 42) == "@3"


class TestDisplayNames:
    def test_get_display_name_fallback(self, mgr: SessionManager) -> None:
        """get_display_name returns window_id when no display name is set."""
        assert mgr.get_display_name("@99") == "@99"

    def test_set_and_get_display_name(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="myproject")
        assert mgr.get_display_name("@1") == "myproject"

    def test_set_display_name_update(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="old-name")
        mgr.window_display_names["@1"] = "new-name"
        assert mgr.get_display_name("@1") == "new-name"

    def test_bind_thread_sets_display_name(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="proj")
        assert mgr.get_display_name("@1") == "proj"

    def test_bind_thread_without_name_no_display(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")
        # No display name set, fallback to window_id
        assert mgr.get_display_name("@1") == "@1"


class TestFindUsersForSession:
    @pytest.mark.asyncio
    async def test_uses_in_memory_session_ids(
        self, mgr: SessionManager, monkeypatch
    ) -> None:
        mgr.bind_thread(100, 1, "@1")
        mgr.bind_thread(100, 2, "@2")
        mgr.get_window_state("@1").session_id = "s1"
        mgr.get_window_state("@2").session_id = "s2"

        async def fail_autodiscover(_window_id: str) -> bool:
            raise AssertionError("autodiscover should not be called")

        monkeypatch.setattr(mgr, "autodiscover_session_for_window", fail_autodiscover)

        result = await mgr.find_users_for_session("s1")
        assert result == [(100, None, "@1", 1)]

    @pytest.mark.asyncio
    async def test_autodiscovers_when_session_id_missing(
        self, mgr: SessionManager, monkeypatch
    ) -> None:
        mgr.bind_thread(100, 1, "@1")
        mgr.get_window_state("@1").session_id = ""
        called: list[str] = []

        async def fake_autodiscover(window_id: str) -> bool:
            called.append(window_id)
            mgr.get_window_state(window_id).session_id = "new-session"
            return True

        monkeypatch.setattr(mgr, "autodiscover_session_for_window", fake_autodiscover)

        result = await mgr.find_users_for_session("new-session")
        assert result == [(100, None, "@1", 1)]
        assert called == ["@1"]


class TestIsWindowId:
    def test_valid_ids(self, mgr: SessionManager) -> None:
        assert mgr._is_window_id("@0") is True
        assert mgr._is_window_id("@12") is True
        assert mgr._is_window_id("@999") is True

    def test_invalid_ids(self, mgr: SessionManager) -> None:
        assert mgr._is_window_id("myproject") is False
        assert mgr._is_window_id("@") is False
        assert mgr._is_window_id("") is False
        assert mgr._is_window_id("@abc") is False


def test_load_state_ignores_legacy_thread_bindings_payload(monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "window_states": {
                    "@9": {
                        "cwd": "/tmp/demo",
                        "window_name": "demo",
                        "codex_thread_id": "thread-9",
                    }
                },
                "thread_bindings": {"100": {"7": "@9"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(session_mod.config, "state_file", state_file)
    loaded = SessionManager()

    assert loaded.resolve_topic_binding(100, 7) is None


def test_load_state_preserves_topic_bindings_v2(
    monkeypatch, tmp_path
):
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "state_schema_version": 2,
                "topic_bindings_v2": {
                    "100": {
                        "7": {
                            "transport": "legacy",
                            "window_id": "@9",
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(session_mod.config, "state_file", state_file)
    loaded = SessionManager()

    binding = loaded.resolve_topic_binding(100, 7)
    assert binding is not None
    assert binding.transport == "window"
    assert binding.window_id == "@9"


def test_save_state_omits_legacy_thread_bindings_key(
    monkeypatch,
    tmp_path,
):
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(session_mod.config, "state_file", state_file)
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)

    manager = SessionManager()
    manager.bind_thread(100, 7, "@9", window_name="demo")

    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["state_schema_version"] == session_mod.STATE_SCHEMA_VERSION
    assert "topic_bindings_v2" in saved
    assert "thread_bindings" not in saved


@pytest.mark.asyncio
async def test_resolve_stale_ids_preserves_recoverable_binding_for_lazy_recovery(
    mgr: SessionManager,
    monkeypatch,
):
    monkeypatch.setattr(session_mod.config, "session_provider", "codex")
    monkeypatch.setattr(session_mod.config, "runtime_mode", "hybrid")

    mgr.window_states["@9"] = mgr.get_window_state("@9")
    mgr.window_states["@9"].cwd = "/tmp/project"
    mgr.window_states["@9"].window_name = "project"
    mgr.window_display_names["@9"] = "project"
    mgr.bind_thread(100, 7, "@9", window_name="project")
    mgr.user_window_offsets = {100: {"@9": 42}}

    await mgr.resolve_stale_ids()

    binding = mgr.resolve_topic_binding(100, 7)
    assert binding is not None
    assert binding.window_id == "@9"
    assert "@9" in mgr.window_states
    assert mgr.user_window_offsets == {100: {"@9": 42}}


@pytest.mark.asyncio
async def test_resolve_stale_ids_keeps_window_id_binding_without_legacy_lookup(
    mgr: SessionManager,
    monkeypatch,
):
    monkeypatch.setattr(session_mod.config, "session_provider", "codex")
    monkeypatch.setattr(session_mod.config, "runtime_mode", "hybrid")

    mgr.window_states["@9"] = mgr.get_window_state("@9")
    mgr.window_states["@9"].cwd = ""
    mgr.window_states["@9"].window_name = "project"
    mgr.window_display_names["@9"] = "project"
    mgr.bind_thread(100, 7, "@9", window_name="project")

    await mgr.resolve_stale_ids()

    binding = mgr.resolve_topic_binding(100, 7)
    assert binding is not None
    assert binding.window_id == "@9"
    assert "@9" in mgr.window_states


@pytest.mark.asyncio
async def test_resolve_stale_ids_is_noop_for_window_ids(
    mgr: SessionManager,
    monkeypatch,
):
    monkeypatch.setattr(session_mod.config, "session_provider", "codex")
    monkeypatch.setattr(session_mod.config, "runtime_mode", "app_server_only")
    mgr.bind_thread(100, 7, "@9", window_name="project")

    await mgr.resolve_stale_ids()
    assert mgr.resolve_topic_binding(100, 7) is not None


def test_normalize_approval_policy_maps_agent_and_full_auto_to_never():
    assert SessionManager._normalize_approval_policy("full-auto") == "never"
    assert SessionManager._normalize_approval_policy("agent") == "never"


def test_codex_app_server_mode_enabled_auto_requires_running(mgr: SessionManager, monkeypatch):
    monkeypatch.setattr(session_mod.config, "session_provider", "codex")
    monkeypatch.setattr(session_mod.config, "runtime_mode", "hybrid")
    monkeypatch.setattr(session_mod.config, "codex_transport", "auto")
    assert mgr._codex_app_server_mode_enabled() is True


def test_codex_app_server_mode_enabled_app_server_forces_enabled(
    mgr: SessionManager,
    monkeypatch,
):
    monkeypatch.setattr(session_mod.config, "session_provider", "codex")
    monkeypatch.setattr(session_mod.config, "runtime_mode", "hybrid")
    monkeypatch.setattr(session_mod.config, "codex_transport", "app_server")
    monkeypatch.setattr(session_mod.codex_app_server_client, "is_running", lambda: False)
    assert mgr._codex_app_server_mode_enabled() is True


def test_codex_app_server_mode_enabled_legacy_disables(mgr: SessionManager, monkeypatch):
    monkeypatch.setattr(session_mod.config, "session_provider", "codex")
    monkeypatch.setattr(session_mod.config, "runtime_mode", "hybrid")
    monkeypatch.setattr(session_mod.config, "codex_transport", "legacy")
    assert mgr._codex_app_server_mode_enabled() is True


def test_codex_app_server_mode_enabled_app_server_only_forces_enabled(
    mgr: SessionManager,
    monkeypatch,
):
    monkeypatch.setattr(session_mod.config, "session_provider", "codex")
    monkeypatch.setattr(session_mod.config, "runtime_mode", "app_server_only")
    monkeypatch.setattr(session_mod.config, "codex_transport", "legacy")
    monkeypatch.setattr(session_mod.codex_app_server_client, "is_running", lambda: False)
    assert mgr._codex_app_server_mode_enabled() is True


@pytest.mark.asyncio
async def test_ensure_codex_thread_uses_app_default_when_window_override_missing(
    mgr: SessionManager,
    monkeypatch,
):
    mgr.set_default_approval_mode("full-auto")
    started: list[str | None] = []

    async def _thread_start(
        *,
        cwd: str | None = None,
        approval_policy: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        service_tier: str | None = None,
    ):
        _ = cwd, model, effort, service_tier
        started.append(approval_policy)
        return {"thread": {"id": "thread-1"}}

    monkeypatch.setattr(
        "coco.session.codex_app_server_client.thread_start",
        _thread_start,
    )

    thread_id, policy = await mgr._ensure_codex_thread_for_window(
        window_id="@1",
        cwd="/tmp/demo",
    )

    assert thread_id == "thread-1"
    assert policy == "never"
    assert started == ["never"]


@pytest.mark.asyncio
async def test_ensure_codex_thread_passes_topic_service_tier(
    mgr: SessionManager,
    monkeypatch,
):
    mgr.bind_thread(100, 1, "@1", window_name="proj")
    mgr.set_topic_service_tier_selection(100, 1, service_tier="fast")
    started: list[tuple[str | None, str | None]] = []

    async def _thread_start(
        *,
        cwd: str | None = None,
        approval_policy: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        service_tier: str | None = None,
    ):
        _ = cwd, approval_policy, model, effort
        started.append((approval_policy, service_tier))
        return {"thread": {"id": "thread-1"}}

    monkeypatch.setattr(
        "coco.session.codex_app_server_client.thread_start",
        _thread_start,
    )

    thread_id, _policy = await mgr._ensure_codex_thread_for_window(
        window_id="@1",
        cwd="/tmp/demo",
    )

    assert thread_id == "thread-1"
    assert started == [("on-request", "fast")]


@pytest.mark.asyncio
async def test_send_inputs_via_app_server_marks_fresh_start_for_new_thread(
    mgr: SessionManager,
    monkeypatch,
):
    captured_inputs: list[dict[str, object]] = []

    async def _thread_start(
        *,
        cwd: str | None = None,
        approval_policy: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        service_tier: str | None = None,
    ):
        _ = cwd, approval_policy, model, effort, service_tier
        return {"thread": {"id": "thread-1"}}

    async def _turn_start(
        *,
        thread_id: str,
        inputs: list[dict[str, object]],
        approval_policy: str | None = None,
        service_tier: str | None = None,
        timeout: float = 90.0,
    ):
        _ = thread_id, approval_policy, service_tier, timeout
        captured_inputs.extend(inputs)
        return {"turn": {"id": "turn-1"}}

    monkeypatch.setattr(
        "coco.session.codex_app_server_client.thread_start",
        _thread_start,
    )
    monkeypatch.setattr(
        "coco.session.codex_app_server_client.turn_start",
        _turn_start,
    )
    monkeypatch.setattr(
        "coco.session.codex_app_server_client.get_active_turn_id",
        lambda _thread_id: None,
    )
    monkeypatch.setattr(
        SessionManager,
        "_runtime_write_state",
        staticmethod(lambda _cwd: ("/tmp/demo", True)),
    )

    ok, _msg = await mgr._send_inputs_via_codex_app_server(
        window_id="@1",
        inputs=[{"type": "text", "text": "hello"}],
        steer=False,
        window_name="demo",
        cwd="/tmp/demo",
    )

    assert ok is True
    assert "Session start reason: fresh_start" in str(captured_inputs[0]["text"])


@pytest.mark.asyncio
async def test_send_inputs_via_app_server_prepends_runtime_capability_hint(
    mgr: SessionManager,
    monkeypatch,
):
    captured_inputs: list[dict[str, object]] = []
    mgr.set_window_codex_thread_id("@1", "thread-1")
    mgr.set_window_approval_mode("@1", "never")

    async def _turn_start(
        *,
        thread_id: str,
        inputs: list[dict[str, object]],
        approval_policy: str | None = None,
        service_tier: str | None = None,
        timeout: float = 90.0,
    ):
        _ = thread_id, approval_policy, service_tier, timeout
        captured_inputs.extend(inputs)
        return {"turn": {"id": "turn-1"}}

    monkeypatch.setattr(
        "coco.session.codex_app_server_client.turn_start",
        _turn_start,
    )
    monkeypatch.setattr(
        "coco.session.codex_app_server_client.get_active_turn_id",
        lambda _thread_id: None,
    )
    monkeypatch.setattr(
        SessionManager,
        "_runtime_write_state",
        staticmethod(lambda _cwd: ("/tmp/demo", True)),
    )

    ok, _msg = await mgr._send_inputs_via_codex_app_server(
        window_id="@1",
        inputs=[{"type": "text", "text": "hello"}],
        steer=False,
        window_name="demo",
        cwd="/tmp/demo",
    )

    assert ok is True
    assert len(captured_inputs) == 2
    assert captured_inputs[0]["type"] == "text"
    assert "Filesystem write access: enabled" in str(captured_inputs[0]["text"])
    assert "Approval policy: never" in str(captured_inputs[0]["text"])
    assert captured_inputs[1] == {"type": "text", "text": "hello"}


@pytest.mark.asyncio
async def test_send_inputs_via_app_server_includes_one_shot_session_start_reason(
    mgr: SessionManager,
    monkeypatch,
):
    captured_inputs: list[dict[str, object]] = []
    mgr.set_window_codex_thread_id("@1", "thread-1")
    mgr.set_window_approval_mode("@1", "never")
    mgr.mark_window_pending_session_start_reason("@1", "after_clear")

    async def _turn_start(
        *,
        thread_id: str,
        inputs: list[dict[str, object]],
        approval_policy: str | None = None,
        service_tier: str | None = None,
        timeout: float = 90.0,
    ):
        _ = thread_id, approval_policy, service_tier, timeout
        captured_inputs.extend(inputs)
        return {"turn": {"id": "turn-1"}}

    monkeypatch.setattr(
        "coco.session.codex_app_server_client.turn_start",
        _turn_start,
    )
    monkeypatch.setattr(
        "coco.session.codex_app_server_client.get_active_turn_id",
        lambda _thread_id: None,
    )
    monkeypatch.setattr(
        SessionManager,
        "_runtime_write_state",
        staticmethod(lambda _cwd: ("/tmp/demo", True)),
    )

    ok, _msg = await mgr._send_inputs_via_codex_app_server(
        window_id="@1",
        inputs=[{"type": "text", "text": "hello"}],
        steer=False,
        window_name="demo",
        cwd="/tmp/demo",
    )

    assert ok is True
    assert "Session start reason: after_clear" in str(captured_inputs[0]["text"])
    assert mgr.consume_window_pending_session_start_reason("@1") == ""


def test_clear_window_session_marks_next_turn_as_after_clear(mgr: SessionManager):
    mgr.clear_window_session("@1")
    assert mgr.consume_window_pending_session_start_reason("@1") == "after_clear"


@pytest.mark.asyncio
async def test_send_inputs_via_app_server_passes_topic_service_tier(
    mgr: SessionManager,
    monkeypatch,
):
    captured_service_tiers: list[str | None] = []
    mgr.bind_thread(100, 1, "@1", window_name="demo")
    mgr.set_topic_service_tier_selection(100, 1, service_tier="flex")
    mgr.set_window_codex_thread_id("@1", "thread-1")
    mgr.set_window_approval_mode("@1", "never")

    async def _turn_start(
        *,
        thread_id: str,
        inputs: list[dict[str, object]],
        approval_policy: str | None = None,
        timeout: float = 90.0,
        model: str | None = None,
        effort: str | None = None,
        service_tier: str | None = None,
    ):
        _ = thread_id, inputs, approval_policy, timeout, model, effort
        captured_service_tiers.append(service_tier)
        return {"turn": {"id": "turn-1"}}

    monkeypatch.setattr(
        "coco.session.codex_app_server_client.turn_start",
        _turn_start,
    )
    monkeypatch.setattr(
        "coco.session.codex_app_server_client.get_active_turn_id",
        lambda _thread_id: None,
    )
    monkeypatch.setattr(
        SessionManager,
        "_runtime_write_state",
        staticmethod(lambda _cwd: ("/tmp/demo", True)),
    )

    ok, _msg = await mgr._send_inputs_via_codex_app_server(
        window_id="@1",
        inputs=[{"type": "text", "text": "hello"}],
        steer=False,
        window_name="demo",
        cwd="/tmp/demo",
    )

    assert ok is True
    assert captured_service_tiers == ["flex"]


@pytest.mark.asyncio
async def test_send_inputs_to_window_app_server_only_uses_cached_state_without_legacy_window(
    mgr: SessionManager,
    monkeypatch,
):
    mgr.get_window_state("@900000").cwd = "/tmp/demo"
    mgr.get_window_state("@900000").window_name = "demo"

    monkeypatch.setattr(session_mod.config, "session_provider", "codex")
    monkeypatch.setattr(session_mod.config, "runtime_mode", "app_server_only")
    monkeypatch.setattr(session_mod.config, "codex_transport", "legacy")

    captured: dict[str, object] = {}

    async def _send_inputs_via_codex_app_server(
        *,
        window_id: str,
        inputs: list[dict[str, object]],
        steer: bool,
        window_name: str,
        cwd: str,
    ):
        captured["window_id"] = window_id
        captured["inputs"] = inputs
        captured["steer"] = steer
        captured["window_name"] = window_name
        captured["cwd"] = cwd
        return True, "ok"

    monkeypatch.setattr(mgr, "_send_inputs_via_codex_app_server", _send_inputs_via_codex_app_server)

    ok, msg = await mgr.send_inputs_to_window(
        "@900000",
        [{"type": "text", "text": "hello"}],
        steer=False,
    )

    assert ok is True
    assert msg == "ok"
    assert captured["window_id"] == "@900000"
    assert captured["window_name"] == "demo"
    assert captured["cwd"] == "/tmp/demo"


@pytest.mark.asyncio
async def test_send_inputs_to_window_hybrid_app_server_mode_skips_legacy_lookup(
    mgr: SessionManager,
    monkeypatch,
):
    mgr.get_window_state("@900001").cwd = "/tmp/demo"
    mgr.get_window_state("@900001").window_name = "demo"

    monkeypatch.setattr(session_mod.config, "session_provider", "codex")
    monkeypatch.setattr(session_mod.config, "runtime_mode", "hybrid")
    monkeypatch.setattr(session_mod.config, "codex_transport", "app_server")

    captured: dict[str, object] = {}

    async def _send_inputs_via_codex_app_server(
        *,
        window_id: str,
        inputs: list[dict[str, object]],
        steer: bool,
        window_name: str,
        cwd: str,
    ):
        captured["window_id"] = window_id
        captured["inputs"] = inputs
        captured["steer"] = steer
        captured["window_name"] = window_name
        captured["cwd"] = cwd
        return True, "ok"

    monkeypatch.setattr(mgr, "_send_inputs_via_codex_app_server", _send_inputs_via_codex_app_server)

    ok, msg = await mgr.send_inputs_to_window(
        "@900001",
        [{"type": "text", "text": "hello"}],
        steer=False,
    )

    assert ok is True
    assert msg == "ok"
    assert captured["window_id"] == "@900001"
    assert captured["window_name"] == "demo"
    assert captured["cwd"] == "/tmp/demo"


@pytest.mark.asyncio
async def test_send_inputs_to_window_app_server_failure_returns_without_legacy_fallback(
    mgr: SessionManager,
    monkeypatch,
):
    mgr.get_window_state("@900002").cwd = "/tmp/demo"
    mgr.get_window_state("@900002").window_name = "demo"

    monkeypatch.setattr(session_mod.config, "session_provider", "codex")
    monkeypatch.setattr(session_mod.config, "runtime_mode", "hybrid")
    monkeypatch.setattr(session_mod.config, "codex_transport", "app_server")

    async def _send_inputs_via_codex_app_server(**_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(mgr, "_send_inputs_via_codex_app_server", _send_inputs_via_codex_app_server)

    telemetry_events: list[tuple[str, dict[str, object]]] = []

    def _emit(event: str, **payload):
        telemetry_events.append((event, payload))

    monkeypatch.setattr(session_mod, "emit_telemetry", _emit)

    ok, msg = await mgr.send_inputs_to_window(
        "@900002",
        [{"type": "text", "text": "hello"}],
        steer=False,
    )

    assert ok is False
    assert msg == "App-server send failed: boom"
    assert telemetry_events
    event, payload = telemetry_events[-1]
    assert event == "transport.app_server.send_failed"
    assert payload["fallback_allowed"] is False


@pytest.mark.asyncio
async def test_send_inputs_to_window_thread_not_found_retries_with_fresh_thread(
    mgr: SessionManager,
    monkeypatch,
):
    state = mgr.get_window_state("@900003")
    state.cwd = "/tmp/demo"
    state.window_name = "demo"
    state.codex_thread_id = "thread-old"
    state.codex_active_turn_id = "turn-old"
    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=7,
        codex_thread_id="thread-old",
        cwd="/tmp/demo",
        display_name="demo",
        window_id="@900003",
    )

    monkeypatch.setattr(session_mod.config, "session_provider", "codex")
    monkeypatch.setattr(session_mod.config, "runtime_mode", "hybrid")
    monkeypatch.setattr(session_mod.config, "codex_transport", "app_server")

    call_states: list[str] = []

    async def _send_inputs_via_codex_app_server(
        *,
        window_id: str,
        inputs: list[dict[str, object]],
        steer: bool,
        window_name: str,
        cwd: str,
    ):
        _ = window_id, inputs, window_name, cwd
        call_states.append(mgr.get_window_codex_thread_id("@900003"))
        if len(call_states) == 1:
            assert steer is False
            raise session_mod.CodexAppServerError("thread not found: thread-old")
        assert steer is False
        mgr.set_window_codex_thread_id("@900003", "thread-new")
        return True, "ok"

    monkeypatch.setattr(mgr, "_send_inputs_via_codex_app_server", _send_inputs_via_codex_app_server)

    telemetry_events: list[tuple[str, dict[str, object]]] = []

    def _emit(event: str, **payload):
        telemetry_events.append((event, payload))

    monkeypatch.setattr(session_mod, "emit_telemetry", _emit)

    ok, msg = await mgr.send_inputs_to_window(
        "@900003",
        [{"type": "text", "text": "hello"}],
        steer=False,
    )

    assert ok is True
    assert msg == "ok"
    assert call_states == ["thread-old", ""]
    assert mgr.get_window_codex_thread_id("@900003") == "thread-new"
    binding = mgr.resolve_topic_binding(100, 7)
    assert binding is not None
    assert binding.codex_thread_id == "thread-new"
    event_names = [event for event, _payload in telemetry_events]
    assert "transport.app_server.thread_missing_retry" in event_names
    assert "transport.app_server.thread_missing_recovered" in event_names
    assert "transport.app_server.send_failed" not in event_names


@pytest.mark.asyncio
async def test_send_inputs_to_window_thread_not_found_prefers_latest_cwd_resume(
    mgr: SessionManager,
    monkeypatch,
):
    state = mgr.get_window_state("@900007")
    state.cwd = "/tmp/demo"
    state.window_name = "demo"
    state.codex_thread_id = "thread-old"
    state.codex_active_turn_id = "turn-old"
    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=7,
        codex_thread_id="thread-old",
        cwd="/tmp/demo",
        display_name="demo",
        window_id="@900007",
    )

    monkeypatch.setattr(session_mod.config, "session_provider", "codex")
    monkeypatch.setattr(session_mod.config, "runtime_mode", "hybrid")
    monkeypatch.setattr(session_mod.config, "codex_transport", "app_server")

    async def _resume_latest(*, window_id: str, cwd: str) -> str:
        assert window_id == "@900007"
        assert cwd == "/tmp/demo"
        mgr.set_window_codex_thread_id("@900007", "thread-resumed")
        mgr.set_window_codex_active_turn_id("@900007", "turn-resumed")
        return "thread-resumed"

    monkeypatch.setattr(
        mgr,
        "resume_latest_codex_session_for_window",
        _resume_latest,
        raising=False,
    )

    call_states: list[str] = []

    async def _send_inputs_via_codex_app_server(
        *,
        window_id: str,
        inputs: list[dict[str, object]],
        steer: bool,
        window_name: str,
        cwd: str,
    ):
        _ = window_id, inputs, window_name, cwd
        call_states.append(mgr.get_window_codex_thread_id("@900007"))
        if len(call_states) == 1:
            assert steer is False
            raise session_mod.CodexAppServerError("thread not found: thread-old")
        assert steer is False
        return True, "ok"

    monkeypatch.setattr(mgr, "_send_inputs_via_codex_app_server", _send_inputs_via_codex_app_server)

    telemetry_events: list[tuple[str, dict[str, object]]] = []

    def _emit(event: str, **payload):
        telemetry_events.append((event, payload))

    monkeypatch.setattr(session_mod, "emit_telemetry", _emit)

    ok, msg = await mgr.send_inputs_to_window(
        "@900007",
        [{"type": "text", "text": "hello"}],
        steer=False,
    )

    assert ok is True
    assert msg == "ok"
    assert call_states == ["thread-old", "thread-resumed"]
    assert mgr.get_window_codex_thread_id("@900007") == "thread-resumed"
    binding = mgr.resolve_topic_binding(100, 7)
    assert binding is not None
    assert binding.codex_thread_id == "thread-resumed"
    event_names = [event for event, _payload in telemetry_events]
    assert "transport.app_server.thread_missing_retry" in event_names
    assert "transport.app_server.thread_missing_recovered" in event_names
    assert "transport.app_server.send_failed" not in event_names


@pytest.mark.asyncio
async def test_send_inputs_to_window_thread_not_found_retry_failure_returns_combined_error(
    mgr: SessionManager,
    monkeypatch,
):
    state = mgr.get_window_state("@900004")
    state.cwd = "/tmp/demo"
    state.window_name = "demo"
    state.codex_thread_id = "thread-old"

    monkeypatch.setattr(session_mod.config, "session_provider", "codex")
    monkeypatch.setattr(session_mod.config, "runtime_mode", "hybrid")
    monkeypatch.setattr(session_mod.config, "codex_transport", "app_server")

    attempts = 0

    async def _send_inputs_via_codex_app_server(**_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise session_mod.CodexAppServerError("thread not found: thread-old")
        raise RuntimeError("retry exploded")

    monkeypatch.setattr(mgr, "_send_inputs_via_codex_app_server", _send_inputs_via_codex_app_server)

    telemetry_events: list[tuple[str, dict[str, object]]] = []

    def _emit(event: str, **payload):
        telemetry_events.append((event, payload))

    monkeypatch.setattr(session_mod, "emit_telemetry", _emit)

    ok, msg = await mgr.send_inputs_to_window(
        "@900004",
        [{"type": "text", "text": "hello"}],
        steer=False,
    )

    assert ok is False
    assert "thread not found: thread-old" in msg
    assert "retry with new thread failed: retry exploded" in msg
    assert mgr.get_window_codex_thread_id("@900004") == ""
    assert telemetry_events
    event, payload = telemetry_events[-1]
    assert event == "transport.app_server.send_failed"
    assert payload["fallback_allowed"] is False


@pytest.mark.asyncio
async def test_send_inputs_to_window_turn_steer_timeout_retries_with_turn_start(
    mgr: SessionManager,
    monkeypatch,
):
    state = mgr.get_window_state("@900005")
    state.cwd = "/tmp/demo"
    state.window_name = "demo"
    state.codex_thread_id = "thread-live"
    state.codex_active_turn_id = "turn-stale"

    monkeypatch.setattr(session_mod.config, "session_provider", "codex")
    monkeypatch.setattr(session_mod.config, "runtime_mode", "hybrid")
    monkeypatch.setattr(session_mod.config, "codex_transport", "app_server")

    call_states: list[tuple[str, str]] = []
    cleared_thread_ids: list[str] = []

    async def _send_inputs_via_codex_app_server(
        *,
        window_id: str,
        inputs: list[dict[str, object]],
        steer: bool,
        window_name: str,
        cwd: str,
    ):
        _ = window_id, inputs, window_name, cwd, steer
        call_states.append(
            (
                mgr.get_window_codex_thread_id("@900005"),
                mgr.get_window_codex_active_turn_id("@900005"),
            )
        )
        if len(call_states) == 1:
            raise session_mod.CodexAppServerError(
                "Timed out waiting for app-server response: turn/steer"
            )
        assert mgr.get_window_codex_active_turn_id("@900005") == ""
        return True, "ok"

    monkeypatch.setattr(mgr, "_send_inputs_via_codex_app_server", _send_inputs_via_codex_app_server)
    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "clear_active_turn",
        lambda thread_id: cleared_thread_ids.append(thread_id),
    )

    telemetry_events: list[tuple[str, dict[str, object]]] = []

    def _emit(event: str, **payload):
        telemetry_events.append((event, payload))

    monkeypatch.setattr(session_mod, "emit_telemetry", _emit)

    ok, msg = await mgr.send_inputs_to_window(
        "@900005",
        [{"type": "text", "text": "hello"}],
        steer=False,
    )

    assert ok is True
    assert msg == "ok"
    assert call_states == [("thread-live", "turn-stale"), ("thread-live", "")]
    assert mgr.get_window_codex_active_turn_id("@900005") == ""
    assert cleared_thread_ids == ["thread-live"]
    event_names = [event for event, _payload in telemetry_events]
    assert "transport.app_server.steer_timeout_retry" in event_names
    assert "transport.app_server.steer_timeout_recovered" in event_names
    assert "transport.app_server.send_failed" not in event_names


@pytest.mark.asyncio
async def test_send_inputs_to_window_turn_steer_timeout_retry_failure_returns_combined_error(
    mgr: SessionManager,
    monkeypatch,
):
    state = mgr.get_window_state("@900006")
    state.cwd = "/tmp/demo"
    state.window_name = "demo"
    state.codex_thread_id = "thread-live"
    state.codex_active_turn_id = "turn-stale"

    monkeypatch.setattr(session_mod.config, "session_provider", "codex")
    monkeypatch.setattr(session_mod.config, "runtime_mode", "hybrid")
    monkeypatch.setattr(session_mod.config, "codex_transport", "app_server")

    attempts = 0

    async def _send_inputs_via_codex_app_server(**_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise session_mod.CodexAppServerError(
                "Timed out waiting for app-server response: turn/steer"
            )
        raise RuntimeError("steer retry exploded")

    monkeypatch.setattr(mgr, "_send_inputs_via_codex_app_server", _send_inputs_via_codex_app_server)
    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "clear_active_turn",
        lambda _thread_id: None,
    )

    telemetry_events: list[tuple[str, dict[str, object]]] = []

    def _emit(event: str, **payload):
        telemetry_events.append((event, payload))

    monkeypatch.setattr(session_mod, "emit_telemetry", _emit)

    ok, msg = await mgr.send_inputs_to_window(
        "@900006",
        [{"type": "text", "text": "hello"}],
        steer=False,
    )

    assert ok is False
    assert "Timed out waiting for app-server response: turn/steer" in msg
    assert "retry with turn/start failed: steer retry exploded" in msg
    assert mgr.get_window_codex_active_turn_id("@900006") == ""
    assert telemetry_events
    event, payload = telemetry_events[-1]
    assert event == "transport.app_server.send_failed"
    assert payload["fallback_allowed"] is False


@pytest.mark.asyncio
async def test_validate_codex_topic_bindings_clears_invalid_thread_ids(
    mgr: SessionManager,
    monkeypatch,
):
    monkeypatch.setattr(session_mod.config, "session_provider", "codex")
    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=7,
        codex_thread_id="thread-dead",
        cwd="/tmp/demo",
        display_name="demo",
        window_id="@900000",
    )
    state = mgr.get_window_state("@900000")
    state.codex_active_turn_id = "turn-1"

    async def _thread_read(*, thread_id: str):
        _ = thread_id
        raise session_mod.CodexAppServerError("thread not found")

    monkeypatch.setattr(session_mod.codex_app_server_client, "thread_read", _thread_read)

    summary = await mgr.validate_codex_topic_bindings()

    assert summary == {"checked": 1, "invalid": 1, "repaired": 1}
    binding = mgr.resolve_topic_binding(100, 7)
    assert binding is not None
    assert binding.codex_thread_id == ""
    assert mgr.get_window_state("@900000").codex_thread_id == ""
    assert mgr.get_window_state("@900000").codex_active_turn_id == ""


@pytest.mark.asyncio
async def test_validate_codex_topic_bindings_keeps_valid_thread_ids(
    mgr: SessionManager,
    monkeypatch,
):
    monkeypatch.setattr(session_mod.config, "session_provider", "codex")
    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=7,
        codex_thread_id="thread-live",
        cwd="/tmp/demo",
        display_name="demo",
        window_id="@900000",
    )

    async def _thread_read(*, thread_id: str):
        return {"thread": {"id": thread_id}}

    monkeypatch.setattr(session_mod.codex_app_server_client, "thread_read", _thread_read)

    summary = await mgr.validate_codex_topic_bindings()

    assert summary == {"checked": 1, "invalid": 0, "repaired": 0}
    binding = mgr.resolve_topic_binding(100, 7)
    assert binding is not None
    assert binding.codex_thread_id == "thread-live"


def test_normalize_app_server_inputs_splits_large_text(mgr: SessionManager):
    text = "x" * 6500
    normalized = mgr._normalize_app_server_inputs([{"type": "text", "text": text}])
    assert len(normalized) == 3
    assert all(item.get("type") == "text" for item in normalized)
    assert "".join(str(item.get("text", "")) for item in normalized) == text


def test_thread_skills_roundtrip_and_unbind_cleanup(mgr: SessionManager):
    mgr.bind_thread(100, 5, "@1")
    mgr.set_thread_skills(100, 5, ["demo", "Demo", "", "ops"])
    mgr.set_thread_codex_skills(100, 5, ["reviewer", "Reviewer"])
    assert mgr.get_thread_skills(100, 5) == ["demo", "ops"]
    assert mgr.get_thread_codex_skills(100, 5) == ["reviewer"]

    mgr.unbind_thread(100, 5)
    assert mgr.get_thread_skills(100, 5) == []
    assert mgr.get_thread_codex_skills(100, 5) == []


@pytest.mark.asyncio
async def test_send_topic_text_to_window_injects_app_context_for_app_server(
    mgr: SessionManager,
    monkeypatch,
    tmp_path: Path,
):
    app_root = tmp_path / "apps"
    app_dir = app_root / "demo"
    app_dir.mkdir(parents=True)
    (app_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo skill\n---\n# Demo\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(session_mod.config, "apps_paths", [app_root])
    monkeypatch.setattr(mgr, "_codex_app_server_mode_enabled", lambda: True)
    mgr.bind_thread(100, 5, "@1")
    mgr.set_thread_skills(100, 5, ["demo"])

    captured: dict[str, object] = {}

    async def _send_inputs_to_window(
        window_id: str,
        inputs: list[dict[str, object]],
        *,
        steer: bool = False,
    ):
        captured["window_id"] = window_id
        captured["inputs"] = inputs
        captured["steer"] = steer
        return True, "ok"

    monkeypatch.setattr(mgr, "send_inputs_to_window", _send_inputs_to_window)

    ok, _msg = await mgr.send_topic_text_to_window(
        user_id=100,
        thread_id=5,
        window_id="@1",
        text="hello world",
        steer=False,
    )

    assert ok is True
    assert captured["window_id"] == "@1"
    inputs = captured["inputs"]
    assert isinstance(inputs, list)
    assert inputs[0]["type"] == "text"
    assert "[coco guidance]" in str(inputs[0]["text"])
    assert "app `demo`" in str(inputs[0]["text"])
    assert inputs[1] == {"type": "text", "text": "hello world"}


@pytest.mark.asyncio
async def test_send_topic_text_to_window_uses_codex_skill_inputs_for_app_server(
    mgr: SessionManager,
    monkeypatch,
    tmp_path: Path,
):
    codex_root = tmp_path / "codex-skills"
    codex_dir = codex_root / "reviewer"
    codex_dir.mkdir(parents=True)
    (codex_dir / "SKILL.md").write_text(
        "---\nname: reviewer\ndescription: Review skill\n---\n# Review\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(session_mod.config, "codex_skills_paths", [codex_root])
    monkeypatch.setattr(mgr, "_codex_app_server_mode_enabled", lambda: True)
    mgr.bind_thread(100, 5, "@1")
    mgr.set_thread_codex_skills(100, 5, ["reviewer"])

    captured: dict[str, object] = {}

    async def _send_inputs_to_window(
        window_id: str,
        inputs: list[dict[str, object]],
        *,
        steer: bool = False,
    ):
        captured["window_id"] = window_id
        captured["inputs"] = inputs
        captured["steer"] = steer
        return True, "ok"

    monkeypatch.setattr(mgr, "send_inputs_to_window", _send_inputs_to_window)

    ok, _msg = await mgr.send_topic_text_to_window(
        user_id=100,
        thread_id=5,
        window_id="@1",
        text="hello world",
        steer=False,
    )

    assert ok is True
    assert captured["window_id"] == "@1"
    inputs = captured["inputs"]
    assert isinstance(inputs, list)
    assert inputs[0]["type"] == "skill"
    assert inputs[0]["name"] == "reviewer"
    assert Path(str(inputs[0]["path"])).name == "reviewer"
    assert inputs[1] == {"type": "text", "text": "hello world"}


@pytest.mark.asyncio
async def test_send_topic_text_to_window_injects_legacy_skill_context(
    mgr: SessionManager,
    monkeypatch,
    tmp_path: Path,
):
    app_root = tmp_path / "apps"
    app_dir = app_root / "demo"
    app_dir.mkdir(parents=True)
    (app_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo skill\n---\n# Demo\n",
        encoding="utf-8",
    )
    codex_root = tmp_path / "codex-skills"
    codex_dir = codex_root / "reviewer"
    codex_dir.mkdir(parents=True)
    (codex_dir / "SKILL.md").write_text(
        "---\nname: reviewer\ndescription: Review skill\n---\n# Review\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(session_mod.config, "apps_paths", [app_root])
    monkeypatch.setattr(session_mod.config, "codex_skills_paths", [codex_root])
    monkeypatch.setattr(mgr, "_codex_app_server_mode_enabled", lambda: False)
    mgr.bind_thread(100, 5, "@1")
    mgr.set_thread_skills(100, 5, ["demo"])
    mgr.set_thread_codex_skills(100, 5, ["reviewer"])

    captured: dict[str, object] = {}

    async def _send_to_window(window_id: str, text: str, *, steer: bool = False):
        captured["window_id"] = window_id
        captured["text"] = text
        captured["steer"] = steer
        return True, "ok"

    monkeypatch.setattr(mgr, "send_to_window", _send_to_window)

    ok, _msg = await mgr.send_topic_text_to_window(
        user_id=100,
        thread_id=5,
        window_id="@1",
        text="hello world",
        steer=True,
    )

    assert ok is True
    assert captured["window_id"] == "@1"
    injected = str(captured["text"])
    assert "[coco guidance]" in injected
    assert "app `demo`" in injected
    assert "skill `reviewer`" in injected
    assert injected.endswith("hello world")
