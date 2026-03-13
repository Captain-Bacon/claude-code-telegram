| Area | What | Why accepted | Robust alternative |
|------|------|--------------|-------------------|
| Persistent client | Draining state relies on undocumented SDK injection behaviour with 120s timeout guess | Works in practice; research complete (9mb notes), behaviour is genuinely unpredictable | Only fixable if SDK documents injection — monitor SDK releases |
| Activity indication | No failure signal to user when Claude stalls/dies; watchdog only logs | stall_callback now exists in persistent.py but not wired to orchestrator yet (18q) | Wire stall_callback in orchestrator to edit Telegram status message |
| Orchestrator | Stream handler functions duplicated — exist in both orchestrator.py and stream_handler.py | Functional but messy; inline imports within _make_stream_callback work | Finish extraction: make orchestrator delegate to make_stream_callback() |
| Orchestrator | Busy-path calls self._enqueue_message which doesn't exist — WILL CRASH | Must fix before running bot. Revert to injection path or implement method | Implement _enqueue_message and _drain_queue per ceq design |
| AgentHandler | Rewired to PersistentClientManager with mock-only testing | Import-time failures would be obvious; integration test needs running bot | Integration test the webhook/scheduled event path |
| AgentHandler | Synthetic state keys spawn new subprocess per event | Low event volume makes this acceptable short-term | Pool or reuse persistent clients for non-user events |
| Pre-commit | Beads hook chains pre-commit but no config file exists | PRE_COMMIT_ALLOW_NO_CONFIG=1 workaround; bead 005 tracks | Either create .pre-commit-config.yaml or patch beads hook |
