"""Session monitoring service — watches JSONL files for new messages.

Runs an async polling loop that:
  1. Loads current topic/window bindings to know which sessions to watch.
  2. Detects binding/session changes and cleans up stale tracked sessions.
  3. Reads new JSONL lines from each session file using byte-offset tracking.
  4. Parses entries via TranscriptParser and emits NewMessage objects to a callback.

Optimizations: mtime cache skips unchanged files; byte offset avoids re-reading.

Key classes: SessionMonitor, NewMessage, SessionInfo.
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Awaitable

import aiofiles

from .config import config
from .monitor_state import MonitorState, TrackedSession
from .transcript_parser import TranscriptParser
logger = logging.getLogger(__name__)

_UUID_SUFFIX_RE = re.compile(
    r"(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)


@dataclass
class SessionInfo:
    """Information about a session transcript."""

    session_id: str
    file_path: Path


@dataclass
class NewMessage:
    """A new message detected by the monitor."""

    session_id: str
    text: str
    is_complete: bool  # True when stop_reason is set (final message)
    content_type: str = "text"  # "text" or "thinking"
    tool_use_id: str | None = None
    role: str = "assistant"  # "user", "assistant", or "system"
    tool_name: str | None = None  # For tool_use messages, the tool name
    source: str = "transcript"  # "transcript" or "app_server"
    event_type: str | None = None  # Lifecycle/control event for routing
    image_data: list[tuple[str, bytes]] | None = None  # From tool_result images
    file_path: str | None = None  # Session JSONL file path for fast offset updates


class SessionMonitor:
    """Monitors assistant sessions for new assistant messages.

    Uses simple async polling with aiofiles for non-blocking I/O.
    Emits both intermediate and complete assistant messages.
    """

    def __init__(
        self,
        projects_path: Path | None = None,
        poll_interval: float | None = None,
        state_file: Path | None = None,
    ):
        self.projects_path = (
            projects_path if projects_path is not None else config.sessions_path
        )
        self.poll_interval = (
            poll_interval if poll_interval is not None else config.monitor_poll_interval
        )

        self.state = MonitorState(state_file=state_file or config.monitor_state_file)
        self.state.load()

        self._running = False
        self._task: asyncio.Task | None = None
        self._message_callback: Callable[[NewMessage], Awaitable[None]] | None = None
        # Per-session pending tool_use state carried across poll cycles
        self._pending_tools: dict[str, dict[str, Any]] = {}  # session_id -> pending
        # Track last known window->session mapping for detecting changes
        self._last_session_map: dict[str, str] = {}  # window_key -> session_id
        # In-memory mtime cache for quick file change detection (not persisted)
        self._file_mtimes: dict[str, float] = {}  # session_id -> last_seen_mtime

    def set_message_callback(
        self, callback: Callable[[NewMessage], Awaitable[None]]
    ) -> None:
        self._message_callback = callback

    async def _get_active_cwds(self) -> set[str]:
        """Get normalized cwd values for active topic bindings."""
        from .session import session_manager

        cwds: set[str] = set()
        for _user_id, _chat_id, _thread_id, binding in session_manager.iter_topic_bindings():
            candidates: list[str] = []
            if binding.cwd:
                candidates.append(binding.cwd)
            if binding.window_id:
                state_cwd = session_manager.get_window_state(binding.window_id).cwd
                if state_cwd:
                    candidates.append(state_cwd)
            for cwd in candidates:
                try:
                    cwds.add(str(Path(cwd).resolve()))
                except (OSError, ValueError):
                    cwds.add(cwd)
        return cwds

    async def scan_projects(self) -> list[SessionInfo]:
        """Scan Codex transcripts that match active topic/session bindings."""
        active_cwds = await self._get_active_cwds()
        if not active_cwds:
            return []
        return await asyncio.to_thread(
            self._scan_codex_sessions_sync,
            active_cwds,
        )

    def _scan_codex_sessions_sync(self, active_cwds: set[str]) -> list[SessionInfo]:
        """Scan Codex session logs under ~/.codex/sessions recursively."""
        sessions: list[SessionInfo] = []
        seen: set[str] = set()
        if not self.projects_path.exists():
            return sessions

        candidates = sorted(
            self.projects_path.glob("**/*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for file_path in candidates:
            try:
                with file_path.open("r", encoding="utf-8") as f:
                    meta_line = f.readline()
            except OSError:
                continue

            session_id = ""
            cwd = ""
            if meta_line:
                try:
                    data = json.loads(meta_line)
                except json.JSONDecodeError:
                    data = None
                if isinstance(data, dict) and data.get("type") == "session_meta":
                    payload = data.get("payload", {})
                    if isinstance(payload, dict):
                        sid = payload.get("id", "")
                        c = payload.get("cwd", "")
                        if isinstance(sid, str):
                            session_id = sid
                        if isinstance(c, str):
                            cwd = c

            if not session_id:
                m = _UUID_SUFFIX_RE.search(file_path.stem)
                if m:
                    session_id = m.group("id")
            if not session_id:
                continue

            try:
                norm_cwd = str(Path(cwd).resolve()) if cwd else ""
            except (OSError, ValueError):
                norm_cwd = cwd
            if not norm_cwd:
                continue
            if norm_cwd not in active_cwds:
                continue
            if session_id in seen:
                continue
            seen.add(session_id)
            sessions.append(
                SessionInfo(
                    session_id=session_id,
                    file_path=file_path,
                )
            )
        return sessions

    def _resolve_codex_session_files_sync(
        self, active_session_ids: set[str]
    ) -> list[SessionInfo]:
        """Resolve active Codex session ids to transcript files."""
        sessions: list[SessionInfo] = []
        if not self.projects_path.exists():
            return sessions
        for session_id in active_session_ids:
            matches = sorted(
                self.projects_path.glob(f"**/*-{session_id}.jsonl"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not matches:
                continue
            sessions.append(
                SessionInfo(
                    session_id=session_id,
                    file_path=matches[0],
                )
            )
        return sessions

    async def _read_new_lines(
        self, session: TrackedSession, file_path: Path
    ) -> list[dict]:
        """Read new lines from a session file using byte offset for efficiency.

        Detects file truncation (e.g. after /clear) and resets offset.
        Recovers from corrupted offsets (mid-line) by scanning to next line.
        """
        new_entries = []
        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                # Get file size to detect truncation
                await f.seek(0, 2)  # Seek to end
                file_size = await f.tell()

                # Detect file truncation: if offset is beyond file size, reset
                if session.last_byte_offset > file_size:
                    logger.info(
                        "File truncated for session %s "
                        "(offset %d > size %d). Resetting.",
                        session.session_id,
                        session.last_byte_offset,
                        file_size,
                    )
                    session.last_byte_offset = 0

                # Seek to last read position for incremental reading
                await f.seek(session.last_byte_offset)

                # Detect corrupted offset: if we're mid-line (not at '{'),
                # scan forward to the next line start. This can happen if
                # the state file was manually edited or corrupted.
                if session.last_byte_offset > 0:
                    first_char = await f.read(1)
                    if first_char and first_char != "{":
                        logger.warning(
                            "Corrupted offset %d in session %s (mid-line), "
                            "scanning to next line",
                            session.last_byte_offset,
                            session.session_id,
                        )
                        await f.readline()  # Skip rest of partial line
                        session.last_byte_offset = await f.tell()
                        return []
                    await f.seek(session.last_byte_offset)  # Reset for normal read

                # Read only new lines from the offset.
                # Track safe_offset: only advance past lines that parsed
                # successfully. A non-empty line that fails JSON parsing is
                # likely a partial write; stop and retry next cycle.
                safe_offset = session.last_byte_offset
                async for line in f:
                    data = TranscriptParser.parse_line(line)
                    if data:
                        new_entries.append(data)
                        safe_offset = await f.tell()
                    elif line.strip():
                        # Partial JSONL line — don't advance offset past it
                        logger.warning(
                            "Partial JSONL line in session %s, will retry next cycle",
                            session.session_id,
                        )
                        break
                    else:
                        # Empty line — safe to skip
                        safe_offset = await f.tell()

                session.last_byte_offset = safe_offset

        except OSError as e:
            logger.error("Error reading session file %s: %s", file_path, e)
        return new_entries

    async def check_for_updates(self, active_session_ids: set[str]) -> list[NewMessage]:
        """Check all sessions for new assistant messages.

        Reads from last byte offset. Emits both intermediate
        (stop_reason=null) and complete messages.

        Args:
            active_session_ids: Set of session IDs currently in session_map
        """
        new_messages = []

        # Resolve available session files
        sessions = await asyncio.to_thread(
            self._resolve_codex_session_files_sync,
            active_session_ids,
        )

        # Only process sessions that are in session_map
        for session_info in sessions:
            if session_info.session_id not in active_session_ids:
                continue
            try:
                tracked = self.state.get_session(session_info.session_id)

                if tracked is None:
                    # For new sessions, initialize offset to end of file
                    # to avoid re-processing old messages
                    try:
                        file_size = session_info.file_path.stat().st_size
                        current_mtime = session_info.file_path.stat().st_mtime
                    except OSError:
                        file_size = 0
                        current_mtime = 0.0
                    tracked = TrackedSession(
                        session_id=session_info.session_id,
                        file_path=str(session_info.file_path),
                        last_byte_offset=file_size,
                    )
                    self.state.update_session(tracked)
                    self._file_mtimes[session_info.session_id] = current_mtime
                    logger.info(f"Started tracking session: {session_info.session_id}")
                    continue

                # Check mtime + file size to see if file has changed
                try:
                    st = session_info.file_path.stat()
                    current_mtime = st.st_mtime
                    current_size = st.st_size
                except OSError:
                    continue

                last_mtime = self._file_mtimes.get(session_info.session_id, 0.0)
                if (
                    current_mtime <= last_mtime
                    and current_size <= tracked.last_byte_offset
                ):
                    # File hasn't changed, skip reading
                    continue

                # File changed, read new content from last offset
                new_entries = await self._read_new_lines(
                    tracked, session_info.file_path
                )
                self._file_mtimes[session_info.session_id] = current_mtime

                if new_entries:
                    logger.debug(
                        f"Read {len(new_entries)} new entries for "
                        f"session {session_info.session_id}"
                    )

                # Parse new entries using the shared logic, carrying over pending tools
                carry = self._pending_tools.get(session_info.session_id, {})
                parsed_entries, remaining = TranscriptParser.parse_entries(
                    new_entries,
                    pending_tools=carry,
                )
                if remaining:
                    self._pending_tools[session_info.session_id] = remaining
                else:
                    self._pending_tools.pop(session_info.session_id, None)

                for entry in parsed_entries:
                    if not entry.text and not entry.image_data and not entry.event_type:
                        continue
                    new_messages.append(
                        NewMessage(
                            session_id=session_info.session_id,
                            text=entry.text,
                            is_complete=True,
                            content_type=entry.content_type,
                            tool_use_id=entry.tool_use_id,
                            role=entry.role,
                            tool_name=entry.tool_name,
                            source="transcript",
                            event_type=entry.event_type,
                            image_data=entry.image_data,
                            file_path=str(session_info.file_path),
                        )
                    )

                self.state.update_session(tracked)

            except OSError as e:
                logger.debug(f"Error processing session {session_info.session_id}: {e}")

        self.state.save_if_dirty()
        return new_messages

    async def _load_current_session_map(self) -> dict[str, str]:
        """Load current window->session_id mapping from in-memory state."""
        window_to_session: dict[str, str] = {}

        from .session import session_manager

        for window_id, session_id in session_manager.current_window_session_map().items():
            window_to_session.setdefault(window_id, session_id)
        return window_to_session

    async def _cleanup_all_stale_sessions(self) -> None:
        """Clean up tracked sessions not in the current window/session mapping."""
        current_map = await self._load_current_session_map()
        active_session_ids = set(current_map.values())

        stale_sessions = []
        for session_id in self.state.tracked_sessions.keys():
            if session_id not in active_session_ids:
                stale_sessions.append(session_id)

        if stale_sessions:
            logger.info(
                f"[Startup cleanup] Removing {len(stale_sessions)} stale sessions"
            )
            for session_id in stale_sessions:
                self.state.remove_session(session_id)
                self._file_mtimes.pop(session_id, None)
            self.state.save_if_dirty()

    async def _detect_and_cleanup_changes(self) -> dict[str, str]:
        """Detect mapping changes and clean up replaced/removed sessions.

        Returns current window/session mapping for further processing.
        """
        current_map = await self._load_current_session_map()

        sessions_to_remove: set[str] = set()

        # Check for window session changes (window exists in both, but session_id changed)
        for window_id, old_session_id in self._last_session_map.items():
            new_session_id = current_map.get(window_id)
            if new_session_id and new_session_id != old_session_id:
                logger.info(
                    "Window '%s' session changed: %s -> %s",
                    window_id,
                    old_session_id,
                    new_session_id,
                )
                sessions_to_remove.add(old_session_id)

        # Check for deleted windows (window in old map but not in current)
        old_windows = set(self._last_session_map.keys())
        current_windows = set(current_map.keys())
        deleted_windows = old_windows - current_windows

        for window_id in deleted_windows:
            old_session_id = self._last_session_map[window_id]
            logger.info(
                "Window '%s' deleted, removing session %s",
                window_id,
                old_session_id,
            )
            sessions_to_remove.add(old_session_id)

        # Perform cleanup
        if sessions_to_remove:
            for session_id in sessions_to_remove:
                self.state.remove_session(session_id)
                self._file_mtimes.pop(session_id, None)
            self.state.save_if_dirty()

        # Update last known map
        self._last_session_map = current_map

        return current_map

    async def _monitor_loop(self) -> None:
        """Background loop for checking session updates.

        Uses simple async polling with aiofiles for non-blocking I/O.
        """
        logger.info("Session monitor started, polling every %ss", self.poll_interval)

        # Deferred import to avoid circular dependency (cached once)
        from .session import session_manager

        # Prime autodiscovery for providers without hooks (e.g. Codex).
        await session_manager.autodiscover_sessions_for_bound_windows()
        # Clean up all stale sessions on startup
        await self._cleanup_all_stale_sessions()
        # Initialize last known session_map
        self._last_session_map = await self._load_current_session_map()

        while self._running:
            try:
                # Keep window->session map fresh even without hook support.
                await session_manager.autodiscover_sessions_for_bound_windows()

                # Detect session_map changes and cleanup replaced/removed sessions
                current_map = await self._detect_and_cleanup_changes()
                active_session_ids = set(current_map.values())

                # Check for new messages (all I/O is async)
                new_messages = await self.check_for_updates(active_session_ids)

                for msg in new_messages:
                    status = "complete" if msg.is_complete else "streaming"
                    preview = msg.text[:80] + ("..." if len(msg.text) > 80 else "")
                    logger.info("[%s] session=%s: %s", status, msg.session_id, preview)
                    if self._message_callback:
                        try:
                            callback_started = time.monotonic()
                            await self._message_callback(msg)
                            callback_elapsed = time.monotonic() - callback_started
                            if callback_elapsed > 2.0:
                                logger.warning(
                                    "Slow message callback: %.2fs (session=%s content_type=%s text_len=%d)",
                                    callback_elapsed,
                                    msg.session_id,
                                    msg.content_type,
                                    len(msg.text),
                                )
                        except Exception as e:
                            logger.error(f"Message callback error: {e}")

            except Exception as e:
                logger.error(f"Monitor loop error: {e}")

            await asyncio.sleep(self.poll_interval)

        logger.info("Session monitor stopped")

    def start(self) -> None:
        if self._running:
            logger.warning("Monitor already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        self.state.save()
        logger.info("Session monitor stopped and state saved")
