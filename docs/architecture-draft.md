# Architecture — Draft Prose

> This is the "draft prose" step of the design methodology loop. It captures intent and how things actually work right now. Confidence markers: **[VERIFIED]** = read the code this session and/or wrote tests for it. **[READ]** = read the code but didn't deeply verify. **[INFERRED]** = understood from context, comments, and adjacent code but didn't trace every line.

## What the bot is

**[VERIFIED]** A personal tool. One user, one bot, running on a Mac Mini. It's a bridge between Telegram (where the user is, usually on their phone) and Claude Code (which needs a terminal). The user has ADHD and executive dysfunction — the bot exists so they can brain-dump, ask questions, kick off tasks, and get reminders without needing to sit at a computer and open a terminal.

It is NOT a developer tool, not a community platform, not multi-tenant. There's one allowed user. The security model exists because the bot has access to the filesystem and runs shell commands — it's about preventing accidents and blocking bad actors who might find the bot, not about managing a team.

## The core loop: message in, Claude turn, response out

**[VERIFIED]** Everything flows through three paths that all converge on the same delivery pipeline:

**Path 1 — Direct text messages** (`orchestrator.agentic_text`)
User sends a Telegram message. It passes through three middleware layers in PTB handler groups: security validation (group -3), authentication (group -2), rate limiting (group -1). Then hits the orchestrator at group 10. The orchestrator finds or creates a persistent Claude subprocess for that chat thread, sends the message, streams the response back.

**Path 2 — Queued messages** (`orchestrator._drain_queue`)
**[VERIFIED]** If the user sends messages while Claude is busy, `agentic_text` checks client state and queues them as `QueuedMessage` objects in `_message_queues` (keyed by state_key). Each gets a placeholder Telegram message ("Queued (N ahead)"). `_drain_queue` is called at the end of `agentic_text` after the turn completes — queued messages do NOT re-enter middleware (already authenticated on arrival). The drain loop: pop the queue, delete placeholders, combine messages with timestamps, send the combined text to Claude as a new turn. If more messages arrive during the drain turn, they get queued again and drained on the next iteration.

**Important distinction**: the orchestrator queues, it never injects. The injection mechanism in `PersistentClientManager.send_message()` (calling `query()` on a busy client) exists for the webhook/scheduler `AgentHandler` path, which calls `send_message` directly without checking state first.

**Path 3 — Media** (`media_handlers._handle_media_message`)
**[READ]** Voice notes get transcribed (Parakeet MLX locally on Apple Silicon, or Mistral/OpenAI APIs). Photos get base64-encoded and sent as multimodal content. Documents get processed. The result is text (or text + images) that gets sent to Claude just like a normal message.

**[VERIFIED]** All three paths create the same set of things before sending to Claude:
- A **progress message** ("Working...") that gets edited during the turn
- A **HeartbeatPin** (if `ENABLE_HEARTBEAT_PIN=true`) — a pinned message showing live tool activity (e.g. "⚙️ Read 7") so the user can see the bot is alive without scrolling. Throttled to one edit per 5 seconds. If pin fails (no admin rights), continues unpinned. If send fails, disables itself.
- A **StreamSession** — the stream callback that receives every event from the Claude subprocess and routes it: tool calls to the heartbeat and tool log, text to Telegram as batched messages, thinking blocks as ephemeral messages that get deleted after
- A **stall callback** (`make_stall_callback`) — fires when the watchdog detects silence, edits the progress message with a warning

**[VERIFIED]** All three paths call **`deliver_turn_result`** when the turn finishes. This is the single delivery pipeline.

## The persistent client — one subprocess per thread

**[VERIFIED]** `PersistentClientManager` keeps one long-lived `ClaudeSDKClient` subprocess per Telegram thread (keyed by `derive_state_key`: chat_id:thread_id for topic chats, chat_id:user_id for private/non-threaded). The subprocess stays alive between turns — this is what gives Claude its memory across messages.

Each client has a `_response_collector` background task that runs for the client's lifetime, reading every message from the CLI's stdout and routing it.

**[VERIFIED]** State machine: **idle → busy → draining → idle**.

- **Idle**: waiting for a message. `send_message()` creates a `TurnContext` with a response future, transitions to busy, calls `client.query()`, then awaits the future.
- **Busy**: processing a turn. The `_response_collector` reads messages, streams them to the `StreamSession` callback, and waits for a `ResultMessage`. When it arrives, `_handle_result_message` builds a `PersistentResponse`, resolves the future, and decides the next state.
- **Draining**: entered when injections occurred during a busy turn (`_injection_count > 0`). The CLI processes injected messages as a second internal turn with its own `ResultMessage`. The draining state waits for that second `ResultMessage` with a 120-second timeout (`_INJECTION_DRAIN_TIMEOUT_S`). If it arrives, transition to idle. If timeout fires, fall back to idle silently.

**[VERIFIED]** Injection: when `send_message()` is called while the client is busy or draining, it calls `client.query()` which injects the message into the running CLI. This works because the CLI reads stdin continuously. `send_message()` returns `None` for injections — the orchestrator's queue handles the user-facing response.

**[VERIFIED]** Lifecycle management:
- Session IDs are saved on idle cleanup so they can be resumed on reconnect (`_saved_sessions`)
- Idle clients are cleaned up after 30 minutes (`_DEFAULT_IDLE_TIMEOUT_S`)
- Stall watchdog fires at 30s, then every 60s, checking if the CLI subprocess is alive
- Interrupt sends `client.interrupt()` with a 10-second timeout safety net — if no `ResultMessage` comes, the turn is resolved as interrupted

