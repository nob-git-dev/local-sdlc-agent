"""Workspace and project-file helpers for the local SDLC runner."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Sequence

from .models import RunnerError

def read_text_if_exists(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")

def resolve_spec_path(project: Path, spec_file: Path | None) -> Path:
    if spec_file is None:
        return project / "SPEC.md"
    if spec_file.is_absolute():
        return spec_file
    project_candidate = project / spec_file
    if project_candidate.exists():
        return project_candidate
    return spec_file

def project_manifest(project: Path, limit: int = 240) -> str:
    ignored_dirs = {".git", ".venv", "node_modules", "__pycache__", ".sdlc-runner"}
    files: list[str] = []
    for root, dirnames, filenames in os.walk(project):
        dirnames[:] = [d for d in dirnames if d not in ignored_dirs]
        root_path = Path(root)
        for filename in filenames:
            path = root_path / filename
            rel = path.relative_to(project)
            files.append(str(rel))
            if len(files) >= limit:
                files.append(f"... truncated after {limit} files")
                return "\n".join(files)
    return "\n".join(sorted(files))

def parse_context_slices(raw_slices: Sequence[str]) -> dict[str, list[tuple[int, int]]]:
    parsed: dict[str, list[tuple[int, int]]] = {}
    for raw in raw_slices:
        try:
            path_raw, start_raw, end_raw = raw.rsplit(":", 2)
        except ValueError as exc:
            raise RunnerError(f"context slice must be path:start:end: {raw}") from exc
        paths = normalize_new_files([path_raw])
        if not paths:
            raise RunnerError(f"context slice path is empty: {raw}")
        try:
            start = int(start_raw)
            end = int(end_raw)
        except ValueError as exc:
            raise RunnerError(f"context slice line numbers must be integers: {raw}") from exc
        if start < 1 or end < start:
            raise RunnerError(f"context slice needs 1 <= start <= end: {raw}")
        parsed.setdefault(paths[0], []).append((start, end))
    return parsed

def slice_text_by_lines(text: str, ranges: Sequence[tuple[int, int]]) -> str:
    lines = text.splitlines()
    chunks: list[str] = []
    for start, end in ranges:
        selected = lines[start - 1:end]
        chunks.append(f"@@ lines {start}-{end}\n" + "\n".join(selected))
    return "\n\n".join(chunks)

def collect_file_context(
    project: Path,
    includes: Sequence[str],
    max_chars: int,
    context_slices: dict[str, list[tuple[int, int]]] | None = None,
) -> str:
    chunks: list[str] = []
    remaining = max_chars
    context_slices = context_slices or {}
    for include in includes:
        path = (project / include).resolve()
        try:
            path.relative_to(project.resolve())
        except ValueError as exc:
            raise RunnerError(f"include is outside project: {include}") from exc
        if not path.exists() or not path.is_file():
            raise RunnerError(f"include file not found: {include}")
        text = path.read_text(encoding="utf-8", errors="replace")
        header = f"### {include}"
        if include in context_slices:
            text = slice_text_by_lines(text, context_slices[include])
            header = f"### {include} (selected line ranges)"
        if len(text) > remaining:
            text = text[:remaining] + "\n... truncated ..."
        chunks.append(f"{header}\n```text\n{text}\n```")
        remaining -= len(text)
        if remaining <= 0:
            break
    return "\n\n".join(chunks)


def existing_file_context_paths(project: Path, paths: Sequence[str]) -> list[str]:
    """Keep ordered context paths that are currently readable project files.

    Writable targets may legitimately name files that a later artifact will
    create. They must not be promoted to read context before that happens.
    """

    existing: list[str] = []
    for path in dict.fromkeys(paths):
        candidate = resolve_project_path(project, path)
        if candidate.is_file():
            existing.append(path)
    return existing

def python_public_symbol_ledger(project: Path, includes: Sequence[str]) -> str:
    rows: list[str] = []
    for include in includes:
        if not include.endswith(".py"):
            continue
        path = (project / include).resolve()
        try:
            path.relative_to(project.resolve())
        except ValueError as exc:
            raise RunnerError(f"include is outside project: {include}") from exc
        if not path.exists() or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        functions = sorted(set(re.findall(r"(?m)^def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", text)))
        classes = sorted(set(re.findall(r"(?m)^class\s+([A-Za-z_][A-Za-z0-9_]*)\s*[\(:]", text)))
        constants = sorted(set(re.findall(r"(?m)^([A-Z][A-Z0-9_]{2,})\s*=", text)))
        parts: list[str] = []
        if functions:
            parts.append("functions=" + ", ".join(functions[:24]))
        if classes:
            parts.append("classes=" + ", ".join(classes[:16]))
        if constants:
            parts.append("constants=" + ", ".join(constants[:24]))
        if parts:
            rows.append(f"- {include}: " + "; ".join(parts))
    if not rows:
        return ""
    return (
        "### Existing Python API symbol ledger\n"
        "Use only these visible symbols when importing from existing/context Python modules. "
        "If a local generated test wants a different descriptive name, alias an existing symbol in the import line.\n"
        + "\n".join(rows)
    )

def normalize_project_relative_paths(paths: Sequence[str], label: str = "path") -> list[str]:
    normalized: list[str] = []
    blocked_parts = {".git", ".sdlc-runner", "__pycache__"}
    for raw in paths:
        value = raw.strip()
        if not value:
            continue
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or any(part in blocked_parts for part in path.parts):
            raise RunnerError(f"{label} must be project-relative and safe: {raw}")
        if path.name.endswith(".pyc"):
            raise RunnerError(f"{label} must not target generated bytecode: {raw}")
        normalized.append(value)
    return normalized

def normalize_new_files(new_files: Sequence[str]) -> list[str]:
    return normalize_project_relative_paths(new_files, "new file path")

def resolve_project_path(project: Path, raw: str) -> Path:
    path = (project / raw).resolve()
    try:
        path.relative_to(project.resolve())
    except ValueError as exc:
        raise RunnerError(f"path is outside project: {raw}") from exc
    return path

def listed_project_files(project: Path, limit: int = 10000) -> list[str]:
    ignored_dirs = {".git", ".venv", "node_modules", "__pycache__", ".sdlc-runner"}
    files: list[str] = []
    for root, dirnames, filenames in os.walk(project):
        dirnames[:] = [d for d in dirnames if d not in ignored_dirs]
        root_path = Path(root)
        for filename in filenames:
            rel = str((root_path / filename).relative_to(project))
            files.append(rel)
            if len(files) >= limit:
                return sorted(files)
    return sorted(files)
