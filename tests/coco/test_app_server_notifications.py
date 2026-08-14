"""Tests for app-server notification fanout in Telegram bridge."""

import asyncio
from types import SimpleNamespace

import pytest

import coco.bot as bot
import coco.handlers.run_watchdog as run_watchdog


def _install_current_topic_owners(
    monkeypatch,
    bindings: list[tuple[int, int | None, str, int]],
    *,
    codex_thread_id: str,
) -> None:
    """Provide raw topic ownership for mocked thread bindings."""
    persisted_bindings = {
        (user_id, bound_thread_id, chat_id): SimpleNamespace(
            window_id=window_id,
            codex_thread_id=codex_thread_id,
            machine_id=bot.session_manager.get_window_machine_id(window_id),
            cwd="",
        )
        for user_id, chat_id, window_id, bound_thread_id in bindings
    }

    def _get_persisted_topic_binding(
        user_id: int,
        thread_id: int,
        *,
        chat_id: int | None = None,
    ) -> SimpleNamespace | None:
        return persisted_bindings.get((user_id, thread_id, chat_id))

    monkeypatch.setattr(
        bot.session_manager,
        "_get_persisted_topic_binding",
        _get_persisted_topic_binding,
    )


def _install_legacy_remote_binding(monkeypatch) -> None:
    monkeypatch.setattr(
        bot,
        "node_registry",
        SimpleNamespace(
            local_machine_id="controller-node",
            get_node=lambda machine_id: (
                SimpleNamespace(
                    machine_id="remote-node",
                    is_local=False,
                    transport="agent_rpc",
                    runtime={},
                )
                if machine_id == "remote-node"
                else None
            ),
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "find_users_for_codex_thread",
        lambda thread_id: (
            [(10, -100, "@remote", 123)]
            if thread_id == "thread-1"
            else []
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_machine_id",
        lambda window_id: (
            "remote-node" if window_id == "@remote" else ""
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_transport_state",
        lambda _window_id: ("", 0.0, 0),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_ids_for_machine",
        lambda machine_id: (
            {"@remote"} if machine_id == "remote-node" else set()
        ),
    )
    monkeypatch.setattr(
        bot,
        "_remote_transport_state_by_machine",
        {},
    )
    monkeypatch.setattr(bot, "_remote_transport_event_locks", {})


def test_protocol_normalizer_does_not_forge_missing_advertisement(
    monkeypatch,
):
    monkeypatch.setattr(
        bot,
        "node_registry",
        SimpleNamespace(
            get_node=lambda _machine_id: SimpleNamespace(
                runtime={"codex_transport_protocol_version": 1}
            )
        ),
    )

    merge_runtime = getattr(
        bot,
        "_merge_remote_transport_protocol_runtime",
        None,
    )
    assert merge_runtime is not None
    runtime = merge_runtime(
        machine_id="remote-node",
        heartbeat_params={"machine_id": "remote-node"},
        runtime={"tts": {"available": True}},
    )

    assert runtime == {"tts": {"available": True}}


def test_malformed_modern_protocol_advertisement_fails_closed(monkeypatch):
    monkeypatch.setattr(
        bot,
        "node_registry",
        SimpleNamespace(get_node=lambda _machine_id: None),
    )

    merge_runtime = getattr(
        bot,
        "_merge_remote_transport_protocol_runtime",
        None,
    )
    assert merge_runtime is not None
    runtime = merge_runtime(
        machine_id="remote-node",
        heartbeat_params={
            "machine_id": "remote-node",
            "codex_transport_protocol_version": "malformed",
        },
        runtime={},
    )

    assert runtime == {"codex_transport_protocol_version": 1}


@pytest.mark.asyncio
async def test_legacy_agent_notification_without_transport_is_delivered(
    monkeypatch,
):
    delivered: list[tuple[str, dict[str, object]]] = []
    _install_legacy_remote_binding(monkeypatch)

    async def _handle(method, params, *, bot, **_kwargs):
        _ = bot
        delivered.append((method, params))

    monkeypatch.setattr(bot, "_handle_codex_app_server_notification", _handle)

    handler = getattr(
        bot,
        "_handle_controller_rpc_notification",
        None,
    )
    assert handler is not None
    await handler(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread-1",
                "item": {"type": "agentMessage", "text": "done"},
            },
        },
        bot=object(),
    )

    assert delivered == [
        (
            "item/completed",
            {
                "threadId": "thread-1",
                "item": {"type": "agentMessage", "text": "done"},
            },
        )
    ]


@pytest.mark.asyncio
async def test_legacy_agent_interactive_request_without_transport_is_answered(
    monkeypatch,
):
    _install_legacy_remote_binding(monkeypatch)

    async def _handle(method, params, *, bot, **_kwargs):
        _ = bot
        assert method == "item/commandExecution/requestApproval"
        assert params["threadId"] == "thread-1"
        return {"decision": "accept"}

    monkeypatch.setattr(bot, "_handle_codex_app_server_request", _handle)

    handler = getattr(bot, "_handle_controller_rpc_request", None)
    assert handler is not None
    result = await handler(
        {
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thread-1"},
        },
        bot=object(),
    )

    assert result == {"decision": "accept"}


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol_version", [1, "malformed"])
async def test_missing_transport_is_rejected_after_agent_advertises_modern_protocol(
    monkeypatch,
    protocol_version,
):
    delivered: list[tuple[str, dict[str, object]]] = []
    _install_legacy_remote_binding(monkeypatch)
    monkeypatch.setattr(
        bot,
        "node_registry",
        SimpleNamespace(
            local_machine_id="controller-node",
            get_node=lambda _machine_id: SimpleNamespace(
                machine_id="remote-node",
                is_local=False,
                transport="agent_rpc",
                runtime={
                    "codex_transport_protocol_version": protocol_version
                },
            ),
        ),
    )

    async def _handle(method, params, *, bot, **_kwargs):
        _ = bot
        delivered.append((method, params))

    monkeypatch.setattr(bot, "_handle_codex_app_server_notification", _handle)

    handler = getattr(
        bot,
        "_handle_controller_rpc_notification",
        None,
    )
    assert handler is not None
    await handler(
        {
            "method": "item/completed",
            "params": {"threadId": "thread-1"},
        },
        bot=object(),
    )

    assert delivered == []


@pytest.mark.asyncio
async def test_missing_transport_is_rejected_when_another_machine_window_is_modern(
    monkeypatch,
):
    delivered: list[tuple[str, dict[str, object]]] = []
    _install_legacy_remote_binding(monkeypatch)
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_ids_for_machine",
        lambda machine_id: (
            {"@remote", "@other-modern"}
            if machine_id == "remote-node"
            else set()
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_transport_state",
        lambda window_id: (
            ("agent-epoch-1", 100.0, 4)
            if window_id == "@other-modern"
            else ("", 0.0, 0)
        ),
    )

    async def _handle(method, params, *, bot, **_kwargs):
        _ = bot
        delivered.append((method, params))

    monkeypatch.setattr(bot, "_handle_codex_app_server_notification", _handle)

    await bot._handle_controller_rpc_notification(
        {
            "method": "item/completed",
            "params": {"threadId": "thread-1"},
        },
        bot=object(),
    )

    assert delivered == []


def test_confirmed_legacy_rollback_overrides_old_machine_transport_state(
    monkeypatch,
):
    _install_legacy_remote_binding(monkeypatch)
    monkeypatch.setattr(
        bot,
        "node_registry",
        SimpleNamespace(
            local_machine_id="controller-node",
            get_node=lambda _machine_id: SimpleNamespace(
                machine_id="remote-node",
                is_local=False,
                transport="agent_rpc",
                runtime={},
                codex_transport_legacy_confirmed=True,
            ),
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_transport_state",
        lambda _window_id: ("old-modern-epoch", 100.0, 4),
    )
    monkeypatch.setattr(
        bot,
        "_remote_transport_state_by_machine",
        {"remote-node": object()},
    )

    assert bot._remote_machine_allows_legacy_transport(
        machine_id="remote-node",
        window_ids={"@remote"},
    )


def test_confirmed_legacy_rollback_invalidates_modern_window_state(
    monkeypatch,
):
    uncertainty_calls: list[dict[str, object]] = []
    cleared_turns: list[set[str]] = []
    cleared_transport: list[tuple[str, str, float, int]] = []
    machine_state = {"remote-node": object()}
    transport_state = {"@remote": ("old-modern-epoch", 100.0, 4)}
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_ids_for_machine",
        lambda _machine_id: {"@remote"},
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_transport_state",
        lambda window_id: transport_state[window_id],
    )
    monkeypatch.setattr(
        bot,
        "note_transport_reset_uncertainty",
        lambda **kwargs: uncertainty_calls.append(kwargs) or 1,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "clear_window_codex_turns",
        lambda window_ids: cleared_turns.append(window_ids) or 1,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_window_codex_transport_state",
        lambda window_id, *, epoch, epoch_started_at, generation: (
            cleared_transport.append(
                (window_id, epoch, epoch_started_at, generation)
            ),
            transport_state.__setitem__(
                window_id,
                (epoch, epoch_started_at, generation),
            ),
        )[-1],
    )
    monkeypatch.setattr(
        bot,
        "_remote_transport_state_by_machine",
        machine_state,
    )
    monkeypatch.setattr(bot, "emit_telemetry", lambda *_args, **_kwargs: None)

    bot._handle_confirmed_remote_legacy_downgrade("remote-node")
    # Heartbeats repeat after a controller crash/restart. Cleanup must be
    # retryable when stale state remains, but a completed cleanup must not
    # repeatedly invalidate new legacy turns on every heartbeat.
    bot._handle_confirmed_remote_legacy_downgrade("remote-node")

    assert uncertainty_calls == [
        {
            "window_ids": {"@remote"},
            "reason": "remote_agent_legacy_transport_confirmed",
        }
    ]
    assert cleared_turns == [{"@remote"}]
    assert cleared_transport == [("@remote", "", 0.0, 0)]
    assert machine_state == {}


@pytest.mark.asyncio
async def test_legacy_callback_rejects_ambiguous_blank_machine_binding(
    monkeypatch,
):
    delivered: list[tuple[str, dict[str, object]]] = []
    _install_legacy_remote_binding(monkeypatch)
    monkeypatch.setattr(
        bot.session_manager,
        "find_users_for_codex_thread",
        lambda thread_id: (
            [
                (10, -100, "@remote", 123),
                (11, -101, "@blank-machine", 124),
            ]
            if thread_id == "thread-1"
            else []
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_machine_id",
        lambda window_id: (
            "remote-node" if window_id == "@remote" else ""
        ),
    )

    async def _handle(method, params, *, bot, **_kwargs):
        _ = bot
        delivered.append((method, params))

    monkeypatch.setattr(bot, "_handle_codex_app_server_notification", _handle)

    await bot._handle_controller_rpc_notification(
        {
            "method": "item/completed",
            "params": {"threadId": "thread-1"},
        },
        bot=object(),
    )

    assert delivered == []


@pytest.mark.asyncio
async def test_legacy_request_response_is_discarded_if_agent_upgrades_mid_request(
    monkeypatch,
):
    runtime: dict[str, object] = {}
    _install_legacy_remote_binding(monkeypatch)
    monkeypatch.setattr(
        bot,
        "node_registry",
        SimpleNamespace(
            local_machine_id="controller-node",
            get_node=lambda _machine_id: SimpleNamespace(
                machine_id="remote-node",
                is_local=False,
                transport="agent_rpc",
                runtime=runtime,
            ),
        ),
    )

    async def _handle(_method, _params, *, bot, **_kwargs):
        _ = bot
        runtime["codex_transport_protocol_version"] = 1
        return {"decision": "accept"}

    monkeypatch.setattr(bot, "_handle_codex_app_server_request", _handle)

    handler = getattr(bot, "_handle_controller_rpc_request", None)
    assert handler is not None
    result = await handler(
        {
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thread-1"},
        },
        bot=object(),
    )

    assert result is None


@pytest.mark.asyncio
async def test_legacy_notification_crossing_upgrade_marks_windows_uncertain(
    monkeypatch,
):
    runtime: dict[str, object] = {}
    uncertainty_calls: list[dict[str, object]] = []
    clear_calls: list[set[str]] = []
    _install_legacy_remote_binding(monkeypatch)
    monkeypatch.setattr(
        bot,
        "node_registry",
        SimpleNamespace(
            local_machine_id="controller-node",
            get_node=lambda _machine_id: SimpleNamespace(
                machine_id="remote-node",
                is_local=False,
                transport="agent_rpc",
                runtime=runtime,
            ),
        ),
    )
    monkeypatch.setattr(
        bot,
        "note_transport_reset_uncertainty",
        lambda **kwargs: uncertainty_calls.append(kwargs) or 1,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "clear_window_codex_turns",
        lambda window_ids: clear_calls.append(window_ids) or len(window_ids),
    )
    monkeypatch.setattr(bot, "emit_telemetry", lambda *_args, **_kwargs: None)

    async def _handle(_method, _params, *, bot, **_kwargs):
        _ = bot
        runtime["codex_transport_protocol_version"] = 1

    monkeypatch.setattr(bot, "_handle_codex_app_server_notification", _handle)

    await bot._handle_controller_rpc_notification(
        {
            "method": "turn/completed",
            "params": {"threadId": "thread-1"},
        },
        bot=object(),
    )

    assert uncertainty_calls == [
        {
            "window_ids": {"@remote"},
            "reason": "legacy_forwarded_notification_became_stale",
        }
    ]
    assert clear_calls == [{"@remote"}]


@pytest.mark.asyncio
async def test_legacy_notification_upgrade_does_not_clear_new_transport_window(
    monkeypatch,
):
    runtime: dict[str, object] = {}
    transport_state: dict[str, tuple[str, float, int]] = {
        "@remote": ("", 0.0, 0)
    }
    uncertainty_calls: list[dict[str, object]] = []
    clear_calls: list[set[str]] = []
    _install_legacy_remote_binding(monkeypatch)
    monkeypatch.setattr(
        bot,
        "node_registry",
        SimpleNamespace(
            local_machine_id="controller-node",
            get_node=lambda _machine_id: SimpleNamespace(
                machine_id="remote-node",
                is_local=False,
                transport="agent_rpc",
                runtime=runtime,
            ),
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_transport_state",
        lambda window_id: transport_state[window_id],
    )
    monkeypatch.setattr(
        bot,
        "note_transport_reset_uncertainty",
        lambda **kwargs: uncertainty_calls.append(kwargs) or 1,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "clear_window_codex_turns",
        lambda window_ids: clear_calls.append(window_ids) or len(window_ids),
    )
    monkeypatch.setattr(bot, "emit_telemetry", lambda *_args, **_kwargs: None)

    async def _handle(_method, _params, *, bot, **_kwargs):
        _ = bot
        runtime["codex_transport_protocol_version"] = 1
        transport_state["@remote"] = ("agent-epoch-1", 100.0, 4)

    monkeypatch.setattr(bot, "_handle_codex_app_server_notification", _handle)

    await bot._handle_controller_rpc_notification(
        {
            "method": "turn/completed",
            "params": {"threadId": "thread-1"},
        },
        bot=object(),
    )

    assert uncertainty_calls == []
    assert clear_calls == []


@pytest.mark.asyncio
async def test_legacy_lifecycle_response_without_transport_metadata_is_accepted(
    monkeypatch,
):
    _install_legacy_remote_binding(monkeypatch)
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_machine_id",
        lambda _window_id: "",
    )
    uncertainty_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        bot,
        "note_transport_reset_uncertainty",
        lambda **kwargs: uncertainty_calls.append(kwargs) or 1,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "clear_window_codex_turns",
        lambda _window_ids: 1,
    )
    monkeypatch.setattr(bot, "emit_telemetry", lambda *_args, **_kwargs: None)

    accepted = await bot._accept_remote_codex_transport_result(
        "@remote",
        {
            "thread_id": "thread-1",
            "turn_id": "turn-1",
            "_coco_remote_machine_id": "remote-node",
        },
    )

    assert accepted is True
    assert uncertainty_calls == []


@pytest.mark.asyncio
async def test_modern_generation_zero_empty_lifecycle_noop_is_accepted(
    monkeypatch,
):
    uncertainty_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_machine_id",
        lambda _window_id: "remote-node",
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_ids_for_machine",
        lambda _machine_id: {"@remote"},
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_transport_state",
        lambda _window_id: ("", 0.0, 0),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "clear_window_codex_turns",
        lambda _window_ids: 1,
    )
    monkeypatch.setattr(
        bot,
        "note_transport_reset_uncertainty",
        lambda **kwargs: uncertainty_calls.append(kwargs) or 1,
    )
    monkeypatch.setattr(bot, "emit_telemetry", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bot, "_remote_transport_state_by_machine", {})

    result = {
        "thread_id": "",
        "turn_id": "",
        "transport_lifecycle_noop": True,
        "transport_epoch": "agent-epoch-idle",
        "transport_epoch_started_at": 100.0,
        "transport_generation": 0,
        "transport_reset_sequence": 0,
        "transport_last_reset_generation": 0,
        "transport_last_reset_reason": "",
    }
    accepted = await bot._accept_remote_codex_transport_result(
        "@remote",
        result,
    )

    assert accepted is True
    assert uncertainty_calls == []

    result["thread_id"] = "thread-mutated"
    rejected = await bot._accept_remote_codex_transport_result(
        "@remote",
        result,
    )
    assert rejected is False


