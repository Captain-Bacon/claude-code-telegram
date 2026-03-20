"""Tests for scheduler workspace alert system."""

from pathlib import Path

import pytest

from src.scheduler.alerts import ALERT_FILENAME, clear_alert, write_alert


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Create a temporary workspace with .claude directory."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    return tmp_path


@pytest.fixture
def sample_job() -> dict:
    return {
        "job_id": "test-job-123",
        "job_name": "daily-standup",
        "prompt": "Give me a morning status update",
        "run_at": "2026-03-20T09:00:00+00:00",
        "priority": "high",
        "on_failure": "Tell the user they missed the standup",
        "relevance_hours": 6,
        "attempts": 2,
        "last_error": "Connection refused",
    }


class TestWriteAlert:
    def test_creates_file_when_missing(self, workspace: Path, sample_job: dict) -> None:
        """Alert file is created if it doesn't exist."""
        alert_path = workspace / ALERT_FILENAME
        assert not alert_path.exists()

        write_alert(workspace, sample_job, "Delivery failed")

        assert alert_path.exists()
        content = alert_path.read_text()
        assert "## ALERT: daily-standup" in content
        assert "Delivery failed" in content

    def test_creates_claude_dir_when_missing(
        self, tmp_path: Path, sample_job: dict
    ) -> None:
        """Creates .claude directory if it doesn't exist."""
        alert_path = tmp_path / ALERT_FILENAME
        assert not alert_path.parent.exists()

        write_alert(tmp_path, sample_job, "Delivery failed")

        assert alert_path.exists()

    def test_includes_header_on_fresh_file(
        self, workspace: Path, sample_job: dict
    ) -> None:
        """Fresh alert file includes the imperative header."""
        write_alert(workspace, sample_job, "Delivery failed")

        content = (workspace / ALERT_FILENAME).read_text()
        assert "You MUST act on them" in content
        assert "Run `date`" in content

    def test_includes_job_details(self, workspace: Path, sample_job: dict) -> None:
        """Alert contains all job metadata."""
        write_alert(workspace, sample_job, "Delivery failed")

        content = (workspace / ALERT_FILENAME).read_text()
        assert "test-job-123" in content
        assert "HIGH" in content
        assert "2026-03-20T09:00:00+00:00" in content
        assert "2" in content  # attempts
        assert "Connection refused" in content

    def test_includes_on_failure_instructions(
        self, workspace: Path, sample_job: dict
    ) -> None:
        """Alert includes creator-provided recovery instructions."""
        write_alert(workspace, sample_job, "Delivery failed")

        content = (workspace / ALERT_FILENAME).read_text()
        assert "Tell the user they missed the standup" in content

    def test_includes_relevance_window(self, workspace: Path, sample_job: dict) -> None:
        """Alert includes relevance window information."""
        write_alert(workspace, sample_job, "Delivery failed")

        content = (workspace / ALERT_FILENAME).read_text()
        assert "6 hours" in content

    def test_no_on_failure_shows_fallback(
        self, workspace: Path, sample_job: dict
    ) -> None:
        """Without on_failure, alert tells agent to ask the user."""
        sample_job["on_failure"] = None
        write_alert(workspace, sample_job, "Delivery failed")

        content = (workspace / ALERT_FILENAME).read_text()
        assert "Raise this to the user" in content

    def test_appends_to_existing(self, workspace: Path, sample_job: dict) -> None:
        """Second alert appends without overwriting the first."""
        write_alert(workspace, sample_job, "First failure")

        second_job = {**sample_job, "job_id": "job-456", "job_name": "weekly-review"}
        write_alert(workspace, second_job, "Second failure")

        content = (workspace / ALERT_FILENAME).read_text()
        assert "daily-standup" in content
        assert "weekly-review" in content
        assert "First failure" in content
        assert "Second failure" in content

    def test_preserves_full_prompt(self, workspace: Path, sample_job: dict) -> None:
        """Long prompts are included in full — agent needs full context to act."""
        long_prompt = "x" * 2000
        sample_job["prompt"] = long_prompt
        write_alert(workspace, sample_job, "Delivery failed")

        content = (workspace / ALERT_FILENAME).read_text()
        assert long_prompt in content
        assert "..." not in content


class TestClearAlert:
    def test_removes_matching_alert(self, workspace: Path, sample_job: dict) -> None:
        """Clearing an alert removes it from the file."""
        write_alert(workspace, sample_job, "Delivery failed")

        result = clear_alert(workspace, "test-job-123")

        assert result is True
        content = (workspace / ALERT_FILENAME).read_text()
        assert content.strip() == ""

    def test_returns_false_when_not_found(
        self, workspace: Path, sample_job: dict
    ) -> None:
        """Returns False when the job_id isn't in the alert file."""
        write_alert(workspace, sample_job, "Delivery failed")

        result = clear_alert(workspace, "nonexistent-job")

        assert result is False

    def test_returns_false_when_no_file(self, workspace: Path) -> None:
        """Returns False when alert file doesn't exist."""
        result = clear_alert(workspace, "any-job")
        assert result is False

    def test_preserves_other_alerts(self, workspace: Path, sample_job: dict) -> None:
        """Clearing one alert preserves others."""
        write_alert(workspace, sample_job, "First failure")
        second_job = {**sample_job, "job_id": "job-456", "job_name": "weekly-review"}
        write_alert(workspace, second_job, "Second failure")

        clear_alert(workspace, "test-job-123")

        content = (workspace / ALERT_FILENAME).read_text()
        assert "daily-standup" not in content
        assert "weekly-review" in content

    def test_clear_matches_header_not_body(
        self, workspace: Path, sample_job: dict
    ) -> None:
        """Clearing only matches job_id in the header, not in prompt/error text."""
        # Job A's prompt mentions Job B's ID — clearing B must not remove A
        job_a = {
            **sample_job,
            "job_id": "job-aaa",
            "job_name": "job-a",
            "prompt": "Check status of job-bbb",
        }
        job_b = {
            **sample_job,
            "job_id": "job-bbb",
            "job_name": "job-b",
            "prompt": "Simple task",
        }
        write_alert(workspace, job_a, "Failed")
        write_alert(workspace, job_b, "Failed")

        clear_alert(workspace, "job-bbb")

        content = (workspace / ALERT_FILENAME).read_text()
        assert "job-a" in content
        assert "Check status of job-bbb" in content  # A's prompt preserved
        assert "## ALERT: job-b" not in content  # B's header gone

    def test_empties_file_when_last_alert_cleared(
        self, workspace: Path, sample_job: dict
    ) -> None:
        """File is emptied when the last alert is cleared."""
        write_alert(workspace, sample_job, "Delivery failed")

        clear_alert(workspace, "test-job-123")

        content = (workspace / ALERT_FILENAME).read_text()
        assert content.strip() == ""
