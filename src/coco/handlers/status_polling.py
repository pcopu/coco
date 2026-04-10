"""Background watchdog/looper polling for thread-bound sessions.

Provides periodic checks for all active users:
  - Run watchdog alerts and auto-retry policy
  - Looper prompt scheduling
  - Topic existence probes and topic cleanup

Key components:
  - STATUS_POLL_INTERVAL: Polling frequency (1 second)
  - TOPIC_CHECK_INTERVAL: Topic existence probe frequency (60 seconds)
  - status_poll_loop: Background polling task
  - update_status_message: Poll and enqueue status updates
"""

import asyncio
import inspect
import logging
import random
import subprocess
import time

from telegram import Bot
from telegram.error import BadRequest

from ..codex_app_server import codex_app_server_client
from ..config import config
from ..node_registry import NODE_STATUS_OFFLINE, NODE_STATUS_ONLINE, node_registry
from ..session import session_manager
from ..telemetry import emit_telemetry
from .interactive_ui import (
    clear_interactive_msg,
    get_interactive_window,
)
from .cleanup import clear_topic_state
from .looper import (
    claim_due_looper_prompt,
    delay_looper_next_prompt,
    prune_looper_topics,
    stop_looper_if_expired,
)
from . import autoresearch, personality
from .message_queue import get_message_queue
from .message_sender import safe_send
from .topic_send import send_text_to_topic as _send_text_to_topic
from .run_watchdog import (
    get_due_run_checks,
    note_auto_retry_attempt,
    note_auto_retry_result,
    note_run_started,
    prune_run_watch_topics,
)

logger = logging.getLogger(__name__)

# Status polling interval
STATUS_POLL_INTERVAL = 1.0  # seconds - faster response (rate limiting at send layer)

# Topic existence probe interval
TOPIC_CHECK_INTERVAL = 60.0  # seconds

WATCHDOG_ACTIVE_TURN_KEEPALIVE_EMOJIS: tuple[str, ...] = (
    "👀",
    "⏳",
    "🧠",
    "⚙️",
    "🔄",
    "📡",
    "🛠️",
)


def _local_machine_identity() -> tuple[str, str]:
    node = node_registry.get_node(node_registry.local_machine_id)
    if node is not None:
        return node.machine_id, node.display_name
    machine_id = config.machine_id.strip()
    machine_name = config.machine_name.strip() or machine_id
    return machine_id, machine_name


def _format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    mins, secs = divmod(total, 60)
    hrs, mins = divmod(mins, 60)
    if hrs > 0:
        return f"{hrs}h {mins:02d}m"
    return f"{mins}m {secs:02d}s"


def _format_checkpoint(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    mins, secs = divmod(seconds, 60)
    if secs == 0:
        return f"{mins}m"
    return f"{mins}m {secs}s"


def _format_node_last_seen(ts_raw: float) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts_raw))
    except Exception:
        return "unknown"


async def _emit_node_status_notifications(bot: Bot) -> None:
    """Relay node online/offline transitions to topics bound to that machine."""
    for change in node_registry.drain_status_changes():
        for user_id, chat_id, thread_id, binding in session_manager.iter_topic_bindings():
            if binding.machine_id.strip() != change.machine_id:
                continue
            resolved_chat_id = (
                session_manager.resolve_chat_id(user_id, thread_id)
                if chat_id is None
                else session_manager.resolve_chat_id(
                    user_id,
                    thread_id,
                    chat_id=chat_id,
                )
            )
            if change.new_status == NODE_STATUS_OFFLINE:
                text = (
                    f"🖥️ Machine offline: `{change.display_name}`\n"
                    f"Last seen: `{_format_node_last_seen(change.last_seen_ts)}`"
                )
            elif change.old_status == NODE_STATUS_OFFLINE and change.new_status == NODE_STATUS_ONLINE:
                text = f"🟢 Machine back online: `{change.display_name}`"
            else:
                continue
            await safe_send(
                bot,
                resolved_chat_id,
                text,
                message_thread_id=thread_id,
            )


def _iter_monitor_workers(*, target_machine_id: str) -> list[str]:
    local_machine_id, _local_machine_name = _local_machine_identity()
    candidates = [
        node
        for node in node_registry.iter_nodes()
        if node.machine_id.strip()
        and node.machine_id.strip() != target_machine_id.strip()
        and node.status == NODE_STATUS_ONLINE
        and "monitor" in {cap.strip().lower() for cap in node.capabilities}
    ]
    candidates.sort(
        key=lambda node: (
            1 if node.machine_id == local_machine_id else 0,
            1 if node.is_local else 0,
            node.display_name.lower(),
            node.machine_id,
        )
    )
    return [node.machine_id for node in candidates]