@pytest.mark.asyncio
async def test_partial_transport_metadata_is_not_treated_as_legacy(
    monkeypatch,
):
    _install_legacy_remote_binding(monkeypatch)
    uncertainty_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        bot,
        "note_transport_reset_uncertainty",
        lambda **kwargs: uncertainty_calls.append(kwargs) or 1,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "clear_window_codex_turns",
        lambda _window_ids: 1,
    )
    monkeypatch.setattr(bot, "emit_telemetry", lambda *_args, **_kwargs: None)

    accepted = await bot._accept_remote_codex_transport_result(
        "@remote",
        {
            "thread_id": "thread-1",
            "turn_id": "turn-1",
            "transport_epoch": "partial-epoch",
        },
    )

    assert accepted is False
    assert uncertainty_calls[-1]["reason"] == (
        "remote_send_transport_metadata_missing"
    )


@pytest.mark.asyncio
async def test_lifecycle_target_machine_must_match_existing_window_binding(
    monkeypatch,
):
    _install_legacy_remote_binding(monkeypatch)
    uncertainty_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        bot,
        "note_transport_reset_uncertainty",
        lambda **kwargs: uncertainty_calls.append(kwargs) or 1,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "clear_window_codex_turns",
        lambda _window_ids: 1,
    )
    monkeypatch.setattr(bot, "emit_telemetry", lambda *_args, **_kwargs: None)

    accepted = await bot._accept_remote_codex_transport_result(
        "@remote",
        {
            "thread_id": "thread-1",
            "turn_id": "turn-1",
            "_coco_remote_machine_id": "different-node",
        },
    )

    assert accepted is False
    assert uncertainty_calls[-1] == {
        "window_ids": {"@remote"},
        "reason": "remote_response_machine_mismatch",
    }


