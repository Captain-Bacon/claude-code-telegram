# Scheduler — Cron & One-Shot Jobs

The scheduler lets Claude create and manage recurring (cron) and one-off (one-shot) jobs. When a job fires, Claude receives the prompt, runs it with full tool access, and sends the result to your Telegram chat.

## What it does

You tell Claude (via Telegram) something like "every weekday at 9am, check my task list and tell me if anything's stale." Claude creates a cron job. At 9am each weekday, the scheduler wakes Claude up with that prompt, Claude does the work, and the result appears in Telegram.

One-shot jobs fire once at a specific time and auto-delete. "Remind me at 3pm to call the dentist" — Claude creates a one-shot job, it fires at 3pm, Claude delivers the reminder, the job is cleaned up.

Jobs aren't limited to reminders. The prompt can be any instruction — check an inbox, progress something, summarise changes, run a review. Claude executes it as a full turn.

## How to enable

Three environment variables in `.env`:

```bash
ENABLE_API_SERVER=true
ENABLE_SCHEDULER=true
WEBHOOK_API_SECRET=your-secret-here   # any random string, e.g. openssl rand -hex 32
```

All three are required. The scheduler API runs on the same FastAPI server used for webhooks. The secret authenticates Claude's own HTTP calls to manage jobs.

Optionally set the API port (default 8080):

```bash
API_SERVER_PORT=8080
```

## How it works

**Startup:** Bot starts the FastAPI server and the scheduler. Persisted jobs are reloaded from SQLite and re-registered with APScheduler. One-shot jobs that were missed while offline are recovered (see "Resilience" below).

**Job creation:** When the scheduler is enabled, Claude's system prompt automatically includes API documentation for creating, listing, and removing jobs. Claude uses `WebFetch` to call these endpoints. No Telegram command needed — just ask Claude in natural language.

**Job execution flow:**

```
Trigger fires (cron or one-shot)
  -> JobScheduler publishes ScheduledEvent to EventBus
  -> AgentHandler.handle_scheduled() picks it up
  -> PersistentClientManager.send_message(prompt)
  -> Claude runs the prompt with full tool access
  -> Response published as AgentResponseEvent
  -> NotificationService delivers to target Telegram chats
```

**One-shot ack loop:** One-shot jobs track delivery. After Claude processes the job, AgentHandler publishes a `ScheduledJobOutcome` (success or failure). On success, the job is soft-deleted. On failure, an alert is written and the status is set to "failed" for recovery on next restart. Cron jobs don't use the ack loop — they fire repeatedly by design.

**Persistence:** Jobs are stored in the `scheduled_jobs` SQLite table. Removal is a soft-delete (`is_active = 0`). Jobs survive bot restarts.

## Resilience (one-shot jobs)

One-shot jobs go through a delivery lifecycle: pending -> fired -> delivered (auto-cleaned) or failed (visible via alert).

**Recovery on startup:** When the bot starts, the scheduler scans for one-shot jobs that were missed, unconfirmed, or failed. Jobs within the recovery window are re-fired. Jobs past the window are expired with an alert.

**Alert system:** When a one-shot job fails or expires, the scheduler writes an alert to `.claude/scheduler-alerts.md`. This file is @-included in CLAUDE.md, so any agent (terminal or Telegram) sees failures in its system prompt automatically — no need to look for them. Alerts are imperative briefings: they tell the agent what failed, how many attempts were made, and what to do about it.

### Behaviour constants

These live at the top of `src/scheduler/scheduler.py`:

| Constant | Value | What it controls |
|----------|-------|------------------|
| `MAX_ONE_SHOT_ATTEMPTS` | 3 | Maximum delivery attempts before the job is abandoned with an alert |
| `RECOVERY_WINDOW_HOURS` | 24 | How far back (in hours) the startup recovery will look for missed jobs. Jobs older than this are expired, not re-fired |

These are not configurable via environment variables — change them in the source if needed.

## API endpoints

Claude calls these automatically. Documented here for reference.

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/scheduler/jobs` | Create a job |
| `GET` | `/scheduler/jobs` | List active jobs with next run time |
| `DELETE` | `/scheduler/jobs/{job_id}` | Remove a job |

All require `Authorization: Bearer {WEBHOOK_API_SECRET}` header.

### Create job payload

**Cron (recurring):**

```json
{
  "name": "daily-inbox-check",
  "cron_expression": "0 9 * * 1-5",
  "prompt": "Check the inbox and progress anything that's ready",
  "description": "Optional description",
  "model": "haiku"
}
```

**One-shot (fire once):**

```json
{
  "name": "dentist-reminder",
  "run_at": "2026-03-21T15:00:00+00:00",
  "prompt": "Remind me to call the dentist",
  "priority": "high",
  "on_failure": "Tell the user the reminder failed and ask what to do",
  "relevance_hours": 6
}
```

Provide exactly one of `cron_expression` or `run_at`. Both or neither is rejected.

**Optional fields:**

| Field | Type | Purpose |
|-------|------|---------|
| `description` | string | Human context about the job |
| `model` | string | Model override (`"haiku"`, `"sonnet"`, `"opus"`) — defaults to bot config |
| `priority` | string | Alert priority if the job fails (default `"medium"`) |
| `on_failure` | string | Recovery instructions included in the alert if delivery fails |
| `relevance_hours` | int | How many hours after `run_at` this job is still worth acting on |

### Cron expression examples

| Expression | Schedule |
|-----------|----------|
| `0 9 * * 1-5` | Weekdays at 9am |
| `*/30 * * * *` | Every 30 minutes |
| `0 0 * * 0` | Weekly, Sunday midnight |
| `0 */4 * * *` | Every 4 hours |

## Key files

- `src/scheduler/scheduler.py` — JobScheduler class (APScheduler wrapper, DB persistence, recovery)
- `src/scheduler/alerts.py` — Workspace alert file system (write/clear/format)
- `src/api/scheduler_routes.py` — HTTP endpoints for job management
- `src/events/handlers.py` — AgentHandler.handle_scheduled() (fires Claude)
- `src/events/types.py` — ScheduledEvent, ScheduledJobOutcome
- `src/claude/sdk_integration.py` — System prompt injection
- `src/storage/database.py` — scheduled_jobs table schema and migrations
