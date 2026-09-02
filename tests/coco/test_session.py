"""Tests for SessionManager pure dict operations."""

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import coco.agent_rpc as agent_rpc_mod
import coco.session as session_mod
from coco.cluster_rpc import ClusterRpcError
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


@pytest.fixture
def telegram_memory_path(tmp_path, monkeypatch) -> Path:
    memory_path = tmp_path / "TELEGRAM_CHAT_MEMORY.jsonl"
    monkeypatch.setenv("COCO_TELEGRAM_MEMORY_LOG_PATH", str(memory_path))
    return memory_path


def _append_memory_entries(path: Path, entries: list[dict[str, object]]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry))
            handle.write("\n")


def test_window_state_round_trips_remote_transport_identity() -> None:
    state = session_mod.WindowState(
        codex_transport_epoch="agent-epoch-2",
        codex_transport_epoch_started_at=200.5,
        codex_transport_generation=9,
    )

    restored = session_mod.WindowState.from_dict(state.to_dict())

    assert restored.codex_transport_epoch == "agent-epoch-2"
    assert restored.codex_transport_epoch_started_at == 200.5
    assert restored.codex_transport_generation == 9


def test_recent_activity_skips_malformed_numeric_memory_fields(
    mgr: SessionManager,
    telegram_memory_path: Path,
):
    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=8,
        chat_id=-100123,
        codex_thread_id="thread-8",
        window_id="@8",
        display_name="demo",
    )
    _append_memory_entries(
        telegram_memory_path,
        [
            {
                "direction": "in",
                "chat_id": "not-a-chat",
                "thread_id": "not-a-thread",
                "from_user_id": "not-a-user",
                "text": "corrupt",
            },
            {
                "direction": "in",
                "chat_id": -100123,
                "thread_id": 8,
                "from_user_id": 100,
                "text": "valid activity",
            },
        ],
    )

    summary = mgr._build_coco_recent_activity_summary(
        user_id=100,
        chat_id=-100123,
        current_thread_id=5,
    )

    assert summary == ["demo: User: valid activity"]


@pytest.mark.asyncio
async def test_session_summary_skips_non_object_jsonl_entries(
    mgr: SessionManager,
    monkeypatch,
    tmp_path: Path,
):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("[]\nnull\n7\n", encoding="utf-8")
    monkeypatch.setattr(
        mgr,
        "_build_session_file_path",
        lambda _session_id, _cwd: transcript,
    )

    result = await mgr._get_session_direct("session-1", str(tmp_path))

    assert result is not None
    assert result.summary == "Untitled"
    assert result.message_count == 0


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

    def test_set_codex_turn_for_thread_can_scope_to_machine(
        self, mgr: SessionManager
    ) -> None:
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=1,
            codex_thread_id="shared-thread",
            window_id="@machine-a",
            machine_id="machine-a",
        )
        mgr.bind_topic_to_codex_thread(
            user_id=200,
            thread_id=2,
            codex_thread_id="shared-thread",
            window_id="@machine-b",
            machine_id="machine-b",
        )
        mgr.set_window_codex_active_turn_id("@machine-a", "old-a")
        mgr.set_window_codex_active_turn_id("@machine-b", "old-b")

        mgr.set_codex_turn_for_thread(
            "shared-thread",
            "new-a",
            machine_id="machine-a",
        )

        assert mgr.get_window_codex_active_turn_id("@machine-a") == "new-a"
        assert mgr.get_window_codex_active_turn_id("@machine-b") == "old-b"

    def test_unbind_topic_removes_legacy_mapping(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")
        removed = mgr.unbind_topic(100, 1)
        assert removed is not None
        assert removed.window_id == "@1"
        assert mgr.resolve_topic_binding(100, 1) is None
        assert mgr.get_window_for_thread(100, 1) is None

    def test_clear_window_codex_turns_for_machine_preserves_remote_turns(
        self, mgr: SessionManager, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            mgr,
            "_local_machine_identity",
            lambda: ("local-node", "Local Node"),
        )
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=1,
            codex_thread_id="local-thread",
            window_id="@local",
            cwd="/tmp/local",
            machine_id="local-node",
            machine_display_name="Local Node",
        )
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=2,
            codex_thread_id="remote-thread",
            window_id="@remote",
            cwd="/tmp/remote",
            machine_id="remote-node",
            machine_display_name="Remote Node",
        )
        mgr.get_window_state("@local").codex_active_turn_id = "local-turn"
        mgr.get_window_state("@remote").codex_active_turn_id = "remote-turn"
        mgr.get_window_state("@unbound").codex_active_turn_id = "legacy-local-turn"
        save_calls = 0

        def _save_state():
            nonlocal save_calls
            save_calls += 1

        monkeypatch.setattr(mgr, "_save_state", _save_state)

        assert mgr.clear_window_codex_turns_for_machine("local-node") == 2
        assert mgr.get_window_state("@local").codex_active_turn_id == ""
        assert mgr.get_window_state("@remote").codex_active_turn_id == "remote-turn"
        assert mgr.get_window_state("@unbound").codex_active_turn_id == ""
        assert save_calls == 1

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

    def test_invalidate_topic_codex_thread_clears_window_and_binding(
        self, mgr: SessionManager
    ) -> None:
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=1,
            chat_id=-100123,
            codex_thread_id="thread-1",
            window_id="@1",
            cwd="/tmp/proj",
            display_name="proj",
        )
        mgr.set_window_codex_active_turn_id("@1", "turn-1")

        changed = mgr.invalidate_topic_codex_thread(
            user_id=100,
            thread_id=1,
            chat_id=-100123,
        )

        binding = mgr.resolve_topic_binding(100, 1, chat_id=-100123)
        assert changed is True
        assert mgr.get_window_codex_thread_id("@1") == ""
        assert mgr.get_window_codex_active_turn_id("@1") == ""
        assert binding is not None
        assert binding.codex_thread_id == ""

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

    def test_topic_response_mode_roundtrip(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="proj")

        changed = mgr.set_topic_response_mode(
            100,
            1,
            response_mode="voice",
        )

        binding = mgr.resolve_topic_binding(100, 1)
        assert changed is True
        assert binding is not None
        assert binding.response_mode == "voice"
        assert mgr.get_topic_response_mode(100, 1) == "voice"

    def test_next_topic_response_mode_is_one_shot(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="proj")

        changed = mgr.set_next_topic_response_mode(
            100,
            1,
            response_mode="voice",
        )

        assert changed is True
        assert mgr.peek_next_topic_response_mode(100, 1) == "voice"
        assert mgr.consume_next_topic_response_mode(100, 1) == "voice"
        assert mgr.peek_next_topic_response_mode(100, 1) == ""
        assert mgr.get_topic_response_mode(100, 1) == "text"

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
    async def test_host_follow_local_resumes_bound_codex_thread_not_latest(
        self, mgr: SessionManager, monkeypatch
    ) -> None:
        canonical_thread_id = "thread-topic-canonical"
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=1,
            codex_thread_id=canonical_thread_id,
            window_id="@1",
            cwd="/tmp/proj",
            display_name="proj",
        )
        mgr.set_topic_sync_mode(100, 1, TOPIC_SYNC_MODE_HOST_FOLLOW_FINAL)
        mgr.get_window_state("@1").cwd = "/tmp/proj"

        exact_resumes: list[tuple[str, str, str]] = []
        latest_resumes: list[tuple[str, str]] = []

        async def _resume_exact(*, window_id: str, cwd: str, thread_id: str) -> str:
            exact_resumes.append((window_id, cwd, thread_id))
            return thread_id

        async def _resume_latest(*, window_id: str, cwd: str) -> str:
            latest_resumes.append((window_id, cwd))
            return "thread-unrelated-latest"

        async def _send_to_window(
            window_id: str,
            text: str,
            *,
            steer: bool = False,
            force_new_turn: bool = False,
            **_kwargs,
        ) -> tuple[bool, str]:
            _ = window_id, text, steer, force_new_turn
            return True, "ok"

        monkeypatch.setattr(mgr, "resume_codex_session_for_window", _resume_exact)
        monkeypatch.setattr(
            mgr,
            "resume_latest_codex_session_for_window",
            _resume_latest,
        )
        monkeypatch.setattr(mgr, "send_to_window", _send_to_window)

        ok, message = await mgr.send_topic_text_to_window(
            user_id=100,
            thread_id=1,
            window_id="@1",
            text="continue this topic",
        )

        assert ok is True
        assert message == "ok"
        assert exact_resumes == [("@1", "/tmp/proj", canonical_thread_id)]
        assert latest_resumes == []

    @pytest.mark.asyncio
    async def test_host_follow_remote_resumes_bound_codex_thread_not_latest(
        self, mgr: SessionManager, monkeypatch
    ) -> None:
        canonical_thread_id = "thread-topic-remote-canonical"
        monkeypatch.setattr(
            mgr,
            "_local_machine_identity",
            lambda: ("local-node", "Local"),
        )
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=1,
            codex_thread_id=canonical_thread_id,
            window_id="@1",
            cwd="/tmp/proj",
            display_name="proj",
            machine_id="remote-node",
            machine_display_name="Remote",
        )
        mgr.set_topic_sync_mode(100, 1, TOPIC_SYNC_MODE_HOST_FOLLOW_FINAL)
        mgr.get_window_state("@1").cwd = "/tmp/proj"

        exact_resumes: list[tuple[str, dict[str, object]]] = []
        latest_resumes: list[tuple[str, dict[str, object]]] = []
        sends: list[tuple[str, dict[str, object]]] = []
        dispatch_state = session_mod.TopicSendDispatchState()

        async def _resume_thread(machine_id: str, **kwargs: object):
            exact_resumes.append((machine_id, kwargs))
            return {
                "thread_id": canonical_thread_id,
                "turn_id": "turn-topic-remote",
                "model_slug": "",
                "reasoning_effort": "",
            }

        async def _resume_latest(machine_id: str, **kwargs: object):
            latest_resumes.append((machine_id, kwargs))
            return {"thread_id": "thread-unrelated-latest"}

        async def _send_inputs(machine_id: str, **kwargs: object):
            sends.append((machine_id, kwargs))
            return {
                "ok": True,
                "message": "ok",
                "thread_id": canonical_thread_id,
                "turn_id": "turn-topic-remote",
                "dispatch_mode": "turn_start",
                "transport_epoch": "agent-epoch-1",
                "transport_epoch_started_at": 100.0,
                "transport_generation": 1,
            }

        monkeypatch.setattr(
            agent_rpc_mod.agent_rpc_client,
            "resume_thread",
            _resume_thread,
        )
        monkeypatch.setattr(
            agent_rpc_mod.agent_rpc_client,
            "resume_latest",
            _resume_latest,
        )
        monkeypatch.setattr(
            agent_rpc_mod.agent_rpc_client,
            "send_inputs",
            _send_inputs,
        )

        ok, message = await mgr.send_topic_text_to_window(
            user_id=100,
            thread_id=1,
            window_id="@1",
            text="continue this remote topic",
            dispatch_state=dispatch_state,
        )

        assert ok is True
        assert message == "ok"
        assert len(exact_resumes) == 1
        assert exact_resumes[0][0] == "remote-node"
        assert exact_resumes[0][1]["window_id"] == "@1"
        assert exact_resumes[0][1]["cwd"] == "/tmp/proj"
        assert exact_resumes[0][1]["thread_id"] == canonical_thread_id
        assert latest_resumes == []
        assert len(sends) == 1
        assert sends[0][1]["thread_id"] == canonical_thread_id
        assert dispatch_state.started_new_turn is True

    @pytest.mark.asyncio
    async def test_host_follow_remote_resume_drops_stale_result_after_explicit_rebind(
        self, mgr: SessionManager, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            mgr,
            "_local_machine_identity",
            lambda: ("local-node", "Local"),
        )
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=1,
            codex_thread_id="thread-a",
            window_id="@1",
            cwd="/tmp/proj",
            display_name="proj",
            machine_id="remote-a",
            machine_display_name="Remote A",
        )
        mgr.set_topic_sync_mode(100, 1, TOPIC_SYNC_MODE_HOST_FOLLOW_FINAL)

        resume_started = asyncio.Event()
        release_resume = asyncio.Event()
        sends: list[tuple[str, dict[str, object]]] = []

        async def _resume_thread(machine_id: str, **kwargs: object):
            assert machine_id == "remote-a"
            assert kwargs["thread_id"] == "thread-a"
            resume_started.set()
            await release_resume.wait()
            return {
                "thread_id": "thread-a",
                "turn_id": "turn-a",
            }

        async def _send_inputs(machine_id: str, **kwargs: object):
            sends.append((machine_id, kwargs))
            return {
                "ok": True,
                "message": "stale request dispatched",
                "thread_id": "thread-a",
                "turn_id": "turn-a",
            }

        monkeypatch.setattr(
            agent_rpc_mod.agent_rpc_client,
            "resume_thread",
            _resume_thread,
        )
        monkeypatch.setattr(
            agent_rpc_mod.agent_rpc_client,
            "send_inputs",
            _send_inputs,
        )

        task = asyncio.create_task(
            mgr.send_topic_text_to_window(
                user_id=100,
                thread_id=1,
                window_id="@1",
                text="continue this topic",
            )
        )
        await asyncio.wait_for(resume_started.wait(), timeout=1)

        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=1,
            codex_thread_id="thread-b",
            window_id="@1",
            cwd="/tmp/proj-b",
            display_name="proj-b",
            machine_id="remote-b",
            machine_display_name="Remote B",
        )
        release_resume.set()
        ok, message = await task

        assert ok is False
        assert message
        assert sends == []
        binding = mgr._get_persisted_topic_binding(100, 1)
        assert binding is not None
        assert binding.codex_thread_id == "thread-b"
        assert binding.window_id == "@1"
        assert binding.machine_id == "remote-b"
        assert mgr.get_window_codex_thread_id("@1") == "thread-b"

    @pytest.mark.asyncio
    async def test_host_follow_local_resume_drops_stale_result_after_explicit_rebind(
        self, mgr: SessionManager, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            mgr,
            "_local_machine_identity",
            lambda: ("local-node", "Local"),
        )
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=1,
            codex_thread_id="thread-a",
            window_id="@900060",
            cwd="/tmp/proj-a",
            display_name="proj-a",
            machine_id="local-node",
            machine_display_name="Local",
        )
        mgr.set_topic_sync_mode(100, 1, TOPIC_SYNC_MODE_HOST_FOLLOW_FINAL)

        resume_started = asyncio.Event()
        release_resume = asyncio.Event()
        sends: list[tuple[str, str]] = []

        async def _resume_exact(*, window_id: str, cwd: str, thread_id: str) -> str:
            assert (window_id, cwd, thread_id) == (
                "@900060",
                "/tmp/proj-a",
                "thread-a",
            )
            resume_started.set()
            await release_resume.wait()
            return thread_id

        async def _send_to_window(
            window_id: str,
            text: str,
            *,
            steer: bool = False,
            force_new_turn: bool = False,
            **_kwargs: object,
        ) -> tuple[bool, str]:
            _ = steer, force_new_turn
            sends.append((window_id, text))
            return True, "stale request dispatched"

        monkeypatch.setattr(mgr, "resume_codex_session_for_window", _resume_exact)
        monkeypatch.setattr(mgr, "send_to_window", _send_to_window)

        task = asyncio.create_task(
            mgr.send_topic_text_to_window(
                user_id=100,
                thread_id=1,
                window_id="@900060",
                text="continue this local topic",
            )
        )
        await asyncio.wait_for(resume_started.wait(), timeout=1)

        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=1,
            codex_thread_id="thread-b",
            window_id="@900061",
            cwd="/tmp/proj-b",
            display_name="proj-b",
            machine_id="local-node",
            machine_display_name="Local",
        )
        release_resume.set()
        ok, message = await task

        assert ok is False
        assert message
        assert sends == []
        binding = mgr._get_persisted_topic_binding(100, 1)
        assert binding is not None
        assert binding.codex_thread_id == "thread-b"
        assert binding.window_id == "@900061"
        assert binding.cwd == "/tmp/proj-b"
        assert binding.machine_id == "local-node"
        assert mgr.get_window_codex_thread_id("@900060") == "thread-a"
        assert mgr.get_window_codex_thread_id("@900061") == "thread-b"

    @pytest.mark.asyncio
    async def test_non_active_writer_resume_error_still_propagates(
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
        mgr.get_window_state("@1").cwd = "/tmp/proj"

        async def _resume_exact(*, window_id: str, cwd: str, thread_id: str) -> str:
            assert (window_id, cwd, thread_id) == ("@1", "/tmp/proj", "thread-old")
            raise session_mod.CodexAppServerError("app-server connection failed")

        async def _resume_latest(*, window_id: str, cwd: str) -> str:
            raise AssertionError("implicit host-follow must not resume latest by cwd")

        monkeypatch.setattr(
            mgr,
            "resume_codex_session_for_window",
            _resume_exact,
        )
        monkeypatch.setattr(
            mgr,
            "resume_latest_codex_session_for_window",
            _resume_latest,
        )

        with pytest.raises(
            session_mod.CodexAppServerError,
            match="app-server connection failed",
        ):
            await mgr.send_topic_text_to_window(
                user_id=100,
                thread_id=1,
                window_id="@1",
                text="do not mask this error",
            )

    @pytest.mark.asyncio
    async def test_active_writer_resume_defers_telegram_takeover(
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
        mgr.get_window_state("@1").cwd = "/tmp/proj"

        async def _resume_exact(*, window_id: str, cwd: str, thread_id: str) -> str:
            assert window_id == "@1"
            assert cwd == "/tmp/proj"
            assert thread_id == "thread-old"
            raise session_mod.CodexAppServerError(
                "thread thread-live already has an active writer"
            )

        async def _resume_latest(*, window_id: str, cwd: str) -> str:
            raise AssertionError("implicit host-follow must not resume latest by cwd")

        async def _unexpected_send(*_args, **_kwargs):
            raise AssertionError("takeover must wait for the external writer")

        monkeypatch.setattr(
            mgr,
            "resume_codex_session_for_window",
            _resume_exact,
        )
        monkeypatch.setattr(
            mgr,
            "resume_latest_codex_session_for_window",
            _resume_latest,
        )
        monkeypatch.setattr(mgr, "send_to_window", _unexpected_send)

        ok, message = await mgr.send_topic_text_to_window(
            user_id=100,
            thread_id=1,
            window_id="@1",
            text="preserve this request",
        )

        assert ok is False
        assert "active writer" in message
        assert mgr.get_topic_sync_mode(100, 1) == TOPIC_SYNC_MODE_HOST_FOLLOW_FINAL
        assert mgr.is_window_external_turn_active("@1") is False

    @pytest.mark.asyncio
    async def test_send_topic_text_to_window_resumes_bound_thread_before_telegram_takeover(
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

        exact_resumes: list[tuple[str, str, str]] = []
        latest_resumes: list[tuple[str, str]] = []
        sent: list[tuple[str, str, bool]] = []

        async def _resume_exact(*, window_id: str, cwd: str, thread_id: str) -> str:
            exact_resumes.append((window_id, cwd, thread_id))
            assert thread_id == "thread-old"
            return thread_id

        async def _resume_latest(*, window_id: str, cwd: str) -> str:
            latest_resumes.append((window_id, cwd))
            raise AssertionError("implicit host-follow must not resume latest by cwd")

        async def _send_to_window(
            window_id: str,
            text: str,
            *,
            steer: bool = False,
            force_new_turn: bool = False,
            **_kwargs: object,
        ):
            _ = force_new_turn
            sent.append((window_id, text, steer))
            return True, "ok"

        monkeypatch.setattr(
            mgr,
            "resume_codex_session_for_window",
            _resume_exact,
        )
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
        assert exact_resumes == [("@1", "/tmp/proj", "thread-old")]
        assert latest_resumes == []
        assert sent == [("@1", "take over from telegram", False)]
        assert mgr.get_topic_sync_mode(100, 1) == TOPIC_SYNC_MODE_TELEGRAM_LIVE
        assert mgr.is_window_external_turn_active("@1") is False

    @pytest.mark.asyncio
    async def test_oversized_bound_resume_fails_without_rebinding_topic(
        self, mgr: SessionManager, monkeypatch
    ) -> None:
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=1,
            codex_thread_id="thread-oversized",
            window_id="@1",
            cwd="/tmp/proj",
            display_name="proj",
        )
        mgr.set_topic_sync_mode(100, 1, TOPIC_SYNC_MODE_HOST_FOLLOW_FINAL)
        mgr.get_window_state("@1").cwd = "/tmp/proj"

        exact_resumes: list[tuple[str, str, str]] = []
        latest_resumes: list[tuple[str, str]] = []

        async def _resume_exact(*, window_id: str, cwd: str, thread_id: str) -> str:
            exact_resumes.append((window_id, cwd, thread_id))
            raise session_mod.CodexAppServerError(
                "Codex transcript exceeds resume limit (999 > 100 bytes): "
                "thread-oversized"
            )

        async def _resume_latest(*, window_id: str, cwd: str) -> str:
            latest_resumes.append((window_id, cwd))
            raise AssertionError("implicit host-follow must not resume latest by cwd")

        monkeypatch.setattr(mgr, "resume_codex_session_for_window", _resume_exact)
        monkeypatch.setattr(mgr, "resume_latest_codex_session_for_window", _resume_latest)

        ok, message = await mgr.send_topic_text_to_window(
            user_id=100,
            thread_id=1,
            window_id="@1",
            text="keep this exact request",
        )

        assert ok is False
        assert "too large to resume automatically" in message
        assert exact_resumes == [("@1", "/tmp/proj", "thread-oversized")]
        assert latest_resumes == []
        assert mgr.get_window_codex_thread_id("@1") == "thread-oversized"
        binding = mgr.resolve_topic_binding(100, 1)
        assert binding is not None
        assert binding.codex_thread_id == "thread-oversized"

    @pytest.mark.asyncio
    async def test_host_follow_refreshes_goal_after_resuming_bound_thread(
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
        mgr.get_window_state("@1").cwd = "/tmp/proj"
        resumed = False
        exact_resumes: list[tuple[str, str, str]] = []
        latest_resumes: list[tuple[str, str]] = []
        sent: list[list[dict[str, object]]] = []

        async def _resume_exact(*, window_id: str, cwd: str, thread_id: str) -> str:
            nonlocal resumed
            assert window_id == "@1"
            assert cwd == "/tmp/proj"
            assert thread_id == "thread-old"
            exact_resumes.append((window_id, cwd, thread_id))
            resumed = True
            mgr.set_window_codex_thread_id("@1", thread_id)
            return thread_id

        async def _resume_latest(*, window_id: str, cwd: str) -> str:
            latest_resumes.append((window_id, cwd))
            raise AssertionError("implicit host-follow must not resume latest by cwd")

        async def _get_topic_goal(**_kwargs):
            assert resumed is True
            return True, {"goal": {"objective": "New thread goal", "status": "active"}}, ""

        async def _send_inputs_to_window(
            window_id: str,
            inputs: list[dict[str, object]],
            *,
            steer: bool = False,
            force_new_turn: bool = False,
            **_kwargs: object,
        ):
            _ = window_id, steer, force_new_turn
            sent.append(inputs)
            return True, "ok"

        monkeypatch.setattr(mgr, "resume_codex_session_for_window", _resume_exact)
        monkeypatch.setattr(mgr, "resume_latest_codex_session_for_window", _resume_latest)
        monkeypatch.setattr(mgr, "get_topic_goal", _get_topic_goal)
        monkeypatch.setattr(mgr, "send_inputs_to_window", _send_inputs_to_window)

        ok, _message = await mgr.send_topic_text_to_window(
            user_id=100,
            thread_id=1,
            window_id="@1",
            text="should we change the goal?",
        )

        assert ok is True
        assert len(sent) == 1
        assert "Current native goal objective: New thread goal" in str(sent[0])
        assert exact_resumes == [("@1", "/tmp/proj", "thread-old")]
        assert latest_resumes == []

    @pytest.mark.asyncio
    async def test_remote_host_follow_inherits_resumed_model_before_send(
        self, mgr: SessionManager, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            mgr,
            "_local_machine_identity",
            lambda: ("local-node", "Local"),
        )
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=1,
            codex_thread_id="thread-old",
            window_id="@1",
            cwd="/tmp/proj",
            display_name="proj",
            machine_id="remote-node",
            machine_display_name="Remote",
        )
        mgr.inherit_window_topic_model_selection(
            window_id="@1",
            model_slug="gpt-5.5",
            reasoning_effort="medium",
        )
        mgr.set_topic_sync_mode(100, 1, TOPIC_SYNC_MODE_HOST_FOLLOW_FINAL)
        mgr.get_window_state("@1").cwd = "/tmp/proj"
        sent: list[tuple[str, str]] = []

        exact_resumes: list[tuple[str, dict[str, object]]] = []
        latest_resumes: list[tuple[str, dict[str, object]]] = []

        async def _resume_thread(_machine_id: str, **kwargs):
            exact_resumes.append((_machine_id, kwargs))
            assert kwargs["thread_id"] == "thread-old"
            return {
                "thread_id": "thread-old",
                "turn_id": "",
                "model_slug": "gpt-5.6-sol",
                "reasoning_effort": "ultra",
            }

        async def _resume_latest(_machine_id: str, **kwargs):
            latest_resumes.append((_machine_id, kwargs))
            raise AssertionError("implicit host-follow must not resume latest by cwd")

        async def _send_inputs(_machine_id: str, **kwargs):
            sent.append((kwargs["model_slug"], kwargs["reasoning_effort"]))
            return {
                "ok": True,
                "message": "ok",
                "thread_id": "thread-old",
                "turn_id": "turn-new",
                "transport_epoch": "agent-epoch-1",
                "transport_epoch_started_at": 100.0,
                "transport_generation": 4,
                "transport_reset_sequence": 0,
                "transport_last_reset_generation": 0,
                "transport_last_reset_reason": "",
            }

        monkeypatch.setattr(agent_rpc_mod.agent_rpc_client, "resume_thread", _resume_thread)
        monkeypatch.setattr(agent_rpc_mod.agent_rpc_client, "resume_latest", _resume_latest)
        monkeypatch.setattr(agent_rpc_mod.agent_rpc_client, "send_inputs", _send_inputs)

        ok, message = await mgr.send_topic_text_to_window(
            user_id=100,
            thread_id=1,
            window_id="@1",
            text="continue",
        )

        assert ok is True
        assert message == "ok"
        assert sent == [("gpt-5.6-sol", "ultra")]
        assert exact_resumes == [
            (
                "remote-node",
                {
                    "window_id": "@1",
                    "cwd": "/tmp/proj",
                    "thread_id": "thread-old",
                    "window_name": "proj",
                    "approval_mode": "",
                },
            )
        ]
        assert latest_resumes == []

    @pytest.mark.asyncio
    async def test_remote_oversized_resume_requires_explicit_rebind(
        self, mgr: SessionManager, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            mgr,
            "_local_machine_identity",
            lambda: ("local-node", "Local"),
        )
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=1,
            codex_thread_id="thread-oversized",
            window_id="@1",
            cwd="/tmp/proj",
            display_name="proj",
            machine_id="remote-node",
            machine_display_name="Remote",
        )
        mgr.set_topic_sync_mode(100, 1, TOPIC_SYNC_MODE_HOST_FOLLOW_FINAL)
        mgr.get_window_state("@1").cwd = "/tmp/proj"
        sent: list[dict[str, object]] = []

        exact_resumes: list[tuple[str, dict[str, object]]] = []
        latest_resumes: list[tuple[str, dict[str, object]]] = []

        async def _resume_thread(_machine_id: str, **kwargs):
            exact_resumes.append((_machine_id, kwargs))
            assert kwargs["thread_id"] == "thread-oversized"
            return {
                "thread_id": "",
                "turn_id": "",
                "model_slug": "",
                "reasoning_effort": "",
                "session_start_reason": "oversized_rollover",
                "transport_lifecycle_noop": True,
            }

        async def _resume_latest(_machine_id: str, **kwargs):
            latest_resumes.append((_machine_id, kwargs))
            raise AssertionError("implicit host-follow must not resume latest by cwd")

        async def _send_inputs(_machine_id: str, **kwargs):
            sent.append(kwargs)
            return {
                "ok": True,
                "message": "ok",
                "thread_id": "thread-fresh",
                "turn_id": "turn-fresh",
                "transport_epoch": "agent-epoch-1",
                "transport_epoch_started_at": 100.0,
                "transport_generation": 4,
                "transport_reset_sequence": 0,
                "transport_last_reset_generation": 0,
                "transport_last_reset_reason": "",
            }

        monkeypatch.setattr(
            agent_rpc_mod.agent_rpc_client,
            "resume_thread",
            _resume_thread,
        )
        monkeypatch.setattr(
            agent_rpc_mod.agent_rpc_client,
            "resume_latest",
            _resume_latest,
        )
        monkeypatch.setattr(
            agent_rpc_mod.agent_rpc_client,
            "send_inputs",
            _send_inputs,
        )

        ok, message = await mgr.send_topic_text_to_window(
            user_id=100,
            thread_id=1,
            window_id="@1",
            text="keep this exact request",
        )

        assert ok is False
        assert "explicit /resume" in message
        assert sent == []
        assert mgr.get_window_codex_thread_id("@1") == "thread-oversized"
        assert exact_resumes == [
            (
                "remote-node",
                {
                    "window_id": "@1",
                    "cwd": "/tmp/proj",
                    "thread_id": "thread-oversized",
                    "window_name": "proj",
                    "approval_mode": "",
                },
            )
        ]
        assert latest_resumes == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("with_goal_context", [False, True])
    @pytest.mark.parametrize("returned_thread_id", ["", "thread-unrelated"])
    async def test_remote_send_rejects_mismatched_thread_without_rebinding(
        self,
        mgr: SessionManager,
        monkeypatch,
        with_goal_context: bool,
        returned_thread_id: str,
    ) -> None:
        monkeypatch.setattr(
            mgr,
            "_local_machine_identity",
            lambda: ("local-node", "Local"),
        )
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=1,
            codex_thread_id="thread-canonical",
            window_id="@1",
            cwd="/tmp/proj",
            display_name="proj",
            machine_id="remote-node",
            machine_display_name="Remote",
        )
        mgr.get_window_state("@1").cwd = "/tmp/proj"

        if with_goal_context:
            async def _goal_context(**_kwargs):
                return "Current native goal objective: keep canonical"

            monkeypatch.setattr(mgr, "_build_live_goal_context", _goal_context)

        async def _send_inputs(_machine_id: str, **kwargs):
            assert kwargs["thread_id"] == "thread-canonical"
            return {
                "ok": True,
                "message": "delivered elsewhere",
                "thread_id": returned_thread_id,
                "turn_id": "turn-unrelated",
                "transport_epoch": "agent-epoch-1",
                "transport_epoch_started_at": 100.0,
                "transport_generation": 4,
                "transport_reset_sequence": 0,
                "transport_last_reset_generation": 0,
                "transport_last_reset_reason": "",
            }

        monkeypatch.setattr(agent_rpc_mod.agent_rpc_client, "send_inputs", _send_inputs)

        ok, message = await mgr.send_topic_text_to_window(
            user_id=100,
            thread_id=1,
            window_id="@1",
            text="keep this topic canonical",
        )

        assert ok is False
        assert "will not be replayed automatically" in message
        assert mgr.get_window_codex_thread_id("@1") == "thread-canonical"
        binding = mgr.resolve_topic_binding(100, 1)
        assert binding is not None
        assert binding.codex_thread_id == "thread-canonical"

    @pytest.mark.asyncio
    async def test_remote_send_rejects_window_cache_disagreement_before_dispatch(
        self,
        mgr: SessionManager,
        monkeypatch,
    ) -> None:
        monkeypatch.setattr(
            mgr,
            "_local_machine_identity",
            lambda: ("local-node", "Local"),
        )
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=1,
            codex_thread_id="thread-canonical",
            window_id="@1",
            cwd="/tmp/proj",
            display_name="proj",
            machine_id="remote-node",
            machine_display_name="Remote",
        )
        state = mgr.get_window_state("@1")
        state.codex_thread_id = "thread-stale-cache"

        async def _unexpected_send_inputs(*_args, **_kwargs):
            raise AssertionError("cache disagreement must be rejected before dispatch")

        monkeypatch.setattr(
            agent_rpc_mod.agent_rpc_client,
            "send_inputs",
            _unexpected_send_inputs,
        )

        ok, message = await mgr.send_topic_text_to_window(
            user_id=100,
            thread_id=1,
            window_id="@1",
            text="do not follow stale cache",
        )

        assert ok is False
        assert "binding and window cache disagree" in message
        binding = mgr.resolve_topic_binding(100, 1)
        assert binding is not None
        assert binding.codex_thread_id == "thread-canonical"
        assert state.codex_thread_id == "thread-stale-cache"

    @pytest.mark.asyncio
    async def test_remote_send_does_not_overwrite_explicit_rebind_during_rpc(
        self,
        mgr: SessionManager,
        monkeypatch,
    ) -> None:
        monkeypatch.setattr(
            mgr,
            "_local_machine_identity",
            lambda: ("local-node", "Local"),
        )
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=1,
            codex_thread_id="thread-before",
            window_id="@1",
            cwd="/tmp/proj",
            display_name="proj",
            machine_id="remote-node",
            machine_display_name="Remote",
        )

        async def _send_inputs(_machine_id: str, **kwargs):
            assert kwargs["thread_id"] == "thread-before"
            mgr.bind_topic_to_codex_thread(
                user_id=100,
                thread_id=1,
                codex_thread_id="thread-explicit-new",
                window_id="@1",
                cwd="/tmp/proj",
                display_name="proj",
                machine_id="remote-node",
                machine_display_name="Remote",
            )
            return {
                "ok": True,
                "message": "old request acknowledged",
                "thread_id": "thread-before",
                "turn_id": "turn-before",
                "transport_epoch": "agent-epoch-1",
                "transport_epoch_started_at": 100.0,
                "transport_generation": 4,
                "transport_reset_sequence": 0,
                "transport_last_reset_generation": 0,
                "transport_last_reset_reason": "",
            }

        monkeypatch.setattr(agent_rpc_mod.agent_rpc_client, "send_inputs", _send_inputs)

        ok, message = await mgr.send_topic_text_to_window(
            user_id=100,
            thread_id=1,
            window_id="@1",
            text="in flight",
        )

        assert ok is False
        assert "changed while the request was in flight" in message
        binding = mgr._get_persisted_topic_binding(100, 1)
        assert binding is not None
        assert binding.codex_thread_id == "thread-explicit-new"
        assert mgr.get_window_codex_thread_id("@1") == "thread-explicit-new"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("with_goal_context", [False, True])
    async def test_remote_topic_send_revalidates_ownership_after_goal_context(
        self,
        mgr: SessionManager,
        monkeypatch,
        with_goal_context: bool,
    ) -> None:
        monkeypatch.setattr(
            mgr,
            "_local_machine_identity",
            lambda: ("local-node", "Local"),
        )
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=1,
            codex_thread_id="thread-a",
            window_id="@1",
            cwd="/tmp/proj-a",
            display_name="proj-a",
            machine_id="remote-a",
            machine_display_name="Remote A",
        )

        context_started = asyncio.Event()
        release_context = asyncio.Event()
        sends: list[tuple[str, dict[str, object]]] = []

        async def _build_live_goal_context(**_kwargs: object) -> str:
            context_started.set()
            await release_context.wait()
            return "[coco goal context] live" if with_goal_context else ""

        async def _send_inputs(machine_id: str, **kwargs: object):
            sends.append((machine_id, kwargs))
            return {
                "ok": True,
                "message": "stale request dispatched",
                "thread_id": "thread-a",
                "turn_id": "turn-a",
            }

        monkeypatch.setattr(mgr, "_build_live_goal_context", _build_live_goal_context)
        monkeypatch.setattr(agent_rpc_mod.agent_rpc_client, "send_inputs", _send_inputs)

        task = asyncio.create_task(
            mgr.send_topic_text_to_window(
                user_id=100,
                thread_id=1,
                window_id="@1",
                text="what is my goal?" if with_goal_context else "continue this topic",
            )
        )
        await asyncio.wait_for(context_started.wait(), timeout=1)

        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=1,
            codex_thread_id="thread-b",
            window_id="@1",
            cwd="/tmp/proj-b",
            display_name="proj-b",
            machine_id="remote-b",
            machine_display_name="Remote B",
        )
        release_context.set()
        ok, message = await task

        assert ok is False
        assert message
        assert sends == []
        binding = mgr._get_persisted_topic_binding(100, 1)
        assert binding is not None
        assert binding.codex_thread_id == "thread-b"
        assert binding.machine_id == "remote-b"
        assert binding.cwd == "/tmp/proj-b"
        assert mgr.get_window_codex_thread_id("@1") == "thread-b"

    @pytest.mark.asyncio
    async def test_remote_send_does_not_treat_cache_as_missing_topic_authority(
        self,
        mgr: SessionManager,
        monkeypatch,
    ) -> None:
        monkeypatch.setattr(
            mgr,
            "_local_machine_identity",
            lambda: ("local-node", "Local"),
        )
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=1,
            codex_thread_id="placeholder",
            window_id="@1",
            cwd="/tmp/proj",
            display_name="proj",
            machine_id="remote-node",
            machine_display_name="Remote",
        )
        raw_binding = mgr.topic_bindings_v2[100]["1"]
        raw_binding.codex_thread_id = ""
        state = mgr.get_window_state("@1")
        state.codex_thread_id = "thread-stale-cache"

        async def _unexpected_send_inputs(*_args, **_kwargs):
            raise AssertionError("an unbound topic must not trust the window cache")

        monkeypatch.setattr(
            agent_rpc_mod.agent_rpc_client,
            "send_inputs",
            _unexpected_send_inputs,
        )

        ok, message = await mgr.send_topic_text_to_window(
            user_id=100,
            thread_id=1,
            window_id="@1",
            text="do not adopt the stale cache",
        )

        assert ok is False
        assert "No canonical Codex thread is persisted" in message
        assert raw_binding.codex_thread_id == ""
        assert state.codex_thread_id == "thread-stale-cache"

    @pytest.mark.asyncio
    async def test_topic_send_requires_raw_persisted_binding(
        self,
        mgr: SessionManager,
        monkeypatch,
    ) -> None:
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=1,
            codex_thread_id="thread-old-cache",
            window_id="@1",
            cwd="/tmp/proj",
            display_name="proj",
        )
        del mgr.topic_bindings_v2[100]["1"]

        async def _unexpected_send(*_args, **_kwargs):
            raise AssertionError("missing raw binding must be rejected")

        monkeypatch.setattr(mgr, "send_to_window", _unexpected_send)

        ok, message = await mgr.send_topic_text_to_window(
            user_id=100,
            thread_id=1,
            window_id="@1",
            text="do not use orphaned cache",
        )

        assert ok is False
        assert "No persisted topic binding" in message
        assert mgr.get_window_codex_thread_id("@1") == "thread-old-cache"

    @pytest.mark.asyncio
    async def test_empty_window_cache_repair_does_not_bind_other_topics(
        self,
        mgr: SessionManager,
        monkeypatch,
    ) -> None:
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=1,
            codex_thread_id="thread-canonical",
            window_id="@1",
            cwd="/tmp/proj",
            display_name="proj",
        )
        mgr.bind_thread(100, 2, "@1")
        other_binding = mgr.topic_bindings_v2[100]["2"]
        other_binding.codex_thread_id = ""
        mgr.get_window_state("@1").codex_thread_id = ""

        async def _send_to_window(*_args, **_kwargs):
            return True, "ok"

        monkeypatch.setattr(mgr, "send_to_window", _send_to_window)

        ok, message = await mgr.send_topic_text_to_window(
            user_id=100,
            thread_id=1,
            window_id="@1",
            text="repair only my cache",
        )

        assert ok is True
        assert message == "ok"
        assert mgr.get_window_codex_thread_id("@1") == "thread-canonical"
        assert other_binding.codex_thread_id == ""

    @pytest.mark.asyncio
    async def test_remote_host_follow_deferred_resume_returns_clean_failure(
        self,
        mgr: SessionManager,
        monkeypatch,
    ) -> None:
        monkeypatch.setattr(
            mgr,
            "_local_machine_identity",
            lambda: ("local-node", "Local"),
        )
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=1,
            codex_thread_id="thread-old",
            window_id="@1",
            cwd="/tmp/proj",
            display_name="proj",
            machine_id="remote-node",
            machine_display_name="Remote",
        )
        mgr.set_topic_sync_mode(100, 1, TOPIC_SYNC_MODE_HOST_FOLLOW_FINAL)
        mgr.set_window_codex_active_turn_id("@1", "turn-old")
        uncertainty_calls: list[tuple[set[str], str]] = []
        mgr.set_transport_uncertainty_handler(
            lambda window_ids, reason: uncertainty_calls.append((window_ids, reason))
        )

        exact_resumes: list[tuple[str, dict[str, object]]] = []
        latest_resumes: list[tuple[str, dict[str, object]]] = []

        async def _resume_thread(_machine_id: str, **kwargs):
            exact_resumes.append((_machine_id, kwargs))
            assert kwargs["thread_id"] == "thread-old"
            raise agent_rpc_mod.RemoteCodexMutationDeferredError(
                "Remote Codex exact resume was not dispatched because "
                "transport replacement confirmation is pending"
            )

        async def _resume_latest(_machine_id: str, **kwargs):
            latest_resumes.append((_machine_id, kwargs))
            raise AssertionError("implicit host-follow must not resume latest by cwd")

        monkeypatch.setattr(
            agent_rpc_mod.agent_rpc_client,
            "resume_thread",
            _resume_thread,
        )
        monkeypatch.setattr(
            agent_rpc_mod.agent_rpc_client,
            "resume_latest",
            _resume_latest,
        )

        ok, message = await mgr.send_topic_text_to_window(
            user_id=100,
            thread_id=1,
            window_id="@1",
            text="continue",
        )

        assert ok is False
        assert "was not dispatched" in message
        assert exact_resumes == [
            (
                "remote-node",
                {
                    "window_id": "@1",
                    "cwd": "/tmp/proj",
                    "thread_id": "thread-old",
                    "window_name": "proj",
                    "approval_mode": "",
                },
            )
        ]
        assert latest_resumes == []
        assert uncertainty_calls == []
        assert mgr.get_window_codex_active_turn_id("@1") == "turn-old"
        assert mgr.get_topic_sync_mode(100, 1) == TOPIC_SYNC_MODE_HOST_FOLLOW_FINAL


