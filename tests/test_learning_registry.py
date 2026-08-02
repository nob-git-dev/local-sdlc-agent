import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from learning_runtime.candidate_store import CandidateStore
from learning_runtime.evaluation_store import EvaluationStore
from learning_runtime.knowledge_schema import KnowledgeItem
from learning_runtime.promotion import PromotionService
from learning_runtime.registry_store import RegistryStore
from learning_runtime.snapshots import SnapshotStore
from learning_runtime.storage import ExperienceStore
from learning_runtime.validation import validate_and_store
from local_sdlc.models import RunnerError
from local_sdlc.safety import read_safety_approvals
from tests.learning_candidate_fixtures import eligible_episode
from tests.learning_knowledge_fixtures import renamed_domain_map
from tests.test_learning_validation import (
    structural_candidate,
    unrelated_map,
    validation_case,
)


def passing_cases():
    return (
        validation_case(
            "VC-replay",
            "replay",
            renamed_domain_map("source", project_fingerprint="project-a"),
            True,
            episode_id="EP-source-a",
        ),
        validation_case("VC-negative", "negative", unrelated_map(), False),
        validation_case(
            "VC-holdout",
            "holdout",
            renamed_domain_map("holdout", project_fingerprint="project-c"),
            True,
        ),
    )


def validated_candidate(
    root: Path,
    *,
    knowledge_id: str = "K-registry-low",
    high_impact: bool = False,
) -> KnowledgeItem:
    experience = ExperienceStore(root)
    for episode_id, project in (
        ("EP-source-a", "project-a"),
        ("EP-source-b", "project-b"),
    ):
        experience.put_episode(eligible_episode(episode_id, project_fingerprint=project))
    payload = structural_candidate().to_dict()
    payload["knowledge_id"] = knowledge_id
    if high_impact:
        payload["kind"] = "normative"
        payload["effect"] = "require"
        payload["conclusion"] = {"requirement": "preserve_verified_boundary"}
    item = KnowledgeItem.from_dict(payload)
    CandidateStore(root).put_candidate(
        item,
        ("EP-source-a", "EP-source-b"),
        ("1" * 64, "2" * 64, "3" * 64),
    )
    report = validate_and_store(item, experience, EvaluationStore(root), passing_cases())
    if report["verdict"] != "shadow_pass":
        raise AssertionError(report)
    return item


