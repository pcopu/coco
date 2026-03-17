---
name: looper
description: Work continuously against a plan file and stop only when completion keyword is returned.
icon: "🔁"
---

# Looper App

Use this app when a recurring plan loop is active for the topic.

## Core behavior

- Treat the referenced plan markdown file as the source of truth.
- Continue executing tasks from that plan until all items are complete.
- Keep progress focused and avoid asking for confirmation unless blocked.
- When all work is complete, reply with exactly the configured completion keyword as a single word.

## Response style during loop

- For in-progress loop ticks, briefly summarize what was finished and what is next.
- Keep momentum and proceed to the next plan steps immediately.
- If blocked, state the blocker and the minimal unblock action.

## Completion

- Only emit the completion keyword when the plan is fully complete.
- Do not include extra words, punctuation, or formatting around the keyword.
