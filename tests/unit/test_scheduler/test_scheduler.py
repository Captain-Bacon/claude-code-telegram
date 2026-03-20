"""Tests for JobScheduler lifecycle: firing, acknowledgement, recovery, alerts."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.events.bus import EventBus
from src.events.types import ScheduledEvent, ScheduledJobOutcome
from src.scheduler.alerts import ALERT_FILENAME
from src.scheduler.scheduler import (
    MAX_ONE_SHOT_ATTEMPTS,
    RECOVERY_WINDOW_HOURS,
    JobScheduler,
)
from src.storage.database import DatabaseManager


async def drain_bus(bus: EventBus) -> None:
    """Let the event bus processor handle all queued events."""
    for _ in range(100):
        await asyncio.sleep(0.01)
        if bus._queue.empty():
            break


@pytest.fixture
async def db_manager(tmp_path: Path) -> DatabaseManager:
    """Create a real database for scheduler tests, with proper cleanup."""
    db_path = tmp_path / "test.db"
    manager = DatabaseManager(str(db_path))
    await manager.initialize()
    yield manager
    await manager.close()


@pytest.fixture
async def event_bus() -> EventBus:
    bus = EventBus()
    await bus.start()
    yield bus
    await bus.stop()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Workspace directory with .claude/ for alert files."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    return tmp_path


@pytest.fixture
async def scheduler(
    event_bus: EventBus, db_manager: DatabaseManager, workspace: Path
) -> JobScheduler:
    """Create a scheduler with real DB but mock APScheduler."""
    sched = JobScheduler(event_bus, db_manager, workspace)
    # Patch the internal APScheduler so jobs don't actually fire on timers
    sched._scheduler = MagicMock()
    yield sched


# -- Adding and persisting jobs --


class TestAddJob:
    async def test_add_cron_job_persists(
        self, scheduler: JobScheduler, db_manager: DatabaseManager
    ) -> None:
        """Cron job is saved to DB and registered with APScheduler."""
        job_id = await scheduler.add_job(
            job_name="daily-standup",
            prompt="Morning update",
            cron_expression="0 9 * * 1-5",
        )

        assert job_id  # UUID string
        scheduler._scheduler.add_job.assert_called_once()

        # Verify persisted
        async with db_manager.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM scheduled_jobs WHERE job_id = ?", (job_id,)
            )
            row = await cursor.fetchone()
            assert row is not None
            assert dict(row)["job_name"] == "daily-standup"
            assert dict(row)["status"] == "pending"

    async def test_add_one_shot_job_persists(
        self, scheduler: JobScheduler, db_manager: DatabaseManager
    ) -> None:
        """One-shot job stores run_at and empty cron_expression."""
        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        job_id = await scheduler.add_job(
            job_name="remind-meeting",
            prompt="Meeting soon",
            run_at=future,
        )

        async with db_manager.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM scheduled_jobs WHERE job_id = ?", (job_id,)
            )
            row = dict(await cursor.fetchone())
            assert row["run_at"] == future
            assert row["cron_expression"] == ""
            assert row["status"] == "pending"

    async def test_add_job_rejects_both_schedules(
        self, scheduler: JobScheduler
    ) -> None:
        """Providing both cron_expression and run_at raises ValueError."""
        with pytest.raises(ValueError, match="not both"):
            await scheduler.add_job(
                job_name="bad",
                prompt="test",
                cron_expression="0 9 * * *",
                run_at="2026-03-21T09:00:00+00:00",
            )

    async def test_add_job_rejects_neither_schedule(
        self, scheduler: JobScheduler
    ) -> None:
        """Providing neither cron_expression nor run_at raises ValueError."""
        with pytest.raises(ValueError, match="either"):
            await scheduler.add_job(job_name="bad", prompt="test")

    async def test_description_persisted(
        self, scheduler: JobScheduler, db_manager: DatabaseManager
    ) -> None:
        """Description field is stored in the database."""
        job_id = await scheduler.add_job(
            job_name="weekly-review",
            prompt="Review time",
            cron_expression="0 0 * * 0",
            description="Sunday midnight review",
        )

        async with db_manager.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT description FROM scheduled_jobs WHERE job_id = ?",
                (job_id,),
            )
            row = await cursor.fetchone()
            assert dict(row)["description"] == "Sunday midnight review"


# -- Firing events --


class TestFireEvent:
    async def test_cron_fire_publishes_event_without_job_id(
        self, scheduler: JobScheduler, event_bus: EventBus
    ) -> None:
        """Cron job fires a ScheduledEvent with empty job_id (no ack loop)."""
        captured: list = []

        async def capture(e: ScheduledEvent) -> None:
            captured.append(e)

        event_bus.subscribe(ScheduledEvent, capture)

        await scheduler._fire_event(
            job_name="daily",
            prompt="Hello",
            working_directory="/tmp",
            target_chat_ids=[123],
            skill_name=None,
            job_id="some-uuid",
            one_shot=False,
        )
        await drain_bus(event_bus)

        assert len(captured) == 1
        assert captured[0].job_id == ""  # Cron = no ack

    async def test_one_shot_fire_publishes_event_with_job_id(
        self, scheduler: JobScheduler, event_bus: EventBus, db_manager: DatabaseManager
    ) -> None:
        """One-shot job fires a ScheduledEvent with job_id (triggers ack loop)."""
        job_id = await scheduler.add_job(
            job_name="remind",
            prompt="Meeting",
            run_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        )

        captured: list = []

        async def capture(e: ScheduledEvent) -> None:
            captured.append(e)

        event_bus.subscribe(ScheduledEvent, capture)

        await scheduler._fire_event(
            job_name="remind",
            prompt="Meeting",
            working_directory="/tmp",
            target_chat_ids=[],
            skill_name=None,
            job_id=job_id,
            one_shot=True,
        )
        await drain_bus(event_bus)

        assert len(captured) == 1
        assert captured[0].job_id == job_id

        # Status should be updated to 'fired'
        async with db_manager.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT status, attempts FROM scheduled_jobs WHERE job_id = ?",
                (job_id,),
            )
            row = dict(await cursor.fetchone())
            assert row["status"] == "fired"
            assert row["attempts"] == 1


# -- Acknowledgement (outcome handling) --


class TestHandleOutcome:
    async def test_successful_delivery_deletes_job(
        self,
        scheduler: JobScheduler,
        event_bus: EventBus,
        db_manager: DatabaseManager,
    ) -> None:
        """Successful ack soft-deletes the job."""
        job_id = await scheduler.add_job(
            job_name="remind",
            prompt="Meeting",
            run_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        )

        event_bus.subscribe(ScheduledJobOutcome, scheduler._handle_outcome)
        await event_bus.publish(ScheduledJobOutcome(job_id=job_id, delivered=True))
        await drain_bus(event_bus)

        # Job should be soft-deleted
        async with db_manager.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT is_active FROM scheduled_jobs WHERE job_id = ?",
                (job_id,),
            )
            row = dict(await cursor.fetchone())
            assert row["is_active"] == 0

    async def test_successful_delivery_clears_alert(
        self,
        scheduler: JobScheduler,
        event_bus: EventBus,
        workspace: Path,
    ) -> None:
        """Successful ack clears any existing alert for the job."""
        job_id = await scheduler.add_job(
            job_name="remind",
            prompt="Meeting",
            run_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            working_directory=workspace,
        )

        # Write a pre-existing alert
        from src.scheduler.alerts import write_alert

        write_alert(workspace, {"job_id": job_id, "job_name": "remind"}, "test")
        assert (workspace / ALERT_FILENAME).read_text().strip()

        event_bus.subscribe(ScheduledJobOutcome, scheduler._handle_outcome)
        await event_bus.publish(ScheduledJobOutcome(job_id=job_id, delivered=True))
        await drain_bus(event_bus)

        # Alert should be cleared
        content = (workspace / ALERT_FILENAME).read_text()
        assert content.strip() == ""

    async def test_failed_delivery_writes_alert_and_status(
        self,
        scheduler: JobScheduler,
        event_bus: EventBus,
        db_manager: DatabaseManager,
        workspace: Path,
    ) -> None:
        """Failed delivery writes alert and updates status to failed."""
        job_id = await scheduler.add_job(
            job_name="remind",
            prompt="Meeting",
            run_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            working_directory=workspace,
        )

        event_bus.subscribe(ScheduledJobOutcome, scheduler._handle_outcome)
        await event_bus.publish(
            ScheduledJobOutcome(
                job_id=job_id, delivered=False, error="Connection refused"
            )
        )
        await drain_bus(event_bus)

        # Alert file should exist
        alert_content = (workspace / ALERT_FILENAME).read_text()
        assert "remind" in alert_content
        assert "Connection refused" in alert_content

        # Status should be 'failed'
        async with db_manager.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT status, last_error FROM scheduled_jobs WHERE job_id = ?",
                (job_id,),
            )
            row = dict(await cursor.fetchone())
            assert row["status"] == "failed"
            assert row["last_error"] == "Connection refused"

    async def test_empty_job_id_ignored(
        self, scheduler: JobScheduler, event_bus: EventBus
    ) -> None:
        """Outcome with empty job_id is silently ignored (cron job path)."""
        event_bus.subscribe(ScheduledJobOutcome, scheduler._handle_outcome)
        # Should not raise
        await event_bus.publish(ScheduledJobOutcome(job_id="", delivered=True))
        await drain_bus(event_bus)


# -- Recovery --


class TestRecovery:
    async def _create_past_job(
        self,
        scheduler: JobScheduler,
        db_manager: DatabaseManager,
        *,
        hours_ago: int = 2,
        status: str = "pending",
        attempts: int = 0,
    ) -> str:
        """Helper: insert a one-shot job that was due in the past."""
        job_id = str(uuid.uuid4())
        run_at = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()
        async with db_manager.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO scheduled_jobs
                (job_id, job_name, cron_expression, prompt, target_chat_ids,
                 working_directory, is_active, run_at, status, attempts,
                 priority)
                VALUES (?, 'test-job', '', 'do something', '', ?, 1, ?, ?, ?, 'medium')
                """,
                (
                    job_id,
                    str(scheduler.default_working_directory),
                    run_at,
                    status,
                    attempts,
                ),
            )
            await conn.commit()
        return job_id

    async def test_recovers_pending_job(
        self,
        scheduler: JobScheduler,
        event_bus: EventBus,
        db_manager: DatabaseManager,
    ) -> None:
        """Pending past-due job is re-fired on recovery."""
        captured: list = []

        async def capture(e: ScheduledEvent) -> None:
            captured.append(e)

        event_bus.subscribe(ScheduledEvent, capture)

        job_id = await self._create_past_job(
            scheduler, db_manager, hours_ago=2, status="pending"
        )
        await scheduler._recover_one_shot_jobs()
        await drain_bus(event_bus)

        fired_events = [e for e in captured if e.job_name == "test-job"]
        assert len(fired_events) == 1
        assert fired_events[0].job_id == job_id

    async def test_recovers_fired_job(
        self,
        scheduler: JobScheduler,
        event_bus: EventBus,
        db_manager: DatabaseManager,
    ) -> None:
        """Job stuck in 'fired' status (unconfirmed delivery) is re-fired."""
        captured: list = []

        async def capture(e: ScheduledEvent) -> None:
            captured.append(e)

        event_bus.subscribe(ScheduledEvent, capture)

        await self._create_past_job(
            scheduler, db_manager, hours_ago=1, status="fired", attempts=1
        )
        await scheduler._recover_one_shot_jobs()
        await drain_bus(event_bus)

        fired_events = [e for e in captured if e.job_name == "test-job"]
        assert len(fired_events) == 1

    async def test_recovers_failed_job(
        self,
        scheduler: JobScheduler,
        event_bus: EventBus,
        db_manager: DatabaseManager,
    ) -> None:
        """Failed job within retry budget is re-fired."""
        captured: list = []

        async def capture(e: ScheduledEvent) -> None:
            captured.append(e)

        event_bus.subscribe(ScheduledEvent, capture)

        await self._create_past_job(
            scheduler, db_manager, hours_ago=1, status="failed", attempts=1
        )
        await scheduler._recover_one_shot_jobs()
        await drain_bus(event_bus)

        fired_events = [e for e in captured if e.job_name == "test-job"]
        assert len(fired_events) == 1

    async def test_exhausted_retries_not_refired(
        self,
        scheduler: JobScheduler,
        event_bus: EventBus,
        db_manager: DatabaseManager,
        workspace: Path,
    ) -> None:
        """Job at max attempts gets an alert but is NOT re-fired."""
        captured: list = []

        async def capture(e: ScheduledEvent) -> None:
            captured.append(e)

        event_bus.subscribe(ScheduledEvent, capture)

        await self._create_past_job(
            scheduler,
            db_manager,
            hours_ago=1,
            status="failed",
            attempts=MAX_ONE_SHOT_ATTEMPTS,
        )
        await scheduler._recover_one_shot_jobs()
        await drain_bus(event_bus)

        # Should NOT have been re-fired
        fired_events = [e for e in captured if e.job_name == "test-job"]
        assert len(fired_events) == 0

        # Should have written an alert
        alert_content = (workspace / ALERT_FILENAME).read_text()
        assert "Exhausted" in alert_content

    async def test_expired_job_cleaned_up(
        self,
        scheduler: JobScheduler,
        event_bus: EventBus,
        db_manager: DatabaseManager,
        workspace: Path,
    ) -> None:
        """Job past the recovery window is expired, alerted, and deleted."""
        captured: list = []

        async def capture(e: ScheduledEvent) -> None:
            captured.append(e)

        event_bus.subscribe(ScheduledEvent, capture)

        job_id = await self._create_past_job(
            scheduler,
            db_manager,
            hours_ago=RECOVERY_WINDOW_HOURS + 1,
            status="pending",
        )
        await scheduler._recover_one_shot_jobs()
        await drain_bus(event_bus)

        # Should NOT have been re-fired
        fired_events = [e for e in captured if e.job_name == "test-job"]
        assert len(fired_events) == 0

        # Should have written an expiry alert
        alert_content = (workspace / ALERT_FILENAME).read_text()
        assert "expired" in alert_content.lower()

        # Should be soft-deleted
        async with db_manager.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT is_active FROM scheduled_jobs WHERE job_id = ?",
                (job_id,),
            )
            row = dict(await cursor.fetchone())
            assert row["is_active"] == 0

    async def test_future_job_not_recovered(
        self,
        scheduler: JobScheduler,
        event_bus: EventBus,
        db_manager: DatabaseManager,
    ) -> None:
        """One-shot job still in the future is left alone by recovery."""
        captured: list = []

        async def capture(e: ScheduledEvent) -> None:
            captured.append(e)

        event_bus.subscribe(ScheduledEvent, capture)

        job_id = str(uuid.uuid4())
        future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
        async with db_manager.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO scheduled_jobs
                (job_id, job_name, cron_expression, prompt, target_chat_ids,
                 working_directory, is_active, run_at, status, attempts, priority)
                VALUES (?, 'future-job', '', 'do later', '', ?, 1, ?, 'pending', 0, 'medium')
                """,
                (job_id, str(scheduler.default_working_directory), future),
            )
            await conn.commit()

        await scheduler._recover_one_shot_jobs()
        await drain_bus(event_bus)

        assert len(captured) == 0

    async def test_recovery_summary_notification(
        self,
        scheduler: JobScheduler,
        event_bus: EventBus,
        db_manager: DatabaseManager,
    ) -> None:
        """Recovery publishes a summary notification listing recovered jobs."""
        captured: list = []

        async def capture(e: ScheduledEvent) -> None:
            captured.append(e)

        event_bus.subscribe(ScheduledEvent, capture)

        await self._create_past_job(
            scheduler, db_manager, hours_ago=1, status="pending"
        )
        await scheduler._recover_one_shot_jobs()
        await drain_bus(event_bus)

        summaries = [
            e for e in captured if e.job_name == "system:recovery-notification"
        ]
        assert len(summaries) == 1
        assert "Recovered" in summaries[0].prompt


# -- Full lifecycle --


class TestFullLifecycle:
    async def test_create_fire_ack_delete(
        self,
        scheduler: JobScheduler,
        event_bus: EventBus,
        db_manager: DatabaseManager,
    ) -> None:
        """Complete happy path: create -> fire -> ack -> soft-delete."""
        # Wire up the outcome handler (start subscribes to ScheduledJobOutcome)
        event_bus.subscribe(ScheduledJobOutcome, scheduler._handle_outcome)

        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        job_id = await scheduler.add_job(
            job_name="lifecycle-test",
            prompt="Test prompt",
            run_at=future,
        )

        # Simulate APScheduler firing the job
        await scheduler._fire_event(
            job_name="lifecycle-test",
            prompt="Test prompt",
            working_directory=str(scheduler.default_working_directory),
            target_chat_ids=[],
            skill_name=None,
            job_id=job_id,
            one_shot=True,
        )
        await drain_bus(event_bus)

        # Verify status is now 'fired'
        job = await scheduler._get_job(job_id)
        assert job["status"] == "fired"
        assert job["attempts"] == 1

        # Simulate successful delivery ack
        await event_bus.publish(ScheduledJobOutcome(job_id=job_id, delivered=True))
        await drain_bus(event_bus)

        # Job should be soft-deleted
        job = await scheduler._get_job(job_id)
        assert job["is_active"] == 0

    async def test_create_fire_fail_retry_succeed(
        self,
        scheduler: JobScheduler,
        event_bus: EventBus,
        db_manager: DatabaseManager,
        workspace: Path,
    ) -> None:
        """Failure path: create -> fire -> fail -> ack success -> delete."""
        event_bus.subscribe(ScheduledJobOutcome, scheduler._handle_outcome)

        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        job_id = await scheduler.add_job(
            job_name="retry-test",
            prompt="Test prompt",
            run_at=future,
            working_directory=workspace,
        )

        # First attempt fires
        await scheduler._fire_event(
            job_name="retry-test",
            prompt="Test prompt",
            working_directory=str(workspace),
            target_chat_ids=[],
            skill_name=None,
            job_id=job_id,
            one_shot=True,
        )
        await drain_bus(event_bus)

        # First attempt fails
        await event_bus.publish(
            ScheduledJobOutcome(job_id=job_id, delivered=False, error="Network error")
        )
        await drain_bus(event_bus)

        # Verify alert was written
        alert_content = (workspace / ALERT_FILENAME).read_text()
        assert "retry-test" in alert_content

        # Verify status is failed
        job = await scheduler._get_job(job_id)
        assert job["status"] == "failed"

        # Now simulate a successful second delivery
        await event_bus.publish(ScheduledJobOutcome(job_id=job_id, delivered=True))
        await drain_bus(event_bus)

        # Job should be soft-deleted, alert cleared
        job = await scheduler._get_job(job_id)
        assert job["is_active"] == 0
        alert_content = (workspace / ALERT_FILENAME).read_text()
        assert alert_content.strip() == ""


# -- Remove job --


class TestRemoveJob:
    async def test_remove_job_soft_deletes(
        self,
        scheduler: JobScheduler,
        db_manager: DatabaseManager,
    ) -> None:
        """Removing a job soft-deletes it from the database."""
        job_id = await scheduler.add_job(
            job_name="to-remove",
            prompt="Goodbye",
            cron_expression="0 0 * * *",
        )

        await scheduler.remove_job(job_id)

        async with db_manager.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT is_active FROM scheduled_jobs WHERE job_id = ?",
                (job_id,),
            )
            row = dict(await cursor.fetchone())
            assert row["is_active"] == 0

    async def test_remove_nonexistent_job_no_error(
        self, scheduler: JobScheduler
    ) -> None:
        """Removing a non-existent job doesn't raise."""
        result = await scheduler.remove_job("does-not-exist")
        assert result is True


# -- List jobs --


class TestListJobs:
    async def test_lists_only_active_jobs(
        self,
        scheduler: JobScheduler,
        db_manager: DatabaseManager,
    ) -> None:
        """list_jobs returns only active jobs."""
        job_id = await scheduler.add_job(
            job_name="active-job",
            prompt="Hello",
            cron_expression="0 9 * * *",
        )
        removed_id = await scheduler.add_job(
            job_name="removed-job",
            prompt="Bye",
            cron_expression="0 10 * * *",
        )
        await scheduler.remove_job(removed_id)

        jobs = await scheduler.list_jobs()
        job_ids = [j["job_id"] for j in jobs]
        assert job_id in job_ids
        assert removed_id not in job_ids
