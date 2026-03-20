"""Job scheduler for recurring and one-shot agent tasks.

Wraps APScheduler's AsyncIOScheduler and publishes ScheduledEvents
to the event bus when jobs fire. One-shot jobs track delivery via
an acknowledgement loop: the AgentHandler publishes ScheduledJobOutcome
after processing, and the scheduler updates job status accordingly.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog
from apscheduler.schedulers.asyncio import (
    AsyncIOScheduler,  # type: ignore[import-untyped]
)
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]
from apscheduler.triggers.date import DateTrigger  # type: ignore[import-untyped]

from ..events.bus import Event, EventBus
from ..events.types import ScheduledEvent, ScheduledJobOutcome
from ..storage.database import DatabaseManager

logger = structlog.get_logger()

MAX_ONE_SHOT_ATTEMPTS = 3
RECOVERY_WINDOW_HOURS = 24


class JobScheduler:
    """Scheduler that publishes ScheduledEvents to the event bus.

    One-shot jobs go through a delivery lifecycle:
    pending -> fired -> delivered (auto-cleaned) or failed (visible).
    """

    def __init__(
        self,
        event_bus: EventBus,
        db_manager: DatabaseManager,
        default_working_directory: Path,
    ) -> None:
        self.event_bus = event_bus
        self.db_manager = db_manager
        self.default_working_directory = default_working_directory
        self._scheduler = AsyncIOScheduler()

    async def start(self) -> None:
        """Load persisted jobs, recover failures, and start the scheduler."""
        self.event_bus.subscribe(ScheduledJobOutcome, self._handle_outcome)
        await self._load_jobs_from_db()
        self._scheduler.start()
        logger.info("Job scheduler started")
        await self._recover_one_shot_jobs()

    async def stop(self) -> None:
        """Shutdown the scheduler gracefully."""
        self._scheduler.shutdown(wait=False)
        logger.info("Job scheduler stopped")

    async def add_job(
        self,
        job_name: str,
        prompt: str,
        cron_expression: Optional[str] = None,
        run_at: Optional[str] = None,
        target_chat_ids: Optional[List[int]] = None,
        working_directory: Optional[Path] = None,
        skill_name: Optional[str] = None,
        created_by: int = 0,
        model: Optional[str] = None,
    ) -> str:
        """Add a new scheduled job (recurring or one-shot).

        Provide exactly one of cron_expression or run_at.

        Args:
            job_name: Human-readable job name.
            prompt: The prompt to send to Claude when the job fires.
            cron_expression: Cron-style schedule (e.g. "0 9 * * 1-5").
            run_at: ISO 8601 timestamp for a one-shot job.
            target_chat_ids: Telegram chat IDs to send the response to.
            working_directory: Working directory for Claude execution.
            skill_name: Optional skill to invoke.
            created_by: Telegram user ID of the creator.
            model: Claude model to use (e.g. "haiku", "sonnet"). None = default.

        Returns:
            The job ID.
        """
        if cron_expression and run_at:
            raise ValueError("Provide cron_expression or run_at, not both")
        if not cron_expression and not run_at:
            raise ValueError("Provide either cron_expression or run_at")

        one_shot = run_at is not None
        if run_at:
            run_dt = datetime.fromisoformat(run_at)
            if run_dt.tzinfo is None:
                run_dt = run_dt.replace(tzinfo=UTC)
            trigger: CronTrigger | DateTrigger = DateTrigger(run_date=run_dt)
        else:
            assert cron_expression is not None
            trigger = CronTrigger.from_crontab(cron_expression)

        work_dir = working_directory or self.default_working_directory

        job = self._scheduler.add_job(
            self._fire_event,
            trigger=trigger,
            kwargs={
                "job_name": job_name,
                "prompt": prompt,
                "working_directory": str(work_dir),
                "target_chat_ids": target_chat_ids or [],
                "skill_name": skill_name,
                "model": model,
                "job_id": None,  # filled in below
                "one_shot": one_shot,
            },
            name=job_name,
        )

        # Patch the job_id into kwargs now that APScheduler assigned one
        job.modify(
            kwargs={
                **job.kwargs,
                "job_id": job.id,
            }
        )

        # Persist to database
        await self._save_job(
            job_id=job.id,
            job_name=job_name,
            cron_expression=cron_expression or "",
            prompt=prompt,
            target_chat_ids=target_chat_ids or [],
            working_directory=str(work_dir),
            skill_name=skill_name,
            created_by=created_by,
            model=model,
            run_at=run_at,
        )

        schedule_desc = run_at if one_shot else cron_expression
        logger.info(
            "Scheduled job added",
            job_id=job.id,
            job_name=job_name,
            schedule=schedule_desc,
            one_shot=one_shot,
        )
        return str(job.id)

    async def remove_job(self, job_id: str) -> bool:
        """Remove a scheduled job."""
        try:
            self._scheduler.remove_job(job_id)
        except Exception:
            logger.warning("Job not found in scheduler", job_id=job_id)

        await self._delete_job(job_id)
        logger.info("Scheduled job removed", job_id=job_id)
        return True

    async def list_jobs(self) -> List[Dict[str, Any]]:
        """List all scheduled jobs from the database."""
        async with self.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM scheduled_jobs WHERE is_active = 1 ORDER BY created_at"
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    # -- Firing and acknowledgement --

    async def _fire_event(
        self,
        job_name: str,
        prompt: str,
        working_directory: str,
        target_chat_ids: List[int],
        skill_name: Optional[str],
        model: Optional[str] = None,
        job_id: Optional[str] = None,
        one_shot: bool = False,
    ) -> None:
        """Called by APScheduler when a job triggers. Publishes a ScheduledEvent."""
        # For one-shot jobs, mark as fired and track the attempt
        if one_shot and job_id:
            await self._update_job_status(
                job_id, "fired", increment_attempts=True
            )

        event = ScheduledEvent(
            job_id=job_id or "",
            job_name=job_name,
            prompt=prompt,
            working_directory=Path(working_directory),
            target_chat_ids=target_chat_ids,
            skill_name=skill_name,
            model=model,
        )

        logger.info(
            "Scheduled job fired",
            job_name=job_name,
            job_id=job_id,
            event_id=event.id,
            one_shot=one_shot,
        )

        await self.event_bus.publish(event)

    async def _handle_outcome(self, event: Event) -> None:
        """Process delivery acknowledgement from AgentHandler."""
        if not isinstance(event, ScheduledJobOutcome):
            return
        if not event.job_id:
            return

        if event.delivered:
            logger.info(
                "One-shot job delivered, cleaning up",
                job_id=event.job_id,
            )
            await self._delete_job(event.job_id)
        else:
            logger.warning(
                "One-shot job delivery failed",
                job_id=event.job_id,
                error=event.error,
            )
            await self._update_job_status(
                event.job_id, "failed", error=event.error
            )

    # -- Startup recovery --

    async def _recover_one_shot_jobs(self) -> None:
        """Find missed or failed one-shot jobs and re-fire them.

        Called after startup. Publishes a notification summarising
        any recovered jobs so the agent and user are aware.
        """
        cutoff = datetime.now(UTC) - timedelta(hours=RECOVERY_WINDOW_HOURS)
        recovered: List[Dict[str, Any]] = []

        try:
            async with self.db_manager.get_connection() as conn:
                cursor = await conn.execute(
                    """
                    SELECT * FROM scheduled_jobs
                    WHERE is_active = 1
                      AND run_at IS NOT NULL
                      AND status IN ('pending', 'fired', 'failed')
                    """,
                )
                rows = list(await cursor.fetchall())
        except Exception:
            return

        for row in rows:
            job = dict(row)
            run_at = job.get("run_at")
            if not run_at:
                continue

            run_dt = datetime.fromisoformat(run_at)
            if run_dt.tzinfo is None:
                run_dt = run_dt.replace(tzinfo=UTC)

            # Only recover jobs within the window
            if run_dt > datetime.now(UTC):
                continue  # Still in the future, handled by normal scheduling
            if run_dt < cutoff:
                # Too old to recover — mark expired and clean up
                logger.info(
                    "Expiring stale one-shot job",
                    job_id=job["job_id"],
                    run_at=run_at,
                )
                await self._update_job_status(job["job_id"], "expired")
                await self._delete_job(job["job_id"])
                continue

            status = job.get("status", "pending")
            attempts = job.get("attempts", 0)

            if attempts >= MAX_ONE_SHOT_ATTEMPTS:
                logger.warning(
                    "One-shot job exhausted retries",
                    job_id=job["job_id"],
                    attempts=attempts,
                )
                await self._update_job_status(
                    job["job_id"],
                    "failed",
                    error=f"Exhausted {MAX_ONE_SHOT_ATTEMPTS} delivery attempts",
                )
                continue

            reason = {
                "pending": "missed while offline",
                "fired": "unconfirmed delivery (restart during processing)",
                "failed": "retrying after delivery failure",
            }.get(status, status)

            logger.info(
                "Recovering one-shot job",
                job_id=job["job_id"],
                job_name=job["job_name"],
                reason=reason,
            )

            chat_ids_str = job.get("target_chat_ids", "")
            chat_ids = (
                [int(x) for x in chat_ids_str.split(",") if x.strip()]
                if chat_ids_str
                else []
            )

            recovered.append(
                {
                    "job_id": job["job_id"],
                    "job_name": job["job_name"],
                    "run_at": run_at,
                    "reason": reason,
                    "prompt": job["prompt"],
                    "working_directory": job["working_directory"],
                    "target_chat_ids": chat_ids,
                    "skill_name": job.get("skill_name"),
                    "model": job.get("model"),
                }
            )

        if not recovered:
            return

        # Re-fire each recovered job
        for job in recovered:
            await self._fire_event(
                job_name=job["job_name"],
                prompt=job["prompt"],
                working_directory=job["working_directory"],
                target_chat_ids=job["target_chat_ids"],
                skill_name=job["skill_name"],
                model=job["model"],
                job_id=job["job_id"],
                one_shot=True,
            )

        # Publish a summary notification so the agent and user know
        summary_lines = [
            f"- {j['job_name']} (was due {j['run_at']}): {j['reason']}"
            for j in recovered
        ]
        summary = (
            "SYSTEM: Recovered one-shot jobs on startup:\n"
            + "\n".join(summary_lines)
            + "\n\nOriginal prompts have been re-delivered."
        )

        await self.event_bus.publish(
            ScheduledEvent(
                job_name="system:recovery-notification",
                prompt=summary,
                working_directory=self.default_working_directory,
                target_chat_ids=[],
            )
        )

        logger.info(
            "One-shot job recovery complete", recovered_count=len(recovered)
        )

    # -- Database operations --

    async def _load_jobs_from_db(self) -> None:
        """Load persisted jobs and re-register them with APScheduler."""
        try:
            async with self.db_manager.get_connection() as conn:
                cursor = await conn.execute(
                    "SELECT * FROM scheduled_jobs WHERE is_active = 1"
                )
                rows = list(await cursor.fetchall())

            for row in rows:
                row_dict = dict(row)
                try:
                    run_at = row_dict.get("run_at")
                    one_shot = bool(run_at)

                    if run_at:
                        run_dt = datetime.fromisoformat(run_at)
                        if run_dt.tzinfo is None:
                            run_dt = run_dt.replace(tzinfo=UTC)
                        # Past one-shot jobs handled by _recover_one_shot_jobs
                        if run_dt <= datetime.now(UTC):
                            continue
                        trigger = DateTrigger(run_date=run_dt)
                    else:
                        trigger = CronTrigger.from_crontab(
                            row_dict["cron_expression"]
                        )

                    # Parse target_chat_ids from stored string
                    chat_ids_str = row_dict.get("target_chat_ids", "")
                    chat_ids = (
                        [int(x) for x in chat_ids_str.split(",") if x.strip()]
                        if chat_ids_str
                        else []
                    )

                    self._scheduler.add_job(
                        self._fire_event,
                        trigger=trigger,
                        kwargs={
                            "job_name": row_dict["job_name"],
                            "prompt": row_dict["prompt"],
                            "working_directory": row_dict["working_directory"],
                            "target_chat_ids": chat_ids,
                            "skill_name": row_dict.get("skill_name"),
                            "model": row_dict.get("model"),
                            "job_id": row_dict["job_id"],
                            "one_shot": one_shot,
                        },
                        id=row_dict["job_id"],
                        name=row_dict["job_name"],
                        replace_existing=True,
                    )
                    logger.debug(
                        "Loaded scheduled job from DB",
                        job_id=row_dict["job_id"],
                        job_name=row_dict["job_name"],
                        one_shot=one_shot,
                    )
                except Exception:
                    logger.exception(
                        "Failed to load scheduled job",
                        job_id=row_dict.get("job_id"),
                    )

            logger.info("Loaded scheduled jobs from database", count=len(rows))
        except Exception:
            # Table might not exist yet on first run
            logger.debug("No scheduled_jobs table found, starting fresh")

    async def _save_job(
        self,
        job_id: str,
        job_name: str,
        cron_expression: str,
        prompt: str,
        target_chat_ids: List[int],
        working_directory: str,
        skill_name: Optional[str],
        created_by: int,
        model: Optional[str] = None,
        run_at: Optional[str] = None,
    ) -> None:
        """Persist a job definition to the database."""
        chat_ids_str = ",".join(str(cid) for cid in target_chat_ids)
        async with self.db_manager.get_connection() as conn:
            await conn.execute(
                """
                INSERT OR REPLACE INTO scheduled_jobs
                (job_id, job_name, cron_expression, prompt, target_chat_ids,
                 working_directory, skill_name, created_by, is_active, model,
                 run_at, status, attempts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 'pending', 0)
                """,
                (
                    job_id,
                    job_name,
                    cron_expression,
                    prompt,
                    chat_ids_str,
                    working_directory,
                    skill_name,
                    created_by,
                    model,
                    run_at,
                ),
            )
            await conn.commit()

    async def _update_job_status(
        self,
        job_id: str,
        status: str,
        error: Optional[str] = None,
        increment_attempts: bool = False,
    ) -> None:
        """Update a job's delivery status."""
        async with self.db_manager.get_connection() as conn:
            if increment_attempts:
                await conn.execute(
                    """
                    UPDATE scheduled_jobs
                    SET status = ?, last_error = ?, attempts = attempts + 1,
                        fired_at = ?
                    WHERE job_id = ?
                    """,
                    (status, error, datetime.now(UTC).isoformat(), job_id),
                )
            else:
                await conn.execute(
                    """
                    UPDATE scheduled_jobs
                    SET status = ?, last_error = ?
                    WHERE job_id = ?
                    """,
                    (status, error, job_id),
                )
            await conn.commit()

    async def _delete_job(self, job_id: str) -> None:
        """Soft-delete a job from the database."""
        async with self.db_manager.get_connection() as conn:
            await conn.execute(
                "UPDATE scheduled_jobs SET is_active = 0 WHERE job_id = ?",
                (job_id,),
            )
            await conn.commit()
