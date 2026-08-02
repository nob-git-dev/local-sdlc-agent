import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from learning_runtime.cli import main as learning_main
from learning_runtime.operations import (
    doctor_report,
    explain_knowledge,
    inspect_knowledge,
)
from learning_runtime.promotion import PromotionService
from tests.test_learning_registry import validated_candidate


def run_cli(arguments):
    with contextlib.redirect_stdout(io.StringIO()) as output:
        result = learning_main(arguments)
    return result, json.loads(output.getvalue())


class LearningOperationsTests(unittest.TestCase):
    def test_inspect_and_explain_separate_authority_validation_and_effective_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            item = validated_candidate(root)
            code, promotion = run_cli(
                ["promote", "--data-dir", str(root), "--candidate", item.knowledge_id]
            )
            inspected = inspect_knowledge(root, item.knowledge_id)
            explained = explain_knowledge(root, item.knowledge_id)
            serialized = json.dumps(explained)

        self.assertEqual(code, 0)
        self.assertEqual(promotion["status"], "promoted")
        self.assertEqual(inspected["lifecycle_state"], "active")
        self.assertTrue(inspected["effective"])
        self.assertEqual(explained["hypothesis"]["authority"], "llm_hypothesis")
        self.assertEqual(explained["mechanical_validation"]["verdict"], "shadow_pass")
        self.assertFalse(explained["human_approval"]["required"])
        for forbidden in ("reasoning_content", "response_hashes", "source_body"):
            self.assertNotIn(forbidden, serialized)

    def test_high_impact_cli_requires_then_consumes_human_approval(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            item = validated_candidate(
                root,
                knowledge_id="K-operations-high",
                high_impact=True,
            )
            pending_code, pending = run_cli(
                ["promote", "--data-dir", str(root), "--candidate", item.knowledge_id]
            )
            approval_code, approval = run_cli(
                [
                    "approve-promotion",
                    "--data-dir",
                    str(root),
                    "--operation",
                    pending["operation_id"],
                    "--decision",
                    pending["decision_id"],
                ]
            )
            promoted_code, promoted = run_cli(
                ["promote", "--data-dir", str(root), "--candidate", item.knowledge_id]
            )

        self.assertEqual(pending_code, 2)
        self.assertEqual(approval_code, 0)
        self.assertEqual(approval["event"], "approved")
        self.assertEqual(promoted_code, 0)
        self.assertEqual(promoted["status"], "promoted")

    def test_challenge_and_rollback_commands_return_event_and_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            item = validated_candidate(root)
            baseline = PromotionService(root).publish_current(
                reason_code="initial_empty"
            )
            _code, promoted = run_cli(
                ["promote", "--data-dir", str(root), "--candidate", item.knowledge_id]
            )
            challenge_code, challenged = run_cli(
                [
                    "challenge",
                    "--data-dir",
                    str(root),
                    "--knowledge",
                    item.knowledge_id,
                    "--reason",
                    "observed_regression",
                ]
            )
            rollback_code, rolled_back = run_cli(
                [
                    "rollback",
                    "--data-dir",
                    str(root),
                    "--snapshot",
                    baseline["snapshot_id"],
                ]
            )

        self.assertEqual(challenge_code, 0)
        self.assertIn("registry_event_id", challenged)
        self.assertIn("snapshot_id", challenged)
        self.assertEqual(rollback_code, 0)
        self.assertEqual(rolled_back["snapshot_id"], baseline["snapshot_id"])

    def test_doctor_and_snapshot_views_account_for_persisted_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            item = validated_candidate(root)
            run_cli(
                ["promote", "--data-dir", str(root), "--candidate", item.knowledge_id]
            )
            report = doctor_report(root)
            snapshots_code, snapshots = run_cli(
                ["snapshots", "--data-dir", str(root)]
            )
            inspect_code, inspected = run_cli(
                ["inspect", "--data-dir", str(root), item.knowledge_id]
            )
            explain_code, explained = run_cli(
                ["explain", "--data-dir", str(root), item.knowledge_id]
            )

        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["candidate_count"], 1)
        self.assertEqual(report["active_count"], 1)
        self.assertGreater(report["storage_bytes"], 0)
        self.assertEqual(snapshots_code, 0)
        self.assertEqual(snapshots["current_snapshot_id"], snapshots["snapshots"][-1]["snapshot_id"])
        self.assertEqual(inspect_code, 0)
        self.assertEqual(explain_code, 0)
        self.assertEqual(inspected["knowledge_id"], explained["knowledge_id"])


if __name__ == "__main__":
    unittest.main()
