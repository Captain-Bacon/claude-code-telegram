"""Tests for merging pinned + discovered projects."""

from pathlib import Path

from src.projects.registry import ProjectDefinition, build_registry


def test_build_registry_pinned_first() -> None:
    pinned = [
        ProjectDefinition(
            slug="pinned",
            name="Pinned",
            relative_path=Path("pinned"),
            absolute_path=Path("/x/pinned"),
        )
    ]
    discovered = [
        ProjectDefinition(
            slug="disc",
            name="Disc",
            relative_path=Path("disc"),
            absolute_path=Path("/x/disc"),
        )
    ]
    reg = build_registry(pinned, discovered)
    assert [p.slug for p in reg.projects] == ["pinned", "disc"]


def test_build_registry_deduplicates_by_slug() -> None:
    pinned = [
        ProjectDefinition(
            slug="same",
            name="Pinned Name",
            relative_path=Path("a"),
            absolute_path=Path("/x/a"),
        )
    ]
    discovered = [
        ProjectDefinition(
            slug="same",
            name="Disc Name",
            relative_path=Path("b"),
            absolute_path=Path("/x/b"),
        )
    ]
    reg = build_registry(pinned, discovered)
    assert len(reg.projects) == 1
    assert reg.projects[0].name == "Pinned Name"


def test_build_registry_deduplicates_by_name() -> None:
    pinned = [
        ProjectDefinition(
            slug="pin-slug",
            name="Same Name",
            relative_path=Path("a"),
            absolute_path=Path("/x/a"),
        )
    ]
    discovered = [
        ProjectDefinition(
            slug="disc-slug",
            name="Same Name",
            relative_path=Path("b"),
            absolute_path=Path("/x/b"),
        )
    ]
    reg = build_registry(pinned, discovered)
    assert len(reg.projects) == 1
    assert reg.projects[0].slug == "pin-slug"


def test_build_registry_empty_inputs() -> None:
    reg = build_registry([], [])
    assert reg.projects == []
