import sqlite3
import tempfile
import unittest
from pathlib import Path

from learning_runtime.applicability import evaluate_applicability
from learning_runtime.candidate_store import CandidateStore
from learning_runtime.evaluation_store import EvaluationStore
from learning_runtime.knowledge_schema import KnowledgeItem
from learning_runtime.operations import (
    doctor_report,
    explain_knowledge,
    inspect_knowledge,
)
from learning_runtime.promotion import PromotionService
from learning_runtime.registry_store import RegistryStore
from learning_runtime.snapshots import SnapshotStore
from learning_runtime.storage import ExperienceStore
from learning_runtime.validation import validate_and_store, validate_candidate
from local_sdlc.learning_context import bind_learning_snapshot
from tests.learning_candidate_fixtures import eligible_episode
from tests.learning_knowledge_fixtures import renamed_domain_map
from tests.test_learning_registry import passing_cases, validated_candidate
from tests.test_learning_validation import (
    structural_candidate,
    unrelated_map,
    validation_case,
)


def project_candidate(project: str, episode_id: str) -> KnowledgeItem:
    payload = structural_candidate().to_dict()
    payload.update(
        {
            "knowledge_id": "K-project-scoped-lifecycle",
            "scope": "project",
            "applicability": {
                "operator": "all",
                "predicates": [
                    {"type": "project_is", "project_fingerprint": project}
                ],
            },
            "evidence_refs": [
                {
                    "sha256": "c" * 64,
                    "media_type": "application/json",
                    "role": "causal_episode",
                    "episode_id": episode_id,
                }
            ],
            "supporting_projects": [project],
            "generalization_rationale": "Evidence supports only one project boundary.",
            "regression_tests": ["project-boundary"],
        }
    )
    return KnowledgeItem.from_dict(payload)


def overbroad_candidate() -> KnowledgeItem:
    payload = structural_candidate().to_dict()
    payload["knowledge_id"] = "K-overbroad-lifecycle"
    payload["conclusion"] = {"recommendation": "apply_to_every_matching_shape"}
    return KnowledgeItem.from_dict(payload)


def put_candidate(root: Path, item: KnowledgeItem, episode_ids: tuple[str, ...]) -> None:
    CandidateStore(root).put_candidate(
        item,
        episode_ids,
        ("4" * 64, "5" * 64, "6" * 64),
    )


