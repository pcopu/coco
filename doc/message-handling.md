# Message Handling

## Message Queue Architecture

Per-user message queues plus a worker pattern are used for all send tasks:
- Messages are sent in receive order (FIFO)
- Status messages always follow content messages
- Multi-user concurrent processing without interference

**Message merging**: The worker automatically merges consecutive mergeable
content messages on dequeue:
- Content messages for the same window can be merged (including text, thinking)
- `tool_use` breaks the merge chain and is sent separately
- `tool_result` breaks the merge chain and is edited into the `tool_use` message
- Merging stops when combined length exceeds 3800 characters

## Status Message Handling

**Conversion**: The status message is edited into the first content message,
reducing message count:
- When a status message exists, the first content message updates it via edit
- Subsequent content messages are sent as new messages

**Polling**: A background task polls terminal status for all active windows at
1-second intervals. Send-layer rate limiting prevents flood control issues.

**Deduplication**: The worker compares `last_text` when processing status
updates; identical content skips the edit, reducing API calls.

## Rate Limiting

- `AIORateLimiter(max_retries=5)` on the Application (30/s global)
- On 429, `AIORateLimiter` pauses all concurrent requests and retries after the ban
- On restart, the global bucket is pre-filled to avoid burst against Telegram's persisted server-side counter
- Status polling interval: 1 second (skips enqueue when queue is non-empty)

## Performance Optimizations

**mtime cache**: The monitoring loop maintains an in-memory file mtime cache,
skipping reads for unchanged files.

**Byte offset incremental reads**: Each tracked session records
`last_byte_offset`, reading only new content. File truncation is detected and
the offset is auto-reset.

## No Message Truncation

Historical messages (tool-use summaries, tool-result text, user and assistant
messages) are always kept in full. Long text is handled exclusively at the send
layer: `split_message` splits by Telegram's 4096-character limit; real-time
messages get `[1/N]` text suffixes, and history pages get inline keyboard
navigation.
