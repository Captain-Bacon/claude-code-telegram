| Area | What | Why accepted | Robust alternative |
|------|------|--------------|-------------------|
| Persistent client | Draining state relies on undocumented SDK injection behaviour with 120s timeout guess | Works in practice; research complete (9mb notes), behaviour is genuinely unpredictable | Only fixable if SDK documents injection — monitor SDK releases |
| AgentHandler | Rewired to PersistentClientManager with mock-only testing | Import-time failures would be obvious; integration test needs running bot | Integration test the webhook/scheduled event path |
| AgentHandler | Synthetic state keys spawn new subprocess per event | Low event volume makes this acceptable short-term | Pool or reuse persistent clients for non-user events |
| Voice handler | Provider availability duplicated in media_handlers.agentic_voice and FeatureFlags.voice_messages_enabled (bead 0cz) | Already caused one bug (parakeet missing), fixed | Use FeatureFlags as single source; media_handlers calls it instead of inline check |
| Orchestrator | /repo and cd: callback reference claude_integration from bot_data — never wired in main.py, session resume silently does nothing (bead lqj) | No crash, just silent no-op | Either wire to PersistentClientManager or remove dead path |
