"""Exhaustive transition-to-event contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TransitionKind(str, Enum):
    RUN_PLANNED = "run_planned"
    RUN_STARTED = "run_started"
    RUN_TERMINATED = "run_terminated"
    STAGE_STARTED = "stage_started"
    STAGE_PROGRESSED = "stage_progressed"
    STAGE_STALLED = "stage_stalled"
    STAGE_CLOSED = "stage_closed"
    GOAL_STALLED = "goal_stalled"
    PROGRESS_OBSERVED = "progress_observed"
    EVIDENCE_OBSERVED = "evidence_observed"
    FAILURE_CLASSIFIED = "failure_classified"
    HYPOTHESIS_REJECTED = "hypothesis_rejected"
    REGRESSION_MEMORY_RECORDED = "regression_memory_recorded"
    SAFETY_DECIDED = "safety_decided"
    ACTION_ADMITTED = "action_admitted"
    ACTION_BLOCKED = "action_blocked"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_CONSUMED = "approval_consumed"
    INTERVENTION_APPLIED = "intervention_applied"
    VERIFICATION_COMPLETED = "verification_completed"
    RECOVERY_PLANNED = "recovery_planned"
    RECOVERY_STARTED = "recovery_started"
    RECOVERY_COMPLETED = "recovery_completed"
    BUDGET_CONSUMED = "budget_consumed"
    BUDGET_REFUNDED = "budget_refunded"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CANCELLATION_REQUESTED = "cancellation_requested"
    CANCELLATION_ENFORCED = "cancellation_enforced"
    EVENT_CONTRACT_VIOLATION = "event_contract_violation"


class EventType(str, Enum):
    RUN_PLANNED = "run_planned"
    RUN_STARTED = "run_started"
    RUN_TERMINATED = "run_terminated"
    STAGE_STARTED = "stage_started"
    STAGE_PROGRESSED = "stage_progressed"
    STAGE_STALLED = "stage_stalled"
    STAGE_CLOSED = "stage_closed"
    GOAL_STALLED = "goal_stalled"
    PROGRESS_OBSERVED = "progress_observed"
    EVIDENCE_OBSERVED = "evidence_observed"
    FAILURE_CLASSIFIED = "failure_classified"
    HYPOTHESIS_REJECTED = "hypothesis_rejected"
    REGRESSION_MEMORY_RECORDED = "regression_memory_recorded"
    SAFETY_DECISION_RECORDED = "safety_decision_recorded"
    ACTION_ADMITTED = "action_admitted"
    ACTION_BLOCKED = "action_blocked"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_CONSUMED = "approval_consumed"
    INTERVENTION_APPLIED = "intervention_applied"
    VERIFICATION_COMPLETED = "verification_completed"
    RECOVERY_PLANNED = "recovery_planned"
    RECOVERY_STARTED = "recovery_started"
    RECOVERY_COMPLETED = "recovery_completed"
    BUDGET_CONSUMED = "budget_consumed"
    BUDGET_REFUNDED = "budget_refunded"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CANCELLATION_REQUESTED = "cancellation_requested"
    CANCELLATION_ENFORCED = "cancellation_enforced"
    EVENT_CONTRACT_VIOLATION = "event_contract_violation"


@dataclass(frozen=True)
class TransitionContract:
    transition_kind: TransitionKind
    event_type: EventType
    aggregate_type: str
    learning_relevant: bool = True
    closure_transition: bool = False


def _contract(
    transition: TransitionKind,
    event: EventType,
    aggregate_type: str,
    *,
    learning_relevant: bool = True,
    closure_transition: bool = False,
) -> TransitionContract:
    return TransitionContract(
        transition_kind=transition,
        event_type=event,
        aggregate_type=aggregate_type,
        learning_relevant=learning_relevant,
        closure_transition=closure_transition,
    )


EVENT_CONTRACTS: dict[TransitionKind, TransitionContract] = {
    TransitionKind.RUN_PLANNED: _contract(TransitionKind.RUN_PLANNED, EventType.RUN_PLANNED, "goal"),
    TransitionKind.RUN_STARTED: _contract(TransitionKind.RUN_STARTED, EventType.RUN_STARTED, "goal"),
    TransitionKind.RUN_TERMINATED: _contract(
        TransitionKind.RUN_TERMINATED, EventType.RUN_TERMINATED, "goal", closure_transition=True
    ),
    TransitionKind.STAGE_STARTED: _contract(TransitionKind.STAGE_STARTED, EventType.STAGE_STARTED, "stage"),
    TransitionKind.STAGE_PROGRESSED: _contract(TransitionKind.STAGE_PROGRESSED, EventType.STAGE_PROGRESSED, "stage"),
    TransitionKind.STAGE_STALLED: _contract(TransitionKind.STAGE_STALLED, EventType.STAGE_STALLED, "stage"),
    TransitionKind.STAGE_CLOSED: _contract(
        TransitionKind.STAGE_CLOSED, EventType.STAGE_CLOSED, "stage", closure_transition=True
    ),
    TransitionKind.GOAL_STALLED: _contract(
        TransitionKind.GOAL_STALLED, EventType.GOAL_STALLED, "goal"
    ),
    TransitionKind.PROGRESS_OBSERVED: _contract(
        TransitionKind.PROGRESS_OBSERVED, EventType.PROGRESS_OBSERVED, "goal"
    ),
    TransitionKind.EVIDENCE_OBSERVED: _contract(
        TransitionKind.EVIDENCE_OBSERVED, EventType.EVIDENCE_OBSERVED, "verification"
    ),
    TransitionKind.FAILURE_CLASSIFIED: _contract(
        TransitionKind.FAILURE_CLASSIFIED, EventType.FAILURE_CLASSIFIED, "verification"
    ),
    TransitionKind.HYPOTHESIS_REJECTED: _contract(
        TransitionKind.HYPOTHESIS_REJECTED, EventType.HYPOTHESIS_REJECTED, "verification"
    ),
    TransitionKind.REGRESSION_MEMORY_RECORDED: _contract(
        TransitionKind.REGRESSION_MEMORY_RECORDED,
        EventType.REGRESSION_MEMORY_RECORDED,
        "knowledge",
    ),
    TransitionKind.SAFETY_DECIDED: _contract(
        TransitionKind.SAFETY_DECIDED, EventType.SAFETY_DECISION_RECORDED, "action"
    ),
    TransitionKind.ACTION_ADMITTED: _contract(
        TransitionKind.ACTION_ADMITTED, EventType.ACTION_ADMITTED, "action"
    ),
    TransitionKind.ACTION_BLOCKED: _contract(
        TransitionKind.ACTION_BLOCKED, EventType.ACTION_BLOCKED, "action"
    ),
    TransitionKind.APPROVAL_REQUIRED: _contract(
        TransitionKind.APPROVAL_REQUIRED, EventType.APPROVAL_REQUIRED, "action"
    ),
    TransitionKind.APPROVAL_GRANTED: _contract(
        TransitionKind.APPROVAL_GRANTED, EventType.APPROVAL_GRANTED, "action"
    ),
    TransitionKind.APPROVAL_CONSUMED: _contract(
        TransitionKind.APPROVAL_CONSUMED, EventType.APPROVAL_CONSUMED, "action"
    ),
    TransitionKind.INTERVENTION_APPLIED: _contract(
        TransitionKind.INTERVENTION_APPLIED, EventType.INTERVENTION_APPLIED, "action"
    ),
    TransitionKind.VERIFICATION_COMPLETED: _contract(
        TransitionKind.VERIFICATION_COMPLETED, EventType.VERIFICATION_COMPLETED, "verification"
    ),
    TransitionKind.RECOVERY_PLANNED: _contract(
        TransitionKind.RECOVERY_PLANNED, EventType.RECOVERY_PLANNED, "stage"
    ),
    TransitionKind.RECOVERY_STARTED: _contract(
        TransitionKind.RECOVERY_STARTED, EventType.RECOVERY_STARTED, "stage"
    ),
    TransitionKind.RECOVERY_COMPLETED: _contract(
        TransitionKind.RECOVERY_COMPLETED, EventType.RECOVERY_COMPLETED, "stage"
    ),
    TransitionKind.BUDGET_CONSUMED: _contract(
        TransitionKind.BUDGET_CONSUMED, EventType.BUDGET_CONSUMED, "action"
    ),
    TransitionKind.BUDGET_REFUNDED: _contract(
        TransitionKind.BUDGET_REFUNDED, EventType.BUDGET_REFUNDED, "action"
    ),
    TransitionKind.BUDGET_EXHAUSTED: _contract(
        TransitionKind.BUDGET_EXHAUSTED, EventType.BUDGET_EXHAUSTED, "action"
    ),
    TransitionKind.CANCELLATION_REQUESTED: _contract(
        TransitionKind.CANCELLATION_REQUESTED, EventType.CANCELLATION_REQUESTED, "goal"
    ),
    TransitionKind.CANCELLATION_ENFORCED: _contract(
        TransitionKind.CANCELLATION_ENFORCED, EventType.CANCELLATION_ENFORCED, "goal"
    ),
    TransitionKind.EVENT_CONTRACT_VIOLATION: _contract(
        TransitionKind.EVENT_CONTRACT_VIOLATION,
        EventType.EVENT_CONTRACT_VIOLATION,
        "goal",
        learning_relevant=False,
    ),
}


def validate_contract_registry() -> list[str]:
    findings: list[str] = []
    transitions = set(TransitionKind)
    registered = set(EVENT_CONTRACTS)
    for missing in sorted(transitions - registered, key=lambda item: item.value):
        findings.append(f"missing_contract:{missing.value}")
    for unknown in sorted(registered - transitions, key=lambda item: item.value):
        findings.append(f"unknown_contract:{unknown.value}")
    for transition, contract in EVENT_CONTRACTS.items():
        if transition != contract.transition_kind:
            findings.append(f"contract_key_mismatch:{transition.value}")
    return findings


def contract_for(value: TransitionKind | str) -> TransitionContract:
    transition = value if isinstance(value, TransitionKind) else TransitionKind(value)
    try:
        return EVENT_CONTRACTS[transition]
    except KeyError as exc:
        raise ValueError(f"transition has no event contract: {transition.value}") from exc
