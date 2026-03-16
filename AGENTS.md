# Repository Memory Notes

## Auto Memory Log
- Memory log path: `TELEGRAM_CHAT_MEMORY.jsonl` (repo root).
- Purpose: store only Telegram-visible chat text (incoming message text/captions, outgoing sends, outgoing edits).
- This is intentionally not a full internal execution transcript.

## Session Start Prompt
Use this at the start of a new agent session in this repo:

```text
Load rolling Telegram memory from TELEGRAM_CHAT_MEMORY.jsonl.
Read the latest 200 lines and build a concise working-memory summary:
- active goals
- recent decisions/constraints
- unresolved tasks
- latest verified environment facts
Treat this log as source-of-truth for what was actually shown in Telegram chat.
```

## Runtime Notes
- Auto-writer is implemented in `src/coco/telegram_memory.py`.
- To override log location, set `COCO_TELEGRAM_MEMORY_LOG_PATH`.
