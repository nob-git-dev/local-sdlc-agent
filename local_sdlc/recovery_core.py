"""Shared types and persistence helpers for stalled-run recovery."""

from __future__ import annotations

import datetime as _datetime
import hashlib
import json
from pathlib import Path
from typing import Mapping

from .models import RunnerError


RECOVERY_PLAN_FILENAME = "recovery_plan.json"
RECOVERY_STATE_FILENAME = "recovery_state.json"
RECOVERY_ORIGIN_FILENAME = "recovery_origin.json"
RECOVERY_SCHEMA_VERSION = 1
DEFAULT_FAILURE_FAMILY_THRESHOLD = 2

ORDINARY_RECOVERY_STRATEGIES = {
    "resume",
    "retry",
    "split",
    "profile_switch",
}
ANALYTIC_RECOVERY_STRATEGIES = {
    "failure_analysis",
    "root_cause_recovery",
}
VALID_RECOVERY_STRATEGIES = ORDINARY_RECOVERY_STRATEGIES | ANALYTIC_RECOVERY_STRATEGIES


class RecoveryPlanRequired(RunnerError):
    """Raised when a plain resume tries to cross a persistent STALLED state."""


class InvalidRecoveryPlan(RunnerError):
    """Raised when recovery evidence or plan identity cannot be verified."""


def recovery_timestamp() -> str:
    return _datetime.datetime.now(tz=_datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def recovery_plan_file_path(run_dir: Path) -> Path:
    return run_dir.resolve() / RECOVERY_PLAN_FILENAME


def recovery_state_file_path(run_dir: Path) -> Path:
    return run_dir.resolve() / RECOVERY_STATE_FILENAME


def recovery_origin_file_path(run_dir: Path) -> Path:
    return run_dir.resolve() / RECOVERY_ORIGIN_FILENAME


def atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def read_json_object(path: Path, label: str) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InvalidRecoveryPlan(f"invalid {label}: {path}") from exc
    if not isinstance(payload, dict):
        raise InvalidRecoveryPlan(f"invalid {label}: {path}")
    return payload


def sha256_file(path: Path) -> str:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise InvalidRecoveryPlan(f"recovery evidence is not readable: {path}") from exc
    return hashlib.sha256(content).hexdigest()
