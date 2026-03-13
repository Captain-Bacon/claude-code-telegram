| Area | What | Why accepted | Robust alternative |
|------|------|--------------|-------------------|
| Persistent client | Draining state relies on undocumented SDK injection behaviour with 120s timeout guess | Works in practice; research complete (9mb notes), behaviour is genuinely unpredictable | Only fixable if SDK documents injection — monitor SDK releases |
| Orchestrator | _drain_queue duplicates ~80 lines of response handling from agentic_text | amv (orchestrator restructure) will extract shared delivery path | Extract _send_and_deliver helper called by both paths |
| Orchestrator | Document, voice, photo handlers still delete progress_msg instead of editing to final state | Lower-traffic paths, amv will unify | Apply same edit-to-final-state pattern as agentic_text |
| AgentHandler | Rewired to PersistentClientManager with mock-only testing | Import-time failures would be obvious; integration test needs running bot | Integration test the webhook/scheduled event path |
| AgentHandler | Synthetic state keys spawn new subprocess per event | Low event volume makes this acceptable short-term | Pool or reuse persistent clients for non-user events |
