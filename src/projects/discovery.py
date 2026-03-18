"""Auto-discover git repositories by recent activity."""

import subprocess
from pathlib import Path
from typing import List, Optional

import structlog

from .registry import ProjectDefinition

logger = structlog.get_logger()


def discover_active_repos(
    base_directory: Path,
    max_results: int = 10,
    max_days: int = 30,
    exclude_slugs: Optional[set[str]] = None,
) -> List[ProjectDefinition]:
    """Scan base_directory for git repos with recent commit activity.

    Returns up to max_results ProjectDefinitions sorted by most recent
    commit (newest first), excluding any slugs already in exclude_slugs
    (typically pinned projects from YAML).

    Only considers immediate subdirectories that contain a .git folder.
    Repos with no commits within max_days are skipped.
    """
    exclude = exclude_slugs or set()
    candidates: list[tuple[int, ProjectDefinition]] = []

    if not base_directory.is_dir():
        logger.warning(
            "Discovery base directory does not exist",
            path=str(base_directory),
        )
        return []

    for child in base_directory.iterdir():
        if not child.is_dir():
            continue
        git_dir = child / ".git"
        if not git_dir.exists():
            continue

        slug = _slugify(child.name)
        if slug in exclude:
            continue

        timestamp = _latest_commit_timestamp(child)
        if timestamp is None:
            continue

        candidates.append(
            (
                timestamp,
                ProjectDefinition(
                    slug=slug,
                    name=child.name,
                    relative_path=child.relative_to(base_directory),
                    absolute_path=child.resolve(),
                    enabled=True,
                ),
            )
        )

    # Sort newest first
    candidates.sort(key=lambda c: c[0], reverse=True)

    # Filter by age if max_days set
    if max_days > 0:
        import time

        cutoff = int(time.time()) - (max_days * 86400)
        candidates = [(ts, p) for ts, p in candidates if ts >= cutoff]

    results = [p for _, p in candidates[:max_results]]
    logger.info(
        "Repo discovery complete",
        scanned=len(list(base_directory.iterdir())),
        with_git=len(candidates) + len(exclude),
        returned=len(results),
        excluded_pinned=len(exclude),
    )
    return results


def _latest_commit_timestamp(repo_path: Path) -> Optional[int]:
    """Get unix timestamp of most recent commit, or None."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%ct"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        raw = result.stdout.strip()
        if not raw:
            return None
        return int(raw)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return None


def _slugify(name: str) -> str:
    """Convert directory name to a URL-safe slug."""
    slug = name.lower().strip()
    # Replace spaces and underscores with hyphens
    slug = slug.replace(" ", "-").replace("_", "-")
    # Remove characters that aren't alphanumeric or hyphens
    slug = "".join(c for c in slug if c.isalnum() or c == "-")
    # Collapse multiple hyphens
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")
