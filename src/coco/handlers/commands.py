"""Slash command handlers extracted from bot routing module."""

from __future__ import annotations

import coco.bot as bot
from telegram import Update
from telegram.ext import ContextTypes

_COMMAND_HANDLER_NAMES = {
    "start_command",
    "folder_command",
    "history_command",
    "unbind_command",
    "esc_command",
    "queue_command",
    "approvals_command",
    "mentions_command",
    "allowed_command",
    "skills_command",
    "apps_command",
    "looper_command",
    "worktree_command",
    "restart_command",
    "resume_command",
    "status_command",
    "model_command",
    "update_command",
}


def _sync_bot_globals() -> None:
    """Refresh handler globals from coco.bot for patch-friendly behavior."""
    target_globals = globals()
    for name, value in bot.__dict__.items():
        if name.startswith("__") or name in _COMMAND_HANDLER_NAMES:
            continue
        target_globals[name] = value


def _scoped_chat_id(update: Update) -> int | None:
    """Return group chat_id for topic-scoped bindings, else None."""
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        return chat.id
    return None


async def _ensure_chat_allowed(update: Update) -> bool:
    """Reject commands in groups not present in ALLOWED_GROUP_IDS."""
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        return True
    if config.is_group_allowed(chat.id):
        return True
    message = update.effective_message
    if message:
        await safe_reply(message, "❌ This group is not allowed to use this bot.")
    return False


def _extract_history_text_from_content(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            text = block.strip()
            if text:
                parts.append(text)
            continue
        if not isinstance(block, dict):
            continue
        text_val = block.get("text")
        if isinstance(text_val, str):
            text = text_val.strip()
            if text:
                parts.append(text)
            continue
        args_val = block.get("arguments")
        if isinstance(args_val, str):
            text = args_val.strip()
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def _render_app_server_history(
    payload: dict[str, object],
    *,
    display_name: str,
    max_items: int = 20,
) -> str:
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raw_items = payload.get("messages")
    if not isinstance(raw_items, list):
        raw_items = []

    rows: list[str] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        if not isinstance(role, str) or role not in {"user", "assistant"}:
            continue
        text = _extract_history_text_from_content(item.get("content"))
        if not text:
            text = _extract_history_text_from_content(item.get("input"))
        if not text:
            text = _extract_history_text_from_content(item.get("output"))
        if not text:
            continue
        prefix = "👤" if role == "user" else "🤖"
        rows.append(f"{prefix} {text}")

    if not rows:
        return f"📋 [{display_name}] No messages yet."

    recent_rows = rows[-max_items:]
    text = "\n\n".join([f"📋 [{display_name}] Recent messages", *recent_rows])
    if len(text) > 3900:
        text = text[-3900:]
        text = f"📋 [{display_name}] Recent messages\n\n…(truncated)\n{text}"
    return text


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _sync_bot_globals()
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return
    if not await _ensure_chat_allowed(update):
        return

    clear_browse_state(context.user_data)
    thread_id = _get_thread_id(update)
    chat = update.effective_chat
    chat_id = _scoped_chat_id(update)
    chat_type = chat.type if chat else "unknown"
    logger.info(
        "Start command received (user=%d, thread=%s, chat_type=%s)",
        user.id,
        thread_id,
        chat_type,
    )

    if not update.message:
        return

    # Capture group chat_id for supergroup forum topic routing.
    if chat_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat_id)

    # Must be in a named topic
    if thread_id is None:
        topic_hint = ""
        emsg = update.effective_message
        if chat and chat.type == "supergroup" and getattr(emsg, "is_topic_message", False):
            topic_hint = (
                "\n\nTelegram did not include topic context for this command."
                "\nReply to any message in this topic and send /start again."
            )
        await safe_reply(
            update.message,
            "🤖 *Codex Monitor*\n\n"
            "This chat is not a named topic.\n"
            "Open or create a topic, then use /start inside that topic."
            f"{topic_hint}",
        )
        return

    # If already bound, confirm and return
    wid = session_manager.get_window_for_thread(user.id, thread_id, chat_id=chat_id)
    if wid:
        binding = session_manager.resolve_topic_binding(
            user.id,
            thread_id,
            chat_id=chat_id,
        )
        if binding and (binding.codex_thread_id or binding.cwd):
            display = binding.display_name or session_manager.get_display_name(wid)
            await safe_reply(
                update.message,
                f"✅ Topic is already bound to `{display}`\n"
                "Send a message in this topic to chat with Codex.",
            )
            return
        session_manager.unbind_thread(user.id, thread_id, chat_id=chat_id)
        clear_queued_topic_inputs(user.id, thread_id)
        await clear_queued_topic_dock(context.bot, user.id, thread_id)

    if not _can_user_create_sessions(user.id):
        await safe_reply(
            update.message,
            "❌ You only have single-session access.\n"
            "Ask an admin to add you to an existing session/topic.",
        )
        return

    logger.info("/start: showing directory browser (user=%d, thread=%d)", user.id, thread_id)
    machine_choices = _sorted_machine_choices()
    if len(machine_choices) > 1:
        msg_text, keyboard = await _open_machine_picker(
            context_user_data=context.user_data,
            thread_id=thread_id,
            chat_id=chat_id,
        )
    else:
        local_machine_id, local_machine_name = _local_machine_identity()
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
            context.user_data[BROWSE_MACHINE_KEY] = local_machine_id
            context.user_data[BROWSE_MACHINE_NAME_KEY] = local_machine_name
            context.user_data["_pending_thread_id"] = thread_id
            context.user_data.pop("_pending_thread_text", None)
        msg_text, keyboard, subdirs = await _build_directory_browser_for_context(
            context.user_data,
            chat_id=chat_id,
        )
        if context.user_data is not None:
            context.user_data[BROWSE_PAGE_KEY] = 0
            context.user_data[BROWSE_DIRS_KEY] = subdirs
    await safe_reply(update.message, msg_text, reply_markup=keyboard)


