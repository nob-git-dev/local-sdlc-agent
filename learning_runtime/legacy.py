"""Adapters from existing JSON/JSONL run evidence to the event contract."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable, Mapping

from sdlc_events import (
    EvidenceReference,
    RuntimeEventLedger,
    TransitionKind,
    TransitionRequest,
    canonical_json,
    contract_for,
    stable_identifier,
)


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative(run_dir: Path, path: Path) -> str:
    return path.resolve().relative_to(run_dir.resolve()).as_posix()


def _evidence(run_dir: Path, path: Path, line: int | None = None) -> EvidenceReference:
    suffix = path.suffix.lower()
    media_type = "application/json" if suffix in {".json", ".jsonl"} else "text/plain"
    return EvidenceReference(
        path=_relative(run_dir, path),
        sha256=_file_hash(path),
        media_type=media_type,
        line=line,
    )


def _read_json(path: Path, findings: list[dict[str, object]]) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        findings.append(
            {
                "code": "legacy_json_malformed",
                "source": path.name,
                "detail": str(exc),
            }
        )
        return None
    if not isinstance(payload, dict):
        findings.append(
            {
                "code": "legacy_json_not_object",
                "source": path.name,
            }
        )
        return None
    return payload


def _read_jsonl(
    path: Path,
    findings: list[dict[str, object]],
) -> list[tuple[int, dict[str, object]]]:
    records: list[tuple[int, dict[str, object]]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        findings.append({"code": "legacy_source_unreadable", "source": path.name, "detail": str(exc)})
        return records
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            findings.append(
                {
                    "code": "legacy_jsonl_malformed",
                    "source": path.name,
                    "line": line_number,
                    "detail": str(exc),
                }
            )
            continue
        if not isinstance(payload, dict):
            findings.append(
                {
                    "code": "legacy_jsonl_not_object",
                    "source": path.name,
                    "line": line_number,
                }
            )
            continue
        records.append((line_number, payload))
    return records


def _emit(
    ledger: RuntimeEventLedger,
    *,
    transition: TransitionKind,
    source_component: str,
    source_key: str,
    payload: Mapping[str, object],
    evidence: EvidenceReference,
    aggregate_id: str = "",
    propositions: Iterable[str] = (),
    eligibility: str = "unknown",
) -> bool:
    contract_aggregate = contract_for(transition).aggregate_type
    identity = aggregate_id or ledger.run_id
    before = ledger.transition_count()
    ledger.commit_transition(
        TransitionRequest(
            transition_kind=transition.value,
            aggregate_type=contract_aggregate,
            aggregate_id=identity,
            source_component=source_component,
            source_key=source_key,
            payload=payload,
            propositions=tuple(propositions),
            evidence_refs=(evidence,),
            knowledge_eligibility=eligibility,
            sensitivity="project",
        )
    )
    return ledger.transition_count() > before


def _scope_kind(run_dir: Path) -> str:
    policy_path = run_dir / "progress_policy.json"
    if not policy_path.is_file():
        return "goal"
    try:
        payload = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "goal"
    return str(payload.get("scope_kind") or "goal") if isinstance(payload, dict) else "goal"


def _import_progress(
    run_dir: Path,
    ledger: RuntimeEventLedger,
    findings: list[dict[str, object]],
) -> int:
    path = run_dir / "progress.jsonl"
    if not path.is_file():
        return 0
    imported = 0
    scope_kind = _scope_kind(run_dir)
    for line, payload in _read_jsonl(path, findings):
        event_name = str(payload.get("event") or "")
        sequence = payload.get("sequence") or line
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
        action_id = str(metadata.get("action_id") or payload.get("action") or f"action-{sequence}")
        if event_name == "work_start":
            transition = TransitionKind.ACTION_ADMITTED
            aggregate_id = action_id
        elif event_name == "cancel_requested":
            transition = TransitionKind.CANCELLATION_REQUESTED
            aggregate_id = ledger.run_id
        elif event_name == "stalled":
            transition = TransitionKind.STAGE_STALLED if scope_kind == "stage" else TransitionKind.GOAL_STALLED
            aggregate_id = stable_identifier("SCOPE", ledger.run_id, scope_kind)
        elif event_name == "progress_observed":
            transition = TransitionKind.STAGE_PROGRESSED if scope_kind == "stage" else TransitionKind.PROGRESS_OBSERVED
            aggregate_id = stable_identifier("SCOPE", ledger.run_id, scope_kind)
        else:
            transition = TransitionKind.EVIDENCE_OBSERVED
            aggregate_id = stable_identifier("EVIDENCE", ledger.run_id, "progress", sequence)
        imported += int(
            _emit(
                ledger,
                transition=transition,
                source_component="legacy.progress",
                source_key=f"progress.jsonl:{sequence}",
                payload={"legacy_event": payload, "source_hash": _file_hash(path)},
                evidence=_evidence(run_dir, path, line),
                aggregate_id=aggregate_id,
                eligibility="eligible" if event_name in {"stalled", "work_start"} else "unknown",
            )
        )
    return imported


def _import_safety(
    run_dir: Path,
    ledger: RuntimeEventLedger,
    findings: list[dict[str, object]],
) -> int:
    imported = 0
    decision_path = run_dir / "safety_decisions.jsonl"
    if decision_path.is_file():
        for line, payload in _read_jsonl(decision_path, findings):
            sequence = payload.get("sequence") or line
            decision = str(payload.get("decision") or "")
            if decision == "block":
                transition = TransitionKind.ACTION_BLOCKED
            elif decision == "require_approval":
                transition = TransitionKind.APPROVAL_REQUIRED
            else:
                transition = TransitionKind.SAFETY_DECIDED
            aggregate_id = str(payload.get("metadata", {}).get("action_id") or payload.get("decision_id") or sequence) if isinstance(payload.get("metadata"), Mapping) else str(payload.get("decision_id") or sequence)
            imported += int(
                _emit(
                    ledger,
                    transition=transition,
                    source_component="legacy.safety",
                    source_key=f"safety_decisions.jsonl:{sequence}",
                    payload={"legacy_decision": payload, "source_hash": _file_hash(decision_path)},
                    evidence=_evidence(run_dir, decision_path, line),
                    aggregate_id=aggregate_id,
                    eligibility="eligible" if decision in {"block", "require_approval"} else "unknown",
                )
            )

    approval_path = run_dir / "safety_approvals.jsonl"
    if approval_path.is_file():
        for line, payload in _read_jsonl(approval_path, findings):
            sequence = payload.get("sequence") or line
            transition = (
                TransitionKind.APPROVAL_CONSUMED
                if payload.get("event") == "consumed"
                else TransitionKind.APPROVAL_GRANTED
            )
            imported += int(
                _emit(
                    ledger,
                    transition=transition,
                    source_component="legacy.approval",
                    source_key=f"safety_approvals.jsonl:{sequence}",
                    payload={"legacy_approval": payload, "source_hash": _file_hash(approval_path)},
                    evidence=_evidence(run_dir, approval_path, line),
                    aggregate_id=str(payload.get("approval_id") or payload.get("decision_id") or sequence),
                    eligibility="ineligible",
                )
            )
    return imported


def _import_budget(
    run_dir: Path,
    ledger: RuntimeEventLedger,
    findings: list[dict[str, object]],
) -> int:
    path = run_dir / "budget_events.jsonl"
    if not path.is_file():
        return 0
    imported = 0
    for line, payload in _read_jsonl(path, findings):
        sequence = payload.get("sequence") or line
        outcome = str(payload.get("outcome") or "")
        if outcome == "consumed":
            transition = TransitionKind.BUDGET_CONSUMED
        elif outcome == "refunded":
            transition = TransitionKind.BUDGET_REFUNDED
        else:
            transition = TransitionKind.BUDGET_EXHAUSTED
        imported += int(
            _emit(
                ledger,
                transition=transition,
                source_component="legacy.budget",
                source_key=f"budget_events.jsonl:{sequence}",
                payload={"legacy_budget": payload, "source_hash": _file_hash(path)},
                evidence=_evidence(run_dir, path, line),
                aggregate_id=str(payload.get("action_id") or payload.get("event_id") or sequence),
                eligibility="eligible" if transition == TransitionKind.BUDGET_EXHAUSTED else "unknown",
            )
        )
    return imported


def _import_stall(
    run_dir: Path,
    ledger: RuntimeEventLedger,
    findings: list[dict[str, object]],
) -> int:
    path = run_dir / "stall.json"
    if not path.is_file():
        return 0
    payload = _read_json(path, findings)
    if payload is None:
        return 0
    scope_kind = _scope_kind(run_dir)
    transition = TransitionKind.STAGE_STALLED if scope_kind == "stage" else TransitionKind.GOAL_STALLED
    return int(
        _emit(
            ledger,
            transition=transition,
            source_component="legacy.stall",
            source_key="stall.json:stalled",
            payload={"legacy_stall": payload, "source_hash": _file_hash(path)},
            evidence=_evidence(run_dir, path),
            aggregate_id=stable_identifier("SCOPE", ledger.run_id, scope_kind),
            eligibility="eligible",
        )
    )


def _import_regression_memory(
    run_dir: Path,
    ledger: RuntimeEventLedger,
    findings: list[dict[str, object]],
) -> int:
    imported = 0
    paths = sorted(
        set(run_dir.glob("*regression-memory*.json"))
        | set(run_dir.glob("*regression_memory*.json"))
    )
    for path in paths:
        document = _read_json(path, findings)
        if document is None:
            continue
        records = document.get("records")
        if not isinstance(records, list):
            findings.append(
                {
                    "code": "legacy_regression_memory_records_missing",
                    "source": path.name,
                }
            )
            continue
        evidence = _evidence(run_dir, path)
        for index, record in enumerate(records, 1):
            if not isinstance(record, Mapping):
                findings.append(
                    {
                        "code": "legacy_regression_memory_record_invalid",
                        "source": path.name,
                        "record": index,
                    }
                )
                continue
            memory_id = str(record.get("id") or "").strip() or stable_identifier(
                "RM", canonical_json(record)
            )
            imported += int(
                _emit(
                    ledger,
                    transition=TransitionKind.REGRESSION_MEMORY_RECORDED,
                    source_component="legacy.regression_memory",
                    source_key=f"regression_memory:{memory_id}",
                    payload={
                        "regression_memory": dict(record),
                        "source_hash": _file_hash(path),
                    },
                    evidence=evidence,
                    aggregate_id=memory_id,
                    propositions=(str(record.get("failure_family") or "regression_memory"),),
                    eligibility="unknown",
                )
            )
    return imported


def _failure_analysis_documents(
    run_dir: Path,
    manifest: Mapping[str, object],
    findings: list[dict[str, object]],
) -> list[tuple[str, dict[str, object], Path]]:
    documents: list[tuple[str, dict[str, object], Path]] = []
    analyses = manifest.get("failure_analyses")
    manifest_path = run_dir / "run.json"
    if isinstance(analyses, list):
        for index, analysis in enumerate(analyses, 1):
            if isinstance(analysis, dict):
                documents.append((f"run.json:failure_analysis:{index}", analysis, manifest_path))
    paths = sorted(
        set(run_dir.glob("*failure-analysis*.json"))
        | set(run_dir.glob("*failure_analysis*.json"))
    )
    for path in paths:
        payload = _read_json(path, findings)
        if payload is None:
            continue
        documents.append((f"{path.name}:failure_analysis", payload, path))
    return documents


def _import_manifest(
    run_dir: Path,
    ledger: RuntimeEventLedger,
    findings: list[dict[str, object]],
) -> int:
    path = run_dir / "run.json"
    if not path.is_file():
        return 0
    manifest = _read_json(path, findings)
    if manifest is None:
        return 0
    imported = 0
    evidence = _evidence(run_dir, path)
    source_hash = _file_hash(path)
    status = str(
        manifest.get("final_verdict")
        or manifest.get("final_status")
        or manifest.get("status")
        or "unknown"
    )
    summary = {
        "status": status,
        "api_calls": manifest.get("api_calls"),
        "completed_rounds": manifest.get("completed_rounds"),
        "completed_stages": manifest.get("completed_stages"),
        "source_hash": source_hash,
    }
    scope_kind = _scope_kind(run_dir)
    imported += int(
        _emit(
            ledger,
            transition=TransitionKind.RUN_STARTED,
            source_component="legacy.manifest",
            source_key="lifecycle:run_started",
            payload={"legacy": True, "source_hash": source_hash},
            evidence=evidence,
            eligibility="ineligible",
        )
    )
    if scope_kind == "stage":
        imported += int(
            _emit(
                ledger,
                transition=TransitionKind.STAGE_STARTED,
                source_component="legacy.manifest",
                source_key="lifecycle:stage_started",
                payload={"legacy": True, "scope_kind": scope_kind, "source_hash": source_hash},
                evidence=evidence,
                aggregate_id=stable_identifier("SCOPE", ledger.run_id, "stage"),
                eligibility="ineligible",
            )
        )
    verification_expected = status not in {"unknown", "planned", "dry_run", "not_judged"}
    if verification_expected:
        imported += int(
            _emit(
                ledger,
                transition=TransitionKind.VERIFICATION_COMPLETED,
                source_component="legacy.manifest",
                source_key="lifecycle:verification_completed",
                payload=summary,
                evidence=evidence,
                aggregate_id=stable_identifier("VERIFY", ledger.run_id, "final"),
                eligibility="eligible",
                propositions=(f"final_status={status}",),
            )
        )
    for source_key, analysis, analysis_path in _failure_analysis_documents(
        run_dir, manifest, findings
    ):
        imported += int(
            _emit(
                ledger,
                transition=TransitionKind.FAILURE_CLASSIFIED,
                source_component="legacy.failure_analysis",
                source_key=source_key,
                payload={"analysis": analysis, "source_hash": _file_hash(analysis_path)},
                evidence=_evidence(run_dir, analysis_path),
                aggregate_id=stable_identifier("VERIFY", ledger.run_id, source_key),
                eligibility="eligible",
                propositions=(str(analysis.get("failure_type") or "failure_classified"),),
            )
        )
        rejected = analysis.get("rejected_hypotheses")
        if isinstance(rejected, list):
            for index, hypothesis in enumerate(rejected, 1):
                if not isinstance(hypothesis, Mapping):
                    continue
                imported += int(
                    _emit(
                        ledger,
                        transition=TransitionKind.HYPOTHESIS_REJECTED,
                        source_component="legacy.failure_analysis",
                        source_key=f"{source_key}:rejected:{index}",
                        payload={"hypothesis": dict(hypothesis), "source_hash": _file_hash(analysis_path)},
                        evidence=_evidence(run_dir, analysis_path),
                        aggregate_id=stable_identifier("VERIFY", ledger.run_id, source_key),
                        eligibility="eligible",
                    )
                )
    if scope_kind == "stage":
        imported += int(
            _emit(
                ledger,
                transition=TransitionKind.STAGE_CLOSED,
                source_component="legacy.manifest",
                source_key="lifecycle:stage_closed",
                payload=summary,
                evidence=evidence,
                aggregate_id=stable_identifier("SCOPE", ledger.run_id, "stage"),
                eligibility="eligible",
            )
        )
    imported += int(
        _emit(
            ledger,
            transition=TransitionKind.RUN_TERMINATED,
            source_component="legacy.manifest",
            source_key="lifecycle:run_terminated",
            payload={**summary, "verification_expected": verification_expected},
            evidence=evidence,
            eligibility="eligible",
            propositions=(f"run_terminated={status}",),
        )
    )
    return imported


def import_legacy_run(run_dir: Path) -> dict[str, object]:
    run_dir = run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    ledger = RuntimeEventLedger(run_dir)
    findings: list[dict[str, object]] = []
    imported = 0
    imported += _import_progress(run_dir, ledger, findings)
    imported += _import_safety(run_dir, ledger, findings)
    imported += _import_budget(run_dir, ledger, findings)
    imported += _import_stall(run_dir, ledger, findings)
    imported += _import_regression_memory(run_dir, ledger, findings)
    imported += _import_manifest(run_dir, ledger, findings)
    return {
        "status": "pass" if not findings else "integrity_failed",
        "run_id": ledger.run_id,
        "imported_count": imported,
        "event_count": len(ledger.list_events()),
        "findings": findings,
    }
