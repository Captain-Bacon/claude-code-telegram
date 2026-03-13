| Area | What | Why accepted | Robust alternative |
|------|------|--------------|-------------------|
| Persistent client | Draining state relies on undocumented SDK injection behaviour with 120s timeout guess | Works in practice; research complete (9mb notes), behaviour is genuinely unpredictable | Only fixable if SDK documents injection — monitor SDK releases |
| Context window | Shows wrong numbers (single API call tokens vs cumulative) AND context_window may be None | Not crashing, just inaccurate; fix designed but not implemented (2qu notes) | Fix token source + add fallback for missing modelUsage |
| Activity indication | No failure signal to user when Claude stalls/dies; watchdog only logs | Typing heartbeat masks the problem partially; design captured in 18q notes | Stall callback from persistent client to orchestrator |
| AgentHandler | Rewired to PersistentClientManager with mock-only testing | Import-time failures would be obvious; integration test needs running bot | Integration test the webhook/scheduled event path |
| AgentHandler | Synthetic state keys spawn new subprocess per event | Low event volume makes this acceptable short-term | Pool or reuse persistent clients for non-user events |
| Pre-commit | Beads hook chains pre-commit but no config file exists | PRE_COMMIT_ALLOW_NO_CONFIG=1 workaround; bead 005 tracks | Either create .pre-commit-config.yaml or patch beads hook |
