| Area | What | Why accepted | Robust alternative |
|------|------|--------------|-------------------|
| CLAUDE.md | Describes system including parts scheduled for removal (handlers, features, facade) | Will be outdated after cleanup but accurate now; updating prematurely would confuse if cleanup doesn't happen | Update CLAUDE.md as part of orchestrator restructure (claude-code-telegram-amv) |
| Persistent client | Draining state relies on undocumented SDK injection behaviour with 120s timeout guess | Works in practice; research bead (claude-code-telegram-9mb) tracks investigation | Understand SDK guarantees, simplify or document as permanent |
| Context window | Shows wrong numbers (single API call tokens vs cumulative) | Not crashing, just inaccurate; tracked in claude-code-telegram-2qu | Fix data source in persistent client response builder |
