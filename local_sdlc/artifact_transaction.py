"""Atomic snapshots for multi-artifact apply transactions."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from .utils import unique_ordered
from .workspace import resolve_project_path


def snapshot_artifact_targets(project: Path, paths: Sequence[str]) -> dict[str, bytes | None]:
    snapshots: dict[str, bytes | None] = {}
    for path in unique_ordered(paths):
        target = resolve_project_path(project, path)
        snapshots[path] = target.read_bytes() if target.exists() else None
    return snapshots


def restore_artifact_targets(project: Path, snapshots: dict[str, bytes | None]) -> list[str]:
    restored: list[str] = []
    for path, content in snapshots.items():
        target = resolve_project_path(project, path)
        if content is None:
            if target.exists():
                target.unlink()
                restored.append(path)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        restored.append(path)
    return restored
