| Area | What | Why accepted | Robust alternative |
|------|------|--------------|-------------------|
| Persistent client | Draining state relies on undocumented SDK injection behaviour with 120s timeout guess | Works in practice; research complete (9mb notes), behaviour is genuinely unpredictable | Only fixable if SDK documents injection — monitor SDK releases |
| AgentHandler | Rewired to PersistentClientManager with mock-only testing | Import-time failures would be obvious; integration test needs running bot | Integration test the webhook/scheduled event path |
| AgentHandler | Synthetic state keys spawn new subprocess per event | Low event volume makes this acceptable short-term | Pool or reuse persistent clients for non-user events |
| HeartbeatPin | No feature flag — always on for private chats, no way to disable | New feature, needs live testing first before adding config surface | Add ENABLE_HEARTBEAT_PIN setting (bead ei8) |
| HeartbeatPin | Pin may be chat-wide not thread-scoped in topic chats | Untested — degrades gracefully (silently disables on error) | Verify against Telegram API, may need alternative approach for topics |