async def _probe_machine_from_monitor(
    machine_id: str,
    *,
    via_machine_id: str = "",
) -> dict[str, object]:
    from ..agent_rpc import agent_rpc_client

    result = await agent_rpc_client.probe_machine(
        machine_id,
        via_machine_id=via_machine_id,
    )
    if not isinstance(result, dict):
        raise RuntimeError("invalid probe response")
    return result


async def _probe_stale_nodes(
    bot: Bot | None,
    *,
    now: float | None = None,
) -> None:
    _ = bot
    timestamp = time.time() if now is None else float(now)
    stale_nodes = [
        node
        for node in node_registry.iter_nodes()
        if not node.is_local
        and node.status == NODE_STATUS_ONLINE
        and node.last_seen_ts > 0
        and timestamp - node.last_seen_ts >= node_registry.offline_timeout_seconds
    ]
    for node in stale_nodes:
        worker_ids = _iter_monitor_workers(target_machine_id=node.machine_id)
        via_machine_id = worker_ids[0] if worker_ids else ""
        try:
            payload = await _probe_machine_from_monitor(
                node.machine_id,
                via_machine_id=via_machine_id,
            )
        except Exception as exc:
            emit_telemetry(
                "node.probe_failed",
                machine_id=node.machine_id,
                via_machine_id=via_machine_id,
                error=str(exc),
            )
            logger.info(
                "Monitor probe failed for stale node %s via %s: %s",
                node.machine_id,
                via_machine_id or "controller",
                exc,
            )
            continue

        machine_id = str(payload.get("machine_id", "")).strip() or node.machine_id
        display_name = str(payload.get("display_name", "")).strip() or node.display_name
        tailnet_name = str(payload.get("tailnet_name", "")).strip()
        transport = str(payload.get("transport", node.transport)).strip() or node.transport
        rpc_host = str(payload.get("rpc_host", node.rpc_host)).strip()
        rpc_port_raw = payload.get("rpc_port", node.rpc_port)
        try:
            rpc_port = int(rpc_port_raw or 0)
        except (TypeError, ValueError):
            rpc_port = int(node.rpc_port)
        capabilities = payload.get("capabilities", node.capabilities)
        browse_roots = payload.get("browse_roots", node.browse_roots)
        agent_version = str(payload.get("agent_version", node.agent_version)).strip()
        controller_capable = bool(payload.get("controller_capable", node.controller_capable))
        controller_active = bool(payload.get("controller_active", node.controller_active))
        preferred_controller = bool(payload.get("preferred_controller", node.preferred_controller))
        node_registry.note_heartbeat(
            machine_id=machine_id,
            display_name=display_name,
            tailnet_name=tailnet_name,
            transport=transport,
            rpc_host=rpc_host,
            rpc_port=rpc_port,
            is_local=False,
            browse_roots=list(browse_roots) if isinstance(browse_roots, list) else list(node.browse_roots),
            capabilities=list(capabilities) if isinstance(capabilities, list) else list(node.capabilities),
            agent_version=agent_version,
            controller_capable=controller_capable,
            controller_active=controller_active,
            preferred_controller=preferred_controller,
            now=timestamp,
        )
        emit_telemetry(
            "node.probe_succeeded",
            machine_id=machine_id,
            via_machine_id=via_machine_id,
        )
    node_registry.mark_stale_nodes_offline(now=timestamp)