@pytest.mark.asyncio
async def test_missing_thread_recovery_resumes_stale_bound_thread_not_latest(
    mgr: SessionManager,
    monkeypatch,
) -> None:
    canonical_thread_id = "thread-stale-canonical"
    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=1,
        codex_thread_id=canonical_thread_id,
        window_id="@1",
        cwd="/tmp/proj",
        display_name="proj",
    )
    mgr.get_window_state("@1").cwd = "/tmp/proj"

    exact_resumes: list[tuple[str, str, str]] = []
    latest_resumes: list[tuple[str, str]] = []
    send_thread_ids: list[str] = []

    async def _send_inputs_via_codex_app_server(
        *,
        window_id: str,
        inputs: list[dict[str, object]],
        steer: bool,
        window_name: str,
        cwd: str,
        **_kwargs: object,
    ) -> tuple[bool, str]:
        _ = inputs, steer, window_name, cwd
        send_thread_ids.append(mgr.get_window_codex_thread_id(window_id))
        if len(send_thread_ids) == 1:
            raise session_mod.CodexAppServerError("thread not found")
        return True, "recovered"

    async def _resume_exact(*, window_id: str, cwd: str, thread_id: str) -> str:
        exact_resumes.append((window_id, cwd, thread_id))
        mgr.set_window_codex_thread_id(window_id, thread_id)
        return thread_id

    async def _resume_latest(*, window_id: str, cwd: str) -> str:
        latest_resumes.append((window_id, cwd))
        mgr.set_window_codex_thread_id(window_id, "thread-unrelated-latest")
        return "thread-unrelated-latest"

    monkeypatch.setattr(
        mgr,
        "_send_inputs_via_codex_app_server",
        _send_inputs_via_codex_app_server,
    )
    monkeypatch.setattr(mgr, "resume_codex_session_for_window", _resume_exact)
    monkeypatch.setattr(
        mgr,
        "resume_latest_codex_session_for_window",
        _resume_latest,
    )

    ok, message = await mgr.send_inputs_to_window(
        "@1",
        [{"type": "text", "text": "retry this request"}],
    )

    assert ok is True
    assert message == "recovered"
    assert exact_resumes == [("@1", "/tmp/proj", canonical_thread_id)]
    assert latest_resumes == []
    assert send_thread_ids == [canonical_thread_id, canonical_thread_id]


@pytest.mark.asyncio
async def test_remote_send_rpc_failure_marks_transport_uncertain(
    mgr: SessionManager,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        mgr,
        "_local_machine_identity",
        lambda: ("local-node", "Local"),
    )
    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=1,
        codex_thread_id="thread-old",
        window_id="@1",
        cwd="/tmp/proj",
        display_name="proj",
        machine_id="remote-node",
        machine_display_name="Remote",
    )
    mgr.set_window_codex_active_turn_id("@1", "turn-old")
    uncertainty_calls: list[tuple[set[str], str]] = []
    mgr.set_transport_uncertainty_handler(
        lambda window_ids, reason: uncertainty_calls.append((window_ids, reason))
    )

    async def _send_inputs(_machine_id: str, **_kwargs):
        raise TimeoutError("RPC acknowledgement timed out")

    monkeypatch.setattr(agent_rpc_mod.agent_rpc_client, "send_inputs", _send_inputs)

    with pytest.raises(TimeoutError, match="acknowledgement"):
        await mgr.send_topic_text_to_window(
            user_id=100,
            thread_id=1,
            window_id="@1",
            text="continue",
        )

    assert uncertainty_calls == [({"@1"}, "remote_send_rpc_failed")]
    assert mgr.get_window_codex_active_turn_id("@1") == ""


@pytest.mark.asyncio
async def test_remote_send_deferred_before_dispatch_preserves_session_state(
    mgr: SessionManager,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        mgr,
        "_local_machine_identity",
        lambda: ("local-node", "Local"),
    )
    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=1,
        codex_thread_id="thread-old",
        window_id="@1",
        cwd="/tmp/proj",
        display_name="proj",
        machine_id="remote-node",
        machine_display_name="Remote",
    )
    mgr.set_window_codex_active_turn_id("@1", "turn-old")
    uncertainty_calls: list[tuple[set[str], str]] = []
    mgr.set_transport_uncertainty_handler(
        lambda window_ids, reason: uncertainty_calls.append((window_ids, reason))
    )
    deferred_error = getattr(
        agent_rpc_mod,
        "RemoteCodexMutationDeferredError",
        ClusterRpcError,
    )

    async def _send_inputs(_machine_id: str, **_kwargs):
        raise deferred_error(
            "Remote Codex send was not dispatched because transport "
            "replacement confirmation is pending"
        )

    monkeypatch.setattr(agent_rpc_mod.agent_rpc_client, "send_inputs", _send_inputs)

    ok, message = await mgr.send_topic_text_to_window(
        user_id=100,
        thread_id=1,
        window_id="@1",
        text="continue",
    )

    assert ok is False
    assert "was not dispatched" in message
    assert uncertainty_calls == []
    assert mgr.get_window_codex_active_turn_id("@1") == "turn-old"


@pytest.mark.asyncio
async def test_stale_remote_send_response_cannot_restore_cleared_turn(
    mgr: SessionManager,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        mgr,
        "_local_machine_identity",
        lambda: ("local-node", "Local"),
    )
    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=1,
        codex_thread_id="thread-old",
        window_id="@1",
        cwd="/tmp/proj",
        display_name="proj",
        machine_id="remote-node",
        machine_display_name="Remote",
    )
    uncertainty_calls: list[tuple[set[str], str]] = []
    mgr.set_transport_uncertainty_handler(
        lambda window_ids, reason: uncertainty_calls.append((window_ids, reason))
    )

    async def _reject_stale_result(
        window_id: str,
        _result: dict[str, object],
    ) -> bool:
        mgr._note_transport_uncertainty(
            window_ids={window_id},
            reason="stale_remote_send_response",
        )
        return False

    mgr.set_remote_transport_result_handler(
        _reject_stale_result,
    )

    async def _send_inputs(_machine_id: str, **_kwargs):
        return {
            "ok": True,
            "message": "ok",
            "thread_id": "thread-new",
            "turn_id": "turn-stale",
            "transport_epoch": "agent-epoch-1",
            "transport_epoch_started_at": 100.0,
            "transport_generation": 7,
            "transport_reset_sequence": 1,
            "transport_last_reset_generation": 7,
            "transport_last_reset_reason": "request_timeout:turn/start",
        }

    monkeypatch.setattr(agent_rpc_mod.agent_rpc_client, "send_inputs", _send_inputs)

    ok, message = await mgr.send_topic_text_to_window(
        user_id=100,
        thread_id=1,
        window_id="@1",
        text="continue",
    )

    assert ok is False
    assert "transport changed before acknowledgement" in message.lower()
    assert mgr.get_window_codex_active_turn_id("@1") == ""
    assert uncertainty_calls == [({"@1"}, "stale_remote_send_response")]


