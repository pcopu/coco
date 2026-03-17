# File Map

Use this map to find the right code surface quickly.

## Primary Bot Routing

- `src/coco/bot.py`
: Telegram command handlers, callback routing, image handling, app-server notification bridge.

- `src/coco/handlers/callback_data.py`
: Callback constants for inline keyboards.

## Session and Transport

- `src/coco/session.py`
: Topic/session state, send locks, and app-server send logic.

- `src/coco/codex_app_server.py`
: JSON-RPC client methods (`thread/*`, `turn/*`, account reads, notifications).

## Queue, Progress, and Watchdog

- `src/coco/handlers/message_queue.py`
: Per-user queue, progress/status updates, queued `/q` dock state.

- `src/coco/handlers/run_watchdog.py`
: No-response tracking, retry guardrails, idempotency fingerprints.

- `src/coco/handlers/status_polling.py`
: Polling loop, watchdog checkpoint emission, stale binding cleanup.

## Parsing and Rendering

- `src/coco/terminal_parser.py`
: Status/panel parsing for `/status` and interactive detection.

- `src/coco/handlers/response_builder.py`
: Streaming response part construction.

## Access Control and Approvals

- `src/coco/config.py`
: Runtime configuration and allowlist loading.

- `src/coco/bot.py`
: `/allowed`, `/approvals`, scope rules, admin checks.

## Tests

- `tests/coco/test_guidance_queue.py`
- `tests/coco/test_session.py`
- `tests/coco/test_session_lifecycle_command.py`
- `tests/coco/test_codex_app_server.py`
- `tests/coco/test_run_watchdog.py`
- `tests/coco/test_status_polling_watchdog.py`
- `tests/coco/test_status_native_fallback.py`
- `tests/coco/test_worktree_helpers.py`