**[INFERRED]** The `_response_collector` also handles client death — if the receive loop throws, it resolves any pending futures with an error and the orchestrator can retry.

## Stream handling during a turn

**[VERIFIED]** When Claude is working, the `_response_collector` streams messages to the `StreamSession` callback:

1. **Tool calls** — StreamSession logs them in the tool_log (for the progress message), notifies HeartbeatPin (which updates its pinned message text to "⚙️ ToolName N"), and optionally forwards to a DraftStreamer (live typing preview in private chats, gated by `ENABLE_STREAM_DRAFTS`).

2. **Assistant text** (Claude's intermediate commentary) — batched in `_pending_text` with a 1.5-second window (`_TEXT_BATCH_WINDOW`). A `_schedule_flush` task fires after the window, sending accumulated text as persistent Telegram messages. The `_stream_lock` protects the shared state; the `_send_lock` serialises network I/O so concurrent flushes don't interleave.

3. **Thinking blocks** — batched in `_pending_thinking`, sent as ephemeral Telegram messages (prefixed with 🧠). Message IDs are tracked in `_thinking_message_ids` for cleanup after the turn.

4. **MCP image interception** — if `mcp_images` list and `approved_directory` are provided, tool calls named `send_image_to_user` (or `*__send_image_to_user`) get intercepted. The file path is validated and the image is captured for delivery.

5. **Progress message edits** — on an 8-second throttle, showing tool activity via `_format_verbose_progress`. BUT if HeartbeatPin has an active message (`has_active_message`), progress edits are skipped entirely — the pin IS the liveness signal.

6. **Secret redaction** — tool inputs are scanned by `_redact_secrets` before being shown in progress messages. Covers API keys, AWS keys, auth tokens, connection strings, Bearer/Basic headers.

## The delivery pipeline

**[VERIFIED]** `deliver_turn_result` handles the end of every turn:

1. Flushes any remaining batched text from StreamSession (`flush_stream_callback`)
2. Checks `text_was_sent` and `flush_succeeded`:
   - If text was streamed AND flush succeeded → skip re-sending (avoids duplicates)
   - If text was streamed BUT flush failed → resend full response as safety net
   - If nothing was streamed → format and send normally
3. Formats the response via `ResponseFormatter` (HTML with code blocks, chunked to Telegram's 4096-char limit)
4. Appends notices: `[Interrupted]` flag, abnormal stop reasons (max_tokens, max_turns, budget_exceeded), context window warnings (threshold-based, deduplicated via user_data)
5. Finalises the progress message to "✅ Done (Xs)" or "❌ Failed (Xs)" — if edit fails, deletes it
6. Image delivery with caption optimisation (single image + short text ≤1024 chars = combined photo message)
7. HTML send failure → plain text fallback → error message to user
8. Cleans up ephemeral thinking messages

**[VERIFIED]** The `_send_text` fallback in StreamSession sends only the unsent remainder (from the failed message onwards), not the entire batch — prevents duplicate delivery of already-sent chunks.

## Middleware chain

**[VERIFIED]** PTB handler groups, lower number = runs first:

| Group | What | Effect |
|-------|------|--------|
| -3 | `security_middleware` | Validates inputs — blocks shell metacharacters, path traversal, secret access patterns. Raises `ApplicationHandlerStop` to prevent further processing if rejected. |
| -2 | `auth_middleware` | Checks user ID against `ALLOWED_USERS` whitelist (and optional token auth). |
| -1 | `rate_limit_middleware` | Token bucket rate limiter per user. |
| 10 | Orchestrator handlers | Commands and `agentic_text` message handler. |

**[READ]** Security can be relaxed with `DISABLE_SECURITY_PATTERNS=true`. Tool validation can be bypassed with `DISABLE_TOOL_VALIDATION=true`. Both are for trusted environments only.

## Configuration and feature flags

**[VERIFIED]** Pydantic Settings v2 loads from environment variables (`.env` file). `FeatureFlags` class in `features.py` wraps the settings with computed properties.

Key feature flags:
- `ENABLE_HEARTBEAT_PIN` (default true) — pinned liveness message during turns
- `VERBOSE_LEVEL` (0-2) — controls tool activity display (0=quiet, 1=tool names, 2=tool names + inputs)
- `ENABLE_STREAM_DRAFTS` — live typing preview via Telegram's sendMessageDraft API
- `ENABLE_MCP` — Model Context Protocol for external tool servers (requires `MCP_CONFIG_PATH`)
- `ENABLE_PROJECT_THREADS` — topic-per-project routing in Telegram forums
- `ENABLE_API_SERVER` + `ENABLE_SCHEDULER` — webhook and cron subsystems
- `ENABLE_VOICE_MESSAGES` + `VOICE_PROVIDER` — voice transcription (parakeet/mistral/openai)

## Dependencies and wiring

**[READ]** Dependencies are injected via `context.bot_data` dict, wired in `main.py`:
- `persistent_manager` — PersistentClientManager
- `auth_manager` — authentication
- `storage` — SQLite via aiosqlite
- `security_validator` — input validation

**[INFERRED]** Initialisation order in main.py: SDK manager → persistent client manager → scheduler → API server.

## What's not covered here

- **Webhook/scheduler/notification subsystem** — built and working but disabled in normal use. Separate request flow via EventBus → AgentHandler → PersistentClientManager with synthetic state keys.
- **Classic mode** — upstream legacy, not used by this fork's user.
- **Multi-project thread management** — works but tangential to the core loop.
- **Storage layer** — SQLite with repository pattern, session persistence, cost tracking. Leaf node, not core architecture.
- **Voice transcription details** — Parakeet MLX is the interesting bit (local, no API key, Apple Silicon), but it's a processing leaf not core architecture.
