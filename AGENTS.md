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

## Self-Update Safety
- CoCo self-update must treat only tracked or staged changes as a dirty worktree.
  Use `git status --porcelain --untracked-files=no` for this check.
- Untracked research, temporary files, and backups are user-owned artifacts. They
  must not block `/update`, and update code must never delete, stash, or clean them.
- Keep the local updater in `src/coco/bot.py` and the remote-node updater in
  `src/coco/agent_rpc.py` aligned when changing update safety behavior.
- Preserve regression coverage proving that untracked-only worktrees can update
  while modified tracked files still block. `git pull --ff-only` must remain the
  final guard against an incoming tracked path overwriting an untracked file.
