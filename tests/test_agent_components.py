import argparse
import tempfile
import unittest
from pathlib import Path

from local_sdlc.agent_prompts import (
    FAILURE_ANALYSIS_OUTPUT_CONTRACT,
    deterministic_pm_control,
    failure_analysis_instruction,
    judge_review_instruction,
    patch_conformance_instruction,
    patch_planner_instruction,
    project_policy_triage_instruction,
    root_cause_evidence_documents,
)
from local_sdlc.artifact_transaction import (
    artifact_snapshot_mismatches,
    restore_artifact_targets,
    snapshot_artifact_targets,
)
from local_sdlc.candidate_history import candidate_hypotheses, replayed_regression_hypotheses
from local_sdlc.domain_modeling import domain_modeling_decision
from local_sdlc.models import RepairAdvice, SearchReplaceArtifact
from local_sdlc.policy_triage import (
    apply_project_policy_triage_to_advice,
    generated_test_oracle_evidence_document,
    generated_test_receiver_identity_facts,
    judge_ownership_classification,
    patch_plan_requests_generated_test_oracle_triage,
    project_policy_triage_enabled,
    triage_allows_test_harness_edit,
    validate_project_policy_triage_proposition,
)
from local_sdlc.patch_conformance import parse_patch_conformance_review
from local_sdlc import repair_advice as repair_advice_facade
from local_sdlc.repair_rules import domain as domain_repair_rules
from local_sdlc.repair_rules import generic as generic_repair_rules


