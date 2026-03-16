# App-Server-Only Migration Plan (Completed)

## Objective
- Move Codex session orchestration to app-server-only runtime.
- Keep topic-to-thread continuity across restarts.
- Remove compatibility layers that increased operational complexity.

## Completion Status
- Runtime is app-server-only (`COCO_RUNTIME_MODE=app_server_only`).
- Codex transport is app-server-only (`CODEX_TRANSPORT=app_server`).
- Topic bindings are canonical (`topic_bindings_v2` in `state.json`).
- Legacy fallback branches and compatibility shims were removed from core runtime paths.
- Screenshot feature and related callback paths were removed.

## Command Surface
- Supported core commands: `/folder`, `/resume`, `/q`, `/esc`, `/history`, `/status`, `/approvals`, `/allowed`, `/apps`, `/looper`, `/worktree`, `/restart`, `/model`, `/unbind`.
- Assistant slash commands continue forwarding through the active topic binding.

## Persistence
- `state.json` stores topic binding metadata and per-topic/session runtime state.
- `monitor_state.json` stores transcript read offsets for deduplicated notifications.
- Transcript discovery uses Codex session data (`~/.codex/sessions` by default).

## Validation
- Full test suite passed after compatibility removal and command-path cleanup.
- Runtime startup no longer depends on external terminal session bootstrap.

## Notes
- This document is now a completion artifact, not an active phased plan.

## Next-Phase Security/Onboarding Plan
- Add a guided local installer command: `coco install`.
- Installer must drive end-to-end bootstrap and hardening in one flow:
  - Validate environment preconditions (config dir writable, session path reachable, app-server command availability).
  - Collect/update core config (token, allowed users seed, browse root/group roots, runtime mode).
  - Initialize local operator auth secret for privileged local admin actions.
  - Open bootstrap claim window (short TTL, default 90s) and print one-time claim code.
  - Instruct operator to claim from Telegram private chat via `/bootstrap <code>`.
  - Confirm first super-admin claim and permanently close bootstrap state.
  - Refuse startup of privileged features until at least one super admin exists.
- Bootstrap security requirements:
  - Claim code must be cryptographically random, short-lived, single-use, and atomically consumed.
  - Claim endpoint is private-chat only, strictly rate-limited, and never logs raw claim codes.
  - Bootstrap window can only be opened locally and only when no super admin exists, unless explicitly forced in break-glass mode.
- Super-admin governance requirements:
  - Super-admin add/remove/list operations are local-only (`coco admin ...`), never via Telegram commands.
  - Changing super admins requires local operator authentication (device prompt using installer-configured secret).
  - Never allow removal of the final super admin.
  - Every super-admin mutation must be audited in local logs/state history.
- Group onboarding policy:
  - Default-deny for new groups (`pending` until approved).
  - Approval must happen inside the target group by a caller who is both:
    - super admin in CoCo role state, and
    - Telegram admin of that group.
- Doctor diagnostics policy:
  - Local-only command (`coco doctor`); remove Telegram `/doctor`.
  - Read-only by default; optional `--fix` for conservative, auditable repairs only.
  - Minimum checks: config validity, app-server connectivity, persisted state health, topic binding integrity.
- Interim risk-acceptance controls (while full auth model is deferred):
  - Restrict `/restart` command to super admins only.
  - Add auth-tamper monitoring for configured auth files (`COCO_AUTH_ENV_FILE` and `COCO_AUTH_META_FILE`).
  - On detected auth-file changes, immediately notify all super admins with timestamp and change summary.
  - Treat this as detection/response, not prevention, under full-access Codex mode.
  - Prefer monitor implementation outside the main bot process so alerts still fire across bot restarts/crashes.