@pytest.mark.asyncio
async def test_local_topic_send_forwards_originating_topic_model_selection(
    mgr: SessionManager, monkeypatch
) -> None:
    monkeypatch.setattr(mgr, "_codex_app_server_mode_enabled", lambda: True)
    monkeypatch.setattr(mgr, "_local_machine_identity", lambda: ("local-node", "Local"))
    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=1,
        chat_id=-100123,
        codex_thread_id="thread-shared",
        window_id="@1",
        cwd="/tmp/proj",
        display_name="proj",
        machine_id="local-node",
    )
    mgr.set_topic_model_selection(
        100,
        1,
        chat_id=-100123,
        model_slug="gpt-5.5",
        reasoning_effort="medium",
    )
    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=2,
        chat_id=-100123,
        codex_thread_id="thread-shared",
        window_id="@1",
        cwd="/tmp/proj",
        display_name="proj",
        machine_id="local-node",
    )
    mgr.set_topic_model_selection(
        100,
        2,
        chat_id=-100123,
        model_slug="gpt-5.6-sol",
        reasoning_effort="ultra",
    )
    captured: list[tuple[str, str]] = []

    async def _send_to_window(
        window_id: str,
        text: str,
        *,
        steer: bool = False,
        force_new_turn: bool = False,
        model_slug: str = "",
        reasoning_effort: str = "",
        service_tier: str = "",
        **_kwargs: object,
    ):
        _ = window_id, text, steer, force_new_turn, service_tier
        captured.append((model_slug, reasoning_effort))
        return True, "ok"

    monkeypatch.setattr(mgr, "send_to_window", _send_to_window)

    ok, message = await mgr.send_topic_text_to_window(
        user_id=100,
        thread_id=2,
        chat_id=-100123,
        window_id="@1",
        text="continue",
    )

    assert ok is True
    assert message == "ok"
    assert captured == [("gpt-5.6-sol", "ultra")]


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


def test_sync_resumed_session_preserves_explicit_topic_model_selection(
    mgr: SessionManager, monkeypatch
) -> None:
    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=1,
        chat_id=-100123,
        codex_thread_id="thread-old",
        window_id="@1",
        cwd="/tmp/proj",
        display_name="proj",
    )
    mgr.set_topic_model_selection(
        100,
        1,
        chat_id=-100123,
        model_slug="gpt-5.6-sol",
        reasoning_effort="ultra",
    )
    monkeypatch.setattr(
        mgr,
        "get_codex_session_model_selection_for_thread",
        lambda _thread_id, *, cwd="": ("gpt-5.5", "medium"),
    )

    changed, model_slug, reasoning_effort = (
        mgr.sync_window_topic_model_selection_from_codex_session(
            window_id="@1",
            codex_thread_id="thread-resumed",
            cwd="/tmp/proj",
        )
    )

    binding = mgr.resolve_topic_binding(100, 1, chat_id=-100123)
    assert changed is False
    assert (model_slug, reasoning_effort) == ("gpt-5.5", "medium")
    assert binding is not None
    assert binding.model_slug == "gpt-5.6-sol"
    assert binding.reasoning_effort == "ultra"


def test_later_resume_replaces_previously_inherited_model_selection(
    mgr: SessionManager, monkeypatch
) -> None:
    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=1,
        chat_id=-100123,
        codex_thread_id="thread-a",
        window_id="@1",
        cwd="/tmp/proj",
        display_name="proj",
    )
    selections = iter(
        [
            ("gpt-5.5", "medium"),
            ("gpt-5.6-sol", "ultra"),
        ]
    )
    monkeypatch.setattr(
        mgr,
        "get_codex_session_model_selection_for_thread",
        lambda _thread_id, *, cwd="": next(selections),
    )

    mgr.sync_window_topic_model_selection_from_codex_session(
        window_id="@1",
        codex_thread_id="thread-a",
        cwd="/tmp/proj",
    )
    mgr.sync_window_topic_model_selection_from_codex_session(
        window_id="@1",
        codex_thread_id="thread-b",
        cwd="/tmp/proj",
    )

    binding = mgr.resolve_topic_binding(100, 1, chat_id=-100123)
    assert binding is not None
    assert binding.model_slug == "gpt-5.6-sol"
    assert binding.reasoning_effort == "ultra"


def test_reselecting_inherited_values_marks_topic_selection_explicit(
    mgr: SessionManager
) -> None:
    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=1,
        chat_id=-100123,
        codex_thread_id="thread-a",
        window_id="@1",
        cwd="/tmp/proj",
        display_name="proj",
    )
    mgr.inherit_window_topic_model_selection(
        window_id="@1",
        model_slug="gpt-5.5",
        reasoning_effort="medium",
    )

    changed = mgr.set_topic_model_selection(
        100,
        1,
        chat_id=-100123,
        model_slug="gpt-5.5",
        reasoning_effort="medium",
    )

    binding = mgr.resolve_topic_binding(100, 1, chat_id=-100123)
    assert changed is True
    assert binding is not None
    assert binding.model_selection_explicit is True


def test_legacy_topic_model_selection_defaults_to_explicit() -> None:
    binding = session_mod.TopicBinding.from_dict(
        {
            "model_slug": "gpt-5.5",
            "reasoning_effort": "medium",
        }
    )

    assert binding.model_selection_explicit is True


@pytest.mark.asyncio
async def test_get_topic_goal_reads_existing_codex_thread(
    mgr: SessionManager, monkeypatch
) -> None:
    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=1,
        chat_id=-100123,
        codex_thread_id="thread-1",
        window_id="@1",
        cwd="/tmp/proj",
        display_name="proj",
    )
    calls: list[str] = []

    async def _thread_goal_get(*, thread_id: str):
        calls.append(thread_id)
        return {"goal": {"objective": "Ship the goal feature", "status": "active"}}

    monkeypatch.setattr(session_mod.codex_app_server_client, "thread_goal_get", _thread_goal_get)

    ok, payload, message = await mgr.get_topic_goal(
        user_id=100,
        thread_id=1,
        chat_id=-100123,
    )

    assert ok is True
    assert payload == {"goal": {"objective": "Ship the goal feature", "status": "active"}}
    assert message == ""
    assert calls == ["thread-1"]


@pytest.mark.asyncio
async def test_set_topic_goal_creates_thread_for_window_when_missing(
    mgr: SessionManager, monkeypatch
) -> None:
    mgr.bind_thread(100, 1, "@1", window_name="proj")
    mgr.get_window_state("@1").cwd = "/tmp/proj"
    raw_binding = mgr._get_persisted_topic_binding(100, 1)
    assert raw_binding is not None
    raw_binding.cwd = "/tmp/proj"
    ensure_calls: list[tuple[str, str]] = []
    set_calls: list[tuple[str, str]] = []
    latest_resume_calls: list[tuple[str, str]] = []

    async def _ensure_codex_thread_for_window(*, window_id: str, cwd: str, **_kwargs):
        ensure_calls.append((window_id, cwd))
        assert _kwargs["sync_topic_bindings"] is False
        mgr._set_window_codex_thread_cache(window_id, "thread-new")
        return "thread-new", "on-request"

    async def _thread_goal_set(*, thread_id: str, goal: str):
        set_calls.append((thread_id, goal))
        return {"goal": {"objective": goal, "status": "active"}}

    async def _resume_latest_codex_session_for_window(*, window_id: str, cwd: str):
        latest_resume_calls.append((window_id, cwd))
        return ""

    monkeypatch.setattr(mgr, "_ensure_codex_thread_for_window", _ensure_codex_thread_for_window)
    monkeypatch.setattr(
        mgr,
        "resume_latest_codex_session_for_window",
        _resume_latest_codex_session_for_window,
    )
    monkeypatch.setattr(session_mod.codex_app_server_client, "thread_goal_set", _thread_goal_set)

    ok, payload, message = await mgr.set_topic_goal(
        user_id=100,
        thread_id=1,
        goal_text="Ship the goal feature",
    )

    assert ok is True
    assert payload == {"goal": {"objective": "Ship the goal feature", "status": "active"}}
    assert message == ""
    assert ensure_calls == [("@1", "/tmp/proj")]
    assert set_calls == [("thread-new", "Ship the goal feature")]
    assert latest_resume_calls == []


@pytest.mark.asyncio
async def test_goal_resolution_does_not_trust_unbound_window_cache(
    mgr: SessionManager, monkeypatch
) -> None:
    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=1,
        codex_thread_id="placeholder",
        window_id="@1",
        cwd="/tmp/proj",
        display_name="proj",
    )
    raw_binding = mgr.topic_bindings_v2[100]["1"]
    raw_binding.codex_thread_id = ""
    mgr.get_window_state("@1").codex_thread_id = "thread-stale-cache"

    async def _unexpected(*_args, **_kwargs):
        raise AssertionError("goal resolution must not trust the window cache")

    monkeypatch.setattr(mgr, "resume_codex_session_for_window", _unexpected)
    monkeypatch.setattr(mgr, "_ensure_codex_thread_for_window", _unexpected)

    thread_id, error = await mgr.resolve_goal_thread_for_topic(
        user_id=100,
        thread_id=1,
        create=True,
    )

    assert thread_id == ""
    assert "No canonical Codex thread is persisted" in error
    assert raw_binding.codex_thread_id == ""
    assert mgr.get_window_codex_thread_id("@1") == "thread-stale-cache"


@pytest.mark.asyncio
async def test_goal_resolution_does_not_use_stale_window_cwd_for_empty_binding(
    mgr: SessionManager, monkeypatch
) -> None:
    """Implicit goal recovery must not create a thread in a cached workspace."""
    window_id = "@920010"
    state = mgr.get_window_state(window_id)
    state.cwd = "/workspace/stale-cache"
    state.window_name = "demo"
    mgr.bind_thread(100, 1, window_id, window_name="demo")
    raw_binding = mgr._get_persisted_topic_binding(100, 1)
    assert raw_binding is not None
    assert raw_binding.codex_thread_id == ""
    raw_binding.cwd = ""

    ensure_calls: list[tuple[str, str]] = []
    resume_calls: list[tuple[str, str, str]] = []

    async def _ensure_codex_thread_for_window(**kwargs: object) -> tuple[str, str]:
        ensure_calls.append((str(kwargs["window_id"]), str(kwargs["cwd"])))
        return "thread-created-in-stale-cache", "on-request"

    async def _resume_codex_session_for_window(
        *, window_id: str, cwd: str, thread_id: str
    ) -> str:
        resume_calls.append((window_id, cwd, thread_id))
        return thread_id

    monkeypatch.setattr(
        mgr,
        "_ensure_codex_thread_for_window",
        _ensure_codex_thread_for_window,
    )
    monkeypatch.setattr(
        mgr,
        "resume_codex_session_for_window",
        _resume_codex_session_for_window,
    )

    thread_id, error = await mgr.resolve_goal_thread_for_topic(
        user_id=100,
        thread_id=1,
        create=True,
    )

    assert thread_id == ""
    assert "No workspace is bound to this topic" in error
    assert ensure_calls == []
    assert resume_calls == []
    binding = mgr._get_persisted_topic_binding(100, 1)
    assert binding is not None
    assert binding.cwd == ""
    assert binding.codex_thread_id == ""
    assert state.cwd == "/workspace/stale-cache"
    assert state.codex_thread_id == ""


@pytest.mark.asyncio
async def test_goal_resolution_requires_raw_persisted_binding(
    mgr: SessionManager, monkeypatch
) -> None:
    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=1,
        codex_thread_id="thread-old-cache",
        window_id="@1",
        cwd="/tmp/proj",
        display_name="proj",
    )
    resolved_fallback = mgr.resolve_topic_binding(100, 1)
    assert resolved_fallback is not None
    del mgr.topic_bindings_v2[100]["1"]
    monkeypatch.setattr(
        mgr,
        "resolve_topic_binding",
        lambda *_args, **_kwargs: resolved_fallback,
    )

    thread_id, error = await mgr.resolve_goal_thread_for_topic(
        user_id=100,
        thread_id=1,
        create=True,
    )

    assert thread_id == ""
    assert "No persisted topic binding" in error


@pytest.mark.asyncio
async def test_set_topic_goal_retries_after_missing_goal_error_on_stale_thread(
    mgr: SessionManager, monkeypatch
) -> None:
    mgr.bind_thread(100, 1, "@1", window_name="proj")
    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=1,
        chat_id=-100123,
        codex_thread_id="thread-stale",
        window_id="@1",
        cwd="/tmp/proj",
        display_name="proj",
    )
    mgr.get_window_state("@1").cwd = "/tmp/proj"
    mgr.set_window_codex_thread_id("@1", "thread-stale")
    set_calls: list[tuple[str, str]] = []
    exact_resume_calls: list[tuple[str, str, str]] = []
    latest_resume_calls: list[tuple[str, str]] = []

    async def _resume_codex_session_for_window(
        *, window_id: str, cwd: str, thread_id: str
    ):
        exact_resume_calls.append((window_id, cwd, thread_id))
        assert thread_id == "thread-stale"
        mgr.set_window_codex_thread_id(window_id, thread_id)
        return thread_id

    async def _resume_latest_codex_session_for_window(*, window_id: str, cwd: str):
        latest_resume_calls.append((window_id, cwd))
        raise AssertionError("goal refresh must not resume latest by cwd")

    async def _thread_goal_set(*, thread_id: str, goal: str):
        set_calls.append((thread_id, goal))
        if len(set_calls) == 1:
            raise session_mod.CodexAppServerError(
                f"cannot update goal for thread {thread_id}: no goal exists"
            )
        return {"goal": {"objective": goal, "status": "active"}}

    monkeypatch.setattr(mgr, "resume_codex_session_for_window", _resume_codex_session_for_window)
    monkeypatch.setattr(mgr, "resume_latest_codex_session_for_window", _resume_latest_codex_session_for_window)
    monkeypatch.setattr(session_mod.codex_app_server_client, "thread_goal_set", _thread_goal_set)

    ok, payload, message = await mgr.set_topic_goal(
        user_id=100,
        thread_id=1,
        chat_id=-100123,
        goal_text="Ship the goal feature",
    )

    assert ok is True
    assert payload == {"goal": {"objective": "Ship the goal feature", "status": "active"}}
    assert message == ""
    assert set_calls == [
        ("thread-stale", "Ship the goal feature"),
        ("thread-stale", "Ship the goal feature"),
    ]
    assert exact_resume_calls == [("@1", "/tmp/proj", "thread-stale")]
    assert latest_resume_calls == []
    binding = mgr.resolve_topic_binding(100, 1, chat_id=-100123)
    assert binding is not None
    assert binding.codex_thread_id == "thread-stale"


@pytest.mark.asyncio
async def test_get_topic_goal_reads_remote_machine_binding(
    mgr: SessionManager, monkeypatch
) -> None:
    monkeypatch.setattr(mgr, "_local_machine_identity", lambda: ("local-node", "Local Node"))
    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=1,
        chat_id=-100123,
        codex_thread_id="thread-1",
        window_id="@1",
        cwd="/tmp/proj",
        display_name="proj",
        machine_id="remote-node",
        machine_display_name="Remote Node",
    )

    async def _thread_goal_get(machine_id: str, *, thread_id: str):
        assert machine_id == "remote-node"
        assert thread_id == "thread-1"
        return {"goal": {"objective": "Remote goal", "status": "active"}}

    monkeypatch.setattr(agent_rpc_mod.agent_rpc_client, "thread_goal_get", _thread_goal_get)

    ok, payload, message = await mgr.get_topic_goal(
        user_id=100,
        thread_id=1,
        chat_id=-100123,
    )

    assert ok is True
    assert payload == {"goal": {"objective": "Remote goal", "status": "active"}}
    assert message == ""


@pytest.mark.asyncio
async def test_set_topic_goal_creates_remote_thread_when_missing(
    mgr: SessionManager, monkeypatch
) -> None:
    monkeypatch.setattr(mgr, "_local_machine_identity", lambda: ("local-node", "Local Node"))
    mgr.bind_thread(100, 1, "@1", window_name="proj")
    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=1,
        chat_id=-100123,
        codex_thread_id="placeholder",
        window_id="@1",
        cwd="/tmp/proj",
        display_name="proj",
        machine_id="remote-node",
        machine_display_name="Remote Node",
    )
    binding = mgr.resolve_topic_binding(100, 1, chat_id=-100123)
    assert binding is not None
    binding.codex_thread_id = ""
    mgr.set_window_codex_thread_id("@1", "")
    latest_resume_calls: list[tuple[str, dict[str, object]]] = []

    async def _resume_latest(
        machine_id: str,
        *,
        window_id: str,
        cwd: str,
        window_name: str = "",
        approval_mode: str = "",
    ):
        latest_resume_calls.append((machine_id, {
            "window_id": window_id,
            "cwd": cwd,
            "window_name": window_name,
            "approval_mode": approval_mode,
        }))
        return {"thread_id": ""}

    async def _ensure_thread(
        machine_id: str,
        *,
        window_id: str,
        cwd: str,
        window_name: str = "",
        approval_mode: str = "",
        model_slug: str = "",
        reasoning_effort: str = "",
        service_tier: str = "",
    ):
        assert machine_id == "remote-node"
        assert window_id == "@1"
        assert cwd == "/tmp/proj"
        _ = window_name, approval_mode, model_slug, reasoning_effort, service_tier
        return {"thread_id": "remote-thread-1"}

    async def _thread_goal_set(machine_id: str, *, thread_id: str, goal: str):
        assert machine_id == "remote-node"
        assert thread_id == "remote-thread-1"
        assert goal == "Ship remote goal"
        return {"goal": {"objective": goal, "status": "active"}}

    monkeypatch.setattr(agent_rpc_mod.agent_rpc_client, "resume_latest", _resume_latest)
    monkeypatch.setattr(agent_rpc_mod.agent_rpc_client, "ensure_thread", _ensure_thread)
    monkeypatch.setattr(agent_rpc_mod.agent_rpc_client, "thread_goal_set", _thread_goal_set)

    ok, payload, message = await mgr.set_topic_goal(
        user_id=100,
        thread_id=1,
        chat_id=-100123,
        goal_text="Ship remote goal",
    )

    assert ok is True
    assert payload == {"goal": {"objective": "Ship remote goal", "status": "active"}}
    assert message == ""
    binding = mgr.resolve_topic_binding(100, 1, chat_id=-100123)
    assert binding is not None
    assert binding.codex_thread_id == "remote-thread-1"
    assert latest_resume_calls == []


@pytest.mark.asyncio
async def test_remote_goal_refresh_inherits_resumed_model_selection(
    mgr: SessionManager, monkeypatch
) -> None:
    monkeypatch.setattr(mgr, "_local_machine_identity", lambda: ("local-node", "Local"))
    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=1,
        chat_id=-100123,
        codex_thread_id="thread-old",
        window_id="@1",
        cwd="/tmp/proj",
        display_name="proj",
        machine_id="remote-node",
        machine_display_name="Remote",
    )
    mgr.inherit_window_topic_model_selection(
        window_id="@1",
        model_slug="gpt-5.5",
        reasoning_effort="medium",
    )

    exact_resume_calls: list[tuple[str, dict[str, object]]] = []
    latest_resume_calls: list[tuple[str, dict[str, object]]] = []

    async def _resume_thread(_machine_id: str, **kwargs):
        exact_resume_calls.append((_machine_id, kwargs))
        assert kwargs["thread_id"] == "thread-old"
        return {
            "thread_id": "thread-old",
            "model_slug": "gpt-5.6-sol",
            "reasoning_effort": "ultra",
        }

    async def _resume_latest(_machine_id: str, **_kwargs):
        latest_resume_calls.append((_machine_id, _kwargs))
        raise AssertionError("remote goal refresh must not resume latest by cwd")

    monkeypatch.setattr(agent_rpc_mod.agent_rpc_client, "resume_thread", _resume_thread)
    monkeypatch.setattr(agent_rpc_mod.agent_rpc_client, "resume_latest", _resume_latest)

    thread_id, error = await mgr.resolve_goal_thread_for_topic(
        user_id=100,
        thread_id=1,
        chat_id=-100123,
        create=True,
        force_refresh=True,
    )

    binding = mgr.resolve_topic_binding(100, 1, chat_id=-100123)
    assert error == ""
    assert thread_id == "thread-old"
    assert binding is not None
    assert exact_resume_calls == [
        (
            "remote-node",
            {
                "window_id": "@1",
                "cwd": "/tmp/proj",
                "thread_id": "thread-old",
                "window_name": "proj",
                "approval_mode": "",
            },
        )
    ]
    assert latest_resume_calls == []
    assert binding.codex_thread_id == "thread-old"
    assert binding.model_slug == "gpt-5.6-sol"
    assert binding.reasoning_effort == "ultra"


@pytest.mark.asyncio
async def test_remote_goal_exact_resume_drops_stale_result_after_explicit_rebind(
    mgr: SessionManager, monkeypatch
) -> None:
    monkeypatch.setattr(
        mgr,
        "_local_machine_identity",
        lambda: ("local-node", "Local"),
    )
    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=1,
        codex_thread_id="thread-a",
        window_id="@1",
        cwd="/tmp/proj",
        display_name="proj",
        machine_id="remote-a",
        machine_display_name="Remote A",
    )

    resume_started = asyncio.Event()
    release_resume = asyncio.Event()

    async def _resume_thread(machine_id: str, **kwargs: object):
        assert machine_id == "remote-a"
        assert kwargs["thread_id"] == "thread-a"
        resume_started.set()
        await release_resume.wait()
        return {"thread_id": "thread-a"}

    monkeypatch.setattr(
        agent_rpc_mod.agent_rpc_client,
        "resume_thread",
        _resume_thread,
    )

    task = asyncio.create_task(
        mgr.resolve_goal_thread_for_topic(
            user_id=100,
            thread_id=1,
            create=True,
            force_refresh=True,
        )
    )
    await asyncio.wait_for(resume_started.wait(), timeout=1)

    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=1,
        codex_thread_id="thread-b",
        window_id="@1",
        cwd="/tmp/proj-b",
        display_name="proj-b",
        machine_id="remote-b",
        machine_display_name="Remote B",
    )
    release_resume.set()
    thread_id, error = await task

    assert thread_id == ""
    assert error
    binding = mgr._get_persisted_topic_binding(100, 1)
    assert binding is not None
    assert binding.codex_thread_id == "thread-b"
    assert binding.machine_id == "remote-b"
    assert mgr.get_window_codex_thread_id("@1") == "thread-b"


@pytest.mark.asyncio
async def test_remote_goal_ensure_drops_stale_result_after_explicit_rebind(
    mgr: SessionManager, monkeypatch
) -> None:
    monkeypatch.setattr(
        mgr,
        "_local_machine_identity",
        lambda: ("local-node", "Local"),
    )
    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=1,
        codex_thread_id="placeholder",
        window_id="@1",
        cwd="/tmp/proj",
        display_name="proj",
        machine_id="remote-a",
        machine_display_name="Remote A",
    )
    raw_binding = mgr._get_persisted_topic_binding(100, 1)
    assert raw_binding is not None
    raw_binding.codex_thread_id = ""
    mgr.set_window_codex_thread_id("@1", "")

    ensure_started = asyncio.Event()
    release_ensure = asyncio.Event()

    async def _ensure_thread(machine_id: str, **kwargs: object):
        assert machine_id == "remote-a"
        assert kwargs["window_id"] == "@1"
        ensure_started.set()
        await release_ensure.wait()
        return {"thread_id": "thread-a"}

    monkeypatch.setattr(
        agent_rpc_mod.agent_rpc_client,
        "ensure_thread",
        _ensure_thread,
    )

    task = asyncio.create_task(
        mgr.resolve_goal_thread_for_topic(
            user_id=100,
            thread_id=1,
            create=True,
        )
    )
    await asyncio.wait_for(ensure_started.wait(), timeout=1)

    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=1,
        codex_thread_id="thread-b",
        window_id="@1",
        cwd="/tmp/proj-b",
        display_name="proj-b",
        machine_id="remote-b",
        machine_display_name="Remote B",
    )
    release_ensure.set()
    thread_id, error = await task

    assert thread_id == ""
    assert error
    binding = mgr._get_persisted_topic_binding(100, 1)
    assert binding is not None
    assert binding.codex_thread_id == "thread-b"
    assert binding.machine_id == "remote-b"
    assert mgr.get_window_codex_thread_id("@1") == "thread-b"


