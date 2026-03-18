<!-- State of play: 2-5 lines of narrative about where the project is headed -->
## State of Play

Core bot stable — delivery pipeline hardened (69 tests), architecture documented with Mermaid diagrams (docs/architecture.md), strip-and-restructure epic complete. Media handlers now queue when Claude is busy (same as text). Scheduled jobs support per-job model selection. Remaining work is peripheral: stale docs (6vz), worktree isolation (9l0), SDK injection research (9mb).

<!-- System shape: architecture at a glance -->
## System Shape

Full architecture with diagrams: **docs/architecture.md**

```
Telegram -> PTB middleware (security -> auth -> rate limit)
  -> MessageOrchestrator (src/bot/orchestrator.py)
    -> Commands, handler registration, agentic_text, message queuing, thread routing
    -> src/bot/delivery.py: turn result formatting, image sending, make_stall_callback
    -> src/bot/media_handlers.py: document/photo/voice handlers
    -> PersistentClientManager -> ClaudeSDKClient (long-lived subprocess per thread)
    -> StreamSession class: src/bot/stream_handler.py (callable, flush, cleanup, state)
    -> HeartbeatPin: src/bot/utils/heartbeat_pin.py (pinned liveness message during turns)

Scheduler -> HTTP endpoints on FastAPI server -> Claude creates/lists/removes cron jobs via WebFetch
  -> APScheduler fires -> ScheduledEvent -> EventBus -> AgentHandler -> Claude -> NotificationService
```

One persistent client per Telegram thread (keyed by chat_id:thread_id or chat_id:user_id). State machine: idle -> busy -> draining (if injections occurred) -> idle. Orchestrator queues messages when busy — injection only used by AgentHandler (webhook/scheduler path).

External triggers: EventBus -> AgentHandler -> PersistentClientManager.

Voice/image: `src/bot/media_handlers.py` (Telegram-facing) delegates to `src/bot/media/` (processing).

Dependencies injected via context.bot_data dict, wired in main.py.

<!-- Key couplings: change X -> must update Y -->
## Key Couplings

- Three turn paths must stay in sync: `agentic_text`, `_drain_queue`, `_handle_media_message`. All three create HeartbeatPin (if enabled), StreamSession, stall callback. If adding something to one, add to all three.
- `make_stream_callback` returns `StreamSession` (callable class with properties), NOT `Optional[Callable]`. `deliver_turn_result` accesses `.text_was_sent` and `.flush_succeeded` as bool properties.
- `make_stall_callback(progress_msg)` in delivery.py is the single source for stall callbacks. Don't inline.
- `derive_state_key()` in persistent.py must match usage in orchestrator `_state_key()`.
- main.py initialization order: scheduler -> API server, persistent_manager depends on sdk_manager.
- AgentHandler uses PersistentClientManager with synthetic state keys (`webhook:{provider}:{id}`, `scheduled:{job_id}`).
- Response delivery goes through `deliver_turn_result` in `src/bot/delivery.py` (called from all three turn paths). To change how responses are formatted or sent — edit delivery.py, not callers.
- HeartbeatPin creation gated by `settings.enable_heartbeat_pin` in all three turn paths. Downstream code handles `heartbeat_pin=None`.

<!-- Verify before trusting: claims that could be stale -->
## Verify Before Trusting

- HeartbeatPin in group chats — compiles and passes tests but pin/unpin/delete permissions not tested against live Telegram groups
- AgentHandler rewire to PersistentClientManager tested via mocks only, not integration tested
- Scheduler API endpoints tested but not integration tested with running bot
- Architecture doc diagrams haven't been rendered with mmdc — valid Mermaid syntax but not visually verified

<!-- Active risks -->
## Active Risks

- Draining state relies on undocumented SDK behaviour with 120s timeout guess
- **Worktree agent isolation DOES NOT WORK** — agents write to main repo. Do not use `isolation: "worktree"`.
- **Linter/autoformatter modifies files between Edit reads and writes** — cause unknown, likely IDE

<!-- What hasn't been decided -->
## Open Questions

- None currently open

<!-- Quick lookups -->
## Quick Lookups

| Question | Where to look |
|----------|--------------|
| Full architecture with diagrams | docs/architecture.md — **extend this when working in undocumented subsystems** (see "What's not documented yet" section at bottom) |
| How are messages routed? | src/bot/orchestrator.py — agentic_text() |
| How does persistent client work? | src/claude/persistent.py — PersistentClientManager |
| How are SDK options built? | src/claude/sdk_integration.py — build_options() |
| Where are deps wired up? | src/main.py — initialization sequence |
| Where are scheduler endpoints? | src/api/scheduler_routes.py |
| What work is tracked? | bd ready / bd list --status=open |
| Stream callback class? | src/bot/stream_handler.py — StreamSession |
| Where are voice/image handlers? | src/bot/media_handlers.py (Telegram-facing), src/bot/media/ (processing) |
| Where is message queuing? | src/bot/orchestrator.py — _enqueue_message, _drain_queue |
| Where is response delivery? | src/bot/delivery.py — deliver_turn_result (shared by all turn paths) |
| Where is heartbeat pin? | src/bot/utils/heartbeat_pin.py — created in all three turn paths |
| Where are scheduler docs? | docs/scheduler.md |

<!-- Gotchas -->
## Gotchas

- PTB concurrent_updates must be True (bool), not integer — 1 means "1 at a time"
- _response_collector runs for the lifetime of the client, not per-turn
- Draining timeout is 120s — falls back to idle silently if no second ResultMessage
- Voice transcription uses Parakeet MLX locally, not cloud providers
- Scheduler jobs without target_chat_ids fall back to NOTIFICATION_CHAT_IDS (bead lcs)
- **Worktree isolation doesn't work** — agents modify main repo directly
- HeartbeatPin cleanup edits message to "Done" before deleting — if delete fails (no admin rights in groups), remnant says "Done" not a cryptic tool count
- task-done script fails when files are pre-staged via `git rm` — git add on deleted path errors
