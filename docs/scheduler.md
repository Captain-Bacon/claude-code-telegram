# Scheduler & Cron Jobs

The scheduler lets Claude create and manage recurring jobs that fire on a cron schedule. When a job fires, Claude receives the prompt, runs it with full tool access, and sends the result to your Telegram chat.

## What it does

You tell Claude (via Telegram) something like "every weekday at 9am, check my task list and tell me if anything's stale." Claude creates a cron job. At 9am each weekday, the scheduler wakes Claude up with that prompt, Claude does the work, and the result appears in Telegram.

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

**Startup:** Bot starts the FastAPI server and the scheduler. Persisted jobs are reloaded from SQLite and re-registered with APScheduler.

**Job creation:** When the scheduler is enabled, Claude's system prompt automatically includes API documentation for creating, listing, and removing jobs. Claude uses `WebFetch` to call these endpoints. No Telegram command needed — just ask Claude in natural language.

**Job execution flow:**

```
Cron trigger fires
  → JobScheduler publishes ScheduledEvent to EventBus
  → AgentHandler.handle_scheduled() picks it up
  → PersistentClientManager.send_message(prompt)
  → Claude runs the prompt with full tool access
  → Response published as AgentResponseEvent
  → NotificationService delivers to target Telegram chats
```

**Persistence:** Jobs are stored in the `scheduled_jobs` SQLite table. Removal is a soft-delete (`is_active = 0`). Jobs survive bot restarts.

## API endpoints

Claude calls these automatically. Documented here for reference.

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/scheduler/jobs` | Create a job |
| `GET` | `/scheduler/jobs` | List active jobs with next run time |
| `DELETE` | `/scheduler/jobs/{job_id}` | Remove a job |

All require `Authorization: Bearer {WEBHOOK_API_SECRET}` header.

### Create job payload

```json
{
  "name": "daily-inbox-check",
  "cron_expression": "0 9 * * 1-5",
  "prompt": "Check the inbox and progress anything that's ready",
  "description": "Optional description",
  "model": "haiku"
}
```

The `model` field is optional. When set, the job runs on that specific Claude model instead of the bot's default. Useful for routine tasks that don't need the most capable model — e.g. categorising emails with Haiku rather than waking up Opus.

Valid values are model identifiers like `"haiku"`, `"sonnet"`, `"opus"`. When omitted, the job uses whatever model the bot is configured with.

### Cron expression examples

| Expression | Schedule |
|-----------|----------|
| `0 9 * * 1-5` | Weekdays at 9am |
| `*/30 * * * *` | Every 30 minutes |
| `0 0 * * 0` | Weekly, Sunday midnight |
| `0 */4 * * *` | Every 4 hours |

## Key files

- `src/scheduler/scheduler.py` — JobScheduler class (APScheduler wrapper, DB persistence)
- `src/api/scheduler_routes.py` — HTTP endpoints for job management
- `src/events/handlers.py` — AgentHandler.handle_scheduled() (fires Claude)
- `src/claude/sdk_integration.py` — System prompt injection (lines 208-246)
- `src/storage/database.py` — scheduled_jobs table schema
