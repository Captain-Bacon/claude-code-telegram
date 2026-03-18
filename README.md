# Claude Code Telegram Bot

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

A personal Telegram bot that bridges your phone to [Claude Code](https://claude.ai/code) running on your machine. Talk to Claude about your projects from anywhere -- brain dump, ask questions, kick off tasks -- without needing a terminal open.

> Fork of [RichardAtCT/claude-code-telegram](https://github.com/RichardAtCT/claude-code-telegram). This fork is optimised for single-user personal use.

## What it does

Send a message on Telegram, Claude reads/edits/runs your code, you get the result back. Claude maintains context across messages -- it's the same persistent subprocess between turns, not a new session each time.

```
You: What's the test coverage looking like?

Bot: ⚙️ Bash 3
Bot: Working... (8s)
     📖 Read: pyproject.toml
     💻 Bash: poetry run pytest --cov
     💬 Coverage is at 72%...
Bot: [Full coverage report with suggestions]

You: Fix the two failing tests in test_delivery.py

Bot: ⚙️ Read 5
Bot: [Claude reads the tests, edits them, runs them, confirms they pass]
```

While Claude is working, you see a pinned message updating with live tool activity (⚙️ Read 7, ⚙️ Bash 12, etc.). If you send more messages while it's busy, they're queued and processed in order.

## Setup

### Prerequisites

- Python 3.10+
- [Claude Code CLI](https://claude.ai/code) installed and authenticated
- A Telegram bot token from [@BotFather](https://t.me/botfather)

### Install

```bash
git clone https://github.com/Captain-Bacon/claude-code-telegram.git
cd claude-code-telegram
make dev  # requires Poetry
```

### Configure

```bash
cp .env.example .env
```

**Required settings:**
```bash
TELEGRAM_BOT_TOKEN=your-bot-token
TELEGRAM_BOT_USERNAME=your_bot_name
APPROVED_DIRECTORY=/path/to/your/projects
ALLOWED_USERS=your-telegram-user-id
```

To find your Telegram user ID, message [@userinfobot](https://t.me/userinfobot).

### Run

```bash
make run          # Production
make run-debug    # With debug logging
```

## Commands

| Command | What it does |
|---------|-------------|
| `/start` | Start the bot |
| `/new` | Start a fresh Claude session (clears conversation context) |
| `/status` | Show current session info |
| `/verbose 0\|1\|2` | Control how much tool activity is shown (0=quiet, 1=normal, 2=detailed) |
| `/repo` | List repos in workspace, or `/repo name` to switch |
| `/model` | Switch Claude model |
| `/restart` | Restart the Claude subprocess |
| `/stop` | Interrupt Claude mid-turn |

## How it works

See [docs/architecture.md](docs/architecture.md) for the full picture with diagrams.

**The short version:** one persistent Claude CLI subprocess per chat thread, managed by a state machine (idle/busy/draining). Three message paths (text, queued, media) all converge on the same delivery pipeline. A pinned heartbeat message shows live tool activity. Streamed text is batched and delivered in real-time. Everything passes through security/auth/rate-limit middleware first.

## Configuration

### Key settings

```bash
# Claude
ANTHROPIC_API_KEY=sk-ant-...     # Optional if CLI is already authenticated
VERBOSE_LEVEL=1                  # 0=quiet, 1=normal, 2=detailed

# Liveness
ENABLE_HEARTBEAT_PIN=true        # Pinned message showing tool activity

# Voice (transcribes voice notes)
ENABLE_VOICE_MESSAGES=true       # Default: true
VOICE_PROVIDER=parakeet           # parakeet (local), mistral, or openai

# Security relaxation (trusted environments only)
DISABLE_SECURITY_PATTERNS=false  # Allow shell metacharacters
DISABLE_TOOL_VALIDATION=false    # Allow all Claude tools
```

### Optional subsystems

```bash
# Webhook server
ENABLE_API_SERVER=false
GITHUB_WEBHOOK_SECRET=...
WEBHOOK_API_SECRET=...

# Scheduler
ENABLE_SCHEDULER=false

# Project threads (Telegram forum topics per project)
ENABLE_PROJECT_THREADS=false
```

> Full reference: [docs/configuration.md](docs/configuration.md) and [`.env.example`](.env.example).

## Security

Defence-in-depth: whitelist authentication, directory sandboxing, input validation (blocks shell injection, path traversal, secret access), rate limiting, audit logging. See [SECURITY.md](SECURITY.md).

## Development

```bash
make dev           # Install all dependencies
make test          # Run tests with coverage (642 tests)
make lint          # Black + isort + flake8 + mypy
make format        # Auto-format code
```

## License

MIT -- see [LICENSE](LICENSE).

## Acknowledgments

- [Claude](https://claude.ai) by Anthropic
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot)
- Upstream: [RichardAtCT/claude-code-telegram](https://github.com/RichardAtCT/claude-code-telegram)
