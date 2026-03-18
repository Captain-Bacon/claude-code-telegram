# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Telegram bot providing remote access to Claude Code. Python 3.10+, built with Poetry, using `python-telegram-bot` for Telegram and `claude-agent-sdk` for Claude Code integration. Personal tool for executive dysfunction mitigation (brain dumps, tasks, reminders), not developer tooling or community platform.

## Commands

```bash
make dev              # Install all deps (including dev)
make install          # Production deps only
make run              # Run the bot
make run-debug        # Run with debug logging
make test             # Run tests with coverage
make lint             # Black + isort + flake8 + mypy
make format           # Auto-format with black + isort

# Run a single test
poetry run pytest tests/unit/test_config.py -k test_name -v

# Type checking only
poetry run mypy src
```

## Architecture

### Claude SDK Integration

**`PersistentClientManager`** (`src/claude/persistent.py`) is the only Claude integration path. One long-lived `ClaudeSDKClient` subprocess per Telegram thread, with idle→busy→draining state machine, message injection mid-turn, interrupt support, and per-turn cost deltas.

`ClaudeSDKManager.build_options()` (`src/claude/sdk_integration.py`) builds SDK configuration (system prompt, allowed tools, MCP config).

**SDK injection is undocumented behaviour.** Calling `query()` on a busy client works because the CLI reads stdin continuously — not by design. The draining state handles the ambiguity of whether the CLI produces a continuation ResultMessage. The 120s drain timeout is a guess. See bead 9mb notes for full research.

**No hard timeout on send_message.** Turns can run 25+ minutes when subagents are involved. Stall detection is via the watchdog callback (30s/60s silence thresholds), not a timeout. Do NOT wrap send_message in asyncio.wait_for.

### Request Flow

**User messages:**

```
Telegram message -> Security middleware (group -3) -> Auth middleware (group -2)
-> Rate limit (group -1) -> MessageOrchestrator.agentic_text() (group 10)
-> PersistentClientManager.send_message() -> ClaudeSDKClient (long-lived)
-> Response streamed -> Sent back to Telegram
```

**External triggers** (webhooks, scheduler):

```
Webhook POST /webhooks/{provider} -> Signature verification -> Deduplication
-> Publish WebhookEvent to EventBus -> AgentHandler.handle_webhook()
-> PersistentClientManager.send_message() -> Publish AgentResponseEvent
-> NotificationService -> Rate-limited Telegram delivery
```

### Dependency Injection

Bot handlers access dependencies via `context.bot_data`:
```python
context.bot_data["auth_manager"]
context.bot_data["persistent_manager"]
context.bot_data["storage"]
context.bot_data["security_validator"]
```

### Key Directories

- `src/config/` -- Pydantic Settings v2 config, feature flags (`features.py`), YAML project loader (`loader.py`)
- `src/bot/orchestrator.py` -- MessageOrchestrator: handler registration, commands, agentic_text, message queuing, thread routing
- `src/bot/delivery.py` -- Response delivery: turn result formatting, image sending, context warnings, typing heartbeat
- `src/bot/media_handlers.py` -- Telegram-facing document/photo/voice handlers (routes to media/ for processing)
- `src/bot/media/` -- Voice transcription (Parakeet MLX), image processing
- `src/bot/middleware/` -- Auth, rate limit, security input validation
- `src/bot/utils/` -- Formatting (HTML escape, message chunking), error formatting
- `src/claude/` -- PersistentClientManager, SDK options builder, session management, tool monitoring
- `src/projects/` -- Multi-project support: `registry.py` (YAML config), `thread_manager.py` (Telegram topic sync)
- `src/storage/` -- SQLite via aiosqlite, repository pattern
- `src/security/` -- Auth (whitelist/token), input validators, rate limiter, audit logging
- `src/events/` -- EventBus (async pub/sub), event types, AgentHandler
- `src/api/` -- FastAPI webhook server
- `src/scheduler/` -- APScheduler cron jobs, SQLite persistence
- `src/notifications/` -- NotificationService, rate-limited Telegram delivery

### Security Model

