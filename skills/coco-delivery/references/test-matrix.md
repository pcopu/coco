# Test Matrix

Run the smallest useful set first, then full suite.

## Focused Runs

```bash
.venv/bin/pytest -q tests/coco/test_guidance_queue.py
.venv/bin/pytest -q tests/coco/test_session_lifecycle_command.py
.venv/bin/pytest -q tests/coco/test_session.py
.venv/bin/pytest -q tests/coco/test_codex_app_server.py
.venv/bin/pytest -q tests/coco/test_worktree_helpers.py
.venv/bin/pytest -q tests/coco/test_run_watchdog.py
.venv/bin/pytest -q tests/coco/test_status_polling_watchdog.py
.venv/bin/pytest -q tests/coco/test_status_native_fallback.py
```

## Full Validation

```bash
.venv/bin/pytest -q
```

## Quick Compile Check

```bash
.venv/bin/python -m py_compile src/coco/bot.py src/coco/session.py src/coco/codex_app_server.py
```

## Delivery Checklist

1. Update behavior and tests in same change.
2. Validate reactions/status transitions for in-progress and complete paths.
3. Validate fallback path if app-server branch fails.
4. Run focused tests for touched modules.
5. Run full test suite.