class LearningRegistryTests(unittest.TestCase):
    def test_low_impact_promotion_publishes_an_immutable_active_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            item = validated_candidate(root)
            service = PromotionService(root)

            result = service.promote(item.knowledge_id)
            repeated = service.promote(item.knowledge_id)
            registry = RegistryStore(root)
            snapshot = SnapshotStore(root).get_snapshot(result["snapshot_id"])
            stored_candidate = CandidateStore(root).get_candidate(item.knowledge_id)
            integrity_findings = registry.integrity_findings()

            self.assertEqual(result["status"], "promoted")
            self.assertEqual(repeated["status"], "already_active")
            self.assertEqual(snapshot["active_items"][0]["state"], "active")
            self.assertEqual(stored_candidate.state, "candidate")
            self.assertEqual(integrity_findings, [])

    def test_high_impact_promotion_waits_for_one_time_human_approval(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            item = validated_candidate(root, knowledge_id="K-registry-high", high_impact=True)
            service = PromotionService(root)

            pending = service.promote(item.knowledge_id)
            self.assertEqual(pending["status"], "approval_required")
            self.assertEqual(RegistryStore(root).current_snapshot_id(), "")
            with self.assertRaises(RunnerError):
                service.approve(
                    pending["operation_id"],
                    pending["decision_id"],
                    source="llm",
                )
            approval = service.approve(
                pending["operation_id"],
                pending["decision_id"],
                source="test",
            )
            promoted = service.promote(item.knowledge_id)
            approval_events = read_safety_approvals(
                service.operations_dir / pending["operation_id"]
            )

        self.assertEqual(promoted["status"], "promoted")
        self.assertEqual(promoted["approval_id"], approval["approval_id"])
        self.assertEqual(
            [event["event"] for event in approval_events],
            ["approved", "consumed"],
        )

    def test_challenge_and_rollback_change_pointer_without_deleting_history(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            item = validated_candidate(root)
            service = PromotionService(root)
            baseline = service.publish_current(reason_code="initial_empty")
            promoted = service.promote(item.knowledge_id)
            challenged = service.challenge(
                item.knowledge_id,
                reason_code="holdout_regression",
            )
            rolled_back = service.rollback(baseline["snapshot_id"])
            registry = RegistryStore(root)
            event_types = [event["event_type"] for event in registry.events()]
            integrity_findings = registry.integrity_findings()

            self.assertNotEqual(promoted["snapshot_id"], baseline["snapshot_id"])
            self.assertEqual(challenged["snapshot_id"], baseline["snapshot_id"])
            self.assertEqual(rolled_back["snapshot_id"], baseline["snapshot_id"])
            self.assertIn("knowledge_challenged", event_types)
            self.assertEqual(event_types[-1], "snapshot_rolled_back")
            self.assertEqual(integrity_findings, [])

    def test_registry_hash_chain_detects_payload_tampering(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            item = validated_candidate(root)
            PromotionService(root).promote(item.knowledge_id)
            registry = RegistryStore(root)
            with sqlite3.connect(registry.path) as connection:
                row = connection.execute(
                    "SELECT record_json FROM registry_events WHERE sequence = 1"
                ).fetchone()
                record = json.loads(str(row[0]))
                record["payload"] = {"tampered": True}
                connection.execute(
                    "UPDATE registry_events SET payload_json = ?, record_json = ? "
                    "WHERE sequence = 1",
                    ('{"tampered":true}', json.dumps(record, sort_keys=True)),
                )
            findings = registry.integrity_findings()

        self.assertIn("event_hash_mismatch:1", findings)

    def test_registry_rejects_conflicting_idempotent_event(self):
        with tempfile.TemporaryDirectory() as temp:
            registry = RegistryStore(Path(temp))
            registry.append_event(
                "snapshot_published",
                snapshot_id="KS-one",
                payload={"reason_code": "first"},
                idempotency_key="same-operation",
            )
            with self.assertRaises(ValueError):
                registry.append_event(
                    "snapshot_published",
                    snapshot_id="KS-one",
                    payload={"reason_code": "different"},
                    idempotency_key="same-operation",
                )

    def test_interrupted_publication_is_completed_on_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            item = validated_candidate(root)
            service = PromotionService(root)
            report = service.evaluations.latest_pass(item.knowledge_id, item.version)
            active = item.to_dict()
            active["state"] = "active"
            service.registry.append_event(
                "knowledge_promoted",
                knowledge_id=item.knowledge_id,
                version=item.version,
                payload={
                    "knowledge": active,
                    "evaluation_id": report["evaluation_id"],
                    "evaluation_hash": report["report_hash"],
                    "high_impact": False,
                    "approval_id": "",
                },
                idempotency_key=report["evaluation_id"],
            )

            result = service.promote(item.knowledge_id)
            snapshot = SnapshotStore(root).get_snapshot(result["snapshot_id"])

        self.assertEqual(result["status"], "already_active")
        self.assertEqual(snapshot["active_items"][0]["knowledge_id"], item.knowledge_id)

    def test_unvalidated_candidate_cannot_enter_the_registry(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            experience = ExperienceStore(root)
            item = structural_candidate()
            CandidateStore(root).put_candidate(
                item,
                ("EP-source-a", "EP-source-b"),
                ("1" * 64, "2" * 64, "3" * 64),
            )

            with self.assertRaises(ValueError):
                PromotionService(root).promote(item.knowledge_id)
            episode_count = experience.episode_count()

        self.assertEqual(episode_count, 0)

    def test_retirement_and_supersession_are_rebuilt_from_events(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = validated_candidate(root, knowledge_id="K-registry-first")
            second = validated_candidate(root, knowledge_id="K-registry-second")
            service = PromotionService(root)
            service.promote(first.knowledge_id)
            service.promote(second.knowledge_id)

            result = service.supersede(
                first.knowledge_id,
                by_knowledge_id=second.knowledge_id,
                reason_code="newer_verified_rule",
            )
            registry = RegistryStore(root)
            snapshot = SnapshotStore(root).get_snapshot(result["snapshot_id"])
            first_state = registry.state_for(first.knowledge_id, first.version)
            second_state = registry.state_for(second.knowledge_id, second.version)

        self.assertEqual(first_state, "retired")
        self.assertEqual(second_state, "active")
        self.assertEqual(
            [item["knowledge_id"] for item in snapshot["active_items"]],
            [second.knowledge_id],
        )

    def test_tampered_evaluation_or_snapshot_is_rejected_before_use(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            item = validated_candidate(root)
            evaluations = EvaluationStore(root)
            report = evaluations.latest_pass(item.knowledge_id, item.version)
            with sqlite3.connect(evaluations.path) as connection:
                altered = dict(report)
                altered["reason_codes"] = ["tampered"]
                connection.execute(
                    "UPDATE validation_reports SET report_json = ? "
                    "WHERE evaluation_id = ?",
                    (json.dumps(altered, sort_keys=True), report["evaluation_id"]),
                )
            with self.assertRaises(ValueError):
                PromotionService(root).promote(item.knowledge_id)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            item = validated_candidate(root)
            promoted = PromotionService(root).promote(item.knowledge_id)
            snapshots = SnapshotStore(root)
            snapshots.snapshot_path(promoted["snapshot_id"]).write_text(
                '{"tampered":true}\n',
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                snapshots.get_snapshot(promoted["snapshot_id"])


if __name__ == "__main__":
    unittest.main()