async def _emit_due_run_watchdog_checks(
    bot: Bot,
    *,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    chat_id: int | None = None,
) -> None:
    """Emit due no-response checks and apply guarded auto-retry policy."""
    if get_interactive_window(user_id, thread_id) == window_id:
        # User prompt is waiting for input; defer watchdog notices.
        emit_telemetry(
            "watchdog.check_deferred",
            user_id=user_id,
            thread_id=thread_id,
            window_id=window_id,
            reason="interactive_ui",
        )
        return

    due_checks = get_due_run_checks(
        user_id=user_id,
        thread_id=thread_id,
        window_id=window_id,
    )
    if not due_checks:
        return

    if chat_id is None:
        resolved_chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    else:
        resolved_chat_id = session_manager.resolve_chat_id(
            user_id,
            thread_id,
            chat_id=chat_id,
        )
    display = session_manager.get_display_name(window_id)
    latest = due_checks[-1]
    checkpoint_label = _format_checkpoint(latest.checkpoint_seconds)

    resend_ok = False
    resend_err = ""
    resend_msg = ""
    auto_retry_attempted = False
    retry_count = latest.retry_count
    retry_limit = latest.max_auto_retries
    auto_retry_allowed = latest.auto_retry_allowed
    auto_retry_reason = latest.auto_retry_reason

    codex_thread_id = session_manager.get_window_codex_thread_id(window_id)
    active_turn_id = session_manager.get_window_codex_active_turn_id(window_id)
    if (
        auto_retry_allowed
        and isinstance(codex_thread_id, str)
        and codex_thread_id
        and (active_turn_id or codex_app_server_client.is_turn_in_progress(codex_thread_id))
    ):
        auto_retry_allowed = False
        auto_retry_reason = "active_turn"

    if auto_retry_allowed and latest.resend_text.strip():
        auto_retry_attempted = True
        retry_count, retry_limit = note_auto_retry_attempt(
            user_id=user_id,
            thread_id=thread_id,
            window_id=window_id,
        )
        if chat_id is None:
            resend_ok, send_result = await session_manager.send_topic_text_to_window(
                user_id=user_id,
                thread_id=thread_id,
                window_id=window_id,
                text=latest.resend_text,
            )
        else:
            resend_ok, send_result = await session_manager.send_topic_text_to_window(
                user_id=user_id,
                thread_id=thread_id,
                chat_id=chat_id,
                window_id=window_id,
                text=latest.resend_text,
            )
        if resend_ok:
            resend_msg = send_result
            resend_err = ""
        else:
            resend_err = send_result
            resend_msg = ""
        note_auto_retry_result(
            user_id=user_id,
            thread_id=thread_id,
            window_id=window_id,
            send_success=resend_ok,
        )

    if auto_retry_attempted and resend_ok:
        action_line = f"Action: auto-retry sent ({retry_count}/{retry_limit})."
    elif auto_retry_attempted and resend_err:
        action_line = (
            f"Action: auto-retry failed ({retry_count}/{retry_limit}) "
            f"(`{resend_err}`)."
        )
    elif auto_retry_reason == "retry_cap":
        action_line = (
            f"Action: retry cap reached ({latest.retry_count}/"
            f"{latest.max_auto_retries}); alert only."
        )
    elif auto_retry_reason == "checkpoint":
        action_line = "Action: alert-only checkpoint; no resend."
    elif auto_retry_reason == "no_payload":
        action_line = "Action: no retry payload available."
    elif auto_retry_reason == "already_sent":
        action_line = "Action: retry already sent successfully; skipping duplicate resend."
    elif auto_retry_reason == "payload_too_large":
        action_line = (
            f"Action: payload too large for auto-retry ({latest.resend_text_len} chars); alert only."
        )
    elif auto_retry_reason == "active_turn":
        action_line = "Action: active turn detected; skipped automatic resend."
    else:
        action_line = "Action: no automatic retry."

    notification_kind = "watchdog_report"
    if auto_retry_reason == "active_turn":
        text = random.choice(WATCHDOG_ACTIVE_TURN_KEEPALIVE_EMOJIS)
        notification_kind = "active_turn_keepalive"
    else:
        text = (
            "🩺 *Run Watchdog Check*\n\n"
            f"Session: `{display}`\n"
            f"No assistant response for: `{_format_elapsed(latest.elapsed_seconds)}`\n"
            f"Checkpoint: `{checkpoint_label}`\n"
            f"{action_line}"
        )
    await safe_send(
        bot,
        resolved_chat_id,
        text,
        message_thread_id=thread_id,
    )
    emit_telemetry(
        "watchdog.check_fired",
        user_id=user_id,
        thread_id=thread_id,
        window_id=window_id,
        checkpoint_seconds=latest.checkpoint_seconds,
        checkpoint_label=checkpoint_label,
        elapsed_seconds=round(latest.elapsed_seconds, 3),
        auto_retry_allowed=auto_retry_allowed,
        auto_retry_reason=auto_retry_reason,
        retry_attempted=auto_retry_attempted,
        retry_count=retry_count,
        retry_limit=retry_limit,
        resend_ok=resend_ok,
        resend_msg=resend_msg,
        resend_err=resend_err,
        resend_text_len=latest.resend_text_len,
        pending_fingerprint=latest.pending_fingerprint,
        notification_kind=notification_kind,
    )
    logger.info(
        "Run watchdog no-response check fired (user=%d thread=%s window=%s checkpoint=%s elapsed=%.1fs retry_reason=%s retry_count=%d/%d retry_attempted=%s resend_ok=%s resend_msg=%s resend_err=%s)",
        user_id,
        thread_id,
        window_id,
        checkpoint_label,
        latest.elapsed_seconds,
        auto_retry_reason,
        retry_count,
        retry_limit,
        auto_retry_attempted,
        resend_ok,
        resend_msg,
        resend_err,
    )