@pytest.mark.asyncio
async def test_remote_transport_reset_marks_machine_topics_uncertain(monkeypatch):
    uncertainty_calls: list[dict[str, object]] = []
    clear_calls: list[set[str]] = []

    monkeypatch.setattr(
        bot.session_manager,
        "get_window_ids_for_machine",
        lambda machine_id: (
            {"@remote-1", "@remote-2"}
            if machine_id == "remote-node"
            else set()
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_transport_state",
        lambda window_id: (
            ("agent-epoch-1", 100.0, 7)
            if window_id == "@remote-1"
            else ("agent-epoch-1", 100.0, 8)
        ),
        raising=False,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "clear_window_codex_turns",
        lambda window_ids: clear_calls.append(window_ids) or len(window_ids),
    )
    monkeypatch.setattr(
        bot,
        "note_transport_reset_uncertainty",
        lambda **kwargs: uncertainty_calls.append(kwargs) or 2,
    )
    monkeypatch.setattr(bot, "emit_telemetry", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        bot,
        "_remote_transport_state_by_machine",
        {},
        raising=False,
    )

    await bot._handle_codex_app_server_notification(
        "coco/transportReset",
        {
            "machineId": "remote-node",
            "reason": "request_timeout:turn/start",
            "generation": 7,
            "transportEpoch": "agent-epoch-1",
            "transportEpochStartedAt": 100.0,
            "resetSequence": 1,
        },
        bot=object(),
    )

    assert clear_calls == [{"@remote-1"}]
    assert uncertainty_calls == [
        {
            "window_ids": {"@remote-1"},
            "reason": "request_timeout:turn/start",
        }
    ]


@pytest.mark.asyncio
async def test_stale_remote_transport_reset_does_not_clear_new_generation(
    monkeypatch,
):
    uncertainty_calls: list[dict[str, object]] = []
    clear_calls: list[set[str]] = []

    monkeypatch.setattr(
        bot.session_manager,
        "get_window_ids_for_machine",
        lambda machine_id: {"@remote"} if machine_id == "remote-node" else set(),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_transport_state",
        lambda _window_id: ("agent-epoch-1", 100.0, 8),
        raising=False,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "clear_window_codex_turns",
        lambda window_ids: clear_calls.append(window_ids) or len(window_ids),
    )
    monkeypatch.setattr(
        bot,
        "note_transport_reset_uncertainty",
        lambda **kwargs: uncertainty_calls.append(kwargs) or 1,
    )
    monkeypatch.setattr(bot, "emit_telemetry", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        bot,
        "_remote_transport_state_by_machine",
        {},
        raising=False,
    )

    await bot._handle_codex_app_server_notification(
        "coco/transportReset",
        {
            "machineId": "remote-node",
            "reason": "request_timeout:turn/start",
            "generation": 7,
            "transportEpoch": "agent-epoch-1",
            "transportEpochStartedAt": 100.0,
            "resetSequence": 1,
        },
        bot=object(),
    )

    assert clear_calls == []
    assert uncertainty_calls == []


def test_remote_heartbeat_reconciles_lost_transport_reset(monkeypatch):
    uncertainty_calls: list[dict[str, object]] = []
    clear_calls: list[set[str]] = []

    monkeypatch.setattr(
        bot.session_manager,
        "get_window_ids_for_machine",
        lambda machine_id: {"@remote"} if machine_id == "remote-node" else set(),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_transport_state",
        lambda _window_id: ("agent-epoch-1", 100.0, 3),
        raising=False,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "clear_window_codex_turns",
        lambda window_ids: clear_calls.append(window_ids) or len(window_ids),
    )
    monkeypatch.setattr(
        bot,
        "note_transport_reset_uncertainty",
        lambda **kwargs: uncertainty_calls.append(kwargs) or 1,
    )
    monkeypatch.setattr(bot, "emit_telemetry", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        bot,
        "_remote_transport_state_by_machine",
        {},
        raising=False,
    )

    bot._reconcile_remote_codex_transport_state(
        machine_id="remote-node",
        transport_epoch="agent-epoch-1",
        transport_epoch_started_at=100.0,
        current_generation=4,
        reset_sequence=1,
        last_reset_generation=3,
        last_reset_reason="request_timeout:turn/start",
        source="heartbeat",
    )

    assert clear_calls == [{"@remote"}]
    assert uncertainty_calls == [
        {
            "window_ids": {"@remote"},
            "reason": "request_timeout:turn/start",
        }
    ]


def test_remote_heartbeat_reset_reconciliation_is_idempotent(monkeypatch):
    uncertainty_calls: list[dict[str, object]] = []
    clear_calls: list[set[str]] = []

    monkeypatch.setattr(
        bot.session_manager,
        "get_window_ids_for_machine",
        lambda machine_id: {"@remote"} if machine_id == "remote-node" else set(),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_transport_state",
        lambda _window_id: ("agent-epoch-1", 100.0, 3),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "clear_window_codex_turns",
        lambda window_ids: clear_calls.append(window_ids) or len(window_ids),
    )
    monkeypatch.setattr(
        bot,
        "note_transport_reset_uncertainty",
        lambda **kwargs: uncertainty_calls.append(kwargs) or 1,
    )
    monkeypatch.setattr(bot, "emit_telemetry", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bot, "_remote_transport_state_by_machine", {})

    snapshot = {
        "machine_id": "remote-node",
        "transport_epoch": "agent-epoch-1",
        "transport_epoch_started_at": 100.0,
        "current_generation": 4,
        "reset_sequence": 1,
        "last_reset_generation": 3,
        "last_reset_reason": "request_timeout:turn/start",
        "source": "heartbeat",
    }
    bot._reconcile_remote_codex_transport_state(**snapshot)
    bot._reconcile_remote_codex_transport_state(**snapshot)

    assert clear_calls == [{"@remote"}]
    assert len(uncertainty_calls) == 1


def test_new_agent_epoch_invalidates_old_generation_even_when_counter_rolls_back(
    monkeypatch,
):
    uncertainty_calls: list[dict[str, object]] = []
    clear_calls: list[set[str]] = []

    monkeypatch.setattr(
        bot.session_manager,
        "get_window_ids_for_machine",
        lambda machine_id: {"@remote"} if machine_id == "remote-node" else set(),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_transport_state",
        lambda _window_id: ("agent-epoch-1", 100.0, 42),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "clear_window_codex_turns",
        lambda window_ids: clear_calls.append(window_ids) or len(window_ids),
    )
    monkeypatch.setattr(
        bot,
        "note_transport_reset_uncertainty",
        lambda **kwargs: uncertainty_calls.append(kwargs) or 1,
    )
    monkeypatch.setattr(bot, "emit_telemetry", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bot, "_remote_transport_state_by_machine", {})

    bot._reconcile_remote_codex_transport_state(
        machine_id="remote-node",
        transport_epoch="agent-epoch-2",
        transport_epoch_started_at=50.0,
        current_generation=1,
        reset_sequence=0,
        last_reset_generation=0,
        last_reset_reason="",
        source="heartbeat",
    )
    assert clear_calls == []
    bot._reconcile_remote_codex_transport_state(
        machine_id="remote-node",
        transport_epoch="agent-epoch-2",
        transport_epoch_started_at=50.0,
        current_generation=1,
        reset_sequence=0,
        last_reset_generation=0,
        last_reset_reason="",
        source="heartbeat",
    )

    assert clear_calls == [{"@remote"}]
    assert uncertainty_calls == [
        {
            "window_ids": {"@remote"},
            "reason": "remote_agent_transport_epoch_changed",
        }
    ]


@pytest.mark.asyncio
async def test_remote_mutation_gate_blocks_unconfirmed_replacement_epoch(
    monkeypatch,
):
    telemetry: list[tuple[str, dict[str, object]]] = []
    state = bot._RemoteCodexTransportState(
        epoch="agent-epoch-1",
        epoch_started_at=100.0,
        current_generation=8,
        candidate_epoch="agent-epoch-2",
        candidate_heartbeat_count=1,
    )
    monkeypatch.setattr(
        bot,
        "_remote_transport_state_by_machine",
        {"remote-node": state},
    )
    monkeypatch.setattr(bot, "_remote_transport_event_locks", {})
    monkeypatch.setattr(
        bot,
        "emit_telemetry",
        lambda event, **fields: telemetry.append((event, fields)),
    )

    gate = getattr(
        bot,
        "_remote_codex_mutation_dispatch_allowed",
        None,
    )
    assert callable(gate)
    assert await gate("remote-node") is False
    assert telemetry[-1] == (
        "transport.app_server.remote_mutation_deferred",
        {
            "machine_id": "remote-node",
            "current_epoch": "agent-epoch-1",
            "candidate_epoch": "agent-epoch-2",
            "candidate_heartbeat_count": 1,
        },
    )

    state.candidate_epoch = ""
    state.candidate_heartbeat_count = 0
    assert await gate("remote-node") == ("agent-epoch-1", 100.0)


@pytest.mark.asyncio
async def test_remote_mutation_gate_blocks_modern_machine_without_heartbeat(
    monkeypatch,
):
    monkeypatch.setattr(bot, "_remote_transport_state_by_machine", {})
    monkeypatch.setattr(bot, "_remote_transport_event_locks", {})
    monkeypatch.setattr(
        bot,
        "node_registry",
        SimpleNamespace(
            get_node=lambda _machine_id: SimpleNamespace(
                is_local=False,
                transport="agent_rpc",
                runtime={"codex_transport_protocol_version": 1},
                codex_transport_legacy_confirmed=False,
            ),
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_ids_for_machine",
        lambda _machine_id: {"@remote"},
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_transport_state",
        lambda _window_id: ("", 0.0, 0),
    )
    monkeypatch.setattr(bot, "emit_telemetry", lambda *_args, **_kwargs: None)

    assert await bot._remote_codex_mutation_dispatch_allowed(
        "remote-node"
    ) is False


@pytest.mark.asyncio
async def test_remote_mutation_gate_allows_confirmed_legacy_machine(
    monkeypatch,
):
    monkeypatch.setattr(bot, "_remote_transport_state_by_machine", {})
    monkeypatch.setattr(bot, "_remote_transport_event_locks", {})
    monkeypatch.setattr(
        bot,
        "node_registry",
        SimpleNamespace(
            get_node=lambda _machine_id: SimpleNamespace(
                is_local=False,
                transport="agent_rpc",
                runtime={},
                codex_transport_legacy_confirmed=True,
            ),
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_ids_for_machine",
        lambda _machine_id: {"@legacy"},
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_transport_state",
        lambda _window_id: ("", 0.0, 0),
    )

    assert await bot._remote_codex_mutation_dispatch_allowed(
        "remote-node"
    ) is True


@pytest.mark.asyncio
async def test_persisted_same_epoch_generation_rejects_older_first_result(
    monkeypatch,
):
    uncertainty_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        bot.session_manager,
        "get_window_machine_id",
        lambda _window_id: "remote-node",
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_ids_for_machine",
        lambda _machine_id: {"@remote"},
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_transport_state",
        lambda _window_id: ("agent-epoch-1", 100.0, 8),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "clear_window_codex_turns",
        lambda _window_ids: 1,
    )
    monkeypatch.setattr(
        bot,
        "note_transport_reset_uncertainty",
        lambda **kwargs: uncertainty_calls.append(kwargs) or 1,
    )
    monkeypatch.setattr(bot, "emit_telemetry", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bot, "_remote_transport_state_by_machine", {})

    accepted = await bot._accept_remote_codex_transport_result(
        "@remote",
        {
            "transport_epoch": "agent-epoch-1",
            "transport_epoch_started_at": 100.0,
            "transport_generation": 5,
            "transport_reset_sequence": 0,
            "transport_last_reset_generation": 0,
            "transport_last_reset_reason": "",
        },
    )

    assert accepted is False
    assert uncertainty_calls[-1]["reason"] == "stale_remote_send_response"


@pytest.mark.asyncio
async def test_delayed_send_response_from_reset_generation_is_rejected(
    monkeypatch,
):
    uncertainty_calls: list[dict[str, object]] = []
    clear_calls: list[set[str]] = []

    monkeypatch.setattr(
        bot.session_manager,
        "get_window_machine_id",
        lambda _window_id: "remote-node",
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_ids_for_machine",
        lambda _machine_id: {"@remote"},
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_transport_state",
        lambda _window_id: ("agent-epoch-1", 100.0, 7),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "clear_window_codex_turns",
        lambda window_ids: clear_calls.append(window_ids) or len(window_ids),
    )
    monkeypatch.setattr(
        bot,
        "note_transport_reset_uncertainty",
        lambda **kwargs: uncertainty_calls.append(kwargs) or 1,
    )
    monkeypatch.setattr(bot, "emit_telemetry", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bot, "_remote_transport_state_by_machine", {})

    bot._reconcile_remote_codex_transport_state(
        machine_id="remote-node",
        transport_epoch="agent-epoch-1",
        transport_epoch_started_at=100.0,
        current_generation=7,
        reset_sequence=1,
        last_reset_generation=7,
        last_reset_reason="request_timeout:turn/start",
        source="notification",
    )
    accepted = await bot._accept_remote_codex_transport_result(
        "@remote",
        {
            "transport_epoch": "agent-epoch-1",
            "transport_epoch_started_at": 100.0,
            "transport_generation": 7,
            "transport_reset_sequence": 0,
            "transport_last_reset_generation": 0,
            "transport_last_reset_reason": "",
        },
    )

    assert accepted is False
    assert clear_calls
    assert uncertainty_calls[-1] == {
        "window_ids": {"@remote"},
        "reason": "stale_remote_send_response",
    }


@pytest.mark.asyncio
async def test_send_response_from_generation_after_reset_is_accepted(
    monkeypatch,
):
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_machine_id",
        lambda _window_id: "remote-node",
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_ids_for_machine",
        lambda _machine_id: {"@remote"},
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_transport_state",
        lambda _window_id: ("agent-epoch-1", 100.0, 7),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "clear_window_codex_turns",
        lambda _window_ids: 1,
    )
    monkeypatch.setattr(
        bot,
        "note_transport_reset_uncertainty",
        lambda **_kwargs: 1,
    )
    monkeypatch.setattr(bot, "emit_telemetry", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bot, "_remote_transport_state_by_machine", {})
    bot._reconcile_remote_codex_transport_state(
        machine_id="remote-node",
        transport_epoch="agent-epoch-1",
        transport_epoch_started_at=100.0,
        current_generation=7,
        reset_sequence=1,
        last_reset_generation=7,
        last_reset_reason="request_timeout:turn/start",
        source="notification",
    )

    accepted = await bot._accept_remote_codex_transport_result(
        "@remote",
        {
            "transport_epoch": "agent-epoch-1",
            "transport_epoch_started_at": 100.0,
            "transport_generation": 8,
            "transport_reset_sequence": 1,
            "transport_last_reset_generation": 7,
            "transport_last_reset_reason": "request_timeout:turn/start",
        },
    )

    assert accepted is True


@pytest.mark.asyncio
async def test_delayed_reset_from_old_agent_epoch_is_ignored(monkeypatch):
    uncertainty_calls: list[dict[str, object]] = []
    clear_calls: list[set[str]] = []

    monkeypatch.setattr(
        bot.session_manager,
        "get_window_ids_for_machine",
        lambda machine_id: {"@remote"} if machine_id == "remote-node" else set(),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_transport_state",
        lambda _window_id: ("agent-epoch-2", 200.0, 1),
        raising=False,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "clear_window_codex_turns",
        lambda window_ids: clear_calls.append(window_ids) or len(window_ids),
    )
    monkeypatch.setattr(
        bot,
        "note_transport_reset_uncertainty",
        lambda **kwargs: uncertainty_calls.append(kwargs) or 1,
    )
    monkeypatch.setattr(bot, "emit_telemetry", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        bot,
        "_remote_transport_state_by_machine",
        {},
        raising=False,
    )

    await bot._handle_codex_app_server_notification(
        "coco/transportReset",
        {
            "machineId": "remote-node",
            "reason": "request_timeout:turn/start",
            "generation": 9,
            "transportEpoch": "agent-epoch-1",
            "transportEpochStartedAt": 100.0,
            "resetSequence": 4,
        },
        bot=object(),
    )

    assert clear_calls == []
    assert uncertainty_calls == []


@pytest.mark.asyncio
async def test_forwarded_notification_from_stale_generation_is_dropped(
    monkeypatch,
):
    delivered: list[tuple[str, dict[str, object]]] = []

    async def _handle(method, params, *, bot, **_kwargs):
        _ = bot
        delivered.append((method, params))

    monkeypatch.setattr(bot, "_handle_codex_app_server_notification", _handle)
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_ids_for_machine",
        lambda _machine_id: set(),
    )
    monkeypatch.setattr(bot, "emit_telemetry", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        bot,
        "_remote_transport_state_by_machine",
        {
            "remote-node": bot._RemoteCodexTransportState(
                epoch="agent-epoch-1",
                epoch_started_at=100.0,
                current_generation=8,
                reset_sequence=1,
                last_reset_generation=7,
            )
        },
    )
    monkeypatch.setattr(bot, "_remote_transport_event_locks", {})

    await bot._handle_remote_codex_app_server_notification(
        method="turn/completed",
        params={"threadId": "thread-1", "turn": {"id": "turn-old"}},
        transport={
            "machine_id": "remote-node",
            "epoch": "agent-epoch-1",
            "epoch_started_at": 100.0,
            "generation": 7,
            "reset_sequence": 0,
            "last_reset_generation": 0,
            "last_reset_reason": "",
        },
        bot=object(),
    )

    assert delivered == []