@pytest.mark.asyncio
async def test_local_goal_creation_revalidates_empty_raw_binding_before_goal_mutation(
    mgr: SessionManager, monkeypatch
) -> None:
    monkeypatch.setattr(
        mgr,
        "_local_machine_identity",
        lambda: ("local-node", "Local"),
    )
    mgr.bind_thread(100, 1, "@1", window_name="proj-a")
    mgr.get_window_state("@1").cwd = "/tmp/proj-a"
    raw_binding = mgr._get_persisted_topic_binding(100, 1)
    assert raw_binding is not None
    raw_binding.cwd = "/tmp/proj-a"
    assert raw_binding.codex_thread_id == ""
    assert mgr.get_window_codex_thread_id("@1") == ""

    thread_start_started = asyncio.Event()
    release_thread_start = asyncio.Event()
    goal_calls: list[tuple[str, str]] = []

    async def _thread_start(**_kwargs: object):
        thread_start_started.set()
        await release_thread_start.wait()
        return {"thread": {"id": "thread-a"}}

    async def _thread_goal_set(*, thread_id: str, goal: str):
        goal_calls.append((thread_id, goal))
        return {"goal": {"objective": goal, "status": "active"}}

    monkeypatch.setattr(session_mod.codex_app_server_client, "thread_start", _thread_start)
    monkeypatch.setattr(session_mod.codex_app_server_client, "thread_goal_set", _thread_goal_set)

    task = asyncio.create_task(
        mgr.set_topic_goal(
            user_id=100,
            thread_id=1,
            goal_text="Ship topic B",
        )
    )
    await asyncio.wait_for(thread_start_started.wait(), timeout=1)

    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=1,
        codex_thread_id="thread-b",
        window_id="@1",
        cwd="/tmp/proj-b",
        display_name="proj-b",
        machine_id="local-node",
        machine_display_name="Local",
    )
    release_thread_start.set()
    ok, payload, message = await task

    assert ok is False
    assert payload is None
    assert message
    assert goal_calls == []
    binding = mgr._get_persisted_topic_binding(100, 1)
    assert binding is not None
    assert binding.codex_thread_id == "thread-b"
    assert binding.cwd == "/tmp/proj-b"
    assert mgr.get_window_codex_thread_id("@1") == "thread-b"


@pytest.mark.asyncio
async def test_local_goal_refresh_drops_stale_result_after_explicit_rebind(
    mgr: SessionManager, monkeypatch
) -> None:
    monkeypatch.setattr(
        mgr,
        "_local_machine_identity",
        lambda: ("local-node", "Local"),
    )
    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=1,
        codex_thread_id="thread-a",
        window_id="@900070",
        cwd="/tmp/proj-a",
        display_name="proj-a",
        machine_id="local-node",
        machine_display_name="Local",
    )

    resume_started = asyncio.Event()
    release_resume = asyncio.Event()
    goal_calls: list[tuple[str, str]] = []

    async def _resume_exact(*, window_id: str, cwd: str, thread_id: str) -> str:
        assert (window_id, cwd, thread_id) == (
            "@900070",
            "/tmp/proj-a",
            "thread-a",
        )
        resume_started.set()
        await release_resume.wait()
        return thread_id

    async def _thread_goal_set(*, thread_id: str, goal: str):
        goal_calls.append((thread_id, goal))
        if len(goal_calls) == 1:
            raise session_mod.CodexAppServerError(
                f"cannot update goal for thread {thread_id}: no goal exists"
            )
        return {"goal": {"objective": goal, "status": "active"}}

    monkeypatch.setattr(mgr, "resume_codex_session_for_window", _resume_exact)
    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "thread_goal_set",
        _thread_goal_set,
    )

    task = asyncio.create_task(
        mgr.set_topic_goal(
            user_id=100,
            thread_id=1,
            goal_text="Ship topic B",
        )
    )
    await asyncio.wait_for(resume_started.wait(), timeout=1)

    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=1,
        codex_thread_id="thread-b",
        window_id="@900071",
        cwd="/tmp/proj-b",
        display_name="proj-b",
        machine_id="local-node",
        machine_display_name="Local",
    )
    release_resume.set()
    ok, payload, message = await task

    assert ok is False
    assert payload is None
    assert message
    assert goal_calls == [("thread-a", "Ship topic B")]
    binding = mgr._get_persisted_topic_binding(100, 1)
    assert binding is not None
    assert binding.codex_thread_id == "thread-b"
    assert binding.window_id == "@900071"
    assert binding.cwd == "/tmp/proj-b"
    assert binding.machine_id == "local-node"
    assert mgr.get_window_codex_thread_id("@900070") == "thread-a"
    assert mgr.get_window_codex_thread_id("@900071") == "thread-b"


@pytest.mark.asyncio
async def test_set_topic_goal_retry_revalidates_binding_after_lifecycle_rebind(
    mgr: SessionManager, monkeypatch
) -> None:
    """A goal retry must not reuse the first transport's machine closure."""
    monkeypatch.setattr(
        mgr,
        "_local_machine_identity",
        lambda: ("local-node", "Local"),
    )
    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=1,
        chat_id=-100123,
        codex_thread_id="thread-topic",
        window_id="@machine-a",
        cwd="/tmp/proj-a",
        display_name="proj-a",
        machine_id="machine-a",
        machine_display_name="Machine A",
    )

    first_goal_attempt = asyncio.Event()
    release_first_goal = asyncio.Event()
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()
    goal_calls: list[tuple[str, str, str]] = []
    refresh_calls: list[tuple[str, dict[str, object]]] = []

    async def _thread_goal_set(
        machine_id: str, *, thread_id: str, goal: str
    ) -> dict[str, object]:
        goal_calls.append((machine_id, thread_id, goal))
        if len(goal_calls) == 1:
            first_goal_attempt.set()
            await release_first_goal.wait()
            raise session_mod.CodexAppServerError(
                f"cannot update goal for thread {thread_id}: no goal exists"
            )
        return {"goal": {"objective": goal, "status": "active"}}

    async def _resume_thread(machine_id: str, **kwargs: object) -> dict[str, str]:
        refresh_calls.append((machine_id, kwargs))
        refresh_started.set()
        await release_refresh.wait()
        return {"thread_id": "thread-topic"}

    monkeypatch.setattr(agent_rpc_mod.agent_rpc_client, "thread_goal_set", _thread_goal_set)
    monkeypatch.setattr(agent_rpc_mod.agent_rpc_client, "resume_thread", _resume_thread)

    task = asyncio.create_task(
        mgr.set_topic_goal(
            user_id=100,
            thread_id=1,
            chat_id=-100123,
            goal_text="Ship the topic goal",
        )
    )
    await asyncio.wait_for(first_goal_attempt.wait(), timeout=1)

    # The lifecycle rebind happens while the first mutation is still in flight;
    # force-refresh must use this complete binding on every subsequent await.
    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=1,
        chat_id=-100123,
        codex_thread_id="thread-topic",
        window_id="@machine-b",
        cwd="/tmp/proj-b",
        display_name="proj-b",
        machine_id="machine-b",
        machine_display_name="Machine B",
    )
    release_first_goal.set()
    await asyncio.wait_for(refresh_started.wait(), timeout=1)
    assert refresh_calls == [
        (
            "machine-b",
            {
                "window_id": "@machine-b",
                "cwd": "/tmp/proj-b",
                "thread_id": "thread-topic",
                "window_name": "proj-b",
                "approval_mode": "",
            },
        )
    ]
    release_refresh.set()

    ok, payload, message = await task

    # A valid implementation may retry through B or abort after noticing the
    # ownership transition, but it must never issue a second mutation to A.
    assert goal_calls[0] == ("machine-a", "thread-topic", "Ship the topic goal")
    assert all(machine_id != "machine-a" for machine_id, _thread_id, _goal in goal_calls[1:])
    if ok:
        assert goal_calls == [
            ("machine-a", "thread-topic", "Ship the topic goal"),
            ("machine-b", "thread-topic", "Ship the topic goal"),
        ]
        assert payload == {
            "goal": {"objective": "Ship the topic goal", "status": "active"}
        }
        assert message == ""
    else:
        assert payload is None
        assert message

    binding = mgr._get_persisted_topic_binding(100, 1, chat_id=-100123)
    assert binding is not None
    assert binding.transport == session_mod.TOPIC_BINDING_TRANSPORT_CODEX_THREAD
    assert binding.chat_id == -100123
    assert binding.thread_id == 1
    assert binding.codex_thread_id == "thread-topic"
    assert binding.window_id == "@machine-b"
    assert binding.cwd == "/tmp/proj-b"
    assert binding.display_name == "proj-b"
    assert binding.machine_id == "machine-b"
    assert binding.machine_display_name == "Machine B"


class TestRuntimeCapabilityHint:
    def test_runtime_hint_includes_telegram_attachment_protocol(self):
        hint = SessionManager._build_runtime_capability_hint(
            workspace_path="/tmp/demo",
            can_write=True,
            approval_policy="on-request",
            tts_available=True,
            tts_default_voice="F2",
            tts_default_speed=1.4,
            transcription_runtime_label="compatible -> cpu / int8 / base",
        )

        assert "Workspace: /tmp/demo" in hint
        assert "Speech-to-text: compatible -> cpu / int8 / base" in hint
        assert "Text-to-speech: available (voice `F2`, speed `1.4`)" in hint
        assert "<telegram-attachment path=" in hint
        assert ".pdf" in hint
        assert ".txt" in hint
        assert ".md" in hint
        assert ".png" in hint
        assert ".jpg" in hint
        assert ".jpeg" in hint
        assert ".webp" in hint
        assert ".mp4" in hint
        assert ".webm" in hint


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


class TestCocoControlTopic:
    def test_set_and_get_coco_control_topic_roundtrip(self, mgr: SessionManager) -> None:
        mgr.ensure_topic_binding(100, 1, chat_id=-100123)

        binding = mgr.set_coco_control_topic(100, 1, chat_id=-100123)

        assert binding is not None
        assert mgr.is_coco_control_topic(100, 1, chat_id=-100123) is True
        control_topic = mgr.get_coco_control_topic()
        assert control_topic is not None
        assert control_topic.user_id == 100
        assert control_topic.chat_id == -100123
        assert control_topic.thread_id == 1

    def test_named_topic_cannot_replace_general_control(
        self,
        mgr: SessionManager,
    ) -> None:
        mgr.set_coco_control_topic(100, 1, chat_id=-100123)

        binding = mgr.set_coco_control_topic(100, 77, chat_id=-100123)

        assert binding is None
        assert mgr.is_coco_control_topic(100, 1, chat_id=-100123)
        assert not mgr.is_coco_control_topic(100, 77, chat_id=-100123)

    def test_general_control_is_independent_per_group_and_shared_by_allowed_users(
        self,
        mgr: SessionManager,
    ) -> None:
        first = mgr.set_coco_control_topic(100, 1, chat_id=-100123)
        second = mgr.set_coco_control_topic(200, 1, chat_id=-100456)

        assert first is not None
        assert second is not None
        assert mgr.get_coco_control_topic(-100123) == session_mod.CocoControlTopic(
            user_id=100,
            thread_id=1,
            chat_id=-100123,
        )
        assert mgr.get_coco_control_topic(-100456) == session_mod.CocoControlTopic(
            user_id=200,
            thread_id=1,
            chat_id=-100456,
        )
        assert mgr.is_coco_control_topic(999, 1, chat_id=-100123)
        assert mgr.is_coco_control_topic(999, 1, chat_id=-100456)
        assert not mgr.is_coco_control_topic(100, 1, chat_id=-100999)

    def test_general_binding_resolution_does_not_alias_non_owner(
        self,
        mgr: SessionManager,
    ) -> None:
        mgr.set_coco_control_topic(100, 1, chat_id=-100123)
        state = mgr.get_window_state("@42")
        state.cwd = "/internal/control"
        mgr.bind_thread(100, 1, "@42", chat_id=-100123)

        owner_resolved = mgr.resolve_topic_binding(100, 1, chat_id=-100123)
        non_owner_resolved = mgr.resolve_topic_binding(999, 1, chat_id=-100123)

        assert owner_resolved is not None
        assert owner_resolved.window_id == "@42"
        assert owner_resolved.cwd == "/internal/control"
        assert non_owner_resolved is None
        assert mgr.resolve_window_for_thread(999, 1, chat_id=-100123) is None

    def test_non_owner_general_lookup_and_config_stay_in_callers_scope(
        self,
        mgr: SessionManager,
    ) -> None:
        chat_id = -100123
        owner_id = 100
        caller_id = 999
        mgr.set_coco_control_topic(owner_id, 1, chat_id=chat_id)
        mgr.bind_thread(owner_id, 1, "@42", chat_id=chat_id)

        assert mgr.resolve_topic_binding(caller_id, 1, chat_id=chat_id) is None

        caller_binding = mgr.ensure_topic_binding(caller_id, 1, chat_id=chat_id)

        assert caller_binding is not None
        assert caller_id in mgr.topic_bindings_v2
        assert caller_binding is not mgr.topic_bindings_v2[owner_id][f"{chat_id}:1"]

        changed = mgr.set_topic_response_mode(
            caller_id,
            1,
            chat_id=chat_id,
            response_mode="voice",
        )

        assert changed is True
        assert mgr.get_topic_response_mode(owner_id, 1, chat_id=chat_id) == "text"
        assert mgr.get_topic_response_mode(caller_id, 1, chat_id=chat_id) == "voice"

    def test_owner_general_setting_mutates_owner_binding(
        self,
        mgr: SessionManager,
    ) -> None:
        owner_binding = mgr.set_coco_control_topic(100, 1, chat_id=-100123)
        assert owner_binding is not None

        changed = mgr.set_topic_response_mode(
            100,
            1,
            chat_id=-100123,
            response_mode="voice",
        )

        assert changed is True
        persisted = mgr._get_persisted_topic_binding(100, 1, chat_id=-100123)
        assert persisted is owner_binding
        assert persisted.response_mode == "voice"

    def test_existing_group_control_owner_cannot_be_replaced(
        self,
        mgr: SessionManager,
    ) -> None:
        original = mgr.set_coco_control_topic(100, 1, chat_id=-100123)

        repeated = mgr.set_coco_control_topic(200, 1, chat_id=-100123)

        assert repeated == original
        assert mgr.get_coco_control_topic(-100123) == session_mod.CocoControlTopic(
            user_id=100,
            thread_id=1,
            chat_id=-100123,
        )
        assert mgr.resolve_topic_binding(200, 1, chat_id=-100123) is None
        assert 200 not in mgr.topic_bindings_v2

    def test_set_control_recreates_binding_for_orphaned_reservation(
        self,
        mgr: SessionManager,
    ) -> None:
        chat_id = -100123
        mgr.coco_control_topics[chat_id] = session_mod.CocoControlTopic(
            user_id=100,
            thread_id=1,
            chat_id=chat_id,
        )

        binding = mgr.set_coco_control_topic(999, 1, chat_id=chat_id)

        assert binding is not None
        assert binding.chat_id == chat_id
        assert binding.thread_id == 1
        assert mgr.resolve_topic_binding(100, 1, chat_id=chat_id) == binding
        assert mgr.resolve_topic_binding(999, 1, chat_id=chat_id) is None
        assert 999 not in mgr.topic_bindings_v2

    def test_set_control_archives_existing_cross_user_general_history(
        self,
        mgr: SessionManager,
    ) -> None:
        mgr.bind_topic_to_codex_thread(
            user_id=200,
            thread_id=1,
            chat_id=-100123,
            codex_thread_id="existing-general",
            cwd="/existing/general",
            window_id="@20",
        )

        binding = mgr.set_coco_control_topic(100, 1, chat_id=-100123)

        assert binding is not None
        assert binding.codex_thread_id == ""
        assert mgr.get_coco_control_topic(-100123) == session_mod.CocoControlTopic(
            user_id=100,
            thread_id=1,
            chat_id=-100123,
        )
        assert mgr.resolve_topic_binding(100, 1, chat_id=-100123) == binding
        assert list(mgr.coco_control_archives.values())[0].codex_thread_id == (
            "existing-general"
        )

    def test_coco_control_topic_persists_across_save_and_load(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(session_mod.config, "state_file", tmp_path / "state.json")
        monkeypatch.setattr(session_mod.config, "sessions_path", tmp_path / "sessions")
        monkeypatch.setattr(session_mod.config, "machine_id", "local-node")
        monkeypatch.setattr(session_mod.config, "machine_name", "Local Node")
        monkeypatch.setattr(session_mod.node_registry, "get_node", lambda _mid: None)

        mgr = SessionManager()
        mgr.ensure_topic_binding(100, 1, chat_id=-100123)
        mgr.set_coco_control_topic(100, 1, chat_id=-100123)

        reloaded = SessionManager()
        control_topic = reloaded.get_coco_control_topic(-100123)

        assert control_topic is not None
        assert control_topic.user_id == 100
        assert control_topic.chat_id == -100123
        assert control_topic.thread_id == 1

    def test_legacy_singleton_control_state_migrates_to_per_group_map(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        state_file = tmp_path / "state.json"
        state_file.write_text(
            json.dumps(
                {
                    "state_schema_version": 6,
                    "coco_control_topic": {
                        "user_id": 100,
                        "thread_id": 77,
                        "chat_id": -100123,
                    },
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(session_mod.config, "state_file", state_file)
        monkeypatch.setattr(session_mod.config, "sessions_path", tmp_path / "sessions")

        reloaded = SessionManager()

        assert reloaded.get_coco_control_topic(-100123) == session_mod.CocoControlTopic(
            user_id=100,
            thread_id=77,
            chat_id=-100123,
        )
        persisted = json.loads(state_file.read_text(encoding="utf-8"))
        assert persisted["coco_control_topics"] == {
            "-100123": {
                "user_id": 100,
                "thread_id": 77,
                "chat_id": -100123,
            }
        }
        assert "coco_control_topic" not in persisted

    def test_migrate_coco_control_to_general_moves_history_and_topic_settings(
        self,
        mgr: SessionManager,
        tmp_path: Path,
    ) -> None:
        chat_id = -100123
        user_id = 100
        old_thread_id = 77
        old_window_id = "@42"
        neutral_workspace = tmp_path / "_coco" / "chat-100123-thread-1"

        state = mgr.get_window_state(old_window_id)
        state.cwd = "/projects/old-control"
        state.window_name = "old-control"
        state.codex_thread_id = "codex-history-123"
        mgr.bind_topic_to_codex_thread(
            user_id=user_id,
            thread_id=old_thread_id,
            chat_id=chat_id,
            codex_thread_id="codex-history-123",
            cwd=state.cwd,
            display_name=state.window_name,
            window_id=old_window_id,
        )
        mgr.set_thread_skills(user_id, old_thread_id, ["ops"], chat_id=chat_id)
        mgr.set_thread_codex_skills(
            user_id,
            old_thread_id,
            ["reviewer"],
            chat_id=chat_id,
        )
        mgr.coco_control_topic = session_mod.CocoControlTopic(
            user_id=user_id,
            thread_id=old_thread_id,
            chat_id=chat_id,
        )
        mgr._save_state()

        migration = mgr.migrate_coco_control_to_general(
            workspace_dir=str(neutral_workspace),
            general_thread_id=1,
        )

        assert migration is not None
        assert migration.user_id == user_id
        assert migration.chat_id == chat_id
        assert migration.previous_thread_id == old_thread_id
        assert migration.general_thread_id == 1
        assert migration.moved_history is True
        assert mgr.resolve_topic_binding(user_id, old_thread_id, chat_id=chat_id) is None
        general = mgr.resolve_topic_binding(user_id, 1, chat_id=chat_id)
        assert general is not None
        assert general.window_id == old_window_id
        assert general.codex_thread_id == "codex-history-123"
        assert general.cwd == str(neutral_workspace)
        assert general.display_name == "coco-control"
        assert mgr.get_thread_skills(user_id, 1, chat_id=chat_id) == ["ops"]
        assert mgr.get_thread_codex_skills(user_id, 1, chat_id=chat_id) == [
            "reviewer"
        ]
        assert mgr.get_thread_skills(user_id, old_thread_id, chat_id=chat_id) == []
        migrated_state = mgr.get_window_state(old_window_id)
        assert migrated_state.codex_thread_id == "codex-history-123"
        assert migrated_state.cwd == str(neutral_workspace)
        assert migrated_state.window_name == "coco-control"
        assert mgr.is_coco_control_topic(user_id, 1, chat_id=chat_id)

    def test_migrate_coco_control_preserves_remote_native_workspace_path(
        self,
        mgr: SessionManager,
    ) -> None:
        chat_id = -100123
        remote_workspace = r"C:\Users\runner\.coco\chat-100123\control"
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=77,
            chat_id=chat_id,
            codex_thread_id="remote-history",
            cwd=r"C:\projects\legacy",
            window_id="@77",
            machine_id="windows-agent",
        )
        mgr.coco_control_topic = session_mod.CocoControlTopic(100, 77, chat_id)

        migration = mgr.migrate_coco_control_to_general(
            chat_id=chat_id,
            workspace_dir=remote_workspace,
        )

        assert migration is not None and not migration.conflict
        general = mgr.resolve_topic_binding(100, 1, chat_id=chat_id)
        assert general is not None
        assert general.cwd == remote_workspace
        assert mgr.get_window_state(general.window_id).cwd == remote_workspace

    def test_migrate_coco_control_preserves_distinct_existing_general_history(
        self,
        mgr: SessionManager,
        tmp_path: Path,
    ) -> None:
        chat_id = -100123
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=77,
            chat_id=chat_id,
            codex_thread_id="legacy-history",
            cwd="/projects/legacy",
            display_name="legacy",
            window_id="@77",
        )
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=1,
            chat_id=chat_id,
            codex_thread_id="general-history",
            cwd="/projects/general",
            display_name="general",
            window_id="@1",
        )
        mgr.coco_control_topic = session_mod.CocoControlTopic(100, 77, chat_id)

        migration = mgr.migrate_coco_control_to_general(
            chat_id=chat_id,
            workspace_dir=str(tmp_path / "control"),
        )

        assert migration is not None
        assert migration.conflict is True
        legacy = mgr.resolve_topic_binding(100, 77, chat_id=chat_id)
        general = mgr.resolve_topic_binding(100, 1, chat_id=chat_id)
        assert legacy is not None and legacy.codex_thread_id == "legacy-history"
        assert general is not None and general.codex_thread_id == "general-history"
        assert general.cwd == "/projects/general"
        assert mgr.get_window_state("@77").cwd == "/projects/legacy"
        assert mgr.is_coco_control_topic(999, 1, chat_id=chat_id)

    @pytest.mark.asyncio
    async def test_stale_cleanup_preserves_archived_legacy_control_binding(
        self,
        mgr: SessionManager,
        tmp_path: Path,
    ) -> None:
        chat_id = -100123
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=77,
            chat_id=chat_id,
            codex_thread_id="legacy-history",
            cwd="/projects/legacy",
            window_id="legacy-pane",
        )
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=1,
            chat_id=chat_id,
            codex_thread_id="general-history",
            cwd="/projects/general",
            window_id="@1",
        )
        mgr.coco_control_topic = session_mod.CocoControlTopic(100, 77, chat_id)
        migration = mgr.migrate_coco_control_to_general(
            chat_id=chat_id,
            workspace_dir=str(tmp_path / "control"),
        )
        assert migration is not None and migration.conflict

        await mgr.resolve_stale_ids()

        archived = mgr._get_persisted_topic_binding(100, 77, chat_id=chat_id)
        assert archived is not None
        assert archived.codex_thread_id == "legacy-history"
        assert archived.window_id.startswith("@")

    def test_migrate_coco_control_preserves_window_backed_general_session(
        self,
        mgr: SessionManager,
        tmp_path: Path,
    ) -> None:
        chat_id = -100123
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=77,
            chat_id=chat_id,
            codex_thread_id="legacy-history",
            cwd="/projects/legacy",
            display_name="legacy",
            window_id="@77",
        )
        general_state = mgr.get_window_state("@1")
        general_state.cwd = "/projects/general"
        general_state.window_name = "general"
        mgr.bind_thread(
            100,
            1,
            "@1",
            window_name="general",
            chat_id=chat_id,
        )
        mgr.coco_control_topic = session_mod.CocoControlTopic(100, 77, chat_id)

        migration = mgr.migrate_coco_control_to_general(
            chat_id=chat_id,
            workspace_dir=str(tmp_path / "control"),
        )

        assert migration is not None and migration.conflict
        legacy = mgr.resolve_topic_binding(100, 77, chat_id=chat_id)
        general = mgr.resolve_topic_binding(100, 1, chat_id=chat_id)
        assert legacy is not None and legacy.codex_thread_id == "legacy-history"
        assert general is not None and general.window_id == "@1"
        assert general.cwd == "/projects/general"
        assert general.codex_thread_id == ""

    def test_migrate_control_detects_general_history_owned_by_another_user(
        self,
        mgr: SessionManager,
        tmp_path: Path,
    ) -> None:
        chat_id = -100123
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=77,
            chat_id=chat_id,
            codex_thread_id="legacy-history",
            cwd="/legacy",
            window_id="@77",
        )
        mgr.bind_topic_to_codex_thread(
            user_id=200,
            thread_id=1,
            chat_id=chat_id,
            codex_thread_id="existing-general",
            cwd="/general",
            window_id="@1",
        )
        mgr.coco_control_topic = session_mod.CocoControlTopic(100, 77, chat_id)

        migration = mgr.migrate_coco_control_to_general(
            chat_id=chat_id,
            workspace_dir=str(tmp_path / "control"),
        )

        assert migration is not None and migration.conflict
        assert mgr.get_coco_control_topic(chat_id) == session_mod.CocoControlTopic(
            200, 1, chat_id
        )
        assert mgr.resolve_topic_binding(100, 77, chat_id=chat_id).codex_thread_id == "legacy-history"
        assert mgr.resolve_topic_binding(200, 1, chat_id=chat_id).codex_thread_id == "existing-general"

    def test_migration_does_not_rewrite_a_window_shared_with_another_topic(
        self,
        mgr: SessionManager,
        tmp_path: Path,
    ) -> None:
        chat_id = -100123
        for thread_id in (77, 88):
            mgr.bind_topic_to_codex_thread(
                user_id=100,
                thread_id=thread_id,
                chat_id=chat_id,
                codex_thread_id="shared-history",
                cwd="/shared-project",
                window_id="@42",
            )
        mgr.coco_control_topic = session_mod.CocoControlTopic(100, 77, chat_id)

        migration = mgr.migrate_coco_control_to_general(
            chat_id=chat_id,
            workspace_dir=str(tmp_path / "control"),
        )

        assert migration is not None and not migration.conflict
        sibling = mgr.resolve_topic_binding(100, 88, chat_id=chat_id)
        general = mgr.resolve_topic_binding(100, 1, chat_id=chat_id)
        assert sibling is not None and sibling.window_id == "@42"
        assert mgr.get_window_state("@42").cwd == "/shared-project"
        assert general is not None and general.window_id != "@42"
        assert general.codex_thread_id == "shared-history"

    def test_migration_notice_outbox_persists_failures_until_acknowledged(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        state_file = tmp_path / "state.json"
        monkeypatch.setattr(session_mod.config, "state_file", state_file)
        monkeypatch.setattr(session_mod.config, "sessions_path", tmp_path / "sessions")
        mgr = SessionManager()
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=77,
            chat_id=-100123,
            codex_thread_id="legacy-history",
            cwd="/projects/legacy",
            display_name="legacy",
            window_id="@77",
        )
        mgr.coco_control_topic = session_mod.CocoControlTopic(100, 77, -100123)

        migration = mgr.migrate_coco_control_to_general(
            chat_id=-100123,
            workspace_dir=str(tmp_path / "_coco" / "chat-100123" / "control"),
        )

        assert migration is not None
        notices = list(mgr.iter_pending_coco_control_notices())
        assert [notice.thread_id for notice in notices] == [77, 1]
        failed_id = notices[0].notice_id
        assert mgr.record_coco_control_notice_failure(failed_id, "network down")

        reloaded = SessionManager()
        pending = {
            notice.notice_id: notice
            for notice in reloaded.iter_pending_coco_control_notices()
        }
        assert pending[failed_id].attempts == 1
        assert pending[failed_id].last_error == "network down"
        assert pending[failed_id].next_attempt_at > 0
        assert reloaded.acknowledge_coco_control_notice(failed_id)
        assert failed_id not in {
            notice.notice_id for notice in reloaded.iter_pending_coco_control_notices()
        }