async def folder_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Alias for /start folder-picker flow."""
    await start_command(update, context)


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _sync_bot_globals()
    """Show message history for the active session or bound thread."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not await _ensure_chat_allowed(update):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    chat_id = _scoped_chat_id(update)
    if chat_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat_id)
    wid = session_manager.resolve_window_for_thread(
        user.id,
        thread_id,
        chat_id=chat_id,
    )
    if not wid:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    if _codex_app_server_enabled():
        binding = session_manager.resolve_topic_binding(
            user.id,
            thread_id,
            chat_id=chat_id,
        )
        codex_thread_id = ""
        if binding:
            codex_thread_id = binding.codex_thread_id.strip()
        if not codex_thread_id:
            codex_thread_id = session_manager.get_window_codex_thread_id(wid)
        if not codex_thread_id:
            await safe_reply(
                update.message,
                "❌ No app-server thread is bound to this topic yet.",
            )
            return

        display = (
            (binding.display_name.strip() if binding else "")
            or session_manager.get_display_name(wid)
        )
        try:
            payload = await codex_app_server_client.thread_read(thread_id=codex_thread_id)
        except Exception as e:
            await safe_reply(update.message, f"❌ Failed to read app-server history: {e}")
            return
        await safe_reply(
            update.message,
            _render_app_server_history(payload, display_name=display),
        )
        return

    await send_history(update.message, wid)


async def unbind_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _sync_bot_globals()
    """Unbind this topic from its session without killing the window."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not await _ensure_chat_allowed(update):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    chat_id = _scoped_chat_id(update)
    if chat_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat_id)
    if thread_id is None:
        await safe_reply(update.message, "❌ This command only works in a topic.")
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id, chat_id=chat_id)
    if not wid:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    display = session_manager.get_display_name(wid)
    session_manager.unbind_thread(user.id, thread_id, chat_id=chat_id)
    await clear_topic_state(user.id, thread_id, context.bot, context.user_data)

    await safe_reply(
        update.message,
        f"✅ Topic unbound from session '{display}'.\n"
        "The app-server thread is still active.\n"
        "Send a message to bind to a new session.",
    )


async def esc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _sync_bot_globals()
    """Send Escape key to interrupt the assistant."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not await _ensure_chat_allowed(update):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    chat_id = _scoped_chat_id(update)
    if chat_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat_id)
    wid = session_manager.resolve_window_for_thread(
        user.id,
        thread_id,
        chat_id=chat_id,
    )
    if not wid:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    if _codex_app_server_enabled():
        codex_thread_id = ""
        binding = session_manager.resolve_topic_binding(
            user.id,
            thread_id,
            chat_id=chat_id,
        )
        if binding:
            codex_thread_id = binding.codex_thread_id.strip()
        if not codex_thread_id:
            codex_thread_id = session_manager.get_window_codex_thread_id(wid)
        active_turn_id = session_manager.get_window_codex_active_turn_id(wid)
        if codex_thread_id and not active_turn_id:
            active_turn_id = codex_app_server_client.get_active_turn_id(codex_thread_id) or ""
        if codex_thread_id and active_turn_id:
            try:
                await codex_app_server_client.turn_interrupt(
                    thread_id=codex_thread_id,
                    turn_id=active_turn_id,
                )
                session_manager.clear_window_codex_turn(wid)
                await safe_reply(update.message, "⎋ Interrupted active turn")
                return
            except Exception as e:
                logger.warning(
                    "App-server interrupt failed (thread=%s turn=%s): %s",
                    codex_thread_id,
                    active_turn_id,
                    e,
                )
                await safe_reply(update.message, f"❌ App-server interrupt failed: {e}")
                return
        await safe_reply(update.message, "ℹ️ No active turn to interrupt.")
        return

    await safe_reply(update.message, "❌ App-server transport is unavailable.")


