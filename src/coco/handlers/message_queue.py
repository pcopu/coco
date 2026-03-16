"""Per-user message queue management for ordered message delivery.

Provides a queue-based message processing system that ensures:
  - Messages are sent in receive order (FIFO)
  - Status messages always follow content messages
  - Consecutive content messages can be merged for efficiency
  - Thread-aware sending: each MessageTask carries an optional thread_id
    for Telegram topic support

Rate limiting is handled globally by AIORateLimiter on the Application.

Key components:
  - MessageTask: Dataclass representing a queued message task (with thread_id)
  - get_or_create_queue: Get or create queue and worker for a user
  - Message queue worker: Background task processing user's queue
  - Content task processing with tool_use/tool_result handling
  - Status message tracking and conversion (keyed by (user_id, thread_id))
  - In-progress message streaming (single editable process message per topic)
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Literal

from telegram import Bot
from telegram.constants import ChatAction
from telegram.error import RetryAfter

from ..markdown_v2 import convert_markdown
from ..session import session_manager
from ..telegram_memory import log_outgoing_edit
from ..transcript_parser import TranscriptParser
from .message_sender import (
    NO_LINK_PREVIEW,
    send_documents,
    send_photo,
    send_with_fallback,
)

logger = logging.getLogger(__name__)

# Merge limit for content messages
MERGE_MAX_LENGTH = 3800  # Leave room for markdown conversion overhead


@dataclass
class MessageTask:
    """Message task for queue processing."""

    task_type: Literal[
        "content",
        "status_update",
        "status_clear",
        "progress_start",
        "progress_update",
        "progress_clear",
        "progress_finalize",
    ]
    text: str | None = None
    window_id: str | None = None
    # content type fields
    parts: list[str] = field(default_factory=list)
    tool_use_id: str | None = None
    content_type: str = "text"
    thread_id: int | None = None  # Telegram topic thread_id for targeted send
    image_data: list[tuple[str, bytes]] | None = None  # From tool_result images
    document_data: list[tuple[str, bytes]] | None = None  # Explicit Telegram docs
    # For progress_finalize tasks: "full" keeps accumulated body, "compact" keeps marker only.
    finalize_mode: str = "full"


# Per-user message queues and worker tasks
_message_queues: dict[int, asyncio.Queue[MessageTask]] = {}
_queue_workers: dict[int, asyncio.Task[None]] = {}
_queue_locks: dict[int, asyncio.Lock] = {}  # Protect drain/refill operations

# Map (tool_use_id, user_id, thread_id_or_0) -> telegram message_id
# for editing tool_use messages with results
_tool_msg_ids: dict[tuple[str, int, int], int] = {}

# Status message tracking: (user_id, thread_id_or_0) -> (message_id, window_id, last_text)
_status_msg_info: dict[tuple[int, int], tuple[int, str, str]] = {}

# Progress message tracking: (user_id, thread_id_or_0) -> (message_id, window_id, accumulated_text)
_progress_msg_info: dict[tuple[int, int], tuple[int, str, str]] = {}
# Progress text cache used for completion-time fallbacks.
# Unlike _progress_msg_info, this is updated when progress is enqueued (not only
# after Telegram edits succeed). This avoids "turn completed" races where queued
# edits have not yet run but we still need the best-known accumulated text.
_progress_text_cache: dict[tuple[int, int], tuple[str, str]] = {}

# Queued user inputs for /q:
# (user_id, thread_id_or_0) -> [(text, source_chat_id, source_message_id), ...]
_queued_topic_inputs: dict[tuple[int, int], list[tuple[str, int, int]]] = {}
# Queue dock tracking: (user_id, thread_id_or_0) -> (message_id, last_text)
_queue_dock_msg_info: dict[tuple[int, int], tuple[int, str]] = {}

# Flood control: user_id -> monotonic time when ban expires
_flood_until: dict[int, float] = {}

# Max seconds to wait for flood control before dropping tasks
FLOOD_CONTROL_MAX_WAIT = 10

# Max chars retained in the in-progress message
PROGRESS_MAX_LENGTH = 3600

# Marker appended when a process message is finalized.
PROGRESS_COMPLETE_MARKER = "✅ Complete"
# Max chars shown inside the live working message body.
PROGRESS_PREVIEW_MAX_LENGTH = 900
QUEUE_DOCK_PREVIEW_LIMIT = 120
QUEUE_DOCK_MAX_VISIBLE_ITEMS = 6


def get_message_queue(user_id: int) -> asyncio.Queue[MessageTask] | None:
    """Get the message queue for a user (if exists)."""
    return _message_queues.get(user_id)


def _topic_key(user_id: int, thread_id: int | None) -> tuple[int, int]:
    """Normalize per-topic key used by queue-related maps."""
    return user_id, thread_id or 0


def _cache_progress_text(
    *,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    chunk: str,
) -> None:
    """Update completion-time progress cache for a topic."""
    skey = _topic_key(user_id, thread_id)
    cached = _progress_text_cache.get(skey)
    cached_wid = cached[0] if cached else ""
    cached_text = cached[1] if cached else ""
    if cached and cached_wid != window_id:
        cached_text = ""
    _progress_text_cache[skey] = (window_id, _merge_progress_text(cached_text, chunk))


def _clear_progress_text_cache(user_id: int, thread_id: int | None = None) -> None:
    """Clear completion-time progress cache for a topic."""
    skey = _topic_key(user_id, thread_id)
    _progress_text_cache.pop(skey, None)


def _strip_sentinels(text: str) -> str:
    """Strip transcript sentinels for plain-text fallback edits."""
    return text.replace(TranscriptParser.EXPANDABLE_QUOTE_START, "").replace(
        TranscriptParser.EXPANDABLE_QUOTE_END, ""
    )


def _queue_item_preview(text: str, *, limit: int = QUEUE_DOCK_PREVIEW_LIMIT) -> str:
    """Return compact single-line preview for queue dock display."""
    compact = " ".join(text.strip().split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"


def _build_queue_dock_text(
    pending_items: list[tuple[str, int, int]],
    *,
    window_id: str | None = None,
) -> str:
    """Render the per-topic queued /q dock message."""
    count = len(pending_items)
    heading = "⏳ Queue" if count <= 1 else f"⏳ Queue ({count})"
    lines = [heading]

    for idx, (text, _chat_id, _message_id) in enumerate(
        pending_items[:QUEUE_DOCK_MAX_VISIBLE_ITEMS],
        start=1,
    ):
        lines.append(f"{idx}. {_queue_item_preview(text)}")

    remaining = count - QUEUE_DOCK_MAX_VISIBLE_ITEMS
    if remaining > 0:
        lines.append(f"... +{remaining} more")
    return "\n".join(lines)


async def _edit_queue_dock_message(
    bot: Bot,
    *,
    chat_id: int,
    thread_id: int | None,
    message_id: int,
    text: str,
) -> bool:
    """Edit one queue dock message with markdown fallback."""
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=convert_markdown(text),
            parse_mode="MarkdownV2",
            link_preview_options=NO_LINK_PREVIEW,
        )
        log_outgoing_edit(
            text=text,
            chat_id=chat_id,
            thread_id=thread_id,
            message_id=message_id,
            source="message_queue.queue_dock",
        )
        return True
    except RetryAfter as e:
        logger.debug("RetryAfter while editing queue dock message %s: %s", message_id, e)
        return False
    except Exception:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=_strip_sentinels(text),
                link_preview_options=NO_LINK_PREVIEW,
            )
            log_outgoing_edit(
                text=text,
                chat_id=chat_id,
                thread_id=thread_id,
                message_id=message_id,
                source="message_queue.queue_dock",
            )
            return True
        except RetryAfter as e:
            logger.debug("RetryAfter while editing queue dock fallback %s: %s", message_id, e)
            return False
        except Exception as e:
            logger.debug("Failed to edit queue dock message %s: %s", message_id, e)
            return False


async def sync_queued_topic_dock(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    *,
    window_id: str | None = None,
) -> None:
    """Sync per-topic queued /q dock message to current internal queue state."""
    skey = _topic_key(user_id, thread_id)
    pending_items = list(_queued_topic_inputs.get(skey, []))
    current_info = _queue_dock_msg_info.get(skey)
    chat_id = session_manager.resolve_chat_id(user_id, thread_id)

    if not pending_items:
        if current_info:
            msg_id, _old_text = current_info
            _queue_dock_msg_info.pop(skey, None)
            try:
                await bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass
        return

    dock_text = _build_queue_dock_text(pending_items, window_id=window_id)

    if current_info:
        msg_id, old_text = current_info
        if old_text == dock_text:
            return
        _queue_dock_msg_info.pop(skey, None)
        try:
            sent = await send_with_fallback(
                bot,
                chat_id,
                dock_text,
                **_send_kwargs(thread_id),
            )
        except RetryAfter as e:
            logger.debug(
                "RetryAfter while replacing queue dock (user=%d thread=%s): %s",
                user_id,
                thread_id,
                e,
            )
            _queue_dock_msg_info[skey] = (msg_id, old_text)
            return
        if sent:
            _queue_dock_msg_info[skey] = (sent.message_id, dock_text)
            try:
                await bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass
            return
        _queue_dock_msg_info[skey] = (msg_id, old_text)
        return

    try:
        sent = await send_with_fallback(
            bot,
            chat_id,
            dock_text,
            **_send_kwargs(thread_id),
        )
    except RetryAfter as e:
        logger.debug("RetryAfter while sending queue dock (user=%d thread=%s): %s", user_id, thread_id, e)
        return
    if sent:
        _queue_dock_msg_info[skey] = (sent.message_id, dock_text)


async def clear_queued_topic_dock(
    bot: Bot | None,
    user_id: int,
    thread_id: int | None = None,
) -> None:
    """Delete the queue dock message for a topic (best effort)."""
    skey = _topic_key(user_id, thread_id)
    info = _queue_dock_msg_info.pop(skey, None)
    if not info or bot is None:
        return
    msg_id, _old_text = info
    chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except Exception:
        pass


def get_or_create_queue(bot: Bot, user_id: int) -> asyncio.Queue[MessageTask]:
    """Get or create message queue and worker for a user."""
    if user_id not in _message_queues:
        _message_queues[user_id] = asyncio.Queue()
        _queue_locks[user_id] = asyncio.Lock()
        # Start worker task for this user
        _queue_workers[user_id] = asyncio.create_task(
            _message_queue_worker(bot, user_id)
        )
    return _message_queues[user_id]


def _inspect_queue(queue: asyncio.Queue[MessageTask]) -> list[MessageTask]:
    """Non-destructively inspect all items in queue.

    Drains the queue and returns all items. Caller must refill.
    """
    items: list[MessageTask] = []
    while not queue.empty():
        try:
            item = queue.get_nowait()
            items.append(item)
        except asyncio.QueueEmpty:
            break
    return items


def _can_merge_tasks(base: MessageTask, candidate: MessageTask) -> bool:
    """Check if two content tasks can be merged."""
    if base.window_id != candidate.window_id:
        return False
    if candidate.task_type != "content":
        return False
    # tool_use/tool_result break merge chain
    # - tool_use: will be edited later by tool_result
    # - tool_result: edits previous message, merging would cause order issues
    if base.content_type in ("tool_use", "tool_result"):
        return False
    if candidate.content_type in ("tool_use", "tool_result"):
        return False
    if base.image_data or candidate.image_data:
        return False
    if base.document_data or candidate.document_data:
        return False
    return True


async def _merge_content_tasks(
    queue: asyncio.Queue[MessageTask],
    first: MessageTask,
    lock: asyncio.Lock,
) -> tuple[MessageTask, int]:
    """Merge consecutive content tasks from queue.

    Returns: (merged_task, merge_count) where merge_count is the number of
    additional tasks merged (0 if no merging occurred).

    Note on queue counter management:
        When we put items back, we call task_done() to compensate for the
        internal counter increment caused by put_nowait(). This is necessary
        because the items were already counted when originally enqueued.
        Without this compensation, queue.join() would wait indefinitely.
    """
    merged_parts = list(first.parts)
    current_length = sum(len(p) for p in merged_parts)
    merge_count = 0

    async with lock:
        items = _inspect_queue(queue)
        remaining: list[MessageTask] = []

        for i, task in enumerate(items):
            if not _can_merge_tasks(first, task):
                # Can't merge, keep this and all remaining items
                remaining = items[i:]
                break

            # Check length before merging
            task_length = sum(len(p) for p in task.parts)
            if current_length + task_length > MERGE_MAX_LENGTH:
                # Too long, stop merging
                remaining = items[i:]
                break

            merged_parts.extend(task.parts)
            current_length += task_length
            merge_count += 1

        # Put remaining items back into the queue
        for item in remaining:
            queue.put_nowait(item)
            # Compensate: this item was already counted when first enqueued,
            # put_nowait adds a duplicate count that must be removed
            queue.task_done()

    if merge_count == 0:
        return first, 0

    return (
        MessageTask(
            task_type="content",
            window_id=first.window_id,
            parts=merged_parts,
            tool_use_id=first.tool_use_id,
            content_type=first.content_type,
            thread_id=first.thread_id,
            image_data=first.image_data,
            document_data=first.document_data,
        ),
        merge_count,
    )


async def _message_queue_worker(bot: Bot, user_id: int) -> None:
    """Process message tasks for a user sequentially."""
    queue = _message_queues[user_id]
    lock = _queue_locks[user_id]
    logger.info(f"Message queue worker started for user {user_id}")

    while True:
        try:
            task = await queue.get()
            try:
                # Flood control: drop status, wait for content
                flood_end = _flood_until.get(user_id, 0)
                if flood_end > 0:
                    remaining = flood_end - time.monotonic()
                    if remaining > 0:
                        if task.task_type != "content":
                            # Status is ephemeral — safe to drop
                            continue
                        # Content is actual assistant output — wait then send
                        logger.debug(
                            "Flood controlled: waiting %.0fs for content (user %d)",
                            remaining,
                            user_id,
                        )
                        await asyncio.sleep(remaining)
                    # Ban expired
                    _flood_until.pop(user_id, None)
                    logger.info("Flood control lifted for user %d", user_id)

                if task.task_type == "content":
                    # Try to merge consecutive content tasks
                    merged_task, merge_count = await _merge_content_tasks(
                        queue, task, lock
                    )
                    if merge_count > 0:
                        logger.debug(f"Merged {merge_count} tasks for user {user_id}")
                        # Mark merged tasks as done
                        for _ in range(merge_count):
                            queue.task_done()
                    await _process_content_task(bot, user_id, merged_task)
                elif task.task_type == "status_update":
                    await _process_status_update_task(bot, user_id, task)
                elif task.task_type == "status_clear":
                    await _do_clear_status_message(bot, user_id, task.thread_id or 0)
                elif task.task_type == "progress_start":
                    await _process_progress_start_task(bot, user_id, task)
                elif task.task_type == "progress_update":
                    await _process_progress_update_task(bot, user_id, task)
                elif task.task_type == "progress_clear":
                    await _do_clear_progress_message(bot, user_id, task.thread_id or 0)
                elif task.task_type == "progress_finalize":
                    await _process_progress_finalize_task(bot, user_id, task)
            except RetryAfter as e:
                retry_secs = (
                    e.retry_after
                    if isinstance(e.retry_after, int)
                    else int(e.retry_after.total_seconds())
                )
                if retry_secs > FLOOD_CONTROL_MAX_WAIT:
                    _flood_until[user_id] = time.monotonic() + retry_secs
                    logger.warning(
                        "Flood control for user %d: retry_after=%ds, "
                        "pausing queue until ban expires",
                        user_id,
                        retry_secs,
                    )
                else:
                    logger.warning(
                        "Flood control for user %d: waiting %ds",
                        user_id,
                        retry_secs,
                    )
                    await asyncio.sleep(retry_secs)
            except Exception as e:
                logger.error(f"Error processing message task for user {user_id}: {e}")
            finally:
                queue.task_done()
        except asyncio.CancelledError:
            logger.info(f"Message queue worker cancelled for user {user_id}")
            break
        except Exception as e:
            logger.error(f"Unexpected error in queue worker for user {user_id}: {e}")


def _send_kwargs(thread_id: int | None) -> dict[str, int]:
    """Build message_thread_id kwargs for bot.send_message()."""
    if thread_id is not None:
        return {"message_thread_id": thread_id}
    return {}


def _can_coalesce_progress_task(base: MessageTask, candidate: MessageTask) -> bool:
    """Return whether pending progress tasks belong to the same topic/window."""
    return (
        base.task_type == "progress_update"
        and candidate.task_type == "progress_update"
        and base.window_id == candidate.window_id
        and (base.thread_id or 0) == (candidate.thread_id or 0)
    )


async def _enqueue_progress_update_coalesced(
    queue: asyncio.Queue[MessageTask],
    lock: asyncio.Lock,
    task: MessageTask,
) -> None:
    """Enqueue progress update while coalescing trailing pending progress tasks.

    We only fold trailing updates for the same topic/window. This preserves queue
    ordering for content/finalization tasks and drastically cuts Telegram edit
    pressure during token-level app-server bursts.
    """
    async with lock:
        items = _inspect_queue(queue)
        retained: list[MessageTask] = items
        collapsed: list[MessageTask] = []

        while retained and _can_coalesce_progress_task(retained[-1], task):
            collapsed.append(retained.pop())

        merged_text = ""
        for pending in reversed(collapsed):
            merged_text = _merge_progress_text(merged_text, pending.text or "")
        merged_text = _merge_progress_text(merged_text, task.text or "")
        task.text = merged_text

        # Requeue retained items and neutralize new put() counter increments.
        for item in retained:
            queue.put_nowait(item)
            queue.task_done()

        # Dropped pending progress tasks were already counted as unfinished; mark
        # them as done now so queue.join() cannot deadlock.
        for _ in collapsed:
            queue.task_done()

        queue.put_nowait(task)


def _merge_progress_text(existing: str, new_chunk: str) -> str:
    """Append a progress chunk to accumulated progress text."""
    if not new_chunk:
        return existing
    if not existing:
        merged = new_chunk
    elif new_chunk.startswith(existing):
        merged = new_chunk
    elif existing.endswith(new_chunk):
        return existing
    elif existing.startswith(new_chunk):
        return existing
    else:
        merged = f"{existing}{new_chunk}"

    if len(merged) <= PROGRESS_MAX_LENGTH:
        return merged

    tail = merged[-(PROGRESS_MAX_LENGTH - 2) :]
    return "… " + tail


def _render_progress_message(text: str) -> str:
    """Render accumulated progress text for Telegram."""
    text = text.strip()
    if not text:
        return "⏳ Working…"
    is_complete = text.endswith(PROGRESS_COMPLETE_MARKER)
    heading = "✅ Process Complete" if is_complete else "⏳ Working…"
    if is_complete:
        return f"{heading}\n\n{text}"

    # Keep working view concise while retaining full accumulated text internally.
    body = text
    if len(body) > PROGRESS_PREVIEW_MAX_LENGTH:
        tail = body[-(PROGRESS_PREVIEW_MAX_LENGTH - 2) :]
        first_break = tail.find("\n")
        if 0 < first_break <= 120:
            tail = tail[first_break + 1 :]
        body = f"… {tail.lstrip()}"
    return f"{heading}\n\n{body}"


def _is_message_not_modified_error(exc: Exception) -> bool:
    """Return True when Telegram rejected edit because text is unchanged."""
    return "message is not modified" in str(exc).lower()


async def _send_task_images(bot: Bot, chat_id: int, task: MessageTask) -> None:
    """Send images attached to a task, if any."""
    if not task.image_data:
        return
    logger.info(
        "Sending %d image(s) in thread %s",
        len(task.image_data),
        task.thread_id,
    )
    await send_photo(
        bot,
        chat_id,
        task.image_data,
        **_send_kwargs(task.thread_id),  # type: ignore[arg-type]
    )


async def _send_task_documents(bot: Bot, chat_id: int, task: MessageTask) -> None:
    """Send documents attached to a task, if any."""
    if not task.document_data:
        return
    logger.info(
        "Sending %d document(s) in thread %s",
        len(task.document_data),
        task.thread_id,
    )
    await send_documents(
        bot,
        chat_id,
        task.document_data,
        **_send_kwargs(task.thread_id),  # type: ignore[arg-type]
    )


async def _process_content_task(bot: Bot, user_id: int, task: MessageTask) -> None:
    """Process a content message task."""
    wid = task.window_id or ""
    tid = task.thread_id or 0
    chat_id = session_manager.resolve_chat_id(user_id, task.thread_id)

    # 1. Handle tool_result editing (merged parts are edited together)
    if task.content_type == "tool_result" and task.tool_use_id:
        _tkey = (task.tool_use_id, user_id, tid)
        edit_msg_id = _tool_msg_ids.pop(_tkey, None)
        if edit_msg_id is not None:
            # Clear status message first
            await _do_clear_status_message(bot, user_id, tid)
            # Join all parts for editing (merged content goes together)
            full_text = "\n\n".join(task.parts)
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=edit_msg_id,
                    text=convert_markdown(full_text),
                    parse_mode="MarkdownV2",
                    link_preview_options=NO_LINK_PREVIEW,
                )
                log_outgoing_edit(
                    text=full_text,
                    chat_id=chat_id,
                    thread_id=task.thread_id,
                    message_id=edit_msg_id,
                    source="message_queue.tool_result",
                )
                await _send_task_images(bot, chat_id, task)
                await _send_task_documents(bot, chat_id, task)
                await _check_and_send_status(bot, user_id, wid, task.thread_id)
                return
            except RetryAfter:
                raise
            except Exception:
                try:
                    # Fallback: plain text with sentinels stripped
                    plain_text = (
                        (task.text or full_text)
                        .replace(TranscriptParser.EXPANDABLE_QUOTE_START, "")
                        .replace(TranscriptParser.EXPANDABLE_QUOTE_END, "")
                    )
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=edit_msg_id,
                        text=plain_text,
                        link_preview_options=NO_LINK_PREVIEW,
                    )
                    log_outgoing_edit(
                        text=plain_text,
                        chat_id=chat_id,
                        thread_id=task.thread_id,
                        message_id=edit_msg_id,
                        source="message_queue.tool_result",
                    )
                    await _send_task_images(bot, chat_id, task)
                    await _send_task_documents(bot, chat_id, task)
                    await _check_and_send_status(bot, user_id, wid, task.thread_id)
                    return
                except RetryAfter:
                    raise
                except Exception:
                    logger.debug(f"Failed to edit tool msg {edit_msg_id}, sending new")
                    # Fall through to send as new message

    # 2. Send content messages, converting status message to first content part
    first_part = True
    last_msg_id: int | None = None
    for part in task.parts:
        sent = None

        # For first part, try to convert status message to content (edit instead of delete)
        if first_part:
            first_part = False
            converted_msg_id = await _convert_status_to_content(
                bot,
                user_id,
                tid,
                wid,
                part,
            )
            if converted_msg_id is not None:
                last_msg_id = converted_msg_id
                continue

        sent = await send_with_fallback(
            bot,
            chat_id,
            part,
            **_send_kwargs(task.thread_id),  # type: ignore[arg-type]
        )

        if sent:
            last_msg_id = sent.message_id

    # 3. Record tool_use message ID for later editing
    if last_msg_id and task.tool_use_id and task.content_type == "tool_use":
        _tool_msg_ids[(task.tool_use_id, user_id, tid)] = last_msg_id

    # 4. Send images if present (from tool_result with base64 image blocks)
    await _send_task_images(bot, chat_id, task)
    await _send_task_documents(bot, chat_id, task)

    # 5. After content, check and send status
    await _check_and_send_status(bot, user_id, wid, task.thread_id)


async def _convert_status_to_content(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int,
    window_id: str,
    content_text: str,
) -> int | None:
    """Convert status message to content message by editing it.

    Returns the message_id if converted successfully, None otherwise.
    """
    skey = (user_id, thread_id_or_0)
    info = _status_msg_info.pop(skey, None)
    if not info:
        return None

    msg_id, stored_wid, _ = info
    chat_id = session_manager.resolve_chat_id(user_id, thread_id_or_0 or None)
    if stored_wid != window_id:
        # Different window, just delete the old status
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass
        return None

    # Edit status message to show content
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=convert_markdown(content_text),
            parse_mode="MarkdownV2",
            link_preview_options=NO_LINK_PREVIEW,
        )
        log_outgoing_edit(
            text=content_text,
            chat_id=chat_id,
            thread_id=thread_id_or_0 or None,
            message_id=msg_id,
            source="message_queue.status_to_content",
        )
        return msg_id
    except RetryAfter:
        raise
    except Exception:
        try:
            # Fallback to plain text with sentinels stripped
            plain = content_text.replace(
                TranscriptParser.EXPANDABLE_QUOTE_START, ""
            ).replace(TranscriptParser.EXPANDABLE_QUOTE_END, "")
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=plain,
                link_preview_options=NO_LINK_PREVIEW,
            )
            log_outgoing_edit(
                text=plain,
                chat_id=chat_id,
                thread_id=thread_id_or_0 or None,
                message_id=msg_id,
                source="message_queue.status_to_content",
            )
            return msg_id
        except RetryAfter:
            raise
        except Exception as e:
            logger.debug(f"Failed to convert status to content: {e}")
            # Message might be deleted or too old, caller will send new message
            return None


async def _process_progress_update_task(
    bot: Bot, user_id: int, task: MessageTask
) -> None:
    """Process an in-progress update (single editable message)."""
    wid = task.window_id or ""
    tid = task.thread_id or 0
    chat_id = session_manager.resolve_chat_id(user_id, task.thread_id)
    skey = (user_id, tid)
    chunk = task.text or ""

    if not chunk.strip():
        await _do_clear_progress_message(bot, user_id, tid)
        return

    # Keep only one ephemeral process message visible at a time.
    await _do_clear_status_message(bot, user_id, tid)

    current_info = _progress_msg_info.get(skey)
    if current_info:
        msg_id, stored_wid, accumulated = current_info

        if stored_wid != wid:
            await _do_clear_progress_message(bot, user_id, tid)
            new_accumulated = _merge_progress_text("", chunk)
            await _do_send_progress_message(
                bot, user_id, tid, wid, new_accumulated, chat_id=chat_id
            )
            return

        if accumulated.endswith(PROGRESS_COMPLETE_MARKER):
            return

        updated = _merge_progress_text(accumulated, chunk)
        if updated == accumulated:
            return

        rendered = _render_progress_message(updated)
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=convert_markdown(rendered),
                parse_mode="MarkdownV2",
                link_preview_options=NO_LINK_PREVIEW,
            )
            log_outgoing_edit(
                text=rendered,
                chat_id=chat_id,
                thread_id=tid or None,
                message_id=msg_id,
                source="message_queue.progress_update",
            )
            _progress_msg_info[skey] = (msg_id, wid, updated)
        except RetryAfter:
            raise
        except Exception as e:
            if _is_message_not_modified_error(e):
                _progress_msg_info[skey] = (msg_id, wid, updated)
                return
            try:
                plain = rendered.replace(
                    TranscriptParser.EXPANDABLE_QUOTE_START, ""
                ).replace(TranscriptParser.EXPANDABLE_QUOTE_END, "")
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=plain,
                    link_preview_options=NO_LINK_PREVIEW,
                )
                log_outgoing_edit(
                    text=plain,
                    chat_id=chat_id,
                    thread_id=tid or None,
                    message_id=msg_id,
                    source="message_queue.progress_update",
                )
                _progress_msg_info[skey] = (msg_id, wid, updated)
            except RetryAfter:
                raise
            except Exception as plain_error:
                if _is_message_not_modified_error(plain_error):
                    _progress_msg_info[skey] = (msg_id, wid, updated)
                    return
                logger.debug(f"Failed to edit progress message: {plain_error}")
                _progress_msg_info.pop(skey, None)
                await _do_send_progress_message(
                    bot, user_id, tid, wid, updated, chat_id=chat_id
                )
    else:
        new_accumulated = _merge_progress_text("", chunk)
        await _do_send_progress_message(
            bot, user_id, tid, wid, new_accumulated, chat_id=chat_id
        )


async def _process_progress_start_task(
    bot: Bot, user_id: int, task: MessageTask
) -> None:
    """Ensure an in-progress message exists for a topic/window."""
    wid = task.window_id or ""
    tid = task.thread_id or 0
    chat_id = session_manager.resolve_chat_id(user_id, task.thread_id)
    skey = (user_id, tid)

    # Hide status when we intentionally enter progress mode.
    await _do_clear_status_message(bot, user_id, tid)

    current = _progress_msg_info.get(skey)
    if current:
        msg_id, stored_wid, _accumulated = current
        if stored_wid == wid:
            return
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass
        _progress_msg_info.pop(skey, None)

    await _do_send_progress_message(
        bot,
        user_id,
        tid,
        wid,
        "",
        chat_id=chat_id,
    )


async def _process_progress_finalize_task(
    bot: Bot, user_id: int, task: MessageTask
) -> None:
    """Mark the in-progress process message as complete and keep it visible."""
    tid = task.thread_id or 0
    wid = task.window_id or ""
    skey = (user_id, tid)
    info = _progress_msg_info.get(skey)
    if not info:
        return

    msg_id, stored_wid, accumulated = info
    if wid and stored_wid != wid:
        return
    if accumulated.endswith(PROGRESS_COMPLETE_MARKER):
        return
    if not accumulated.strip():
        # No real progress content was ever shown; remove the placeholder.
        await _do_clear_progress_message(bot, user_id, tid)
        return

    compact_finalize = (task.finalize_mode or "").strip().lower() == "compact"
    finalized = f"{accumulated.strip()}\n\n{PROGRESS_COMPLETE_MARKER}".strip()
    chat_id = session_manager.resolve_chat_id(user_id, task.thread_id)
    rendered = "✅ Process Complete" if compact_finalize else _render_progress_message(finalized)

    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=convert_markdown(rendered),
            parse_mode="MarkdownV2",
            link_preview_options=NO_LINK_PREVIEW,
        )
        log_outgoing_edit(
            text=rendered,
            chat_id=chat_id,
            thread_id=task.thread_id,
            message_id=msg_id,
            source="message_queue.progress_finalize",
        )
        # Keep the finalized process message in chat history, but stop tracking it
        # so the next user turn creates a fresh process message.
        _progress_msg_info.pop(skey, None)
        _clear_progress_text_cache(user_id, task.thread_id)
    except RetryAfter:
        raise
    except Exception as e:
        if _is_message_not_modified_error(e):
            _progress_msg_info.pop(skey, None)
            _clear_progress_text_cache(user_id, task.thread_id)
            return
        try:
            plain = rendered.replace(
                TranscriptParser.EXPANDABLE_QUOTE_START, ""
            ).replace(TranscriptParser.EXPANDABLE_QUOTE_END, "")
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=plain,
                link_preview_options=NO_LINK_PREVIEW,
            )
            log_outgoing_edit(
                text=plain,
                chat_id=chat_id,
                thread_id=task.thread_id,
                message_id=msg_id,
                source="message_queue.progress_finalize",
            )
            _progress_msg_info.pop(skey, None)
            _clear_progress_text_cache(user_id, task.thread_id)
        except RetryAfter:
            raise
        except Exception as plain_error:
            if _is_message_not_modified_error(plain_error):
                _progress_msg_info.pop(skey, None)
                _clear_progress_text_cache(user_id, task.thread_id)
                return
            logger.debug(f"Failed to finalize progress message: {plain_error}")


async def _do_send_progress_message(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int,
    window_id: str,
    accumulated_text: str,
    chat_id: int | None = None,
) -> None:
    """Send a new in-progress message and track it."""
    skey = (user_id, thread_id_or_0)
    thread_id: int | None = thread_id_or_0 if thread_id_or_0 != 0 else None
    if chat_id is None:
        chat_id = session_manager.resolve_chat_id(user_id, thread_id)

    # Remove any orphaned progress message first.
    old = _progress_msg_info.pop(skey, None)
    if old:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=old[0])
        except Exception:
            pass

    rendered = _render_progress_message(accumulated_text)
    sent = await send_with_fallback(
        bot,
        chat_id,
        rendered,
        **_send_kwargs(thread_id),  # type: ignore[arg-type]
    )
    if sent:
        _progress_msg_info[skey] = (sent.message_id, window_id, accumulated_text)


async def _do_clear_progress_message(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int = 0,
) -> None:
    """Delete the in-progress message for a user/topic."""
    skey = (user_id, thread_id_or_0)
    info = _progress_msg_info.pop(skey, None)
    _clear_progress_text_cache(user_id, thread_id_or_0 or None)
    if info:
        msg_id = info[0]
        chat_id = session_manager.resolve_chat_id(user_id, thread_id_or_0 or None)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception as e:
            logger.debug(f"Failed to delete progress message {msg_id}: {e}")


async def _process_status_update_task(
    bot: Bot, user_id: int, task: MessageTask
) -> None:
    """Process a status update task."""
    wid = task.window_id or ""
    tid = task.thread_id or 0
    chat_id = session_manager.resolve_chat_id(user_id, task.thread_id)
    skey = (user_id, tid)
    status_text = task.text or ""

    if not status_text:
        # No status text means clear status
        await _do_clear_status_message(bot, user_id, tid)
        return

    current_info = _status_msg_info.get(skey)

    if current_info:
        msg_id, stored_wid, last_text = current_info

        if stored_wid != wid:
            # Window changed - delete old and send new
            await _do_clear_status_message(bot, user_id, tid)
            await _do_send_status_message(bot, user_id, tid, wid, status_text)
        elif status_text == last_text:
            # Same content, skip edit
            return
        else:
            # Same window, text changed - edit in place
            # Send typing indicator when Codex is working
            if "esc to interrupt" in status_text.lower():
                try:
                    await bot.send_chat_action(
                        chat_id=chat_id, action=ChatAction.TYPING
                    )
                except RetryAfter:
                    raise
                except Exception:
                    pass
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=convert_markdown(status_text),
                    parse_mode="MarkdownV2",
                    link_preview_options=NO_LINK_PREVIEW,
                )
                log_outgoing_edit(
                    text=status_text,
                    chat_id=chat_id,
                    thread_id=tid or None,
                    message_id=msg_id,
                    source="message_queue.status_update",
                )
                _status_msg_info[skey] = (msg_id, wid, status_text)
            except RetryAfter:
                raise
            except Exception:
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg_id,
                        text=status_text,
                        link_preview_options=NO_LINK_PREVIEW,
                    )
                    log_outgoing_edit(
                        text=status_text,
                        chat_id=chat_id,
                        thread_id=tid or None,
                        message_id=msg_id,
                        source="message_queue.status_update",
                    )
                    _status_msg_info[skey] = (msg_id, wid, status_text)
                except RetryAfter:
                    raise
                except Exception as e:
                    logger.debug(f"Failed to edit status message: {e}")
                    _status_msg_info.pop(skey, None)
                    await _do_send_status_message(bot, user_id, tid, wid, status_text)
    else:
        # No existing status message, send new
        await _do_send_status_message(bot, user_id, tid, wid, status_text)


async def _do_send_status_message(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int,
    window_id: str,
    text: str,
) -> None:
    """Send a new status message and track it (internal, called from worker)."""
    skey = (user_id, thread_id_or_0)
    thread_id: int | None = thread_id_or_0 if thread_id_or_0 != 0 else None
    chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    # Safety net: delete any orphaned status message before sending a new one.
    # This catches edge cases where tracking was cleared without deleting the message.
    old = _status_msg_info.pop(skey, None)
    if old:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=old[0])
        except Exception:
            pass
    # Send typing indicator when Codex is working
    if "esc to interrupt" in text.lower():
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except RetryAfter:
            raise
        except Exception:
            pass
    sent = await send_with_fallback(
        bot,
        chat_id,
        text,
        **_send_kwargs(thread_id),  # type: ignore[arg-type]
    )
    if sent:
        _status_msg_info[skey] = (sent.message_id, window_id, text)


async def _do_clear_status_message(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int = 0,
) -> None:
    """Delete the status message for a user (internal, called from worker)."""
    skey = (user_id, thread_id_or_0)
    info = _status_msg_info.pop(skey, None)
    if info:
        msg_id = info[0]
        chat_id = session_manager.resolve_chat_id(user_id, thread_id_or_0 or None)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception as e:
            logger.debug(f"Failed to delete status message {msg_id}: {e}")


async def _check_and_send_status(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
) -> None:
    """Legacy status polling hook retained as no-op in app-server runtime."""
    _ = bot, user_id, window_id, thread_id


async def enqueue_content_message(
    bot: Bot,
    user_id: int,
    window_id: str,
    parts: list[str],
    tool_use_id: str | None = None,
    content_type: str = "text",
    text: str | None = None,
    thread_id: int | None = None,
    image_data: list[tuple[str, bytes]] | None = None,
    document_data: list[tuple[str, bytes]] | None = None,
) -> None:
    """Enqueue a content message task."""
    logger.debug(
        "Enqueue content: user=%d, window_id=%s, content_type=%s",
        user_id,
        window_id,
        content_type,
    )
    queue = get_or_create_queue(bot, user_id)

    task = MessageTask(
        task_type="content",
        text=text,
        window_id=window_id,
        parts=parts,
        tool_use_id=tool_use_id,
        content_type=content_type,
        thread_id=thread_id,
        image_data=image_data,
        document_data=document_data,
    )
    queue.put_nowait(task)


async def enqueue_status_update(
    bot: Bot,
    user_id: int,
    window_id: str,
    status_text: str | None,
    thread_id: int | None = None,
) -> None:
    """Enqueue status update. Skipped if text unchanged or during flood control."""
    # Don't enqueue during flood control — they'd just be dropped
    flood_end = _flood_until.get(user_id, 0)
    if flood_end > time.monotonic():
        return

    tid = thread_id or 0

    # Suppress terminal status updates while an in-progress message is active.
    if (user_id, tid) in _progress_msg_info:
        return

    # Deduplicate: skip if text matches what's already displayed
    if status_text:
        skey = (user_id, tid)
        info = _status_msg_info.get(skey)
        if info and info[1] == window_id and info[2] == status_text:
            return

    queue = get_or_create_queue(bot, user_id)

    if status_text:
        task = MessageTask(
            task_type="status_update",
            text=status_text,
            window_id=window_id,
            thread_id=thread_id,
        )
    else:
        task = MessageTask(task_type="status_clear", thread_id=thread_id)

    queue.put_nowait(task)


async def enqueue_progress_update(
    bot: Bot,
    user_id: int,
    window_id: str,
    progress_text: str,
    thread_id: int | None = None,
) -> None:
    """Enqueue an in-progress update (single editable process message)."""
    flood_end = _flood_until.get(user_id, 0)
    if flood_end > time.monotonic():
        return

    if not progress_text.strip():
        return

    _cache_progress_text(
        user_id=user_id,
        thread_id=thread_id,
        window_id=window_id,
        chunk=progress_text,
    )
    queue = get_or_create_queue(bot, user_id)
    lock = _queue_locks[user_id]
    task = MessageTask(
        task_type="progress_update",
        text=progress_text,
        window_id=window_id,
        thread_id=thread_id,
    )
    await _enqueue_progress_update_coalesced(queue, lock, task)


async def enqueue_progress_start(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
) -> None:
    """Enqueue creation of an in-progress placeholder message."""
    flood_end = _flood_until.get(user_id, 0)
    if flood_end > time.monotonic():
        return

    queue = get_or_create_queue(bot, user_id)
    task = MessageTask(
        task_type="progress_start",
        window_id=window_id,
        thread_id=thread_id,
    )
    queue.put_nowait(task)


async def enqueue_progress_clear(
    bot: Bot,
    user_id: int,
    thread_id: int | None = None,
) -> None:
    """Enqueue clearing of the in-progress message."""
    _clear_progress_text_cache(user_id, thread_id)
    queue = get_or_create_queue(bot, user_id)
    task = MessageTask(task_type="progress_clear", thread_id=thread_id)
    queue.put_nowait(task)


async def enqueue_progress_finalize(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
    *,
    compact: bool = False,
) -> None:
    """Enqueue finalization of an in-progress message (kept in chat)."""
    queue = get_or_create_queue(bot, user_id)
    task = MessageTask(
        task_type="progress_finalize",
        window_id=window_id,
        thread_id=thread_id,
        finalize_mode="compact" if compact else "full",
    )
    queue.put_nowait(task)


def clear_status_msg_info(user_id: int, thread_id: int | None = None) -> None:
    """Clear status message tracking for a user (and optionally a specific thread)."""
    skey = (user_id, thread_id or 0)
    _status_msg_info.pop(skey, None)


def clear_progress_msg_info(user_id: int, thread_id: int | None = None) -> None:
    """Clear in-progress message tracking for a user/topic."""
    skey = (user_id, thread_id or 0)
    _progress_msg_info.pop(skey, None)
    _progress_text_cache.pop(skey, None)


def is_progress_active(user_id: int, thread_id: int | None = None) -> bool:
    """Return whether a topic currently has an active in-progress message."""
    skey = (user_id, thread_id or 0)
    return skey in _progress_msg_info


def get_progress_text(user_id: int, thread_id: int | None = None) -> str:
    """Return accumulated in-progress text for a user/topic, if present."""
    skey = (user_id, thread_id or 0)
    cached = _progress_text_cache.get(skey)
    if cached:
        return cached[1]
    info = _progress_msg_info.get(skey)
    if not info:
        return ""
    return info[2]


def enqueue_queued_topic_input(
    user_id: int,
    thread_id: int | None,
    text: str,
    source_chat_id: int,
    source_message_id: int,
) -> int:
    """Queue a /q input for dispatch after the current run completes.

    Returns the new queue length for this topic.
    """
    skey = (user_id, thread_id or 0)
    bucket = _queued_topic_inputs.setdefault(skey, [])
    bucket.append((text, source_chat_id, source_message_id))
    return len(bucket)


def prepend_queued_topic_input(
    user_id: int,
    thread_id: int | None,
    text: str,
    source_chat_id: int,
    source_message_id: int,
) -> int:
    """Put one /q item back at the front of the topic queue.

    Returns the new queue length for this topic.
    """
    skey = (user_id, thread_id or 0)
    bucket = _queued_topic_inputs.setdefault(skey, [])
    bucket.insert(0, (text, source_chat_id, source_message_id))
    return len(bucket)


def get_queued_topic_input_snapshot(
    user_id: int,
    thread_id: int | None,
) -> list[tuple[str, int, int]]:
    """Return a shallow copy of queued /q items for a topic."""
    skey = (user_id, thread_id or 0)
    return list(_queued_topic_inputs.get(skey, []))


def pop_queued_topic_input(
    user_id: int,
    thread_id: int | None,
) -> tuple[str, int, int] | None:
    """Pop the next queued /q input for a topic (FIFO)."""
    skey = (user_id, thread_id or 0)
    bucket = _queued_topic_inputs.get(skey)
    if not bucket:
        return None
    item = bucket.pop(0)
    if not bucket:
        _queued_topic_inputs.pop(skey, None)
    return item


def queued_topic_input_count(user_id: int, thread_id: int | None) -> int:
    """Return number of queued /q inputs for a topic."""
    skey = (user_id, thread_id or 0)
    return len(_queued_topic_inputs.get(skey, []))


def clear_queued_topic_inputs(user_id: int, thread_id: int | None = None) -> None:
    """Clear queued /q inputs for a topic."""
    skey = (user_id, thread_id or 0)
    _queued_topic_inputs.pop(skey, None)


def clear_tool_msg_ids_for_topic(user_id: int, thread_id: int | None = None) -> None:
    """Clear tool message ID tracking for a specific topic.

    Removes all entries in _tool_msg_ids that match the given user and thread.
    """
    tid = thread_id or 0
    # Find and remove all matching keys
    keys_to_remove = [
        key for key in _tool_msg_ids if key[1] == user_id and key[2] == tid
    ]
    for key in keys_to_remove:
        _tool_msg_ids.pop(key, None)


async def shutdown_workers() -> None:
    """Stop all queue workers (called during bot shutdown)."""
    for _, worker in list(_queue_workers.items()):
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
    _queue_workers.clear()
    _message_queues.clear()
    _queue_locks.clear()
    _queue_dock_msg_info.clear()
    logger.info("Message queue workers stopped")
