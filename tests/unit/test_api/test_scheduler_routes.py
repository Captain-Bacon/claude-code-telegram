"""Tests for the scheduler API routes."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI

from src.api.scheduler_routes import create_scheduler_router
from src.api.server import create_api_app

# Shared test constants
API_SECRET = "test-secret-token"
AUTH_HEADER = f"Bearer {API_SECRET}"


@pytest.fixture
def mock_scheduler() -> MagicMock:
    """Create a mock JobScheduler."""
    scheduler = MagicMock()
    scheduler.add_job = AsyncMock(return_value="job-123")
    scheduler.remove_job = AsyncMock(return_value=True)
    scheduler.list_jobs = AsyncMock(
        return_value=[
            {
                "job_id": "job-123",
                "job_name": "daily-standup",
                "cron_expression": "0 9 * * 1-5",
                "prompt": "Give me a morning update",
                "description": None,
            }
        ]
    )
    # Mock the internal APScheduler for next_run_time lookups
    scheduler._scheduler = MagicMock()
    scheduler._scheduler.get_job.return_value = None
    return scheduler


@pytest.fixture
def mock_settings() -> MagicMock:
    """Create mock Settings."""
    settings = MagicMock()
    settings.webhook_api_secret = API_SECRET
    settings.github_webhook_secret = None
    settings.development_mode = True
    settings.api_server_port = 8080
    settings.debug = False
    return settings


@pytest.fixture
def mock_event_bus() -> MagicMock:
    """Create a mock EventBus."""
    return MagicMock()


@pytest.fixture
def app(
    mock_event_bus: MagicMock,
    mock_settings: MagicMock,
    mock_scheduler: MagicMock,
) -> FastAPI:
    """Create a FastAPI app with scheduler routes."""
    return create_api_app(
        event_bus=mock_event_bus,
        settings=mock_settings,
        db_manager=None,
        scheduler=mock_scheduler,
    )


@pytest.fixture
def app_no_scheduler(
    mock_event_bus: MagicMock,
    mock_settings: MagicMock,
) -> FastAPI:
    """Create a FastAPI app without scheduler routes."""
    return create_api_app(
        event_bus=mock_event_bus,
        settings=mock_settings,
        db_manager=None,
        scheduler=None,
    )


class TestSchedulerAPICreateJob:
    """Tests for POST /scheduler/jobs."""

    async def test_create_job_success(
        self, app: FastAPI, mock_scheduler: MagicMock
    ) -> None:
        """Creating a job returns the job ID."""
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/scheduler/jobs",
                json={
                    "name": "daily-standup",
                    "cron_expression": "0 9 * * 1-5",
                    "prompt": "Give me a morning update",
                },
                headers={"Authorization": AUTH_HEADER},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "created"
        assert data["job_id"] == "job-123"

        mock_scheduler.add_job.assert_called_once_with(
            job_name="daily-standup",
            prompt="Give me a morning update",
            cron_expression="0 9 * * 1-5",
            run_at=None,
            model=None,
        )

    async def test_create_job_with_model(
        self, app: FastAPI, mock_scheduler: MagicMock
    ) -> None:
        """Model field is passed through to scheduler."""
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/scheduler/jobs",
                json={
                    "name": "spam-filter",
                    "cron_expression": "0 8 * * *",
                    "prompt": "Categorise today's spam emails",
                    "model": "haiku",
                },
                headers={"Authorization": AUTH_HEADER},
            )

        assert response.status_code == 200
        mock_scheduler.add_job.assert_called_once_with(
            job_name="spam-filter",
            prompt="Categorise today's spam emails",
            cron_expression="0 8 * * *",
            run_at=None,
            model="haiku",
        )

    async def test_create_job_with_description(
        self, app: FastAPI, mock_scheduler: MagicMock
    ) -> None:
        """Description field is accepted (passed through to scheduler)."""
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/scheduler/jobs",
                json={
                    "name": "weekly-review",
                    "cron_expression": "0 0 * * 0",
                    "prompt": "Weekly review time",
                    "description": "Sunday midnight review",
                },
                headers={"Authorization": AUTH_HEADER},
            )

        assert response.status_code == 200

    async def test_create_job_missing_auth(self, app: FastAPI) -> None:
        """Missing auth header returns 401."""
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/scheduler/jobs",
                json={
                    "name": "test",
                    "cron_expression": "* * * * *",
                    "prompt": "test",
                },
            )

        assert response.status_code == 401

    async def test_create_job_wrong_auth(self, app: FastAPI) -> None:
        """Wrong auth token returns 401."""
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/scheduler/jobs",
                json={
                    "name": "test",
                    "cron_expression": "* * * * *",
                    "prompt": "test",
                },
                headers={"Authorization": "Bearer wrong-token"},
            )

        assert response.status_code == 401

    async def test_create_job_invalid_cron(
        self, app: FastAPI, mock_scheduler: MagicMock
    ) -> None:
        """Invalid cron expression returns 400."""
        mock_scheduler.add_job = AsyncMock(
            side_effect=ValueError("Invalid cron expression")
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/scheduler/jobs",
                json={
                    "name": "bad-cron",
                    "cron_expression": "not a cron",
                    "prompt": "test",
                },
                headers={"Authorization": AUTH_HEADER},
            )

        assert response.status_code == 400

    async def test_create_job_missing_required_fields(self, app: FastAPI) -> None:
        """Missing required fields returns 422."""
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/scheduler/jobs",
                json={"name": "test"},
                headers={"Authorization": AUTH_HEADER},
            )

        assert response.status_code == 422

    async def test_create_one_shot_job(
        self, app: FastAPI, mock_scheduler: MagicMock
    ) -> None:
        """One-shot job with run_at instead of cron_expression."""
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/scheduler/jobs",
                json={
                    "name": "remind-meeting",
                    "run_at": "2026-03-21T14:00:00+00:00",
                    "prompt": "Team meeting in 30 minutes",
                },
                headers={"Authorization": AUTH_HEADER},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "created"

        mock_scheduler.add_job.assert_called_once_with(
            job_name="remind-meeting",
            prompt="Team meeting in 30 minutes",
            cron_expression=None,
            run_at="2026-03-21T14:00:00+00:00",
            model=None,
        )

    async def test_create_job_both_schedules_rejected(
        self, app: FastAPI
    ) -> None:
        """Providing both cron_expression and run_at returns 422."""
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/scheduler/jobs",
                json={
                    "name": "bad-job",
                    "cron_expression": "0 9 * * 1-5",
                    "run_at": "2026-03-21T14:00:00+00:00",
                    "prompt": "test",
                },
                headers={"Authorization": AUTH_HEADER},
            )

        assert response.status_code == 422

    async def test_create_job_no_schedule_rejected(self, app: FastAPI) -> None:
        """Providing neither cron_expression nor run_at returns 422."""
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/scheduler/jobs",
                json={
                    "name": "bad-job",
                    "prompt": "test",
                },
                headers={"Authorization": AUTH_HEADER},
            )

        assert response.status_code == 422


class TestSchedulerAPIListJobs:
    """Tests for GET /scheduler/jobs."""

    async def test_list_jobs_success(
        self, app: FastAPI, mock_scheduler: MagicMock
    ) -> None:
        """Listing jobs returns all active jobs."""
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/scheduler/jobs",
                headers={"Authorization": AUTH_HEADER},
            )

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["job_id"] == "job-123"
        assert data[0]["name"] == "daily-standup"
        assert data[0]["cron_expression"] == "0 9 * * 1-5"
        assert data[0]["prompt"] == "Give me a morning update"

    async def test_list_jobs_empty(
        self, app: FastAPI, mock_scheduler: MagicMock
    ) -> None:
        """Empty job list returns empty array."""
        mock_scheduler.list_jobs = AsyncMock(return_value=[])

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/scheduler/jobs",
                headers={"Authorization": AUTH_HEADER},
            )

        assert response.status_code == 200
        assert response.json() == []

    async def test_list_jobs_with_next_run_time(
        self, app: FastAPI, mock_scheduler: MagicMock
    ) -> None:
        """Jobs with next_run_time from APScheduler include it."""
        mock_ap_job = MagicMock()
        mock_ap_job.next_run_time = MagicMock()
        mock_ap_job.next_run_time.isoformat.return_value = "2026-03-14T09:00:00+00:00"
        mock_scheduler._scheduler.get_job.return_value = mock_ap_job

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/scheduler/jobs",
                headers={"Authorization": AUTH_HEADER},
            )

        assert response.status_code == 200
        data = response.json()
        assert data[0]["next_run_time"] == "2026-03-14T09:00:00+00:00"

    async def test_list_jobs_missing_auth(self, app: FastAPI) -> None:
        """Missing auth header returns 401."""
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/scheduler/jobs")

        assert response.status_code == 401


class TestSchedulerAPIDeleteJob:
    """Tests for DELETE /scheduler/jobs/{job_id}."""

    async def test_delete_job_success(
        self, app: FastAPI, mock_scheduler: MagicMock
    ) -> None:
        """Deleting a job returns success."""
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.delete(
                "/scheduler/jobs/job-123",
                headers={"Authorization": AUTH_HEADER},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "deleted"
        assert data["job_id"] == "job-123"
        mock_scheduler.remove_job.assert_called_once_with("job-123")

    async def test_delete_job_missing_auth(self, app: FastAPI) -> None:
        """Missing auth header returns 401."""
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.delete("/scheduler/jobs/job-123")

        assert response.status_code == 401


class TestSchedulerRoutesNotMounted:
    """Tests that scheduler routes aren't mounted when no scheduler is provided."""

    async def test_no_scheduler_routes_without_scheduler(
        self, app_no_scheduler: FastAPI
    ) -> None:
        """Scheduler endpoints return 404 when no scheduler is wired."""
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_no_scheduler), base_url="http://test"
        ) as client:
            response = await client.get(
                "/scheduler/jobs",
                headers={"Authorization": AUTH_HEADER},
            )

        assert response.status_code == 404

    async def test_health_still_works_without_scheduler(
        self, app_no_scheduler: FastAPI
    ) -> None:
        """Health endpoint works even without scheduler."""
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_no_scheduler), base_url="http://test"
        ) as client:
            response = await client.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestSchedulerAPINoSecret:
    """Tests for when WEBHOOK_API_SECRET is not configured."""

    async def test_create_job_no_secret_configured(
        self,
        mock_event_bus: MagicMock,
        mock_scheduler: MagicMock,
    ) -> None:
        """Returns 500 when no API secret is configured."""
        settings = MagicMock()
        settings.webhook_api_secret = None
        settings.github_webhook_secret = None
        settings.development_mode = True
        settings.debug = False

        app = create_api_app(
            event_bus=mock_event_bus,
            settings=settings,
            scheduler=mock_scheduler,
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/scheduler/jobs",
                json={
                    "name": "test",
                    "cron_expression": "* * * * *",
                    "prompt": "test",
                },
                headers={"Authorization": "Bearer anything"},
            )

        assert response.status_code == 500
        assert "WEBHOOK_API_SECRET" in response.json()["detail"]
