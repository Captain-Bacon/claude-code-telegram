<!-- State of play: 2-5 lines of narrative about where the project is headed -->
## State of Play

Fork of RichardAtCT/claude-code-telegram (origin: Captain-Bacon). 65% of the codebase is dead weight from upstream — classic mode, feature registries, inline keyboards — none of it runs. Active work is on `feature/persistent-client-v2` (10 commits ahead of main): persistent Claude subprocess per Telegram thread with injection, interrupt, session survival. A major cleanup is planned — see epic `claude-code-telegram-kyj` and `bd ready` for tracked work.

The bot's purpose is NOT developer tooling. It's an executive dysfunction mitigation tool: brain dumps, tasks, reminders, working through issues via Telegram.

<!-- System shape: architecture at a glance -->
## System Shape

```
Telegram -> PTB middleware (security -> auth -> rate limit)
  -> MessageOrchestrator
    -> PersistentClientManager -> ClaudeSDKClient (long-lived subprocess per thread)
```

One persistent client per Telegram thread (keyed by chat_id:thread_id or chat_id:user_id). State machine: idle -> busy -> draining (if injections occurred) -> idle. Fire-and-forget facade exists but is unused and scheduled for removal.

Dependencies injected via context.bot_data dict, wired in main.py.

<!-- Key couplings: change X -> must update Y -->
## Key Couplings

- `ClaudeSDKManager.build_options()` -> used by both persistent client and fire-and-forget. Changes affect all Claude execution.
- `derive_state_key()` in persistent.py must match usage in orchestrator `_state_key()`.
- main.py initialization order: persistent_manager depends on sdk_manager, bot depends on both.
- Heartbeat loop in main.py calls `persistent_manager.cleanup_idle_clients()` every 5 min.
- Budget/cost params in build_options() are HARMFUL on subscription — actively change Claude behaviour. Scheduled for removal (claude-code-telegram-pa8).

<!-- Verify before trusting: claims that could be stale -->
## Verify Before Trusting

- Context window remaining indicator may show wrong numbers — uses single API call tokens not cumulative (claude-code-telegram-2qu)
- Activity indication (Working... messages) unreliable after duplicate message fix saga
- Classic mode handlers still in codebase but never execute — scheduled for removal
- SDK injection behaviour (what happens when you query() a busy client) is empirically observed, not documented — draining state is a safety net

<!-- Active risks -->
## Active Risks

- Uncommitted changes in persistent.py (stream event tracking) and core.py (NetworkError handling)
- Branch not merged to main
- Budget system actively degrading Claude's behaviour until removed
- Draining state relies on undocumented SDK behaviour with 120s timeout guess

<!-- What hasn't been decided -->
## Open Questions

- SDK injection: does claude-agent-sdk document/guarantee continuation ResultMessages for injected queries? (claude-code-telegram-9mb)
- How to expose scheduler to Claude so users can create cron jobs from conversation (claude-code-telegram-zus)
- Activity indication UX design — what should started/working/done/failed look like in Telegram? (claude-code-telegram-18q)

<!-- Quick lookups -->
## Quick Lookups

| Question | Where to look |
|----------|--------------|
| How are messages routed? | src/bot/orchestrator.py — agentic_text() |
| How does persistent client work? | src/claude/persistent.py — PersistentClientManager |
| How are SDK options built? | src/claude/sdk_integration.py — build_options() |
| Where are deps wired up? | src/main.py — initialization sequence |
| What work is tracked? | bd ready / bd show claude-code-telegram-kyj (epic) |
| What's the bot actually for? | Memory: project_bot_purpose.md |

<!-- Gotchas -->
## Gotchas

- PTB concurrent_updates must be True (bool), not integer — 1 means "1 at a time"
- _response_collector runs for the lifetime of the client, not per-turn
- Injection during busy state returns None from send_message() — caller must handle
- Draining timeout is 120s — falls back to idle silently if no second ResultMessage
- max_budget_usd in SDK options makes Claude ration itself — remove, do not set to high value
- Voice transcription uses Parakeet MLX locally, not cloud providers