@pytest.mark.asyncio
async def test_forwarded_notification_can_dispatch_send_without_machine_deadlock(
    monkeypatch,
):
    notification_started = asyncio.Event()
    release_notification = asyncio.Event()

    async def _handle(_method, _params, *, bot, **_kwargs):
        _ = bot
        notification_started.set()
        await release_notification.wait()

    monkeypatch.setattr(bot, "_handle_codex_app_server_notification", _handle)
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_machine_id",
        lambda _window_id: "remote-node",
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_ids_for_machine",
        lambda _machine_id: {"@remote"},
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_transport_state",
        lambda _window_id: ("agent-epoch-1", 100.0, 8),
    )
    monkeypatch.setattr(bot, "emit_telemetry", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        bot,
        "_remote_transport_state_by_machine",
        {
            "remote-node": bot._RemoteCodexTransportState(
                epoch="agent-epoch-1",
                epoch_started_at=100.0,
                current_generation=8,
            )
        },
    )
    monkeypatch.setattr(bot, "_remote_transport_event_locks", {})
    transport = {
        "machine_id": "remote-node",
        "epoch": "agent-epoch-1",
        "epoch_started_at": 100.0,
        "generation": 8,
        "reset_sequence": 0,
        "last_reset_generation": 0,
        "last_reset_reason": "",
    }
    notification_task = asyncio.create_task(
        bot._handle_remote_codex_app_server_notification(
            method="turn/started",
            params={"threadId": "thread-1", "turn": {"id": "turn-new"}},
            transport=transport,
            bot=object(),
        )
    )
    await notification_started.wait()
    result_task = asyncio.create_task(
        bot._accept_remote_codex_transport_result(
            "@remote",
            {
                "transport_epoch": "agent-epoch-1",
                "transport_epoch_started_at": 100.0,
                "transport_generation": 8,
                "transport_reset_sequence": 0,
                "transport_last_reset_generation": 0,
                "transport_last_reset_reason": "",
            },
        )
    )
    await asyncio.sleep(0)

    assert await asyncio.wait_for(result_task, timeout=0.2) is True
    assert notification_task.done() is False

    release_notification.set()
    await notification_task


@pytest.mark.asyncio
async def test_forwarded_request_response_is_discarded_after_transport_reset(
    monkeypatch,
):
    request_started = asyncio.Event()
    release_request = asyncio.Event()

    async def _handle(_method, _params, *, bot, **_kwargs):
        _ = bot
        request_started.set()
        await release_request.wait()
        return {"decision": "accept"}

    monkeypatch.setattr(bot, "_handle_codex_app_server_request", _handle)
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_ids_for_machine",
        lambda _machine_id: set(),
    )
    monkeypatch.setattr(bot, "emit_telemetry", lambda *_args, **_kwargs: None)
    state = bot._RemoteCodexTransportState(
        epoch="agent-epoch-1",
        epoch_started_at=100.0,
        current_generation=8,
    )
    monkeypatch.setattr(
        bot,
        "_remote_transport_state_by_machine",
        {"remote-node": state},
    )
    monkeypatch.setattr(bot, "_remote_transport_event_locks", {})
    transport = {
        "machine_id": "remote-node",
        "epoch": "agent-epoch-1",
        "epoch_started_at": 100.0,
        "generation": 8,
        "reset_sequence": 0,
        "last_reset_generation": 0,
        "last_reset_reason": "",
    }
    request_task = asyncio.create_task(
        bot._handle_remote_codex_app_server_request(
            method="item/commandExecution/requestApproval",
            params={"threadId": "thread-1"},
            transport=transport,
            bot=object(),
        )
    )
    await request_started.wait()
    state.current_generation = 9
    state.reset_sequence = 1
    state.last_reset_generation = 8
    release_request.set()

    assert await request_task is None