class TestResolveWindowForThread:
    def test_none_thread_id_returns_none(self, mgr: SessionManager) -> None:
        assert mgr.resolve_window_for_thread(100, None) is None

    def test_unbound_thread_returns_none(self, mgr: SessionManager) -> None:
        assert mgr.resolve_window_for_thread(100, 42) is None

    def test_bound_thread_returns_window(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 42, "@3")
        assert mgr.resolve_window_for_thread(100, 42) == "@3"


class TestSessionDirectPathCache:
    @pytest.mark.asyncio
    async def test_get_session_direct_reuses_resolved_file_path(
        self, mgr: SessionManager, monkeypatch, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        sessions_root = tmp_path / "sessions"
        sessions_dir = sessions_root / "2026" / "06" / "11"
        sessions_dir.mkdir(parents=True)
        session_id = "019eb7f2-0a01-7023-a382-194ec2966267"
        session_file = sessions_dir / f"rollout-2026-06-11T18-29-13-{session_id}.jsonl"
        session_file.write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {"id": session_id, "cwd": str(workspace)},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        class _CountingSessionsPath:
            def __init__(self, root: Path) -> None:
                self.root = root
                self.glob_calls = 0

            def glob(self, pattern: str):
                self.glob_calls += 1
                return self.root.glob(pattern)

        counting_root = _CountingSessionsPath(sessions_root)
        monkeypatch.setattr(session_mod.config, "sessions_path", counting_root)

        assert await mgr._get_session_direct(session_id, str(workspace)) is not None
        assert await mgr._get_session_direct(session_id, str(workspace)) is not None
        assert counting_root.glob_calls == 1


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


class TestAutodiscoverBoundWindows:
    def test_current_session_map_omits_noncanonical_session(
        self, mgr: SessionManager
    ) -> None:
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=1,
            codex_thread_id="thread-topic-canonical",
            window_id="@1",
            cwd="/tmp/proj",
        )
        mgr.get_window_state("@1").session_id = "thread-unrelated"

        assert mgr.current_window_session_map() == {}

    def test_current_session_map_omits_topic_without_canonical_thread(
        self, mgr: SessionManager
    ) -> None:
        mgr.bind_thread(100, 1, "@1")
        state = mgr.get_window_state("@1")
        state.cwd = "/tmp/proj"
        state.session_id = "thread-unrelated"

        assert mgr.current_window_session_map() == {}

    def test_autodiscover_preserves_canonical_codex_thread_over_newer_summary(
        self, mgr: SessionManager, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        canonical_thread_id = "thread-topic-canonical"
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=1,
            codex_thread_id=canonical_thread_id,
            window_id="@1",
            cwd=str(workspace),
            display_name="proj",
        )
        state = mgr.get_window_state("@1")
        state.session_id = "stale-session-id"

        summaries = [
            CodexSessionSummary(
                thread_id=canonical_thread_id,
                file_path=workspace / "canonical.jsonl",
                created_at=10.0,
                last_active_at=10.0,
            ),
            CodexSessionSummary(
                thread_id="thread-unrelated-newer",
                file_path=workspace / "newer.jsonl",
                created_at=20.0,
                last_active_at=20.0,
            ),
        ]

        assert mgr._autodiscover_session_for_window_from_summaries("@1", summaries)
        assert state.session_id == canonical_thread_id

    def test_autodiscover_resolves_canonical_thread_outside_summary_window(
        self, mgr: SessionManager, monkeypatch, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        canonical_thread_id = "thread-topic-canonical"
        canonical_rollout = workspace / "canonical.jsonl"
        canonical_rollout.write_text("{}\n", encoding="utf-8")
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=1,
            codex_thread_id=canonical_thread_id,
            window_id="@1",
            cwd=str(workspace),
            display_name="proj",
        )
        state = mgr.get_window_state("@1")
        state.session_id = "thread-unrelated-newer"

        def _find_exact(thread_id: str, *, cwd: str = ""):
            assert thread_id == canonical_thread_id
            assert cwd == str(workspace)
            return canonical_rollout

        monkeypatch.setattr(mgr, "_find_codex_session_file_for_thread", _find_exact)
        summaries = [
            CodexSessionSummary(
                thread_id="thread-unrelated-newer",
                file_path=workspace / "newer.jsonl",
                created_at=20.0,
                last_active_at=20.0,
            )
        ]

        assert mgr._autodiscover_session_for_window_from_summaries("@1", summaries)
        assert state.session_id == canonical_thread_id

        # A later discovery must keep the authoritative topic thread instead of
        # replacing it with an unrelated newer transcript in the same cwd.
        assert mgr._autodiscover_session_for_window_from_summaries("@1", summaries)
        assert state.session_id == canonical_thread_id

    @pytest.mark.asyncio
    async def test_reuses_session_summary_lookup_for_windows_in_same_cwd(
        self, mgr: SessionManager, monkeypatch, tmp_path
    ) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        mgr.bind_thread(100, 1, "@1")
        mgr.bind_thread(100, 2, "@2")
        mgr.get_window_state("@1").cwd = str(workspace)
        mgr.get_window_state("@1").last_input_ts = 10.0
        mgr.get_window_state("@2").cwd = str(workspace)
        mgr.get_window_state("@2").last_input_ts = 10.0

        calls: list[str] = []

        def fake_list_summaries(cwd: str, *, limit: int = 100):
            calls.append(cwd)
            return [
                CodexSessionSummary(
                    thread_id="latest-session",
                    file_path=workspace / "rollout.jsonl",
                    created_at=11.0,
                    last_active_at=12.0,
                )
            ]

        monkeypatch.setattr(
            mgr,
            "list_codex_session_summaries_for_cwd",
            fake_list_summaries,
        )

        await mgr.autodiscover_sessions_for_bound_windows()

        assert calls == [str(workspace)]
        assert mgr.get_window_state("@1").session_id == ""
        assert mgr.get_window_state("@2").session_id == ""


class TestFindUsersForSession:
    @pytest.mark.asyncio
    async def test_ignores_noncanonical_window_session(
        self, mgr: SessionManager
    ) -> None:
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=1,
            codex_thread_id="thread-topic-canonical",
            window_id="@1",
            cwd="/tmp/proj",
        )
        mgr.get_window_state("@1").session_id = "thread-unrelated"

        assert await mgr.find_users_for_session("thread-unrelated") == []

    @pytest.mark.asyncio
    async def test_uses_raw_topic_binding_when_window_cache_disagrees(
        self, mgr: SessionManager
    ) -> None:
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=1,
            codex_thread_id="thread-topic-canonical",
            window_id="@1",
            cwd="/tmp/proj",
        )
        state = mgr.get_window_state("@1")
        state.codex_thread_id = "thread-unrelated"
        state.session_id = "thread-unrelated"

        assert await mgr.find_users_for_session("thread-unrelated") == []
        assert await mgr.find_users_for_session("thread-topic-canonical") == []

    def test_codex_thread_routing_does_not_use_unbound_window_cache(
        self, mgr: SessionManager
    ) -> None:
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=1,
            codex_thread_id="placeholder",
            window_id="@1",
            cwd="/tmp/proj",
        )
        raw_binding = mgr.topic_bindings_v2[100]["1"]
        raw_binding.codex_thread_id = ""
        mgr.get_window_state("@1").codex_thread_id = "thread-window-cache"

        assert mgr.find_users_for_codex_thread("thread-window-cache") == []

    @pytest.mark.asyncio
    async def test_repairs_noncanonical_cache_when_canonical_session_arrives(
        self, mgr: SessionManager, monkeypatch
    ) -> None:
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=1,
            codex_thread_id="thread-topic-canonical",
            window_id="@1",
            cwd="/tmp/proj",
        )
        mgr.get_window_state("@1").session_id = "thread-unrelated"
        calls: list[str] = []

        async def _autodiscover(window_id: str) -> bool:
            calls.append(window_id)
            mgr.get_window_state(window_id).session_id = "thread-topic-canonical"
            return True

        monkeypatch.setattr(mgr, "autodiscover_session_for_window", _autodiscover)

        assert await mgr.find_users_for_session("thread-topic-canonical") == [
            (100, None, "@1", 1)
        ]
        assert calls == ["@1"]

    @pytest.mark.asyncio
    async def test_uses_in_memory_session_ids(
        self, mgr: SessionManager, monkeypatch
    ) -> None:
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=1,
            codex_thread_id="s1",
            window_id="@1",
        )
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=2,
            codex_thread_id="s2",
            window_id="@2",
        )
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
        mgr.bind_topic_to_codex_thread(
            user_id=100,
            thread_id=1,
            codex_thread_id="new-session",
            window_id="@1",
        )
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


@pytest.mark.parametrize("payload", [[], None, "bad", 7])
def test_load_state_recovers_from_non_object_json(monkeypatch, tmp_path, payload):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(session_mod.config, "state_file", state_file)

    loaded = SessionManager()

    assert loaded.window_states == {}
    assert loaded.topic_bindings_v2 == {}


def test_load_state_salvages_valid_entries_from_malformed_maps(monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "window_states": {
                    "@bad": None,
                    "@2": {"cwd": None, "window_name": 7},
                },
                "user_window_offsets": {
                    "bad-user": {},
                    "100": {"@bad": "bad", "@2": 5},
                },
                "window_display_names": [],
                "group_chat_ids": {"valid": -100, "bad": "not-an-id"},
                "topic_bindings_v2": {
                    "100": {
                        "3": {
                            "window_id": None,
                            "codex_thread_id": None,
                            "cwd": None,
                            "display_name": None,
                            "machine_id": None,
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(session_mod.config, "state_file", state_file)

    loaded = SessionManager()

    assert loaded.get_window_state("@2").cwd == ""
    assert loaded.get_window_state("@2").window_name == ""
    assert loaded.user_window_offsets == {100: {"@2": 5}}
    assert loaded.window_display_names == {}
    assert loaded.group_chat_ids == {"valid": -100}
    binding = loaded.resolve_topic_binding(100, 3)
    assert binding is not None
    assert binding.window_id == ""
    assert binding.codex_thread_id == ""
    assert binding.cwd == ""
    assert binding.display_name == ""


def test_load_state_skips_non_finite_window_offset(monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(
        '{"user_window_offsets":{"100":{"@infinite":1e10000,"@valid":7}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(session_mod.config, "state_file", state_file)

    loaded = SessionManager()

    assert loaded.user_window_offsets == {100: {"@valid": 7}}


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


@pytest.mark.asyncio
async def test_resolve_stale_ids_repairs_invalid_general_window(
    mgr: SessionManager,
) -> None:
    binding = mgr.set_coco_control_topic(100, 1, chat_id=-100123)
    assert binding is not None
    binding.window_id = "legacy-window"
    binding.codex_thread_id = "control-history"
    binding.cwd = "/internal/control"
    binding.display_name = "coco-control"
    stale_state = mgr.get_window_state("legacy-window")
    stale_state.cwd = "/internal/control"
    stale_state.codex_thread_id = "control-history"

    await mgr.resolve_stale_ids()

    repaired = mgr.resolve_topic_binding(100, 1, chat_id=-100123)
    assert repaired is not None
    assert repaired.window_id.startswith("@")
    assert repaired.window_id != "legacy-window"
    assert repaired.codex_thread_id == "control-history"
    assert repaired.cwd == "/internal/control"
    repaired_state = mgr.get_window_state(repaired.window_id)
    assert repaired_state.codex_thread_id == "control-history"
    assert repaired_state.cwd == "/internal/control"


@pytest.mark.asyncio
async def test_resolve_stale_ids_preserves_deferred_legacy_control(
    mgr: SessionManager,
) -> None:
    mgr.coco_control_topic = session_mod.CocoControlTopic(100, 77, -100123)
    binding = mgr.ensure_topic_binding(100, 77, chat_id=-100123)
    assert binding is not None
    binding.window_id = "legacy-window"
    binding.codex_thread_id = "legacy-history"
    binding.cwd = "/remote/control"

    await mgr.resolve_stale_ids()

    repaired = mgr.resolve_topic_binding(100, 77, chat_id=-100123)
    assert repaired is not None
    assert repaired.window_id.startswith("@")
    assert repaired.codex_thread_id == "legacy-history"
    assert repaired.cwd == "/remote/control"


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
async def test_send_inputs_via_app_server_passes_topic_model_and_effort(
    mgr: SessionManager,
    monkeypatch,
):
    captured: list[tuple[str | None, str | None]] = []
    mgr.bind_thread(100, 1, "@1", window_name="demo")
    mgr.set_topic_model_selection(
        100,
        1,
        model_slug="gpt-5.6-sol",
        reasoning_effort="ultra",
    )
    mgr.set_window_codex_thread_id("@1", "thread-1")

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
        _ = thread_id, inputs, approval_policy, timeout, service_tier
        captured.append((model, effort))
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
    assert captured == [("gpt-5.6-sol", "ultra")]


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
        force_new_turn: bool = False,
        window_name: str,
        cwd: str,
        **_kwargs: object,
    ):
        captured["window_id"] = window_id
        captured["inputs"] = inputs
        captured["steer"] = steer
        captured["force_new_turn"] = force_new_turn
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
        force_new_turn: bool = False,
        window_name: str,
        cwd: str,
        **_kwargs: object,
    ):
        captured["window_id"] = window_id
        captured["inputs"] = inputs
        captured["steer"] = steer
        captured["force_new_turn"] = force_new_turn
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
@pytest.mark.parametrize(
    ("request_dispatched", "expected_pre_dispatch"),
    [(False, True), (None, False), (True, False)],
)
async def test_send_inputs_only_marks_definite_failure_pre_dispatch(
    mgr: SessionManager,
    monkeypatch,
    request_dispatched: bool | None,
    expected_pre_dispatch: bool,
) -> None:
    state = mgr.get_window_state("@900002")
    state.cwd = "/tmp/demo"
    state.window_name = "demo"

    monkeypatch.setattr(session_mod.config, "session_provider", "codex")
    monkeypatch.setattr(session_mod.config, "runtime_mode", "hybrid")
    monkeypatch.setattr(session_mod.config, "codex_transport", "app_server")

    async def _send_inputs_via_codex_app_server(**_kwargs: object) -> tuple[bool, str]:
        raise session_mod.CodexAppServerError(
            "app-server startup failed",
            request_dispatched=request_dispatched,
        )

    monkeypatch.setattr(
        mgr,
        "_send_inputs_via_codex_app_server",
        _send_inputs_via_codex_app_server,
    )
    dispatch_state = session_mod.TopicSendDispatchState()

    ok, message = await mgr.send_inputs_to_window(
        "@900002",
        [{"type": "localImage", "path": "/tmp/photo.png"}],
        dispatch_state=dispatch_state,
    )

    assert ok is False
    assert message == "App-server send failed: app-server startup failed"
    assert dispatch_state.transport_dispatch_started is False
    assert dispatch_state.pre_dispatch_transport_failure is expected_pre_dispatch


@pytest.mark.asyncio
async def test_send_inputs_to_window_thread_not_found_retries_bound_thread(
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
        force_new_turn: bool = False,
        window_name: str,
        cwd: str,
        **_kwargs: object,
    ):
        _ = window_id, inputs, window_name, cwd, force_new_turn
        call_states.append(mgr.get_window_codex_thread_id("@900003"))
        if len(call_states) == 1:
            assert steer is False
            raise session_mod.CodexAppServerError("thread not found: thread-old")
        assert steer is False
        return True, "ok"

    exact_resume_calls: list[tuple[str, str, str]] = []

    async def _resume_exact(*, window_id: str, cwd: str, thread_id: str) -> str:
        exact_resume_calls.append((window_id, cwd, thread_id))
        assert thread_id == "thread-old"
        mgr.set_window_codex_thread_id(window_id, thread_id)
        return thread_id

    async def _resume_latest(*, window_id: str, cwd: str) -> str:
        raise AssertionError("missing-thread recovery must not resume latest by cwd")

    monkeypatch.setattr(mgr, "_send_inputs_via_codex_app_server", _send_inputs_via_codex_app_server)
    monkeypatch.setattr(mgr, "resume_codex_session_for_window", _resume_exact)
    monkeypatch.setattr(mgr, "resume_latest_codex_session_for_window", _resume_latest)

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
    assert call_states == ["thread-old", "thread-old"]
    assert exact_resume_calls == [("@900003", "/tmp/demo", "thread-old")]
    assert mgr.get_window_codex_thread_id("@900003") == "thread-old"
    binding = mgr.resolve_topic_binding(100, 7)
    assert binding is not None
    assert binding.codex_thread_id == "thread-old"
    event_names = [event for event, _payload in telemetry_events]
    assert "transport.app_server.thread_missing_retry" in event_names
    assert "transport.app_server.thread_missing_recovered" in event_names
    assert "transport.app_server.send_failed" not in event_names


@pytest.mark.asyncio
async def test_send_inputs_to_window_thread_not_found_skips_subagent_session(
    mgr: SessionManager,
    monkeypatch,
    tmp_path: Path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions_root = tmp_path / "sessions"
    sessions_dir = sessions_root / "2026" / "07"
    sessions_dir.mkdir(parents=True)

    subagent_transcript = sessions_dir / "session-subagent.jsonl"
    subagent_transcript.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "timestamp": "2026-07-10T22:15:44Z",
                "payload": {
                    "id": "thread-subagent",
                    "cwd": str(workspace.resolve()),
                    "parent_thread_id": "thread-parent",
                    "thread_source": "subagent",
                    "multi_agent_version": "v2",
                    "source": {
                        "subagent": {
                            "thread_spawn": {
                                "parent_thread_id": "thread-parent",
                                "depth": 1,
                            }
                        }
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    state = mgr.get_window_state("@900009")
    state.cwd = str(workspace)
    state.window_name = "demo"
    state.codex_thread_id = "thread-subagent"
    state.codex_active_turn_id = "turn-old"
    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=7,
        codex_thread_id="thread-subagent",
        cwd=str(workspace),
        display_name="demo",
        window_id="@900009",
    )

    monkeypatch.setattr(session_mod.config, "session_provider", "codex")
    monkeypatch.setattr(session_mod.config, "sessions_path", sessions_root)
    monkeypatch.setattr(session_mod.config, "runtime_mode", "hybrid")
    monkeypatch.setattr(session_mod.config, "codex_transport", "app_server")

    call_states: list[str] = []

    async def _send_inputs_via_codex_app_server(
        *,
        window_id: str,
        inputs: list[dict[str, object]],
        steer: bool,
        force_new_turn: bool = False,
        window_name: str,
        cwd: str,
        **_kwargs: object,
    ):
        _ = window_id, inputs, window_name, cwd, force_new_turn
        thread_id = mgr.get_window_codex_thread_id("@900009")
        call_states.append(thread_id)
        if len(call_states) == 1:
            assert steer is False
            raise session_mod.CodexAppServerError("thread not found: thread-subagent")
        assert steer is False
        return True, "ok"

    exact_resume_calls: list[tuple[str, str, str]] = []

    async def _resume_exact(*, window_id: str, cwd: str, thread_id: str) -> str:
        exact_resume_calls.append((window_id, cwd, thread_id))
        assert thread_id == "thread-subagent"
        mgr.set_window_codex_thread_id(window_id, thread_id)
        return thread_id

    async def _resume_latest(*, window_id: str, cwd: str) -> str:
        raise AssertionError("missing-thread recovery must not resume latest by cwd")

    monkeypatch.setattr(mgr, "_send_inputs_via_codex_app_server", _send_inputs_via_codex_app_server)
    monkeypatch.setattr(mgr, "resume_codex_session_for_window", _resume_exact)
    monkeypatch.setattr(mgr, "resume_latest_codex_session_for_window", _resume_latest)

    ok, msg = await mgr.send_inputs_to_window(
        "@900009",
        [{"type": "text", "text": "hello"}],
        steer=False,
    )

    assert ok is True
    assert msg == "ok"
    assert exact_resume_calls == [("@900009", str(workspace), "thread-subagent")]
    assert call_states == ["thread-subagent", "thread-subagent"]
    assert mgr.get_window_codex_thread_id("@900009") == "thread-subagent"
    binding = mgr.resolve_topic_binding(100, 7)
    assert binding is not None
    assert binding.codex_thread_id == "thread-subagent"


@pytest.mark.asyncio
async def test_send_inputs_to_window_thread_not_found_resumes_bound_thread(
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

    exact_resume_calls: list[tuple[str, str, str]] = []
    latest_resume_calls: list[tuple[str, str]] = []

    async def _resume_exact(*, window_id: str, cwd: str, thread_id: str) -> str:
        assert window_id == "@900007"
        assert cwd == "/tmp/demo"
        assert thread_id == "thread-old"
        exact_resume_calls.append((window_id, cwd, thread_id))
        mgr.set_window_codex_thread_id("@900007", thread_id)
        mgr.set_window_codex_active_turn_id("@900007", "turn-resumed")
        return thread_id

    async def _resume_latest(*, window_id: str, cwd: str) -> str:
        latest_resume_calls.append((window_id, cwd))
        raise AssertionError("missing-thread recovery must not resume latest by cwd")

    monkeypatch.setattr(
        mgr,
        "resume_codex_session_for_window",
        _resume_exact,
    )
    monkeypatch.setattr(
        mgr,
        "resume_latest_codex_session_for_window",
        _resume_latest,
    )

    call_states: list[str] = []

    async def _send_inputs_via_codex_app_server(
        *,
        window_id: str,
        inputs: list[dict[str, object]],
        steer: bool,
        force_new_turn: bool = False,
        window_name: str,
        cwd: str,
        **_kwargs: object,
    ):
        _ = window_id, inputs, window_name, cwd, force_new_turn
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
    assert call_states == ["thread-old", "thread-old"]
    assert exact_resume_calls == [("@900007", "/tmp/demo", "thread-old")]
    assert latest_resume_calls == []
    assert mgr.get_window_codex_thread_id("@900007") == "thread-old"
    binding = mgr.resolve_topic_binding(100, 7)
    assert binding is not None
    assert binding.codex_thread_id == "thread-old"
    event_names = [event for event, _payload in telemetry_events]
    assert "transport.app_server.thread_missing_retry" in event_names
    assert "transport.app_server.thread_missing_recovered" in event_names
    assert "transport.app_server.send_failed" not in event_names


@pytest.mark.asyncio
async def test_send_inputs_to_window_thread_not_found_drops_retry_after_explicit_rebind(
    mgr: SessionManager,
    monkeypatch,
):
    monkeypatch.setattr(
        mgr,
        "_local_machine_identity",
        lambda: ("local-node", "Local"),
    )
    state = mgr.get_window_state("@900080")
    state.cwd = "/tmp/proj-a"
    state.window_name = "proj-a"
    state.codex_thread_id = "thread-a"
    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=7,
        codex_thread_id="thread-a",
        cwd="/tmp/proj-a",
        display_name="proj-a",
        window_id="@900080",
        machine_id="local-node",
        machine_display_name="Local",
    )

    send_attempts: list[tuple[str, list[dict[str, object]]]] = []
    retry_dispatches: list[tuple[str, list[dict[str, object]]]] = []
    resume_started = asyncio.Event()
    release_resume = asyncio.Event()

    async def _send_inputs_via_codex_app_server(
        *,
        window_id: str,
        inputs: list[dict[str, object]],
        steer: bool,
        force_new_turn: bool = False,
        window_name: str,
        cwd: str,
        **_kwargs: object,
    ) -> tuple[bool, str]:
        _ = steer, force_new_turn, window_name, cwd
        send_attempts.append((window_id, inputs))
        if len(send_attempts) == 1:
            raise session_mod.CodexAppServerError("thread not found: thread-a")
        retry_dispatches.append((window_id, inputs))
        return True, "stale request dispatched"

    async def _resume_exact(*, window_id: str, cwd: str, thread_id: str) -> str:
        assert (window_id, cwd, thread_id) == (
            "@900080",
            "/tmp/proj-a",
            "thread-a",
        )
        resume_started.set()
        await release_resume.wait()
        mgr._set_window_codex_thread_cache(window_id, thread_id)
        return thread_id

    monkeypatch.setattr(
        mgr,
        "_send_inputs_via_codex_app_server",
        _send_inputs_via_codex_app_server,
    )
    monkeypatch.setattr(mgr, "resume_codex_session_for_window", _resume_exact)

    task = asyncio.create_task(
        mgr.send_inputs_to_window(
            "@900080",
            [{"type": "text", "text": "original prompt"}],
            steer=False,
        )
    )
    await asyncio.wait_for(resume_started.wait(), timeout=1)

    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=7,
        codex_thread_id="thread-b",
        cwd="/tmp/proj-b",
        display_name="proj-b",
        window_id="@900081",
        machine_id="local-node",
        machine_display_name="Local",
    )
    release_resume.set()
    ok, message = await task

    assert ok is False
    assert message
    assert send_attempts == [
        ("@900080", [{"type": "text", "text": "original prompt"}])
    ]
    assert retry_dispatches == []
    binding = mgr._get_persisted_topic_binding(100, 7)
    assert binding is not None
    assert binding.codex_thread_id == "thread-b"
    assert binding.window_id == "@900081"
    assert binding.cwd == "/tmp/proj-b"
    assert binding.machine_id == "local-node"
    assert mgr.get_window_codex_thread_id("@900080") == "thread-a"
    assert mgr.get_window_codex_thread_id("@900081") == "thread-b"


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

    exact_resume_calls: list[tuple[str, str, str]] = []

    async def _resume_exact(*, window_id: str, cwd: str, thread_id: str) -> str:
        exact_resume_calls.append((window_id, cwd, thread_id))
        assert thread_id == "thread-old"
        mgr.set_window_codex_thread_id(window_id, thread_id)
        return thread_id

    async def _resume_latest(*, window_id: str, cwd: str) -> str:
        raise AssertionError("missing-thread recovery must not resume latest by cwd")

    monkeypatch.setattr(mgr, "resume_codex_session_for_window", _resume_exact)
    monkeypatch.setattr(mgr, "resume_latest_codex_session_for_window", _resume_latest)

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
    assert exact_resume_calls == [("@900004", "/tmp/demo", "thread-old")]
    assert mgr.get_window_codex_thread_id("@900004") == "thread-old"
    assert telemetry_events
    event, payload = telemetry_events[-1]
    assert event == "transport.app_server.send_failed"
    assert payload["fallback_allowed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_method", ["turn/start", "turn/steer"])
async def test_send_inputs_to_window_thread_not_found_retry_timeout_recovers_transport(
    mgr: SessionManager,
    monkeypatch,
    retry_method: str,
):
    state = mgr.get_window_state("@900010")
    state.cwd = "/tmp/demo"
    state.window_name = "demo"
    state.codex_thread_id = "thread-old"

    monkeypatch.setattr(session_mod.config, "session_provider", "codex")
    monkeypatch.setattr(session_mod.config, "runtime_mode", "hybrid")
    monkeypatch.setattr(session_mod.config, "codex_transport", "app_server")

    attempts = 0
    recovery_calls: list[tuple[str, str, str]] = []

    exact_resume_calls: list[tuple[str, str, str]] = []
    latest_resume_calls: list[tuple[str, str]] = []

    async def _resume_exact(*, window_id: str, cwd: str, thread_id: str) -> str:
        exact_resume_calls.append((window_id, cwd, thread_id))
        assert thread_id == "thread-old"
        mgr.set_window_codex_thread_id(window_id, thread_id)
        return thread_id

    async def _resume_latest(*, window_id: str, cwd: str) -> str:
        latest_resume_calls.append((window_id, cwd))
        raise AssertionError("missing-thread recovery must not resume latest by cwd")

    async def _send_inputs_via_codex_app_server(**_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise session_mod.CodexAppServerError("thread not found: thread-old")
        if retry_method == "turn/steer":
            mgr.set_window_codex_active_turn_id("@900010", "turn-retry")
        raise session_mod.CodexAppServerError(
            f"Timed out waiting for app-server response: {retry_method}"
        )

    async def _recover_uncertain_turn_timeout(
        *, method: str, thread_id: str = "", turn_id: str = ""
    ) -> bool:
        recovery_calls.append((method, thread_id, turn_id))
        return True

    monkeypatch.setattr(
        mgr,
        "resume_codex_session_for_window",
        _resume_exact,
    )
    monkeypatch.setattr(
        mgr,
        "resume_latest_codex_session_for_window",
        _resume_latest,
    )
    monkeypatch.setattr(
        mgr,
        "_send_inputs_via_codex_app_server",
        _send_inputs_via_codex_app_server,
    )
    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "recover_uncertain_turn_timeout",
        _recover_uncertain_turn_timeout,
    )

    ok, msg = await mgr.send_inputs_to_window(
        "@900010",
        [{"type": "text", "text": "run exactly once"}],
        steer=False,
    )

    assert ok is False
    assert attempts == 2
    assert exact_resume_calls == [("@900010", "/tmp/demo", "thread-old")]
    assert latest_resume_calls == []
    expected_recovery = (
        ("turn/steer", "thread-old", "turn-retry")
        if retry_method == "turn/steer"
        else ("turn/start", "", "")
    )
    assert recovery_calls == [expected_recovery]
    assert "retry with new thread failed" in msg
    assert "transport recovered" in msg
    assert "uncertain request was not replayed" in msg


@pytest.mark.asyncio
async def test_send_inputs_to_window_turn_steer_timeout_is_not_replayed(
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
    recovery_calls: list[tuple[str, str, str]] = []

    async def _send_inputs_via_codex_app_server(
        *,
        window_id: str,
        inputs: list[dict[str, object]],
        steer: bool,
        force_new_turn: bool = False,
        window_name: str,
        cwd: str,
    ):
        _ = window_id, inputs, window_name, cwd, steer, force_new_turn
        call_states.append(
            (
                mgr.get_window_codex_thread_id("@900005"),
                mgr.get_window_codex_active_turn_id("@900005"),
            )
        )
        raise session_mod.CodexAppServerError(
            "Timed out waiting for app-server response: turn/steer"
        )

    monkeypatch.setattr(mgr, "_send_inputs_via_codex_app_server", _send_inputs_via_codex_app_server)
    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "clear_active_turn",
        lambda thread_id: cleared_thread_ids.append(thread_id),
    )

    async def _recover_uncertain_turn_timeout(
        *, method: str, thread_id: str = "", turn_id: str = ""
    ) -> bool:
        recovery_calls.append((method, thread_id, turn_id))
        return True

    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "recover_uncertain_turn_timeout",
        _recover_uncertain_turn_timeout,
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

    assert ok is False
    assert "Timed out waiting for app-server response: turn/steer" in msg
    assert "not replayed" in msg
    assert call_states == [("thread-live", "turn-stale")]
    assert mgr.get_window_codex_active_turn_id("@900005") == ""
    assert cleared_thread_ids == []
    assert recovery_calls == [("turn/steer", "thread-live", "turn-stale")]
    event_names = [event for event, _payload in telemetry_events]
    assert "transport.app_server.steer_timeout_uncertain" in event_names
    assert "transport.app_server.uncertain_turn_timeout_recovered" in event_names
    assert "transport.app_server.send_failed" in event_names


@pytest.mark.asyncio
async def test_send_inputs_to_window_skipped_steer_recovery_preserves_active_turn(
    mgr: SessionManager,
    monkeypatch,
):
    state = mgr.get_window_state("@900011")
    state.cwd = "/tmp/demo"
    state.window_name = "demo"
    state.codex_thread_id = "thread-live"
    state.codex_active_turn_id = "turn-stale"

    monkeypatch.setattr(session_mod.config, "session_provider", "codex")
    monkeypatch.setattr(session_mod.config, "runtime_mode", "hybrid")
    monkeypatch.setattr(session_mod.config, "codex_transport", "app_server")

    cleared_thread_ids: list[str] = []
    recovery_calls: list[tuple[str, str, str]] = []

    async def _send_inputs_via_codex_app_server(**_kwargs):
        raise session_mod.CodexAppServerError(
            "Timed out waiting for app-server response: turn/steer"
        )

    async def _recover_uncertain_turn_timeout(
        *, method: str, thread_id: str = "", turn_id: str = ""
    ) -> bool:
        recovery_calls.append((method, thread_id, turn_id))
        return False

    monkeypatch.setattr(
        mgr,
        "_send_inputs_via_codex_app_server",
        _send_inputs_via_codex_app_server,
    )
    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "clear_active_turn",
        lambda thread_id: cleared_thread_ids.append(thread_id),
    )
    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "get_active_turn_id",
        lambda thread_id: "turn-stale" if thread_id == "thread-live" else None,
    )
    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "recover_uncertain_turn_timeout",
        _recover_uncertain_turn_timeout,
    )

    ok, msg = await mgr.send_inputs_to_window(
        "@900011",
        [{"type": "text", "text": "hello"}],
        steer=True,
    )

    assert ok is False
    assert "transport recovery failed" in msg
    assert cleared_thread_ids == []
    assert recovery_calls == [("turn/steer", "thread-live", "turn-stale")]
    assert mgr.get_window_codex_active_turn_id("@900011") == "turn-stale"


@pytest.mark.asyncio
@pytest.mark.parametrize("transport_recovered", [False, True])
async def test_send_inputs_to_window_uses_dispatched_turn_and_preserves_newer_turn(
    mgr: SessionManager,
    monkeypatch,
    transport_recovered: bool,
):
    state = mgr.get_window_state("@900013")
    state.cwd = "/tmp/demo"
    state.window_name = "demo"
    state.codex_thread_id = "thread-live"
    state.codex_active_turn_id = "turn-dispatched"

    monkeypatch.setattr(session_mod.config, "session_provider", "codex")
    monkeypatch.setattr(session_mod.config, "runtime_mode", "hybrid")
    monkeypatch.setattr(session_mod.config, "codex_transport", "app_server")
    monkeypatch.setattr(
        SessionManager,
        "_runtime_write_state",
        staticmethod(lambda _cwd: ("/tmp/demo", True)),
    )

    recovery_calls: list[tuple[str, str, str]] = []

    async def _turn_steer(**_kwargs):
        mgr.set_window_codex_active_turn_id("@900013", "turn-new")
        raise session_mod.CodexAppServerError(
            "Timed out waiting for app-server response: turn/steer"
        )

    async def _recover_uncertain_turn_timeout(
        *, method: str, thread_id: str = "", turn_id: str = ""
    ) -> bool:
        recovery_calls.append((method, thread_id, turn_id))
        return transport_recovered

    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "turn_steer",
        _turn_steer,
    )
    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "get_active_turn_id",
        lambda thread_id: "turn-new" if thread_id == "thread-live" else None,
    )
    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "recover_uncertain_turn_timeout",
        _recover_uncertain_turn_timeout,
    )

    ok, msg = await mgr.send_inputs_to_window(
        "@900013",
        [{"type": "text", "text": "hello"}],
        steer=True,
    )

    assert ok is False
    expected_status = (
        "transport recovered"
        if transport_recovered
        else "transport recovery failed"
    )
    assert expected_status in msg
    assert recovery_calls == [
        ("turn/steer", "thread-live", "turn-dispatched")
    ]
    assert mgr.get_window_codex_active_turn_id("@900013") == "turn-new"