async def _emit_due_looper_prompt(
    bot: Bot,
    *,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    chat_id: int | None = None,
    force: bool = False,
) -> None:
    """Send one due looper nudge when configured for this topic."""
    if thread_id is None:
        return
    if get_interactive_window(user_id, thread_id) == window_id:
        emit_telemetry(
            "looper.prompt_deferred",
            user_id=user_id,
            thread_id=thread_id,
            window_id=window_id,
            reason="interactive_ui",
        )
        return

    expired = stop_looper_if_expired(
        user_id=user_id,
        thread_id=thread_id,
        window_id=window_id,
    )
    if expired:
        resolved_chat_id = (
            session_manager.resolve_chat_id(user_id, thread_id)
            if chat_id is None
            else session_manager.resolve_chat_id(
                user_id,
                thread_id,
                chat_id=chat_id,
            )
        )
        await safe_send(
            bot,
            resolved_chat_id,
            (
                "⏱️ Looper stopped: time limit reached.\n"
                f"Plan: `{expired.plan_path}`\n"
                f'Completion keyword was: `{expired.keyword}`'
            ),
            message_thread_id=thread_id,
        )
        return

    due = claim_due_looper_prompt(
        user_id=user_id,
        thread_id=thread_id,
        window_id=window_id,
        force=force,
    )
    if not due:
        return

    if due.runner_command:
        exit_code, runner_stdout = await asyncio.to_thread(
            _run_looper_runner,
            runner_command=due.runner_command,
            window_id=window_id,
        )
        runner_text = runner_stdout.strip()
        emit_telemetry(
            "looper.runner_result",
            user_id=user_id,
            thread_id=thread_id,
            window_id=window_id,
            exit_code=exit_code,
            prompt_count=due.prompt_count,
            text_len=len(runner_text),
        )
        if exit_code != 0:
            logger.warning(
                "Looper runner failed (user=%d thread=%d window=%s cmd=%r exit=%d)",
                user_id,
                thread_id,
                window_id,
                due.runner_command,
                exit_code,
            )
            delay_looper_next_prompt(
                user_id=user_id,
                thread_id=thread_id,
                delay_seconds=60,
            )
            return
        if not runner_text:
            return
        send_result = _send_text_to_topic(
            bot=bot,
            user_id=user_id,
            thread_id=thread_id,
            chat_id=chat_id,
            text=runner_text,
        )
        if inspect.isawaitable(send_result):
            send_ok, send_err = await send_result
        else:
            send_ok, send_err = send_result
        if not send_ok:
            logger.warning(
                "Looper runner send failed (user=%d thread=%d window=%s): %s",
                user_id,
                thread_id,
                window_id,
                send_err,
            )
            delay_looper_next_prompt(
                user_id=user_id,
                thread_id=thread_id,
                delay_seconds=60,
            )
        return

    if chat_id is None:
        send_ok, send_err = await session_manager.send_topic_text_to_window(
            user_id=user_id,
            thread_id=thread_id,
            window_id=window_id,
            text=due.prompt_text,
        )
    else:
        send_ok, send_err = await session_manager.send_topic_text_to_window(
            user_id=user_id,
            thread_id=thread_id,
            chat_id=chat_id,
            window_id=window_id,
            text=due.prompt_text,
        )
    emit_telemetry(
        "looper.prompt_send_result",
        user_id=user_id,
        thread_id=thread_id,
        window_id=window_id,
        send_ok=send_ok,
        send_err=send_err,
        prompt_count=due.prompt_count,
        interval_seconds=due.interval_seconds,
        text_len=len(due.prompt_text),
    )
    if send_ok:
        note_run_started(
            user_id=user_id,
            thread_id=thread_id,
            window_id=window_id,
            source="looper_tick",
            pending_text=due.prompt_text,
            expect_response=True,
        )
        logger.info(
            "Looper nudge sent (user=%d thread=%d window=%s prompt=%d)",
            user_id,
            thread_id,
            window_id,
            due.prompt_count,
        )
        return

    delay_looper_next_prompt(
        user_id=user_id,
        thread_id=thread_id,
        delay_seconds=60,
    )
    resolved_chat_id = (
        session_manager.resolve_chat_id(user_id, thread_id)
        if chat_id is None
        else session_manager.resolve_chat_id(
            user_id,
            thread_id,
            chat_id=chat_id,
        )
    )
    await safe_send(
        bot,
        resolved_chat_id,
        f"⚠️ Looper nudge failed to send: `{send_err}`. Retrying soon.",
        message_thread_id=thread_id,
    )


