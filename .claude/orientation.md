<!-- State of play: 2-5 lines of narrative about where the project is headed -->
## State of Play

Epic kyj (strip and restructure) merged to main. All structural work complete. Remaining open items are investigations (worktree isolation, linter interference, SDK injection research), orchestrator size assessment (7yi), and a lower-priority feature (scheduler target_chat_ids). The bot is a personal executive dysfunction tool, not developer tooling.

<!-- System shape: architecture at a glance -->
## System Shape

```
Telegram -> PTB middleware (security -> auth -> rate limit)
  -> MessageOrchestrator (src/bot/orchestrator.py, ~1,813 lines)
    -> PersistentClientManager -> ClaudeSDKClient (long-lived subprocess per thread)
    -> Stream callbacks: src/bot/stream_handler.py (make_stream_callback, flush, cleanup)
    -> Message queuing: _enqueue_message / _drain_queue (busy path)
    -> Activity lifecycle: progress msg edited to Done/Failed, stall_callback from watchdog

Scheduler -> HTTP endpoints on FastAPI server -> Claude creates/lists/removes cron jobs via WebFetch
  -> APScheduler fires -> ScheduledEvent -> EventBus -> AgentHandler -> Claude -> NotificationService
```

One persistent client per Telegram thread (keyed by chat_id:thread_id or chat_id:user_id). State machine: idle -> busy -> draining (if injections occurred) -> idle.

External triggers: EventBus -> AgentHandler -> PersistentClientManager.

Voice/image: `src/bot/media/` — orchestrator instantiates handlers directly.

Dependencies injected via context.bot_data dict, wired in main.py.

<!-- Key couplings: change X -> must update Y -->
## Key Couplings

- `ClaudeSDKManager.build_options()` -> used by persistent client. Contains scheduler API prompt (conditional on API+scheduler+secret).
- `derive_state_key()` in persistent.py must match usage in orchestrator `_state_key()`.
- main.py initialization order: scheduler -> API server, persistent_manager depends on sdk_manager.
- Heartbeat loop in main.py calls `persistent_manager.cleanup_idle_clients()` every 5 min.
- AgentHandler uses PersistentClientManager with synthetic state keys (`webhook:{provider}:{id}`, `scheduled:{job_id}`).
- `src/bot/stream_handler.py` is the single source for stream callback logic. Orchestrator imports and calls `make_stream_callback(settings, ...)`.
- Response delivery goes through `_deliver_turn_result` (shared by agentic_text, _drain_queue, _handle_agentic_media_message). To change how responses are formatted, progress is finalised, or messages are sent — edit that one method, not the callers.
- Voice provider availability is checked in TWO places: `FeatureFlags.voice_messages_enabled` AND `orchestrator.agentic_voice` (inline `voice_key_available` block). Adding a new provider requires updating both. Bead 7yi tracks consolidating this.

<!-- Verify before trusting: claims that could be stale -->
## Verify Before Trusting

- AgentHandler rewire to PersistentClientManager tested via mocks only, not integration tested
- Scheduler API endpoints tested but not integration tested with running bot

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
| Where are voice/image handlers? | src/bot/media/ |
| Where is message queuing? | src/bot/orchestrator.py — _enqueue_message, _drain_queue, _combine_queued_messages |
| Where is response delivery? | src/bot/orchestrator.py — _deliver_turn_result (shared by all message types) |

<!-- Gotchas -->
## Gotchas

- PTB concurrent_updates must be True (bool), not integer — 1 means "1 at a time"
- _response_collector runs for the lifetime of the client, not per-turn
- Draining timeout is 120s — falls back to idle silently if no second ResultMessage
- Voice transcription uses Parakeet MLX locally, not cloud providers
- Scheduler jobs without target_chat_ids fall back to NOTIFICATION_CHAT_IDS (bead lcs)
- **Worktree isolation doesn't work** — agents modify main repo directly
