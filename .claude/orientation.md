<!-- State of play: 2-5 lines of narrative about where the project is headed -->
## State of Play

Epic kyj (strip and restructure) merged to main. Structural work complete. Scheduler subsystem is fully built but disabled (needs ENABLE_API_SERVER, ENABLE_SCHEDULER, WEBHOOK_API_SECRET in .env). New HeartbeatPin feature added — pinned message showing live tool activity counter during turns — untested in production, no feature flag yet (bead ei8). User wants to enable the scheduler and discuss what recurring jobs to create. The bot is a personal executive dysfunction tool, not developer tooling.

<!-- System shape: architecture at a glance -->
## System Shape

```
Telegram -> PTB middleware (security -> auth -> rate limit)
  -> MessageOrchestrator (src/bot/orchestrator.py, ~1,242 lines)
    -> Commands, handler registration, agentic_text, message queuing, thread routing
    -> src/bot/delivery.py (~327 lines): turn result formatting, image sending, typing heartbeat
    -> src/bot/media_handlers.py (~333 lines): document/photo/voice handlers
    -> PersistentClientManager -> ClaudeSDKClient (long-lived subprocess per thread)
    -> Stream callbacks: src/bot/stream_handler.py (make_stream_callback, flush, cleanup)

Scheduler -> HTTP endpoints on FastAPI server -> Claude creates/lists/removes cron jobs via WebFetch
  -> APScheduler fires -> ScheduledEvent -> EventBus -> AgentHandler -> Claude -> NotificationService
```

One persistent client per Telegram thread (keyed by chat_id:thread_id or chat_id:user_id). State machine: idle -> busy -> draining (if injections occurred) -> idle.

External triggers: EventBus -> AgentHandler -> PersistentClientManager.

Voice/image: `src/bot/media_handlers.py` (Telegram-facing) delegates to `src/bot/media/` (processing).

Dependencies injected via context.bot_data dict, wired in main.py.

<!-- Key couplings: change X -> must update Y -->
## Key Couplings

- `ClaudeSDKManager.build_options()` -> used by persistent client. Contains scheduler API prompt (conditional on API+scheduler+secret).
- `derive_state_key()` in persistent.py must match usage in orchestrator `_state_key()`.
- main.py initialization order: scheduler -> API server, persistent_manager depends on sdk_manager.
- Heartbeat loop in main.py calls `persistent_manager.cleanup_idle_clients()` every 5 min.
- AgentHandler uses PersistentClientManager with synthetic state keys (`webhook:{provider}:{id}`, `scheduled:{job_id}`).
- `src/bot/stream_handler.py` is the single source for stream callback logic. Orchestrator imports and calls `make_stream_callback(settings, ...)`.
- Response delivery goes through `deliver_turn_result` in `src/bot/delivery.py` (called from orchestrator's agentic_text, _drain_queue, and media_handlers). To change how responses are formatted or sent — edit delivery.py, not callers.
- Media handler registration in orchestrator uses inline wrappers to bind settings — adding a new media handler requires a wrapper in `_register_agentic_handlers`.
- Voice provider availability: `FeatureFlags.voice_messages_enabled` is the single source of truth. `media_handlers.agentic_voice` calls it. Adding a new provider: update FeatureFlags only.

<!-- Verify before trusting: claims that could be stale -->
## Verify Before Trusting

- AgentHandler rewire to PersistentClientManager tested via mocks only, not integration tested
- Scheduler API endpoints tested but not integration tested with running bot
- HeartbeatPin (src/bot/utils/heartbeat_pin.py) compiles and passes type checks but has never run against Telegram — pin behaviour in private chat topics may be chat-wide not thread-scoped

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
| How are messages routed? | src/bot/orchestrator.py — agentic_text() |
| How does persistent client work? | src/claude/persistent.py — PersistentClientManager |
| How are SDK options built? | src/claude/sdk_integration.py — build_options() |
| Where are deps wired up? | src/main.py — initialization sequence |
| Where are scheduler endpoints? | src/api/scheduler_routes.py |
| What work is tracked? | bd ready / bd show claude-code-telegram-kyj (epic) |
| Where are stream callback functions? | src/bot/stream_handler.py (sole location) |
| Where are voice/image handlers? | src/bot/media_handlers.py (Telegram-facing), src/bot/media/ (processing) |
| Where is message queuing? | src/bot/orchestrator.py — _enqueue_message, _drain_queue, _combine_queued_messages |
| Where is response delivery? | src/bot/delivery.py — deliver_turn_result (shared by all message types) |
| Where is heartbeat pin? | src/bot/utils/heartbeat_pin.py — created in orchestrator, fed by stream_handler |
| Where are scheduler docs? | docs/scheduler.md — full reference for enabling and using the scheduler |

<!-- Gotchas -->
## Gotchas

- PTB concurrent_updates must be True (bool), not integer — 1 means "1 at a time"
- _response_collector runs for the lifetime of the client, not per-turn
- Draining timeout is 120s — falls back to idle silently if no second ResultMessage
- Voice transcription uses Parakeet MLX locally, not cloud providers
- Scheduler jobs without target_chat_ids fall back to NOTIFICATION_CHAT_IDS (bead lcs)
- **Worktree isolation doesn't work** — agents modify main repo directly