@pytest.mark.asyncio
async def test_send_inputs_to_window_turn_start_timeout_recovers_transport_without_replay(
    mgr: SessionManager,
    monkeypatch,
):
    state = mgr.get_window_state("@900009")
    state.cwd = "/tmp/demo"
    state.window_name = "demo"
    state.codex_thread_id = "thread-live"

    monkeypatch.setattr(session_mod.config, "session_provider", "codex")
    monkeypatch.setattr(session_mod.config, "runtime_mode", "hybrid")
    monkeypatch.setattr(session_mod.config, "codex_transport", "app_server")

    send_calls: list[str] = []
    recovery_calls: list[str] = []

    async def _send_inputs_via_codex_app_server(**_kwargs):
        send_calls.append("send")
        raise session_mod.CodexAppServerError(
            "Timed out waiting for app-server response: turn/start"
        )

    async def _recover_uncertain_turn_timeout(*, method: str) -> bool:
        recovery_calls.append(method)
        return True

    monkeypatch.setattr(
        mgr,
        "_send_inputs_via_codex_app_server",
        _send_inputs_via_codex_app_server,
    )
    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "recover_uncertain_turn_timeout",
        _recover_uncertain_turn_timeout,
    )

    telemetry_events: list[tuple[str, dict[str, object]]] = []

    def _emit(event: str, **payload):
        telemetry_events.append((event, payload))

    monkeypatch.setattr(session_mod, "emit_telemetry", _emit)

    ok, msg = await mgr.send_inputs_to_window(
        "@900009",
        [{"type": "text", "text": "run exactly once"}],
        steer=False,
    )

    assert ok is False
    assert send_calls == ["send"]
    assert recovery_calls == ["turn/start"]
    assert "transport recovered" in msg
    assert "uncertain request was not replayed" in msg
    event_names = [event for event, _payload in telemetry_events]
    assert "transport.app_server.uncertain_turn_timeout_recovered" in event_names
    assert "transport.app_server.send_failed" in event_names


@pytest.mark.asyncio
async def test_send_inputs_to_window_turn_start_timeout_clears_stale_cached_turn(
    mgr: SessionManager,
    monkeypatch,
):
    state = mgr.get_window_state("@900014")
    state.cwd = "/tmp/demo"
    state.window_name = "demo"
    state.codex_thread_id = "thread-live"
    state.codex_active_turn_id = "turn-stale"

    monkeypatch.setattr(session_mod.config, "session_provider", "codex")
    monkeypatch.setattr(session_mod.config, "runtime_mode", "hybrid")
    monkeypatch.setattr(session_mod.config, "codex_transport", "app_server")

    async def _send_inputs_via_codex_app_server(**kwargs):
        assert kwargs["force_new_turn"] is True
        raise session_mod.CodexAppServerError(
            "Timed out waiting for app-server response: turn/start"
        )

    async def _recover_uncertain_turn_timeout(*, method: str) -> bool:
        assert method == "turn/start"
        return False

    monkeypatch.setattr(
        mgr,
        "_send_inputs_via_codex_app_server",
        _send_inputs_via_codex_app_server,
    )
    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "get_active_turn_id",
        lambda _thread_id: None,
    )
    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "recover_uncertain_turn_timeout",
        _recover_uncertain_turn_timeout,
    )

    ok, msg = await mgr.send_inputs_to_window(
        "@900014",
        [{"type": "text", "text": "run exactly once"}],
        force_new_turn=True,
    )

    assert ok is False
    assert "transport recovery failed" in msg
    assert mgr.get_window_codex_active_turn_id("@900014") == ""


@pytest.mark.asyncio
async def test_send_inputs_to_window_no_active_turn_retry_failure_returns_combined_error(
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
                "no active turn to steer"
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
    assert "no active turn to steer" in msg
    assert "retry with turn/start failed: steer retry exploded" in msg
    assert mgr.get_window_codex_active_turn_id("@900006") == ""
    assert telemetry_events
    event, payload = telemetry_events[-1]
    assert event == "transport.app_server.send_failed"
    assert payload["fallback_allowed"] is False


@pytest.mark.asyncio
async def test_send_inputs_to_window_no_active_turn_retry_marks_new_turn_dispatch(
    mgr: SessionManager,
    monkeypatch,
):
    """A stale steer recovered through turn/start reports the resolved mode."""
    state = mgr.get_window_state("@900015")
    state.cwd = "/tmp/demo"
    state.window_name = "demo"
    state.codex_thread_id = "thread-live"
    state.codex_active_turn_id = "turn-stale"

    monkeypatch.setattr(session_mod.config, "session_provider", "codex")
    monkeypatch.setattr(session_mod.config, "runtime_mode", "hybrid")
    monkeypatch.setattr(session_mod.config, "codex_transport", "app_server")

    attempts = 0
    dispatch_state = session_mod.TopicSendDispatchState()

    async def _send_inputs_via_codex_app_server(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise session_mod.CodexAppServerError("no active turn to steer")
        assert kwargs["steer"] is False
        kwargs["dispatch_state"].mark_turn_started()
        return True, "started"

    monkeypatch.setattr(
        mgr,
        "_send_inputs_via_codex_app_server",
        _send_inputs_via_codex_app_server,
    )
    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "clear_active_turn",
        lambda _thread_id: None,
    )

    ok, message = await mgr.send_inputs_to_window(
        "@900015",
        [{"type": "text", "text": "recover this steer"}],
        steer=True,
        dispatch_state=dispatch_state,
    )

    assert ok is True
    assert message == "started"
    assert attempts == 2
    assert dispatch_state.dispatch_mode == "turn_start"
    assert dispatch_state.started_new_turn is True


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_method", ["turn/start", "turn/steer"])
async def test_send_inputs_to_window_no_active_turn_retry_timeout_recovers_transport(
    mgr: SessionManager,
    monkeypatch,
    retry_method: str,
):
    state = mgr.get_window_state("@900012")
    state.cwd = "/tmp/demo"
    state.window_name = "demo"
    state.codex_thread_id = "thread-live"
    state.codex_active_turn_id = "turn-stale"

    monkeypatch.setattr(session_mod.config, "session_provider", "codex")
    monkeypatch.setattr(session_mod.config, "runtime_mode", "hybrid")
    monkeypatch.setattr(session_mod.config, "codex_transport", "app_server")

    attempts = 0
    recovery_calls: list[tuple[str, str, str]] = []

    async def _send_inputs_via_codex_app_server(**_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise session_mod.CodexAppServerError("no active turn to steer")
        if retry_method == "turn/steer":
            mgr.set_window_codex_active_turn_id("@900012", "turn-retry")
        raise session_mod.CodexAppServerError(
            f"Timed out waiting for app-server response: {retry_method}"
        )

    async def _recover_uncertain_turn_timeout(
        *, method: str, thread_id: str = "", turn_id: str = ""
    ) -> bool:
        recovery_calls.append((method, thread_id, turn_id))
        return True

    monkeypatch.setattr(
        mgr,
        "_send_inputs_via_codex_app_server",
        _send_inputs_via_codex_app_server,
    )
    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "recover_uncertain_turn_timeout",
        _recover_uncertain_turn_timeout,
    )

    ok, msg = await mgr.send_inputs_to_window(
        "@900012",
        [{"type": "text", "text": "run exactly once"}],
        steer=True,
    )

    assert ok is False
    assert attempts == 2
    expected_recovery = (
        ("turn/steer", "thread-live", "turn-retry")
        if retry_method == "turn/steer"
        else ("turn/start", "", "")
    )
    assert recovery_calls == [expected_recovery]
    assert "retry with turn/start failed" in msg
    assert "transport recovered" in msg
    assert "uncertain request was not replayed" in msg


@pytest.mark.asyncio
async def test_turn_start_timeout_is_not_replayed(
    mgr: SessionManager,
    monkeypatch,
):
    calls: list[str] = []

    async def _turn_start(**kwargs):
        calls.append(str(kwargs["thread_id"]))
        raise session_mod.CodexAppServerError(
            "Timed out waiting for app-server response: turn/start"
        )

    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "turn_start",
        _turn_start,
    )

    with pytest.raises(
        session_mod.CodexAppServerError,
        match="Timed out waiting for app-server response: turn/start",
    ):
        await mgr._turn_start_with_retry(
            thread_id="thread-once",
            inputs=[{"type": "text", "text": "run once"}],
            approval_policy="on-request",
        )

    assert calls == ["thread-once"]


@pytest.mark.asyncio
async def test_send_inputs_to_window_force_new_turn_ignores_cached_active_turn(
    mgr: SessionManager,
    monkeypatch,
):
    state = mgr.get_window_state("@900008")
    state.cwd = "/tmp/demo"
    state.window_name = "demo"
    state.codex_thread_id = "thread-live"
    state.codex_active_turn_id = "turn-stale"

    monkeypatch.setattr(session_mod.config, "session_provider", "codex")
    monkeypatch.setattr(session_mod.config, "runtime_mode", "hybrid")
    monkeypatch.setattr(session_mod.config, "codex_transport", "app_server")

    async def _ensure_codex_thread_for_window(**_kwargs):
        return "thread-live", "full-auto"

    turn_start_calls: list[dict[str, object]] = []
    dispatch_state = session_mod.TopicSendDispatchState()

    async def _turn_start_with_retry(
        *,
        thread_id: str,
        inputs: list[dict[str, object]],
        approval_policy: str,
        service_tier: str,
        **_kwargs: object,
    ):
        turn_start_calls.append(
            {
                "thread_id": thread_id,
                "inputs": inputs,
                "approval_policy": approval_policy,
                "service_tier": service_tier,
            }
        )
        return {"turn": {"id": "turn-new"}}

    async def _unexpected_turn_steer(**_kwargs):
        raise AssertionError("force_new_turn should bypass turn/steer")

    monkeypatch.setattr(mgr, "_ensure_codex_thread_for_window", _ensure_codex_thread_for_window)
    monkeypatch.setattr(mgr, "_turn_start_with_retry", _turn_start_with_retry)
    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "turn_steer",
        _unexpected_turn_steer,
    )
    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "get_active_turn_id",
        lambda _thread_id: "turn-live-from-client",
    )

    ok, msg = await mgr.send_inputs_to_window(
        "@900008",
        [{"type": "text", "text": "queued task"}],
        steer=False,
        force_new_turn=True,
        dispatch_state=dispatch_state,
    )

    assert ok is True
    assert msg == "Sent via app-server to demo"
    assert len(turn_start_calls) == 1
    assert turn_start_calls[0]["thread_id"] == "thread-live"
    assert mgr.get_window_codex_active_turn_id("@900008") == "turn-new"
    assert dispatch_state.dispatch_mode == "turn_start"
    assert dispatch_state.started_new_turn is True


