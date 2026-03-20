"""Scheduler API routes for managing cron jobs.

Allows Claude (via WebFetch) to create, list, and remove scheduled jobs.
"""

from typing import Any, Dict, List, Optional

import structlog
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field, model_validator

from ..scheduler.scheduler import JobScheduler
from .auth import verify_shared_secret

logger = structlog.get_logger()


class CreateJobRequest(BaseModel):
    """Request body for creating a scheduled job.

    Provide exactly one of cron_expression (recurring) or run_at (one-shot).
    """

    name: str = Field(..., description="Human-readable job name")
    cron_expression: Optional[str] = Field(
        None, description='Cron schedule expression (e.g. "0 9 * * 1-5")'
    )
    run_at: Optional[str] = Field(
        None,
        description="ISO 8601 timestamp for a one-shot job (e.g. "
        '"2026-03-21T09:00:00+00:00")',
    )
    prompt: str = Field(..., description="Prompt sent to Claude when the job fires")
    description: Optional[str] = Field(
        None, description="Optional description of what the job does"
    )
    model: Optional[str] = Field(
        None,
        description='Claude model to use (e.g. "haiku", "sonnet"). Defaults to bot config.',
    )

    @model_validator(mode="after")
    def _require_exactly_one_schedule(self) -> "CreateJobRequest":
        if self.cron_expression and self.run_at:
            raise ValueError("Provide cron_expression or run_at, not both")
        if not self.cron_expression and not self.run_at:
            raise ValueError("Provide either cron_expression or run_at")
        return self


class CreateJobResponse(BaseModel):
    """Response after creating a job."""

    status: str = "created"
    job_id: str


class JobResponse(BaseModel):
    """Response for a single job."""

    job_id: str
    name: str
    cron_expression: Optional[str] = None
    run_at: Optional[str] = None
    prompt: str
    description: Optional[str] = None
    model: Optional[str] = None
    status: Optional[str] = None
    attempts: Optional[int] = None
    last_error: Optional[str] = None
    next_run_time: Optional[str] = None


class DeleteJobResponse(BaseModel):
    """Response after deleting a job."""

    status: str = "deleted"
    job_id: str


def create_scheduler_router(
    scheduler: JobScheduler,
    webhook_api_secret: Optional[str],
) -> APIRouter:
    """Create the scheduler API router.

    Args:
        scheduler: The JobScheduler instance.
        webhook_api_secret: Shared secret for Bearer token auth.
    """
    router = APIRouter(prefix="/scheduler", tags=["scheduler"])

    def _verify_auth(authorization: Optional[str]) -> None:
        """Verify Bearer token auth, matching the webhook pattern."""
        if not webhook_api_secret:
            raise HTTPException(
                status_code=500,
                detail=(
                    "Webhook API secret not configured. "
                    "Set WEBHOOK_API_SECRET to use scheduler endpoints."
                ),
            )
        if not verify_shared_secret(authorization, webhook_api_secret):
            raise HTTPException(status_code=401, detail="Invalid authorization")

    @router.post("/jobs", response_model=CreateJobResponse)
    async def create_job(
        body: CreateJobRequest,
        authorization: Optional[str] = Header(None),
    ) -> Dict[str, str]:
        """Create a new scheduled job."""
        _verify_auth(authorization)

        try:
            job_id = await scheduler.add_job(
                job_name=body.name,
                prompt=body.prompt,
                cron_expression=body.cron_expression,
                run_at=body.run_at,
                model=body.model,
            )
        except Exception as e:
            logger.error("Failed to create scheduled job", error=str(e))
            raise HTTPException(
                status_code=400,
                detail=f"Failed to create job: {e}",
            )

        logger.info(
            "Scheduled job created via API",
            job_id=job_id,
            job_name=body.name,
            schedule=body.run_at or body.cron_expression,
        )
        return {"status": "created", "job_id": job_id}

    @router.get("/jobs", response_model=List[JobResponse])
    async def list_jobs(
        authorization: Optional[str] = Header(None),
    ) -> List[Dict[str, Any]]:
        """List all active scheduled jobs."""
        _verify_auth(authorization)

        jobs = await scheduler.list_jobs()
        result = []
        for job in jobs:
            # Try to get next_run_time from APScheduler
            next_run = None
            ap_job = scheduler._scheduler.get_job(job.get("job_id", ""))
            if ap_job and ap_job.next_run_time:
                next_run = ap_job.next_run_time.isoformat()

            cron = job.get("cron_expression", "")
            result.append(
                {
                    "job_id": job.get("job_id", ""),
                    "name": job.get("job_name", ""),
                    "cron_expression": cron or None,
                    "run_at": job.get("run_at"),
                    "prompt": job.get("prompt", ""),
                    "description": job.get("description"),
                    "model": job.get("model"),
                    "status": job.get("status"),
                    "attempts": job.get("attempts"),
                    "last_error": job.get("last_error"),
                    "next_run_time": next_run,
                }
            )
        return result

    @router.delete("/jobs/{job_id}", response_model=DeleteJobResponse)
    async def delete_job(
        job_id: str,
        authorization: Optional[str] = Header(None),
    ) -> Dict[str, str]:
        """Remove a scheduled job."""
        _verify_auth(authorization)

        await scheduler.remove_job(job_id)
        logger.info("Scheduled job removed via API", job_id=job_id)
        return {"status": "deleted", "job_id": job_id}

    return router
