# Topic-Only Architecture

The bot runs exclusively in Telegram Forum topic mode. There is no non-topic
routing path. Every active conversation is anchored to a topic binding.

## 1 Topic = 1 Binding = 1 Session Thread

```
┌─────────────┐      ┌──────────────────┐      ┌─────────────┐
│  Topic ID   │ ───▶ │  Binding Record  │ ───▶ │  Thread ID  │
│  (Telegram) │      │  (state.json)    │      │  (Codex)    │
└─────────────┘      └──────────────────┘      └─────────────┘
```

## Mapping 1: Topic -> Binding (`topic_bindings_v2`)

```python
# session.py: SessionManager
# user_id -> {thread_id -> TopicBinding}
topic_bindings_v2: dict[int, dict[int, TopicBinding]]
```

- Storage: memory plus `state.json`
- Written when: user creates, resumes, or forks a session in a topic
- Purpose: route user messages and callbacks to the correct Codex thread

## Mapping 2: Binding -> Session Thread

Each binding stores at least:
- `window_id` (stable internal key)
- `display_name`
- `cwd`
- `codex_thread_id`
- `codex_active_turn_id`

This enables restart continuity and turn-level coordination.

## Message Flows

Outbound (user -> assistant):
```
User sends message in topic
  -> resolve topic binding
  -> turn/start or turn/steer via app-server
```

Inbound (assistant -> user):
```
SessionMonitor reads transcript/app-server events
  -> resolve topic by binding/thread id
  -> deliver message into the mapped topic
```

New topic flow:
- First message in unbound topic
- Directory browser selection
- Start thread and persist binding
- Forward pending message

Topic lifecycle:
- Closing or deleting a topic unbinds it and clears topic runtime state.
- Missing or invalid topics are cleaned during polling loops.

## Session Lifecycle

Startup validation:
- On startup, bindings are validated against persisted session data.
- Invalid thread ids are repaired or cleared without crashing the bot.

Runtime change detection:
- Poll loop tracks transcript growth and active-turn state.
- Watchdog and looper checks run per-topic and respect bound thread state.
