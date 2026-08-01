import argparse
import tempfile
import unittest
from pathlib import Path

from local_sdlc.agent_prompts import (
    FAILURE_ANALYSIS_OUTPUT_CONTRACT,
    deterministic_pm_control,
    failure_analysis_instruction,
    judge_review_instruction,
    patch_planner_instruction,
    project_policy_triage_instruction,
)
from local_sdlc.artifact_transaction import restore_artifact_targets, snapshot_artifact_targets
from local_sdlc.domain_modeling import domain_modeling_decision
from local_sdlc.models import RepairAdvice
from local_sdlc.policy_triage import (
    apply_project_policy_triage_to_advice,
    project_policy_triage_enabled,
    triage_allows_test_harness_edit,
)
from local_sdlc import repair_advice as repair_advice_facade
from local_sdlc.repair_rules import domain as domain_repair_rules
from local_sdlc.repair_rules import generic as generic_repair_rules


class AgentComponentTests(unittest.TestCase):
    def test_repair_advice_facade_delegates_to_separate_rule_modules(self):
        self.assertIs(
            repair_advice_facade.acceptance_gate_blockers_from_command_docs,
            generic_repair_rules.acceptance_gate_blockers_from_command_docs,
        )
        self.assertIs(
            repair_advice_facade.repair_advice_from_command_docs,
            domain_repair_rules.repair_advice_from_command_docs,
        )

    def test_role_prompt_builders_preserve_machine_contracts(self):
        failure = failure_analysis_instruction(
            "repair app",
            2,
            "command_failed",
            "family:test_app",
            1,
            [{"failure_type": "command_failed"}],
            [],
            "exit_code: 1",
        )
        planner = patch_planner_instruction("repair app", "root_cause_analysis", "{}")
        triage = project_policy_triage_instruction(
            "repair app",
            "test_harness_ownership",
            "edit tests/test_app.py",
            [],
            [],
            "SPEC evidence",
        )
        judge = judge_review_instruction("repair app", 2, 4)
        pm = deterministic_pm_control("repair app", ["app.py"], False, ["tests/test_app.py"])

        self.assertIn("same(F_i, F_t) and applied(A_i)", failure)
        self.assertIn('"next_required_action"', failure)
        self.assertIn("No code artifacts", FAILURE_ANALYSIS_OUTPUT_CONTRACT)
        self.assertIn("patch_type: search_replace|unified_diff|missing_context", planner)
        self.assertIn("T cannot directly apply an edit", triage)
        self.assertIn('start with "判定: 承認"', judge)
        self.assertIn("Only writable targets", pm)

    def test_artifact_transaction_restores_existing_and_new_targets(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            existing = project / "app.py"
            existing.write_text("before\n", encoding="utf-8")
            snapshots = snapshot_artifact_targets(project, ["app.py", "new.py"])
            existing.write_text("after\n", encoding="utf-8")
            (project / "new.py").write_text("new\n", encoding="utf-8")

            restored = restore_artifact_targets(project, snapshots)

            self.assertEqual(existing.read_text(encoding="utf-8"), "before\n")
            self.assertFalse((project / "new.py").exists())
            self.assertEqual(restored, ["app.py", "new.py"])

    def test_project_policy_triage_requires_explicit_high_or_medium_authorization(self):
        record = {
            "case_type": "test_harness",
            "safe_next_action": "edit_test_harness",
            "confidence": "high",
            "editable_paths": ["tests/test_app.py"],
        }
        advice = RepairAdvice(
            strategy="replace_test_harness",
            focus_files=("tests/test_app.py",),
            instructions=("repair generated oracle",),
        )

        updated = apply_project_policy_triage_to_advice(
            advice,
            record,
            ["app.py", "tests/test_app.py"],
            ["replace_test_harness"],
        )

        self.assertTrue(project_policy_triage_enabled("auto", "test_harness_ownership"))
        self.assertTrue(triage_allows_test_harness_edit(record))
        self.assertEqual(updated.strategy, "replace_test_harness")
        self.assertIn("tests/test_app.py", updated.focus_files)

    def test_domain_modeling_decision_respects_disabled_mode(self):
        args = argparse.Namespace(domain_modeling="never", domain_skill="ddd", brief="build app")

        decision = domain_modeling_decision(args, {}, "# SPEC")

        self.assertFalse(decision["run"])
        self.assertEqual(decision["reason"], "disabled")


if __name__ == "__main__":
    unittest.main()