async def emit_looper_tick(
    bot: Bot,
    *,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    chat_id: int | None = None,
    force: bool = False,
) -> None:
    """Public looper tick entrypoint for schedulers and immediate triggers."""
    await _emit_due_looper_prompt(
        bot,
        user_id=user_id,
        thread_id=thread_id,
        window_id=window_id,
        chat_id=chat_id,
        force=force,
    )


def _run_looper_runner(
    *,
    runner_command: str,
    window_id: str,
) -> tuple[int, str]:
    """Execute one looper runner in the bound workspace and capture stdout."""
    workspace_dir = session_manager.get_window_state(window_id).cwd.strip() or None
    completed = subprocess.run(
        runner_command,
        shell=True,
        cwd=workspace_dir,
        text=True,
        capture_output=True,
    )
    return completed.returncode, completed.stdout


async def _emit_due_personality_delivery(
    bot: Bot,
    *,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    chat_id: int | None = None,
) -> None:
    """Send one due morning personality digest when enabled for the topic."""
    _ = window_id
    if thread_id is None:
        return

    enabled = session_manager.resolve_thread_skills(
        user_id,
        thread_id,
        chat_id=chat_id,
    )
    if "personality" not in {skill.name for skill in enabled}:
        return

    resolved_chat_id = (
        session_manager.resolve_chat_id(user_id, thread_id)
        if chat_id is None
        else session_manager.resolve_chat_id(
            user_id,
            thread_id,
            chat_id=chat_id,
        )
    )
    digest_text = personality.claim_due_personality_delivery(
        user_id=user_id,
        chat_id=resolved_chat_id,
        thread_id=thread_id,
    )
    if not digest_text:
        return
    await safe_send(
        bot,
        resolved_chat_id,
        digest_text,
        message_thread_id=thread_id,
    )


async def _emit_due_autoresearch_delivery(
    bot: Bot,
    *,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    chat_id: int | None = None,
) -> None:
    """Send one due morning auto research digest when enabled for the topic."""
    _ = window_id
    if thread_id is None:
        return

    enabled = session_manager.resolve_thread_skills(
        user_id,
        thread_id,
        chat_id=chat_id,
    )
    if "autoresearch" not in {skill.name for skill in enabled}:
        return

    resolved_chat_id = (
        session_manager.resolve_chat_id(user_id, thread_id)
        if chat_id is None
        else session_manager.resolve_chat_id(
            user_id,
            thread_id,
            chat_id=chat_id,
        )
    )
    digest_text = autoresearch.claim_due_autoresearch_delivery(
        user_id=user_id,
        chat_id=resolved_chat_id,
        thread_id=thread_id,
    )
    if not digest_text:
        return
    await safe_send(
        bot,
        resolved_chat_id,
        digest_text,
        message_thread_id=thread_id,
    )


async def update_status_message(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
) -> None:
    """Legacy polling entrypoint retained as no-op in app-server runtime."""
    _ = window_id
    if get_interactive_window(user_id, thread_id) is not None:
        await clear_interactive_msg(user_id, bot, thread_id)


