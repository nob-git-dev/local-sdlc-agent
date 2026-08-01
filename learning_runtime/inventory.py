"""Declared inventory of execution-plane state mutations and event hooks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sdlc_events import TransitionKind, contract_for


@dataclass(frozen=True)
class MutationContract:
    projection: str
    writer: str
    runtime_hook: str
    legacy_adapter: str
    transitions: tuple[TransitionKind, ...]
    canonical_role: str


MUTATION_CONTRACTS: tuple[MutationContract, ...] = (
    MutationContract(
        "cancel.json",
        "local_sdlc.control.request_cancel",
        "local_sdlc.runtime_events.record_progress_payload",
        "learning_runtime.legacy._import_progress",
        (TransitionKind.CANCELLATION_REQUESTED,),
        "compatibility_projection",
    ),
    MutationContract(
        "progress.jsonl",
        "local_sdlc.control._append_progress_event_unlocked",
        "local_sdlc.runtime_events.record_progress_payload",
        "learning_runtime.legacy._import_progress",
        (
            TransitionKind.ACTION_ADMITTED,
            TransitionKind.PROGRESS_OBSERVED,
            TransitionKind.STAGE_PROGRESSED,
            TransitionKind.GOAL_STALLED,
            TransitionKind.STAGE_STALLED,
        ),
        "compatibility_projection",
    ),
    MutationContract(
        "safety_decisions.jsonl",
        "local_sdlc.safety._record_safety_decision_unlocked",
        "local_sdlc.runtime_events.record_safety_payload",
        "learning_runtime.legacy._import_safety",
        (
            TransitionKind.SAFETY_DECIDED,
            TransitionKind.ACTION_BLOCKED,
            TransitionKind.APPROVAL_REQUIRED,
        ),
        "compatibility_projection",
    ),
    MutationContract(
        "safety_approvals.jsonl",
        "local_sdlc.safety._append_approval_event_unlocked",
        "local_sdlc.runtime_events.record_approval_payload",
        "learning_runtime.legacy._import_safety",
        (TransitionKind.APPROVAL_GRANTED, TransitionKind.APPROVAL_CONSUMED),
        "compatibility_projection",
    ),
    MutationContract(
        "budget_events.jsonl",
        "local_sdlc.budget._append_budget_event_unlocked",
        "local_sdlc.runtime_events.record_budget_payload",
        "learning_runtime.legacy._import_budget",
        (
            TransitionKind.BUDGET_CONSUMED,
            TransitionKind.BUDGET_REFUNDED,
            TransitionKind.BUDGET_EXHAUSTED,
        ),
        "compatibility_projection",
    ),
    MutationContract(
        "budget_stop.json",
        "local_sdlc.budget._persist_stop_unlocked",
        "local_sdlc.runtime_events.record_budget_payload",
        "learning_runtime.legacy._import_budget",
        (TransitionKind.BUDGET_EXHAUSTED,),
        "derived_projection",
    ),
    MutationContract(
        "progress_policy.json/progress_state.json",
        "local_sdlc.progress_monitor.initialize_progress_monitor/observe_progress",
        "local_sdlc.runtime_events.record_scope_started/record_progress_payload",
        "learning_runtime.legacy._import_progress",
        (
            TransitionKind.RUN_STARTED,
            TransitionKind.STAGE_STARTED,
            TransitionKind.PROGRESS_OBSERVED,
            TransitionKind.STAGE_PROGRESSED,
        ),
        "derived_projection",
    ),
    MutationContract(
        "stall.json",
        "local_sdlc.progress_monitor._persist_stall_unlocked",
        "local_sdlc.runtime_events.record_progress_payload",
        "learning_runtime.legacy._import_stall",
        (TransitionKind.GOAL_STALLED, TransitionKind.STAGE_STALLED),
        "derived_projection",
    ),
    MutationContract(
        "run.partial.json/run.json",
        "local_sdlc.utils.write_run_document",
        "local_sdlc.runtime_events.record_manifest_transitions",
        "learning_runtime.legacy._import_manifest",
        (
            TransitionKind.RUN_STARTED,
            TransitionKind.VERIFICATION_COMPLETED,
            TransitionKind.STAGE_CLOSED,
            TransitionKind.RUN_TERMINATED,
        ),
        "compatibility_projection",
    ),
    MutationContract(
        "failure-analysis.json/run.json.failure_analyses",
        "local_sdlc.agent_runner",
        "local_sdlc.runtime_events.record_manifest_transitions",
        "learning_runtime.legacy._import_manifest",
        (TransitionKind.FAILURE_CLASSIFIED, TransitionKind.HYPOTHESIS_REJECTED),
        "legacy_evidence",
    ),
    MutationContract(
        "regression-memory.json",
        "local_sdlc.history.persist_regression_memories_for_manifest",
        "local_sdlc.runtime_events.record_regression_memory_document",
        "learning_runtime.legacy._import_regression_memory",
        (TransitionKind.REGRESSION_MEMORY_RECORDED,),
        "compatibility_projection",
    ),
)


def validate_mutation_inventory(repo_root: Path | None = None) -> list[str]:
    findings: list[str] = []
    projections: set[str] = set()
    for item in MUTATION_CONTRACTS:
        if item.projection in projections:
            findings.append(f"duplicate_projection:{item.projection}")
        projections.add(item.projection)
        if not item.writer or not item.runtime_hook or not item.legacy_adapter:
            findings.append(f"incomplete_mutation_contract:{item.projection}")
        for transition in item.transitions:
            try:
                contract_for(transition)
            except ValueError:
                findings.append(f"unregistered_transition:{item.projection}:{transition.value}")
    if repo_root is not None:
        required_modules = {
            "local_sdlc/runtime_events.py",
            "learning_runtime/legacy.py",
            "sdlc_events/contracts.py",
        }
        for relative in sorted(required_modules):
            if not (repo_root / relative).is_file():
                findings.append(f"missing_hook_module:{relative}")
    return findings
