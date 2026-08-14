"""Tests for /coco control-topic command behavior."""

import asyncio
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest, Forbidden

import coco.bot as bot
import coco.handlers.message_queue as mq
from coco.agent_rpc import RemoteCodexMutationUncertainError
from coco.session import CocoControlNotice, SessionManager


def _make_update(
    text: str,
    *,
    thread_id: int = 1,
    user_id: int = 1147817421,
    is_forum: bool = False,
):
    chat = SimpleNamespace(type="supergroup", id=-100123, is_forum=is_forum)
    message = SimpleNamespace(
        text=text,
        message_thread_id=thread_id,
        chat=chat,
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_message=message,
        effective_chat=chat,
        message=message,
    )


def test_get_thread_id_uses_general_topic_sentinel_for_forum_general():
    update = _make_update("hello", thread_id=None, is_forum=True)

    assert bot._get_thread_id(update) == 1


def test_get_thread_id_keeps_explicit_general_topic_id_without_forum_metadata():
    update = _make_update("hello", thread_id=1, is_forum=False)

    assert bot._get_thread_id(update) == 1


@pytest.mark.asyncio
async def test_startup_migrates_named_control_history_and_notifies_both_topics(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(bot.config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(bot.config, "sessions_path", tmp_path / "sessions")
    monkeypatch.setattr(bot.config, "browse_root", tmp_path)
    monkeypatch.setattr(bot.config, "config_dir", tmp_path)
    manager = SessionManager()
    state = manager.get_window_state("@42")
    state.cwd = "/projects/old-control"
    state.window_name = "old-control"
    state.codex_thread_id = "codex-history-123"
    manager.bind_topic_to_codex_thread(
        user_id=1147817421,
        thread_id=77,
        chat_id=-100123,
        codex_thread_id=state.codex_thread_id,
        cwd=state.cwd,
        display_name=state.window_name,
        window_id="@42",
    )
    manager.coco_control_topic = bot.CocoControlTopic(
        user_id=1147817421,
        thread_id=77,
        chat_id=-100123,
    )
    manager._save_state()
    sends: list[tuple[int, str, int | None]] = []

    monkeypatch.setattr(bot, "session_manager", manager)
    async def _safe_send(_bot, chat_id: int, text: str, **kwargs):
        sends.append((chat_id, text, kwargs.get("message_thread_id")))
        return SimpleNamespace(message_id=len(sends))

    monkeypatch.setattr(bot, "safe_send", _safe_send)

    migration = await bot._migrate_coco_control_to_general(SimpleNamespace())

    assert migration is not None
    general = manager.resolve_topic_binding(1147817421, 1, chat_id=-100123)
    assert general is not None
    assert general.window_id == "@42"
    assert general.codex_thread_id == "codex-history-123"
    assert general.cwd == str(tmp_path / "_coco" / "chat-100123" / "control")
    assert [thread_id for _chat, _text, thread_id in sends] == [77, 1]
    assert "moved permanently to General" in sends[0][1]
    assert "permanent CoCo control channel" in sends[1][1]


@pytest.mark.asyncio
async def test_startup_archives_existing_general_workspace_and_starts_control(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(bot.config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(bot.config, "sessions_path", tmp_path / "sessions")
    monkeypatch.setattr(bot.config, "browse_root", tmp_path)
    monkeypatch.setattr(bot.config, "config_dir", tmp_path)
    manager = SessionManager()
    state = manager.get_window_state("@42")
    state.cwd = "/projects/legacy-control"
    state.codex_thread_id = "codex-history-123"
    manager.bind_topic_to_codex_thread(
        user_id=1147817421,
        thread_id=1,
        chat_id=-100123,
        codex_thread_id=state.codex_thread_id,
        cwd=state.cwd,
        display_name="legacy-control",
        window_id="@42",
    )
    manager.set_coco_control_topic(1147817421, 1, chat_id=-100123)
    monkeypatch.setattr(bot, "session_manager", manager)

    migration = await bot._migrate_coco_control_to_general(SimpleNamespace())
    binding = manager.resolve_topic_binding(1147817421, 1, chat_id=-100123)

    assert migration is None
    assert binding is not None
    assert binding.cwd == str(tmp_path / "_coco" / "chat-100123" / "control")
    assert binding.codex_thread_id == ""
    archived = list(manager.coco_control_archives.values())
    assert len(archived) == 1
    assert archived[0].cwd == "/projects/legacy-control"
    assert archived[0].codex_thread_id == "codex-history-123"


def test_general_bootstrap_repairs_an_orphaned_persisted_reservation(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(bot.config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(bot.config, "sessions_path", tmp_path / "sessions")
    monkeypatch.setattr(bot.config, "config_dir", tmp_path)
    manager = SessionManager()
    manager.coco_control_topics[-100123] = bot.CocoControlTopic(
        user_id=42,
        thread_id=1,
        chat_id=-100123,
    )
    monkeypatch.setattr(bot, "session_manager", manager)

    binding = bot._ensure_default_coco_general_control(
        user_id=999,
        thread_id=1,
        chat_id=-100123,
    )

    assert binding is not None
    assert binding.window_id
    assert binding.cwd == str(tmp_path / "_coco" / "chat-100123" / "control")
    assert manager.resolve_topic_binding(42, 1, chat_id=-100123) == binding
    assert manager.resolve_topic_binding(999, 1, chat_id=-100123) is None


def test_general_bootstrap_archives_existing_session_and_uses_admin_owner(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(bot.config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(bot.config, "sessions_path", tmp_path / "sessions")
    monkeypatch.setattr(bot.config, "config_dir", tmp_path)
    manager = SessionManager()
    manager.bind_topic_to_codex_thread(
        user_id=999,
        thread_id=1,
        chat_id=-100123,
        codex_thread_id="ordinary-general-history",
        cwd="/projects/ordinary-general",
        window_id="@99",
    )
    monkeypatch.setattr(bot, "session_manager", manager)
    monkeypatch.setattr(bot, "_get_allowed_admins", lambda: {42})

    binding = bot._ensure_default_coco_general_control(
        user_id=999,
        thread_id=1,
        chat_id=-100123,
    )

    assert manager.get_coco_control_topic(-100123) == bot.CocoControlTopic(
        42, 1, -100123
    )
    assert binding is not None
    assert binding.codex_thread_id == ""
    assert binding.cwd == str(tmp_path / "_coco" / "chat-100123" / "control")
    archived = list(manager.coco_control_archives.values())
    assert len(archived) == 1
    assert archived[0].codex_thread_id == "ordinary-general-history"
    assert archived[0].cwd == "/projects/ordinary-general"


def test_general_bootstrap_uses_admin_when_non_admin_arrives_first(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(bot.config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(bot.config, "sessions_path", tmp_path / "sessions")
    monkeypatch.setattr(bot.config, "config_dir", tmp_path)
    manager = SessionManager()
    monkeypatch.setattr(bot, "session_manager", manager)
    monkeypatch.setattr(bot, "_get_allowed_admins", lambda: {42})

    binding = bot._ensure_default_coco_general_control(
        user_id=999,
        thread_id=1,
        chat_id=-100123,
    )

    assert binding is not None
    assert manager.get_coco_control_topic(-100123) == bot.CocoControlTopic(
        42, 1, -100123
    )
    assert manager._get_persisted_topic_binding(42, 1, chat_id=-100123) is not None
    assert 999 not in manager.topic_bindings_v2


def test_general_bootstrap_fences_all_noncanonical_general_bindings(
    monkeypatch,
    tmp_path,
):
    """A reserved owner must fence every stale same-chat General binding."""
    monkeypatch.setattr(bot.config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(bot.config, "sessions_path", tmp_path / "sessions")
    monkeypatch.setattr(bot.config, "config_dir", tmp_path)
    manager = SessionManager()

    canonical = manager.ensure_topic_binding(100, 1, chat_id=-100123)
    assert canonical is not None
    canonical.display_name = "coco-control"
    manager.bind_topic_to_codex_thread(
        user_id=200,
        thread_id=1,
        chat_id=-100123,
        codex_thread_id="stale-general-200",
        cwd="/projects/stale-200",
        window_id="@200",
    )
    stale_201 = manager.ensure_topic_binding(201, 1, chat_id=-100123)
    assert stale_201 is not None
    stale_201.cwd = "/projects/stale-201"
    stale_201.window_id = "@201"
    manager.bind_topic_to_codex_thread(
        user_id=300,
        thread_id=1,
        chat_id=-100999,
        codex_thread_id="other-chat-general",
        cwd="/projects/other-chat",
        window_id="@300",
    )
    manager.coco_control_topics[-100123] = bot.CocoControlTopic(100, 1, -100123)
    monkeypatch.setattr(bot, "session_manager", manager)

    binding = bot._ensure_default_coco_general_control(
        user_id=999,
        thread_id=1,
        chat_id=-100123,
    )

    assert binding is not None
    assert manager.resolve_topic_binding(100, 1, chat_id=-100123) is not None
    assert manager.resolve_topic_binding(200, 1, chat_id=-100123) is None
    assert manager.resolve_topic_binding(201, 1, chat_id=-100123) is None
    assert manager.find_users_for_codex_thread("stale-general-200") == []
    assert manager.find_users_for_codex_thread("other-chat-general") == [
        (300, -100999, "@300", 1)
    ]
    archived = list(manager.coco_control_archives.values())
    assert {entry.codex_thread_id or entry.cwd for entry in archived} == {
        "stale-general-200",
        "/projects/stale-201",
    }

    # Re-running activation must not duplicate archived history or change the
    # canonical owner binding.
    canonical_window = binding.window_id
    binding_again = bot._ensure_default_coco_general_control(
        user_id=999,
        thread_id=1,
        chat_id=-100123,
    )
    assert binding_again is not None
    assert binding_again.window_id == canonical_window
    assert len(manager.coco_control_archives) == 2


def test_general_bootstrap_does_not_claim_control_without_configured_admin(
    monkeypatch,
    tmp_path,
):
    """An ordinary first sender must not become the group's control owner."""
    monkeypatch.setattr(bot.config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(bot.config, "sessions_path", tmp_path / "sessions")
    monkeypatch.setattr(bot.config, "config_dir", tmp_path)
    manager = SessionManager()
    monkeypatch.setattr(bot, "session_manager", manager)
    monkeypatch.setattr(bot, "_get_allowed_admins", lambda: set())

    binding = bot._ensure_default_coco_general_control(
        user_id=999,
        thread_id=1,
        chat_id=-100123,
    )

    assert binding is None
    assert manager.get_coco_control_topic(-100123) is None
    assert manager.resolve_topic_binding(999, 1, chat_id=-100123) is None


@pytest.mark.asyncio
async def test_startup_does_not_reinitialize_collision_preserved_general(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(bot.config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(bot.config, "sessions_path", tmp_path / "sessions")
    manager = SessionManager()
    manager.set_coco_control_topic(100, 1, chat_id=-100123)
    state = manager.get_window_state("@1")
    state.cwd = "/projects/general-window"
    manager.bind_thread(100, 1, "@1", chat_id=-100123)
    manager.coco_control_migration_conflicts[-100123] = "preserved collision"
    monkeypatch.setattr(bot, "session_manager", manager)
    monkeypatch.setattr(
        bot,
        "_ensure_default_coco_general_control",
        lambda **_kwargs: pytest.fail("preserved General must not be rewritten"),
    )

    migration = await bot._migrate_coco_control_to_general(SimpleNamespace())

    assert migration is None
    binding = manager.resolve_topic_binding(100, 1, chat_id=-100123)
    assert binding is not None and binding.cwd == "/projects/general-window"


@pytest.mark.asyncio
async def test_remote_workspace_allocation_is_fenced_to_source_binding(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(bot.config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(bot.config, "sessions_path", tmp_path / "sessions")
    manager = SessionManager()
    manager.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=77,
        chat_id=-100123,
        codex_thread_id="legacy-history",
        cwd="/old",
        window_id="@77",
        machine_id="node-a",
    )
    manager.coco_control_topic = bot.CocoControlTopic(100, 77, -100123)
    monkeypatch.setattr(bot, "session_manager", manager)

    async def _allocate(_control):
        binding = manager._get_persisted_topic_binding(100, 77, chat_id=-100123)
        assert binding is not None
        binding.machine_id = "node-b"
        binding.window_id = "@78"
        manager._save_state()
        return "/node-a/control"

    monkeypatch.setattr(bot, "_control_workspace_for_migration", _allocate)

    migration = await bot._migrate_coco_control_to_general(SimpleNamespace())

    assert migration is None
    assert manager.get_coco_control_topic(-100123).thread_id == 77
    rebound = manager.resolve_topic_binding(100, 77, chat_id=-100123)
    assert rebound is not None and rebound.machine_id == "node-b"
    assert rebound.cwd == "/old"


@pytest.mark.asyncio
async def test_remote_workspace_timeout_keeps_control_pending_for_retry(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(bot.config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(bot.config, "sessions_path", tmp_path / "sessions")
    manager = SessionManager()
    manager.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=77,
        chat_id=-100123,
        codex_thread_id="legacy-history",
        cwd="/old",
        window_id="@77",
        machine_id="offline-node",
    )
    control = bot.CocoControlTopic(100, 77, -100123)
    manager.coco_control_topic = control
    monkeypatch.setattr(bot, "session_manager", manager)
    monkeypatch.setattr(
        bot,
        "node_registry",
        SimpleNamespace(
            local_machine_id="local-node",
            get_node=lambda _machine_id: SimpleNamespace(
                machine_id="local-node",
                display_name="Local Node",
                status=bot.NODE_STATUS_ONLINE,
            )
        ),
    )
    monkeypatch.setattr(bot, "_COCO_CONTROL_WORKSPACE_RPC_TIMEOUT_SECONDS", 0.01)
    started = asyncio.Event()

    async def _never_finishes(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        bot.agent_rpc_client,
        "ensure_control_workspace",
        _never_finishes,
    )

    migration_task = asyncio.create_task(
        bot._migrate_coco_control_to_general(SimpleNamespace())
    )
    await asyncio.wait_for(started.wait(), timeout=0.2)
    migration = await asyncio.wait_for(migration_task, timeout=0.2)

    assert migration is None
    assert manager.get_coco_control_topic(-100123) == control
    assert manager.resolve_topic_binding(100, 77, chat_id=-100123).cwd == "/old"
    assert manager.coco_control_archives == {}


@pytest.mark.asyncio
async def test_remote_general_workspace_allocation_persists_before_bootstrap_retry(
    monkeypatch,
    tmp_path,
):
    """A remote General allocation must survive detached binding resolution."""
    monkeypatch.setattr(bot.config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(bot.config, "sessions_path", tmp_path / "sessions")
    manager = SessionManager()
    binding = manager.ensure_topic_binding(100, 1, chat_id=-100123)
    assert binding is not None
    binding.machine_id = "remote-node"
    binding.machine_display_name = "Remote Node"
    manager.set_coco_control_topic(100, 1, chat_id=-100123)
    monkeypatch.setattr(bot, "session_manager", manager)
    monkeypatch.setattr(
        bot,
        "_local_machine_identity",
        lambda: ("local-node", "Local"),
    )
    workspace_dir = "/remote/coco-control"
    rpc_calls = 0

    async def _allocate(_machine_id, *, chat_id):
        nonlocal rpc_calls
        rpc_calls += 1
        assert chat_id == -100123
        return {"workspace_path": workspace_dir}

    monkeypatch.setattr(
        bot.agent_rpc_client,
        "ensure_control_workspace",
        _allocate,
    )

    await bot._migrate_coco_control_to_general(SimpleNamespace())

    assert rpc_calls == 1
    assert manager.coco_control_archives == {}
    persisted = manager._get_persisted_topic_binding(100, 1, chat_id=-100123)
    assert persisted is not None
    assert persisted.chat_id == -100123
    assert persisted.thread_id == 1
    assert persisted.cwd == workspace_dir
    assert persisted.machine_id == "remote-node"
    assert persisted.codex_thread_id == ""
    assert persisted.window_id
    window_id = persisted.window_id

    reloaded = SessionManager()
    reloaded_binding = reloaded._get_persisted_topic_binding(
        100,
        1,
        chat_id=-100123,
    )
    assert reloaded_binding is not None
    assert reloaded_binding.cwd == workspace_dir
    assert reloaded_binding.machine_id == "remote-node"
    assert reloaded_binding.window_id == window_id

    retry = bot._ensure_default_coco_general_control(
        user_id=999,
        thread_id=1,
        chat_id=-100123,
    )
    assert retry is not None
    assert retry.window_id == window_id
    assert retry.cwd == workspace_dir
    assert rpc_calls == 1


@pytest.mark.asyncio
async def test_existing_general_is_preserved_while_legacy_remote_is_offline(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(bot.config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(bot.config, "sessions_path", tmp_path / "sessions")
    manager = SessionManager()
    manager.bind_topic_to_codex_thread(
        user_id=100,
        thread_id=77,
        chat_id=-100123,
        codex_thread_id="legacy-history",
        cwd="/remote/legacy",
        window_id="@77",
        machine_id="offline-node",
    )
    manager.bind_topic_to_codex_thread(
        user_id=200,
        thread_id=1,
        chat_id=-100123,
        codex_thread_id="general-history",
        cwd="/general",
        window_id="@1",
    )
    manager.coco_control_topic = bot.CocoControlTopic(100, 77, -100123)
    monkeypatch.setattr(bot, "session_manager", manager)

    async def _offline(_control):
        raise RuntimeError("offline")

    monkeypatch.setattr(bot, "_control_workspace_for_migration", _offline)

    migration = await bot._migrate_coco_control_to_general(SimpleNamespace())

    assert migration is None
    assert manager.get_coco_control_topic(-100123) == bot.CocoControlTopic(
        100,
        77,
        -100123,
    )
    assert manager.resolve_topic_binding(200, 1, chat_id=-100123).codex_thread_id == (
        "general-history"
    )
    assert manager.coco_control_archives == {}


@pytest.mark.asyncio
async def test_migration_notices_retry_after_failed_telegram_send(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(bot.config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(bot.config, "sessions_path", tmp_path / "sessions")
    monkeypatch.setattr(bot.config, "config_dir", tmp_path)
    manager = SessionManager()
    manager.bind_topic_to_codex_thread(
        user_id=1147817421,
        thread_id=77,
        chat_id=-100123,
        codex_thread_id="history",
        cwd="/old",
        window_id="@77",
    )
    manager.coco_control_topic = bot.CocoControlTopic(1147817421, 77, -100123)
    monkeypatch.setattr(bot, "session_manager", manager)
    attempts: list[int] = []

    async def _safe_send(_bot, _chat_id, _text, **kwargs):
        attempts.append(kwargs["message_thread_id"])
        return None

    monkeypatch.setattr(bot, "safe_send", _safe_send)
    await bot._migrate_coco_control_to_general(SimpleNamespace())
    assert len(list(manager.iter_pending_coco_control_notices())) == 2

    async def _successful_send(_bot, _chat_id, _text, **kwargs):
        attempts.append(kwargs["message_thread_id"])
        return SimpleNamespace(message_id=len(attempts))

    monkeypatch.setattr(bot, "safe_send", _successful_send)
    for notice in manager.pending_coco_control_notices.values():
        notice.next_attempt_at = 0
    await bot._migrate_coco_control_to_general(SimpleNamespace())
    assert list(manager.iter_pending_coco_control_notices()) == []
    assert attempts == [77, 1, 77, 1]


@pytest.mark.asyncio
async def test_migration_notice_failures_use_exponential_backoff(monkeypatch, tmp_path):
    monkeypatch.setattr(bot.config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(bot.config, "sessions_path", tmp_path / "sessions")
    manager = SessionManager()
    manager.pending_coco_control_notices["notice"] = CocoControlNotice(
        notice_id="notice",
        chat_id=-100123,
        thread_id=77,
        text="migration notice",
    )
    monkeypatch.setattr(bot, "session_manager", manager)
    attempts = 0

    async def _failed_send(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        return None

    monkeypatch.setattr(bot, "safe_send", _failed_send)
    monkeypatch.setattr(bot.time, "time", lambda: 1000.0)

    await bot._flush_coco_control_notices(SimpleNamespace())
    await bot._flush_coco_control_notices(SimpleNamespace())

    notice = manager.pending_coco_control_notices["notice"]
    assert attempts == 1
    assert notice.attempts == 1
    assert notice.next_attempt_at == 1030.0


@pytest.mark.asyncio
async def test_migration_notice_retries_forbidden_send_failure(monkeypatch, tmp_path):
    """A transient Telegram Forbidden must remain in the durable outbox."""
    monkeypatch.setattr(bot.config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(bot.config, "sessions_path", tmp_path / "sessions")
    manager = SessionManager()
    manager.pending_coco_control_notices["notice"] = CocoControlNotice(
        notice_id="notice",
        chat_id=-100123,
        thread_id=77,
        text="migration notice",
    )
    monkeypatch.setattr(bot, "session_manager", manager)
    monkeypatch.setattr(bot.time, "time", lambda: 1000.0)

    async def _forbidden_send(*_args, **_kwargs):
        raise Forbidden("bot lacks permission temporarily")

    monkeypatch.setattr(bot, "safe_send", _forbidden_send)

    delivered = await bot._flush_coco_control_notices(SimpleNamespace())

    assert delivered == 0
    notice = manager.pending_coco_control_notices["notice"]
    assert notice.attempts == 1
    assert notice.next_attempt_at == 1030.0


@pytest.mark.asyncio
async def test_migration_notice_drops_permanently_invalid_topic(monkeypatch, tmp_path):
    monkeypatch.setattr(bot.config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(bot.config, "sessions_path", tmp_path / "sessions")
    manager = SessionManager()
    manager.pending_coco_control_notices["notice"] = CocoControlNotice(
        notice_id="notice",
        chat_id=-100123,
        thread_id=77,
        text="migration notice",
    )
    monkeypatch.setattr(bot, "session_manager", manager)

    async def _failed_send(*_args, **_kwargs):
        raise BadRequest("Topic_id_invalid")

    monkeypatch.setattr(bot, "safe_send", _failed_send)

    delivered = await bot._flush_coco_control_notices(SimpleNamespace())

    assert delivered == 0
    assert list(manager.iter_pending_coco_control_notices()) == []


@pytest.mark.parametrize(
    ("error_message", "expected_pending", "expected_attempts", "expected_next_attempt_at"),
    [
        ("TOPIC_CLOSED", True, 1, 1030.0),
        ("TOPIC_DELETED", False, 0, 0.0),
    ],
)
@pytest.mark.asyncio
async def test_migration_notice_retries_closed_but_dead_letters_deleted_topic(
    monkeypatch,
    tmp_path,
    error_message,
    expected_pending,
    expected_attempts,
    expected_next_attempt_at,
):
    monkeypatch.setattr(bot.config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(bot.config, "sessions_path", tmp_path / "sessions")
    manager = SessionManager()
    manager.pending_coco_control_notices["notice"] = CocoControlNotice(
        notice_id="notice",
        chat_id=-100123,
        thread_id=77,
        text="migration notice",
    )
    monkeypatch.setattr(bot, "session_manager", manager)
    monkeypatch.setattr(bot.time, "time", lambda: 1000.0)

    async def _failed_send(*_args, **_kwargs):
        raise BadRequest(error_message)

    monkeypatch.setattr(bot, "safe_send", _failed_send)

    delivered = await bot._flush_coco_control_notices(SimpleNamespace())

    assert delivered == 0
    if expected_pending:
        notice = manager.pending_coco_control_notices["notice"]
        assert notice.attempts == expected_attempts
        assert notice.next_attempt_at == expected_next_attempt_at
    else:
        assert list(manager.iter_pending_coco_control_notices()) == []


@pytest.mark.asyncio
async def test_coco_command_requires_named_topic(monkeypatch):
    update = _make_update("/coco", thread_id=None)
    replies: list[tuple[str, object]] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)

    async def _safe_reply(_message, text: str, **kwargs):
        replies.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.coco_command(update, SimpleNamespace(user_data={}))

    assert replies == [("❌ Use `/coco` inside a named topic.", None)]


@pytest.mark.asyncio
async def test_coco_command_makes_forum_general_the_default_control_topic(
    monkeypatch,
    tmp_path,
):
    update = _make_update("/coco", thread_id=None, is_forum=True)
    replies: list[tuple[str, object]] = []

    monkeypatch.setattr(bot.config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(bot.config, "sessions_path", tmp_path / "sessions")
    monkeypatch.setattr(bot.config, "browse_root", tmp_path)
    monkeypatch.setattr(bot.config, "config_dir", tmp_path)
    manager = SessionManager()
    monkeypatch.setattr(bot, "session_manager", manager)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)

    async def _safe_reply(_message, text: str, **kwargs):
        replies.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.coco_command(update, SimpleNamespace(user_data={}))

    assert manager.is_coco_control_topic(
        1147817421,
        1,
        chat_id=-100123,
    )
    control = manager.get_coco_control_topic(-100123)
    assert control is not None
    binding = manager.resolve_topic_binding(control.user_id, 1, chat_id=-100123)
    assert binding is not None
    assert binding.window_id
    assert binding.cwd == str(tmp_path / "_coco" / "chat-100123" / "control")
    assert len(replies) == 1
    text, markup = replies[0]
    assert "CoCo dashboard" in text
    assert [button.text for row in markup.inline_keyboard for button in row] == [
        "Doctor",
        "Refresh",
    ]


@pytest.mark.asyncio
async def test_coco_command_rejects_named_topic_as_control(
    monkeypatch,
    tmp_path,
):
    update = _make_update("/coco", thread_id=77, is_forum=True)
    replies: list[tuple[str, object]] = []

    monkeypatch.setattr(bot.config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(bot.config, "sessions_path", tmp_path / "sessions")
    monkeypatch.setattr(bot.config, "browse_root", tmp_path)
    monkeypatch.setattr(bot.config, "config_dir", tmp_path)
    manager = SessionManager()
    manager.set_coco_control_topic(1147817421, 1, chat_id=-100123)
    monkeypatch.setattr(bot, "session_manager", manager)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)

    async def _safe_reply(_message, text: str, **kwargs):
        replies.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.coco_command(update, SimpleNamespace(user_data={}))

    assert manager.is_coco_control_topic(1147817421, 1, chat_id=-100123)
    assert manager.resolve_topic_binding(1147817421, 77, chat_id=-100123) is None
    assert replies == [
        (
            "ℹ️ CoCo control is permanently assigned to General. Use `/coco` there.",
            None,
        )
    ]


@pytest.mark.asyncio
async def test_coco_doctor_reports_healthy_general_binding(monkeypatch, tmp_path):
    update = _make_update("/coco doctor", thread_id=1, is_forum=True)
    replies: list[str] = []
    monkeypatch.setattr(bot.config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(bot.config, "sessions_path", tmp_path / "sessions")
    monkeypatch.setattr(bot.config, "config_dir", tmp_path)
    manager = SessionManager()
    monkeypatch.setattr(bot, "session_manager", manager)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)
    await bot.coco_command(update, SimpleNamespace(user_data={}, bot=object()))
    assert len(replies) == 1
    assert "CoCo doctor" in replies[0]
    assert "per-group General reservation" in replies[0]
    assert "Control health is good" in replies[0]


@pytest.mark.asyncio
async def test_coco_doctor_callback_denies_unauthorized_member_before_diagnostics(
    monkeypatch,
):
    chat = SimpleNamespace(type="supergroup", id=-100123, is_forum=True)
    message = SimpleNamespace(message_thread_id=1, chat=chat, chat_id=chat.id)
    answers: list[tuple[str, bool]] = []
    edits: list[str] = []
    built: list[bool] = []

    async def _answer(text: str, *, show_alert: bool = False):
        answers.append((text, show_alert))

    async def _safe_edit(_query, text: str, **_kwargs):
        edits.append(text)

    query = SimpleNamespace(data=bot.CB_COCO_DOCTOR, message=message, answer=_answer)
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=chat,
        effective_user=SimpleNamespace(id=999),
        effective_message=message,
    )

    monkeypatch.setattr(bot, "_is_chat_allowed", lambda _chat: True)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: False)
    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(100, 1, -100123),
    )
    async def _build_doctor(_chat_id):
        built.append(True)
        return "sensitive diagnostics"

    monkeypatch.setattr(bot, "_build_coco_doctor_text", _build_doctor)
    monkeypatch.setattr(bot, "safe_edit", _safe_edit)

    await bot.callback_handler(update, SimpleNamespace(user_data={}, bot=object()))

    assert answers == [(bot._COCO_CONTROL_PERMISSION_DENIED_TEXT, True)]
    assert built == []
    assert edits == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "callback_data",
    [
        bot.CB_COCO_PAGE + "1",
        bot.CB_COCO_REFRESH,
        bot.CB_COCO_DASHBOARD,
    ],
)
async def test_non_owner_member_can_navigate_coco_dashboard_in_general(
    monkeypatch,
    callback_data,
):
    """Read-only dashboard navigation is shared by every allowlisted member."""
    chat = SimpleNamespace(type="supergroup", id=-100123, is_forum=True)
    message = SimpleNamespace(message_thread_id=1, chat=chat, chat_id=chat.id)
    answers: list[str] = []
    edits: list[str] = []
    events: list[str] = []

    async def _answer(text: str, **_kwargs):
        answers.append(text)

    async def _safe_edit(_query, text: str, **_kwargs):
        edits.append(text)

    query = SimpleNamespace(data=callback_data, message=message, answer=_answer)
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=chat,
        effective_user=SimpleNamespace(id=999),
        effective_message=message,
    )

    monkeypatch.setattr(bot, "_is_chat_allowed", lambda _chat: True)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: False)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(100, 1, -100123),
    )
    monkeypatch.setattr(
        bot,
        "_ensure_default_coco_general_control",
        lambda **_kwargs: events.append("materialize"),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: events.append("set-group"),
    )
    monkeypatch.setattr(
        bot,
        "_build_coco_dashboard",
        lambda **_kwargs: ("shared dashboard", object()),
    )
    monkeypatch.setattr(bot, "safe_edit", _safe_edit)

    await bot.callback_handler(update, SimpleNamespace(user_data={}, bot=object()))

    assert edits == ["shared dashboard"]
    assert answers == ["Refreshed"]
    assert events == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("callback_data", "control", "expected"),
    [
        (
            bot.CB_COCO_PAGE + "bad",
            bot.CocoControlTopic(100, 1, -100123),
            "This dashboard page is stale.",
        ),
        (
            bot.CB_COCO_PAGE + "0",
            None,
            bot._COCO_CONTROL_UNCONFIGURED_TEXT,
        ),
        (
            bot.CB_COCO_PAGE + "0",
            bot.CocoControlTopic(100, 77, -100123),
            bot._COCO_CONTROL_MIGRATION_PENDING_TEXT,
        ),
    ],
)
async def test_coco_dashboard_navigation_fails_closed_before_rendering(
    monkeypatch,
    callback_data,
    control,
    expected,
):
    """Forged/stale navigation cannot bypass General's foundational state."""
    chat = SimpleNamespace(type="supergroup", id=-100123, is_forum=True)
    message = SimpleNamespace(message_thread_id=1, chat=chat, chat_id=chat.id)
    answers: list[tuple[str, bool]] = []
    events: list[str] = []

    async def _answer(text: str, **kwargs):
        answers.append((text, bool(kwargs.get("show_alert", False))))

    query = SimpleNamespace(data=callback_data, message=message, answer=_answer)
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=chat,
        effective_user=SimpleNamespace(id=999),
        effective_message=message,
    )

    monkeypatch.setattr(bot, "_is_chat_allowed", lambda _chat: True)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: False)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: control,
    )
    monkeypatch.setattr(
        bot,
        "_ensure_default_coco_general_control",
        lambda **_kwargs: events.append("materialize"),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: events.append("set-group"),
    )
    monkeypatch.setattr(
        bot,
        "_build_coco_dashboard",
        lambda **_kwargs: events.append("render") or ("dashboard", object()),
    )

    await bot.callback_handler(update, SimpleNamespace(user_data={}, bot=object()))

    assert answers == [(expected, True)]
    assert events == []


@pytest.mark.asyncio
async def test_non_owner_member_still_cannot_mutate_general_from_callback(
    monkeypatch,
):
    """Dashboard navigation exemption must not weaken General mutation auth."""
    chat = SimpleNamespace(type="supergroup", id=-100123, is_forum=True)
    message = SimpleNamespace(message_thread_id=1, chat=chat, chat_id=chat.id)
    answers: list[tuple[str, bool]] = []

    async def _answer(text: str, **kwargs):
        answers.append((text, bool(kwargs.get("show_alert", False))))

    query = SimpleNamespace(data=bot.CB_MODEL_REFRESH, message=message, answer=_answer)
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=chat,
        effective_user=SimpleNamespace(id=999),
        effective_message=message,
    )

    monkeypatch.setattr(bot, "_is_chat_allowed", lambda _chat: True)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: False)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(100, 1, -100123),
    )

    await bot.callback_handler(update, SimpleNamespace(user_data={}, bot=object()))

    assert answers == [(bot._COCO_CONTROL_PERMISSION_DENIED_TEXT, True)]


@pytest.mark.asyncio
async def test_coco_doctor_callback_allows_authorized_admin_and_builds_diagnostics(
    monkeypatch,
):
    chat = SimpleNamespace(type="supergroup", id=-100123, is_forum=True)
    message = SimpleNamespace(message_thread_id=1, chat=chat, chat_id=chat.id)
    answers: list[str] = []
    edits: list[str] = []
    scheduled_tasks: list[asyncio.Task] = []

    async def _answer(text: str, **_kwargs):
        answers.append(text)

    async def _safe_edit(_query, text: str, **_kwargs):
        edits.append(text)

    def _create_task(coro, **_kwargs):
        task = asyncio.create_task(coro)
        scheduled_tasks.append(task)
        return task

    query = SimpleNamespace(data=bot.CB_COCO_DOCTOR, message=message, answer=_answer)
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=chat,
        effective_user=SimpleNamespace(id=200),
        effective_message=message,
    )

    monkeypatch.setattr(bot, "_is_chat_allowed", lambda _chat: True)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda uid: uid == 200)
    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(100, 1, -100123),
    )
    async def _build_doctor(_chat_id):
        return "safe diagnostics"

    monkeypatch.setattr(bot, "_build_coco_doctor_text", _build_doctor)
    monkeypatch.setattr(bot, "safe_edit", _safe_edit)

    await bot.callback_handler(
        update,
        SimpleNamespace(
            user_data={},
            bot=object(),
            application=SimpleNamespace(create_task=_create_task),
        ),
    )
    await asyncio.gather(*scheduled_tasks)

    assert edits == ["safe diagnostics"]
    assert answers == ["Doctor running…"]


@pytest.mark.asyncio
async def test_coco_doctor_callback_acknowledges_before_background_diagnostics(
    monkeypatch,
):
    """A slow doctor check must not hold the callback update open."""
    chat = SimpleNamespace(type="supergroup", id=-100123, is_forum=True)
    message = SimpleNamespace(message_thread_id=1, chat=chat, chat_id=chat.id)
    events: list[str] = []
    edits: list[str] = []
    builder_started = asyncio.Event()
    release_builder = asyncio.Event()
    scheduled_tasks: list[asyncio.Task] = []

    async def _answer(text: str, **_kwargs):
        events.append(f"answer:{text}")

    async def _safe_edit(_query, text: str, **_kwargs):
        events.append("edit")
        edits.append(text)

    async def _build_doctor(_chat_id):
        events.append("build-start")
        builder_started.set()
        await release_builder.wait()
        events.append("build-done")
        return "safe diagnostics"

    def _create_task(coro, **_kwargs):
        task = asyncio.create_task(coro)
        scheduled_tasks.append(task)
        return task

    query = SimpleNamespace(data=bot.CB_COCO_DOCTOR, message=message, answer=_answer)
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=chat,
        effective_user=SimpleNamespace(id=200),
        effective_message=message,
    )
    context = SimpleNamespace(
        user_data={},
        bot=object(),
        application=SimpleNamespace(create_task=_create_task),
    )

    monkeypatch.setattr(bot, "_is_chat_allowed", lambda _chat: True)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda uid: uid == 200)
    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(100, 1, -100123),
    )
    monkeypatch.setattr(bot, "_build_coco_doctor_text", _build_doctor)
    monkeypatch.setattr(bot, "safe_edit", _safe_edit)

    callback_task = asyncio.create_task(bot.callback_handler(update, context))
    try:
        await asyncio.wait_for(builder_started.wait(), timeout=1)
        assert events[:2] == ["answer:Doctor running…", "build-start"]
        assert callback_task.done()
        assert not release_builder.is_set()
    finally:
        release_builder.set()
        await callback_task
        if scheduled_tasks:
            await asyncio.gather(*scheduled_tasks)

    assert edits == ["safe diagnostics"]


def test_legacy_nonadmin_control_owner_cannot_control_other_users(monkeypatch):
    owner_user_id = 100
    target_user_id = 200
    chat_id = -100123

    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: False)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(owner_user_id, 1, chat_id),
    )

    assert bot._can_coco_control_target(
        caller_user_id=owner_user_id,
        target_user_id=owner_user_id,
        chat_id=chat_id,
    )
    assert not bot._can_coco_control_target(
        caller_user_id=owner_user_id,
        target_user_id=target_user_id,
        chat_id=chat_id,
    )


def test_current_admin_can_control_legacy_owner_binding(monkeypatch):
    owner_user_id = 100
    admin_user_id = 300
    chat_id = -100123

    monkeypatch.setattr(bot, "_is_admin_user", lambda uid: uid == admin_user_id)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(owner_user_id, 1, chat_id),
    )

    assert bot._can_coco_control_target(
        caller_user_id=admin_user_id,
        target_user_id=owner_user_id,
        chat_id=chat_id,
    )


def test_coco_dashboard_explains_unconfigured_general_control(monkeypatch, tmp_path):
    monkeypatch.setattr(bot.config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(bot.config, "sessions_path", tmp_path / "sessions")
    manager = SessionManager()
    monkeypatch.setattr(bot, "session_manager", manager)
    monkeypatch.setattr(bot.node_registry, "iter_nodes", lambda: iter(()))
    monkeypatch.setattr(bot, "get_recent_failures", lambda **_kwargs: [])

    text, _keyboard = bot._build_coco_dashboard(chat_id=-100123)

    assert "General control is not configured" in text
    assert "allowlist admin" in text


@pytest.mark.asyncio
async def test_coco_doctor_explains_unconfigured_general_control(monkeypatch, tmp_path):
    monkeypatch.setattr(bot.config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(bot.config, "sessions_path", tmp_path / "sessions")
    manager = SessionManager()
    monkeypatch.setattr(bot, "session_manager", manager)

    text = await bot._build_coco_doctor_text(-100123)

    assert "per-group General reservation (not configured)" in text
    assert "Configure an allowlist admin" in text


def test_coco_dashboard_paginates_topic_text_and_buttons(monkeypatch):
    bindings = [
        (
            100,
            -100123,
            thread_id,
            SimpleNamespace(
                display_name=f"topic-{thread_id}",
                window_id=f"@{thread_id}",
                machine_id="local",
            ),
        )
        for thread_id in range(2, 25)
    ]
    monkeypatch.setattr(bot.session_manager, "iter_topic_bindings", lambda: iter(bindings))
    monkeypatch.setattr(bot.session_manager, "get_coco_control_topic", lambda _chat: None)
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_active_turn_id",
        lambda _wid: "",
    )
    monkeypatch.setattr(bot, "queued_topic_input_count", lambda *_args: 0)
    monkeypatch.setattr(bot.node_registry, "iter_nodes", lambda: [])
    monkeypatch.setattr(bot, "get_recent_failures", lambda **_kwargs: [])
    monkeypatch.setattr(
        bot.session_manager,
        "iter_pending_coco_control_notices",
        lambda: iter(()),
    )

    text, keyboard = bot._build_coco_dashboard(chat_id=-100123, page=1)

    assert len(text) < 4096
    assert "page `2/4`" in text
    topic_rows = [
        row
        for row in keyboard.inline_keyboard
        if row and row[0].text == "Inspect"
    ]
    assert len(topic_rows) == bot._COCO_DASHBOARD_PAGE_SIZE
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    assert "Previous" in labels
    assert "Next" in labels
    assert all(
        len(button.callback_data or "") <= 64
        for row in keyboard.inline_keyboard
        for button in row
    )


def test_coco_dashboard_escapes_dynamic_markdown_fields(monkeypatch):
    binding = SimpleNamespace(
        display_name="api_v2 [docs](https://example.invalid) `prod`",
        window_id="@2",
        machine_id="node-1",
    )
    node = SimpleNamespace(
        machine_id="node-1",
        display_name="build_[west]`1`",
        status="online",
    )
    monkeypatch.setattr(
        bot.session_manager,
        "iter_topic_bindings",
        lambda: iter([(100, -100123, 2, binding)]),
    )
    monkeypatch.setattr(bot.session_manager, "get_coco_control_topic", lambda _chat: None)
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_active_turn_id",
        lambda _wid: "",
    )
    monkeypatch.setattr(bot, "queued_topic_input_count", lambda *_args: 0)
    monkeypatch.setattr(bot.node_registry, "iter_nodes", lambda: [node])
    monkeypatch.setattr(bot, "get_recent_failures", lambda **_kwargs: [])
    monkeypatch.setattr(
        bot.session_manager,
        "iter_pending_coco_control_notices",
        lambda: iter(()),
    )

    text, _keyboard = bot._build_coco_dashboard(chat_id=-100123)

    assert r"api\_v2 \[docs\]\(https://example.invalid\) \`prod\`" in text
    assert r"build\_\[west\]\`1\`" in text


@pytest.mark.asyncio
async def test_coco_dashboard_interrupt_fences_output_and_clears_queue(monkeypatch):
    chat = SimpleNamespace(type="supergroup", id=-100123, is_forum=True)
    message = SimpleNamespace(message_thread_id=1, chat=chat, chat_id=chat.id)
    answers: list[tuple[str, bool]] = []
    edits: list[str] = []
    events: list[str] = []
    routing_users: list[int] = []

    async def _answer(text: str, *, show_alert: bool = False):
        events.append("ack")
        answers.append((text, show_alert))

    async def _safe_edit(_query, text: str, **_kwargs):
        events.append("edit")
        edits.append(text)

    query = SimpleNamespace(
        data=f"{bot.CB_COCO_INTERRUPT}200:88",
        message=message,
        answer=_answer,
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=chat,
        effective_user=SimpleNamespace(id=999),
        effective_message=message,
    )
    binding = SimpleNamespace(
        display_name="target",
        codex_thread_id="codex-88",
        window_id="@88",
        machine_id="local-node",
        machine_display_name="Local",
        cwd="/tmp/target",
    )
    token = bot._register_coco_dashboard_snapshot(
        chat_id=chat.id,
        owner_user_id=200,
        thread_id=88,
        binding=binding,
        active_turn_id="turn-88",
    )
    query.data = f"{bot.CB_COCO_INTERRUPT}200:88:{token}"
    monkeypatch.setattr(bot, "_is_chat_allowed", lambda _chat: True)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: True)
    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda uid, *_args, **_kwargs: routing_users.append(uid),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda uid, tid, **_kwargs: binding if (uid, tid) == (200, 88) else None,
    )
    monkeypatch.setattr(
        bot,
        "capture_topic_ownership",
        lambda *_args, **_kwargs: bot.TopicOwnership(
            window_id="@88",
            codex_thread_id="codex-88",
            machine_id="local-node",
            cwd="/tmp/target",
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_active_turn_id",
        lambda _wid: "turn-88",
    )
    monkeypatch.setattr(bot, "_local_machine_identity", lambda: ("local-node", "Local"))
    monkeypatch.setattr(
        bot,
        "clear_queued_topic_inputs",
        lambda *_args, **_kwargs: events.append("clear-queue"),
    )
    monkeypatch.setattr(bot, "queued_topic_input_count", lambda *_args: 2)

    async def _cancel(*_args, **_kwargs):
        events.append("cancel-delivery")
        return 2

    async def _interrupt(*, thread_id: str, turn_id: str):
        state_key = bot._codex_thread_state_key(thread_id, "local-node")
        # The pending record is the only pre-dispatch state; output fencing is
        # committed after the RPC succeeds.
        assert state_key not in bot._interrupted_codex_threads
        assert state_key not in bot._interrupted_codex_turns
        assert state_key in bot._discard_queued_on_interrupts
        events.append("interrupt")

    async def _clear_dock(*_args, **_kwargs):
        events.append("clear-dock")

    monkeypatch.setattr(bot, "cancel_topic_delivery", _cancel)
    monkeypatch.setattr(bot.codex_app_server_client, "turn_interrupt", _interrupt)
    monkeypatch.setattr(bot.codex_app_server_client, "clear_active_turn", lambda _tid: None)
    monkeypatch.setattr(bot.session_manager, "clear_window_codex_turn", lambda _wid: None)
    monkeypatch.setattr(bot, "clear_run_watch_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bot, "clear_queued_topic_dock", _clear_dock)
    monkeypatch.setattr(bot, "safe_edit", _safe_edit)

    state_key = bot._codex_thread_state_key("codex-88", "local-node")
    try:
        await bot.callback_handler(update, SimpleNamespace(user_data={}, bot=object()))
        assert events == ["ack", "interrupt", "cancel-delivery", "edit"]
        assert routing_users == [200]
        assert state_key in bot._interrupted_codex_threads
        assert answers == [("Interrupting target…", False)]
        assert edits == [
            "Interrupt requested for target; 2 queued guidance item(s) will be "
            "discarded when completion is confirmed."
        ]
    finally:
        bot._interrupted_codex_threads.discard(state_key)
        bot._interrupted_codex_turns.pop(state_key, None)
        bot._discard_queued_on_interrupts.pop(state_key, None)


@pytest.mark.asyncio
async def test_coco_dashboard_interrupt_failure_preserves_guidance(monkeypatch):
    chat = SimpleNamespace(type="supergroup", id=-100123, is_forum=True)
    message = SimpleNamespace(message_thread_id=1, chat=chat, chat_id=chat.id)
    answers: list[str] = []
    edits: list[str] = []
    events: list[str] = []
    cleared: list[bool] = []
    dock_syncs: list[bool] = []

    async def _answer(text: str, **_kwargs):
        events.append("ack")
        answers.append(text)

    async def _safe_edit(_query, text: str, **_kwargs):
        events.append("edit")
        edits.append(text)

    query = SimpleNamespace(
        data=f"{bot.CB_COCO_INTERRUPT}200:88",
        message=message,
        answer=_answer,
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=chat,
        effective_user=SimpleNamespace(id=999),
        effective_message=message,
    )
    binding = SimpleNamespace(
        display_name="target",
        codex_thread_id="codex-88",
        window_id="@88",
        machine_id="local-node",
        machine_display_name="Local",
        cwd="/tmp/target",
    )
    token = bot._register_coco_dashboard_snapshot(
        chat_id=chat.id,
        owner_user_id=200,
        thread_id=88,
        binding=binding,
        active_turn_id="stale-turn",
    )
    query.data = f"{bot.CB_COCO_INTERRUPT}200:88:{token}"
    monkeypatch.setattr(bot, "_is_chat_allowed", lambda _chat: True)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: True)
    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(bot.session_manager, "set_group_chat_id", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda uid, tid, **_kwargs: binding if (uid, tid) == (200, 88) else None,
    )
    monkeypatch.setattr(
        bot,
        "capture_topic_ownership",
        lambda *_args, **_kwargs: bot.TopicOwnership(
            window_id="@88",
            codex_thread_id="codex-88",
            machine_id="local-node",
            cwd="/tmp/target",
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_active_turn_id",
        lambda _wid: "stale-turn",
    )
    monkeypatch.setattr(bot, "_local_machine_identity", lambda: ("local-node", "Local"))
    monkeypatch.setattr(
        bot,
        "clear_queued_topic_inputs",
        lambda *_args, **_kwargs: cleared.append(True),
    )
    monkeypatch.setattr(bot, "queued_topic_input_count", lambda *_args: 3)

    async def _cancel(*_args, **_kwargs):
        return 1

    async def _interrupt(**_kwargs):
        assert events == ["ack"]
        raise RuntimeError("stale turn")

    async def _sync(*_args, **_kwargs):
        dock_syncs.append(True)

    monkeypatch.setattr(bot, "cancel_topic_delivery", _cancel)
    monkeypatch.setattr(bot.codex_app_server_client, "turn_interrupt", _interrupt)
    monkeypatch.setattr(bot, "sync_queued_topic_dock", _sync)
    monkeypatch.setattr(bot, "safe_edit", _safe_edit)

    state_key = bot._codex_thread_state_key("codex-88", "local-node")
    await bot.callback_handler(update, SimpleNamespace(user_data={}, bot=object()))

    assert cleared == []
    assert dock_syncs == [True]
    assert answers == ["Interrupting target…"]
    assert edits == [
        "Interrupt failed: stale turn; 3 queued guidance item(s) were preserved."
    ]
    assert events == ["ack", "edit"]
    assert state_key not in bot._interrupted_codex_threads


@pytest.mark.asyncio
async def test_coco_dashboard_interrupt_failure_preserves_pending_assistant_delivery(
    monkeypatch,
):
    """A failed interrupt must not discard already queued assistant/status output."""
    user_id = 90200
    thread_id = 88
    chat_id = -100123
    queue: asyncio.Queue[mq.MessageTask] = asyncio.Queue()
    mq._message_queues[user_id] = queue
    mq._queue_locks[user_id] = asyncio.Lock()
    ownership = mq.TopicOwnership(
        window_id="@88",
        codex_thread_id="codex-88",
        machine_id="local-node",
        cwd="/tmp/target",
    )
    mq._put_queued_task(
        user_id,
        queue,
        mq.MessageTask(
            task_type="content",
            parts=["assistant output waiting to be delivered"],
            thread_id=thread_id,
            chat_id=chat_id,
            topic_ownership=ownership,
        ),
    )
    mq._put_queued_task(
        user_id,
        queue,
        mq.MessageTask(
            task_type="status_update",
            text="assistant is still working",
            thread_id=thread_id,
            chat_id=chat_id,
            topic_ownership=ownership,
        ),
    )

    chat = SimpleNamespace(type="supergroup", id=chat_id, is_forum=True)
    message = SimpleNamespace(message_thread_id=1, chat=chat, chat_id=chat.id)
    query = SimpleNamespace(
        data=f"{bot.CB_COCO_INTERRUPT}{user_id}:{thread_id}",
        message=message,
        answer=lambda *_args, **_kwargs: None,
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=chat,
        effective_user=SimpleNamespace(id=999),
        effective_message=message,
    )
    binding = SimpleNamespace(
        display_name="target",
        codex_thread_id="codex-88",
        window_id="@88",
        machine_id="local-node",
        machine_display_name="Local",
        cwd="/tmp/target",
    )
    token = bot._register_coco_dashboard_snapshot(
        chat_id=chat_id,
        owner_user_id=user_id,
        thread_id=thread_id,
        binding=binding,
        active_turn_id="stale-turn",
    )
    query.data = f"{bot.CB_COCO_INTERRUPT}{user_id}:{thread_id}:{token}"

    async def _answer(*_args, **_kwargs):
        return None

    async def _safe_edit(*_args, **_kwargs):
        return None

    async def _interrupt(**_kwargs):
        raise RuntimeError("stale turn")

    monkeypatch.setattr(bot, "_is_chat_allowed", lambda _chat: True)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(
        bot,
        "_ensure_default_coco_general_control",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda uid, tid, **_kwargs: binding
        if (uid, tid) == (user_id, thread_id)
        else None,
    )
    monkeypatch.setattr(
        bot,
        "capture_topic_ownership",
        lambda *_args, **_kwargs: bot.TopicOwnership(
            window_id="@88",
            codex_thread_id="codex-88",
            machine_id="local-node",
            cwd="/tmp/target",
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_active_turn_id",
        lambda _wid: "stale-turn",
    )
    monkeypatch.setattr(bot, "_local_machine_identity", lambda: ("local-node", "Local"))
    monkeypatch.setattr(bot, "queued_topic_input_count", lambda *_args: 0)
    monkeypatch.setattr(bot, "sync_queued_topic_dock", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bot.codex_app_server_client, "turn_interrupt", _interrupt)
    monkeypatch.setattr(bot, "safe_edit", _safe_edit)
    query.answer = _answer

    state_key = bot._codex_thread_state_key("codex-88", "local-node")
    try:
        await bot.callback_handler(update, SimpleNamespace(user_data={}, bot=object()))

        assert queue.qsize() == 2
        retained = [queue.get_nowait(), queue.get_nowait()]
        assert [task.task_type for task in retained] == ["content", "status_update"]
        for _task in retained:
            queue.task_done()
    finally:
        while not queue.empty():
            queue.get_nowait()
            queue.task_done()
        mq._message_queues.pop(user_id, None)
        mq._queue_locks.pop(user_id, None)
        mq._queued_delivery_topic_counts.pop(user_id, None)
        mq._topic_delivery_generations.pop((user_id, chat_id, thread_id), None)
        bot._interrupted_codex_threads.discard(state_key)
        bot._interrupted_codex_turns.pop(state_key, None)
        bot._discard_queued_on_interrupts.pop(state_key, None)


@pytest.mark.asyncio
async def test_coco_dashboard_interrupt_uncertain_keeps_fence_and_defers_queue(
    monkeypatch,
):
    """A written remote interrupt with a lost reply must not replay queued input."""
    user_id = 90202
    thread_id = 90
    chat_id = -100126
    codex_thread_id = "codex-90"
    machine_id = "remote-node"
    state_key = bot._codex_thread_state_key(codex_thread_id, machine_id)
    chat = SimpleNamespace(type="supergroup", id=chat_id, is_forum=True)
    message = SimpleNamespace(message_thread_id=1, chat=chat, chat_id=chat_id)
    edits: list[str] = []
    dispatches: list[dict[str, object]] = []
    queue_dock_syncs: list[bool] = []

    async def _answer(*_args, **_kwargs):
        return None

    async def _safe_edit(_query, text: str, **_kwargs):
        edits.append(text)

    query = SimpleNamespace(
        data=f"{bot.CB_COCO_INTERRUPT}{user_id}:{thread_id}",
        message=message,
        answer=_answer,
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=chat,
        effective_user=SimpleNamespace(id=999),
        effective_message=message,
    )
    binding = SimpleNamespace(
        display_name="remote target",
        codex_thread_id=codex_thread_id,
        window_id="@90",
        machine_id=machine_id,
        machine_display_name="Remote",
        cwd="/tmp/target",
    )
    token = bot._register_coco_dashboard_snapshot(
        chat_id=chat_id,
        owner_user_id=user_id,
        thread_id=thread_id,
        binding=binding,
        active_turn_id="turn-90",
    )
    query.data = f"{bot.CB_COCO_INTERRUPT}{user_id}:{thread_id}:{token}"

    monkeypatch.setattr(bot, "_is_chat_allowed", lambda _chat: True)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: True)
    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(bot.session_manager, "set_group_chat_id", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda uid, tid, **_kwargs: binding
        if (uid, tid) == (user_id, thread_id)
        else None,
    )
    monkeypatch.setattr(
        bot,
        "capture_topic_ownership",
        lambda *_args, **_kwargs: bot.TopicOwnership(
            window_id="@90",
            codex_thread_id=codex_thread_id,
            machine_id=machine_id,
            cwd="/tmp/target",
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_active_turn_id",
        lambda _wid: "turn-90",
    )
    monkeypatch.setattr(bot, "_local_machine_identity", lambda: ("controller-node", "Controller"))
    monkeypatch.setattr(bot, "queued_topic_input_count", lambda *_args, **_kwargs: 2)
    async def _sync_dock(*_args, **_kwargs):
        queue_dock_syncs.append(True)

    monkeypatch.setattr(bot, "sync_queued_topic_dock", _sync_dock)
    monkeypatch.setattr(bot, "safe_edit", _safe_edit)
    async def _interrupt(_machine_id, **_kwargs):
        raise RemoteCodexMutationUncertainError(
            "empty_response",
            request_dispatched=True,
        )

    monkeypatch.setattr(bot.agent_rpc_client, "turn_interrupt", _interrupt)
    monkeypatch.setattr(
        bot,
        "_dispatch_next_queued_input",
        lambda **kwargs: dispatches.append(kwargs),
    )

    try:
        await bot.callback_handler(update, SimpleNamespace(user_data={}, bot=object()))

        record = bot._discard_queued_on_interrupts[state_key]
        assert isinstance(record, bot._DashboardInterruptRecord)
        assert record.uncertain is True
        assert state_key in bot._interrupted_codex_threads
        assert bot._interrupted_codex_turns[state_key] == "turn-90"
        assert dispatches == []
        assert queue_dock_syncs == []
        assert edits and "uncertain" in edits[-1].lower()
    finally:
        bot._interrupted_codex_threads.discard(state_key)
        bot._interrupted_codex_turns.pop(state_key, None)
        bot._discard_queued_on_interrupts.pop(state_key, None)


@pytest.mark.asyncio
async def test_dashboard_interrupt_rejects_duplicate_in_flight_request(monkeypatch):
    """Concurrent clicks for one turn must not replace its interrupt record."""
    user_id = 90203
    thread_id = 91
    chat_id = -100127
    codex_thread_id = "codex-91"
    machine_id = "local-node"
    state_key = bot._codex_thread_state_key(codex_thread_id, machine_id)
    chat = SimpleNamespace(type="supergroup", id=chat_id, is_forum=True)
    message = SimpleNamespace(message_thread_id=1, chat=chat, chat_id=chat_id)
    binding = SimpleNamespace(
        display_name="target",
        codex_thread_id=codex_thread_id,
        window_id="@91",
        machine_id=machine_id,
        machine_display_name="Local",
        cwd="/tmp/target",
    )
    token = bot._register_coco_dashboard_snapshot(
        chat_id=chat_id,
        owner_user_id=user_id,
        thread_id=thread_id,
        binding=binding,
        active_turn_id="turn-91",
    )

    answers: list[list[str]] = [[], []]
    edits: list[str] = []
    rpc_calls: list[dict[str, str]] = []
    rpc_started = asyncio.Event()
    release_rpc = asyncio.Event()

    def _make_update(answer_index: int):
        async def _answer(text: str, **_kwargs):
            answers[answer_index].append(text)

        query = SimpleNamespace(
            data=f"{bot.CB_COCO_INTERRUPT}{user_id}:{thread_id}:{token}",
            message=message,
            answer=_answer,
        )
        return SimpleNamespace(
            callback_query=query,
            effective_chat=chat,
            effective_user=SimpleNamespace(id=999),
            effective_message=message,
        )

    update_one = _make_update(0)
    update_two = _make_update(1)

    async def _interrupt(**kwargs):
        rpc_calls.append(kwargs)
        rpc_started.set()
        await release_rpc.wait()

    async def _safe_edit(_query, text: str, **_kwargs):
        edits.append(text)

    monkeypatch.setattr(bot, "_is_chat_allowed", lambda _chat: True)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: True)
    monkeypatch.setattr(
        bot,
        "_ensure_default_coco_general_control",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda uid, tid, **_kwargs: binding
        if (uid, tid) == (user_id, thread_id)
        else None,
    )
    monkeypatch.setattr(
        bot,
        "capture_topic_ownership",
        lambda *_args, **_kwargs: bot.TopicOwnership(
            window_id="@91",
            codex_thread_id=codex_thread_id,
            machine_id=machine_id,
            cwd="/tmp/target",
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_active_turn_id",
        lambda _wid: "turn-91",
    )
    monkeypatch.setattr(bot, "_local_machine_identity", lambda: (machine_id, "Local"))
    monkeypatch.setattr(bot, "queued_topic_input_count", lambda *_args: 0)
    monkeypatch.setattr(
        bot,
        "get_queued_topic_input_generation",
        lambda *_args: 5,
    )
    monkeypatch.setattr(
        bot,
        "get_topic_delivery_generation",
        lambda *_args: 7,
    )
    async def _cancel(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(bot, "cancel_topic_delivery", _cancel)
    monkeypatch.setattr(bot.codex_app_server_client, "turn_interrupt", _interrupt)
    monkeypatch.setattr(bot, "safe_edit", _safe_edit)

    try:
        first = asyncio.create_task(
            bot.callback_handler(update_one, SimpleNamespace(user_data={}, bot=object()))
        )
        await asyncio.wait_for(rpc_started.wait(), timeout=1)

        second = asyncio.create_task(
            bot.callback_handler(update_two, SimpleNamespace(user_data={}, bot=object()))
        )
        await asyncio.wait_for(second, timeout=1)

        assert len(rpc_calls) == 1
        assert answers[0] == ["Interrupting target…"]
        assert answers[1] == ["Interrupt already in progress."]

        release_rpc.set()
        await asyncio.wait_for(first, timeout=1)
        assert len(rpc_calls) == 1
        record = bot._discard_queued_on_interrupts[state_key]
        assert isinstance(record, bot._DashboardInterruptRecord)
        assert record.committed is True
        assert record.queued_input_generation_cutoff == 5
        assert record.delivery_generation_cutoff == 7
        assert edits == [
            "Interrupt requested for target; awaiting completion confirmation."
        ]
    finally:
        release_rpc.set()
        bot._interrupted_codex_threads.discard(state_key)
        bot._interrupted_codex_turns.pop(state_key, None)
        bot._discard_queued_on_interrupts.pop(state_key, None)
        claims = getattr(bot, "_dashboard_interrupt_claims", None)
        if claims is not None:
            claims.discard(state_key)


def test_dashboard_interrupt_transport_timeout_is_uncertain():
    assert bot._dashboard_interrupt_outcome_is_uncertain(TimeoutError("timed out"))
    assert not bot._dashboard_interrupt_outcome_is_uncertain(
        RuntimeError("stale turn")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_kind", ["ownership", "active_turn"])
async def test_dashboard_interrupt_rejects_stale_rendered_ownership_snapshot(
    monkeypatch,
    stale_kind,
):
    """An old dashboard button cannot interrupt a rebound/advanced topic."""
    chat_id = -100125
    owner_user_id = 200
    thread_id = 88
    chat = SimpleNamespace(type="supergroup", id=chat_id, is_forum=True)
    message = SimpleNamespace(message_thread_id=1, chat=chat, chat_id=chat_id)
    answers: list[tuple[str, bool]] = []
    rpc_calls: list[dict[str, object]] = []
    queue_mutations: list[str] = []
    old_binding = SimpleNamespace(
        display_name="target",
        codex_thread_id="codex-old",
        window_id="@88",
        machine_id="local-node",
        machine_display_name="Local",
        cwd="/tmp/old",
    )
    rebound_binding = SimpleNamespace(
        display_name="target",
        codex_thread_id="codex-new",
        window_id="@88",
        machine_id="local-node",
        machine_display_name="Local",
        cwd="/tmp/new",
    )
    current_binding = old_binding if stale_kind == "active_turn" else rebound_binding
    token = bot._register_coco_dashboard_snapshot(
        chat_id=chat_id,
        owner_user_id=owner_user_id,
        thread_id=thread_id,
        binding=old_binding,
        active_turn_id="turn-old",
    )
    query = SimpleNamespace(
        data=f"{bot.CB_COCO_INTERRUPT}{owner_user_id}:{thread_id}:{token}",
        message=message,
        answer=None,
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=chat,
        effective_user=SimpleNamespace(id=999),
        effective_message=message,
    )

    async def _answer(text: str, **kwargs):
        answers.append((text, bool(kwargs.get("show_alert", False))))

    async def _interrupt(**kwargs):
        rpc_calls.append(kwargs)

    query.answer = _answer
    monkeypatch.setattr(bot, "_is_chat_allowed", lambda _chat: True)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: True)
    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(bot.session_manager, "set_group_chat_id", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda uid, tid, **_kwargs: current_binding
        if (uid, tid) == (owner_user_id, thread_id)
        else None,
    )
    monkeypatch.setattr(
        bot,
        "capture_topic_ownership",
        lambda *_args, **_kwargs: bot.TopicOwnership(
            window_id="@88",
            codex_thread_id=(
                "codex-old" if stale_kind == "active_turn" else "codex-new"
            ),
            machine_id="local-node",
            cwd="/tmp/old" if stale_kind == "active_turn" else "/tmp/new",
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_active_turn_id",
        lambda _wid: "turn-new" if stale_kind == "active_turn" else "turn-old",
    )
    monkeypatch.setattr(bot, "_local_machine_identity", lambda: ("local-node", "Local"))
    monkeypatch.setattr(bot.codex_app_server_client, "turn_interrupt", _interrupt)
    monkeypatch.setattr(bot, "cancel_topic_delivery", lambda *_args, **_kwargs: queue_mutations.append("cancel"))
    monkeypatch.setattr(
        bot,
        "discard_queued_topic_inputs_before_generation",
        lambda *_args, **_kwargs: queue_mutations.append("discard"),
    )

    await bot.callback_handler(update, SimpleNamespace(user_data={}, bot=object()))

    assert rpc_calls == []
    assert queue_mutations == []
    assert answers == [("This dashboard control is stale. Refresh /coco.", True)]


@pytest.mark.asyncio
async def test_dashboard_target_routes_group_mapping_to_selected_topic(monkeypatch):
    """Cross-owner dashboard controls must never reserve the owner's General slot."""
    chat_id = -100127
    owner_user_id = 200
    target_thread_id = 88
    chat = SimpleNamespace(type="supergroup", id=chat_id, is_forum=True)
    message = SimpleNamespace(message_thread_id=1, chat=chat, chat_id=chat_id)
    answers: list[str] = []
    binding = SimpleNamespace(
        display_name="other-owner-topic",
        codex_thread_id="codex-88",
        window_id="@88",
        machine_id="local-node",
        machine_display_name="Local",
        cwd="/tmp/target",
    )
    token = bot._register_coco_dashboard_snapshot(
        chat_id=chat_id,
        owner_user_id=owner_user_id,
        thread_id=target_thread_id,
        binding=binding,
        active_turn_id="",
    )
    query = SimpleNamespace(
        data=(
            f"{bot.CB_COCO_INSPECT}{owner_user_id}:"
            f"{target_thread_id}:{token}"
        ),
        message=message,
        answer=None,
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=chat,
        effective_user=SimpleNamespace(id=999),
        effective_message=message,
    )

    async def _answer(text: str, **_kwargs):
        answers.append(text)

    query.answer = _answer
    monkeypatch.setattr(bot, "_is_chat_allowed", lambda _chat: True)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: True)
    monkeypatch.setattr(
        bot,
        "_ensure_default_coco_general_control",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda uid, tid, **_kwargs: binding
        if (uid, tid) == (owner_user_id, target_thread_id)
        else None,
    )
    monkeypatch.setattr(
        bot,
        "capture_topic_ownership",
        lambda *_args, **_kwargs: bot.TopicOwnership(
            window_id="@88",
            codex_thread_id="codex-88",
            machine_id="local-node",
            cwd="/tmp/target",
        ),
    )
    # Use the real routing implementation so this assertion covers both the
    # scoped target lookup and the unscoped General fallback.
    monkeypatch.setattr(bot.session_manager, "group_chat_ids", {})
    monkeypatch.setattr(bot.session_manager, "_save_state", lambda: None)

    await bot.callback_handler(update, SimpleNamespace(user_data={}, bot=object()))

    assert bot.session_manager.resolve_chat_id(
        owner_user_id,
        target_thread_id,
        chat_id=chat_id,
    ) == chat_id
    assert bot.session_manager.resolve_chat_id(owner_user_id, 1) == owner_user_id
    assert answers and "other-owner-topic" in answers[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("target_thread_id", [88, int("9" * 40)])
async def test_dashboard_inspect_compacts_long_target_detail_without_losing_identifiers(
    monkeypatch,
    target_thread_id,
):
    chat_id = -100129
    owner_user_id = 200
    chat = SimpleNamespace(type="supergroup", id=chat_id, is_forum=True)
    message = SimpleNamespace(message_thread_id=1, chat=chat, chat_id=chat_id)
    binding = SimpleNamespace(
        display_name="[topic-" + "L" * 500 + "](https://example.invalid)",
        codex_thread_id="codex-88-stable-" + "C" * 500,
        window_id="@88-stable-" + "W" * 500,
        machine_id="machine-88-stable-" + "M" * 500,
        machine_display_name="machine-display-" + "D" * 500,
        cwd="/tmp/target/" + "P" * 500,
    )
    token = bot._register_coco_dashboard_snapshot(
        chat_id=chat_id,
        owner_user_id=owner_user_id,
        thread_id=target_thread_id,
        binding=binding,
        active_turn_id="",
    )
    query = SimpleNamespace(
        data=(
            f"{bot.CB_COCO_INSPECT}{owner_user_id}:"
            f"{target_thread_id}:{token}"
        ),
        message=message,
        answer=None,
    )
    assert len(query.data.encode("utf-8")) <= 64
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=chat,
        effective_user=SimpleNamespace(id=999),
        effective_message=message,
    )
    answers: list[str] = []

    async def _answer(text: str, **_kwargs):
        answers.append(text)

    query.answer = _answer
    monkeypatch.setattr(bot, "_is_chat_allowed", lambda _chat: True)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda uid: uid == 999)
    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(bot.session_manager, "set_group_chat_id", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda uid, tid, **_kwargs: binding
        if (uid, tid) == (owner_user_id, target_thread_id)
        else None,
    )
    monkeypatch.setattr(
        bot,
        "capture_topic_ownership",
        lambda *_args, **_kwargs: bot.TopicOwnership(
            window_id=binding.window_id,
            codex_thread_id=binding.codex_thread_id,
            machine_id=binding.machine_id,
            cwd=binding.cwd,
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_active_turn_id",
        lambda _wid: "",
    )

    await bot.callback_handler(update, SimpleNamespace(user_data={}, bot=object()))

    assert len(answers) == 1
    assert len(answers[0]) <= 200
    assert f"thread: {target_thread_id}" in answers[0]
    assert "window: @88-stable-" in answers[0]
    assert "codex: codex-88-stable-" in answers[0]
    assert "machine: machine-88-stable-" in answers[0]


@pytest.mark.asyncio
async def test_dashboard_interrupt_stale_after_ack_answers_once_and_clears_ui(
    monkeypatch,
):
    """A turn advancing during Telegram ack must not trigger a second answer or RPC."""
    chat_id = -100128
    owner_user_id = 200
    target_thread_id = 88
    chat = SimpleNamespace(type="supergroup", id=chat_id, is_forum=True)
    message = SimpleNamespace(message_thread_id=1, chat=chat, chat_id=chat_id)
    answers: list[str] = []
    edits: list[tuple[str, dict[str, object]]] = []
    rpc_calls: list[dict[str, object]] = []
    queue_mutations: list[str] = []
    binding = SimpleNamespace(
        display_name="target",
        codex_thread_id="codex-88",
        window_id="@88",
        machine_id="local-node",
        machine_display_name="Local",
        cwd="/tmp/target",
    )
    token = bot._register_coco_dashboard_snapshot(
        chat_id=chat_id,
        owner_user_id=owner_user_id,
        thread_id=target_thread_id,
        binding=binding,
        active_turn_id="turn-old",
    )
    query = SimpleNamespace(
        data=(
            f"{bot.CB_COCO_INTERRUPT}{owner_user_id}:"
            f"{target_thread_id}:{token}"
        ),
        message=message,
        answer=None,
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=chat,
        effective_user=SimpleNamespace(id=999),
        effective_message=message,
    )
    active_turn = "turn-old"

    async def _answer(text: str, **_kwargs):
        nonlocal active_turn
        answers.append(text)
        # Telegram acknowledgement yields to the event loop; model the turn
        # completing/advancing before callback code resumes.
        active_turn = "turn-new"

    async def _safe_edit(_query, text: str, **kwargs):
        edits.append((text, kwargs))

    def _active_turn(_window_id):
        return active_turn

    async def _interrupt(**kwargs):
        rpc_calls.append(kwargs)

    query.answer = _answer
    monkeypatch.setattr(bot, "_is_chat_allowed", lambda _chat: True)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: True)
    monkeypatch.setattr(
        bot,
        "_ensure_default_coco_general_control",
        lambda **_kwargs: queue_mutations.append("materialize"),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: queue_mutations.append("route"),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda uid, tid, **_kwargs: binding
        if (uid, tid) == (owner_user_id, target_thread_id)
        else None,
    )
    monkeypatch.setattr(
        bot,
        "capture_topic_ownership",
        lambda *_args, **_kwargs: bot.TopicOwnership(
            window_id="@88",
            codex_thread_id="codex-88",
            machine_id="local-node",
            cwd="/tmp/target",
        ),
    )
    monkeypatch.setattr(bot.session_manager, "get_window_codex_active_turn_id", _active_turn)
    monkeypatch.setattr(bot.codex_app_server_client, "turn_interrupt", _interrupt)
    monkeypatch.setattr(
        bot,
        "cancel_topic_delivery",
        lambda *_args, **_kwargs: queue_mutations.append("cancel"),
    )
    monkeypatch.setattr(
        bot,
        "discard_queued_topic_inputs_before_generation",
        lambda *_args, **_kwargs: queue_mutations.append("discard"),
    )
    monkeypatch.setattr(bot, "safe_edit", _safe_edit)

    await bot.callback_handler(update, SimpleNamespace(user_data={}, bot=object()))

    assert len(answers) == 1
    assert answers == ["Interrupting target…"]
    assert rpc_calls == []
    assert queue_mutations == []
    assert edits == [
        (
            "This dashboard control is stale. Refresh /coco.",
            {"reply_markup": None},
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rpc_outcome",
    ["success", "definite_failure", "uncertain"],
)
async def test_dashboard_interrupt_pending_terminal_commits_after_success_or_confirmation(
    monkeypatch,
    rpc_outcome,
):
    """A terminal observed while interrupt RPC is pending must not discard yet."""
    user_id = 90201
    thread_id = 89
    chat_id = -100124
    codex_thread_id = "codex-89"
    machine_id = "local-node"
    state_key = bot._codex_thread_state_key(codex_thread_id, machine_id)
    chat = SimpleNamespace(type="supergroup", id=chat_id, is_forum=True)
    message = SimpleNamespace(message_thread_id=1, chat=chat, chat_id=chat_id)
    query = SimpleNamespace(
        data=f"{bot.CB_COCO_INTERRUPT}{user_id}:{thread_id}",
        message=message,
        answer=None,
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=chat,
        effective_user=SimpleNamespace(id=999),
        effective_message=message,
    )
    binding = SimpleNamespace(
        display_name="target",
        codex_thread_id=codex_thread_id,
        window_id="@89",
        machine_id=machine_id,
        machine_display_name="Local",
        cwd="/tmp/target",
    )
    token = bot._register_coco_dashboard_snapshot(
        chat_id=chat_id,
        owner_user_id=user_id,
        thread_id=thread_id,
        binding=binding,
        active_turn_id="turn-89",
    )
    query.data = f"{bot.CB_COCO_INTERRUPT}{user_id}:{thread_id}:{token}"
    rpc_started = asyncio.Event()
    release_rpc = asyncio.Event()
    cleanup_calls: list[tuple[int, int, int | None, int]] = []
    cancel_calls: list[int] = []
    dispatch_calls: list[dict[str, object]] = []
    delivered_text: list[str] = []

    async def _answer(*_args, **_kwargs):
        return None

    async def _safe_edit(*_args, **_kwargs):
        return None

    async def _interrupt(**_kwargs):
        rpc_started.set()
        await release_rpc.wait()
        if rpc_outcome == "definite_failure":
            raise RuntimeError("interrupt transport failed")
        if rpc_outcome == "uncertain":
            raise RemoteCodexMutationUncertainError(
                "request_timeout",
                request_dispatched=True,
            )

    async def _enqueue_clear(*_args, **_kwargs):
        return None

    async def _cancel(*_args, **kwargs):
        cancel_calls.append(int(kwargs["generation_cutoff"]))
        return 1

    async def _dispatch(**kwargs):
        dispatch_calls.append(kwargs)

    async def _route(message, _bot, **_kwargs):
        delivered_text.append(message.text)

    query.answer = _answer
    monkeypatch.setattr(bot, "_is_chat_allowed", lambda _chat: True)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: True)
    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(bot.session_manager, "set_group_chat_id", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda uid, tid, **_kwargs: binding
        if (uid, tid) == (user_id, thread_id)
        else None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_active_turn_id",
        lambda _wid: "turn-89",
    )
    monkeypatch.setattr(bot, "_local_machine_identity", lambda: (machine_id, "Local"))
    monkeypatch.setattr(bot, "queued_topic_input_count", lambda *_args, **_kwargs: 2)
    monkeypatch.setattr(bot, "get_queued_topic_input_generation", lambda *_args, **_kwargs: 5)
    monkeypatch.setattr(bot, "get_topic_delivery_generation", lambda *_args, **_kwargs: 7)
    monkeypatch.setattr(
        bot,
        "discard_queued_topic_inputs_before_generation",
        lambda uid, tid, cid, *, generation_cutoff: cleanup_calls.append(
            (uid, tid, cid, generation_cutoff)
        )
        or 2,
    )
    monkeypatch.setattr(bot, "cancel_topic_delivery", _cancel)
    monkeypatch.setattr(bot.codex_app_server_client, "turn_interrupt", _interrupt)
    monkeypatch.setattr(bot, "safe_edit", _safe_edit)
    monkeypatch.setattr(bot, "enqueue_progress_clear", _enqueue_clear)
    monkeypatch.setattr(bot, "note_run_completed", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "_dispatch_next_queued_input", _dispatch)
    monkeypatch.setattr(bot, "_route_app_server_message", _route)
    monkeypatch.setattr(bot, "sync_queued_topic_dock", lambda *_args, **_kwargs: asyncio.sleep(0))
    monkeypatch.setattr(bot, "clear_queued_topic_dock", lambda *_args, **_kwargs: asyncio.sleep(0))
    monkeypatch.setattr(
        bot,
        "_find_codex_thread_bindings_for_source",
        lambda *_args, **_kwargs: [(user_id, chat_id, "@89", thread_id)],
    )
    monkeypatch.setattr(
        bot,
        "capture_topic_ownership",
        lambda *_args, **_kwargs: bot.TopicOwnership(
            window_id="@89",
            codex_thread_id=codex_thread_id,
            machine_id=machine_id,
            cwd="/tmp/target",
        ),
    )
    monkeypatch.setattr(bot.session_manager, "set_codex_turn_for_thread", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bot, "clear_queued_topic_dock", lambda *_args, **_kwargs: None)

    task = asyncio.create_task(
        bot.callback_handler(update, SimpleNamespace(user_data={}, bot=object()))
    )
    await asyncio.wait_for(rpc_started.wait(), timeout=1)

    # A natural final item can arrive while the interrupt RPC is still
    # pending.  It must not be fenced before the RPC outcome is known.
    await bot._handle_codex_app_server_notification(
        "item/completed",
        {
            "threadId": codex_thread_id,
            "item": {"type": "agentMessage", "text": "final while pending"},
        },
        bot=object(),
        source_machine_id=machine_id,
    )
    assert delivered_text == ["final while pending"]

    await bot._handle_codex_app_server_notification(
        "turn/completed",
        {
            "threadId": codex_thread_id,
            "turn": {"id": "turn-89", "status": "interrupted"},
        },
        bot=object(),
        source_machine_id=machine_id,
    )
    assert cleanup_calls == []
    assert cancel_calls == []

    release_rpc.set()
    await asyncio.wait_for(task, timeout=1)

    if rpc_outcome == "definite_failure":
        assert cleanup_calls == []
        assert cancel_calls == []
    else:
        assert cleanup_calls == [(user_id, thread_id, chat_id, 5)]
        assert cancel_calls == [7]
    assert len(dispatch_calls) == 1
    assert dispatch_calls[0]["thread_id"] == thread_id
    bot._interrupted_codex_threads.discard(state_key)
    bot._interrupted_codex_turns.pop(state_key, None)
    bot._discard_queued_on_interrupts.pop(state_key, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prefix",
    [bot.CB_COCO_INSPECT, bot.CB_COCO_STEER, bot.CB_COCO_INTERRUPT],
)
async def test_single_session_user_cannot_dashboard_control_or_inspect_another_owner(
    monkeypatch,
    prefix,
):
    chat = SimpleNamespace(type="supergroup", id=-100123, is_forum=True)
    message = SimpleNamespace(message_thread_id=1, chat=chat, chat_id=chat.id)
    answers: list[tuple[str, bool]] = []
    query = SimpleNamespace(
        data=f"{prefix}200:88",
        message=message,
        answer=None,
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=chat,
        effective_user=SimpleNamespace(id=999),
        effective_message=message,
    )
    binding = SimpleNamespace(
        display_name="other-owner-topic",
        codex_thread_id="codex-88",
        window_id="@88",
        machine_id="local-node",
        machine_display_name="Local",
        cwd="/tmp/target",
    )
    token = bot._register_coco_dashboard_snapshot(
        chat_id=chat.id,
        owner_user_id=200,
        thread_id=88,
        binding=binding,
        active_turn_id="turn-88",
    )
    query.data = f"{prefix}200:88:{token}"
    calls: list[str] = []

    async def _answer(text: str, **kwargs):
        answers.append((text, bool(kwargs.get("show_alert", False))))

    query.answer = _answer
    monkeypatch.setattr(bot, "_is_chat_allowed", lambda _chat: True)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: False)
    monkeypatch.setattr(bot, "_get_user_scope", lambda _uid: bot.SCOPE_SINGLE_SESSION)
    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(bot.session_manager, "set_group_chat_id", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(100, 1, -100123),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda uid, tid, **_kwargs: binding if (uid, tid) == (200, 88) else None,
    )
    monkeypatch.setattr(
        bot,
        "capture_topic_ownership",
        lambda *_args, **_kwargs: bot.TopicOwnership(
            window_id="@88",
            codex_thread_id="codex-88",
            machine_id="local-node",
            cwd="/tmp/target",
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_active_turn_id",
        lambda _wid: "turn-88",
    )
    monkeypatch.setattr(
        bot,
        "cancel_topic_delivery",
        lambda *_args, **_kwargs: calls.append("cancel"),
    )
    monkeypatch.setattr(
        bot.codex_app_server_client,
        "turn_interrupt",
        lambda **_kwargs: calls.append("interrupt"),
    )

    await bot.callback_handler(update, SimpleNamespace(user_data={}, bot=object()))

    assert calls == []
    assert answers == [
        (
            "Only the CoCo control owner or an admin can control another user's topic.",
            True,
        )
    ]


@pytest.mark.asyncio
async def test_non_owner_cannot_forward_general_command_to_canonical_owner(monkeypatch):
    """Unsupported slash commands must not inherit General's owner alias."""
    caller_user_id = 999
    control_owner_id = 100
    chat_id = -100123
    chat = SimpleNamespace(type="supergroup", id=chat_id, is_forum=True)
    message = SimpleNamespace(
        text="/clear",
        message_thread_id=1,
        chat=chat,
    )
    update = SimpleNamespace(
        effective_chat=chat,
        effective_user=SimpleNamespace(id=caller_user_id),
        effective_message=message,
        message=message,
    )
    replies: list[str] = []
    group_mappings: list[tuple[tuple, dict]] = []

    async def _send_action(_action):
        raise AssertionError("unauthorized General command must not dispatch")

    message.chat.send_action = _send_action

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "_is_chat_allowed", lambda _chat: True)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: False)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(control_owner_id, 1, chat_id),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *args, **kwargs: group_mappings.append((args, kwargs)),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_window_for_thread",
        lambda *_args, **_kwargs: pytest.fail(
            "unauthorized General command must be rejected before canonical lookup"
        ),
    )
    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.forward_command_handler(update, SimpleNamespace())

    assert replies == [bot._COCO_CONTROL_PERMISSION_DENIED_TEXT]
    assert group_mappings == []


