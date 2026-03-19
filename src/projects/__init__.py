"""Project registry and Telegram thread management."""

from .discovery import discover_active_repos, slugify
from .registry import (
    ProjectDefinition,
    ProjectRegistry,
    build_registry,
    load_pinned_projects,
    load_project_registry,
)
from .thread_manager import (
    PrivateTopicsUnavailableError,
    ProjectThreadManager,
)

__all__ = [
    "ProjectDefinition",
    "ProjectRegistry",
    "build_registry",
    "discover_active_repos",
    "slugify",
    "load_pinned_projects",
    "load_project_registry",
    "ProjectThreadManager",
    "PrivateTopicsUnavailableError",
]
