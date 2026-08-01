"""Single preflight boundary for cancellable and safety-audited actions."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Mapping, Sequence
import uuid

from .control import ensure_not_cancelled, read_progress_events, record_work_start, work_starts_after_cancel
from .models import RunnerError
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
    command: str = "",
    decision: SafetyDecision | None = None,
) -> dict[str, object]:
    """Authorize and record one action before any side effect starts.

    Ordering invariant:
    cancel check -> persisted SafetyDecision -> atomic work_start -> execution.
    """
    ensure_not_cancelled(run_dir, action, control_dirs)
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
    record_work_start(
        run_dir,
        action,
        metadata={
            **action_metadata,
            "safety_decision_id": persisted.get("decision_id"),
            "safety_decision": persisted.get("decision"),
            "risk_class": persisted.get("risk_class"),
        },
        control_dirs=control_dirs,
    )
    return persisted


def action_gate_audit(run_dir: Path) -> dict[str, object]:
    decisions = {
        str(item.get("decision_id")): item
        for item in read_safety_decisions(run_dir)
        if item.get("decision_id")
    }
    safety_violations: list[dict[str, object]] = []
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
    cancel_violations = work_starts_after_cancel(run_dir)
    return {
        "status": "pass" if not cancel_violations and not safety_violations else "fail",
        "cancel_absorbing": not cancel_violations,
        "safety_precedes_work": not safety_violations,
        "cancel_violations": cancel_violations,
        "safety_violations": safety_violations,
    }
