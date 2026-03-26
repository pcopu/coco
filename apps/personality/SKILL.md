---
name: personality
description: Learn from yesterday's Coco sessions and send a short 9am fit report for the topic.
icon: "🧠"
---

# Personality App

Use this app when you want Coco to study yesterday's topic activity and send a short morning note about what worked, what did not, and what the user seems to prefer.

## Core behavior

- Read the Telegram-visible memory log for the prior day.
- Split activity into sessions based on idle gaps.
- Look for recurring interests, success signals, and frustration signals.
- Deliver a short topic-specific summary at 9am server time when there was activity yesterday.

## Notes

- This app is enabled per topic through `/apps`.
- The first implementation keeps the summary simple and grounded in visible Coco chat history.