class AgentComponentTests(unittest.TestCase):
    def test_root_cause_context_pins_executable_evidence_outside_recent_window(self):
        documents = [
            ("Initial command 1", "command one failed"),
            ("Initial command 2", "command two failed"),
            ("Initial observation summary", "two failures"),
            ("Unrelated diagnostic", "old detail"),
            ("Candidate regression rollback round 1", "candidate was worse"),
            ("Failure transition round 2", "transition"),
            ("Root cause missing context round 2", "request file"),
        ]

        selected = root_cause_evidence_documents(documents, 1)
        titles = [title for title, _document in selected]

        self.assertIn("Initial command 1", titles)
        self.assertIn("Initial command 2", titles)
        self.assertIn("Initial observation summary", titles)
        self.assertIn("Candidate regression rollback round 1", titles)
        self.assertIn("Root cause missing context round 2", titles)
        self.assertNotIn("Unrelated diagnostic", titles)

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
        conformance = patch_conformance_instruction(
            "repair app",
            "PATCH_PLAN\n- minimal_patch_goal: synchronize all entries",
            "BEGIN_SEARCH_REPLACE: app.py\n...",
        )
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
        self.assertIn("Select one independent defect", planner)
        self.assertIn("one contiguous source span", planner)
        self.assertIn("coordinated edits", planner)
        self.assertIn("does not by itself prove", planner)
        self.assertIn("escalation: none|generated_test_oracle_triage|collect_context", planner)
        self.assertIn("planner cannot authorize or apply a test edit", planner)
        self.assertIn("Merely touching required_path", conformance)
        self.assertIn('"missing_obligations"', conformance)
        self.assertIn("T cannot directly apply an edit", triage)
        self.assertIn('start with "判定: 承認"', judge)
        self.assertIn("OWNERSHIP:", judge)
        self.assertIn("Only writable targets", pm)

    def test_patch_conformance_review_fails_closed_on_unresolved_obligation(self):
        review = parse_patch_conformance_review(
            """
            {
              "status": "pass",
              "obligations": [
                {
                  "id": "O1",
                  "requirement": "index exactly matches target tree",
                  "status": "not_satisfied",
                  "candidate_evidence": "removes paths absent from target",
                  "counterexample": "target-only path is never added"
                }
              ],
              "missing_obligations": [],
              "missing_context_paths": [],
              "safe_next_action": "apply",
              "repair_instruction": "add target-only entries"
            }
            """
        )

        self.assertEqual(review["status"], "fail")
        self.assertEqual(review["safe_next_action"], "repair_artifact")
        self.assertIn("index exactly matches target tree", review["missing_obligations"])

    def test_patch_conformance_review_accepts_evidenced_complete_candidate(self):
        review = parse_patch_conformance_review(
            """
            {
              "status": "pass",
              "obligations": [
                {
                  "id": "O1",
                  "requirement": "index exactly matches target tree",
                  "status": "satisfied",
                  "candidate_evidence": "clears index, copies every target entry, then saves",
                  "counterexample": "(none)"
                }
              ],
              "missing_obligations": [],
              "missing_context_paths": [],
              "safe_next_action": "apply",
              "repair_instruction": ""
            }
            """
        )

        self.assertEqual(review["status"], "pass")
        self.assertEqual(review["safe_next_action"], "apply")

    def test_candidate_hypothesis_ignores_unchanged_search_context(self):
        wide = SearchReplaceArtifact(
            path="app.py",
            search="call(old)\nkeep()\n",
            replace="call(new)\nkeep()\n",
        )
        narrow = SearchReplaceArtifact(
            path="app.py",
            search="call(old)\n",
            replace="call(new)\n",
        )

        self.assertEqual(
            candidate_hypotheses([wide])[0]["signature"],
            candidate_hypotheses([narrow])[0]["signature"],
        )

    def test_regression_replay_blocks_subset_but_allows_new_compensating_delta(self):
        rejected_primary = SearchReplaceArtifact("app.py", "call(old)\n", "call(bad)\n")
        rejected_secondary = SearchReplaceArtifact("app.py", "flag = 0\n", "flag = 1\n")
        rejected = candidate_hypotheses([rejected_primary, rejected_secondary])
        regressions = [{"round": 1, "candidate_hypotheses": rejected}]

        replay = candidate_hypotheses([rejected_primary])
        compensating = candidate_hypotheses(
            [
                rejected_primary,
                SearchReplaceArtifact("owner.py", "state = old\n", "state = repaired\n"),
            ]
        )

        self.assertEqual(len(replayed_regression_hypotheses(replay, regressions)), 1)
        self.assertEqual(replayed_regression_hypotheses(compensating, regressions), [])

    def test_generated_test_oracle_evidence_uses_primary_sources_only(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "tests").mkdir()
            (project / "tests" / "test_app.py").write_text(
                "def test_rule():\n    assert run() == 'expected'\n",
                encoding="utf-8",
            )
            evidence = generated_test_oracle_evidence_document(
                project,
                "# SPEC\nrun() returns 'actual'.\n",
                ["tests/test_app.py"],
                [("Command", "AssertionError: actual != expected")],
                "判定: 修正依頼\nOWNERSHIP: test_harness",
            )

        self.assertIn("stage_owned_generated_tests: tests/test_app.py", evidence)
        self.assertIn("assert run() == 'expected'", evidence)
        self.assertIn("run() returns 'actual'", evidence)
        self.assertIn("AssertionError: actual != expected", evidence)
        self.assertIn("OWNERSHIP: test_harness", evidence)
        self.assertIn("Repair advice and prior failure-analysis conclusions are intentionally excluded", evidence)

    def test_generated_test_receiver_identity_facts_distinguish_fresh_instances(self):
        source = """
def test_resume():
    first = Engine(tasks, handlers).run(max_tasks=1)
    second = Engine(tasks, handlers).run(max_tasks=1)
    assert second.completed
"""

        facts = generated_test_receiver_identity_facts(source, "tests/test_engine.py")

        self.assertEqual(len(facts), 1)
        self.assertIn("2 distinct fresh constructor expressions", facts[0])
        self.assertIn("these receivers are not the same instance", facts[0])

    def test_generated_test_oracle_evidence_includes_receiver_identity_facts(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "tests").mkdir()
            (project / "tests" / "test_engine.py").write_text(
                "def test_resume():\n"
                "    first = Engine(tasks, handlers).run(max_tasks=1)\n"
                "    second = Engine(tasks, handlers).run(max_tasks=1)\n",
                encoding="utf-8",
            )
            evidence = generated_test_oracle_evidence_document(
                project,
                "# SPEC\nNo implicit persistence.\n",
                ["tests/test_engine.py"],
                [("Command", "AssertionError")],
            )

        self.assertIn("Mechanical Receiver Identity Facts", evidence)
        self.assertIn("these receivers are not the same instance", evidence)

    def test_generated_oracle_proposition_gate_rejects_unsupported_product_vote(self):
        gated = validate_project_policy_triage_proposition(
            {
                "trigger": "generated_test_oracle_conflict",
                "case_type": "product_bug",
                "selected_hypothesis": "H_product",
                "product_violation_evidence": [],
                "safe_next_action": "root_cause_analysis",
                "editable_paths": ["app.py"],
            }
        )

        self.assertEqual(gated["case_type"], "insufficient_context")
        self.assertEqual(gated["safe_next_action"], "reject")
        self.assertEqual(gated["editable_paths"], [])
        self.assertEqual(gated["proposition_gate"]["status"], "reject")

    def test_generated_oracle_proposition_gate_accepts_positive_test_counterexample(self):
        gated = validate_project_policy_triage_proposition(
            {
                "trigger": "generated_test_oracle_conflict",
                "case_type": "test_harness",
                "selected_hypothesis": "H_test",
                "test_contradiction_evidence": ["setup deletes a tracked file before non-force checkout"],
                "safe_next_action": "edit_test_harness",
                "editable_paths": ["tests/test_app.py"],
            }
        )

        self.assertEqual(gated["case_type"], "test_harness")
        self.assertEqual(gated["proposition_gate"]["status"], "pass")

    def test_judge_ownership_parser_requires_explicit_line(self):
        self.assertEqual(
            judge_ownership_classification("判定: 修正依頼\nOWNERSHIP: product_bug\n"),
            "product_bug",
        )
        self.assertEqual(judge_ownership_classification("the test may be wrong"), "not_applicable")

    def test_patch_plan_oracle_escalation_requires_one_exact_control_pair(self):
        valid = """PATCH_PLAN
- patch_type: missing_context
- escalation: generated_test_oracle_triage
"""
        prose_only = "The generated test may need generated_test_oracle_triage."
        conflicting = """PATCH_PLAN
- patch_type: missing_context
- escalation: generated_test_oracle_triage
- escalation: collect_context
"""

        self.assertTrue(patch_plan_requests_generated_test_oracle_triage(valid))
        self.assertFalse(patch_plan_requests_generated_test_oracle_triage(prose_only))
        self.assertFalse(patch_plan_requests_generated_test_oracle_triage(conflicting))

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
            self.assertEqual(artifact_snapshot_mismatches(project, snapshots), [])

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
