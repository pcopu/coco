# Multi-Machine Controller Plan

## Objective

Extend `CoCo` from a single-host Telegram controller into a single-controller,
multi-machine system:

- one Telegram bot/controller process
- many machine agents
- one topic bound to `machine_id + cwd + codex_thread_id`
- stable behavior when agents are offline or reconnecting

This plan prioritizes stability and migration safety over speed.

## Non-Goals

- running the same Telegram bot token on multiple machines
- exposing public HTTP services
- replacing Codex app-server with raw SSH command streaming
- synchronized multi-client live terminal mirroring

## Requirements

1. Existing single-machine installs must keep working without manual migration.
2. Existing topic bindings must survive the introduction of `machine_id`.
3. `/folder` must become `machine -> folder -> session`.
4. Offline agents must not destroy or clear existing topic bindings.
5. Sending to an offline machine must fail clearly and safely.
6. Session discovery must be scoped by `(machine_id, cwd)`, not just `cwd`.
7. The controller must remain the only Telegram-facing runtime.
8. All new runtime paths need focused tests plus full-suite validation.

## Target Architecture

### Controller

Responsibilities:

- Telegram polling and callback handling
- persistent topic binding state
- node registry and heartbeat tracking
- machine/folder/session picker UX
- routing prompts to the correct machine agent
- receiving agent events and relaying Telegram-visible output

### Machine Agent

Responsibilities:

- local Codex app-server lifecycle
- local folder browsing under configured browse roots
- local Codex session discovery and resume
- local attachment reads for Telegram relay
- heartbeat + capability reporting to controller

### Network

Use Tailscale private connectivity between controller and agents.

Preferred pattern:

- controller listens only on Tailscale address or uses outbound-only polling RPC
- agents authenticate using per-node shared secret or Tailscale identity binding
- never expose agent RPC on public internet

## Persistence Model

### Node Registry

Add persistent node records with these fields:

- `machine_id`
- `display_name`
- `tailnet_name`
- `status`
- `last_seen_ts`
- `browse_roots`
- `capabilities`
- `agent_version`
- `transport`
- `is_local`

Recommended defaults:

- `machine_id`: explicit env/config override, else stable hostname-derived id
- `status`: `online` | `offline` | `degraded`
- `transport`: `local` or `agent_rpc`

### Topic Binding

Extend transport-neutral topic binding metadata with:

- `machine_id`
- `machine_display_name`
- `last_seen_session_id`

Binding identity becomes:

- `machine_id`
- `cwd`
- `codex_thread_id`

### Migration Contract

For existing bindings with no machine information:

1. Create or resolve one local node record.
2. Backfill all existing bindings to that local `machine_id`.
3. Preserve current `cwd`, `codex_thread_id`, `sync_mode`, and chat/topic mapping.
4. Do not rename topics or recreate sessions during migration.

Migration must be lazy-safe:

- reading old state should still work
- saving should normalize into the new schema
- mixed old/new persisted data must not break startup

## UX Plan

### `/folder`

New flow:

1. Pick machine
2. Pick folder on that machine
3. Pick prior session for that machine+folder, or start fresh

Machine picker should show:

- machine display name
- online/offline state
- last seen
- local/remote marker

Folder picker should be machine-aware:

- browse root resolved per machine
- selected machine stored in callback/panel state
- current topic binding shown in panel text

Session picker should show:

- session/thread id
- creation date
- last active date
- machine name
- `Resume`
- `Start Fresh`

### `/status`

Add machine visibility:

- bound machine
- machine status
- last seen
- local vs remote transport

If machine is offline:

- show clear offline indicator
- do not imply the topic is unbound

## Offline Rules

When a machine goes offline:

- keep the topic binding intact
- keep `cwd` and `codex_thread_id`
- mark machine/topic unavailable
- block new sends with a clear message
- allow `/folder` and `/resume` to rebind elsewhere

Do not:

- auto-clear the binding
- auto-reassign to another machine
- auto-queue arbitrary user prompts for later replay

When a machine returns:

- heartbeat updates node status
- existing bindings become live again
- resume/session controls work against the same bound machine

## Agent RPC Surface

Initial RPC methods:

- `node/heartbeat`
- `node/info`
- `browse/list`
- `session/list`
- `session/resume_latest`
- `session/resume`
- `turn/send`
- `thread/read`
- `attachment/read`

Event fanout from agent to controller:

- progress/final assistant messages
- approval requests
- turn started/completed
- transport errors

### RPC Principles

- versioned request/response payloads
- explicit machine id on every request
- idempotent heartbeat updates
- short request timeouts
- clear degraded/offline transitions

## Delivery Phases

### Phase 1: Machine-Aware State

Deliver:

- node registry model and persistence
- local machine auto-registration
- topic binding schema extended with `machine_id`
- migration from old bindings to local node
- machine-aware session discovery helpers

Acceptance:

- old state loads cleanly
- old bindings remain attached to same local folders/sessions
- tests cover mixed-schema persisted state

### Phase 2: Machine Picker UX

Deliver:

- `/folder` machine selection panel
- callback state for selected machine
- machine-aware folder browser
- machine-aware folder session picker

Acceptance:

- user can choose among multiple registered machines
- existing local-machine behavior remains simple when only one machine exists

### Phase 3: Offline Awareness

Deliver:

- heartbeat tracking
- node status rendering
- offline send guard
- machine availability shown in `/status` and `/folder`

Acceptance:

- topics remain bound while a machine is offline
- send attempts fail fast with clear user-facing reason

### Phase 4: Remote Agent RPC

Deliver:

- lightweight agent process
- authenticated controller-agent RPC
- remote browse/session/send primitives

Acceptance:

- remote machine can appear in `/folder`
- remote session resume works without shelling over raw SSH

### Phase 5: Remote Attachments and Progress

Deliver:

- remote attachment fetch/relay
- remote progress/final event forwarding
- remote approval request routing

Acceptance:

- images/docs from remote machine arrive in Telegram like local ones
- remote progress/final semantics match local app-server flow

## File Touchpoints

Primary controller changes:

- `src/coco/session.py`
- `src/coco/bot.py`
- `src/coco/config.py`
- `src/coco/handlers/directory_browser.py`
- `src/coco/handlers/callback_data.py`
- `src/coco/handlers/commands.py`

New likely files:

- `src/coco/node_registry.py`
- `src/coco/agent_client.py`
- `src/coco/agent_server.py`

Primary tests:

- `tests/coco/test_session.py`
- `tests/coco/test_directory_browser.py`
- `tests/coco/test_folder_session_picker.py`
- `tests/coco/test_start_command.py`
- new node-registry / agent-rpc tests

## Stability Guardrails

1. Keep local single-machine as the default path.
2. Make remote support additive, not invasive.
3. Never require a remote machine to be online just to read existing topic state.
4. Do not let remote/offline failures corrupt topic bindings.
5. Keep queue, watchdog, and host-follow logic machine-aware before enabling remote send.
6. Avoid broad refactors while introducing machine dimension.

## Recommended Build Order

1. node registry + migration
2. machine-aware binding helpers
3. machine picker in `/folder`
4. offline status and send guard
5. agent RPC skeleton
6. remote browse/session list
7. remote send/resume
8. remote attachments and progress relay

## Definition of Done

This plan is complete when:

- one controller can manage multiple machine records
- `/folder` is machine-aware
- bindings survive offline/online transitions
- session resume is scoped by machine + folder
- remote machines can browse, resume, send, and attach files through agents
- focused tests and full suite pass except for known pre-existing admin permission failures