@pytest.mark.asyncio
async def test_remote_config_warning_only_targets_source_machine(monkeypatch):
    sent: list[tuple[int, int | None]] = []
    monkeypatch.setattr(
        bot,
        "_remote_forwarded_transport_is_current",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(bot, "_remote_transport_event_locks", {})
    monkeypatch.setattr(
        bot.session_manager,
        "iter_topic_window_bindings",
        lambda: iter(
            [
                (10, -10010, 110, "@machine-a"),
                (20, -10020, 220, "@machine-b"),
            ]
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_machine_id",
        lambda window_id: {
            "@machine-a": "machine-a",
            "@machine-b": "machine-b",
        }.get(window_id, ""),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_chat_id",
        lambda _user_id, _thread_id, *, chat_id=None: chat_id,
    )

    async def _safe_send(
        _bot,
        chat_id,
        _text,
        *,
        message_thread_id=None,
        **_kwargs,
    ):
        sent.append((chat_id, message_thread_id))

    monkeypatch.setattr(bot, "safe_send", _safe_send)

    await bot._handle_remote_codex_app_server_notification(
        method="configWarning",
        params={"summary": "machine-a warning"},
        transport={"machine_id": "machine-a"},
        bot=object(),
    )

    assert sent == [(-10010, 110)]


@pytest.mark.asyncio
async def test_remote_user_input_request_only_targets_source_machine(monkeypatch):
    sent: list[tuple[int, int | None]] = []
    monkeypatch.setattr(
        bot,
        "_remote_forwarded_transport_is_current",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(bot, "_remote_transport_event_locks", {})
    monkeypatch.setattr(
        bot.session_manager,
        "find_users_for_codex_thread",
        lambda _thread_id: [
            (10, -10010, "@machine-a", 110),
            (20, -10020, "@machine-b", 220),
        ],
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_machine_id",
        lambda window_id: {
            "@machine-a": "machine-a",
            "@machine-b": "machine-b",
        }.get(window_id, ""),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_chat_id",
        lambda _user_id, _thread_id, *, chat_id=None: chat_id,
    )

    async def _safe_send(
        _bot,
        chat_id,
        _text,
        *,
        message_thread_id=None,
        **_kwargs,
    ):
        sent.append((chat_id, message_thread_id))

    monkeypatch.setattr(bot, "safe_send", _safe_send)

    result = await bot._handle_remote_codex_app_server_request(
        method="item/tool/requestUserInput",
        params={
            "threadId": "shared-thread",
            "questions": [
                {
                    "id": "choice",
                    "question": "Continue?",
                    "options": [{"label": "Yes"}],
                }
            ],
        },
        transport={"machine_id": "machine-a"},
        bot=object(),
    )

    assert result == {"answers": {"choice": {"answers": ["Yes"]}}}
    assert sent == [(-10010, 110)]


@pytest.mark.asyncio
async def test_remote_turn_started_scopes_state_mutation_to_source_machine(
    monkeypatch,
):
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        bot,
        "_remote_forwarded_transport_is_current",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(bot, "_remote_transport_event_locks", {})
    monkeypatch.setattr(
        bot.session_manager,
        "set_codex_turn_for_thread",
        lambda thread_id, turn_id, *, machine_id="": calls.append(
            (thread_id, turn_id, machine_id)
        ),
    )

    await bot._handle_remote_codex_app_server_notification(
        method="turn/started",
        params={"threadId": "shared-thread", "turn": {"id": "turn-a"}},
        transport={"machine_id": "machine-a"},
        bot=object(),
    )

    assert calls == [("shared-thread", "turn-a", "machine-a")]


@pytest.mark.asyncio
async def test_remote_final_item_passes_source_machine_to_message_routing(
    monkeypatch,
):
    routed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        bot,
        "_remote_forwarded_transport_is_current",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(bot, "_remote_transport_event_locks", {})

    async def _handle_new_message(
        msg,
        _bot,
        *,
        source_machine_id="",
    ):
        routed.append((msg.text, source_machine_id))

    monkeypatch.setattr(bot, "handle_new_message", _handle_new_message)

    await bot._handle_remote_codex_app_server_notification(
        method="item/completed",
        params={
            "threadId": "shared-thread",
            "item": {"type": "agentMessage", "text": "machine-a answer"},
        },
        transport={"machine_id": "machine-a"},
        bot=object(),
    )

    assert routed == [("machine-a answer", "machine-a")]


@pytest.mark.asyncio
async def test_remote_bookkeeping_does_not_cross_same_thread_between_machines(
    monkeypatch,
):
    retry_calls: list[dict[str, object]] = []
    final_content: list[dict[str, object]] = []
    bindings = [
        (10, -10010, "@machine-a", 110),
        (20, -10020, "@machine-b", 220),
    ]
    monkeypatch.setattr(
        bot.session_manager,
        "find_users_for_codex_thread",
        lambda _thread_id: bindings,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_machine_id",
        lambda window_id: {
            "@machine-a": "machine-a",
            "@machine-b": "machine-b",
        }.get(window_id, ""),
    )
    _install_current_topic_owners(
        monkeypatch,
        bindings,
        codex_thread_id="shared-thread",
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_codex_turn_for_thread",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_chat_id",
        lambda _user_id, _thread_id, *, chat_id=None: chat_id,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "consume_next_topic_response_mode",
        lambda *_args, **_kwargs: "",
    )
    monkeypatch.setattr(bot, "note_run_completed", lambda **_kwargs: None)
    monkeypatch.setattr(
        bot,
        "queued_topic_input_count",
        lambda *_args, **_kwargs: 0,
    )
    monkeypatch.setattr(bot, "get_progress_text", lambda **_kwargs: "")

    async def _noop(*_args, **_kwargs):
        return None

    async def _retry(**kwargs):
        retry_calls.append(kwargs)
        return set()

    async def _enqueue_content(**kwargs):
        final_content.append(kwargs)

    monkeypatch.setattr(bot, "safe_send", _noop)
    monkeypatch.setattr(bot, "handle_new_message", _noop)
    monkeypatch.setattr(bot, "enqueue_progress_clear", _noop)
    monkeypatch.setattr(bot, "enqueue_progress_finalize", _noop)
    monkeypatch.setattr(bot, "enqueue_content_message", _enqueue_content)
    monkeypatch.setattr(
        bot,
        "_retry_failed_turn_after_transient_app_server_error",
        _retry,
    )
    bot._turn_has_final_text.clear()
    bot._pending_transient_app_server_errors.clear()
    bot._pending_image_generation_threads.clear()

    try:
        await bot._handle_codex_app_server_notification(
            "error",
            {
                "threadId": "shared-thread",
                "willRetry": False,
                "error": {
                    "message": "stream disconnected before completion",
                    "additionalDetails": (
                        "an error occurred while processing your request"
                    ),
                },
            },
            bot=object(),
            source_machine_id="machine-a",
        )
        await bot._handle_codex_app_server_notification(
            "turn/completed",
            {
                "threadId": "shared-thread",
                "turn": {"id": "turn-b-failed", "status": "failed"},
            },
            bot=object(),
            source_machine_id="machine-b",
        )

        await bot._handle_codex_app_server_notification(
            "item/completed",
            {
                "threadId": "shared-thread",
                "item": {"type": "agentMessage", "text": "answer from A"},
            },
            bot=object(),
            source_machine_id="machine-a",
        )
        await bot._handle_codex_app_server_notification(
            "turn/completed",
            {
                "threadId": "shared-thread",
                "turn": {"id": "turn-b-complete", "status": "completed"},
            },
            bot=object(),
            source_machine_id="machine-b",
        )
    finally:
        bot._turn_has_final_text.clear()
        bot._pending_transient_app_server_errors.clear()
        bot._pending_image_generation_threads.clear()

    assert retry_calls == []
    assert len(final_content) == 1
    assert final_content[0]["window_id"] == "@machine-b"
    assert "without a final assistant response" in final_content[0]["text"]



@pytest.mark.asyncio
async def test_error_notification_routes_to_thread_bindings(monkeypatch):
    sent: list[tuple[int, int | None, str]] = []

    monkeypatch.setattr(
        bot.session_manager,
        "find_users_for_codex_thread",
        lambda _thread_id: [(10, None, "@1", 111), (20, None, "@2", 222)],
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_chat_id",
        lambda user_id, _thread_id, **_kwargs: -100000 - user_id,
    )

    async def _safe_send(_bot, chat_id, text, *, message_thread_id=None, **_kwargs):
        sent.append((chat_id, message_thread_id, text))

    monkeypatch.setattr(bot, "safe_send", _safe_send)

    await bot._handle_codex_app_server_notification(
        "error",
        {
            "threadId": "thr-1",
            "turnId": "turn-9",
            "willRetry": True,
            "error": {
                "message": "network timeout",
                "additionalDetails": "upstream 504",
            },
        },
        bot=object(),
    )

    assert len(sent) == 2
    assert all("Codex app-server error" in text for _chat, _tid, text in sent)
    assert all("Will retry: yes" in text for _chat, _tid, text in sent)


@pytest.mark.asyncio
async def test_config_warning_notification_broadcasts_once_per_topic(monkeypatch):
    sent: list[tuple[int, int | None, str]] = []

    monkeypatch.setattr(
        bot.session_manager,
        "iter_topic_window_bindings",
        lambda: iter(
            [
                (1, -100001, 10, "@1"),
                (2, -100002, 20, "@2"),
                (3, -100002, 20, "@9"),
            ]
        ),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_chat_id",
        lambda _user_id, _thread_id, *, chat_id=None: chat_id if chat_id is not None else -100001,
    )

    async def _safe_send(_bot, chat_id, text, *, message_thread_id=None, **_kwargs):
        sent.append((chat_id, message_thread_id, text))

    monkeypatch.setattr(bot, "safe_send", _safe_send)

    await bot._handle_codex_app_server_notification(
        "configWarning",
        {
            "summary": "Unknown key in config",
            "details": "model.foo is ignored",
            "path": "/home/user/.codex/config.toml",
        },
        bot=object(),
    )

    # Dedupe is by (chat_id, thread_id); two users share topic 20 in same chat.
    assert len(sent) == 2
    assert any("Codex config warning" in text for _chat, _tid, text in sent)


@pytest.mark.asyncio
async def test_deprecation_notice_notification_broadcasts(monkeypatch):
    sent: list[tuple[int, int | None, str]] = []

    monkeypatch.setattr(
        bot.session_manager,
        "iter_topic_window_bindings",
        lambda: iter([(1, -100010, 10, "@1")]),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_chat_id",
        lambda _user_id, _thread_id, *, chat_id=None: chat_id if chat_id is not None else -100010,
    )

    async def _safe_send(_bot, chat_id, text, *, message_thread_id=None, **_kwargs):
        sent.append((chat_id, message_thread_id, text))

    monkeypatch.setattr(bot, "safe_send", _safe_send)

    await bot._handle_codex_app_server_notification(
        "deprecationNotice",
        {
            "summary": "Legacy endpoint will be removed soon",
            "details": "Migrate to turn/start",
        },
        bot=object(),
    )

    assert len(sent) == 1
    assert "Codex deprecation notice" in sent[0][2]


@pytest.mark.asyncio
async def test_reasoning_delta_notification_is_ignored(monkeypatch):
    handled = []

    async def _handle_new_message(msg, _bot):
        handled.append(msg)

    monkeypatch.setattr(bot, "handle_new_message", _handle_new_message)

    await bot._handle_codex_app_server_notification(
        "item/reasoning/textDelta",
        {"threadId": "th-1", "delta": "thinking token"},
        bot=object(),
    )

    assert handled == []


@pytest.mark.asyncio
async def test_agent_message_delta_notification_is_ignored(monkeypatch):
    handled = []

    async def _handle_new_message(msg, _bot):
        handled.append(msg)

    monkeypatch.setattr(bot, "handle_new_message", _handle_new_message)

    await bot._handle_codex_app_server_notification(
        "item/agentMessage/delta",
        {"threadId": "th-1", "delta": "progress token"},
        bot=object(),
    )

    assert handled == []


@pytest.mark.asyncio
async def test_item_completed_agent_message_routes_final_text(monkeypatch):
    handled = []

    async def _handle_new_message(msg, _bot):
        handled.append(msg)

    monkeypatch.setattr(bot, "handle_new_message", _handle_new_message)
    bot._turn_has_final_text.pop("th-item", None)

    await bot._handle_codex_app_server_notification(
        "item/completed",
        {
            "threadId": "th-item",
            "item": {
                "type": "agentMessage",
                "id": "msg-1",
                "text": "hello world",
            },
        },
        bot=object(),
    )

    assert len(handled) == 1
    msg = handled[0]
    assert msg.session_id == "th-item"
    assert msg.content_type == "text"
    assert msg.text == "hello world"
    assert bot._turn_has_final_text.get("th-item") is True


@pytest.mark.asyncio
async def test_raw_response_commentary_completed_routes_progress(monkeypatch):
    handled = []

    async def _handle_new_message(msg, _bot):
        handled.append(msg)

    monkeypatch.setattr(bot, "handle_new_message", _handle_new_message)

    await bot._handle_codex_app_server_notification(
        "rawResponseItem/completed",
        {
            "threadId": "th-9",
            "item": {
                "type": "message",
                "role": "assistant",
                "phase": "commentary",
                "content": [{"type": "output_text", "text": "overview update"}],
            },
        },
        bot=object(),
    )

    assert len(handled) == 1
    msg = handled[0]
    assert msg.session_id == "th-9"
    assert msg.content_type == "progress"
    assert msg.text == "overview update"


@pytest.mark.asyncio
async def test_raw_response_unknown_phase_stays_progress(monkeypatch):
    handled = []

    async def _handle_new_message(msg, _bot):
        handled.append(msg)

    monkeypatch.setattr(bot, "handle_new_message", _handle_new_message)
    bot._turn_has_final_text.pop("th-10", None)

    await bot._handle_codex_app_server_notification(
        "rawResponseItem/completed",
        {
            "threadId": "th-10",
            "item": {
                "type": "message",
                "role": "assistant",
                "phase": "tool_preamble",
                "content": [{"type": "output_text", "text": "checking files"}],
            },
        },
        bot=object(),
    )

    assert len(handled) == 1
    msg = handled[0]
    assert msg.session_id == "th-10"
    assert msg.content_type == "progress"
    assert msg.text == "checking files"
    assert bot._turn_has_final_text.get("th-10") is None


@pytest.mark.asyncio
async def test_raw_response_completed_ignores_late_text_after_interrupt(monkeypatch):
    handled = []

    async def _handle_new_message(msg, _bot):
        handled.append(msg)

    monkeypatch.setattr(bot, "handle_new_message", _handle_new_message)
    bot._interrupted_codex_threads.add("th-interrupted")
    bot._turn_has_final_text.pop("th-interrupted", None)

    try:
        await bot._handle_codex_app_server_notification(
            "rawResponseItem/completed",
            {
                "threadId": "th-interrupted",
                "item": {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final",
                    "content": [{"type": "output_text", "text": "late final text"}],
                },
            },
            bot=object(),
        )
    finally:
        bot._interrupted_codex_threads.discard("th-interrupted")

    assert handled == []
    assert bot._turn_has_final_text.get("th-interrupted") is None


@pytest.mark.asyncio
async def test_interrupt_fence_survives_parent_completion_and_blocks_late_child_text(
    monkeypatch,
):
    handled = []

    async def _handle_new_message(msg, _bot):
        handled.append(msg)

    monkeypatch.setattr(bot, "handle_new_message", _handle_new_message)
    monkeypatch.setattr(
        bot.session_manager,
        "set_codex_turn_for_thread",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "find_users_for_codex_thread",
        lambda _thread_id: [],
    )
    bot._interrupted_codex_threads.add("th-child")

    try:
        await bot._handle_codex_app_server_notification(
            "turn/completed",
            {
                "threadId": "th-child",
                "turn": {"id": "turn-parent", "status": "interrupted"},
            },
            bot=object(),
        )
        await bot._handle_codex_app_server_notification(
            "item/completed",
            {
                "threadId": "th-child",
                "item": {
                    "type": "agentMessage",
                    "id": "late-child-message",
                    "text": "sub-agent finished after escape",
                },
            },
            bot=object(),
        )

        assert "th-child" in bot._interrupted_codex_threads
        assert handled == []
    finally:
        bot._interrupted_codex_threads.discard("th-child")


@pytest.mark.asyncio
async def test_interrupt_fence_ignores_stale_turn_started_for_same_turn(monkeypatch):
    monkeypatch.setattr(
        bot.session_manager,
        "set_codex_turn_for_thread",
        lambda *_args, **_kwargs: None,
    )
    thread_id = "th-fenced"
    bot._interrupted_codex_threads.add(thread_id)
    bot._interrupted_codex_turns[thread_id] = "turn-old"

    try:
        await bot._handle_codex_app_server_notification(
            "turn/started",
            {"threadId": thread_id, "turn": {"id": "turn-old"}},
            bot=object(),
        )
        assert thread_id in bot._interrupted_codex_threads

        await bot._handle_codex_app_server_notification(
            "turn/started",
            {"threadId": thread_id, "turn": {"id": "turn-new"}},
            bot=object(),
        )
        assert thread_id not in bot._interrupted_codex_threads
    finally:
        bot._interrupted_codex_threads.discard(thread_id)
        bot._interrupted_codex_turns.pop(thread_id, None)


@pytest.mark.asyncio
async def test_interrupted_completion_dispatches_input_queued_after_escape(monkeypatch):
    dispatched: list[dict[str, object]] = []
    monkeypatch.setattr(
        bot.session_manager,
        "set_codex_turn_for_thread",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "find_users_for_codex_thread",
        lambda _thread_id: [(10, -10010, "@1", 111)],
    )
    _install_current_topic_owners(
        monkeypatch,
        [(10, -10010, "@1", 111)],
        codex_thread_id="th-interrupted-queue",
    )
    monkeypatch.setattr(bot, "note_run_completed", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "queued_topic_input_count", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(
        bot,
        "enqueue_progress_clear",
        lambda *_args, **_kwargs: asyncio.sleep(0),
    )

    async def _dispatch_next(**kwargs):
        dispatched.append(kwargs)

    monkeypatch.setattr(bot, "_dispatch_next_queued_input", _dispatch_next)
    bot._interrupted_codex_threads.add("th-interrupted-queue")
    try:
        await bot._handle_codex_app_server_notification(
            "turn/completed",
            {
                "threadId": "th-interrupted-queue",
                "turn": {"id": "turn-old", "status": "interrupted"},
            },
            bot=object(),
        )
    finally:
        bot._interrupted_codex_threads.discard("th-interrupted-queue")

    assert len(dispatched) == 1
    assert dispatched[0]["thread_id"] == 111


@pytest.mark.asyncio
async def test_dashboard_interrupt_completion_discards_queue_without_dispatch(monkeypatch):
    thread_id = "th-dashboard-interrupt"
    state_key = thread_id
    discarded: list[tuple[int, int, int | None]] = []
    docks: list[tuple[int, int, int | None]] = []
    dispatched: list[dict[str, object]] = []
    monkeypatch.setattr(
        bot.session_manager,
        "set_codex_turn_for_thread",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "find_users_for_codex_thread",
        lambda _thread_id: [(10, -10010, "@1", 111)],
    )
    _install_current_topic_owners(
        monkeypatch,
        [(10, -10010, "@1", 111)],
        codex_thread_id=thread_id,
    )
    monkeypatch.setattr(bot, "note_run_completed", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "queued_topic_input_count", lambda *_args, **_kwargs: 2)
    monkeypatch.setattr(
        bot,
        "enqueue_progress_clear",
        lambda *_args, **_kwargs: asyncio.sleep(0),
    )
    monkeypatch.setattr(
        bot,
        "clear_queued_topic_inputs",
        lambda uid, tid, chat_id: discarded.append((uid, tid, chat_id)),
    )

    async def _clear_dock(_bot, uid, tid, chat_id):
        docks.append((uid, tid, chat_id))

    async def _dispatch(**kwargs):
        dispatched.append(kwargs)

    monkeypatch.setattr(bot, "clear_queued_topic_dock", _clear_dock)
    monkeypatch.setattr(bot, "_dispatch_next_queued_input", _dispatch)
    monkeypatch.setattr(
        bot,
        "_discard_queued_on_interrupts",
        {state_key: "turn-dashboard"},
        raising=False,
    )
    bot._interrupted_codex_threads.add(state_key)
    bot._interrupted_codex_turns[state_key] = "turn-dashboard"
    try:
        await bot._handle_codex_app_server_notification(
            "turn/completed",
            {
                "threadId": thread_id,
                "turn": {"id": "turn-dashboard", "status": "interrupted"},
            },
            bot=object(),
        )
    finally:
        bot._interrupted_codex_threads.discard(state_key)
        bot._interrupted_codex_turns.pop(state_key, None)

    assert discarded == [(10, 111, -10010)]
    assert docks == [(10, 111, -10010)]
    assert dispatched == []


@pytest.mark.asyncio
async def test_dashboard_interrupt_completion_is_scoped_to_captured_binding(
    monkeypatch,
):
    """Shared Codex threads retain other bindings and later guidance."""
    thread_id = "th-dashboard-shared"
    state_key = thread_id
    target = (10, -10010, "@1", 111)
    other = (20, -10020, "@2", 222)
    discarded: list[tuple[int, int, int | None, int]] = []
    dispatched: list[tuple[int, int, int | None]] = []
    refreshed: list[tuple[int, int, int | None]] = []
    cleared_docks: list[tuple[int, int, int | None]] = []
    monkeypatch.setattr(
        bot.session_manager,
        "set_codex_turn_for_thread",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "find_users_for_codex_thread",
        lambda _thread_id: [target, other],
    )
    _install_current_topic_owners(
        monkeypatch,
        [target, other],
        codex_thread_id=thread_id,
    )
    monkeypatch.setattr(bot, "note_run_completed", lambda **_kwargs: None)
    monkeypatch.setattr(
        bot,
        "queued_topic_input_count",
        lambda uid, tid, chat_id: 1,
    )
    monkeypatch.setattr(
        bot,
        "capture_topic_ownership",
        lambda *_args, **_kwargs: SimpleNamespace(codex_thread_id=thread_id),
    )
    monkeypatch.setattr(
        bot,
        "discard_queued_topic_inputs_before_generation",
        lambda uid, tid, chat_id, *, generation_cutoff: discarded.append(
            (uid, tid, chat_id, generation_cutoff)
        )
        or 1,
    )
    monkeypatch.setattr(
        bot,
        "enqueue_progress_clear",
        lambda *_args, **_kwargs: asyncio.sleep(0),
    )
    async def _sync_dock(_bot, uid, tid, **kwargs):
        refreshed.append((uid, tid, kwargs.get("chat_id")))

    async def _clear_dock(_bot, uid, tid, chat_id):
        cleared_docks.append((uid, tid, chat_id))

    monkeypatch.setattr(bot, "sync_queued_topic_dock", _sync_dock)
    monkeypatch.setattr(bot, "clear_queued_topic_dock", _clear_dock)

    async def _dispatch(**kwargs):
        dispatched.append((kwargs["user_id"], kwargs["thread_id"], kwargs["chat_id"]))

    monkeypatch.setattr(bot, "_dispatch_next_queued_input", _dispatch)
    bot._discard_queued_on_interrupts[state_key] = bot._DashboardInterruptRecord(
        turn_id="turn-shared",
        owner_user_id=target[0],
        chat_id=target[1],
        thread_id=target[3],
        queued_input_generation_cutoff=4,
        delivery_generation_cutoff=8,
        committed=True,
    )
    bot._interrupted_codex_threads.add(state_key)
    bot._interrupted_codex_turns[state_key] = "turn-shared"
    try:
        await bot._handle_codex_app_server_notification(
            "turn/completed",
            {
                "threadId": thread_id,
                "turn": {"id": "turn-shared", "status": "interrupted"},
            },
            bot=object(),
        )
    finally:
        bot._interrupted_codex_threads.discard(state_key)
        bot._interrupted_codex_turns.pop(state_key, None)
        bot._discard_queued_on_interrupts.pop(state_key, None)

    assert discarded == [(target[0], target[3], target[1], 4)]
    assert dispatched == [
        (target[0], target[3], target[1]),
        (other[0], other[3], other[1]),
    ]
    assert refreshed == [(target[0], target[3], target[1])]
    assert cleared_docks == []


@pytest.mark.asyncio
async def test_uncertain_dashboard_interrupt_discards_only_after_confirmed_terminal(
    monkeypatch,
):
    """A lost interrupt reply is reconciled by a later interrupted terminal."""
    thread_id = "th-dashboard-uncertain"
    state_key = thread_id
    discarded: list[tuple[int, int, int | None, int]] = []
    dispatched: list[dict[str, object]] = []
    synced: list[tuple[int, int, int | None]] = []
    delivered: list[str] = []
    monkeypatch.setattr(
        bot.session_manager,
        "set_codex_turn_for_thread",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "find_users_for_codex_thread",
        lambda _thread_id: [(10, -10010, "@1", 111)],
    )
    _install_current_topic_owners(
        monkeypatch,
        [(10, -10010, "@1", 111)],
        codex_thread_id=thread_id,
    )
    monkeypatch.setattr(bot, "note_run_completed", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "queued_topic_input_count", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(
        bot,
        "discard_queued_topic_inputs_before_generation",
        lambda uid, tid, chat_id, *, generation_cutoff: discarded.append(
            (uid, tid, chat_id, generation_cutoff)
        )
        or 1,
    )
    monkeypatch.setattr(
        bot,
        "enqueue_progress_clear",
        lambda *_args, **_kwargs: asyncio.sleep(0),
    )

    async def _sync_dock(_bot, uid, tid, **kwargs):
        synced.append((uid, tid, kwargs.get("chat_id")))

    async def _dispatch(**kwargs):
        dispatched.append(kwargs)

    monkeypatch.setattr(bot, "sync_queued_topic_dock", _sync_dock)
    monkeypatch.setattr(bot, "clear_queued_topic_dock", lambda *_args, **_kwargs: asyncio.sleep(0))
    monkeypatch.setattr(bot, "_dispatch_next_queued_input", _dispatch)
    monkeypatch.setattr(
        bot,
        "_route_app_server_message",
        lambda message, _bot, **_kwargs: delivered.append(message.text)
        or asyncio.sleep(0),
    )
    bot._discard_queued_on_interrupts[state_key] = bot._DashboardInterruptRecord(
        turn_id="turn-uncertain",
        owner_user_id=10,
        chat_id=-10010,
        thread_id=111,
        queued_input_generation_cutoff=4,
        delivery_generation_cutoff=8,
        uncertain=True,
    )
    bot._interrupted_codex_threads.add(state_key)
    bot._interrupted_codex_turns[state_key] = "turn-uncertain"
    try:
        await bot._handle_codex_app_server_notification(
            "item/completed",
            {
                "threadId": thread_id,
                "item": {"type": "agentMessage", "text": "must discard"},
            },
            bot=object(),
        )
        assert delivered == []
        await bot._handle_codex_app_server_notification(
            "turn/completed",
            {
                "threadId": thread_id,
                "turn": {"id": "turn-uncertain", "status": "interrupted"},
            },
            bot=object(),
        )
    finally:
        bot._interrupted_codex_threads.discard(state_key)
        bot._interrupted_codex_turns.pop(state_key, None)
        bot._discard_queued_on_interrupts.pop(state_key, None)

    assert discarded == [(10, 111, -10010, 4)]
    assert delivered == []
    assert synced == [(10, 111, -10010)]
    assert len(dispatched) == 1
    assert dispatched[0]["thread_id"] == 111


@pytest.mark.asyncio
async def test_uncertain_dashboard_interrupt_releases_queue_on_non_interrupt_terminal(
    monkeypatch,
):
    """A completed terminal proves an uncertain interrupt did not stop the turn."""
    thread_id = "th-dashboard-uncertain-completed"
    state_key = thread_id
    dispatched: list[dict[str, object]] = []
    finalized: list[tuple[int, int]] = []
    delivered: list[str] = []
    monkeypatch.setattr(
        bot.session_manager,
        "set_codex_turn_for_thread",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "find_users_for_codex_thread",
        lambda _thread_id: [(10, -10010, "@1", 111)],
    )
    _install_current_topic_owners(
        monkeypatch,
        [(10, -10010, "@1", 111)],
        codex_thread_id=thread_id,
    )
    monkeypatch.setattr(bot, "note_run_completed", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "queued_topic_input_count", lambda *_args, **_kwargs: 2)

    async def _enqueue_finalize(_bot, uid, window_id, thread_id=None, *, compact=False, chat_id=None):
        finalized.append((uid, thread_id))

    async def _dispatch(**kwargs):
        dispatched.append(kwargs)

    async def _enqueue_content(**_kwargs):
        return None

    monkeypatch.setattr(bot, "enqueue_progress_finalize", _enqueue_finalize)
    monkeypatch.setattr(bot, "enqueue_content_message", _enqueue_content)
    monkeypatch.setattr(bot, "_dispatch_next_queued_input", _dispatch)
    monkeypatch.setattr(
        bot,
        "_route_app_server_message",
        lambda message, _bot, **_kwargs: delivered.append(message.text)
        or asyncio.sleep(0),
    )
    bot._discard_queued_on_interrupts[state_key] = bot._DashboardInterruptRecord(
        turn_id="turn-uncertain-completed",
        owner_user_id=10,
        chat_id=-10010,
        thread_id=111,
        queued_input_generation_cutoff=4,
        delivery_generation_cutoff=8,
        uncertain=True,
    )
    bot._interrupted_codex_threads.add(state_key)
    bot._interrupted_codex_turns[state_key] = "turn-uncertain-completed"
    try:
        await bot._handle_codex_app_server_notification(
            "item/completed",
            {
                "threadId": thread_id,
                "item": {"type": "agentMessage", "text": "natural final"},
            },
            bot=object(),
        )
        assert delivered == []
        await bot._handle_codex_app_server_notification(
            "turn/completed",
            {
                "threadId": thread_id,
                "turn": {"id": "turn-uncertain-completed", "status": "completed"},
            },
            bot=object(),
        )
    finally:
        bot._interrupted_codex_threads.discard(state_key)
        bot._interrupted_codex_turns.pop(state_key, None)
        bot._discard_queued_on_interrupts.pop(state_key, None)
        bot._turn_has_final_text.pop(state_key, None)

    assert finalized == [(10, 111)]
    assert delivered == ["natural final"]
    assert len(dispatched) == 1
    assert dispatched[0]["thread_id"] == 111


@pytest.mark.asyncio
async def test_uncertain_dashboard_interrupt_ignores_unmatched_terminal(
    monkeypatch,
):
    """A terminal for another turn cannot resolve an uncertain interrupt."""
    thread_id = "th-dashboard-uncertain-unmatched"
    state_key = thread_id
    record = bot._DashboardInterruptRecord(
        turn_id="turn-uncertain",
        owner_user_id=10,
        chat_id=-10010,
        thread_id=111,
        queued_input_generation_cutoff=4,
        delivery_generation_cutoff=8,
        uncertain=True,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "set_codex_turn_for_thread",
        lambda *_args, **_kwargs: None,
    )
    bot._discard_queued_on_interrupts[state_key] = record
    bot._interrupted_codex_threads.add(state_key)
    bot._interrupted_codex_turns[state_key] = record.turn_id
    try:
        await bot._handle_codex_app_server_notification(
            "turn/completed",
            {
                "threadId": thread_id,
                "turn": {"id": "another-turn", "status": "completed"},
            },
            bot=object(),
        )
    finally:
        bot._interrupted_codex_threads.discard(state_key)
        bot._interrupted_codex_turns.pop(state_key, None)
        bot._discard_queued_on_interrupts.pop(state_key, None)

    assert record.terminal_seen is False
    assert record.terminal_status == ""
    assert record.uncertain is True


@pytest.mark.asyncio
async def test_raw_response_image_generation_routes_image_output(monkeypatch):
    handled = []

    async def _handle_new_message(msg, _bot):
        handled.append(msg)

    monkeypatch.setattr(bot, "handle_new_message", _handle_new_message)
    bot._turn_has_final_text.pop("th-img", None)

    await bot._handle_codex_app_server_notification(
        "rawResponseItem/completed",
        {
            "threadId": "th-img",
            "item": {
                "type": "image_generation_call",
                "id": "ig_123",
                "status": "completed",
                "result": "aGVsbG8=",
            },
        },
        bot=object(),
    )

    assert len(handled) == 1
    msg = handled[0]
    assert msg.session_id == "th-img"
    assert msg.content_type == "text"
    assert msg.text == ""
    assert msg.image_data == [("image/png", b"hello")]
    assert bot._turn_has_final_text.get("th-img") is True


@pytest.mark.asyncio
async def test_raw_response_image_generation_progress_marks_pending_thread(monkeypatch):
    handled = []

    async def _handle_new_message(msg, _bot):
        handled.append(msg)

    monkeypatch.setattr(bot, "handle_new_message", _handle_new_message)
    bot._pending_image_generation_threads.discard("th-img-progress")

    await bot._handle_codex_app_server_notification(
        "rawResponseItem/completed",
        {
            "threadId": "th-img-progress",
            "item": {
                "type": "message",
                "role": "assistant",
                "phase": "commentary",
                "content": [{"type": "output_text", "text": "image generation"}],
            },
        },
        bot=object(),
    )

    assert len(handled) == 1
    assert handled[0].content_type == "progress"
    assert "th-img-progress" in bot._pending_image_generation_threads


@pytest.mark.asyncio
async def test_turn_completed_finalizes_progress_and_clears_active_turn(monkeypatch):
    set_turn_calls: list[tuple[str, str]] = []
    completed: list[dict[str, object]] = []
    finalized: list[tuple[int, str, int | None]] = []
    cleared: list[tuple[int, int | None]] = []

    monkeypatch.setattr(
        bot.session_manager,
        "set_codex_turn_for_thread",
        lambda thread_id, turn_id: set_turn_calls.append((thread_id, turn_id)),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "find_users_for_codex_thread",
        lambda _thread_id: [(10, None, "@1", 111)],
    )
    _install_current_topic_owners(
        monkeypatch,
        [(10, None, "@1", 111)],
        codex_thread_id="th-1",
    )
    monkeypatch.setattr(bot, "note_run_completed", lambda **kwargs: completed.append(kwargs))

    async def _enqueue_finalize(_bot, user_id, window_id, thread_id=None, *, compact=False, chat_id=None):
        finalized.append((user_id, window_id, thread_id, compact))

    async def _enqueue_clear(_bot, user_id, thread_id=None, chat_id=None):
        cleared.append((user_id, thread_id))

    async def _dispatch_next(**_kwargs):
        raise AssertionError("queue dispatch should not run for completed status")

    async def _enqueue_content(**_kwargs):
        raise AssertionError("fallback content should not be sent when final text exists")

    monkeypatch.setattr(bot, "enqueue_progress_finalize", _enqueue_finalize)
    monkeypatch.setattr(bot, "enqueue_progress_clear", _enqueue_clear)
    monkeypatch.setattr(bot, "enqueue_content_message", _enqueue_content)
    monkeypatch.setattr(bot, "queued_topic_input_count", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(bot, "_dispatch_next_queued_input", _dispatch_next)

    bot._turn_has_final_text["th-1"] = True
    await bot._handle_codex_app_server_notification(
        "turn/completed",
        {
            "threadId": "th-1",
            "turn": {"status": "completed"},
        },
        bot=object(),
    )

    assert set_turn_calls == [("th-1", "")]
    assert finalized == [(10, "@1", 111, True)]
    assert cleared == []
    assert completed and completed[0]["reason"] == "turn_completed:completed"


@pytest.mark.asyncio
async def test_turn_completed_failed_clears_progress_and_dispatches_queue(monkeypatch):
    set_turn_calls: list[tuple[str, str]] = []
    finalized: list[tuple[int, str, int | None]] = []
    cleared: list[tuple[int, int | None]] = []
    dispatched: list[dict[str, object]] = []

    monkeypatch.setattr(
        bot.session_manager,
        "set_codex_turn_for_thread",
        lambda thread_id, turn_id: set_turn_calls.append((thread_id, turn_id)),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "find_users_for_codex_thread",
        lambda _thread_id: [(10, -10010, "@1", 111)],
    )
    _install_current_topic_owners(
        monkeypatch,
        [(10, -10010, "@1", 111)],
        codex_thread_id="th-2",
    )
    monkeypatch.setattr(bot, "note_run_completed", lambda **_kwargs: None)

    async def _enqueue_finalize(_bot, user_id, window_id, thread_id=None, *, compact=False, chat_id=None):
        finalized.append((user_id, window_id, thread_id, compact))

    async def _enqueue_clear(_bot, user_id, thread_id=None, chat_id=None):
        cleared.append((user_id, thread_id))

    async def _dispatch_next(**kwargs):
        dispatched.append(kwargs)

    monkeypatch.setattr(bot, "enqueue_progress_finalize", _enqueue_finalize)
    monkeypatch.setattr(bot, "enqueue_progress_clear", _enqueue_clear)
    monkeypatch.setattr(bot, "queued_topic_input_count", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(bot, "_dispatch_next_queued_input", _dispatch_next)

    await bot._handle_codex_app_server_notification(
        "turn/completed",
        {
            "threadId": "th-2",
            "turn": {"status": "failed"},
        },
        bot=object(),
    )

    assert set_turn_calls == [("th-2", "")]
    assert cleared == [(10, 111)]
    assert finalized == []
    assert len(dispatched) == 1
    assert dispatched[0]["thread_id"] == 111
    assert dispatched[0]["window_id"] == "@1"


@pytest.mark.asyncio
async def test_turn_completed_completed_dispatches_queued_input(monkeypatch):
    set_turn_calls: list[tuple[str, str]] = []
    finalized: list[tuple[int, str, int | None]] = []
    cleared: list[tuple[int, int | None]] = []
    dispatched: list[dict[str, object]] = []

    monkeypatch.setattr(
        bot.session_manager,
        "set_codex_turn_for_thread",
        lambda thread_id, turn_id: set_turn_calls.append((thread_id, turn_id)),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "find_users_for_codex_thread",
        lambda _thread_id: [(10, -10010, "@1", 111)],
    )
    _install_current_topic_owners(
        monkeypatch,
        [(10, -10010, "@1", 111)],
        codex_thread_id="th-completed-q",
    )
    monkeypatch.setattr(bot, "note_run_completed", lambda **_kwargs: None)

    async def _enqueue_finalize(_bot, user_id, window_id, thread_id=None, *, compact=False, chat_id=None):
        finalized.append((user_id, window_id, thread_id, compact))

    async def _enqueue_clear(_bot, user_id, thread_id=None, chat_id=None):
        cleared.append((user_id, thread_id))

    async def _dispatch_next(**kwargs):
        dispatched.append(kwargs)

    async def _enqueue_content(**_kwargs):
        raise AssertionError("fallback content should not be sent when final text exists")

    monkeypatch.setattr(bot, "enqueue_progress_finalize", _enqueue_finalize)
    monkeypatch.setattr(bot, "enqueue_progress_clear", _enqueue_clear)
    monkeypatch.setattr(bot, "enqueue_content_message", _enqueue_content)
    monkeypatch.setattr(bot, "queued_topic_input_count", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(bot, "_dispatch_next_queued_input", _dispatch_next)

    bot._turn_has_final_text["th-completed-q"] = True
    await bot._handle_codex_app_server_notification(
        "turn/completed",
        {
            "threadId": "th-completed-q",
            "turn": {"status": "completed"},
        },
        bot=object(),
    )

    assert set_turn_calls == [("th-completed-q", "")]
    assert finalized == [(10, "@1", 111, True)]
    assert cleared == []
    assert len(dispatched) == 1
    assert dispatched[0]["thread_id"] == 111
    assert dispatched[0]["window_id"] == "@1"


@pytest.mark.asyncio
async def test_turn_completed_failed_retries_pending_text_after_transient_stream_error(
    monkeypatch,
):
    run_watchdog.reset_run_watchdog_for_tests()
    set_turn_calls: list[tuple[str, str]] = []
    completed: list[dict[str, object]] = []
    cleared: list[tuple[int, int | None]] = []
    progress_started: list[tuple[int, str, int | None]] = []
    dispatched: list[dict[str, object]] = []
    retry_calls: list[dict[str, object]] = []
    sent: list[tuple[int, int | None, str]] = []

    monkeypatch.setattr(
        bot.session_manager,
        "set_codex_turn_for_thread",
        lambda thread_id, turn_id: set_turn_calls.append((thread_id, turn_id)),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "find_users_for_codex_thread",
        lambda _thread_id: [(10, -10010, "@1", 111)],
    )
    _install_current_topic_owners(
        monkeypatch,
        [(10, -10010, "@1", 111)],
        codex_thread_id="th-retry",
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_chat_id",
        lambda _user_id, _thread_id, *, chat_id=None: chat_id if chat_id is not None else -10010,
    )
    monkeypatch.setattr(bot, "note_run_completed", lambda **kwargs: completed.append(kwargs))

    async def _enqueue_clear(_bot, user_id, thread_id=None, chat_id=None):
        cleared.append((user_id, thread_id))

    async def _enqueue_progress_start(_bot, user_id, window_id, thread_id=None, chat_id=None):
        progress_started.append((user_id, window_id, thread_id))

    async def _dispatch_next(**kwargs):
        dispatched.append(kwargs)

    async def _send_topic_text_to_window(**kwargs):
        retry_calls.append(kwargs)
        return True, "Sent via app-server to demo"

    async def _safe_send(_bot, chat_id, text, *, message_thread_id=None, **_kwargs):
        sent.append((chat_id, message_thread_id, text))

    monkeypatch.setattr(bot, "enqueue_progress_clear", _enqueue_clear)
    monkeypatch.setattr(bot, "enqueue_progress_start", _enqueue_progress_start)
    monkeypatch.setattr(bot, "_dispatch_next_queued_input", _dispatch_next)
    monkeypatch.setattr(bot, "queued_topic_input_count", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(bot, "safe_send", _safe_send)
    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        _send_topic_text_to_window,
    )

    run_watchdog.note_run_started(
        user_id=10,
        thread_id=111,
        window_id="@1",
        source="user_input",
        pending_text="retry me",
        expect_response=True,
    )

    await bot._handle_codex_app_server_notification(
        "error",
        {
            "threadId": "th-retry",
            "turnId": "turn-retry",
            "willRetry": False,
            "error": {
                "message": (
                    "stream disconnected before completion: "
                    "An error occurred while processing your request. "
                    "Please include the request ID req-1 in your message."
                ),
            },
        },
        bot=object(),
    )

    await bot._handle_codex_app_server_notification(
        "turn/completed",
        {
            "threadId": "th-retry",
            "turn": {"status": "failed"},
        },
        bot=object(),
    )

    assert set_turn_calls == [("th-retry", "")]
    assert cleared == [(10, 111)]
    assert progress_started == [(10, "@1", 111)]
    assert len(retry_calls) == 1
    assert retry_calls[0]["user_id"] == 10
    assert retry_calls[0]["thread_id"] == 111
    assert retry_calls[0]["chat_id"] == -10010
    assert retry_calls[0]["window_id"] == "@1"
    assert retry_calls[0]["text"] == "retry me"
    assert completed == []
    assert dispatched == []
    assert any("Codex app-server error" in text for _chat, _tid, text in sent)
    assert any("Retrying last message after transient Codex stream failure" in text for _chat, _tid, text in sent)
    run_watchdog.reset_run_watchdog_for_tests()


@pytest.mark.asyncio
async def test_turn_completed_promotes_progress_when_no_final_text(monkeypatch):
    set_turn_calls: list[tuple[str, str]] = []
    finalized: list[tuple[int, str, int | None, bool]] = []
    final_content: list[dict[str, object]] = []

    monkeypatch.setattr(
        bot.session_manager,
        "set_codex_turn_for_thread",
        lambda thread_id, turn_id: set_turn_calls.append((thread_id, turn_id)),
    )
    monkeypatch.setattr(
        bot.session_manager,
        "find_users_for_codex_thread",
        lambda _thread_id: [(10, -10010, "@1", 111)],
    )
    _install_current_topic_owners(
        monkeypatch,
        [(10, -10010, "@1", 111)],
        codex_thread_id="th-fallback",
    )
    monkeypatch.setattr(bot, "note_run_completed", lambda **_kwargs: None)
    monkeypatch.setattr(
        bot,
        "get_progress_text",
        lambda *_args, **_kwargs: "promoted from progress",
    )

    async def _enqueue_finalize(_bot, user_id, window_id, thread_id=None, *, compact=False, chat_id=None):
        finalized.append((user_id, window_id, thread_id, compact))

    async def _enqueue_content(**kwargs):
        final_content.append(kwargs)

    async def _dispatch_next(**_kwargs):
        raise AssertionError("queue dispatch should not run when no queued input exists")

    monkeypatch.setattr(bot, "enqueue_progress_finalize", _enqueue_finalize)
    monkeypatch.setattr(bot, "enqueue_content_message", _enqueue_content)
    monkeypatch.setattr(bot, "queued_topic_input_count", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(bot, "_dispatch_next_queued_input", _dispatch_next)

    bot._turn_has_final_text["th-fallback"] = False
    await bot._handle_codex_app_server_notification(
        "turn/completed",
        {
            "threadId": "th-fallback",
            "turn": {"status": "completed"},
        },
        bot=object(),
    )

    assert set_turn_calls == [("th-fallback", "")]
    assert finalized == [(10, "@1", 111, True)]
    assert len(final_content) == 1
    assert final_content[0]["content_type"] == "text"
    assert final_content[0]["text"] == "promoted from progress"


@pytest.mark.asyncio
async def test_turn_completed_uses_warning_when_progress_empty(monkeypatch):
    finalized: list[tuple[int, str, int | None, bool]] = []
    final_content: list[dict[str, object]] = []

    monkeypatch.setattr(
        bot.session_manager,
        "set_codex_turn_for_thread",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "find_users_for_codex_thread",
        lambda _thread_id: [(10, -10010, "@1", 111)],
    )
    _install_current_topic_owners(
        monkeypatch,
        [(10, -10010, "@1", 111)],
        codex_thread_id="th-empty",
    )
    monkeypatch.setattr(bot, "note_run_completed", lambda **_kwargs: None)
    monkeypatch.setattr(
        bot,
        "get_progress_text",
        lambda *_args, **_kwargs: "   ",
    )

    async def _enqueue_finalize(_bot, user_id, window_id, thread_id=None, *, compact=False, chat_id=None):
        finalized.append((user_id, window_id, thread_id, compact))

    async def _enqueue_content(**kwargs):
        final_content.append(kwargs)

    monkeypatch.setattr(bot, "enqueue_progress_finalize", _enqueue_finalize)
    monkeypatch.setattr(bot, "enqueue_content_message", _enqueue_content)
    monkeypatch.setattr(bot, "queued_topic_input_count", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(bot, "_dispatch_next_queued_input", lambda **_kwargs: None)

    bot._turn_has_final_text["th-empty"] = False
    await bot._handle_codex_app_server_notification(
        "turn/completed",
        {"threadId": "th-empty", "turn": {"status": "completed"}},
        bot=object(),
    )

    assert finalized == [(10, "@1", 111, True)]
    assert len(final_content) == 1
    assert "without a final assistant response" in final_content[0]["text"]


@pytest.mark.asyncio
async def test_turn_completed_skips_warning_after_image_only_tool_result(monkeypatch):
    finalized: list[tuple[int, str, int | None, bool]] = []
    final_content: list[dict[str, object]] = []

    monkeypatch.setattr(
        bot.session_manager,
        "set_codex_turn_for_thread",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "find_users_for_codex_thread",
        lambda _thread_id: [(10, -10010, "@1", 111)],
    )
    _install_current_topic_owners(
        monkeypatch,
        [(10, -10010, "@1", 111)],
        codex_thread_id="th-image",
    )
    monkeypatch.setattr(bot, "note_run_completed", lambda **_kwargs: None)

    async def _enqueue_finalize(_bot, user_id, window_id, thread_id=None, *, compact=False, chat_id=None):
        finalized.append((user_id, window_id, thread_id, compact))

    async def _enqueue_content(**kwargs):
        final_content.append(kwargs)

    monkeypatch.setattr(bot, "enqueue_progress_finalize", _enqueue_finalize)
    monkeypatch.setattr(bot, "enqueue_content_message", _enqueue_content)
    monkeypatch.setattr(bot, "queued_topic_input_count", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(bot, "_dispatch_next_queued_input", lambda **_kwargs: None)

    bot._turn_has_final_text["th-image"] = True
    await bot._handle_codex_app_server_notification(
        "turn/completed",
        {"threadId": "th-image", "turn": {"status": "completed"}},
        bot=object(),
    )

    assert finalized == [(10, "@1", 111, True)]
    assert final_content == []


@pytest.mark.asyncio
async def test_turn_completed_waits_for_late_image_generation_result(monkeypatch):
    finalized: list[tuple[int, str, int | None, bool]] = []
    final_content: list[dict[str, object]] = []
    slept: list[float] = []

    monkeypatch.setattr(
        bot.session_manager,
        "set_codex_turn_for_thread",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "find_users_for_codex_thread",
        lambda _thread_id: [(10, -10010, "@1", 111)],
    )
    _install_current_topic_owners(
        monkeypatch,
        [(10, -10010, "@1", 111)],
        codex_thread_id="th-late-image",
    )
    monkeypatch.setattr(bot, "note_run_completed", lambda **_kwargs: None)

    async def _enqueue_finalize(_bot, user_id, window_id, thread_id=None, *, compact=False, chat_id=None):
        finalized.append((user_id, window_id, thread_id, compact))

    async def _enqueue_content(**kwargs):
        final_content.append(kwargs)

    async def _sleep(delay: float):
        slept.append(delay)
        bot._turn_has_final_text["th-late-image"] = True

    monkeypatch.setattr(bot, "enqueue_progress_finalize", _enqueue_finalize)
    monkeypatch.setattr(bot, "enqueue_content_message", _enqueue_content)
    monkeypatch.setattr(bot, "queued_topic_input_count", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(bot, "_dispatch_next_queued_input", lambda **_kwargs: None)
    monkeypatch.setattr(bot.asyncio, "sleep", _sleep)

    bot._turn_has_final_text["th-late-image"] = False
    bot._pending_image_generation_threads.add("th-late-image")
    await bot._handle_codex_app_server_notification(
        "turn/completed",
        {"threadId": "th-late-image", "turn": {"status": "completed"}},
        bot=object(),
    )

    assert slept == [bot._IMAGE_GENERATION_COMPLETION_GRACE_SECONDS]
    assert finalized == [(10, "@1", 111, True)]
    assert final_content == []
    assert "th-late-image" not in bot._pending_image_generation_threads


@pytest.mark.asyncio
async def test_turn_completed_image_generation_still_warns_after_grace_when_no_result(monkeypatch):
    finalized: list[tuple[int, str, int | None, bool]] = []
    final_content: list[dict[str, object]] = []
    slept: list[float] = []

    monkeypatch.setattr(
        bot.session_manager,
        "set_codex_turn_for_thread",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "find_users_for_codex_thread",
        lambda _thread_id: [(10, -10010, "@1", 111)],
    )
    _install_current_topic_owners(
        monkeypatch,
        [(10, -10010, "@1", 111)],
        codex_thread_id="th-image-empty",
    )
    monkeypatch.setattr(bot, "note_run_completed", lambda **_kwargs: None)
    monkeypatch.setattr(
        bot,
        "get_progress_text",
        lambda *_args, **_kwargs: "   ",
    )

    async def _enqueue_finalize(_bot, user_id, window_id, thread_id=None, *, compact=False, chat_id=None):
        finalized.append((user_id, window_id, thread_id, compact))

    async def _enqueue_content(**kwargs):
        final_content.append(kwargs)

    async def _sleep(delay: float):
        slept.append(delay)

    monkeypatch.setattr(bot, "enqueue_progress_finalize", _enqueue_finalize)
    monkeypatch.setattr(bot, "enqueue_content_message", _enqueue_content)
    monkeypatch.setattr(bot, "queued_topic_input_count", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(bot, "_dispatch_next_queued_input", lambda **_kwargs: None)
    monkeypatch.setattr(bot.asyncio, "sleep", _sleep)

    bot._turn_has_final_text["th-image-empty"] = False
    bot._pending_image_generation_threads.add("th-image-empty")
    await bot._handle_codex_app_server_notification(
        "turn/completed",
        {"threadId": "th-image-empty", "turn": {"status": "completed"}},
        bot=object(),
    )

    assert slept == [bot._IMAGE_GENERATION_COMPLETION_GRACE_SECONDS]
    assert finalized == [(10, "@1", 111, True)]
    assert len(final_content) == 1
    assert "without a final assistant response" in final_content[0]["text"]


@pytest.mark.asyncio
async def test_transient_retry_drops_request_after_topic_rebind_during_progress_clear(
    monkeypatch,
):
    """A retry accepted for owner A must not resend into rebound owner B."""
    user_id = 1147817421
    thread_id = 177
    chat_id = -100123
    codex_thread_id = "retry-thread-a"
    owner_a = SimpleNamespace(
        window_id="@retry-a",
        codex_thread_id=codex_thread_id,
        machine_id="machine-a",
        cwd="/workspace/a",
    )
    owner_b = SimpleNamespace(
        window_id="@retry-b",
        codex_thread_id="retry-thread-b",
        machine_id="machine-b",
        cwd="/workspace/b",
    )
    current_owner = {"value": owner_a}
    captured: list[object] = []
    progress_clear_started = asyncio.Event()
    release_progress_clear = asyncio.Event()
    dispatched: list[tuple[str, str]] = []

    candidate = SimpleNamespace(
        resend_text="retry this prompt",
        resend_text_len=len("retry this prompt"),
        auto_retry_allowed=True,
        auto_retry_reason="eligible",
        retry_count=0,
        max_auto_retries=2,
    )

    def _capture_topic_ownership(*_args, **_kwargs):
        assert current_owner["value"] is owner_a
        captured.append(owner_a)
        return owner_a

    async def _enqueue_progress_clear(*_args, **_kwargs):
        progress_clear_started.set()
        # Explicit lifecycle rebind while the retry is awaiting progress
        # cleanup. Any later send must still be fenced to owner A.
        current_owner["value"] = owner_b
        await release_progress_clear.wait()

    async def _send_topic_text_to_window(
        *,
        window_id: str,
        text: str,
        topic_ownership=None,
        **_kwargs,
    ):
        if topic_ownership is None:
            dispatched.append((current_owner["value"].window_id, text))
            return True, ""
        if topic_ownership != current_owner["value"]:
            return False, "stale topic owner; request was not sent"
        dispatched.append((window_id, text))
        return True, ""

    monkeypatch.setattr(
        bot,
        "_find_codex_thread_bindings_for_source",
        lambda *_args, **_kwargs: [
            (user_id, chat_id, owner_a.window_id, thread_id)
        ],
    )
    monkeypatch.setattr(bot, "capture_topic_ownership", _capture_topic_ownership)
    monkeypatch.setattr(
        bot,
        "get_immediate_auto_retry_candidate",
        lambda **_kwargs: candidate,
    )
    monkeypatch.setattr(bot, "note_auto_retry_attempt", lambda **_kwargs: (1, 2))
    monkeypatch.setattr(bot, "note_auto_retry_result", lambda **_kwargs: None)
    monkeypatch.setattr(bot, "enqueue_progress_clear", _enqueue_progress_clear)
    monkeypatch.setattr(
        bot.session_manager,
        "send_topic_text_to_window",
        _send_topic_text_to_window,
    )
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_chat_id",
        lambda *_args, **_kwargs: chat_id,
    )
    monkeypatch.setattr(bot, "enqueue_progress_start", _enqueue_progress_clear)
    monkeypatch.setattr(bot, "safe_send", _enqueue_progress_clear)
    monkeypatch.setattr(bot, "emit_telemetry", lambda *_args, **_kwargs: None)

    retry_task = asyncio.create_task(
        bot._retry_failed_turn_after_transient_app_server_error(
            bot=object(),
            codex_thread_id=codex_thread_id,
            status="failed",
            error_message="transient stream failure",
            source_machine_id="machine-a",
        )
    )
    await asyncio.wait_for(progress_clear_started.wait(), timeout=1)
    assert current_owner["value"] is owner_b
    release_progress_clear.set()
    await asyncio.wait_for(retry_task, timeout=1)

    assert not any(window_id == owner_b.window_id for window_id, _text in dispatched)
    assert captured == [owner_a]
