# Documentation

Quick links to every doc in this project. Start with the [README](../README.md) for an overview and quick start.

## Architecture

| Document | Description |
|----------|-------------|
| [Architecture](architecture.md) | How the bot works end-to-end: request flow, persistent client state machine, stream handling, delivery pipeline. With diagrams. |
| [Architecture Draft](architecture-draft.md) | Prose-first design draft with confidence markers. The thinking behind the architecture doc. |

## Guides

| Document | Description |
|----------|-------------|
| [Setup & Installation](setup.md) | Prerequisites, authentication options, install methods, and first-run troubleshooting |
| [Configuration](configuration.md) | Full environment variable reference, feature flags, and Pydantic Settings internals |
| [Available Tools](tools.md) | The 16 tools Claude can use, allowlist/disallowlist config, and ToolMonitor behaviour |
| [Development](development.md) | Local dev setup, running tests, linting, project structure, and contribution workflow |
| [Scheduler](scheduler.md) | APScheduler cron jobs, API endpoints, event flow |
| [Systemd Setup](../SYSTEMD_SETUP.md) | Running the bot as a persistent systemd user service |

## Reference

| Document | Description |
|----------|-------------|
| [SDK Duplication Review](SDK_DUPLICATION_REVIEW.md) | Audit of `src/claude/` against the Claude Agent SDK -- what to simplify or remove |

## Repository-Level

| Document | Description |
|----------|-------------|
| [Contributing](../CONTRIBUTING.md) | How to contribute: branching, code standards, PR process |
| [Security Policy](../SECURITY.md) | Supported versions, vulnerability reporting, and security architecture |
| [Changelog](../CHANGELOG.md) | Release history following Keep a Changelog format |