@pytest.mark.asyncio
async def test_validate_codex_topic_bindings_preserves_invalid_thread_ids(
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

    async def _thread_read(
        *,
        thread_id: str,
        timeout: float,
        include_turns: bool,
    ):
        _ = thread_id
        assert timeout == 10.0
        assert include_turns is False
        raise session_mod.CodexAppServerError("thread not found")

    monkeypatch.setattr(session_mod.codex_app_server_client, "thread_read", _thread_read)

    summary = await mgr.validate_codex_topic_bindings()

    assert summary == {"checked": 1, "invalid": 1, "repaired": 0}
    binding = mgr.resolve_topic_binding(100, 7)
    assert binding is not None
    assert binding.codex_thread_id == "thread-dead"
    assert mgr.get_window_state("@900000").codex_thread_id == "thread-dead"
    assert mgr.get_window_state("@900000").codex_active_turn_id == "turn-1"


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

    async def _thread_read(
        *,
        thread_id: str,
        timeout: float,
        include_turns: bool,
    ):
        assert timeout == 10.0
        assert include_turns is False
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


def _bind_test_codex_thread(mgr: SessionManager) -> None:
    """Give context-injection fixtures a realistic pre-existing thread."""
    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=5,
        codex_thread_id="thread-5",
        window_id="@1",
        cwd="/tmp/project",
        display_name="project",
    )


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
    _bind_test_codex_thread(mgr)
    mgr.set_thread_skills(100, 5, ["demo"])

    captured: dict[str, object] = {}

    async def _send_inputs_to_window(
        window_id: str,
        inputs: list[dict[str, object]],
        *,
        steer: bool = False,
        force_new_turn: bool = False,
        **_kwargs: object,
    ):
        captured["window_id"] = window_id
        captured["inputs"] = inputs
        captured["steer"] = steer
        captured["force_new_turn"] = force_new_turn
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
async def test_send_topic_text_to_window_injects_live_goal_context_for_plain_goal_requests(
    mgr: SessionManager,
    monkeypatch,
):
    monkeypatch.setattr(mgr, "_codex_app_server_mode_enabled", lambda: True)
    mgr.bind_thread(100, 5, "@1")
    _bind_test_codex_thread(mgr)

    captured: dict[str, object] = {}

    async def _get_topic_goal(**_kwargs):
        return False, None, "No goal is set for this Codex thread."

    async def _send_inputs_to_window(
        window_id: str,
        inputs: list[dict[str, object]],
        *,
        steer: bool = False,
        force_new_turn: bool = False,
        **_kwargs: object,
    ):
        captured["window_id"] = window_id
        captured["inputs"] = inputs
        captured["steer"] = steer
        captured["force_new_turn"] = force_new_turn
        return True, "ok"

    monkeypatch.setattr(mgr, "get_topic_goal", _get_topic_goal)
    monkeypatch.setattr(mgr, "send_inputs_to_window", _send_inputs_to_window)

    ok, _msg = await mgr.send_topic_text_to_window(
        user_id=100,
        thread_id=5,
        window_id="@1",
        text="please change the goal to ship the docs",
        steer=False,
    )

    assert ok is True
    assert captured["window_id"] == "@1"
    inputs = captured["inputs"]
    assert isinstance(inputs, list)
    assert inputs[0]["type"] == "text"
    goal_context = str(inputs[0]["text"])
    assert "[coco goal context]" in goal_context
    assert "Live native goal state for this topic: no goal is currently set." in goal_context
    assert "Trust this live goal state over stale session memory." in goal_context
    assert inputs[1] == {"type": "text", "text": "please change the goal to ship the docs"}


@pytest.mark.parametrize(
    "text",
    [
        "Help me fix this Objective-C build",
        "Compare Objective-C and Swift",
        "We need goal-oriented programming examples",
    ],
)
def test_goal_context_trigger_ignores_hyphenated_compounds(text):
    assert SessionManager._message_requests_live_goal_context(text) is False


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
    _bind_test_codex_thread(mgr)
    mgr.set_thread_codex_skills(100, 5, ["reviewer"])

    captured: dict[str, object] = {}

    async def _send_inputs_to_window(
        window_id: str,
        inputs: list[dict[str, object]],
        *,
        steer: bool = False,
        force_new_turn: bool = False,
        **_kwargs: object,
    ):
        captured["window_id"] = window_id
        captured["inputs"] = inputs
        captured["steer"] = steer
        captured["force_new_turn"] = force_new_turn
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
async def test_send_topic_text_to_window_injects_live_goal_context_into_app_server_inputs(
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
    _bind_test_codex_thread(mgr)
    mgr.set_thread_codex_skills(100, 5, ["reviewer"])

    captured: dict[str, object] = {}

    async def _get_topic_goal(**_kwargs):
        return True, {"goal": {"objective": "Ship the docs", "status": "active"}}, ""

    async def _send_inputs_to_window(
        window_id: str,
        inputs: list[dict[str, object]],
        *,
        steer: bool = False,
        force_new_turn: bool = False,
        **_kwargs: object,
    ):
        captured["window_id"] = window_id
        captured["inputs"] = inputs
        captured["steer"] = steer
        captured["force_new_turn"] = force_new_turn
        return True, "ok"

    monkeypatch.setattr(mgr, "get_topic_goal", _get_topic_goal)
    monkeypatch.setattr(mgr, "send_inputs_to_window", _send_inputs_to_window)

    ok, _msg = await mgr.send_topic_text_to_window(
        user_id=100,
        thread_id=5,
        window_id="@1",
        text="check whether the goal should change",
        steer=False,
    )

    assert ok is True
    assert captured["window_id"] == "@1"
    inputs = captured["inputs"]
    assert isinstance(inputs, list)
    assert inputs[0]["type"] == "skill"
    assert inputs[1]["type"] == "text"
    goal_context = str(inputs[1]["text"])
    assert "[coco goal context]" in goal_context
    assert "Current native goal status: active." in goal_context
    assert "Current native goal objective: Ship the docs" in goal_context
    assert inputs[2] == {"type": "text", "text": "check whether the goal should change"}


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
    _bind_test_codex_thread(mgr)
    mgr.set_thread_skills(100, 5, ["demo"])
    mgr.set_thread_codex_skills(100, 5, ["reviewer"])

    captured: dict[str, object] = {}

    async def _send_to_window(
        window_id: str,
        text: str,
        *,
        steer: bool = False,
        force_new_turn: bool = False,
        **_kwargs: object,
    ):
        captured["window_id"] = window_id
        captured["text"] = text
        captured["steer"] = steer
        captured["force_new_turn"] = force_new_turn
        return True, "ok"

    monkeypatch.setattr(mgr, "send_to_window", _send_to_window)

    ok, _msg = await mgr.send_topic_text_to_window(
        user_id=100,
        thread_id=5,
        window_id="@1",
        text="hello world",
        steer=True,
        force_new_turn=True,
    )

    assert ok is True
    assert captured["window_id"] == "@1"
    injected = str(captured["text"])
    assert "[coco guidance]" in injected
    assert "app `demo`" in injected
    assert "skill `reviewer`" in injected
    assert injected.endswith("hello world")
    assert captured["force_new_turn"] is True


@pytest.mark.asyncio
async def test_send_topic_text_to_window_injects_coco_operator_context_for_app_server(
    mgr: SessionManager,
    monkeypatch,
    telegram_memory_path: Path,
):
    _append_memory_entries(
        telegram_memory_path,
        [
            {
                "ts_utc": "2026-05-30T12:40:00+00:00",
                "direction": "in",
                "chat_id": -100123,
                "thread_id": 8,
                "from_user_id": 100,
                "text": "The PDF flow is still broken",
            },
            {
                "ts_utc": "2026-05-30T12:41:00+00:00",
                "direction": "out_send",
                "chat_id": -100123,
                "thread_id": 8,
                "text": "I’m tracing the document handler now.",
            },
        ],
    )
    monkeypatch.setattr(mgr, "_codex_app_server_mode_enabled", lambda: True)
    mgr.bind_thread(100, 1, "@1")
    mgr.bind_thread(100, 8, "@8")
    mgr.bind_thread(100, 9, "@9")
    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=1,
        chat_id=-100123,
        codex_thread_id="thread-1",
        window_id="@1",
        cwd="/env/_coco/chat-100123-thread-1",
        display_name="coco-control",
    )
    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=8,
        chat_id=-100123,
        codex_thread_id="thread-8",
        window_id="@8",
        cwd="/env/fmwblog",
        display_name="fmwblog",
        machine_id="remote-node",
        machine_display_name="Browse Node",
    )
    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=9,
        chat_id=-100123,
        codex_thread_id="thread-9",
        window_id="@9",
        cwd="/env/bottleshot",
        display_name="bottleshot",
    )
    mgr.set_coco_control_topic(100, 1, chat_id=-100123)
    mgr.set_topic_sync_mode(100, 8, session_mod.TOPIC_SYNC_MODE_HOST_FOLLOW_FINAL, chat_id=-100123)
    mgr.set_topic_response_mode(100, 8, chat_id=-100123, response_mode="voice")
    mgr.set_window_codex_active_turn_id("@8", "turn-8")

    captured: dict[str, object] = {}

    async def _send_inputs_to_window(
        window_id: str,
        inputs: list[dict[str, object]],
        *,
        steer: bool = False,
        force_new_turn: bool = False,
        **_kwargs: object,
    ):
        captured["window_id"] = window_id
        captured["inputs"] = inputs
        captured["steer"] = steer
        captured["force_new_turn"] = force_new_turn
        return True, "ok"

    monkeypatch.setattr(mgr, "send_inputs_to_window", _send_inputs_to_window)

    ok, _msg = await mgr.send_topic_text_to_window(
        user_id=100,
        thread_id=1,
        chat_id=-100123,
        window_id="@1",
        text="What is happening right now?",
        steer=False,
    )

    assert ok is True
    inputs = captured["inputs"]
    assert isinstance(inputs, list)
    assert inputs[0]["type"] == "text"
    operator_text = str(inputs[0]["text"])
    assert "[coco operator]" in operator_text
    assert "This topic is the singleton CoCo control topic." in operator_text
    assert "You can inspect, summarize, and steer other topics in this chat." in operator_text
    assert "thread `8`: `fmwblog` — machine `Browse Node`, sync `host_follow_final`, response `voice`, turn `active`, workspace `/env/fmwblog`" in operator_text
    assert "thread `9`: `bottleshot` — machine `" in operator_text
    assert "sync `telegram_live`" in operator_text
    assert "response `text`" in operator_text
    assert "Recent visible activity:" in operator_text
    assert "fmwblog: User: The PDF flow is still broken | CoCo: I’m tracing the document handler now." in operator_text
    assert inputs[1] == {"type": "text", "text": "What is happening right now?"}


@pytest.mark.asyncio
async def test_send_topic_text_to_window_injects_coco_operator_context_for_legacy_mode(
    mgr: SessionManager,
    monkeypatch,
    telegram_memory_path: Path,
):
    _append_memory_entries(
        telegram_memory_path,
        [
            {
                "ts_utc": "2026-05-30T12:40:00+00:00",
                "direction": "in",
                "chat_id": -100123,
                "thread_id": 8,
                "from_user_id": 100,
                "text": "The PDF flow is still broken",
            },
            {
                "ts_utc": "2026-05-30T12:41:00+00:00",
                "direction": "out_send",
                "chat_id": -100123,
                "thread_id": 8,
                "text": "I’m tracing the document handler now.",
            },
        ],
    )
    monkeypatch.setattr(mgr, "_codex_app_server_mode_enabled", lambda: False)
    mgr.bind_thread(100, 1, "@1")
    mgr.bind_thread(100, 8, "@8")
    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=1,
        chat_id=-100123,
        codex_thread_id="thread-1",
        window_id="@1",
        cwd="/env/_coco/chat-100123-thread-1",
        display_name="coco-control",
    )
    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=8,
        chat_id=-100123,
        codex_thread_id="thread-8",
        window_id="@8",
        cwd="/env/fmwblog",
        display_name="fmwblog",
    )
    mgr.set_coco_control_topic(100, 1, chat_id=-100123)
    mgr.set_topic_response_mode(100, 8, chat_id=-100123, response_mode="voice")

    captured: dict[str, object] = {}

    async def _send_to_window(
        window_id: str,
        text: str,
        *,
        steer: bool = False,
        force_new_turn: bool = False,
        **_kwargs: object,
    ):
        _ = steer
        _ = force_new_turn
        captured["window_id"] = window_id
        captured["text"] = text
        return True, "ok"

    monkeypatch.setattr(mgr, "send_to_window", _send_to_window)

    ok, _msg = await mgr.send_topic_text_to_window(
        user_id=100,
        thread_id=1,
        chat_id=-100123,
        window_id="@1",
        text="Summarize the active topics.",
        steer=False,
    )

    assert ok is True
    injected = str(captured["text"])
    assert "[coco operator]" in injected
    assert "thread `8`: `fmwblog` — machine `" in injected
    assert "response `voice`" in injected
    assert "Recent visible activity:" in injected
    assert "fmwblog: User: The PDF flow is still broken | CoCo: I’m tracing the document handler now." in injected
    assert injected.endswith("Summarize the active topics.")


@pytest.mark.asyncio
async def test_send_topic_text_to_window_uses_persisted_cwd_for_empty_canonical_thread(
    mgr: SessionManager,
    monkeypatch,
) -> None:
    """A new-thread send must not create a session in a stale window cwd."""
    monkeypatch.setattr(
        mgr,
        "_local_machine_identity",
        lambda: ("local-node", "Local"),
    )
    monkeypatch.setattr(mgr, "_codex_app_server_mode_enabled", lambda: True)

    window_id = "@920001"
    state = mgr.get_window_state(window_id)
    state.cwd = "/workspace/canonical"
    state.window_name = "demo"
    mgr.bind_thread(100, 1, window_id, window_name="demo")
    binding = mgr._get_persisted_topic_binding(100, 1)
    assert binding is not None
    assert binding.codex_thread_id == ""
    assert binding.cwd == "/workspace/canonical"

    # Simulate a stale cache after the topic's canonical binding was persisted.
    state.cwd = "/workspace/stale-cache"
    dispatched_cwds: list[str] = []

    async def _send_inputs_via_codex_app_server(
        *,
        window_id: str,
        inputs: list[dict[str, object]],
        steer: bool,
        force_new_turn: bool,
        window_name: str,
        cwd: str,
        **_kwargs: object,
    ) -> tuple[bool, str]:
        _ = window_id, inputs, steer, force_new_turn, window_name
        dispatched_cwds.append(cwd)
        return True, "stale-cwd dispatch"

    monkeypatch.setattr(
        mgr,
        "_send_inputs_via_codex_app_server",
        _send_inputs_via_codex_app_server,
    )

    ok, _message = await mgr.send_topic_text_to_window(
        user_id=100,
        thread_id=1,
        window_id=window_id,
        text="start this topic",
    )

    # A safe implementation either refuses the contradictory cache or sends
    # with the persisted cwd; it must never dispatch in the stale directory.
    assert not dispatched_cwds or dispatched_cwds == ["/workspace/canonical"]
    assert ok is False or dispatched_cwds == ["/workspace/canonical"]


@pytest.mark.asyncio
async def test_local_topic_send_commits_new_thread_before_first_turn_dispatch(
    mgr: SessionManager,
    monkeypatch,
) -> None:
    """A first prompt may create its canonical thread and still dispatch."""
    monkeypatch.setattr(
        mgr,
        "_local_machine_identity",
        lambda: ("local-node", "Local"),
    )
    monkeypatch.setattr(mgr, "_codex_app_server_mode_enabled", lambda: True)
    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "get_active_turn_id",
        lambda _thread_id: "",
    )

    window_id = "@920007"
    state = mgr.get_window_state(window_id)
    state.cwd = "/workspace/canonical"
    state.window_name = "demo"
    mgr.bind_thread(100, 7, window_id, window_name="demo")
    binding = mgr._get_persisted_topic_binding(100, 7)
    assert binding is not None
    assert binding.codex_thread_id == ""
    assert binding.cwd == "/workspace/canonical"

    # The raw topic binding is authoritative even if the mutable window cache
    # has since drifted to another workspace.
    state.cwd = "/workspace/stale-cache"
    thread_start_calls: list[dict[str, object]] = []
    turn_start_calls: list[dict[str, object]] = []

    async def _thread_start(**kwargs: object) -> dict[str, object]:
        thread_start_calls.append(kwargs)
        return {"thread": {"id": "thread-920007"}}

    async def _turn_start(**kwargs: object) -> dict[str, object]:
        turn_start_calls.append(kwargs)
        return {"turn": {"id": "turn-920007"}}

    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "thread_start",
        _thread_start,
    )
    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "turn_start",
        _turn_start,
    )

    ok, message = await mgr.send_topic_text_to_window(
        user_id=100,
        thread_id=7,
        window_id=window_id,
        text="start this topic",
    )

    assert ok is True, message
    assert [call["cwd"] for call in thread_start_calls] == [
        "/workspace/canonical"
    ]
    assert [call["thread_id"] for call in turn_start_calls] == ["thread-920007"]
    assert mgr.get_window_codex_thread_id(window_id) == "thread-920007"
    binding = mgr._get_persisted_topic_binding(100, 7)
    assert binding is not None
    assert binding.codex_thread_id == "thread-920007"
    assert mgr.get_window_state(window_id).cwd == "/workspace/canonical"


@pytest.mark.asyncio
async def test_local_topic_send_drops_stale_thread_after_rebind_during_thread_start(
    mgr: SessionManager,
    monkeypatch,
) -> None:
    """A stale first-thread result cannot overwrite an explicit rebind."""
    monkeypatch.setattr(
        mgr,
        "_local_machine_identity",
        lambda: ("local-node", "Local"),
    )
    monkeypatch.setattr(mgr, "_codex_app_server_mode_enabled", lambda: True)

    old_window_id = "@920008"
    old_state = mgr.get_window_state(old_window_id)
    old_state.cwd = "/workspace/old"
    old_state.window_name = "old"
    mgr.bind_thread(100, 8, old_window_id, window_name="old")
    old_binding = mgr._get_persisted_topic_binding(100, 8)
    assert old_binding is not None
    assert old_binding.codex_thread_id == ""
    assert old_binding.cwd == "/workspace/old"

    thread_start_started = asyncio.Event()
    release_thread_start = asyncio.Event()
    turn_start_calls: list[dict[str, object]] = []

    async def _thread_start(**_kwargs: object) -> dict[str, object]:
        thread_start_started.set()
        await release_thread_start.wait()
        return {"thread": {"id": "stale-thread-920008"}}

    async def _turn_start(**kwargs: object) -> dict[str, object]:
        turn_start_calls.append(kwargs)
        return {"turn": {"id": "unexpected-turn"}}

    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "thread_start",
        _thread_start,
    )
    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "turn_start",
        _turn_start,
    )

    task = asyncio.create_task(
        mgr.send_topic_text_to_window(
            user_id=100,
            thread_id=8,
            window_id=old_window_id,
            text="start this topic",
        )
    )
    await asyncio.wait_for(thread_start_started.wait(), timeout=1)

    # /folder or /resume may explicitly move the topic while the implicit
    # first-thread request is waiting on app-server thread/start.
    new_window_id = "@920009"
    new_state = mgr.get_window_state(new_window_id)
    new_state.cwd = "/workspace/new"
    new_state.window_name = "new"
    mgr.bind_thread(100, 8, new_window_id, window_name="new")

    release_thread_start.set()
    ok, message = await task

    assert ok is False
    assert message
    assert turn_start_calls == []
    binding = mgr._get_persisted_topic_binding(100, 8)
    assert binding is not None
    assert binding.window_id == new_window_id
    assert binding.cwd == "/workspace/new"
    assert binding.codex_thread_id == ""
    assert mgr.get_window_codex_thread_id(old_window_id) == ""
    assert mgr.get_window_codex_thread_id(new_window_id) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("steer", [False, True])
async def test_local_topic_send_drops_success_after_rebind_during_turn_dispatch(
    mgr: SessionManager,
    monkeypatch,
    steer: bool,
) -> None:
    """A local turn result cannot be applied after the topic moves ownership."""
    monkeypatch.setattr(
        mgr,
        "_local_machine_identity",
        lambda: ("local-node", "Local"),
    )
    monkeypatch.setattr(mgr, "_codex_app_server_mode_enabled", lambda: True)

    old_window_id = "@920002"
    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=2,
        codex_thread_id="thread-a",
        window_id=old_window_id,
        cwd="/workspace/a",
        display_name="a",
        machine_id="local-node",
        machine_display_name="Local",
    )
    if steer:
        mgr.set_window_codex_active_turn_id(old_window_id, "turn-before")

    async def _ensure_codex_thread_for_window(**_kwargs: object) -> tuple[str, str]:
        return "thread-a", "on-request"

    monkeypatch.setattr(
        mgr,
        "_ensure_codex_thread_for_window",
        _ensure_codex_thread_for_window,
    )
    monkeypatch.setattr(
        SessionManager,
        "_runtime_write_state",
        staticmethod(lambda _cwd: ("/workspace/a", True)),
    )
    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "get_active_turn_id",
        lambda _thread_id: "",
    )

    dispatch_started = asyncio.Event()
    release_dispatch = asyncio.Event()

    if steer:

        async def _turn_steer(**kwargs: object) -> dict[str, object]:
            assert kwargs["thread_id"] == "thread-a"
            assert kwargs["expected_turn_id"] == "turn-before"
            dispatch_started.set()
            await release_dispatch.wait()
            return {"turnId": "turn-a-after-rebind"}

        monkeypatch.setattr(
            session_mod.codex_app_server_client,
            "turn_steer",
            _turn_steer,
        )
    else:

        async def _turn_start(**kwargs: object) -> dict[str, object]:
            assert kwargs["thread_id"] == "thread-a"
            dispatch_started.set()
            await release_dispatch.wait()
            return {"turn": {"id": "turn-a-after-rebind"}}

        monkeypatch.setattr(
            session_mod.codex_app_server_client,
            "turn_start",
            _turn_start,
        )

    marked_live: list[dict[str, object]] = []
    monkeypatch.setattr(
        mgr,
        "mark_topic_telegram_live",
        lambda **kwargs: marked_live.append(kwargs),
    )

    task = asyncio.create_task(
        mgr.send_topic_text_to_window(
            user_id=100,
            thread_id=2,
            window_id=old_window_id,
            text="continue topic",
            steer=steer,
        )
    )
    await asyncio.wait_for(dispatch_started.wait(), timeout=1)

    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=2,
        codex_thread_id="thread-b",
        window_id="@920003",
        cwd="/workspace/b",
        display_name="b",
        machine_id="local-node",
        machine_display_name="Local",
    )
    release_dispatch.set()
    ok, message = await task

    assert ok is False
    assert message
    assert marked_live == []
    assert mgr.get_window_codex_active_turn_id(old_window_id) != "turn-a-after-rebind"
    binding = mgr._get_persisted_topic_binding(100, 2)
    assert binding is not None
    assert (binding.window_id, binding.codex_thread_id, binding.cwd) == (
        "@920003",
        "thread-b",
        "/workspace/b",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("with_goal_context", [False, True])
async def test_remote_topic_send_rejects_ack_after_full_binding_rebind(
    mgr: SessionManager,
    monkeypatch,
    with_goal_context: bool,
) -> None:
    """Remote ACKs must still belong to the same window/machine/cwd binding."""
    monkeypatch.setattr(
        mgr,
        "_local_machine_identity",
        lambda: ("local-node", "Local"),
    )
    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=3,
        codex_thread_id="thread-shared",
        window_id="@920004",
        cwd="/workspace/a",
        display_name="a",
        machine_id="remote-a",
        machine_display_name="Remote A",
    )

    if with_goal_context:

        async def _build_live_goal_context(**_kwargs: object) -> str:
            return "[coco goal context] live"

        monkeypatch.setattr(mgr, "_build_live_goal_context", _build_live_goal_context)

    send_started = asyncio.Event()
    release_send = asyncio.Event()

    async def _send_inputs(machine_id: str, **kwargs: object) -> dict[str, object]:
        assert machine_id == "remote-a"
        assert kwargs["window_id"] == "@920004"
        assert kwargs["thread_id"] == "thread-shared"
        send_started.set()
        await release_send.wait()
        return {
            "ok": True,
            "message": "stale remote acknowledgement",
            "thread_id": "thread-shared",
            "turn_id": "turn-a",
            "transport_epoch": "agent-epoch-a",
            "transport_epoch_started_at": 100.0,
            "transport_generation": 1,
        }

    monkeypatch.setattr(agent_rpc_mod.agent_rpc_client, "send_inputs", _send_inputs)
    marked_live: list[dict[str, object]] = []
    monkeypatch.setattr(
        mgr,
        "mark_topic_telegram_live",
        lambda **kwargs: marked_live.append(kwargs),
    )

    task = asyncio.create_task(
        mgr.send_topic_text_to_window(
            user_id=100,
            thread_id=3,
            window_id="@920004",
            text="continue remote topic",
        )
    )
    await asyncio.wait_for(send_started.wait(), timeout=1)

    # Keep the same Codex thread but move every other ownership coordinate.
    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=3,
        codex_thread_id="thread-shared",
        window_id="@920005",
        cwd="/workspace/b",
        display_name="b",
        machine_id="remote-b",
        machine_display_name="Remote B",
    )
    release_send.set()
    ok, message = await task

    assert ok is False
    assert message
    assert marked_live == []
    assert mgr.get_window_codex_active_turn_id("@920004") == ""
    binding = mgr._get_persisted_topic_binding(100, 3)
    assert binding is not None
    assert (binding.window_id, binding.codex_thread_id, binding.machine_id, binding.cwd) == (
        "@920005",
        "thread-shared",
        "remote-b",
        "/workspace/b",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("error_kind", ["missing", "no_active"])
async def test_send_inputs_to_window_validates_owner_before_recovery_after_rebind(
    mgr: SessionManager,
    monkeypatch,
    error_kind: str,
) -> None:
    """A's failure must not clear or retry B after a same-window rebind."""
    monkeypatch.setattr(mgr, "_codex_app_server_mode_enabled", lambda: True)
    monkeypatch.setattr(
        mgr,
        "_local_machine_identity",
        lambda: ("local-node", "Local"),
    )
    window_id = "@920006"
    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=4,
        codex_thread_id="thread-a",
        window_id=window_id,
        cwd="/workspace/a",
        display_name="a",
        machine_id="local-node",
        machine_display_name="Local",
    )
    mgr.set_window_codex_active_turn_id(window_id, "turn-a")

    dispatch_started = asyncio.Event()
    release_failure = asyncio.Event()
    attempts = 0

    async def _send_inputs_via_codex_app_server(**_kwargs: object) -> tuple[bool, str]:
        nonlocal attempts
        attempts += 1
        dispatch_started.set()
        await release_failure.wait()
        if error_kind == "missing":
            raise session_mod.CodexAppServerError("thread not found: thread-a")
        raise session_mod.CodexAppServerError("no active turn to steer")

    monkeypatch.setattr(
        mgr,
        "_send_inputs_via_codex_app_server",
        _send_inputs_via_codex_app_server,
    )

    resume_calls: list[tuple[str, str, str]] = []

    async def _resume_exact(*, window_id: str, cwd: str, thread_id: str) -> str:
        resume_calls.append((window_id, cwd, thread_id))
        return thread_id

    monkeypatch.setattr(mgr, "resume_codex_session_for_window", _resume_exact)
    clear_calls: list[str] = []
    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "clear_active_turn",
        lambda thread_id: clear_calls.append(thread_id),
    )

    task = asyncio.create_task(
        mgr.send_inputs_to_window(
            window_id,
            [{"type": "text", "text": "continue"}],
            steer=error_kind == "no_active",
        )
    )
    await asyncio.wait_for(dispatch_started.wait(), timeout=1)

    mgr.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=4,
        codex_thread_id="thread-b",
        window_id=window_id,
        cwd="/workspace/b",
        display_name="b",
        machine_id="local-node",
        machine_display_name="Local",
    )
    mgr.set_window_codex_active_turn_id(window_id, "turn-b")
    release_failure.set()
    ok, message = await task

    assert ok is False
    assert message
    assert attempts == 1
    assert resume_calls == []
    assert clear_calls == []
    state = mgr.get_window_state(window_id)
    assert (state.codex_thread_id, state.cwd, state.codex_active_turn_id) == (
        "thread-b",
        "/workspace/b",
        "turn-b",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("topic_kind", ["named", "general"])
async def test_local_topic_rolls_over_stale_thread_after_aggregate_resume_limit(
    mgr: SessionManager,
    monkeypatch,
    topic_kind: str,
) -> None:
    """An isolated Telegram-live topic may commit a fresh thread after recovery."""
    monkeypatch.setattr(
        mgr,
        "_local_machine_identity",
        lambda: ("local-node", "Local"),
    )
    monkeypatch.setattr(mgr, "_codex_app_server_mode_enabled", lambda: True)

    async def _empty_goal_context(**_kwargs: object) -> str:
        return ""

    monkeypatch.setattr(mgr, "_build_live_goal_context", _empty_goal_context)
    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "get_active_turn_id",
        lambda _thread_id: "",
    )

    user_id = 100
    chat_id = -100123
    topic_id = 1 if topic_kind == "general" else 17
    window_id = "@930001"
    if topic_kind == "general":
        mgr.set_coco_control_topic(user_id, topic_id, chat_id=chat_id)
    mgr.bind_topic_to_codex_thread(
        user_id=user_id,
        thread_id=topic_id,
        chat_id=chat_id,
        codex_thread_id="thread-old",
        window_id=window_id,
        cwd="/workspace/topic",
        display_name="coco-control" if topic_kind == "general" else "named-topic",
        machine_id="local-node",
        machine_display_name="Local",
    )

    exact_resume_calls: list[tuple[str, str, str]] = []

    async def _resume_exact(*, window_id: str, cwd: str, thread_id: str) -> str:
        exact_resume_calls.append((window_id, cwd, thread_id))
        raise session_mod._CodexAggregateResumeLimitError(
            "Codex transcripts exceed aggregate resume limit"
        )

    monkeypatch.setattr(mgr, "resume_codex_session_for_window", _resume_exact)

    thread_start_calls: list[dict[str, object]] = []

    async def _thread_start(**kwargs: object) -> dict[str, object]:
        thread_start_calls.append(kwargs)
        return {"thread": {"id": "thread-fresh"}}

    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "thread_start",
        _thread_start,
    )

    turn_start_thread_ids: list[str] = []
    binding_before_fresh_turn: list[tuple[str, str]] = []

    async def _turn_start(**kwargs: object) -> dict[str, object]:
        thread_id = str(kwargs["thread_id"])
        turn_start_thread_ids.append(thread_id)
        if thread_id == "thread-old":
            raise session_mod.CodexAppServerError("thread not found: thread-old")
        binding = mgr.resolve_topic_binding(user_id, topic_id, chat_id=chat_id)
        assert binding is not None
        binding_before_fresh_turn.append(
            (binding.codex_thread_id, mgr.get_window_codex_thread_id(window_id))
        )
        return {"turn": {"id": "turn-fresh"}}

    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "turn_start",
        _turn_start,
    )

    ok, message = await mgr.send_topic_text_to_window(
        user_id=user_id,
        thread_id=topic_id,
        chat_id=chat_id,
        window_id=window_id,
        text="continue this named topic",
    )

    assert ok is True, message
    assert exact_resume_calls == [(window_id, "/workspace/topic", "thread-old")]
    assert len(thread_start_calls) == 1
    assert turn_start_thread_ids == ["thread-old", "thread-fresh"]
    # The canonical binding is not changed until the fresh turn is accepted.
    assert binding_before_fresh_turn == [("thread-old", "thread-fresh")]
    binding = mgr.resolve_topic_binding(user_id, topic_id, chat_id=chat_id)
    assert binding is not None
    assert binding.codex_thread_id == "thread-fresh"
    state = mgr.get_window_state(window_id)
    assert state.codex_thread_id == "thread-fresh"
    assert state.codex_active_turn_id == "turn-fresh"