async def queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _sync_bot_globals()
    """Queue a message to run after the current in-progress response completes."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not await _ensure_chat_allowed(update):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        await safe_reply(
            update.message,
            "❌ Please use a named topic. Create a new topic to start a session.",
        )
        return

    chat_id = _scoped_chat_id(update)
    if chat_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat_id)

    queued_text = _extract_command_args(update.message.text or "")
    if not queued_text:
        await safe_reply(update.message, "Usage: `/q <message>`")
        emit_telemetry(
            "queue.q_command.invalid_usage",
            user_id=user.id,
            thread_id=thread_id,
            text_len=0,
        )
        return

    wid = session_manager.resolve_window_for_thread(
        user.id,
        thread_id,
        chat_id=chat_id,
    )
    if not wid:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        emit_telemetry(
            "queue.q_command.rejected_unbound",
            user_id=user.id,
            thread_id=thread_id,
            text_len=len(queued_text),
        )
        return

    binding = session_manager.resolve_topic_binding(
        user.id,
        thread_id,
        chat_id=chat_id,
    )
    if binding is None or (not binding.codex_thread_id and not binding.cwd):
        await safe_reply(
            update.message,
            "❌ Session binding is incomplete. Send a normal message to reinitialize.",
        )
        emit_telemetry(
            "queue.q_command.rejected_incomplete_binding",
            user_id=user.id,
            thread_id=thread_id,
            window_id=wid,
            text_len=len(queued_text),
        )
        return

    if await _is_window_in_progress(user.id, thread_id, wid):
        existing_internal = queued_topic_input_count(user.id, thread_id)
        qsize = enqueue_queued_topic_input(
            user.id,
            thread_id,
            queued_text,
            update.message.chat_id,
            update.message.message_id,
        )
        await _set_hourglass_reaction(update.message)
        await sync_queued_topic_dock(
            context.bot,
            user.id,
            thread_id,
            window_id=wid,
        )
        emit_telemetry(
            "queue.q_internal_enqueued",
            user_id=user.id,
            thread_id=thread_id,
            window_id=wid,
            queue_size=qsize,
            used_native_queue=False,
            native_attempts=0,
            native_error="",
            text_len=len(queued_text),
        )
        logger.info(
            "Queued /q input (user=%d, thread=%d, window=%s, size=%d)",
            user.id,
            thread_id,
            wid,
            qsize,
        )
        return

    success, send_msg = await session_manager.send_topic_text_to_window(
        user_id=user.id,
        thread_id=thread_id,
        chat_id=chat_id,
        window_id=wid,
        text=queued_text,
    )
    emit_telemetry(
        "queue.q_immediate_send_result",
        user_id=user.id,
        thread_id=thread_id,
        window_id=wid,
        success=success,
        error=send_msg,
        text_len=len(queued_text),
    )
    if not success:
        await safe_reply(update.message, f"❌ {send_msg}")
        return
    note_run_started(
        user_id=user.id,
        thread_id=thread_id,
        window_id=wid,
        source="q_immediate",
        pending_text=queued_text,
        expect_response=True,
    )
    await sync_queued_topic_dock(
        context.bot,
        user.id,
        thread_id,
        window_id=wid,
    )
    await _set_eyes_reaction(update.message)


async def approvals_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _sync_bot_globals()
    """Show/change approval policy for the current bound session."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not await _ensure_chat_allowed(update):
        return
    if not update.message:
        return
    if not _is_admin_user(user.id):
        await safe_reply(update.message, "❌ Only admins can change session approvals.")
        return
    thread_id = _get_thread_id(update)
    if thread_id is None:
        await safe_reply(
            update.message,
            "❌ Use `/approvals` inside a named topic bound to a session.",
        )
        return

    chat_id = _scoped_chat_id(update)
    if chat_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat_id)

    wid = session_manager.resolve_window_for_thread(
        user.id,
        thread_id,
        chat_id=chat_id,
    )
    if not wid:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    workspace_dir = _resolve_workspace_dir_for_window(
        user_id=user.id,
        thread_id=thread_id,
        window_id=wid,
    )

    await safe_reply(
        update.message,
        _build_approvals_text(
            user.id,
            wid,
            workspace_dir=workspace_dir,
            defaults_view=False,
        ),
        reply_markup=_build_approvals_keyboard(
            wid,
            defaults_view=False,
            can_use_dangerous=_is_admin_user(user.id),
        ),
    )


