"""Tests for auto-discovery of git repos."""

import subprocess
import time
from pathlib import Path
from unittest.mock import patch

from src.projects.discovery import slugify, discover_active_repos


def _make_git_repo(path: Path, commit_message: str = "init") -> None:
    """Create a minimal git repo with one commit."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(path), capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(path),
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(path),
        capture_output=True,
    )
    (path / "README.md").write_text("test")
    subprocess.run(["git", "add", "."], cwd=str(path), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", commit_message],
        cwd=str(path),
        capture_output=True,
        check=True,
    )


def test_discover_finds_git_repos(tmp_path: Path) -> None:
    _make_git_repo(tmp_path / "repo-a")
    _make_git_repo(tmp_path / "repo-b")
    (tmp_path / "not-a-repo").mkdir()  # No .git

    results = discover_active_repos(tmp_path, max_results=10, max_days=1)
    slugs = {r.slug for r in results}
    assert "repo-a" in slugs
    assert "repo-b" in slugs
    assert "not-a-repo" not in slugs


def test_discover_respects_max_results(tmp_path: Path) -> None:
    for i in range(5):
        _make_git_repo(tmp_path / f"repo-{i}")

    results = discover_active_repos(tmp_path, max_results=2, max_days=1)
    assert len(results) == 2


def test_discover_excludes_pinned_slugs(tmp_path: Path) -> None:
    _make_git_repo(tmp_path / "keep-me")
    _make_git_repo(tmp_path / "skip-me")

    results = discover_active_repos(
        tmp_path, max_results=10, max_days=1, exclude_slugs={"skip-me"}
    )
    slugs = {r.slug for r in results}
    assert "keep-me" in slugs
    assert "skip-me" not in slugs


def test_discover_returns_empty_for_nonexistent_dir(tmp_path: Path) -> None:
    results = discover_active_repos(tmp_path / "nope", max_results=10, max_days=1)
    assert results == []


def test_discover_paths_are_correct(tmp_path: Path) -> None:
    _make_git_repo(tmp_path / "my-project")
    results = discover_active_repos(tmp_path, max_results=10, max_days=1)
    assert len(results) == 1
    p = results[0]
    assert p.slug == "my-project"
    assert p.name == "my-project"
    assert p.relative_path == Path("my-project")
    assert p.absolute_path == (tmp_path / "my-project").resolve()
    assert p.enabled is True


def testslugify_handles_spaces_and_underscores() -> None:
    assert slugify("Meal Planner - Django") == "meal-planner-django"
    assert slugify("claude_strategic_workspaces") == "claude-strategic-workspaces"
    assert slugify("ACE-Step-1.5") == "ace-step-15"
    assert slugify("  leading  spaces  ") == "leading-spaces"
