# Multi-Machine Setup

This is the supported topology for multi-machine `CoCo`:

- one active Telegram-facing controller
- one or more non-Telegram agents
- private controller<->agent RPC over Tailscale
- no public ports exposed

The controller is the only process that needs the Telegram bot token. Agents do
not talk to Telegram directly.

## Roles

### Controller

Responsibilities:

- Telegram polling
- topic bindings
- machine/folder/session picker UX
- node registry and online/offline state
- routing prompts to the correct machine
- relaying agent events back to Telegram

### Agent

Responsibilities:

- local Codex app-server lifecycle
- local folder browsing
- local thread discovery and resume
- local attachment reads
- heartbeats to controller
- monitor-capable peer probes for stale-node verification

## Network model

Use Tailscale addresses or MagicDNS names only.

Recommended:

- controller binds RPC on its Tailscale IP or `100.x.y.z`
- agents bind RPC on their own Tailscale IP or `100.x.y.z`
- `COCO_CLUSTER_SHARED_SECRET` is set on every node
- no port is exposed on the public internet

## Controller environment

Controller needs normal Telegram auth plus cluster settings.

Example `~/.coco/.env` on the controller:

```ini
COCO_NODE_ROLE=controller

TELEGRAM_BOT_TOKEN=123456:telegram-bot-token
ALLOWED_USERS=123456789

COCO_MACHINE_ID=controller-main
COCO_MACHINE_NAME=Controller Main
COCO_TAILNET_NAME=controller-main.tailnet.ts.net

COCO_RPC_LISTEN_HOST=100.64.0.10
COCO_RPC_PORT=8787
COCO_RPC_ADVERTISE_HOST=100.64.0.10

COCO_CLUSTER_SHARED_SECRET=replace-with-a-long-random-secret

COCO_CONTROLLER_CAPABLE=true
COCO_CONTROLLER_ACTIVE=true
COCO_PREFERRED_CONTROLLER=true
```

Notes:

- `TELEGRAM_BOT_TOKEN` and `ALLOWED_USERS` are required only on the controller.
- `COCO_RPC_LISTEN_HOST` should be a Tailscale-reachable address, not `0.0.0.0`.
- `COCO_RPC_ADVERTISE_HOST` should be whatever agents can dial.

## Agent environment

Agent does not need Telegram credentials. It needs to know how to reach the
active controller.

Example `~/.coco/.env` on an agent:

```ini
COCO_NODE_ROLE=agent

COCO_MACHINE_ID=macbook-pro
COCO_MACHINE_NAME=MacBook Pro
COCO_TAILNET_NAME=macbook-pro.tailnet.ts.net

COCO_RPC_LISTEN_HOST=100.64.0.21
COCO_RPC_PORT=8787
COCO_RPC_ADVERTISE_HOST=100.64.0.21

COCO_CONTROLLER_RPC_HOST=100.64.0.10
COCO_CONTROLLER_RPC_PORT=8787
COCO_CLUSTER_SHARED_SECRET=replace-with-the-same-secret

COCO_CONTROLLER_CAPABLE=true
COCO_CONTROLLER_ACTIVE=false
COCO_PREFERRED_CONTROLLER=false
```

Notes:

- agent mode does not require `TELEGRAM_BOT_TOKEN`
- agent mode does not require `ALLOWED_USERS`
- `COCO_CONTROLLER_RPC_HOST` must point at the active controller

## Startup

Start the controller on the primary node:

```bash
uv run coco
```

Start each agent on its own machine:

```bash
uv run coco
```

The executable is the same. Runtime role is selected by `COCO_NODE_ROLE`.

## What appears in Telegram

With more than one registered machine:

1. `/folder` becomes `machine -> folder -> session`
2. `/resume` lists sessions for the topic's bound machine
3. `/status` shows the bound machine and online/offline state

If a machine goes offline:

- the topic binding remains intact
- sends are blocked clearly
- one offline notice is sent
- one recovery notice is sent when the machine returns

## Distributed monitor behavior in v1

`CoCo` now does a brokered stale-node probe before marking a remote machine
offline:

1. controller sees a node miss heartbeat timeout
2. controller selects an online monitor-capable worker
3. worker probes the stale node over cluster RPC
4. if probe succeeds, the node stays online
5. if probe fails, controller marks it offline and notifies bound topics

If no remote monitor-capable worker is available, the controller probes the
stale node directly.

This is not a general-purpose cross-machine job runner yet. It is the first
distributed monitor slice for machine liveness.

## Current limits

Implemented:

- controller/agent runtime split
- machine-aware `/folder`, `/resume`, `/status`
- remote send/resume/fork/rollback
- remote document attachments (`.pdf`, `.txt`, `.md`)
- offline/recovery notices
- stale-node peer probing

Not implemented yet:

- automatic controller failover
- automatic failback to preferred controller
- generic cross-machine monitor jobs
- project folder sync / handoff

## Recommended operating model

- keep one always-on server as the active controller
- run laptops/desktops as agents
- use Tailscale ACLs to limit which machines can reach the controller RPC port
- keep the cluster secret out of repo-managed files
