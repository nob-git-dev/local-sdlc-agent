"""Run directory, resume, and worktree state helpers."""

from __future__ import annotations

import datetime as _datetime
import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Sequence

from .artifacts import observation_summary_document
from .models import GENERATED_DIR, RunnerError, Skill
from .utils import truncate_text, unique_ordered
from .workspace import resolve_project_path


def resolve_run_dir(project: Path, requested: Path | None = None) -> Path:
    """Resolve a run directory without creating or otherwise mutating it."""
    if requested is not None:
        run_dir = requested
        if not run_dir.is_absolute():
            cwd_candidate = run_dir.resolve()
            project_resolved = project.resolve()
            if cwd_candidate == project_resolved or cwd_candidate.is_relative_to(project_resolved):
                run_dir = cwd_candidate
            else:
                run_dir = project / run_dir
    else:
        timestamp = _datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        run_dir = project / GENERATED_DIR / "runs" / timestamp
    return run_dir


def make_run_dir(project: Path, requested: Path | None = None) -> Path:
    run_dir = resolve_run_dir(project, requested)
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir

def slugify(text: str, limit: int = 48) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    if not slug:
        slug = "stage"
    return slug[:limit].rstrip("-") or "stage"

def resolve_document_path(project: Path, run_dir: Path, raw: str) -> Path:
    path = Path(raw)
    if path.is_absolute():
        return path
    project_candidate = project / path
    if project_candidate.exists():
        return project_candidate
    return run_dir / path

def load_resume_context(resume_dir: Path, project: Path) -> tuple[dict[str, object], list[tuple[str, str]], list[Path]]:
    manifest_path = resume_dir / "run.json"
    if not manifest_path.exists():
        partial_manifest_path = resume_dir / "run.partial.json"
        if partial_manifest_path.exists():
            manifest_path = partial_manifest_path
    if not manifest_path.exists():
        raise RunnerError(f"resume run.json not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    documents: list[tuple[str, str]] = []
    command_documents: list[tuple[str, str]] = []
    paths: list[Path] = []
    for raw in manifest.get("documents", []):
        if not isinstance(raw, str) or raw.endswith("run.json"):
            continue
        path = resolve_document_path(project, resume_dir, raw)
        if not path.exists() or path.suffix.lower() != ".md":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "## Command Result" in text:
            command_documents.append((f"Resume command: {path.name}", text))
            limit = 3500
        elif "observation-summary" in path.name:
            limit = 8000
        else:
            limit = 6000
        documents.append((f"Resume context: {path.name}", truncate_text(text, limit)))
        paths.append(path)
    if command_documents:
        completed_rounds = int(manifest.get("completed_rounds", 0) or 0)
        summary = observation_summary_document(completed_rounds, command_documents[-8:])
        documents.append(("Resume observation summary", summary))
    return manifest, documents[-8:], paths

def create_copy_worktree(project: Path) -> Path:
    temp_root = Path(tempfile.mkdtemp(prefix="local-sdlc-worktree-"))
    target = temp_root / "project"
    shutil.copytree(
        project,
        target,
        ignore=shutil.ignore_patterns(".git", ".sdlc-runner", "__pycache__", "*.pyc"),
    )
    return target


def changed_allowed_paths_between(
    source_project: Path,
    target_project: Path,
    paths: Sequence[str],
) -> list[str]:
    """Return allowed file paths whose resumable source bytes differ.

    A standalone ``--resume-worktree-path`` has no previous run manifest from
    which to restore ``changed_paths``. Comparing only declared writable paths
    preserves completed work from earlier rounds without widening copy-back
    authority to unrelated files.
    """

    changed: list[str] = []
    for raw in unique_ordered(paths):
        source = resolve_project_path(source_project, raw)
        target = resolve_project_path(target_project, raw)
        if not source.is_file():
            continue
        if not target.is_file() or source.read_bytes() != target.read_bytes():
            changed.append(raw)
    return changed


def copy_allowed_paths_back(source_project: Path, target_project: Path, paths: Sequence[str]) -> list[str]:
    copied: list[str] = []
    for raw in unique_ordered(paths):
        source = resolve_project_path(source_project, raw)
        target = resolve_project_path(target_project, raw)
        if not source.exists() or not source.is_file():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(raw)
    return copied

def required_skill(skills: dict[str, Skill], name: str) -> Skill:
    try:
        return skills[name]
    except KeyError as exc:
        raise RunnerError(f"required skill not found: {name}") from exc

def round_filename(round_index: int, kind: str, auto_fix: bool) -> str:
    if not auto_fix:
        if kind == "coder":
            return "03-coder-output.md"
        if kind == "judge":
            return "04-judge-review.md"
    prefix = "03" if kind == "coder" else "04"
    suffix = "coder-output" if kind == "coder" else "judge-review"
    return f"{prefix}-r{round_index:02d}-{suffix}.md"
