"""Immutable, evidence-bound plans for recovering persistently stalled runs."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from sdlc_events import (
    EvidenceReference,
    EventType,
    RuntimeEventLedger,
    TransitionKind,
    stable_identifier,
)

from .models import RunnerError
from .progress_monitor import read_stall_state, stall_file_path
from .recovery_analysis import (
    failure_family_plateau,
    next_recovery_target,
    select_recovery_strategy,
)
from .recovery_core import (
    ANALYTIC_RECOVERY_STRATEGIES,
    DEFAULT_FAILURE_FAMILY_THRESHOLD,
    RECOVERY_SCHEMA_VERSION,
    VALID_RECOVERY_STRATEGIES,
    InvalidRecoveryPlan,
    RecoveryPlanRequired,
    atomic_write_json,
    read_json_object,
    recovery_plan_file_path,
    recovery_timestamp,
    sha256_file,
)
from .runtime_events import record_runtime_transition


def _latest_stall_event_id(run_dir: Path) -> str | None:
    try:
        events = RuntimeEventLedger(run_dir).list_events()
    except Exception:
        return None
    stalled_types = {EventType.GOAL_STALLED.value, EventType.STAGE_STALLED.value}
    for event in reversed(events):
        if event.event_type in stalled_types:
            return event.event_id
    return None


def read_recovery_plan(run_dir: Path) -> dict[str, object]:
    return read_json_object(recovery_plan_file_path(run_dir), "recovery plan")


def validate_recovery_plan(
    source_run_dir: Path,
    plan: Mapping[str, object] | Path,
) -> dict[str, object]:
    source = source_run_dir.resolve()
    document = read_json_object(plan.resolve(), "recovery plan") if isinstance(plan, Path) else dict(plan)
    if int(document.get("schema_version", 0) or 0) != RECOVERY_SCHEMA_VERSION:
        raise InvalidRecoveryPlan("unsupported recovery plan schema")
    if str(document.get("status") or "") != "RECOVERY_PLANNED":
        raise InvalidRecoveryPlan("recovery plan is not in RECOVERY_PLANNED state")
    if str(document.get("source_run_dir") or "") != str(source):
        raise InvalidRecoveryPlan("recovery plan source does not match the stalled run")
    strategy = str(document.get("strategy") or "")
    if strategy not in VALID_RECOVERY_STRATEGIES:
        raise InvalidRecoveryPlan(f"invalid recovery strategy: {strategy or '(missing)'}")
    target = Path(str(document.get("target_run_dir") or "")).expanduser()
    if not target.is_absolute() or target.resolve() == source:
        raise InvalidRecoveryPlan("recovery target must be a different absolute run directory")
    stall_path = stall_file_path(source)
    stall = read_stall_state(source)
    if not stall or str(stall.get("status") or "") != "STALLED":
        raise InvalidRecoveryPlan("recovery source is not persistently STALLED")
    expected_digest = str(document.get("source_stall_sha256") or "")
    actual_digest = sha256_file(stall_path)
    if len(expected_digest) != 64 or expected_digest != actual_digest:
        raise InvalidRecoveryPlan("recovery stall evidence hash does not match")
    expected_id = stable_identifier(
        "RP",
        source,
        expected_digest,
        strategy,
        str(document.get("target_profile") or ""),
        target.resolve(),
    )
    if str(document.get("plan_id") or "") != expected_id:
        raise InvalidRecoveryPlan("recovery plan identity does not match its evidence")
    return document


def plan_stalled_recovery(
    source_run_dir: Path,
    *,
    requested_strategy: str = "auto",
    failure_family_threshold: int = DEFAULT_FAILURE_FAMILY_THRESHOLD,
    target_run_dir: Path | None = None,
    target_profile: str = "",
) -> dict[str, object]:
    source = source_run_dir.resolve()
    stall_path = stall_file_path(source)
    stall = read_stall_state(source)
    if not stall or str(stall.get("status") or "") != "STALLED":
        raise InvalidRecoveryPlan(f"run is not persistently STALLED: {source}")
    stall_digest = sha256_file(stall_path)
    plateau = failure_family_plateau(source, threshold=failure_family_threshold)
    strategy, rationale = select_recovery_strategy(requested_strategy, plateau, target_profile)
    target = (target_run_dir or next_recovery_target(source)).expanduser().resolve()
    plan_id = stable_identifier(
        "RP",
        source,
        stall_digest,
        strategy,
        target_profile,
        target,
    )
    existing = read_recovery_plan(source)
    if existing:
        validated = validate_recovery_plan(source, existing)
        same_request = (
            str(validated.get("requested_strategy") or "") == requested_strategy
            and str(validated.get("target_run_dir") or "") == str(target)
            and int(validated.get("failure_family_threshold", 0) or 0) == int(failure_family_threshold)
            and str(validated.get("target_profile") or "") == target_profile
        )
        if not same_request:
            raise InvalidRecoveryPlan("a different immutable recovery plan already exists for this stall")
        return validated

    plan: dict[str, object] = {
        "schema_version": RECOVERY_SCHEMA_VERSION,
        "plan_id": plan_id,
        "status": "RECOVERY_PLANNED",
        "created_at": recovery_timestamp(),
        "source_run_dir": str(source),
        "source_stall_sha256": stall_digest,
        "source_stall_event_id": _latest_stall_event_id(source),
        "requested_strategy": requested_strategy,
        "strategy": strategy,
        "strategy_class": "analysis" if strategy in ANALYTIC_RECOVERY_STRATEGIES else "ordinary",
        "rationale": rationale,
        "failure_family": plateau.get("failure_family"),
        "failure_family_count": plateau.get("failure_family_count", 0),
        "failure_family_threshold": plateau.get("failure_family_threshold"),
        "plateau_detected": plateau.get("plateau_detected", False),
        "analysis_available": plateau.get("analysis_available", False),
        "failure_history": plateau.get("history", []),
        "target_run_dir": str(target),
        "target_profile": target_profile or None,
        "resumed_from": str(source),
    }
    evidence = EvidenceReference(
        path=stall_path.name,
        sha256=stall_digest,
        media_type="application/json",
    )
    event = record_runtime_transition(
        source,
        TransitionKind.RECOVERY_PLANNED,
        source_component="execution.recovery_planner",
        source_key=f"recovery_plan:{plan_id}",
        payload={"recovery_plan": plan},
        aggregate_id=plan_id,
        propositions=("P04", "P05") if bool(plateau.get("plateau_detected")) else ("P04",),
        evidence_refs=(evidence,),
        eligibility="eligible",
        causation_id=str(plan.get("source_stall_event_id") or "") or None,
    )
    plan["recovery_planned_event_id"] = event.event_id
    atomic_write_json(recovery_plan_file_path(source), plan)
    return plan


def require_recovery_plan_for_resume(
    source_run_dir: Path,
    plan: Mapping[str, object] | Path | None,
) -> dict[str, object]:
    source = source_run_dir.resolve()
    if not read_stall_state(source):
        return {}
    if plan is None:
        raise RecoveryPlanRequired(
            f"stalled run requires an evidence-bound recovery plan before resume: {source}"
        )
    return validate_recovery_plan(source, plan)


def recovery_authorization(plan: Mapping[str, object]) -> dict[str, object]:
    source = Path(str(plan.get("source_run_dir") or "")).resolve()
    return {
        "plan_id": plan.get("plan_id"),
        "plan_path": str(recovery_plan_file_path(source)),
        "source_run_dir": str(source),
        "source_stall_sha256": plan.get("source_stall_sha256"),
        "target_run_dir": plan.get("target_run_dir"),
        "strategy": plan.get("strategy"),
    }


def recovery_authorization_is_valid(event: Mapping[str, object]) -> bool:
    metadata = event.get("metadata")
    if not isinstance(metadata, Mapping):
        return False
    authorization = metadata.get("recovery_authorization")
    if not isinstance(authorization, Mapping):
        return False
    try:
        plan_path = Path(str(authorization.get("plan_path") or "")).resolve()
        source = Path(str(authorization.get("source_run_dir") or "")).resolve()
        if plan_path != recovery_plan_file_path(source):
            return False
        plan = validate_recovery_plan(source, plan_path)
    except (RunnerError, OSError, ValueError):
        return False
    if str(authorization.get("plan_id") or "") != str(plan.get("plan_id") or ""):
        return False
    if str(authorization.get("strategy") or "") != str(plan.get("strategy") or ""):
        return False
    if str(authorization.get("target_run_dir") or "") != str(plan.get("target_run_dir") or ""):
        return False
    child = str(metadata.get("child_run_dir") or "")
    return not child or str(Path(child).resolve()) == str(plan.get("target_run_dir") or "")
