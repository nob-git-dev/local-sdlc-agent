"""Structured inspection, explanation, and health views for learned knowledge."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Mapping

from .candidate_store import CandidateStore
from .evaluation_store import EvaluationStore
from .knowledge_schema import KnowledgeItem
from .promotion_policy import is_high_impact
from .registry_store import RegistryStore
from .snapshots import SnapshotStore
from .storage import ExperienceStore, learning_data_dir


def _evaluation_summary(report: Mapping[str, object]) -> dict[str, object]:
    return {
        key: report.get(key)
        for key in (
            "evaluation_id",
            "report_hash",
            "verdict",
            "suite_coverage",
            "case_count",
            "passed_case_count",
            "critical_regressions",
            "unresolved_counterexamples",
            "evidence_quality",
            "independent_support",
            "precision_lower_bound",
            "reason_codes",
        )
    }


def _event_summary(event: Mapping[str, object]) -> dict[str, object]:
    payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
    return {
        "sequence": event.get("sequence"),
        "event_id": event.get("event_id"),
        "event_type": event.get("event_type"),
        "event_hash": event.get("event_hash"),
        "occurred_at": event.get("occurred_at"),
        "snapshot_id": event.get("snapshot_id"),
        "reason_code": payload.get("reason_code"),
        "evaluation_id": payload.get("evaluation_id"),
        "high_impact": payload.get("high_impact"),
        "human_approval_consumed": bool(payload.get("approval_id")),
    }


def _effective_identity(
    registry: RegistryStore,
    snapshots: SnapshotStore,
) -> tuple[str, set[tuple[str, int]]]:
    snapshot_id = registry.current_snapshot_id()
    if not snapshot_id:
        return "", set()
    snapshot = snapshots.get_snapshot(snapshot_id)
    identities = {
        (str(item["knowledge_id"]), int(item["version"]))
        for item in snapshot["active_items"]
    }
    return snapshot_id, identities


def inspect_knowledge(
    data_dir: Path | None,
    knowledge_id: str,
    version: int | None = None,
) -> dict[str, object]:
    candidates = CandidateStore(data_dir)
    evaluations = EvaluationStore(data_dir)
    registry = RegistryStore(data_dir)
    snapshots = SnapshotStore(data_dir)
    item = candidates.get_candidate(knowledge_id, version)
    reports = evaluations.reports(item.knowledge_id)
    events = [
        event
        for event in registry.events()
        if event.get("knowledge_id") == item.knowledge_id
        and int(event.get("knowledge_version") or 0) == item.version
    ]
    snapshot_id, effective = _effective_identity(registry, snapshots)
    return {
        "knowledge_id": item.knowledge_id,
        "knowledge_version": item.version,
        "kind": item.kind,
        "scope": item.scope,
        "effect": item.effect,
        "authority": item.authority,
        "created_by": item.created_by,
        "confidence": item.confidence,
        "applicability": item.applicability.to_dict(),
        "antecedents": [dict(value) for value in item.antecedents],
        "conclusion": dict(item.conclusion),
        "supporting_project_count": len(item.supporting_projects),
        "evidence_count": len(item.evidence_refs),
        "counterexample_count": len(item.counterexamples),
        "candidate_state": item.state,
        "lifecycle_state": registry.state_for(item.knowledge_id, item.version),
        "effective": (item.knowledge_id, item.version) in effective,
        "effective_snapshot_id": snapshot_id,
        "evaluations": [_evaluation_summary(report) for report in reports],
        "registry_events": [_event_summary(event) for event in events],
    }


def explain_knowledge(
    data_dir: Path | None,
    knowledge_id: str,
    version: int | None = None,
) -> dict[str, object]:
    inspected = inspect_knowledge(data_dir, knowledge_id, version)
    candidate = CandidateStore(data_dir).get_candidate(knowledge_id, version)
    latest = inspected["evaluations"][-1] if inspected["evaluations"] else {}
    promotion_events = [
        event
        for event in inspected["registry_events"]
        if event["event_type"] == "knowledge_promoted"
    ]
    required = is_high_impact(candidate)
    consumed = any(event["human_approval_consumed"] for event in promotion_events)
    return {
        "knowledge_id": inspected["knowledge_id"],
        "knowledge_version": inspected["knowledge_version"],
        "observed_facts": {
            "supporting_project_count": inspected["supporting_project_count"],
            "evidence_count": inspected["evidence_count"],
            "counterexample_count": inspected["counterexample_count"],
            "scope": inspected["scope"],
            "effect": inspected["effect"],
        },
        "hypothesis": {
            "authority": inspected["authority"],
            "created_by": inspected["created_by"],
            "conclusion": inspected["conclusion"],
            "confidence": inspected["confidence"],
        },
        "mechanical_validation": latest,
        "human_approval": {
            "required": required,
            "consumed": consumed,
            "satisfied": consumed if required else True,
        },
        "lifecycle": {
            "candidate_state": inspected["candidate_state"],
            "current_state": inspected["lifecycle_state"],
        },
        "effective_snapshot": {
            "effective": inspected["effective"],
            "snapshot_id": inspected["effective_snapshot_id"],
        },
    }


def snapshot_view(data_dir: Path | None) -> dict[str, object]:
    registry = RegistryStore(data_dir)
    snapshots = SnapshotStore(data_dir)
    return {
        "current_snapshot_id": registry.current_snapshot_id(),
        "snapshots": [
            {
                "snapshot_id": item["snapshot_id"],
                "snapshot_hash": item["snapshot_hash"],
                "created_at": item["created_at"],
                "active_count": len(item["active_items"]),
                "active_knowledge": [
                    {
                        "knowledge_id": active["knowledge_id"],
                        "knowledge_version": active["version"],
                        "scope": active["scope"],
                        "effect": active["effect"],
                    }
                    for active in item["active_items"]
                ],
            }
            for item in snapshots.snapshots()
        ],
    }


def doctor_report(data_dir: Path | None = None) -> dict[str, object]:
    root = learning_data_dir(data_dir)
    findings: list[str] = []
    try:
        experience = ExperienceStore(root)
        candidates = CandidateStore(root)
        evaluations = EvaluationStore(root)
        registry = RegistryStore(root)
        snapshots = SnapshotStore(root)
        reports = evaluations.reports()
        registry_findings = registry.integrity_findings()
        findings.extend(registry_findings)
        snapshot_records = snapshots.snapshots()
        snapshot_ids = {str(item["snapshot_id"]) for item in snapshot_records}
        current_snapshot = registry.current_snapshot_id()
        if current_snapshot and current_snapshot not in snapshot_ids:
            findings.append("current_snapshot_missing")
        candidate_records = candidates.candidates()
        lifecycle_counts = Counter(
            registry.state_for(str(item["knowledge_id"]), int(item["version"]))
            for item in candidate_records
        )
        verdict_counts = Counter(str(report["verdict"]) for report in reports)
        active_count = len(registry.active_items())
        storage_bytes = sum(
            path.stat().st_size for path in root.rglob("*") if path.is_file()
        )
        return {
            "status": "pass" if not findings else "fail",
            "event_count": experience.event_count(),
            "episode_count": experience.episode_count(),
            "candidate_count": len(candidate_records),
            "evaluation_count": len(reports),
            "snapshot_count": len(snapshot_records),
            "active_count": active_count,
            "current_snapshot_id": current_snapshot,
            "lifecycle_counts": dict(sorted(lifecycle_counts.items())),
            "validation_verdict_counts": dict(sorted(verdict_counts.items())),
            "registry_event_count": len(registry.events()),
            "registry_integrity": "pass" if not registry_findings else "fail",
            "storage_bytes": storage_bytes,
            "findings": findings,
        }
    except Exception as exc:
        return {
            "status": "fail",
            "storage_bytes": 0,
            "findings": [f"learning_runtime_unavailable:{type(exc).__name__}"],
        }


__all__ = [
    "doctor_report",
    "explain_knowledge",
    "inspect_knowledge",
    "snapshot_view",
]
