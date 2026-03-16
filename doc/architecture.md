# System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Telegram Bot (bot.py)                       │
│  - Topic-based routing: 1 topic = 1 binding = 1 session thread     │
│  - /history: paginated message history                             │
│  - /esc: interrupt active run                                      │
│  - Send text -> Codex app-server turn APIs                         │
│  - Forward assistant slash commands                                │
│  - Create sessions via directory browser in unbound topics         │
│  - Tool use -> tool result: edit message in-place                  │
│  - Interactive UI: AskUserQuestion / ExitPlanMode / Permission     │
│  - Per-user message queue + worker (merge, rate limit)             │
│  - MarkdownV2 output with auto fallback to plain text              │
├──────────────────────┬──────────────────────────────────────────────┤
│  markdown_v2.py      │  telegram_sender.py                         │
│  MD -> MarkdownV2    │  split_message (4096 limit)                 │
│  + expandable quotes │                                             │
├──────────────────────┴──────────────────────────────────────────────┤
│  terminal_parser.py                                                 │
│  - Detect interactive terminal UI states                           │
│  - Parse status line and panels                                    │
└──────────┬──────────────────────────────────────────────────────────┘
           │
           │ app-server notifications + transcript polling
           │
┌──────────┴──────────────┐    ┌──────────────────────────────────────┐
│  SessionMonitor         │    │  Codex App Server Client            │
│  (session_monitor.py)   │    │  (codex_app_server.py)              │
│  - Poll JSONL every 2s  │    │  - thread/* and turn/* calls        │
│  - Parse new lines      │    │  - app-server process manager       │
│  - Track pending tools  │    │  - active turn coordination         │
│  - Emit notifications   │    └──────────────────────────────────────┘
└──────────┬──────────────┘
           │
           ▼
┌────────────────────────┐
│  SessionManager        │
│  (session.py)          │
│  - topic_bindings_v2   │
│    (topic -> binding)  │
│  - codex_thread_id and │
│    active turn state   │
│  - message history     │
│    retrieval           │
└──────────┬─────────────┘
           │ reads
           ▼
┌────────────────────────┐
│  Codex Sessions        │
│  ~/.codex/sessions/    │
│  - session JSONL files │
└────────────────────────┘

┌────────────────────────┐
│  MonitorState          │
│  (monitor_state.py)    │
│  - byte offsets        │
│  - dedupe after restart│
└────────────────────────┘
```

Additional modules:
- `main.py` - CLI entry point
- `utils.py` - shared utilities for path resolution and atomic JSON writes
- `telemetry.py` - runtime telemetry helpers
- `skills.py` - app/skill discovery and resolution
- `telegram_memory.py` - session memory helpers

Handler modules (`handlers/`):
- `commands.py` - slash command handlers
- `message_sender.py` - `safe_reply` / `safe_edit` / `safe_send`
- `message_queue.py` - per-user queue + worker (merge, status dedup)
- `status_polling.py` - background status/watchdog polling
- `response_builder.py` - response pagination and formatting
- `interactive_ui.py` - AskUserQuestion / ExitPlan / Permission UI
- `directory_browser.py` - directory selection UI for new topics
- `cleanup.py` - topic state cleanup on close/delete
- `callback_data.py` - callback data constants

State files (`~/.coco/` or `$COCO_DIR/`):
- `state.json` - topic bindings + window state + display names + read offsets
- `monitor_state.json` - per-session poll progress (byte offset)

## Key Design Decisions

- Topic-centric routing: each Telegram topic maps to one persisted session binding.
- App-server transport only: no fallback send path in runtime.
- Transcript-driven monitoring: parser consumes JSONL output and emits incremental updates.
- Tool-use to tool-result pairing: edits original tool message in-place for continuity.
- MarkdownV2 with fallback: failed parse paths degrade to plain text instead of dropping output.
- Parse layer keeps full content; send layer handles platform length limits.
