<!-- State of play: 2-5 lines of narrative about where the project is headed -->
## State of Play

Epic kyj (strip and restructure) is well advanced. Classic mode, facade, budget enforcement all removed. Message queuing (ceq) and activity lifecycle (18q) implemented — the two highest-value user-facing changes. Next logical step: orchestrator restructure (amv) which is now unblocked. The bot is a personal executive dysfunction tool, not developer tooling.

<!-- System shape: architecture at a glance -->
## System Shape

```
Telegram -> PTB middleware (security -> auth -> rate limit)
  -> MessageOrchestrator (src/bot/orchestrator.py, ~1,917 lines)
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

<!-- Verify before trusting: claims that could be stale -->
## Verify Before Trusting

- AgentHandler rewire to PersistentClientManager tested via mocks only, not integration tested
- Scheduler API endpoints tested but not integration tested with running bot

<!-- Active risks -->
## Active Risks

- Branch `cleanup/phase1-strip` not merged to `feature/persistent-client-v2` or `main`
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

<!-- Gotchas -->
## Gotchas

- PTB concurrent_updates must be True (bool), not integer — 1 means "1 at a time"
- _response_collector runs for the lifetime of the client, not per-turn
- Draining timeout is 120s — falls back to idle silently if no second ResultMessage
- Voice transcription uses Parakeet MLX locally, not cloud providers
- Scheduler jobs without target_chat_ids fall back to NOTIFICATION_CHAT_IDS (bead lcs)
- **Worktree isolation doesn't work** — agents modify main repo directly
- Document/voice/photo handlers still delete progress_msg instead of editing to final state (only agentic_text and _drain_queue use the new lifecycle)