async def mentions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _sync_bot_globals()
    """Show/change mention-only invocation mode for the current bound session."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not await _ensure_chat_allowed(update):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        await safe_reply(update.message, "❌ Use `/mentions` inside a named topic.")
        return

    chat_id = _scoped_chat_id(update)
    if chat_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat_id)

    wid = session_manager.resolve_window_for_thread(
        user.id,
        thread_id,
        chat_id=chat_id,
    )
    if not wid:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    current_mode = session_manager.get_window_mention_only(wid)
    bot_username = _resolve_bot_username(context)
    mention_example = f"@{bot_username}" if bot_username else "@bot_username"
    raw_args = _extract_command_args(update.message.text or "").strip()
    usage = "Usage: `/mentions` or `/mentions on|off|toggle`"

    if not raw_args:
        state_label = "ON" if current_mode else "OFF"
        if current_mode:
            detail = (
                f"Only messages that mention the bot (for example `{mention_example}`) invoke this session."
            )
        else:
            detail = "Any text message in this topic can invoke this session."
        await safe_reply(
            update.message,
            f"Mention-only mode: `{state_label}`\n{detail}\n\n{usage}",
        )
        return

    action = raw_args.split(maxsplit=1)[0].strip().lower()
    if action in {"on", "mention", "mentions", "mention-only", "mentions-only", "enable"}:
        desired = True
    elif action in {"off", "any", "all", "disable"}:
        desired = False
    elif action in {"toggle", "flip"}:
        desired = not current_mode
    else:
        await safe_reply(update.message, f"❌ Unknown mode `{action}`.\n{usage}")
        return

    session_manager.set_window_mention_only(wid, desired)
    state_label = "ON" if desired else "OFF"
    if desired:
        detail = (
            f"Only messages containing a bot mention (for example `{mention_example}`) "
            "invoke this session. Slash commands still work."
        )
    else:
        detail = "Any text message in this topic can invoke this session."
    await safe_reply(update.message, f"✅ Mention-only mode is now `{state_label}`.\n{detail}")


async def allowed_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _sync_bot_globals()
    """Manage allowlist requests and token approvals."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not await _ensure_chat_allowed(update):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    chat_id = _scoped_chat_id(update)
    if chat_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat_id)

    usage_text = (
        "Usage:\n"
        "`/allowed`\n"
        "`/allowed request_add <user_id> [display_name]`\n"
        "`/allowed request_remove <user_id>`\n"
        "`/allowed approve <token>`"
    )

    raw_args = _extract_command_args(update.message.text or "")
    if not raw_args:
        _clear_allowed_flow_state(context.user_data)
        await safe_reply(
            update.message,
            _build_allowed_overview_text(user.id),
            reply_markup=_build_allowed_overview_keyboard(user.id),
        )
        return

    parts = raw_args.split(maxsplit=2)
    subcmd = parts[0].strip().lower()

    if subcmd in {"help", "h", "?"}:
        await safe_reply(update.message, usage_text)
        return

    if subcmd in {"approve", "accept"}:
        if not _is_admin_user(user.id):
            await safe_reply(update.message, "❌ Only admins can approve allowlist requests.")
            return
        token = parts[1].strip() if len(parts) >= 2 else ""
        if not token:
            await safe_reply(update.message, "Usage: `/allowed approve <token>`")
            return
        ok, message = _apply_allowed_auth_request_token(
            token,
            acting_user_id=user.id,
        )
        if not ok:
            await safe_reply(update.message, f"❌ {message}")
            return
        await safe_reply(update.message, f"✅ {message}")
        await safe_reply(
            update.message,
            _build_allowed_overview_text(user.id),
            reply_markup=_build_allowed_overview_keyboard(user.id),
        )
        return

    if subcmd in {"request_add", "request-add", "add", "reqadd"}:
        if not _is_admin_user(user.id):
            await safe_reply(update.message, "❌ Only admins can request allowlist changes.")
            return
        if len(parts) < 2:
            await safe_reply(update.message, "Usage: `/allowed request_add <user_id> [display_name]`")
            return
        try:
            target_user_id = int(parts[1].strip())
        except ValueError:
            await safe_reply(update.message, "❌ User ID must be numeric.")
            return
        name = parts[2].strip() if len(parts) >= 3 else ""

        scope = SCOPE_CREATE_SESSIONS
        bind_thread_id: int | None = None
        bind_window_id: str | None = None
        bind_chat_id: int | None = None
        if thread_id is not None:
            wid = session_manager.resolve_window_for_thread(
                user.id,
                thread_id,
                chat_id=chat_id,
            )
            if wid:
                scope = SCOPE_SINGLE_SESSION
                bind_thread_id = thread_id
                bind_window_id = wid
                bind_chat_id = chat_id

        ok, err, request = _queue_allowed_add_request(
            requested_by=user.id,
            new_user_id=target_user_id,
            name=name,
            scope=scope,
            bind_thread_id=bind_thread_id,
            bind_window_id=bind_window_id,
            bind_chat_id=bind_chat_id,
        )
        if not ok or request is None:
            await safe_reply(update.message, f"❌ {err or 'Failed to queue add request.'}")
            return

        delivered, total = await _notify_allowed_auth_token(
            bot=context.bot,
            request=request,
        )
        await safe_reply(
            update.message,
            "✅ Pending add request created.\n"
            f"Target user: `{target_user_id}`\n"
            f"Scope: {_format_scope_label(scope)}\n"
            f"Approval token sent to `{delivered}/{total}` super admins.\n"
            "Paste `/allowed approve <token>` in this group/topic to apply.",
        )
        return

    if subcmd in {"request_remove", "request-remove", "remove", "reqrm", "reqremove"}:
        if not _is_admin_user(user.id):
            await safe_reply(update.message, "❌ Only admins can request allowlist changes.")
            return
        if len(parts) < 2:
            await safe_reply(update.message, "Usage: `/allowed request_remove <user_id>`")
            return
        try:
            target_user_id = int(parts[1].strip())
        except ValueError:
            await safe_reply(update.message, "❌ User ID must be numeric.")
            return

        ok, err, request = _queue_allowed_remove_request(
            requested_by=user.id,
            target_user_id=target_user_id,
        )
        if not ok or request is None:
            await safe_reply(update.message, f"❌ {err or 'Failed to queue remove request.'}")
            return

        delivered, total = await _notify_allowed_auth_token(
            bot=context.bot,
            request=request,
        )
        await safe_reply(
            update.message,
            "✅ Pending remove request created.\n"
            f"Target user: `{target_user_id}`\n"
            f"Approval token sent to `{delivered}/{total}` super admins.\n"
            "Paste `/allowed approve <token>` in this group/topic to apply.",
        )
        return

    await safe_reply(update.message, f"❌ Unknown /allowed action: `{subcmd}`\n\n{usage_text}")


