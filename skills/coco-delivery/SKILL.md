---
name: coco-delivery
description: "Implement and verify CoCo features end-to-end (Telegram commands, callbacks, app-server transport, queueing, watchdogs, approvals, and tests). Use when changing this repository's bot behavior and needing repo-specific file targets, workflows, and validation commands. NOT for generic Python tasks outside CoCo."
icon: "🚚"
metadata: { "openclaw": { "emoji": "🤖" } }
---

# CoCo Delivery Skill

Ship `CoCo` changes with a consistent workflow and repo-aware checks.

## When to Use

Use this skill when making behavior changes in this repository, especially for:

- slash commands and callback menus
- app-server send paths
- queueing, steer, and watchdog behavior
- session/worktree lifecycle flows
- status/usage/model panels
- allowlist and approval UX

## When NOT to Use

Do not use this skill for:

- unrelated Python projects
- one-off shell tasks not tied to `CoCo`
- docs-only edits with no bot/runtime behavior impact

## Workflow

1. Locate the flow entrypoints.
2. Patch the smallest safe surface first (helpers before handlers).
3. Add or update targeted tests.
4. Run focused tests for touched areas.
5. Run full test suite before finalizing.

Use `references/file-map.md` for file touchpoints and `references/test-matrix.md` for test commands.

## Guardrails

- Prefer `rg` for code discovery.
- Keep queue and status behavior thread/topic-safe.
- Keep queue and status behavior aligned with app-server flows.
- Add telemetry for new failure/retry branches.
- Avoid broad refactors while shipping feature requests.
