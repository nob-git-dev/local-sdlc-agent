"""Mechanical promotion gates and registry lifecycle operations."""

from __future__ import annotations

import hashlib
from pathlib import Path

from sdlc_events import canonical_json, stable_identifier

from local_sdlc.safety import (
    action_safety_decision,
    authorize_safety_decision,
    request_safety_approval,
)

from .candidate_store import CandidateStore
from .evaluation_store import EvaluationStore
from .promotion_policy import active_item, is_high_impact
from .registry_store import RegistryStore
from .schema_validation import require_identifier, require_slug
from .snapshots import SnapshotStore
from .storage import learning_data_dir


class PromotionService:
    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = learning_data_dir(data_dir)
        self.candidates = CandidateStore(self.data_dir)
        self.evaluations = EvaluationStore(self.data_dir)
        self.registry = RegistryStore(self.data_dir)
        self.snapshots = SnapshotStore(self.data_dir)
        self.operations_dir = self.data_dir / "promotion-operations"
        self.operations_dir.mkdir(parents=True, exist_ok=True)

    def _operation_dir(self, operation_id: str) -> Path:
        return self.operations_dir / require_identifier(operation_id, "operation_id")

    def approve(
        self,
        operation_id: str,
        decision_id: str,
        *,
        source: str = "cli",
        note: str = "",
    ) -> dict[str, object]:
        operation_dir = self._operation_dir(operation_id)
        if not operation_dir.is_dir():
            raise ValueError(f"promotion operation not found: {operation_id}")
        return request_safety_approval(
            operation_dir,
            decision_id,
            source=source,
            note=note,
        )

    def publish_current(
        self,
        *,
        reason_code: str,
        cause_event_id: str = "",
    ) -> dict[str, object]:
        reason = require_slug(reason_code, "reason_code")
        snapshot = self.snapshots.put_snapshot(self.registry.active_items())
        event = self.registry.append_event(
            "snapshot_published",
            snapshot_id=str(snapshot["snapshot_id"]),
            payload={
                "snapshot_hash": snapshot["snapshot_hash"],
                "active_count": len(snapshot["active_items"]),
                "reason_code": reason,
                "cause_event_id": cause_event_id,
            },
            idempotency_key=cause_event_id or f"{reason}:{snapshot['snapshot_id']}",
        )
        return {**snapshot, "registry_event_id": event["event_id"]}

    def _validated_report(self, item: KnowledgeItem) -> dict[str, object]:
        report = self.evaluations.latest_pass(item.knowledge_id, item.version)
        if report is None:
            raise ValueError("candidate has no shadow_pass validation report")
        calculated = hashlib.sha256(
            canonical_json(item.to_dict()).encode("utf-8")
        ).hexdigest()
        if calculated != report.get("candidate_hash"):
            raise ValueError("validation report does not match candidate content")
        if int(report.get("critical_regressions") or 0) != 0:
            raise ValueError("candidate has critical validation regressions")
        return report

    def _current_snapshot_matches_registry(self) -> bool:
        snapshot_id = self.registry.current_snapshot_id()
        if not snapshot_id:
            return False
        snapshot = self.snapshots.get_snapshot(snapshot_id)
        expected = {
            (item.knowledge_id, item.version)
            for item in self.registry.active_items()
        }
        actual = {
            (str(item["knowledge_id"]), int(item["version"]))
            for item in snapshot["active_items"]
        }
        return actual == expected

    def promote(
        self,
        knowledge_id: str,
        version: int | None = None,
    ) -> dict[str, object]:
        item = self.candidates.get_candidate(knowledge_id, version)
        state = self.registry.state_for(item.knowledge_id, item.version)
        if state == "active":
            if not self._current_snapshot_matches_registry():
                snapshot = self.publish_current(
                    reason_code="promotion_recovery",
                    cause_event_id=f"recover:{item.knowledge_id}:{item.version}",
                )
            else:
                snapshot = {"snapshot_id": self.registry.current_snapshot_id()}
            return {
                "status": "already_active",
                "knowledge_id": item.knowledge_id,
                "knowledge_version": item.version,
                "snapshot_id": snapshot["snapshot_id"],
            }
        if state in {"challenged", "retired"}:
            raise ValueError(f"candidate lifecycle does not permit promotion: {state}")
        report = self._validated_report(item)
        evaluation_id = str(report["evaluation_id"])
        self.registry.append_event(
            "shadow_validated",
            knowledge_id=item.knowledge_id,
            version=item.version,
            payload={
                "evaluation_id": evaluation_id,
                "evaluation_hash": report["report_hash"],
            },
            idempotency_key=evaluation_id,
        )
        approval_id = ""
        operation_id = ""
        if is_high_impact(item):
            operation_id = stable_identifier(
                "PO",
                item.knowledge_id,
                item.version,
                evaluation_id,
                report["report_hash"],
            )
            operation_dir = self._operation_dir(operation_id)
            decision = action_safety_decision(
                "promote_knowledge",
                action_type="knowledge_promotion",
                risk_class="knowledge_activation",
                metadata={
                    "action_id": operation_id,
                    "approval_scope": canonical_json(
                        {
                            "knowledge_id": item.knowledge_id,
                            "version": item.version,
                            "evaluation_id": evaluation_id,
                            "evaluation_hash": report["report_hash"],
                        }
                    ),
                },
            )
            authorization = authorize_safety_decision(operation_dir, decision)
            if authorization["decision"] != "allow":
                return {
                    "status": "approval_required",
                    "knowledge_id": item.knowledge_id,
                    "knowledge_version": item.version,
                    "operation_id": operation_id,
                    "decision_id": authorization["decision_id"],
                }
            approval_id = str(authorization.get("approval_id") or "")
        active = active_item(item)
        event = self.registry.append_event(
            "knowledge_promoted",
            knowledge_id=item.knowledge_id,
            version=item.version,
            payload={
                "knowledge": active.to_dict(),
                "evaluation_id": evaluation_id,
                "evaluation_hash": report["report_hash"],
                "high_impact": is_high_impact(item),
                "approval_id": approval_id,
            },
            idempotency_key=evaluation_id,
        )
        snapshot = self.publish_current(
            reason_code="promotion",
            cause_event_id=str(event["event_id"]),
        )
        return {
            "status": "promoted",
            "knowledge_id": item.knowledge_id,
            "knowledge_version": item.version,
            "registry_event_id": event["event_id"],
            "snapshot_id": snapshot["snapshot_id"],
            "operation_id": operation_id,
            "approval_id": approval_id,
        }

    def challenge(self, knowledge_id: str, *, reason_code: str) -> dict[str, object]:
        item = self.candidates.get_candidate(knowledge_id)
        if self.registry.state_for(item.knowledge_id, item.version) != "active":
            raise ValueError("only active knowledge can be challenged")
        event = self.registry.append_event(
            "knowledge_challenged",
            knowledge_id=item.knowledge_id,
            version=item.version,
            payload={"reason_code": require_slug(reason_code, "reason_code")},
            idempotency_key=reason_code,
        )
        snapshot = self.publish_current(
            reason_code="challenge",
            cause_event_id=str(event["event_id"]),
        )
        return {
            "status": "challenged",
            "registry_event_id": event["event_id"],
            "snapshot_id": snapshot["snapshot_id"],
        }

    def retire(self, knowledge_id: str, *, reason_code: str) -> dict[str, object]:
        item = self.candidates.get_candidate(knowledge_id)
        state = self.registry.state_for(item.knowledge_id, item.version)
        if state not in {"active", "challenged"}:
            raise ValueError("only active or challenged knowledge can be retired")
        event = self.registry.append_event(
            "knowledge_retired",
            knowledge_id=item.knowledge_id,
            version=item.version,
            payload={"reason_code": require_slug(reason_code, "reason_code")},
            idempotency_key=reason_code,
        )
        snapshot = self.publish_current(
            reason_code="retirement",
            cause_event_id=str(event["event_id"]),
        )
        return {
            "status": "retired",
            "registry_event_id": event["event_id"],
            "snapshot_id": snapshot["snapshot_id"],
        }

    def supersede(
        self,
        knowledge_id: str,
        *,
        by_knowledge_id: str,
        reason_code: str,
    ) -> dict[str, object]:
        old = self.candidates.get_candidate(knowledge_id)
        replacement = self.candidates.get_candidate(by_knowledge_id)
        if self.registry.state_for(replacement.knowledge_id, replacement.version) != "active":
            raise ValueError("replacement knowledge must already be active")
        event = self.registry.append_event(
            "knowledge_superseded",
            knowledge_id=old.knowledge_id,
            version=old.version,
            payload={
                "by_knowledge_id": replacement.knowledge_id,
                "by_version": replacement.version,
                "reason_code": require_slug(reason_code, "reason_code"),
            },
            idempotency_key=f"{replacement.knowledge_id}:{replacement.version}",
        )
        snapshot = self.publish_current(
            reason_code="supersession",
            cause_event_id=str(event["event_id"]),
        )
        return {
            "status": "superseded",
            "registry_event_id": event["event_id"],
            "snapshot_id": snapshot["snapshot_id"],
        }

    def rollback(self, snapshot_id: str) -> dict[str, object]:
        snapshot = self.snapshots.get_snapshot(snapshot_id)
        current = self.registry.current_snapshot_id()
        event = self.registry.append_event(
            "snapshot_rolled_back",
            snapshot_id=str(snapshot["snapshot_id"]),
            payload={
                "from_snapshot_id": current,
                "snapshot_hash": snapshot["snapshot_hash"],
            },
            idempotency_key=f"{current}:{snapshot['snapshot_id']}",
        )
        return {
            "status": "rolled_back",
            "registry_event_id": event["event_id"],
            "snapshot_id": snapshot["snapshot_id"],
        }


__all__ = ["PromotionService", "is_high_impact"]