async def skills_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _sync_bot_globals()
    """Manage per-topic enabled Codex skills (from ~/.codex/skills by default)."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not await _ensure_chat_allowed(update):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        await safe_reply(
            update.message,
            "❌ Use `/skills` inside a named topic.",
        )
        return

    chat_id = _scoped_chat_id(update)
    if chat_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat_id)

    catalog = session_manager.discover_codex_skill_catalog()
    resolved_enabled = session_manager.resolve_thread_codex_skills(
        user.id,
        thread_id,
        chat_id=chat_id,
        catalog=catalog,
    )
    enabled_names = [skill.name for skill in resolved_enabled]

    raw_args = _extract_command_args(update.message.text or "")
    if not raw_args:
        await safe_reply(
            update.message,
            _build_skills_overview_text(
                title="Codex Skills",
                noun_plural="skills",
                command_name="skills",
                enabled_skill_names=enabled_names,
                catalog=catalog,
                roots=config.codex_skills_paths,
            ),
        )
        return

    parts = raw_args.split()
    subcmd = parts[0].strip().lower()
    subargs = parts[1:]

    if subcmd in {"list", "ls"}:
        await safe_reply(
            update.message,
            _build_skills_overview_text(
                title="Codex Skills",
                noun_plural="skills",
                command_name="skills",
                enabled_skill_names=enabled_names,
                catalog=catalog,
                roots=config.codex_skills_paths,
            ),
        )
        return

    if subcmd in {"clear", "reset"}:
        session_manager.clear_thread_codex_skills(
            user.id,
            thread_id,
            chat_id=chat_id,
        )
        await safe_reply(update.message, "✅ Cleared Codex skills for this topic.")
        await safe_reply(
            update.message,
            _build_skills_overview_text(
                title="Codex Skills",
                noun_plural="skills",
                command_name="skills",
                enabled_skill_names=[],
                catalog=catalog,
                roots=config.codex_skills_paths,
            ),
        )
        return

    if subcmd in {"enable", "on", "use", "add"}:
        if not subargs:
            await safe_reply(update.message, "Usage: `/skills enable <name>`")
            return
        identifier = " ".join(subargs).strip()
        canonical = resolve_skill_identifier(identifier, catalog)
        if not canonical:
            await safe_reply(
                update.message,
                f"❌ Unknown skill: `{identifier}`",
            )
            return
        if canonical not in enabled_names:
            session_manager.set_thread_codex_skills(
                user.id,
                thread_id,
                [*enabled_names, canonical],
                chat_id=chat_id,
            )
            enabled_names.append(canonical)
        await safe_reply(
            update.message, f"✅ Enabled Codex skill `{canonical}` for this topic."
        )
        await safe_reply(
            update.message,
            _build_skills_overview_text(
                title="Codex Skills",
                noun_plural="skills",
                command_name="skills",
                enabled_skill_names=enabled_names,
                catalog=catalog,
                roots=config.codex_skills_paths,
            ),
        )
        return

    if subcmd in {"disable", "off", "remove", "rm"}:
        if not subargs:
            await safe_reply(update.message, "Usage: `/skills disable <name>`")
            return
        identifier = " ".join(subargs).strip()
        canonical = resolve_skill_identifier(identifier, catalog)
        if not canonical:
            await safe_reply(
                update.message,
                f"❌ Unknown skill: `{identifier}`",
            )
            return
        if canonical in enabled_names:
            enabled_names = [name for name in enabled_names if name != canonical]
            session_manager.set_thread_codex_skills(
                user.id,
                thread_id,
                enabled_names,
                chat_id=chat_id,
            )
            await safe_reply(
                update.message, f"✅ Disabled Codex skill `{canonical}` for this topic."
            )
        else:
            await safe_reply(update.message, f"ℹ️ Skill `{canonical}` was not enabled.")
        await safe_reply(
            update.message,
            _build_skills_overview_text(
                title="Codex Skills",
                noun_plural="skills",
                command_name="skills",
                enabled_skill_names=enabled_names,
                catalog=catalog,
                roots=config.codex_skills_paths,
            ),
        )
        return

    await safe_reply(
        update.message,
        "Unknown subcommand.\n"
        "Use `/skills`, `/skills enable <name>`, `/skills disable <name>`, or `/skills clear`.",
    )


async def apps_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _sync_bot_globals()
    """Manage per-topic enabled CoCo apps (local SKILL.md folders)."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not await _ensure_chat_allowed(update):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        await safe_reply(
            update.message,
            "❌ Use `/apps` inside a named topic.",
        )
        return

    chat_id = _scoped_chat_id(update)
    if chat_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat_id)

    _clear_apps_flow_state(context.user_data)

    catalog = session_manager.discover_skill_catalog()
    resolved_enabled = session_manager.resolve_thread_skills(
        user.id,
        thread_id,
        chat_id=chat_id,
        catalog=catalog,
    )
    enabled_names = [skill.name for skill in resolved_enabled]

    raw_args = _extract_command_args(update.message.text or "")
    if not raw_args:
        text, keyboard = _build_apps_overview_payload(
            enabled_names=enabled_names,
            catalog=catalog,
        )
        await safe_reply(
            update.message,
            text,
            reply_markup=keyboard,
        )
        return

    parts = raw_args.split()
    subcmd = parts[0].strip().lower()
    subargs = parts[1:]

    if subcmd in {"list", "ls"}:
        text, keyboard = _build_apps_overview_payload(
            enabled_names=enabled_names,
            catalog=catalog,
        )
        await safe_reply(
            update.message,
            text,
            reply_markup=keyboard,
        )
        return

    if subcmd in {"clear", "reset"}:
        session_manager.clear_thread_skills(user.id, thread_id, chat_id=chat_id)
        await safe_reply(update.message, "✅ Cleared apps for this topic.")
        text, keyboard = _build_apps_overview_payload(
            enabled_names=[],
            catalog=catalog,
        )
        await safe_reply(
            update.message,
            text,
            reply_markup=keyboard,
        )
        return

    if subcmd in {"enable", "on", "use", "add"}:
        if not subargs:
            await safe_reply(update.message, "Usage: `/apps enable <name>`")
            return
        identifier = " ".join(subargs).strip()
        canonical = resolve_skill_identifier(identifier, catalog)
        if not canonical:
            await safe_reply(
                update.message,
                f"❌ Unknown app: `{identifier}`",
            )
            return
        if canonical not in enabled_names:
            session_manager.set_thread_skills(
                user.id,
                thread_id,
                [*enabled_names, canonical],
                chat_id=chat_id,
            )
            enabled_names.append(canonical)
        await safe_reply(update.message, f"✅ Enabled app `{canonical}` for this topic.")
        text, keyboard = _build_apps_overview_payload(
            enabled_names=enabled_names,
            catalog=catalog,
        )
        await safe_reply(
            update.message,
            text,
            reply_markup=keyboard,
        )
        return

    if subcmd in {"disable", "off", "remove", "rm"}:
        if not subargs:
            await safe_reply(update.message, "Usage: `/apps disable <name>`")
            return
        identifier = " ".join(subargs).strip()
        canonical = resolve_skill_identifier(identifier, catalog)
        if not canonical:
            await safe_reply(
                update.message,
                f"❌ Unknown app: `{identifier}`",
            )
            return
        if canonical in enabled_names:
            enabled_names = [name for name in enabled_names if name != canonical]
            session_manager.set_thread_skills(
                user.id,
                thread_id,
                enabled_names,
                chat_id=chat_id,
            )
            await safe_reply(update.message, f"✅ Disabled app `{canonical}` for this topic.")
        else:
            await safe_reply(update.message, f"ℹ️ App `{canonical}` was not enabled.")
        text, keyboard = _build_apps_overview_payload(
            enabled_names=enabled_names,
            catalog=catalog,
        )
        await safe_reply(
            update.message,
            text,
            reply_markup=keyboard,
        )
        return

    await safe_reply(
        update.message,
        "Unknown subcommand.\n"
        "Use `/apps`, `/apps enable <name>`, `/apps disable <name>`, or `/apps clear`.",
    )


