"""Single preflight boundary for cancellable and safety-audited actions."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Callable, Mapping, Sequence
import uuid

from .control import ensure_not_cancelled, read_progress_events, record_work_start, work_starts_after_cancel
from .budget import consume_action_budget, read_budget_events, read_budget_policy, refund_action_budget
from .models import RunnerError
from .progress_monitor import (
    ensure_not_stalled,
    read_progress_policy,
    start_progress_action,
    work_starts_after_stall,
)
from .safety import (
    SafetyDecision,
    action_safety_decision,
    authorize_safety_decision,
    blocked_reason_from_safety_decision,
    read_safety_decisions,
)


class SafetyGateDenied(RunnerError):
    """Raised when a persisted safety decision refuses an action."""

    def __init__(self, decision: Mapping[str, object]):
        self.decision = dict(decision)
        decision_id = str(decision.get("decision_id") or "unknown")
        reason = blocked_reason_from_safety_decision(decision) or "action was not authorized"
        super().__init__(f"safety decision {decision_id}: {reason}")


def begin_action(
    run_dir: Path,
    action: str,
    *,
    action_type: str,
    risk_class: str,
    metadata: Mapping[str, object] | None = None,
    control_dirs: Sequence[Path] = (),
    cancel_dirs: Sequence[Path] | None = None,
    budget_dirs: Sequence[Path] | None = None,
    progress_dirs: Sequence[Path] | None = None,
    command: str = "",
    decision: SafetyDecision | None = None,
    pre_work_check: Callable[[], None] | None = None,
) -> dict[str, object]:
    """Authorize and record one action before any side effect starts.

    Ordering invariant:
    cancel check -> stall check -> persisted SafetyDecision -> consumed budget
    -> atomic stall recheck/work_start -> execution.
    """
    cancellation_scope = tuple(control_dirs if cancel_dirs is None else cancel_dirs)
    budget_scope = tuple(control_dirs if budget_dirs is None else budget_dirs)
    progress_scope = tuple(control_dirs if progress_dirs is None else progress_dirs)
    ensure_not_cancelled(run_dir, action, cancellation_scope)
    ensure_not_stalled(run_dir, action, progress_scope)
    action_id = uuid.uuid4().hex
    action_metadata = dict(metadata or {})
    action_metadata["action_id"] = action_id
    proposed = decision or action_safety_decision(
        action,
        action_type=action_type,
        risk_class=risk_class,
        command=command,
        metadata=metadata,
    )
    proposed = replace(
        proposed,
        action=action,
        action_type=action_type,
        risk_class=risk_class,
        command=command or proposed.command,
        metadata={**dict(proposed.metadata or {}), **action_metadata},
    )
    persisted = authorize_safety_decision(run_dir, proposed)
    if str(persisted.get("decision") or "") not in {"allow", "allow_in_worktree"}:
        raise SafetyGateDenied(persisted)
    budget_event = consume_action_budget(
        run_dir,
        action,
        action_type=action_type,
        action_id=action_id,
        budget_dirs=budget_scope,
    )
    work_metadata = {
        **action_metadata,
        "safety_decision_id": persisted.get("decision_id"),
        "safety_decision": persisted.get("decision"),
        "risk_class": persisted.get("risk_class"),
    }
    if budget_event:
        work_metadata["budget_event_id"] = budget_event.get("event_id")

    def start_work() -> dict[str, object]:
        return record_work_start(
            run_dir,
            action,
            metadata=work_metadata,
            control_dirs=cancellation_scope,
            pre_start_check=pre_work_check,
        )

    try:
        start_progress_action(
            run_dir,
            action,
            action_type=action_type,
            metadata=work_metadata,
            control_dirs=progress_scope,
            start_work=start_work,
        )
    except RunnerError as exc:
        if budget_event:
            refund_action_budget(
                run_dir,
                action,
                action_type=action_type,
                action_id=action_id,
                budget_dirs=budget_scope,
                reason=f"work_start was not recorded: {exc}",
            )
        raise
    return persisted


def action_gate_audit(run_dir: Path) -> dict[str, object]:
    decisions = {
        str(item.get("decision_id")): item
        for item in read_safety_decisions(run_dir)
        if item.get("decision_id")
    }
    safety_violations: list[dict[str, object]] = []
    budget_enabled = bool(read_budget_policy(run_dir))
    all_budget_events = read_budget_events(run_dir)
    refunded_budget_event_ids = {
        str(item.get("consumed_event_id"))
        for item in all_budget_events
        if item.get("outcome") == "refunded" and item.get("consumed_event_id")
    }
    budget_events = {
        str(item.get("event_id")): item
        for item in all_budget_events
        if item.get("event_id") and item.get("outcome") == "consumed"
        and str(item.get("event_id")) not in refunded_budget_event_ids
    }
    budget_violations: list[dict[str, object]] = []
    for event in read_progress_events(run_dir):
        if not event.get("starts_work"):
            continue
        metadata = event.get("metadata")
        if isinstance(metadata, dict) and metadata.get("mirrored"):
            continue
        decision_id = str(metadata.get("safety_decision_id") or "") if isinstance(metadata, dict) else ""
        decision = decisions.get(decision_id)
        if not decision_id or decision is None:
            safety_violations.append(
                {
                    "sequence": event.get("sequence"),
                    "action": event.get("action"),
                    "reason": "work_start has no preceding persisted SafetyDecision",
                }
            )
        elif decision.get("decision") not in {"allow", "allow_in_worktree"}:
            safety_violations.append(
                {
                    "sequence": event.get("sequence"),
                    "action": event.get("action"),
                    "decision_id": decision_id,
                    "reason": "work_start references a non-authorizing SafetyDecision",
                }
            )
        if budget_enabled:
            budget_event_id = str(metadata.get("budget_event_id") or "") if isinstance(metadata, dict) else ""
            if not budget_event_id or budget_event_id not in budget_events:
                budget_violations.append(
                    {
                        "sequence": event.get("sequence"),
                        "action": event.get("action"),
                        "reason": "work_start has no preceding consumed budget event",
                    }
                )
    cancel_violations = work_starts_after_cancel(run_dir)
    stall_enabled = bool(read_progress_policy(run_dir))
    raw_stall_violations = work_starts_after_stall(run_dir) if stall_enabled else []
    if raw_stall_violations:
        # Recovery is the only controlled exception to STALLED absorption. The
        # validator rechecks the persisted plan and stall digest; a metadata flag
        # by itself never authorizes work after a stall.
        from .recovery_plan import recovery_authorization_is_valid

        stall_violations = [
            event
            for event in raw_stall_violations
            if not recovery_authorization_is_valid(event)
        ]
    else:
        stall_violations = []
    return {
        "status": "pass"
        if not cancel_violations and not stall_violations and not safety_violations and not budget_violations
        else "fail",
        "cancel_absorbing": not cancel_violations,
        "stall_absorbing": not stall_violations,
        "safety_precedes_work": not safety_violations,
        "budget_precedes_work": not budget_violations,
        "cancel_violations": cancel_violations,
        "stall_violations": stall_violations,
        "safety_violations": safety_violations,
        "budget_violations": budget_violations,
    }
