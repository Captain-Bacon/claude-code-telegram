<!-- State of play: 2-5 lines of narrative about where the project is headed -->
## State of Play

Phase 1 cleanup done, Phase 2 partially done on `cleanup/phase1-strip`. Context window fix (2qu) and scheduler wiring (zus) shipped — 537 tests pass. Next: orchestrator restructure (amv) carrying activity lifecycle (18q) and message queuing (ceq). This is the last big piece before the bot is usable end-to-end. See handover bead `claude-code-telegram-9t3` and epic `claude-code-telegram-kyj`.

The bot's purpose is NOT developer tooling. It's an executive dysfunction mitigation tool: brain dumps, tasks, reminders, working through issues via Telegram.

<!-- System shape: architecture at a glance -->
## System Shape

```
Telegram -> PTB middleware (security -> auth -> rate limit)
  -> MessageOrchestrator (src/bot/orchestrator.py, ~1,600 lines)
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

- `ClaudeSDKManager.build_options()` -> used by persistent client. Changes affect all Claude execution. Now also contains scheduler API prompt (conditional on API+scheduler+secret).
- `derive_state_key()` in persistent.py must match usage in orchestrator `_state_key()`.
- main.py initialization order: scheduler -> API server (scheduler passed to API), persistent_manager depends on sdk_manager, bot depends on both.
- Heartbeat loop in main.py calls `persistent_manager.cleanup_idle_clients()` every 5 min.
- AgentHandler uses PersistentClientManager with synthetic state keys (`webhook:{provider}:{id}`, `scheduled:{job_id}`) — each creates a new subprocess instance.

<!-- Verify before trusting: claims that could be stale -->
## Verify Before Trusting

- ~~Context window indicator~~ FIXED (2qu): uses last_input_tokens from collector, 200k fallback for context_window
- Activity indication (Working... messages) unreliable — no failure signal, no stall notification to user, injection feedback is fire-and-forget (claude-code-telegram-18q)
- SDK injection behaviour is undocumented — draining state is empirically derived (claude-code-telegram-9mb, research complete, findings in bead notes)
- AgentHandler rewire to PersistentClientManager tested via mocks only, not integration tested
- Scheduler API endpoints tested but not integration tested with running bot

<!-- Active risks -->
## Active Risks

- Branch `cleanup/phase1-strip` not merged to `feature/persistent-client-v2` or `main`
- Draining state relies on undocumented SDK behaviour with 120s timeout guess
- `await future` in orchestrator has no timeout — if persistent client dies silently, orchestrator hangs forever with heartbeat ticking
- Synthetic state keys in AgentHandler each spawn a new Claude subprocess — resource implications unexamined
- Worktree agents branch from old commits, not current HEAD — verify base or avoid for branch-sensitive work

<!-- What hasn't been decided -->
## Open Questions

- Activity indication UX: what should started/working/done/failed look like? (claude-code-telegram-18q)
- Message queuing UX details: placeholder visual (clock emoji? text?), re-post formatting (claude-code-telegram-ceq)

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
| Where are error formatting utils? | src/bot/utils/error_format.py (relocated from deleted handlers/) |
| Where are voice/image handlers? | src/bot/media/ (relocated from deleted features/) |

<!-- Gotchas -->
## Gotchas

- PTB concurrent_updates must be True (bool), not integer — 1 means "1 at a time"
- _response_collector runs for the lifetime of the client, not per-turn
- Injection during busy state returns None from send_message() — caller must handle (ceq will replace this with queuing)
- Draining timeout is 120s — falls back to idle silently if no second ResultMessage
- Voice transcription uses Parakeet MLX locally, not cloud providers
- Pre-commit hook: beads chains it but no .pre-commit-config.yaml exists — use PRE_COMMIT_ALLOW_NO_CONFIG=1
- Injected message responses are silently discarded — ceq design replaces this with queued delivery
- Scheduler jobs without target_chat_ids fall back to NOTIFICATION_CHAT_IDS (bead lcs tracks enhancement)