async def looper_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _sync_bot_globals()
    """Manage recurring plan nudges for one topic."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not await _ensure_chat_allowed(update):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        await safe_reply(
            update.message,
            "❌ Use `/looper` inside a named topic.",
        )
        return

    chat_id = _scoped_chat_id(update)
    if chat_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat_id)

    wid = session_manager.resolve_window_for_thread(
        user.id,
        thread_id,
        chat_id=chat_id,
    )
    if not wid:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    raw_args = _extract_command_args(update.message.text or "")
    if not raw_args:
        state = get_looper_state(user_id=user.id, thread_id=thread_id)
        await safe_reply(
            update.message,
            _build_looper_overview_text(state=state),
        )
        return

    try:
        parts = shlex.split(raw_args)
    except ValueError as e:
        await safe_reply(update.message, f"❌ Invalid command arguments: {e}")
        return
    if not parts:
        state = get_looper_state(user_id=user.id, thread_id=thread_id)
        await safe_reply(update.message, _build_looper_overview_text(state=state))
        return

    subcmd = parts[0].strip().lower()
    subargs = parts[1:]

    if subcmd in {"status", "list", "show"}:
        state = get_looper_state(user_id=user.id, thread_id=thread_id)
        await safe_reply(update.message, _build_looper_overview_text(state=state))
        return

    if subcmd in {"stop", "off", "clear"}:
        stopped = stop_looper(user_id=user.id, thread_id=thread_id, reason="manual_stop")
        if not stopped:
            await safe_reply(update.message, "ℹ️ Looper is already off for this topic.")
            return
        await safe_reply(
            update.message,
            (
                "🛑 Looper stopped.\n"
                f"Plan: `{stopped.plan_path}`\n"
                f"Nudges sent: `{stopped.prompt_count}`"
            ),
        )
        return

    if subcmd not in {"start", "on", "run"}:
        await safe_reply(
            update.message,
            "Unknown subcommand.\n"
            "Use `/looper start <plan.md> <keyword>`, `/looper status`, or `/looper stop`.",
        )
        return

    if len(subargs) < 2:
        await safe_reply(
            update.message,
            (
                "Usage: `/looper start <plan.md> <keyword> "
                "[--every 10m] [--limit 1h] [--instructions \"...\"]`"
            ),
        )
        return

    plan_path = subargs[0].strip()
    keyword = subargs[1].strip()

    interval_seconds = LOOPER_DEFAULT_INTERVAL_SECONDS
    limit_seconds = 0
    instructions = ""
    idx = 2
    while idx < len(subargs):
        token = subargs[idx]
        token_l = token.lower()

        if token_l in {"--every", "--interval"}:
            idx += 1
            if idx >= len(subargs):
                await safe_reply(update.message, "❌ Missing value for `--every`.")
                return
            every_raw = subargs[idx]
            parsed = _parse_duration_to_seconds(every_raw, default_unit="m")
            if (
                parsed is not None
                and every_raw.isdigit()
                and idx + 1 < len(subargs)
                and _is_duration_unit_token(subargs[idx + 1])
            ):
                combined = f"{every_raw} {subargs[idx + 1]}"
                parsed_combined = _parse_duration_to_seconds(combined, default_unit="m")
                if parsed_combined is not None:
                    parsed = parsed_combined
                    idx += 1
            if parsed is None:
                await safe_reply(update.message, f"❌ Invalid interval: `{subargs[idx]}`")
                return
            interval_seconds = parsed
            idx += 1
            continue

        if token_l.startswith("--every=") or token_l.startswith("--interval="):
            _flag, _sep, value = token.partition("=")
            parsed = _parse_duration_to_seconds(value, default_unit="m")
            if parsed is None:
                await safe_reply(update.message, f"❌ Invalid interval: `{value}`")
                return
            interval_seconds = parsed
            idx += 1
            continue

        if token_l in {"--limit", "--time-limit", "--ttl"}:
            idx += 1
            if idx >= len(subargs):
                await safe_reply(update.message, "❌ Missing value for `--limit`.")
                return
            limit_raw = subargs[idx]
            parsed = _parse_duration_to_seconds(limit_raw, default_unit="h")
            if (
                parsed is not None
                and limit_raw.isdigit()
                and idx + 1 < len(subargs)
                and _is_duration_unit_token(subargs[idx + 1])
            ):
                combined = f"{limit_raw} {subargs[idx + 1]}"
                parsed_combined = _parse_duration_to_seconds(combined, default_unit="h")
                if parsed_combined is not None:
                    parsed = parsed_combined
                    idx += 1
            if parsed is None:
                await safe_reply(update.message, f"❌ Invalid time limit: `{subargs[idx]}`")
                return
            limit_seconds = parsed
            idx += 1
            continue

        if (
            token_l.startswith("--limit=")
            or token_l.startswith("--time-limit=")
            or token_l.startswith("--ttl=")
        ):
            _flag, _sep, value = token.partition("=")
            parsed = _parse_duration_to_seconds(value, default_unit="h")
            if parsed is None:
                await safe_reply(update.message, f"❌ Invalid time limit: `{value}`")
                return
            limit_seconds = parsed
            idx += 1
            continue

        if token_l in {"--instructions", "--instruction", "--custom"}:
            idx += 1
            instructions = " ".join(subargs[idx:]).strip()
            break

        if token_l.startswith("--instructions=") or token_l.startswith("--custom="):
            _flag, _sep, value = token.partition("=")
            instructions = value.strip()
            idx += 1
            if idx < len(subargs):
                trailing = " ".join(subargs[idx:]).strip()
                if trailing:
                    instructions = f"{instructions} {trailing}".strip()
            break

        # Free-form tail without explicit flag is treated as custom instructions.
        instructions = " ".join(subargs[idx:]).strip()
        break

    if interval_seconds < LOOPER_MIN_INTERVAL_SECONDS:
        await safe_reply(
            update.message,
            (
                "❌ Interval is too short. "
                f"Minimum is `{_format_duration_brief(LOOPER_MIN_INTERVAL_SECONDS)}`."
            ),
        )
        return
    if interval_seconds > LOOPER_MAX_INTERVAL_SECONDS:
        await safe_reply(
            update.message,
            (
                "❌ Interval is too long. "
                f"Maximum is `{_format_duration_brief(LOOPER_MAX_INTERVAL_SECONDS)}`."
            ),
        )
        return

    try:
        state = start_looper(
            user_id=user.id,
            thread_id=thread_id,
            window_id=wid,
            plan_path=plan_path,
            keyword=keyword,
            interval_seconds=interval_seconds,
            limit_seconds=limit_seconds,
            instructions=instructions,
        )
    except ValueError as e:
        await safe_reply(update.message, f"❌ {e}")
        return

    # If the looper app exists locally, auto-enable it for this topic once.
    auto_enabled = False
    app_catalog = session_manager.discover_skill_catalog()
    if "looper" in app_catalog:
        enabled = [
            item.name
            for item in session_manager.resolve_thread_skills(
                user.id,
                thread_id,
                chat_id=chat_id,
                catalog=app_catalog,
            )
        ]
        if "looper" not in enabled:
            session_manager.set_thread_skills(
                user.id,
                thread_id,
                [*enabled, "looper"],
                chat_id=chat_id,
            )
            auto_enabled = True

    example_prompt = build_looper_prompt(
        plan_path=state.plan_path,
        keyword=state.keyword,
        instructions=state.instructions,
        deadline_at=state.deadline_at,
    )

    lines = [
        "✅ Looper started for this topic.",
        f"Plan file: `{state.plan_path}`",
        f"Completion keyword: `{state.keyword}`",
        f"Interval: `{_format_duration_brief(state.interval_seconds)}`",
    ]
    if state.deadline_at > 0:
        lines.append(
            f"Time limit: `{_format_duration_brief(int(state.deadline_at - state.started_at))}`"
        )
    else:
        lines.append("Time limit: `(none)`")
    lines.append(f"First nudge in: `{_format_duration_brief(state.interval_seconds)}`")
    if auto_enabled:
        lines.append("App auto-enabled: `looper`")
    lines.extend(
        [
            "",
            "Example nudge:",
            example_prompt,
        ]
    )
    await safe_reply(update.message, "\n".join(lines))


async def worktree_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _sync_bot_globals()
    """Manage git worktrees for the current topic/session."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not await _ensure_chat_allowed(update):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        await safe_reply(
            update.message,
            "❌ Use /worktree inside a named topic bound to a session.",
        )
        return

    chat_id = _scoped_chat_id(update)
    if chat_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat_id)

    raw_args = _extract_command_args(update.message.text or "")
    if not raw_args:
        _clear_worktree_flow_state(context.user_data)
        await _show_worktree_panel(
            update.message,
            user_id=user.id,
            thread_id=thread_id,
            chat_id=chat_id,
        )
        return

    parts = raw_args.split()
    subcmd = parts[0].lower()
    subargs = parts[1:]

    if subcmd in {"list", "ls"}:
        _clear_worktree_flow_state(context.user_data)
        await _show_worktree_panel(
            update.message,
            user_id=user.id,
            thread_id=thread_id,
            chat_id=chat_id,
        )
        return

    if subcmd in {"new", "create"}:
        if not _can_user_create_sessions(user.id):
            await safe_reply(
                update.message,
                "❌ You do not have permission to create worktrees/sessions.",
            )
            return

        wid = session_manager.resolve_window_for_thread(
            user.id,
            thread_id,
            chat_id=chat_id,
        )
        if not wid:
            await safe_reply(update.message, "❌ No session bound to this topic.")
            return

        if not subargs:
            if context.user_data is not None:
                _clear_worktree_flow_state(context.user_data)
                context.user_data[STATE_KEY] = STATE_WORKTREE_NEW_NAME
                context.user_data[WORKTREE_PENDING_THREAD_KEY] = thread_id
                context.user_data[WORKTREE_PENDING_WINDOW_ID_KEY] = wid
            await safe_reply(
                update.message,
                "Send the new worktree name.\nExample: `auth-fix`",
            )
            return

        worktree_name = " ".join(subargs).strip()
        if not worktree_name:
            await safe_reply(update.message, "Usage: `/worktree new <name>`")
            return

        ok, msg = await _create_worktree_from_topic(
            bot=context.bot,
            user_id=user.id,
            thread_id=thread_id,
            worktree_name=worktree_name,
            chat_id=chat_id,
        )
        if ok:
            await safe_reply(update.message, f"✅ {msg}")
        else:
            await safe_reply(update.message, f"❌ {msg}")
        return

    if subcmd == "fold":
        if not _can_user_create_sessions(user.id):
            await safe_reply(
                update.message,
                "❌ You do not have permission to fold worktrees.",
            )
            return
        if not subargs:
            await safe_reply(
                update.message,
                "Usage: `/worktree fold <worktree1> [worktree2 ...]`",
            )
            return
        wid = session_manager.resolve_window_for_thread(
            user.id,
            thread_id,
            chat_id=chat_id,
        )
        if not wid:
            await safe_reply(update.message, "❌ No session bound to this topic.")
            return
        workspace_dir, workspace_err = await _resolve_live_workspace_dir_for_window(
            user_id=user.id,
            thread_id=thread_id,
            window_id=wid,
        )
        if not workspace_dir:
            await safe_reply(
                update.message,
                f"❌ {workspace_err or 'No workspace bound to this topic.'}",
            )
            return

        ok, msg = await asyncio.to_thread(
            _fold_worktrees_into_branch,
            target_cwd=Path(workspace_dir),
            selectors=subargs,
        )
        if ok:
            await safe_reply(update.message, f"✅ {msg}")
        else:
            await safe_reply(update.message, f"❌ {msg}")
        return

    await safe_reply(
        update.message,
        "Unknown subcommand.\n"
        "Use `/worktree`, `/worktree new <name>`, or `/worktree fold <wt...>`.",
    )


