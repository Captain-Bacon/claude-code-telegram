"""YAML-backed project registry for thread mode."""

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import structlog
import yaml

logger = structlog.get_logger()


@dataclass(frozen=True)
class ProjectDefinition:
    """Project entry from YAML configuration or auto-discovery."""

    slug: str
    name: str
    relative_path: Path
    absolute_path: Path
    enabled: bool = True


class ProjectRegistry:
    """In-memory validated project registry."""

    def __init__(self, projects: List[ProjectDefinition]) -> None:
        self._projects = projects
        self._by_slug: Dict[str, ProjectDefinition] = {p.slug: p for p in projects}

    @property
    def projects(self) -> List[ProjectDefinition]:
        """Return all projects."""
        return list(self._projects)

    def list_enabled(self) -> List[ProjectDefinition]:
        """Return enabled projects only."""
        return [p for p in self._projects if p.enabled]

    def get_by_slug(self, slug: str) -> Optional[ProjectDefinition]:
        """Get project by slug."""
        return self._by_slug.get(slug)


def load_pinned_projects(
    config_path: Path, approved_directory: Path
) -> List[ProjectDefinition]:
    """Load pinned project definitions from YAML.

    Returns an empty list if the file is empty or has no projects entry.
    Raises ValueError for malformed entries.
    """
    if not config_path.exists():
        raise ValueError(f"Projects config file does not exist: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    if not isinstance(data, dict):
        raise ValueError("Projects config must be a YAML object")

    raw_projects = data.get("projects")
    if not raw_projects:
        return []
    if not isinstance(raw_projects, list):
        raise ValueError("Projects 'projects' key must be a list")

    approved_root = approved_directory.resolve()
    seen_slugs: set[str] = set()
    seen_names: set[str] = set()
    seen_rel_paths: set[str] = set()
    projects: List[ProjectDefinition] = []

    for idx, raw in enumerate(raw_projects):
        if not isinstance(raw, dict):
            raise ValueError(f"Project entry at index {idx} must be an object")

        slug = str(raw.get("slug", "")).strip()
        name = str(raw.get("name", "")).strip()
        rel_path_raw = str(raw.get("path", "")).strip()
        enabled = bool(raw.get("enabled", True))

        if not slug:
            raise ValueError(f"Project entry at index {idx} is missing 'slug'")
        if not name:
            raise ValueError(f"Project '{slug}' is missing 'name'")
        if not rel_path_raw:
            raise ValueError(f"Project '{slug}' is missing 'path'")

        rel_path = Path(rel_path_raw)
        if rel_path.is_absolute():
            raise ValueError(f"Project '{slug}' path must be relative: {rel_path_raw}")

        absolute_path = (approved_root / rel_path).resolve()

        try:
            absolute_path.relative_to(approved_root)
        except ValueError as e:
            raise ValueError(
                f"Project '{slug}' path outside approved " f"directory: {rel_path_raw}"
            ) from e

        if not absolute_path.exists() or not absolute_path.is_dir():
            logger.warning(
                "Pinned project path missing, skipping",
                slug=slug,
                path=str(absolute_path),
            )
            continue

        rel_path_norm = str(rel_path)
        if slug in seen_slugs:
            raise ValueError(f"Duplicate project slug: {slug}")
        if name in seen_names:
            raise ValueError(f"Duplicate project name: {name}")
        if rel_path_norm in seen_rel_paths:
            raise ValueError(f"Duplicate project path: {rel_path_norm}")

        seen_slugs.add(slug)
        seen_names.add(name)
        seen_rel_paths.add(rel_path_norm)

        projects.append(
            ProjectDefinition(
                slug=slug,
                name=name,
                relative_path=rel_path,
                absolute_path=absolute_path,
                enabled=enabled,
            )
        )

    return projects


def build_registry(
    pinned: List[ProjectDefinition],
    discovered: List[ProjectDefinition],
) -> ProjectRegistry:
    """Merge pinned and discovered projects into a registry.

    Pinned projects come first. Discovered projects are appended
    if their slug doesn't collide with a pinned entry.
    """
    by_slug: set[str] = set()
    by_name: set[str] = set()
    merged: List[ProjectDefinition] = []

    for p in pinned:
        by_slug.add(p.slug)
        by_name.add(p.name)
        merged.append(p)

    for p in discovered:
        if p.slug in by_slug or p.name in by_name:
            continue
        by_slug.add(p.slug)
        by_name.add(p.name)
        merged.append(p)

    return ProjectRegistry(merged)


# Backwards-compatible loader for existing code paths
def load_project_registry(
    config_path: Path, approved_directory: Path
) -> ProjectRegistry:
    """Load project registry from YAML only (no discovery)."""
    pinned = load_pinned_projects(config_path, approved_directory)
    return ProjectRegistry(pinned)
