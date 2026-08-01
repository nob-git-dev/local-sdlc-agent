"""Execution-plane adapter to the shared durable event contract."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Mapping, Sequence

from sdlc_events import (
    EvidenceReference,
    EventEnvelope,
    EventType,
    RuntimeEventLedger,
    TransitionKind,
    TransitionRequest,
    contract_for,
    stable_identifier,
)

from .models import RunnerError


def _scope_kind(run_dir: Path) -> str:
    path = run_dir / "progress_policy.json"
    if not path.is_file():
        return "goal"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "goal"
    return str(payload.get("scope_kind") or "goal") if isinstance(payload, dict) else "goal"


def record_runtime_transition(
    run_dir: Path,
    transition: TransitionKind,
    *,
    source_component: str,
    source_key: str,
    payload: Mapping[str, object],
    aggregate_id: str = "",
    propositions: Sequence[str] = (),
    evidence_refs: Sequence[EvidenceReference] = (),
    eligibility: str = "unknown",
    correlation_id: str = "",
    causation_id: str | None = None,
) -> EventEnvelope:
    try:
        ledger = RuntimeEventLedger(run_dir)
        aggregate_type = contract_for(transition).aggregate_type
        identity = aggregate_id or (
            stable_identifier("SCOPE", ledger.run_id, aggregate_type)
            if aggregate_type != "goal"
            else ledger.run_id
        )
        return ledger.commit_transition(
            TransitionRequest(
                transition_kind=transition.value,
                aggregate_type=aggregate_type,
                aggregate_id=identity,
                source_component=source_component,
                source_key=source_key,
                payload=payload,
                propositions=tuple(propositions),
                evidence_refs=tuple(evidence_refs),
                correlation_id=correlation_id,
                causation_id=causation_id,
                knowledge_eligibility=eligibility,
                sensitivity="project",
            )
        )
    except Exception as exc:
        if isinstance(exc, RunnerError):
            raise
        raise RunnerError(f"runtime event commit failed before {transition.value}: {exc}") from exc


def record_progress_payload(run_dir: Path, payload: Mapping[str, object]) -> EventEnvelope:
    name = str(payload.get("event") or "")
    sequence = payload.get("sequence") or "unknown"
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
    action_id = str(metadata.get("action_id") or payload.get("action") or f"action-{sequence}")
    scope_kind = _scope_kind(run_dir)
    if name == "work_start":
        transition = TransitionKind.ACTION_ADMITTED
        aggregate_id = action_id
        eligibility = "eligible"
    elif name == "cancel_requested":
        transition = TransitionKind.CANCELLATION_REQUESTED
        aggregate_id = ""
        eligibility = "eligible"
    elif name == "stalled":
        transition = TransitionKind.STAGE_STALLED if scope_kind == "stage" else TransitionKind.GOAL_STALLED
        aggregate_id = stable_identifier("SCOPE", RuntimeEventLedger(run_dir).run_id, scope_kind)
        eligibility = "eligible"
    elif name == "progress_observed":
        transition = TransitionKind.STAGE_PROGRESSED if scope_kind == "stage" else TransitionKind.PROGRESS_OBSERVED
        aggregate_id = stable_identifier("SCOPE", RuntimeEventLedger(run_dir).run_id, scope_kind)
        eligibility = "unknown"
    else:
        transition = TransitionKind.EVIDENCE_OBSERVED
        aggregate_id = stable_identifier("EVIDENCE", run_dir.resolve(), "progress", sequence)
        eligibility = "unknown"
    return record_runtime_transition(
        run_dir,
        transition,
        source_component="execution.control",
        source_key=f"progress.jsonl:{sequence}",
        payload={"progress_event": dict(payload)},
        aggregate_id=aggregate_id,
        eligibility=eligibility,
    )


def record_safety_payload(run_dir: Path, payload: Mapping[str, object]) -> EventEnvelope:
    sequence = payload.get("sequence") or "unknown"
    decision = str(payload.get("decision") or "")
    if decision == "block":
        transition = TransitionKind.ACTION_BLOCKED
        eligibility = "eligible"
    elif decision == "require_approval":
        transition = TransitionKind.APPROVAL_REQUIRED
        eligibility = "eligible"
    else:
        transition = TransitionKind.SAFETY_DECIDED
        eligibility = "unknown"
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
    aggregate_id = str(metadata.get("action_id") or payload.get("decision_id") or sequence)
    return record_runtime_transition(
        run_dir,
        transition,
        source_component="execution.safety",
        source_key=f"safety_decisions.jsonl:{sequence}",
        payload={"safety_decision": dict(payload)},
        aggregate_id=aggregate_id,
        eligibility=eligibility,
    )


def record_approval_payload(run_dir: Path, payload: Mapping[str, object]) -> EventEnvelope:
    sequence = payload.get("sequence") or "unknown"
    transition = (
        TransitionKind.APPROVAL_CONSUMED
        if payload.get("event") == "consumed"
        else TransitionKind.APPROVAL_GRANTED
    )
    return record_runtime_transition(
        run_dir,
        transition,
        source_component="execution.approval",
        source_key=f"safety_approvals.jsonl:{sequence}",
        payload={"approval_event": dict(payload)},
        aggregate_id=str(payload.get("approval_id") or payload.get("decision_id") or sequence),
        eligibility="ineligible",
    )


def record_budget_payload(run_dir: Path, payload: Mapping[str, object]) -> EventEnvelope:
    sequence = payload.get("sequence") or "unknown"
    outcome = str(payload.get("outcome") or "")
    if outcome == "consumed":
        transition = TransitionKind.BUDGET_CONSUMED
        eligibility = "unknown"
    elif outcome == "refunded":
        transition = TransitionKind.BUDGET_REFUNDED
        eligibility = "unknown"
    else:
        transition = TransitionKind.BUDGET_EXHAUSTED
        eligibility = "eligible"
    return record_runtime_transition(
        run_dir,
        transition,
        source_component="execution.budget",
        source_key=f"budget_events.jsonl:{sequence}",
        payload={"budget_event": dict(payload)},
        aggregate_id=str(payload.get("action_id") or payload.get("event_id") or sequence),
        eligibility=eligibility,
    )


def record_scope_started(run_dir: Path, scope_kind: str) -> EventEnvelope:
    ledger = RuntimeEventLedger(run_dir)
    run_event = record_runtime_transition(
        run_dir,
        TransitionKind.RUN_STARTED,
        source_component="execution.progress_monitor",
        source_key="lifecycle:run_started",
        payload={"scope_kind": scope_kind},
        aggregate_id=ledger.run_id,
        eligibility="ineligible",
    )
    if scope_kind != "stage":
        return run_event
    return record_runtime_transition(
        run_dir,
        TransitionKind.STAGE_STARTED,
        source_component="execution.progress_monitor",
        source_key="lifecycle:stage_started",
        payload={"scope_kind": scope_kind},
        aggregate_id=stable_identifier("SCOPE", ledger.run_id, "stage"),
        eligibility="ineligible",
    )


def _record_failure_analysis(
    run_dir: Path,
    *,
    source_key: str,
    analysis: Mapping[str, object],
    evidence: EvidenceReference,
) -> None:
    aggregate_id = stable_identifier("VERIFY", RuntimeEventLedger(run_dir).run_id, source_key)
    record_runtime_transition(
        run_dir,
        TransitionKind.FAILURE_CLASSIFIED,
        source_component="execution.failure_analysis",
        source_key=source_key,
        payload={"analysis": dict(analysis)},
        aggregate_id=aggregate_id,
        propositions=(str(analysis.get("failure_type") or "failure_classified"),),
        evidence_refs=(evidence,),
        eligibility="eligible",
    )
    rejected = analysis.get("rejected_hypotheses")
    if not isinstance(rejected, list):
        return
    for index, hypothesis in enumerate(rejected, 1):
        if not isinstance(hypothesis, Mapping):
            continue
        record_runtime_transition(
            run_dir,
            TransitionKind.HYPOTHESIS_REJECTED,
            source_component="execution.failure_analysis",
            source_key=f"{source_key}:rejected:{index}",
            payload={"hypothesis": dict(hypothesis)},
            aggregate_id=aggregate_id,
            evidence_refs=(evidence,),
            eligibility="eligible",
        )


def record_regression_memory_document(run_dir: Path, filename: str, content: str) -> None:
    try:
        document = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RunnerError(f"regression memory document is not valid JSON: {exc}") from exc
    records = document.get("records") if isinstance(document, Mapping) else None
    if not isinstance(records, list):
        raise RunnerError("regression memory document has no records list")
    rendered = content.rstrip() + "\n"
    evidence = EvidenceReference(
        path=filename,
        sha256=hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
        media_type="application/json",
    )
    ledger = RuntimeEventLedger(run_dir)
    for index, record in enumerate(records, 1):
        if not isinstance(record, Mapping):
            raise RunnerError(f"regression memory record {index} is not an object")
        memory_id = str(record.get("id") or "").strip() or stable_identifier(
            "RM", ledger.run_id, index, record
        )
        record_runtime_transition(
            run_dir,
            TransitionKind.REGRESSION_MEMORY_RECORDED,
            source_component="execution.regression_memory",
            source_key=f"regression_memory:{memory_id}",
            payload={"regression_memory": dict(record)},
            aggregate_id=memory_id,
            propositions=(str(record.get("failure_family") or "regression_memory"),),
            evidence_refs=(evidence,),
            eligibility="unknown",
        )


def record_manifest_transitions(run_dir: Path, filename: str, content: str) -> None:
    is_manifest = filename in {"run.partial.json", "run.json"}
    is_failure_analysis = filename.endswith(".json") and (
        "failure-analysis" in filename or "failure_analysis" in filename
    )
    if not is_manifest and not is_failure_analysis:
        return
    try:
        manifest = json.loads(content)
    except json.JSONDecodeError:
        return
    if not isinstance(manifest, dict):
        return
    rendered = content.rstrip() + "\n"
    evidence = EvidenceReference(
        path=filename,
        sha256=hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
        media_type="application/json",
    )
    if is_failure_analysis:
        _record_failure_analysis(
            run_dir,
            source_key=f"{filename}:failure_analysis",
            analysis=manifest,
            evidence=evidence,
        )
        return
    if filename == "run.json":
        analyses = manifest.get("failure_analyses")
        if isinstance(analyses, list):
            for index, analysis in enumerate(analyses, 1):
                if isinstance(analysis, Mapping):
                    _record_failure_analysis(
                        run_dir,
                        source_key=f"run.json:failure_analysis:{index}",
                        analysis=analysis,
                        evidence=evidence,
                    )
    scope_kind = _scope_kind(run_dir)
    record_scope_started(run_dir, scope_kind)
    if filename != "run.json":
        return
    status = str(
        manifest.get("final_verdict")
        or manifest.get("final_status")
        or manifest.get("status")
        or "unknown"
    )
    verification_expected = status not in {"unknown", "planned", "dry_run", "not_judged"}
    summary = {
        "status": status,
        "api_calls": manifest.get("api_calls"),
        "completed_rounds": manifest.get("completed_rounds"),
        "completed_stages": manifest.get("completed_stages"),
    }
    if verification_expected:
        record_runtime_transition(
            run_dir,
            TransitionKind.VERIFICATION_COMPLETED,
            source_component="execution.manifest",
            source_key="lifecycle:verification_completed",
            payload=summary,
            aggregate_id=stable_identifier("VERIFY", RuntimeEventLedger(run_dir).run_id, "final"),
            propositions=(f"final_status={status}",),
            evidence_refs=(evidence,),
            eligibility="eligible",
        )
    if scope_kind == "stage":
        record_runtime_transition(
            run_dir,
            TransitionKind.STAGE_CLOSED,
            source_component="execution.manifest",
            source_key="lifecycle:stage_closed",
            payload=summary,
            aggregate_id=stable_identifier("SCOPE", RuntimeEventLedger(run_dir).run_id, "stage"),
            evidence_refs=(evidence,),
            eligibility="eligible",
        )
    record_runtime_transition(
        run_dir,
        TransitionKind.RUN_TERMINATED,
        source_component="execution.manifest",
        source_key="lifecycle:run_terminated",
        payload={**summary, "verification_expected": verification_expected},
        propositions=(f"run_terminated={status}",),
        evidence_refs=(evidence,),
        eligibility="eligible",
    )


def cancellation_state_from_ledger(run_dir: Path) -> dict[str, object]:
    path = run_dir / "runtime-events.sqlite3"
    if not path.is_file():
        return {}
    try:
        events = RuntimeEventLedger(run_dir).list_events()
    except Exception:
        return {}
    for event in reversed(events):
        if event.event_type == EventType.CANCELLATION_REQUESTED.value:
            progress = event.payload.get("progress_event")
            progress = progress if isinstance(progress, Mapping) else {}
            metadata = progress.get("metadata") if isinstance(progress.get("metadata"), Mapping) else {}
            return {
                "status": "cancelled",
                "source": metadata.get("source", "event_ledger"),
                "reason": metadata.get("reason", "user_cancelled"),
                "requested_at": event.occurred_at,
                "event_id": event.event_id,
                "progress_sequence": progress.get("sequence", -1),
            }
    return {}
