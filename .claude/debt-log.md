| Area | What | Why accepted | Robust alternative |
|------|------|--------------|-------------------|
| Persistent client | Draining state relies on undocumented SDK injection behaviour with 120s timeout guess | Works in practice; research complete (9mb notes), behaviour is genuinely unpredictable | Only fixable if SDK documents injection — monitor SDK releases |
| AgentHandler | Rewired to PersistentClientManager with mock-only testing | Import-time failures would be obvious; integration test needs running bot | Integration test the webhook/scheduled event path |
| AgentHandler | Synthetic state keys spawn new subprocess per event | Low event volume makes this acceptable short-term | Pool or reuse persistent clients for non-user events |
| HeartbeatPin | Pin may be chat-wide not thread-scoped in topic chats | Untested — degrades gracefully (silently continues unpinned on error) | Verify against Telegram API in live group with topics |
| Restart confirmation | Env vars survive os.execv but not process manager restarts | Bot runs in tmux via direct execution — os.execv is the restart path | SQLite flag or temp file if deployment model changes |
| Topic auto-adopt | Unmapped topics auto-adopt on first message with name "Untitled" — forum_topic_created attr unavailable on regular messages (bead `bzc`) | Works functionally; name is cosmetic, corrected when `/repo` aims the topic | Use Telegram `getForumTopicIconStickers` or `getChat` API to fetch real topic name |
| Security allowlist | File extension allowlist is designed for coding tool (upstream), not personal assistant | Image/doc types added; broader denylist model would be better fit | Switch to denylist of dangerous extensions instead of allowlist of safe ones |
