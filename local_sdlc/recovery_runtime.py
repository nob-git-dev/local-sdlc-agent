"""Runtime admission and lifecycle events for stalled-run recovery."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

from sdlc_events import TransitionKind

from .action_gate import begin_action
from .recovery_core import (
    ANALYTIC_RECOVERY_STRATEGIES,
    RECOVERY_SCHEMA_VERSION,
    InvalidRecoveryPlan,
    atomic_write_json,
    read_json_object,
    recovery_origin_file_path,
    recovery_plan_file_path,
    recovery_state_file_path,
    recovery_timestamp,
)
from .recovery_plan import recovery_authorization, validate_recovery_plan
from .runtime_events import record_runtime_transition


def begin_stalled_recovery(
    source_run_dir: Path,
    target_run_dir: Path,
    plan: Mapping[str, object] | Path,
    *,
    cancel_dirs: Sequence[Path] = (),
    budget_dirs: Sequence[Path] = (),
    progress_dirs: Sequence[Path] = (),
) -> dict[str, object]:
    source = source_run_dir.resolve()
    target = target_run_dir.resolve()
    document = validate_recovery_plan(source, plan)
    if str(document.get("target_run_dir") or "") != str(target):
        raise InvalidRecoveryPlan("recovery target does not match the plan")
    authorization = recovery_authorization(document)
    strategy = str(document["strategy"])
    action_type = {
        "resume": "resume",
        "retry": "retry",
        "split": "stage_split",
    }.get(strategy, "recovery")
    inherited_cancel_dirs = tuple(dict.fromkeys((source, *cancel_dirs)))
    inherited_budget_dirs = tuple(dict.fromkeys((source, *budget_dirs)))
    begin_action(
        target,
        f"recovery_{strategy}_start",
        action_type=action_type,
        risk_class="read_only",
        metadata={"recovery_authorization": authorization},
        cancel_dirs=inherited_cancel_dirs,
        budget_dirs=inherited_budget_dirs,
        progress_dirs=tuple(progress_dirs),
        pre_work_check=lambda: validate_recovery_plan(source, document),
    )
    planned_event_id = str(document.get("recovery_planned_event_id") or "") or None
    started_event = record_runtime_transition(
        source,
        TransitionKind.RECOVERY_STARTED,
        source_component="execution.recovery_runtime",
        source_key=f"recovery_started:{document['plan_id']}",
        payload={
            "plan_id": document["plan_id"],
            "strategy": strategy,
            "target_run_dir": str(target),
            "resumed_from": str(source),
        },
        aggregate_id=str(document["plan_id"]),
        propositions=("P04", "P05") if strategy in ANALYTIC_RECOVERY_STRATEGIES else ("P04",),
        eligibility="eligible",
        causation_id=planned_event_id,
    )
    state = {
        "schema_version": RECOVERY_SCHEMA_VERSION,
        "status": "RESUMED",
        "plan_id": document["plan_id"],
        "strategy": strategy,
        "source_run_dir": str(source),
        "target_run_dir": str(target),
        "resumed_from": str(source),
        "started_at": recovery_timestamp(),
        "recovery_started_event_id": started_event.event_id,
    }
    atomic_write_json(recovery_state_file_path(source), state)
    atomic_write_json(
        recovery_origin_file_path(target),
        {**state, "recovery_authorization": authorization},
    )
    return state


def complete_stalled_recovery(
    source_run_dir: Path,
    target_run_dir: Path,
    *,
    outcome: str,
) -> dict[str, object]:
    source = source_run_dir.resolve()
    target = target_run_dir.resolve()
    plan = validate_recovery_plan(source, recovery_plan_file_path(source))
    state = read_json_object(recovery_state_file_path(source), "recovery state")
    if str(state.get("status") or "") != "RESUMED":
        raise InvalidRecoveryPlan("recovery has not started")
    if str(state.get("target_run_dir") or "") != str(target):
        raise InvalidRecoveryPlan("recovery completion target does not match the started recovery")
    event = record_runtime_transition(
        source,
        TransitionKind.RECOVERY_COMPLETED,
        source_component="execution.recovery_runtime",
        source_key=f"recovery_completed:{plan['plan_id']}:{outcome}",
        payload={
            "plan_id": plan["plan_id"],
            "strategy": plan["strategy"],
            "target_run_dir": str(target),
            "outcome": outcome,
        },
        aggregate_id=str(plan["plan_id"]),
        propositions=(f"recovery_outcome={outcome}",),
        eligibility="eligible",
        causation_id=str(state.get("recovery_started_event_id") or "") or None,
    )
    completed = {
        **state,
        "status": "RECOVERY_COMPLETED",
        "outcome": outcome,
        "completed_at": recovery_timestamp(),
        "recovery_completed_event_id": event.event_id,
    }
    atomic_write_json(recovery_state_file_path(source), completed)
    return completed
