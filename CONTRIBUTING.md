# Contributing

CoCo is a pragmatic Telegram overlay for real Codex sessions. Keep changes
small, test the behavior you touch, and prefer clear operational docs over
hand-wavy abstractions.

## Local Setup

```bash
uv sync --extra dev
```

## Common Checks

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
```

## Guidelines

- Keep user-facing behavior documented in [README.md](README.md) when it changes.
- Preserve the repo's Codex-first scope; avoid reintroducing generic assistant layers.
- Prefer targeted fixes over sweeping refactors unless the migration is intentional.
- Do not commit secrets, live tokens, or local state from `~/.coco`.
