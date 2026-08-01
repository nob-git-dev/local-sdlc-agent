"""Failure-family analysis and strategy selection for stalled recovery."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from .models import RunnerError
from .recovery_core import (
    DEFAULT_FAILURE_FAMILY_THRESHOLD,
    ORDINARY_RECOVERY_STRATEGIES,
    VALID_RECOVERY_STRATEGIES,
    read_json_object,
)


def _manifest_path(run_dir: Path) -> Path | None:
    for name in ("run.json", "run.partial.json"):
        path = run_dir / name
        if path.is_file():
            return path
    return None


def _manifest(run_dir: Path) -> dict[str, object]:
    path = _manifest_path(run_dir)
    return read_json_object(path, "run manifest") if path else {}


def _failure_family(manifest: Mapping[str, object]) -> str:
    direct = manifest.get("last_functional_failure_family_signature")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    summary = manifest.get("failure_summary")
    if isinstance(summary, Mapping):
        nested = summary.get("failure_family_signature")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    return ""


def _resume_parent(run_dir: Path, manifest: Mapping[str, object]) -> Path | None:
    raw = manifest.get("resumed_from")
    if not isinstance(raw, str) or not raw.strip():
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = run_dir / path
    return path.resolve()


def _has_completed_analysis(manifest: Mapping[str, object], family: str) -> bool:
    analyses = manifest.get("failure_analyses")
    if not isinstance(analyses, list) or not family:
        return False
    for analysis in analyses:
        if not isinstance(analysis, Mapping):
            continue
        status = str(analysis.get("analysis_status") or "completed")
        signature = str(
            analysis.get("failure_family_signature")
            or analysis.get("failure_signature")
            or ""
        )
        if status not in {"aborted", "failed"} and signature == family:
            return True
    return False


def failure_family_plateau(
    run_dir: Path,
    *,
    threshold: int = DEFAULT_FAILURE_FAMILY_THRESHOLD,
    max_depth: int = 64,
) -> dict[str, object]:
    """Measure only the newest consecutive normalized failure family.

    Different families break the sequence. Generic failure types are not used as
    substitutes because doing so would over-generalize unrelated project errors.
    """
    if int(threshold) < 2:
        raise RunnerError("failure family threshold must be at least 2")
    current = run_dir.resolve()
    seen: set[str] = set()
    newest_family = ""
    consecutive = 0
    local_repeat_count = 0
    history: list[dict[str, object]] = []
    analysis_available = False

    for depth in range(max_depth):
        key = str(current)
        if key in seen:
            break
        seen.add(key)
        manifest = _manifest(current)
        if not manifest:
            break
        family = _failure_family(manifest)
        if depth == 0:
            newest_family = family
            try:
                local_repeat_count = max(0, int(manifest.get("repeated_same_failure_count", 0) or 0))
            except (TypeError, ValueError):
                local_repeat_count = 0
        history.append({"run_dir": key, "failure_family": family or None})
        if not newest_family or family != newest_family:
            break
        consecutive += 1
        analysis_available = analysis_available or _has_completed_analysis(manifest, newest_family)
        parent = _resume_parent(current, manifest)
        if parent is None:
            break
        current = parent

    observed_count = max(consecutive, local_repeat_count + 1 if newest_family else 0)
    return {
        "failure_family": newest_family or None,
        "failure_family_count": observed_count,
        "failure_family_threshold": int(threshold),
        "plateau_detected": bool(newest_family and observed_count >= int(threshold)),
        "analysis_available": analysis_available,
        "history": history,
    }


def select_recovery_strategy(
    requested: str,
    plateau: Mapping[str, object],
    target_profile: str,
) -> tuple[str, str]:
    normalized = requested.strip().lower() or "auto"
    if normalized != "auto" and normalized not in VALID_RECOVERY_STRATEGIES:
        raise RunnerError(f"unknown recovery strategy: {requested}")
    if normalized == "profile_switch" and not target_profile.strip():
        raise RunnerError("profile_switch recovery requires a target profile")

    if bool(plateau.get("plateau_detected")):
        if normalized in ORDINARY_RECOVERY_STRATEGIES or normalized == "auto":
            if bool(plateau.get("analysis_available")):
                return (
                    "root_cause_recovery",
                    "the same normalized failure family persisted after a completed failure analysis",
                )
            return (
                "failure_analysis",
                "the same normalized failure family reached the plateau threshold",
            )
    if normalized == "auto":
        return "resume", "the run is stalled but no repeated failure plateau is proven"
    return normalized, "the requested strategy is compatible with the observed failure sequence"


def next_recovery_target(source_run_dir: Path) -> Path:
    parent = source_run_dir.resolve().parent
    stem = source_run_dir.resolve().name
    index = 1
    while True:
        candidate = parent / f"{stem}-recovery-{index:02d}"
        if not candidate.exists():
            return candidate
        index += 1