async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _sync_bot_globals()
    """Restart the CoCo process."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not await _ensure_chat_allowed(update):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    chat_id = update.message.chat_id

    if bot._restart_requested:
        await safe_send(
            context.bot,
            chat_id,
            "Restart already in progress.",
            message_thread_id=thread_id,
        )
        return

    _set_restart_notice_target(chat_id, thread_id)
    bot._restart_requested = True
    await safe_send(
        context.bot,
        chat_id,
        _pick_restart_shutdown_message(),
        message_thread_id=thread_id,
    )
    asyncio.create_task(_restart_process_after_delay())


async def _show_resume_panel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    command_name: str,
) -> None:
    """Render session lifecycle panel in menu-only mode."""
    _sync_bot_globals()
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not await _ensure_chat_allowed(update):
        return
    if not update.message:
        return
    if not _codex_app_server_preferred():
        await safe_reply(
            update.message,
            f"❌ `/{command_name}` requires Codex app-server transport (`codex_transport=auto|app_server`).",
        )
        return

    thread_id = _get_thread_id(update)
    chat_id = _scoped_chat_id(update)
    if chat_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat_id)
    wid = session_manager.resolve_window_for_thread(
        user.id,
        thread_id,
        chat_id=chat_id,
    )
    if not wid:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    raw_args = _extract_command_args(update.message.text or "").strip()
    ok, text, keyboard = await _build_session_panel_payload(
        user_id=user.id,
        thread_id=thread_id,
        context_user_data=context.user_data,
        chat_id=chat_id,
    )
    if raw_args:
        text = (
            "ℹ️ Text subcommands are disabled. Use the buttons below to choose.\n\n"
            f"{text}"
        )
    await safe_reply(
        update.message,
        text,
        reply_markup=keyboard if ok else None,
    )


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Menu-only session lifecycle command."""
    await _show_resume_panel(update, context, command_name="resume")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _sync_bot_globals()
    """Fetch Codex /status panel and send to Telegram."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not await _ensure_chat_allowed(update):
        return
    if not update.message:
        return
    if _codex_app_server_preferred() or _codex_app_server_enabled():
        native_sent = await _show_app_server_status(
            update,
            allow_tui_fallback=False,
        )
        if native_sent:
            return
    await safe_reply(update.message, "❌ Failed to fetch status from app-server.")


async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _sync_bot_globals()
    """Show per-topic Codex model options and reasoning levels."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not await _ensure_chat_allowed(update):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    chat_id = _group_chat_id(update.effective_chat)
    if thread_id is None:
        await safe_reply(update.message, "❌ Use `/model` inside a named topic.")
        return
    session_manager.ensure_topic_binding(user.id, thread_id, chat_id=chat_id)
    catalog = _resolve_topic_model_catalog(
        user_id=user.id,
        thread_id=thread_id,
        chat_id=chat_id,
    )
    await safe_reply(
        update.message,
        _build_model_info_text(catalog),
        reply_markup=_build_model_keyboard(catalog),
    )


async def update_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _sync_bot_globals()
    """Show Codex update panel and optionally run upgrade + restart."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not await _ensure_chat_allowed(update):
        return
    if not update.message:
        return

    raw_args = _extract_command_args(update.message.text or "").strip().lower()
    can_trigger_upgrade = _is_admin_user(user.id)
    if not raw_args or raw_args in {"status", "check", "panel"}:
        text, keyboard = await _build_update_panel_payload(
            can_trigger_upgrade=can_trigger_upgrade,
        )
        await safe_reply(update.message, text, reply_markup=keyboard)
        return

    if raw_args in {"run", "upgrade"}:
        if not can_trigger_upgrade:
            await safe_reply(update.message, "❌ Only admins can run updates.")
            return
        chat_id = update.message.chat_id
        thread_id = _get_thread_id(update)
        ok, result_text = await _run_codex_upgrade_and_restart(
            chat_id=chat_id,
            thread_id=thread_id,
        )
        await safe_reply(update.message, result_text)
        return

    await safe_reply(
        update.message,
        "Usage: `/update`, `/update status`, or `/update run`.",
    )