async def status_poll_loop(bot: Bot) -> None:
    """Background task to poll terminal status for all thread-bound windows."""
    logger.info("Status polling started (interval: %ss)", STATUS_POLL_INTERVAL)
    last_topic_check = 0.0
    last_local_node_heartbeat = 0.0
    while True:
        try:
            now = time.monotonic()
            if now - last_local_node_heartbeat >= config.node_heartbeat_interval:
                node_registry.ensure_local_node(now=time.time())
                last_local_node_heartbeat = now
            current_ts = time.time()
            await _probe_stale_nodes(bot, now=current_ts)
            await _emit_node_status_notifications(bot)

            raw_bindings = list(session_manager.iter_topic_window_bindings())
            bindings: list[tuple[int, int | None, int, str]] = []
            for entry in raw_bindings:
                if not isinstance(entry, tuple):
                    continue
                if len(entry) == 4:
                    user_id, chat_id, thread_id, wid = entry
                    bindings.append((user_id, chat_id, thread_id, wid))
                    continue
                if len(entry) == 3:
                    user_id, thread_id, wid = entry
                    bindings.append((user_id, None, thread_id, wid))
            active_topics = {
                (user_id, thread_id or 0)
                for user_id, _chat_id, thread_id, _ in bindings
            }
            prune_run_watch_topics(active_topics)
            prune_looper_topics(active_topics)
            autoresearch.prune_autoresearch_topics(active_topics)
            personality.prune_personality_topics(active_topics)

            # Periodic topic existence probe
            if now - last_topic_check >= TOPIC_CHECK_INTERVAL:
                last_topic_check = now
                for user_id, chat_id, thread_id, wid in bindings:
                    try:
                        resolved_chat_id = (
                            session_manager.resolve_chat_id(user_id, thread_id)
                            if chat_id is None
                            else session_manager.resolve_chat_id(
                                user_id,
                                thread_id,
                                chat_id=chat_id,
                            )
                        )
                        await bot.unpin_all_forum_topic_messages(
                            chat_id=resolved_chat_id,
                            message_thread_id=thread_id,
                        )
                    except BadRequest as e:
                        if "Topic_id_invalid" in str(e):
                            # Topic deleted — unbind and clean up state.
                            if chat_id is None:
                                session_manager.unbind_thread(user_id, thread_id)
                            else:
                                session_manager.unbind_thread(
                                    user_id,
                                    thread_id,
                                    chat_id=chat_id,
                                )
                            await clear_topic_state(user_id, thread_id, bot)
                            logger.info(
                                "Topic deleted: unbound window_id '%s' "
                                "for thread %d user %d",
                                wid,
                                thread_id,
                                user_id,
                            )
                        else:
                            logger.debug(
                                "Topic probe error for %s: %s",
                                wid,
                                e,
                            )
                    except Exception as e:
                        logger.debug(
                            "Topic probe error for %s: %s",
                            wid,
                            e,
                        )

            for user_id, chat_id, thread_id, wid in bindings:
                try:
                    queue = get_message_queue(user_id)
                    if queue and not queue.empty():
                        continue

                    if chat_id is None:
                        await _emit_due_run_watchdog_checks(
                            bot,
                            user_id=user_id,
                            thread_id=thread_id,
                            window_id=wid,
                        )
                        await _emit_due_looper_prompt(
                            bot,
                            user_id=user_id,
                            thread_id=thread_id,
                            window_id=wid,
                        )
                        await _emit_due_personality_delivery(
                            bot,
                            user_id=user_id,
                            thread_id=thread_id,
                            window_id=wid,
                        )
                        await _emit_due_autoresearch_delivery(
                            bot,
                            user_id=user_id,
                            thread_id=thread_id,
                            window_id=wid,
                        )
                    else:
                        await _emit_due_run_watchdog_checks(
                            bot,
                            user_id=user_id,
                            thread_id=thread_id,
                            window_id=wid,
                            chat_id=chat_id,
                        )
                        await _emit_due_looper_prompt(
                            bot,
                            user_id=user_id,
                            thread_id=thread_id,
                            window_id=wid,
                            chat_id=chat_id,
                        )
                        await _emit_due_personality_delivery(
                            bot,
                            user_id=user_id,
                            thread_id=thread_id,
                            window_id=wid,
                            chat_id=chat_id,
                        )
                        await _emit_due_autoresearch_delivery(
                            bot,
                            user_id=user_id,
                            thread_id=thread_id,
                            window_id=wid,
                            chat_id=chat_id,
                        )
                except Exception as e:
                    logger.debug(
                        f"Status update error for user {user_id} "
                        f"thread {thread_id}: {e}"
                    )
        except Exception as e:
            logger.error(f"Status poll loop error: {e}")

        await asyncio.sleep(STATUS_POLL_INTERVAL)
