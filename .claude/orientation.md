<!-- State of play: 2-5 lines of narrative about where the project is headed -->
## State of Play

Orchestrator is in a BROKEN half-migrated state on `cleanup/phase1-strip`. Three things happened concurrently: ceq message queue (partially written), stall_callback (done, 3 test bugs), stream_handler extraction (partial — functions in two places). Priority: stabilise the orchestrator before adding anything new. See handover bead `claude-code-telegram-dii` for full damage report. Epic: `claude-code-telegram-kyj`.

The bot's purpose is NOT developer tooling. It's an executive dysfunction mitigation tool: brain dumps, tasks, reminders, working through issues via Telegram.

<!-- System shape: architecture at a glance -->
## System Shape

```
Telegram -> PTB middleware (security -> auth -> rate limit)
  -> MessageOrchestrator (src/bot/orchestrator.py, ~2,000 lines, HALF-MIGRATED)
    -> PersistentClientManager -> ClaudeSDKClient (long-lived subprocess per thread)

Scheduler -> HTTP endpoints on FastAPI server -> Claude creates/lists/removes cron jobs via WebFetch
  -> APScheduler fires -> ScheduledEvent -> EventBus -> AgentHandler -> Claude -> NotificationService
```

One persistent client per Telegram thread (keyed by chat_id:thread_id or chat_id:user_id). State machine: idle -> busy -> draining (if injections occurred) -> idle.

External triggers: EventBus -> AgentHandler -> PersistentClientManager (rewired from old facade in Phase 1).

Voice/image: `src/bot/media/` — orchestrator instantiates handlers directly.

Dependencies injected via context.bot_data dict, wired in main.py.

<!-- Key couplings: change X -> must update Y -->
## Key Couplings

- `ClaudeSDKManager.build_options()` -> used by persistent client. Changes affect all Claude execution. Contains scheduler API prompt (conditional on API+scheduler+secret).
- `derive_state_key()` in persistent.py must match usage in orchestrator `_state_key()`.
- main.py initialization order: scheduler -> API server (scheduler passed to API), persistent_manager depends on sdk_manager, bot depends on both.
- Heartbeat loop in main.py calls `persistent_manager.cleanup_idle_clients()` every 5 min.
- AgentHandler uses PersistentClientManager with synthetic state keys (`webhook:{provider}:{id}`, `scheduled:{job_id}`) — each creates a new subprocess instance.
- **NEW**: `src/bot/stream_handler.py` exports functions that orchestrator imports BOTH at top-level (unused) AND inline within `_make_stream_callback`. Must finish migration or revert.

<!-- Verify before trusting: claims that could be stale -->
## Verify Before Trusting

- **Orchestrator busy path CRASHES** — calls `self._enqueue_message()` which doesn't exist. Verify before running.
- Stream handler extraction is incomplete — orchestrator has BOTH its own `_make_stream_callback` AND imports from `stream_handler`. Functional but messy.
- 3 stall_callback watchdog tests fail (timing bug in tests, not production code)
- AgentHandler rewire to PersistentClientManager tested via mocks only, not integration tested
- Scheduler API endpoints tested but not integration tested with running bot

<!-- Active risks -->
## Active Risks

- **Orchestrator will crash on busy-path messages** until `_enqueue_message` is implemented or injection path is restored
- Branch `cleanup/phase1-strip` not merged to `feature/persistent-client-v2` or `main`
- Draining state relies on undocumented SDK behaviour with 120s timeout guess
- `await future` in orchestrator has no timeout — if persistent client dies silently, orchestrator hangs forever with heartbeat ticking
- **Worktree agent isolation DOES NOT WORK** — agents write to main repo. Do not use `isolation: "worktree"`.
- **Linter/autoformatter modifies files between Edit reads and writes** — cause unknown, likely IDE watching directory

<!-- What hasn't been decided -->
## Open Questions

- Activity indication UX: what should started/working/done/failed look like? (claude-code-telegram-18q)
- Message queuing UX details: placeholder visual, re-post formatting (claude-code-telegram-ceq)
- Stream handler: finish extraction or revert? (finish is recommended — module exists and works)

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
| Where are stream callback functions? | src/bot/stream_handler.py (extracted) AND src/bot/orchestrator.py (still inline) |
| Where are voice/image handlers? | src/bot/media/ (relocated from deleted features/) |

<!-- Gotchas -->
## Gotchas

- PTB concurrent_updates must be True (bool), not integer — 1 means "1 at a time"
- _response_collector runs for the lifetime of the client, not per-turn
- **Injection path is partially removed** — orchestrator calls _enqueue_message which doesn't exist. Will crash.
- Draining timeout is 120s — falls back to idle silently if no second ResultMessage
- Voice transcription uses Parakeet MLX locally, not cloud providers
- Pre-commit hook: beads chains it but no .pre-commit-config.yaml exists — use PRE_COMMIT_ALLOW_NO_CONFIG=1
- Scheduler jobs without target_chat_ids fall back to NOTIFICATION_CHAT_IDS (bead lcs tracks enhancement)
- **Worktree isolation doesn't work** — agents modify main repo directly, causing concurrent edit conflicts