5-layer defense: authentication (whitelist/token) -> directory isolation (APPROVED_DIRECTORY + path traversal prevention) -> input validation (blocks `..`, `;`, `&&`, `$()`, etc.) -> rate limiting (token bucket) -> audit logging.

`SecurityValidator` blocks access to secrets (`.env`, `.ssh`, `id_rsa`, `.pem`) and dangerous shell patterns. Can be relaxed with `DISABLE_SECURITY_PATTERNS=true` (trusted environments only).

`ToolMonitor` validates Claude's tool calls against allowlist/disallowlist, file path boundaries, and dangerous bash patterns. Tool name validation can be bypassed with `DISABLE_TOOL_VALIDATION=true`.

### Configuration

Settings loaded from environment variables via Pydantic Settings. Required: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME`, `APPROVED_DIRECTORY`. Key optional: `ALLOWED_USERS` (comma-separated Telegram IDs), `ANTHROPIC_API_KEY`, `ENABLE_MCP`, `MCP_CONFIG_PATH`.

Platform settings: `ENABLE_API_SERVER`, `API_SERVER_PORT` (default 8080), `GITHUB_WEBHOOK_SECRET`, `WEBHOOK_API_SECRET`, `ENABLE_SCHEDULER`, `NOTIFICATION_CHAT_IDS`.

Security relaxation (trusted environments only): `DISABLE_SECURITY_PATTERNS` (default false), `DISABLE_TOOL_VALIDATION` (default false).

Multi-project topics: `ENABLE_PROJECT_THREADS` (default false), `PROJECT_THREADS_MODE` (`private`|`group`), `PROJECT_THREADS_CHAT_ID` (required for group mode), `PROJECTS_CONFIG_PATH` (path to YAML project registry), `PROJECT_THREADS_SYNC_ACTION_INTERVAL_SECONDS` (default `1.1`, set `0` to disable pacing). See `config/projects.example.yaml`.

Output verbosity: `VERBOSE_LEVEL` (default 1, range 0-2). 0 = quiet, 1 = tool names + reasoning, 2 = detailed. Users override via `/verbose 0|1|2`. Typing indicator refreshes every ~2 seconds at all levels.

Voice transcription: `ENABLE_VOICE_MESSAGES` (default true), `VOICE_PROVIDER` (`mistral`|`openai`|`parakeet`, default `mistral`), `MISTRAL_API_KEY`, `OPENAI_API_KEY`, `PARAKEET_MODEL`. Parakeet runs locally on Apple Silicon via MLX — no API key needed. Implementation in `src/bot/media/voice_handler.py`.

Feature flags in `src/config/features.py` control: MCP, voice messages, API server, scheduler.

### DateTime Convention

All datetimes use timezone-aware UTC: `datetime.now(UTC)` (not `datetime.utcnow()`). SQLite adapters auto-convert TIMESTAMP/DATETIME columns to `datetime` objects via `detect_types=PARSE_DECLTYPES`. Model `from_row()` methods must guard `fromisoformat()` calls with `isinstance(val, str)` checks.

## Code Style

- Black (88 char line length), isort (black profile), flake8, mypy strict, autoflake for unused imports
- pytest-asyncio with `asyncio_mode = "auto"`
- structlog for all logging (JSON in prod, console in dev)
- Type hints required on all functions (`disallow_untyped_defs = true`)
- Use `datetime.now(UTC)` not `datetime.utcnow()` (deprecated)

## Adding a New Bot Command

Commands: `/start`, `/new`, `/status`, `/verbose`, `/repo`, `/model`, `/restart`, `/stop`. If `ENABLE_PROJECT_THREADS=true`: `/sync_threads`.

1. Add handler function in `src/bot/orchestrator.py`
2. Register in `MessageOrchestrator._register_agentic_handlers()`
3. Add to `MessageOrchestrator.get_bot_commands()` for Telegram's command menu
4. Add audit logging for the command

## Git Workflow

Fork of `RichardAtCT/claude-code-telegram`. Push to `origin` (Captain-Bacon), NOT `upstream`. Working on `main`. Pre-commit hook: beads auto-flushes issues.jsonl on commit. No other pre-commit framework in use.