@pytest.mark.asyncio
@pytest.mark.parametrize("topic_kind", ["named", "general"])
async def test_local_topic_structured_inputs_roll_over_with_topic_settings(
    mgr: SessionManager,
    monkeypatch,
    topic_kind: str,
) -> None:
    """Structured image inputs inherit topic settings and stale-thread rollover."""
    monkeypatch.setattr(
        mgr,
        "_local_machine_identity",
        lambda: ("local-node", "Local"),
    )
    monkeypatch.setattr(mgr, "_codex_app_server_mode_enabled", lambda: True)

    async def _empty_goal_context(**_kwargs: object) -> str:
        return ""

    monkeypatch.setattr(mgr, "_build_live_goal_context", _empty_goal_context)
    monkeypatch.setattr(mgr, "_build_coco_operator_context", lambda **_kwargs: "")
    monkeypatch.setattr(mgr, "resolve_thread_skills", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        mgr,
        "resolve_thread_codex_skills",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "get_active_turn_id",
        lambda _thread_id: "",
    )

    user_id = 100
    chat_id = -100123
    topic_id = 1 if topic_kind == "general" else 18
    window_id = "@930010"
    if topic_kind == "general":
        mgr.set_coco_control_topic(user_id, topic_id, chat_id=chat_id)
    mgr.bind_topic_to_codex_thread(
        user_id=user_id,
        thread_id=topic_id,
        chat_id=chat_id,
        codex_thread_id="thread-old",
        window_id=window_id,
        cwd="/workspace/topic",
        display_name="coco-control" if topic_kind == "general" else "image-topic",
        machine_id="local-node",
        machine_display_name="Local",
    )
    mgr.set_topic_model_selection(
        user_id,
        topic_id,
        chat_id=chat_id,
        model_slug="gpt-5.6-luna",
        reasoning_effort="max",
    )
    mgr.set_topic_service_tier_selection(
        user_id,
        topic_id,
        chat_id=chat_id,
        service_tier="fast",
    )

    async def _resume_exact(*, window_id: str, cwd: str, thread_id: str) -> str:
        raise session_mod._CodexAggregateResumeLimitError(
            "Codex transcripts exceed aggregate resume limit"
        )

    monkeypatch.setattr(mgr, "resume_codex_session_for_window", _resume_exact)

    async def _thread_start(**_kwargs: object) -> dict[str, object]:
        return {"thread": {"id": "thread-fresh"}}

    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "thread_start",
        _thread_start,
    )

    turn_start_calls: list[dict[str, object]] = []

    async def _turn_start(**kwargs: object) -> dict[str, object]:
        turn_start_calls.append(kwargs)
        if kwargs["thread_id"] == "thread-old":
            raise session_mod.CodexAppServerError("thread not found: thread-old")
        return {"turn": {"id": "turn-fresh"}}

    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "turn_start",
        _turn_start,
    )
    inputs = [
        {"type": "localImage", "path": "/workspace/topic/chart.png"},
        {"type": "text", "text": "Inspect this image"},
    ]

    ok, message = await mgr.send_topic_inputs_to_window(
        user_id=user_id,
        thread_id=topic_id,
        chat_id=chat_id,
        window_id=window_id,
        inputs=inputs,
    )

    assert ok is True, message
    assert [call["thread_id"] for call in turn_start_calls] == [
        "thread-old",
        "thread-fresh",
    ]
    assert all(call["inputs"][1:] == inputs for call in turn_start_calls)
    assert all(call["inputs"][0]["type"] == "text" for call in turn_start_calls)
    assert all(call["model"] == "gpt-5.6-luna" for call in turn_start_calls)
    assert all(call["effort"] == "max" for call in turn_start_calls)
    assert all(call["service_tier"] == "fast" for call in turn_start_calls)
    binding = mgr.resolve_topic_binding(user_id, topic_id, chat_id=chat_id)
    assert binding is not None
    assert binding.codex_thread_id == "thread-fresh"
    assert binding.cwd == "/workspace/topic"
    assert binding.display_name == (
        "coco-control" if topic_kind == "general" else "image-topic"
    )
    assert binding.machine_id == "local-node"
    assert binding.model_slug == "gpt-5.6-luna"
    assert binding.reasoning_effort == "max"
    assert binding.service_tier == "fast"
    assert mgr.get_window_codex_thread_id(window_id) == "thread-fresh"


@pytest.mark.asyncio
async def test_non_owner_general_binding_cannot_auto_roll_over(
    mgr: SessionManager,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        mgr,
        "_local_machine_identity",
        lambda: ("local-node", "Local"),
    )
    monkeypatch.setattr(mgr, "_codex_app_server_mode_enabled", lambda: True)

    async def _empty_goal_context(**_kwargs: object) -> str:
        return ""

    monkeypatch.setattr(mgr, "_build_live_goal_context", _empty_goal_context)
    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "get_active_turn_id",
        lambda _thread_id: "",
    )

    owner_user_id = 100
    caller_user_id = 200
    chat_id = -100123
    topic_id = 1
    window_id = "@930020"
    mgr.set_coco_control_topic(owner_user_id, topic_id, chat_id=chat_id)
    mgr._set_topic_binding(
        user_id=caller_user_id,
        thread_id=topic_id,
        chat_id=chat_id,
        binding=session_mod.TopicBinding(
            transport=session_mod.TOPIC_BINDING_TRANSPORT_CODEX_THREAD,
            chat_id=chat_id,
            thread_id=topic_id,
            window_id=window_id,
            codex_thread_id="thread-old",
            cwd="/workspace/topic",
            display_name="coco-control",
            sync_mode=session_mod.TOPIC_SYNC_MODE_TELEGRAM_LIVE,
            machine_id="local-node",
            machine_display_name="Local",
        ),
    )
    state = mgr.get_window_state(window_id)
    state.codex_thread_id = "thread-old"
    state.cwd = "/workspace/topic"
    state.window_name = "coco-control"

    async def _resume_exact(*, window_id: str, cwd: str, thread_id: str) -> str:
        raise session_mod._CodexAggregateResumeLimitError(
            "Codex transcripts exceed aggregate resume limit"
        )

    monkeypatch.setattr(mgr, "resume_codex_session_for_window", _resume_exact)
    thread_start_calls: list[dict[str, object]] = []

    async def _thread_start(**kwargs: object) -> dict[str, object]:
        thread_start_calls.append(kwargs)
        return {"thread": {"id": "thread-fresh"}}

    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "thread_start",
        _thread_start,
    )

    async def _turn_start(**kwargs: object) -> dict[str, object]:
        if kwargs["thread_id"] == "thread-old":
            raise session_mod.CodexAppServerError("thread not found: thread-old")
        return {"turn": {"id": "turn-fresh"}}

    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "turn_start",
        _turn_start,
    )

    ok, message = await mgr.send_topic_text_to_window(
        user_id=caller_user_id,
        thread_id=topic_id,
        chat_id=chat_id,
        window_id=window_id,
        text="continue this invalid control binding",
    )

    assert ok is False
    assert message
    assert thread_start_calls == []
    binding = mgr.resolve_topic_binding(caller_user_id, topic_id, chat_id=chat_id)
    assert binding is not None
    assert binding.codex_thread_id == "thread-old"
    assert mgr.get_window_codex_thread_id(window_id) == "thread-old"


@pytest.mark.asyncio
async def test_remote_topic_rejects_controller_local_structured_inputs(
    mgr: SessionManager,
    monkeypatch,
) -> None:
    """A remote topic must never receive controller-local attachment paths."""
    monkeypatch.setattr(
        mgr,
        "_local_machine_identity",
        lambda: ("local-node", "Local"),
    )
    monkeypatch.setattr(mgr, "_codex_app_server_mode_enabled", lambda: True)
    user_id = 100
    chat_id = -100123
    topic_id = 19
    window_id = "@930019"
    mgr.bind_topic_to_codex_thread(
        user_id=user_id,
        thread_id=topic_id,
        chat_id=chat_id,
        codex_thread_id="thread-remote",
        window_id=window_id,
        cwd="/workspace/remote",
        display_name="remote-image-topic",
        machine_id="remote-node",
        machine_display_name="Remote",
    )

    async def _unexpected_remote_send(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("controller-local image path reached remote transport")

    monkeypatch.setattr(
        agent_rpc_mod.agent_rpc_client,
        "send_inputs",
        _unexpected_remote_send,
    )

    ok, message = await mgr.send_topic_inputs_to_window(
        user_id=user_id,
        thread_id=topic_id,
        chat_id=chat_id,
        window_id=window_id,
        inputs=[{"type": "localImage", "path": "/controller/photo.png"}],
    )

    assert ok is False
    assert "remote sessions" in message


@pytest.mark.asyncio
async def test_named_local_topic_rollover_quarantines_fresh_thread_after_rebind(
    mgr: SessionManager,
    monkeypatch,
) -> None:
    """A fresh recovery thread must not remain attached to an orphaned window."""
    monkeypatch.setattr(
        mgr,
        "_local_machine_identity",
        lambda: ("local-node", "Local"),
    )
    user_id = 100
    chat_id = -100123
    topic_id = 117
    old_window_id = "@930011"
    new_window_id = "@930012"
    mgr.bind_topic_to_codex_thread(
        user_id=user_id,
        thread_id=topic_id,
        chat_id=chat_id,
        codex_thread_id="thread-old",
        window_id=old_window_id,
        cwd="/workspace/topic",
        display_name="named-topic",
        machine_id="local-node",
        machine_display_name="Local",
    )

    sends = 0

    async def _send_to_window(*_args: object, **_kwargs: object) -> tuple[bool, str]:
        nonlocal sends
        sends += 1
        if sends == 1:
            return (
                False,
                "App-server send failed: thread not found: thread-old; "
                "retry with new thread failed: Codex transcripts exceed "
                "aggregate resume limit",
            )
        mgr._set_window_codex_thread_cache(old_window_id, "thread-fresh")
        mgr.set_window_codex_active_turn_id(old_window_id, "turn-fresh")
        mgr.bind_topic_to_codex_thread(
            user_id=user_id,
            thread_id=topic_id,
            chat_id=chat_id,
            codex_thread_id="thread-rebound",
            window_id=new_window_id,
            cwd="/workspace/rebound",
            display_name="rebound-topic",
            machine_id="local-node",
            machine_display_name="Local",
        )
        return True, "fresh recovery turn accepted"

    monkeypatch.setattr(mgr, "send_to_window", _send_to_window)

    ok, message = await mgr.send_topic_text_to_window(
        user_id=user_id,
        thread_id=topic_id,
        chat_id=chat_id,
        window_id=old_window_id,
        text="continue this named topic",
    )

    assert ok is False
    assert "binding changed" in message
    rebound = mgr.resolve_topic_binding(user_id, topic_id, chat_id=chat_id)
    assert rebound is not None
    assert rebound.window_id == new_window_id
    assert rebound.codex_thread_id == "thread-rebound"
    assert mgr.get_window_codex_thread_id(old_window_id) == ""
    assert mgr.get_window_codex_active_turn_id(old_window_id) == ""


@pytest.mark.asyncio
async def test_named_remote_topic_rollover_retries_empty_thread_and_commits_fresh_binding(
    mgr: SessionManager,
    monkeypatch,
) -> None:
    """A remote rollover is accepted only after the controller proves both dispatches."""
    monkeypatch.setattr(
        mgr,
        "_local_machine_identity",
        lambda: ("local-node", "Local"),
    )
    monkeypatch.setattr(session_mod.node_registry, "get_node", lambda _mid: None)

    user_id = 100
    chat_id = -100123
    topic_id = 18
    window_id = "@930002"
    mgr.bind_topic_to_codex_thread(
        user_id=user_id,
        thread_id=topic_id,
        chat_id=chat_id,
        codex_thread_id="thread-old",
        window_id=window_id,
        cwd="/workspace/remote-topic",
        display_name="remote-topic",
        machine_id="remote-node",
        machine_display_name="Remote",
    )

    calls: list[dict[str, object]] = []

    def _transport_fields() -> dict[str, object]:
        return {
            "transport_epoch": "agent-epoch-1",
            "transport_epoch_started_at": 100.0,
            "transport_generation": 1,
            "transport_reset_sequence": 0,
            "transport_last_reset_generation": 0,
            "transport_last_reset_reason": "",
        }

    async def _send_inputs(machine_id: str, **kwargs: object) -> dict[str, object]:
        assert machine_id == "remote-node"
        calls.append(dict(kwargs))
        if len(calls) == 1:
            return {
                "ok": False,
                "message": (
                    "App-server send failed: thread not found: thread-old; "
                    "retry with new thread failed: Codex transcripts exceed "
                    "aggregate resume limit (999 > 100 bytes): thread-old"
                ),
                "thread_id": "thread-old",
                "turn_id": "",
                **_transport_fields(),
            }
        assert kwargs["thread_id"] == ""
        return {
            "ok": True,
            "message": "fresh remote dispatch",
            "thread_id": "thread-fresh",
            "turn_id": "turn-fresh",
            "dispatch_mode": "turn_start",
            **_transport_fields(),
        }

    monkeypatch.setattr(agent_rpc_mod.agent_rpc_client, "send_inputs", _send_inputs)

    ok, message = await mgr.send_topic_text_to_window(
        user_id=user_id,
        thread_id=topic_id,
        chat_id=chat_id,
        window_id=window_id,
        text="continue this remote topic",
    )

    assert ok is True, message
    assert [call["thread_id"] for call in calls] == ["thread-old", ""]
    binding = mgr.resolve_topic_binding(user_id, topic_id, chat_id=chat_id)
    assert binding is not None
    assert binding.codex_thread_id == "thread-fresh"
    assert mgr.get_window_codex_thread_id(window_id) == "thread-fresh"
    assert mgr.get_window_codex_active_turn_id(window_id) == "turn-fresh"


@pytest.mark.asyncio
async def test_named_remote_topic_rejects_unsolicited_mismatched_thread_response(
    mgr: SessionManager,
    monkeypatch,
) -> None:
    """A mismatched remote acknowledgement is not implicit rollover proof."""
    monkeypatch.setattr(
        mgr,
        "_local_machine_identity",
        lambda: ("local-node", "Local"),
    )
    monkeypatch.setattr(session_mod.node_registry, "get_node", lambda _mid: None)
    user_id = 100
    chat_id = -100123
    topic_id = 19
    window_id = "@930003"
    mgr.bind_topic_to_codex_thread(
        user_id=user_id,
        thread_id=topic_id,
        chat_id=chat_id,
        codex_thread_id="thread-old",
        window_id=window_id,
        cwd="/workspace/remote-topic",
        display_name="remote-topic",
        machine_id="remote-node",
        machine_display_name="Remote",
    )

    calls: list[dict[str, object]] = []

    async def _send_inputs(_machine_id: str, **kwargs: object) -> dict[str, object]:
        calls.append(dict(kwargs))
        return {
            "ok": True,
            "message": "unsolicited mismatched acknowledgement",
            "thread_id": "thread-unrelated",
            "turn_id": "turn-unrelated",
            "transport_epoch": "agent-epoch-1",
            "transport_epoch_started_at": 100.0,
            "transport_generation": 1,
        }

    monkeypatch.setattr(agent_rpc_mod.agent_rpc_client, "send_inputs", _send_inputs)

    ok, message = await mgr.send_topic_text_to_window(
        user_id=user_id,
        thread_id=topic_id,
        chat_id=chat_id,
        window_id=window_id,
        text="continue this remote topic",
    )

    assert ok is False
    assert "exact expected thread" in message
    assert [call["thread_id"] for call in calls] == ["thread-old"]
    binding = mgr.resolve_topic_binding(user_id, topic_id, chat_id=chat_id)
    assert binding is not None
    assert binding.codex_thread_id == "thread-old"
    assert mgr.get_window_codex_thread_id(window_id) == "thread-old"


@pytest.mark.asyncio
async def test_local_shared_window_bindings_do_not_auto_rollover(
    mgr: SessionManager,
    monkeypatch,
) -> None:
    """A shared topic window must not implicitly leave a stale thread behind."""
    monkeypatch.setattr(
        mgr,
        "_local_machine_identity",
        lambda: ("local-node", "Local"),
    )
    monkeypatch.setattr(mgr, "_codex_app_server_mode_enabled", lambda: True)

    async def _empty_goal_context(**_kwargs: object) -> str:
        return ""

    monkeypatch.setattr(mgr, "_build_live_goal_context", _empty_goal_context)
    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "get_active_turn_id",
        lambda _thread_id: "",
    )

    user_id = 100
    chat_id = -100123
    window_id = "@930004"
    topic_id = 20
    mgr.bind_topic_to_codex_thread(
        user_id=user_id,
        thread_id=topic_id,
        chat_id=chat_id,
        codex_thread_id="thread-old",
        window_id=window_id,
        cwd="/workspace/topic",
        display_name="shared-topic",
        machine_id="local-node",
        machine_display_name="Local",
    )
    mgr.bind_topic_to_codex_thread(
        user_id=200,
        thread_id=21,
        chat_id=chat_id,
        codex_thread_id="thread-old",
        window_id=window_id,
        cwd="/workspace/topic",
        display_name="other-topic",
        machine_id="local-node",
        machine_display_name="Local",
    )

    exact_resume_calls: list[tuple[str, str, str]] = []

    async def _resume_exact(*, window_id: str, cwd: str, thread_id: str) -> str:
        exact_resume_calls.append((window_id, cwd, thread_id))
        raise session_mod._CodexAggregateResumeLimitError(
            "Codex transcripts exceed aggregate resume limit"
        )

    monkeypatch.setattr(mgr, "resume_codex_session_for_window", _resume_exact)
    thread_start_calls: list[dict[str, object]] = []

    async def _thread_start(**kwargs: object) -> dict[str, object]:
        thread_start_calls.append(kwargs)
        return {"thread": {"id": "thread-fresh"}}

    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "thread_start",
        _thread_start,
    )
    turn_start_thread_ids: list[str] = []

    async def _turn_start(**kwargs: object) -> dict[str, object]:
        turn_start_thread_ids.append(str(kwargs["thread_id"]))
        raise session_mod.CodexAppServerError("thread not found: thread-old")

    monkeypatch.setattr(
        session_mod.codex_app_server_client,
        "turn_start",
        _turn_start,
    )

    ok, message = await mgr.send_topic_text_to_window(
        user_id=user_id,
        thread_id=topic_id,
        chat_id=chat_id,
        window_id=window_id,
        text="do not leave this binding implicitly",
    )

    assert ok is False
    assert message
    assert turn_start_thread_ids == ["thread-old"]
    assert exact_resume_calls == [(window_id, "/workspace/topic", "thread-old")]
    assert thread_start_calls == []
    binding = mgr.resolve_topic_binding(user_id, topic_id, chat_id=chat_id)
    assert binding is not None
    assert binding.codex_thread_id == "thread-old"
    state = mgr.get_window_state(window_id)
    assert state.codex_thread_id == "thread-old"