@pytest.mark.asyncio
@pytest.mark.parametrize("command_text", ["/clear", "/compact fallback command"])
async def test_forward_command_general_rejects_unconfigured_control_before_state_lookup(
    monkeypatch,
    command_text,
):
    update = _make_update(command_text, user_id=999, is_forum=True)
    replies: list[str] = []
    events: list[str] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_chat_allowed", lambda _chat: True)
    monkeypatch.setattr(
        bot,
        "_ensure_default_coco_general_control",
        lambda **_kwargs: events.append("materialize"),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: events.append("set-group"),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_window_for_thread",
        lambda *_args, **_kwargs: pytest.fail(
            "unconfigured General command must not resolve a caller session"
        ),
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.forward_command_handler(update, SimpleNamespace())

    assert events == ["materialize"]
    assert replies == [bot._COCO_CONTROL_UNCONFIGURED_TEXT]


@pytest.mark.asyncio
@pytest.mark.parametrize("command_text", ["/clear", "/compact fallback command"])
async def test_forward_command_general_rejects_pending_migration_before_state_lookup(
    monkeypatch,
    command_text,
):
    update = _make_update(command_text, user_id=999, is_forum=True)
    replies: list[str] = []
    events: list[str] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_chat_allowed", lambda _chat: True)
    monkeypatch.setattr(
        bot,
        "_ensure_default_coco_general_control",
        lambda **_kwargs: events.append("materialize"),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(100, 77, -100123),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: events.append("set-group"),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_window_for_thread",
        lambda *_args, **_kwargs: pytest.fail(
            "pending General migration must not resolve a caller session"
        ),
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.forward_command_handler(update, SimpleNamespace())

    assert events == ["materialize"]
    assert replies == [
        "⏳ CoCo's General control migration is still pending. The legacy "
        "control history was preserved; retry after the remote machine is online."
    ]


def test_general_workspace_trusts_exact_control_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(bot.config, "config_dir", tmp_path)
    monkeypatch.setattr(bot.config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(bot.config, "sessions_path", tmp_path / "sessions")
    manager = SessionManager()
    monkeypatch.setattr(bot, "session_manager", manager)
    trusted: list[object] = []
    monkeypatch.setattr(
        bot,
        "_ensure_codex_project_trust",
        lambda path: (trusted.append(path) is None, ""),
    )

    binding = bot._ensure_default_coco_general_control(
        user_id=100,
        thread_id=1,
        chat_id=-100123,
    )

    expected = tmp_path / "_coco" / "chat-100123" / "control"
    assert binding is not None and binding.cwd == str(expected)
    assert trusted == [expected]


@pytest.mark.asyncio
async def test_coco_control_keyboard_never_offers_reassignment(monkeypatch, tmp_path):
    update = _make_update("/coco", thread_id=1, is_forum=True)
    replies: list[tuple[str, object]] = []
    binding = SimpleNamespace(
        cwd="",
        display_name="",
        window_id="",
    )

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(bot.config, "browse_root", tmp_path)
    monkeypatch.setattr(bot.config, "config_dir", tmp_path)
    monkeypatch.setattr(bot.session_manager, "set_group_chat_id", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bot.session_manager, "ensure_topic_binding", lambda *_args, **_kwargs: binding)
    monkeypatch.setattr(bot.session_manager, "iter_topic_bindings", lambda: iter(()))
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda *_args: bot.CocoControlTopic(1147817421, 1, -100123),
    )
    monkeypatch.setattr(bot.session_manager, "is_coco_control_topic", lambda *_args, **_kwargs: True)

    async def _safe_reply(_message, text: str, **kwargs):
        replies.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.coco_command(update, SimpleNamespace(user_data={}))

    assert len(replies) == 1
    text, markup = replies[0]
    assert "CoCo dashboard" in text
    assert markup is not None
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert labels == ["Doctor", "Refresh"]
    forced_labels = [
        button.text
        for row in bot._build_coco_control_keyboard(is_current=False).inline_keyboard
        for button in row
    ]
    assert forced_labels == ["Refresh"]


@pytest.mark.asyncio
async def test_stale_named_set_callback_cannot_reassign_control(monkeypatch, tmp_path):
    chat = SimpleNamespace(type="supergroup", id=-100123, is_forum=True)
    message = SimpleNamespace(message_thread_id=77, chat=chat)
    answers: list[tuple[str, bool]] = []
    edits: list[str] = []

    async def _answer(text: str, *, show_alert: bool = False):
        answers.append((text, show_alert))

    query = SimpleNamespace(
        data=bot.CB_COCO_SET,
        message=message,
        answer=_answer,
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=chat,
        effective_user=SimpleNamespace(id=1147817421),
        effective_message=message,
    )

    monkeypatch.setattr(bot.config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(bot.config, "sessions_path", tmp_path / "sessions")
    manager = SessionManager()
    manager.set_coco_control_topic(1147817421, 1, chat_id=-100123)
    monkeypatch.setattr(bot, "session_manager", manager)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)

    async def _safe_edit(_query, text: str, **_kwargs):
        edits.append(text)

    monkeypatch.setattr(bot, "safe_edit", _safe_edit)

    await bot.callback_handler(update, SimpleNamespace(user_data={}, bot=None))

    assert manager.is_coco_control_topic(1147817421, 1, chat_id=-100123)
    assert manager.resolve_topic_binding(1147817421, 77, chat_id=-100123) is None
    assert edits == [
        "ℹ️ CoCo control is permanently assigned to General. Use `/coco` there."
    ]
    assert answers == [("CoCo is fixed to General.", True)]


@pytest.mark.asyncio
async def test_coco_command_topics_lists_other_topics(monkeypatch):
    update = _make_update("/coco topics")
    replies: list[str] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(bot.session_manager, "set_group_chat_id", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bot.session_manager, "is_coco_control_topic", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(100, 1, -100123),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "iter_topic_bindings",
        lambda: iter(
            [
                (1147817421, -100123, 77, SimpleNamespace(display_name="coco-control", cwd="/env/_coco/ctl")),
                (1147817421, -100123, 88, SimpleNamespace(display_name="fmwblog", cwd="/env/fmwblog")),
                (1147817421, -100123, 99, SimpleNamespace(display_name="bottleshot", cwd="/env/bottleshot")),
            ]
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_window_for_thread",
        lambda _uid, tid, **_kwargs: "@88" if tid == 88 else None,
    )
    monkeypatch.setattr(
        bot,
        "capture_topic_ownership",
        lambda *_args, **_kwargs: bot.TopicOwnership(
            window_id="@88",
            codex_thread_id="codex-88",
            machine_id="local-node",
            cwd="/tmp/target",
        ),
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.coco_command(update, SimpleNamespace(user_data={}))

    assert len(replies) == 1
    assert "CoCo control topic inventory" in replies[0]
    assert "thread `88`" in replies[0]
    assert "`fmwblog`" in replies[0]
    assert "thread `99`" in replies[0]


@pytest.mark.asyncio
async def test_coco_command_topics_hides_other_owners_from_single_session_member(
    monkeypatch,
):
    update = _make_update("/coco topics", user_id=999)
    replies: list[str] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: False)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "is_coco_control_topic",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        bot,
        "_ensure_default_coco_general_control",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(100, 1, -100123),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "iter_topic_bindings",
        lambda: iter(
            [
                (
                    100,
                    -100123,
                    77,
                    SimpleNamespace(display_name="owner-topic", cwd="/secret/owner"),
                ),
                (
                    999,
                    -100123,
                    88,
                    SimpleNamespace(display_name="my-topic", cwd="/workspace/mine"),
                ),
                (
                    200,
                    -100123,
                    99,
                    SimpleNamespace(display_name="other-topic", cwd="/secret/other"),
                ),
            ]
        ),
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.coco_command(update, SimpleNamespace(user_data={}))

    assert len(replies) == 1
    assert "thread `88`" in replies[0]
    assert "/workspace/mine" in replies[0]
    assert "thread `77`" not in replies[0]
    assert "/secret/owner" not in replies[0]
    assert "thread `99`" not in replies[0]
    assert "/secret/other" not in replies[0]


@pytest.mark.asyncio
async def test_coco_command_steer_sends_to_target_topic(monkeypatch):
    update = _make_update("/coco steer 88 Focus on the PDF bug")
    replies: list[str] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(
        bot,
        "_ensure_default_coco_general_control",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(bot.session_manager, "set_group_chat_id", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bot.session_manager, "is_coco_control_topic", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        bot.session_manager,
        "iter_topic_bindings",
        lambda: iter(
            [
                (1147817421, -100123, 77, SimpleNamespace(display_name="coco-control", cwd="/env/_coco/ctl")),
                (1147817421, -100123, 88, SimpleNamespace(display_name="fmwblog", cwd="/env/fmwblog")),
            ]
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_window_for_thread",
        lambda _uid, tid, **_kwargs: "@88" if tid == 88 else "@77",
    )
    monkeypatch.setattr(
        bot,
        "capture_topic_ownership",
        lambda *_args, **_kwargs: SimpleNamespace(
            window_id="@88",
            codex_thread_id="thread-88",
            machine_id="test-machine",
            cwd="/env/fmwblog",
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        lambda **kwargs: _async_result((True, f"sent:{kwargs['thread_id']}:{kwargs['text']}")),
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.coco_command(update, SimpleNamespace(user_data={}))

    assert replies == ["✅ Steered topic `88` (`fmwblog`)."]


@pytest.mark.asyncio
@pytest.mark.parametrize("subcommand", ["steer", "queue"])
async def test_single_session_user_cannot_control_another_owner_from_general(
    monkeypatch,
    subcommand,
):
    update = _make_update(
        f"/coco {subcommand} 88 Focus on the PDF bug",
        user_id=999,
    )
    replies: list[str] = []
    dispatched: list[object] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: False)
    monkeypatch.setattr(bot, "_get_user_scope", lambda _uid: bot.SCOPE_SINGLE_SESSION)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(bot.session_manager, "set_group_chat_id", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(100, 1, -100123),
    )
    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(bot.session_manager, "is_coco_control_topic", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        bot.session_manager,
        "iter_topic_bindings",
        lambda: iter(
            [
                (100, -100123, 1, SimpleNamespace(display_name="coco-control")),
                (200, -100123, 88, SimpleNamespace(display_name="other-owner")),
            ]
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_window_for_thread",
        lambda _uid, tid, **_kwargs: "@88" if tid == 88 else None,
    )
    monkeypatch.setattr(
        bot,
        "capture_topic_ownership",
        lambda *_args, **_kwargs: bot.TopicOwnership(
            window_id="@88",
            codex_thread_id="codex-88",
            machine_id="local-node",
            cwd="/tmp/target",
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        lambda **kwargs: dispatched.append(kwargs),
    )
    monkeypatch.setattr(
        bot,
        "enqueue_queued_topic_input",
        lambda *args, **kwargs: dispatched.append((args, kwargs)),
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.coco_command(update, SimpleNamespace(user_data={}, bot=object()))

    assert dispatched == []
    assert replies == [
        "❌ Only the CoCo control owner or an admin can control another user's topic."
    ]


def _install_general_text_handler_control_fixtures(monkeypatch, *, dispatched, replies, routing):
    """Install a shared General control owned by A for end-to-end text tests."""
    chat_id = -100123
    monkeypatch.setattr(bot, "_is_chat_allowed", lambda _chat: True)
    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: False)
    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(100, 1, chat_id),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "iter_topic_bindings",
        lambda: iter(
            [
                (100, chat_id, 1, SimpleNamespace(display_name="coco-control")),
                (200, chat_id, 88, SimpleNamespace(display_name="my-topic")),
                (300, chat_id, 99, SimpleNamespace(display_name="other-topic")),
                (400, chat_id, 101, SimpleNamespace(display_name="duplicate")),
                (500, chat_id, 102, SimpleNamespace(display_name="duplicate")),
            ]
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda user_id, *_args, **_kwargs: routing.append(user_id),
    )

    async def _dispatch(**kwargs):
        dispatched.append(kwargs)

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "_dispatch_coco_control_action", _dispatch)
    monkeypatch.setattr(bot, "safe_reply", _safe_reply)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "action"),
    [
        ("tell my-topic to inspect this", "steer"),
        ("queue my-topic: inspect this later", "queue"),
    ],
)
async def test_text_handler_allows_single_session_user_to_target_own_general_topic(
    monkeypatch,
    text,
    action,
):
    """Self-targeted General controls reach the secured inner dispatcher."""
    caller_user_id = 200
    chat = SimpleNamespace(type="supergroup", id=-100123, is_forum=True)
    message = SimpleNamespace(
        text=text,
        message_thread_id=1,
        chat=chat,
        chat_id=chat.id,
        message_id=7,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=caller_user_id),
        effective_message=message,
        effective_chat=chat,
        message=message,
    )
    context = SimpleNamespace(bot=object(), user_data={})
    dispatched: list[dict[str, object]] = []
    replies: list[str] = []
    routing: list[int] = []
    _install_general_text_handler_control_fixtures(
        monkeypatch,
        dispatched=dispatched,
        replies=replies,
        routing=routing,
    )

    await bot.text_handler(update, context)

    assert replies == []
    assert dispatched and dispatched[0]["action"] == action
    assert dispatched[0]["target_user_id"] == caller_user_id
    assert dispatched[0]["target_thread_id"] == 88
    assert routing == [100]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "action"),
    [
        ("tell my-topic to inspect this", "steer"),
        ("queue my-topic: inspect this later", "queue"),
    ],
)
async def test_text_handler_admin_can_target_other_owner_from_general(
    monkeypatch,
    text,
    action,
):
    """An admin's natural General control action reaches its selected topic."""
    chat = SimpleNamespace(type="supergroup", id=-100123, is_forum=True)
    message = SimpleNamespace(
        text=text,
        message_thread_id=1,
        chat=chat,
        chat_id=chat.id,
        message_id=7,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=999),
        effective_message=message,
        effective_chat=chat,
        message=message,
    )
    context = SimpleNamespace(bot=object(), user_data={})
    dispatched: list[dict[str, object]] = []
    replies: list[str] = []
    routing: list[int] = []
    _install_general_text_handler_control_fixtures(
        monkeypatch,
        dispatched=dispatched,
        replies=replies,
        routing=routing,
    )
    monkeypatch.setattr(bot, "_is_admin_user", lambda user_id: user_id == 999)

    await bot.text_handler(update, context)

    assert replies == []
    assert dispatched and dispatched[0]["action"] == action
    assert dispatched[0]["target_user_id"] == 200
    assert dispatched[0]["target_thread_id"] == 88
    assert routing == [100]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "ordinary General prompt",
        "tell other-topic to inspect this",
        "tell duplicate to inspect this",
        "tell my-topic",
    ],
)
async def test_text_handler_keeps_non_owner_general_actions_fail_closed(monkeypatch, text):
    """Only a parseable self-target bypasses the outer General owner gate."""
    chat = SimpleNamespace(type="supergroup", id=-100123, is_forum=True)
    message = SimpleNamespace(
        text=text,
        message_thread_id=1,
        chat=chat,
        chat_id=chat.id,
        message_id=7,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=200),
        effective_message=message,
        effective_chat=chat,
        message=message,
    )
    context = SimpleNamespace(bot=object(), user_data={})
    dispatched: list[dict[str, object]] = []
    replies: list[str] = []
    routing: list[int] = []
    _install_general_text_handler_control_fixtures(
        monkeypatch,
        dispatched=dispatched,
        replies=replies,
        routing=routing,
    )

    await bot.text_handler(update, context)

    assert dispatched == []
    assert routing == []
    assert replies == [f"❌ {bot._COCO_CONTROL_PERMISSION_DENIED_TEXT}"]