class LearningLifecycleTests(unittest.TestCase):
    def test_l12_complete_lifecycle_is_scoped_reversible_and_explainable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            experience = ExperienceStore(root)

            snapshots = SnapshotStore(root)
            original_snapshot_connect = snapshots._connect
            snapshot_calls = [0]

            def fail_snapshot_database_once():
                snapshot_calls[0] += 1
                if snapshot_calls[0] == 2:
                    raise sqlite3.OperationalError("injected snapshot boundary failure")
                return original_snapshot_connect()

            snapshots._connect = fail_snapshot_database_once
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    snapshots.put_snapshot(())
            finally:
                snapshots._connect = original_snapshot_connect
            orphan_snapshot_files = list(snapshots.snapshots_dir.glob("*.json"))
            recovered_empty = snapshots.put_snapshot(())

            structural = validated_candidate(
                root,
                knowledge_id="K-structural-lifecycle",
            )
            structural_report = EvaluationStore(root).latest_pass(
                structural.knowledge_id,
                structural.version,
            )
            service = PromotionService(root)
            baseline = service.publish_current(reason_code="initial_empty")
            structural_promotion = service.promote(structural.knowledge_id)
            structural_snapshot = structural_promotion["snapshot_id"]
            structural_map = renamed_domain_map(
                "runtime-a",
                project_fingerprint="project-runtime-a",
            )
            run_a = root / "run-a"
            binding_a_before = bind_learning_snapshot(
                run_a,
                data_dir=root,
                domain_map=structural_map,
            )

            scoped_map = unrelated_map("project-family-two")
            scoped_episode_id = "EP-project-family-two"
            experience.put_episode(
                eligible_episode(
                    scoped_episode_id,
                    project_fingerprint=scoped_map.project_fingerprint,
                    structural_signature=scoped_map.structural_signature,
                )
            )
            scoped = project_candidate(scoped_map.project_fingerprint, scoped_episode_id)
            put_candidate(root, scoped, (scoped_episode_id,))
            scoped_cases = (
                validation_case(
                    "VC-project-replay",
                    "replay",
                    scoped_map,
                    True,
                    episode_id=scoped_episode_id,
                ),
                validation_case(
                    "VC-project-negative",
                    "negative",
                    unrelated_map("project-family-other"),
                    False,
                ),
            )
            scoped_report = validate_and_store(
                scoped,
                experience,
                EvaluationStore(root),
                scoped_cases,
            )
            scoped_promotion = service.promote(scoped.knowledge_id)
            run_b = root / "run-b"
            binding_b_before = bind_learning_snapshot(
                run_b,
                data_dir=root,
                domain_map=scoped_map,
            )

            overbroad = overbroad_candidate()
            put_candidate(root, overbroad, ("EP-source-a", "EP-source-b"))
            broad_cases = list(passing_cases())
            broad_cases[-1] = validation_case(
                "VC-unrelated-holdout",
                "holdout",
                renamed_domain_map(
                    "holdout-negative",
                    project_fingerprint="project-holdout-negative",
                ),
                False,
            )
            broad_core = validate_candidate(overbroad, experience, broad_cases)
            evaluations = EvaluationStore(root)
            original_evaluation_connect = evaluations._connect
            evaluation_calls = [0]

            def fail_evaluation_database_once():
                evaluation_calls[0] += 1
                if evaluation_calls[0] == 2:
                    raise sqlite3.OperationalError("injected evaluation boundary failure")
                return original_evaluation_connect()

            files_before = set(evaluations.evaluations_dir.glob("*.json"))
            evaluations._connect = fail_evaluation_database_once
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    evaluations.put_report(broad_core)
            finally:
                evaluations._connect = original_evaluation_connect
            orphan_evaluations = set(evaluations.evaluations_dir.glob("*.json")) - files_before
            broad_report = evaluations.put_report(broad_core)

            high = validated_candidate(
                root,
                knowledge_id="K-high-impact-lifecycle",
                high_impact=True,
            )
            before_high_snapshot = service.registry.current_snapshot_id()
            pending = service.promote(high.knowledge_id)
            after_pending_snapshot = service.registry.current_snapshot_id()
            approval = service.approve(
                pending["operation_id"],
                pending["decision_id"],
                source="test",
            )
            high_promotion = service.promote(high.knowledge_id)

            history_before_reversal = service.registry.events()
            challenged_scoped = service.challenge(
                scoped.knowledge_id,
                reason_code="holdout_regression",
            )
            rolled_back = service.rollback(structural_snapshot)
            challenged_high = service.challenge(
                high.knowledge_id,
                reason_code="policy_withdrawn",
            )
            history_after_reversal = service.registry.events()
            binding_a_after = bind_learning_snapshot(
                run_a,
                data_dir=root,
                domain_map=structural_map,
            )
            binding_b_after = bind_learning_snapshot(
                run_b,
                data_dir=root,
                domain_map=scoped_map,
            )

            doctor = doctor_report(root)
            structural_inspect = inspect_knowledge(root, structural.knowledge_id)
            scoped_explain = explain_knowledge(root, scoped.knowledge_id)
            high_explain = explain_knowledge(root, high.knowledge_id)
            current_snapshot = SnapshotStore(root).get_snapshot(
                RegistryStore(root).current_snapshot_id()
            )
            private_markers = (
                str(root).encode(),
                b"person@example.invalid",
                b"credential-value",
                b"192.0.2.123",
            )
            persisted = b"".join(
                path.read_bytes() for path in root.rglob("*") if path.is_file()
            )
            episode_signatures = {
                str(episode["structural_signature"])
                for episode in experience.episodes()
            }
            registry_findings = RegistryStore(root).integrity_findings()

        self.assertEqual(len(orphan_snapshot_files), 1)
        self.assertEqual(recovered_empty["snapshot_id"], baseline["snapshot_id"])
        self.assertEqual(structural_promotion["status"], "promoted")
        self.assertEqual(
            set(structural_report["suite_coverage"]),
            {"replay", "metamorphic", "negative", "holdout"},
        )
        self.assertGreaterEqual(len(episode_signatures), 2)
        self.assertNotEqual(structural_map.structural_signature, scoped_map.structural_signature)
        self.assertEqual(scoped_report["verdict"], "shadow_pass")
        self.assertTrue(evaluate_applicability(scoped, scoped_map).applies)
        self.assertFalse(
            evaluate_applicability(scoped, unrelated_map("project-outside")).applies
        )
        self.assertEqual(broad_report["verdict"], "rejected")
        self.assertEqual(len(orphan_evaluations), 1)
        self.assertEqual(pending["status"], "approval_required")
        self.assertEqual(before_high_snapshot, after_pending_snapshot)
        self.assertEqual(high_promotion["approval_id"], approval["approval_id"])
        self.assertNotEqual(challenged_scoped["snapshot_id"], structural_snapshot)
        self.assertEqual(rolled_back["snapshot_id"], structural_snapshot)
        self.assertEqual(challenged_high["snapshot_id"], structural_snapshot)
        self.assertEqual(history_after_reversal[: len(history_before_reversal)], history_before_reversal)
        self.assertGreater(len(history_after_reversal), len(history_before_reversal))
        self.assertEqual(binding_a_before, binding_a_after)
        self.assertEqual(binding_b_before, binding_b_after)
        self.assertNotEqual(binding_a_before["snapshot_id"], binding_b_before["snapshot_id"])
        self.assertEqual(doctor["status"], "pass")
        self.assertEqual(doctor["registry_event_count"], len(history_after_reversal))
        self.assertEqual(doctor["learning_active_operation_count"], 0)
        self.assertEqual(doctor["effective_count"], 1)
        self.assertEqual(registry_findings, [])
        self.assertTrue(structural_inspect["effective"])
        self.assertFalse(scoped_explain["effective_snapshot"]["effective"])
        self.assertTrue(high_explain["human_approval"]["consumed"])
        self.assertFalse(high_explain["effective_snapshot"]["effective"])
        self.assertEqual(
            [item["knowledge_id"] for item in current_snapshot["active_items"]],
            [structural.knowledge_id],
        )
        for marker in private_markers:
            self.assertNotIn(marker, persisted)

if __name__ == "__main__":
    unittest.main()
