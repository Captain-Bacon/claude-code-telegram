<!-- State of play: 2-5 lines of narrative about where the project is headed -->
## State of Play

Phase 1 cleanup complete on `cleanup/phase1-strip` — ~7,000 lines of dead code removed (facade, classic handlers, features directory, budget enforcement). 515 tests pass, 66% coverage. Next: orchestrator restructure (amv) which is the vehicle for fixing activity indication (18q) and context window (2qu). Scheduler wiring (zus) runs parallel. See epic `claude-code-telegram-kyj` for full plan.

The bot's purpose is NOT developer tooling. It's an executive dysfunction mitigation tool: brain dumps, tasks, reminders, working through issues via Telegram.

<!-- System shape: architecture at a glance -->
## System Shape

```
Telegram -> PTB middleware (security -> auth -> rate limit)
  -> MessageOrchestrator (src/bot/orchestrator.py, ~1,600 lines)
    -> PersistentClientManager -> ClaudeSDKClient (long-lived subprocess per thread)
```

One persistent client per Telegram thread (keyed by chat_id:thread_id or chat_id:user_id). State machine: idle -> busy -> draining (if injections occurred) -> idle.

External triggers: EventBus -> AgentHandler -> PersistentClientManager (rewired from old facade in Phase 1).

Voice/image: `src/bot/media/` — orchestrator instantiates handlers directly.

Dependencies injected via context.bot_data dict, wired in main.py.

<!-- Key couplings: change X -> must update Y -->
## Key Couplings

- `ClaudeSDKManager.build_options()` -> used by persistent client. Changes affect all Claude execution.
- `derive_state_key()` in persistent.py must match usage in orchestrator `_state_key()`.
- main.py initialization order: persistent_manager depends on sdk_manager, bot depends on both.
- Heartbeat loop in main.py calls `persistent_manager.cleanup_idle_clients()` every 5 min.
- AgentHandler now uses PersistentClientManager with synthetic state keys (`webhook:{provider}:{id}`, `scheduled:{job_id}`) — each creates a new subprocess instance.

<!-- Verify before trusting: claims that could be stale -->
## Verify Before Trusting

- Context window indicator shows wrong numbers — uses single API call tokens not cumulative, AND context_window from modelUsage may be None for persistent client (claude-code-telegram-2qu)
- Activity indication (Working... messages) unreliable — no failure signal, no stall notification to user, injection feedback is fire-and-forget (claude-code-telegram-18q)
- SDK injection behaviour is undocumented — draining state is empirically derived (claude-code-telegram-9mb, research complete, findings in bead notes)
- AgentHandler rewire to PersistentClientManager tested via mocks only, not integration tested

<!-- Active risks -->
## Active Risks

- Branch `cleanup/phase1-strip` not merged to `feature/persistent-client-v2` or `main`
- Draining state relies on undocumented SDK behaviour with 120s timeout guess
- `await future` in orchestrator has no timeout — if persistent client dies silently, orchestrator hangs forever with heartbeat ticking
- Synthetic state keys in AgentHandler each spawn a new Claude subprocess — resource implications unexamined

<!-- What hasn't been decided -->
## Open Questions

- Activity indication UX: what should started/working/done/failed look like? Design task, not just code. (claude-code-telegram-18q)
- Scheduler exposure: HTTP endpoints on FastAPI server decided as approach, but not implemented (claude-code-telegram-zus)

<!-- Quick lookups -->
## Quick Lookups

| Question | Where to look |
|----------|--------------|
| How are messages routed? | src/bot/orchestrator.py — agentic_text() |
| How does persistent client work? | src/claude/persistent.py — PersistentClientManager |
| How are SDK options built? | src/claude/sdk_integration.py — build_options() |
| Where are deps wired up? | src/main.py — initialization sequence |
| What work is tracked? | bd ready / bd show claude-code-telegram-kyj (epic) |
| Where are error formatting utils? | src/bot/utils/error_format.py (relocated from deleted handlers/) |
| Where are voice/image handlers? | src/bot/media/ (relocated from deleted features/) |

<!-- Gotchas -->
## Gotchas

- PTB concurrent_updates must be True (bool), not integer — 1 means "1 at a time"
- _response_collector runs for the lifetime of the client, not per-turn
- Injection during busy state returns None from send_message() — caller must handle
- Draining timeout is 120s — falls back to idle silently if no second ResultMessage
- Voice transcription uses Parakeet MLX locally, not cloud providers
- Pre-commit hook: beads chains it but no .pre-commit-config.yaml exists — use PRE_COMMIT_ALLOW_NO_CONFIG=1
- Injected message responses are silently discarded — user gets 👀 reaction but no confirmation their follow-up was processed