@pytest.mark.asyncio
async def test_single_session_user_cannot_natural_steer_another_owner_from_general(
    monkeypatch,
):
    message = SimpleNamespace(
        chat=SimpleNamespace(id=-100123),
        chat_id=-100123,
        message_id=1,
    )
    context = SimpleNamespace(bot=object(), user_data={})
    replies: list[str] = []
    dispatched: list[object] = []

    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: False)
    monkeypatch.setattr(bot, "_get_user_scope", lambda _uid: bot.SCOPE_SINGLE_SESSION)
    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(100, 1, -100123),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_for_thread",
        lambda uid, tid, **_kwargs: "@control" if (uid, tid) == (100, 1) else None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda uid, tid, **_kwargs: (
            SimpleNamespace(codex_thread_id="control-thread", cwd="/control")
            if (uid, tid) == (100, 1)
            else None
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_topic_response_mode",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "is_coco_control_topic",
        lambda _uid, tid, **_kwargs: tid == 1,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "iter_topic_bindings",
        lambda: iter(
            [
                (100, -100123, 1, SimpleNamespace(display_name="coco-control")),
                (200, -100123, 88, SimpleNamespace(display_name="other-owner")),
            ]
        ),
    )
    monkeypatch.setattr(
        bot,
        "capture_topic_ownership",
        lambda *_args, **_kwargs: bot.TopicOwnership(
            window_id="@control",
            codex_thread_id="control-thread",
            machine_id="local-node",
            cwd="/control",
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        lambda **kwargs: dispatched.append(kwargs),
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot._forward_topic_text_message(
        message=message,
        context=context,
        user_id=999,
        thread_id=1,
        chat_id=-100123,
        text="tell other-owner to focus on the PDF bug",
    )

    assert dispatched == []
    assert replies == [
        "❌ Only the CoCo control owner or an admin can control another user's topic."
    ]


@pytest.mark.asyncio
async def test_single_session_user_cannot_send_plain_prompt_to_general_control(
    monkeypatch,
):
    message = SimpleNamespace(
        chat=SimpleNamespace(id=-100123),
        chat_id=-100123,
        message_id=1,
    )
    context = SimpleNamespace(bot=object(), user_data={})
    replies: list[str] = []
    sends: list[object] = []

    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: False)
    monkeypatch.setattr(bot, "_get_user_scope", lambda _uid: bot.SCOPE_SINGLE_SESSION)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(100, 1, -100123),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_for_thread",
        lambda uid, tid, **_kwargs: "@control" if (uid, tid) == (100, 1) else None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_topic_binding",
        lambda uid, tid, **_kwargs: (
            SimpleNamespace(codex_thread_id="control-thread", cwd="/control")
            if (uid, tid) == (100, 1)
            else None
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_topic_response_mode",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "is_coco_control_topic",
        lambda _uid, tid, **_kwargs: tid == 1,
    )
    monkeypatch.setattr(
        bot,
        "capture_topic_ownership",
        lambda *_args, **_kwargs: bot.TopicOwnership(
            window_id="@control",
            codex_thread_id="control-thread",
            machine_id="local-node",
            cwd="/control",
        ),
    )
    monkeypatch.setattr(bot, "_is_window_in_progress", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(bot.session_manager, "get_window_mention_only", lambda _wid: False)
    monkeypatch.setattr(bot.session_manager, "is_window_external_turn_active", lambda _wid: False)
    monkeypatch.setattr(bot, "enqueue_status_update", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bot, "enqueue_progress_clear", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bot, "enqueue_progress_start", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bot, "note_run_started", lambda **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        lambda **kwargs: sends.append(kwargs),
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot._forward_topic_text_message(
        message=message,
        context=context,
        user_id=999,
        thread_id=1,
        chat_id=-100123,
        text="plain General prompt",
    )

    assert sends == []
    assert replies == [
        "❌ Only the CoCo control owner or an admin can control another user's topic."
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("has_existing_general_binding", [False, True])
async def test_general_without_configured_admin_denies_before_binding_lookup(
    monkeypatch,
    has_existing_general_binding,
):
    message = SimpleNamespace(
        chat=SimpleNamespace(id=-100123),
        chat_id=-100123,
        message_id=1,
    )
    context = SimpleNamespace(bot=object(), user_data={})
    replies: list[str] = []
    lookups: list[tuple[int, int]] = []

    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: False)
    monkeypatch.setattr(bot, "_get_allowed_admins", lambda: set())
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_for_thread",
        lambda uid, tid, **_kwargs: (
            lookups.append((uid, tid))
            or ("@existing-general" if has_existing_general_binding else None)
        ),
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot._forward_topic_text_message(
        message=message,
        context=context,
        user_id=999,
        thread_id=bot.GENERAL_TOPIC_THREAD_ID,
        chat_id=-100123,
        text="General prompt",
    )

    assert replies == [bot._COCO_CONTROL_UNCONFIGURED_TEXT]
    assert lookups == []


@pytest.mark.asyncio
@pytest.mark.parametrize("command_text", ["/q queued guidance", "/esc"])
async def test_single_session_user_cannot_mutate_control_owner_from_general_command(
    monkeypatch,
    command_text,
):
    update = _make_update(command_text, user_id=999, is_forum=True)
    replies: list[str] = []
    resolved: list[object] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_is_admin_user", lambda _uid: False)
    monkeypatch.setattr(bot, "_get_user_scope", lambda _uid: bot.SCOPE_SINGLE_SESSION)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(bot.session_manager, "set_group_chat_id", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(100, 1, -100123),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_window_for_thread",
        lambda *args, **kwargs: resolved.append((args, kwargs)),
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    if command_text.startswith("/q"):
        await bot.queue_command(update, SimpleNamespace(user_data={}, bot=object()))
    else:
        await bot.esc_command(update, SimpleNamespace(user_data={}, bot=object()))

    assert resolved == []
    assert replies == [
        "❌ Only the CoCo control owner or an admin can control another user's topic."
    ]


@pytest.mark.asyncio
async def test_coco_command_rejects_ambiguous_cross_user_topic_target(monkeypatch):
    update = _make_update("/coco steer 88 Focus on the PDF bug")
    replies: list[str] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(bot.session_manager, "set_group_chat_id", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bot.session_manager, "is_coco_control_topic", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(bot, "_ensure_default_coco_general_control", lambda **_kwargs: None)
    monkeypatch.setattr(
        bot.session_manager,
        "get_coco_control_topic",
        lambda _chat_id: bot.CocoControlTopic(100, 1, -100123),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "iter_topic_bindings",
        lambda: iter(
            [
                (100, -100123, 88, SimpleNamespace(display_name="alpha", cwd="/a")),
                (200, -100123, 88, SimpleNamespace(display_name="beta", cwd="/b")),
            ]
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        lambda **_kwargs: pytest.fail("ambiguous target must not be dispatched"),
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    await bot.coco_command(update, SimpleNamespace(user_data={}))

    assert replies == ["❌ Ambiguous target topic `88`; use the dashboard controls."]


@pytest.mark.asyncio
async def test_coco_command_steer_drops_target_after_rebind_during_send(monkeypatch):
    """A direct steer captured for owner A must not dispatch into owner B."""
    user_id = 1147817421
    target_thread_id = 88
    chat_id = -100123
    owner_a = SimpleNamespace(
        window_id="@target-a",
        codex_thread_id="target-thread-a",
        machine_id="machine-a",
        cwd="/workspace/a",
    )
    owner_b = SimpleNamespace(
        window_id="@target-b",
        codex_thread_id="target-thread-b",
        machine_id="machine-b",
        cwd="/workspace/b",
    )
    current_owner = {"value": owner_a}
    capture_calls: list[tuple[int, int, int | None]] = []
    send_started = asyncio.Event()
    release_send = asyncio.Event()
    dispatched: list[str] = []
    update = _make_update("/coco steer 88 Focus on the PDF bug")
    replies: list[str] = []

    def _capture_topic_ownership(
        capture_user_id: int,
        capture_thread_id: int,
        capture_chat_id: int | None,
    ):
        capture_calls.append((capture_user_id, capture_thread_id, capture_chat_id))
        return owner_a

    async def _send_topic_text_to_window(
        *,
        window_id: str,
        topic_ownership=None,
        **_kwargs,
    ):
        send_started.set()
        await release_send.wait()
        if topic_ownership is None:
            # Model the vulnerable call: without a snapshot, dispatch resolves
            # the rebound canonical target (owner B).
            dispatched.append(current_owner["value"].window_id)
            return True, ""
        if topic_ownership != current_owner["value"]:
            return False, "stale topic owner; request was not sent"
        dispatched.append(window_id)
        return True, ""

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(
        bot,
        "_ensure_default_coco_general_control",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(bot.config, "is_group_allowed", lambda _chat_id: True)
    monkeypatch.setattr(
        bot.session_manager,
        "set_group_chat_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "is_coco_control_topic",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "iter_topic_bindings",
        lambda: iter(
            [
                (
                    user_id,
                    chat_id,
                    77,
                    SimpleNamespace(display_name="coco-control", cwd="/env/_coco/ctl"),
                ),
                (
                    user_id,
                    chat_id,
                    target_thread_id,
                    SimpleNamespace(display_name="fmwblog", cwd="/workspace/a"),
                ),
            ]
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_window_for_thread",
        lambda _uid, tid, **_kwargs: (
            owner_a.window_id if tid == target_thread_id else "@control"
        ),
    )
    monkeypatch.setattr(bot, "capture_topic_ownership", _capture_topic_ownership)
    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        _send_topic_text_to_window,
    )

    async def _safe_reply(_message, text: str, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    steer_task = asyncio.create_task(
        bot.coco_command(update, SimpleNamespace(user_data={}))
    )
    await asyncio.wait_for(send_started.wait(), timeout=1)
    current_owner["value"] = owner_b
    release_send.set()
    await asyncio.wait_for(steer_task, timeout=1)

    assert not any(window_id == owner_b.window_id for window_id in dispatched)
    assert capture_calls == [(user_id, target_thread_id, chat_id)]


async def _async_result(result):
    return result
