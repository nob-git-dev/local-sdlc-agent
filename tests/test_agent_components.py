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
    generated_test_receiver_lineage_facts,
    judge_ownership_classification,
    patch_plan_requests_generated_test_oracle_triage,
    project_policy_triage_enabled,
    receiver_identity_facts_from_evidence,
    receiver_lineage_facts_from_evidence,
    triage_allows_test_harness_edit,
    validate_project_policy_triage_proposition,
)
from local_sdlc.patch_conformance import (
    patch_plan_policy_contradictions,
    patch_plan_signature,
    parse_patch_conformance_review,
    repeated_patch_conformance_failure,
)
from local_sdlc import repair_advice as repair_advice_facade
from local_sdlc.repair_rules import domain as domain_repair_rules
from local_sdlc.repair_rules import generic as generic_repair_rules


class AgentComponentTests(unittest.TestCase):
    def test_patch_plan_rejects_mutation_of_its_forbidden_generated_test(self):
        plan = """
PATCH_PLAN
- proposition: If ownership moves, change app/model.py and update tests/test_model.py.
- required_paths: app/model.py, app/validator.py
- readonly_paths: SPEC.md
- forbidden_paths: tests/, acceptance_tests/
- patch_type: unified_diff
- escalation: none
- minimal_patch_goal: Move validation from the model to the validator.
"""

        conflicts = patch_plan_policy_contradictions(plan)

        self.assertEqual(len(conflicts), 1)
        self.assertIn("tests/test_model.py", conflicts[0])

    def test_patch_plan_allows_protected_test_as_non_mutated_evidence(self):
        plan = """
PATCH_PLAN
- proposition: If ownership moves, change app/model.py so fixed acceptance passes.
- required_paths: app/model.py, app/validator.py
- readonly_paths: tests/test_model.py
- forbidden_paths: acceptance_tests/
- patch_type: unified_diff
- escalation: none
- minimal_patch_goal: Preserve tests/test_model.py as post-apply evidence.
"""

        self.assertEqual(patch_plan_policy_contradictions(plan), [])

    def test_patch_plan_allows_explicit_non_mutation_of_protected_test(self):
        plan = """
PATCH_PLAN
- proposition: Change app/model.py without editing tests/test_model.py.
- required_path: app/model.py
- readonly_paths: tests/test_model.py
- forbidden_paths: tests/
- patch_type: search_replace
- escalation: none
- minimal_patch_goal: Change only product validation ownership.
"""

        self.assertEqual(patch_plan_policy_contradictions(plan), [])

    def test_patch_plan_allows_oracle_escalation_to_name_generated_test(self):
        plan = """
PATCH_PLAN
- proposition: If tests/test_model.py contradicts SPEC.md, update the generated oracle.
- required_paths: (none)
- readonly_paths: SPEC.md
- forbidden_paths: tests/
- patch_type: missing_context
- escalation: generated_test_oracle_triage
- minimal_patch_goal: Route ownership without applying an edit.
"""

        self.assertEqual(patch_plan_policy_contradictions(plan), [])

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
        self.assertIn("required_paths", planner)
        self.assertIn("responsibility move", planner)
        self.assertIn("does not by itself prove", planner)
        self.assertIn("escalation: none|generated_test_oracle_triage|collect_context", planner)
        self.assertIn("planner cannot authorize or apply a test edit", planner)
        self.assertIn("Merely touching required_path", conformance)
        self.assertIn("post-apply test success", conformance)
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

    def test_repeated_patch_conformance_failure_requires_same_plan_and_obligations(self):
        signature = patch_plan_signature("PATCH_PLAN\n- required_path: app.py")
        reviews = [
            {
                "status": "fail",
                "patch_plan_signature": signature,
                "missing_obligations": ["move validation to its owner"],
            },
            {
                "status": "fail",
                "patch_plan_signature": signature,
                "missing_obligations": ["move validation to its owner"],
            },
        ]

        self.assertTrue(repeated_patch_conformance_failure(reviews))
        reviews[-1]["patch_plan_signature"] = patch_plan_signature("another plan")
        self.assertFalse(repeated_patch_conformance_failure(reviews))
        reviews[-1] = {
            **reviews[0],
            "status": "pass",
            "missing_obligations": [],
        }
        reviews.append(dict(reviews[0]))
        self.assertFalse(repeated_patch_conformance_failure(reviews))

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

    def test_patch_conformance_defers_post_apply_test_evidence_to_runner(self):
        review = parse_patch_conformance_review(
            """
            {
              "status": "fail",
              "obligations": [
                {
                  "id": "O1",
                  "requirement": "move validation from the value object to the aggregate",
                  "status": "satisfied",
                  "candidate_evidence": "removes the old check and adds the aggregate check",
                  "counterexample": "(none)"
                },
                {
                  "id": "O2",
                  "requirement": "Stop when the unit test suite exits 0",
                  "status": "not_satisfied",
                  "candidate_evidence": "(none)",
                  "counterexample": "tests have not run yet"
                }
              ],
              "missing_obligations": ["Executable evidence that the test suite exits 0"],
              "missing_context_paths": [],
              "safe_next_action": "repair_artifact",
              "repair_instruction": "run tests first"
            }
            """
        )

        self.assertEqual(review["status"], "pass")
        self.assertEqual(review["safe_next_action"], "apply")
        self.assertEqual(
            review["deferred_verification_obligations"],
            ["Stop when the unit test suite exits 0"],
        )

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
        self.assertIn('shared constructor arguments JSON: ["handlers", "tasks"]', facts[0])

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
        self.assertEqual(len(receiver_identity_facts_from_evidence(evidence)), 1)

    def test_generated_test_receiver_lineage_facts_track_method_return_aliases(self):
        source = """
def test_resume():
    first = Engine(tasks, handlers).run(max_tasks=1)
    second = first
    second = second.run()
"""

        facts = generated_test_receiver_lineage_facts(source, "tests/test_engine.py")

        self.assertEqual(len(facts), 1)
        self.assertIn("second.run()", facts[0])
        self.assertIn("alias of the syntactic return value of Engine.run()", facts[0])
        self.assertIn("does not establish that this value is the original constructor receiver", facts[0])

    def test_generated_test_oracle_evidence_includes_receiver_lineage_facts(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "tests").mkdir()
            (project / "tests" / "test_engine.py").write_text(
                "def test_resume():\n"
                "    report = Engine(tasks, handlers).run(max_tasks=1)\n"
                "    report.run()\n",
                encoding="utf-8",
            )
            evidence = generated_test_oracle_evidence_document(
                project,
                "# SPEC\nEngine.run returns a report.\n",
                ["tests/test_engine.py"],
                [("Command", "AttributeError")],
            )

        self.assertIn("Mechanical Receiver Lineage Facts", evidence)
        self.assertIn("syntactic return value of Engine.run()", evidence)
        self.assertEqual(len(receiver_lineage_facts_from_evidence(evidence)), 1)

    def test_generated_oracle_gate_corrects_unsupported_cross_instance_continuity(self):
        fact = (
            "tests/test_engine.py:test_resume calls Engine(...).run(...) on 2 distinct "
            "fresh constructor expressions at lines 4, 5; these receivers are not the same instance."
        )
        gated = validate_project_policy_triage_proposition(
            {
                "trigger": "generated_test_oracle_conflict",
                "case_type": "product_bug",
                "selected_hypothesis": "H_product",
                "product_violation_evidence": ["a fresh engine should resume the prior run"],
                "test_contradiction_evidence": [],
                "receiver_scope_analysis": {
                    "mechanical_identity": "distinct_fresh",
                    "requires_cross_instance_continuity": True,
                    "continuity_witness": "none",
                    "witness_evidence": [],
                },
                "safe_next_action": "root_cause_analysis",
                "editable_paths": ["engine.py"],
            },
            receiver_identity_facts=[fact],
            generated_test_paths=["tests/test_engine.py"],
        )

        self.assertEqual(gated["case_type"], "test_harness")
        self.assertEqual(gated["selected_hypothesis"], "H_test")
        self.assertEqual(gated["safe_next_action"], "edit_test_harness")
        self.assertEqual(gated["editable_paths"], ["tests/test_engine.py"])
        self.assertEqual(gated["proposition_gate"]["status"], "corrected")

    def test_generated_oracle_gate_accepts_explicit_cross_instance_persistence(self):
        gated = validate_project_policy_triage_proposition(
            {
                "trigger": "generated_test_oracle_conflict",
                "case_type": "product_bug",
                "selected_hypothesis": "H_product",
                "product_violation_evidence": ["SPEC requires both instances to load state.db"],
                "test_contradiction_evidence": [],
                "receiver_scope_analysis": {
                    "mechanical_identity": "distinct_fresh",
                    "requires_cross_instance_continuity": True,
                    "continuity_witness": "explicit_shared_persistence",
                    "continuity_witness_expression": "state_path",
                    "witness_evidence": ["SPEC: new instances load the supplied state path"],
                },
            },
            receiver_identity_facts=[
                "distinct fresh constructor expressions; receivers are not the same instance; "
                'shared constructor arguments JSON: ["state_path", "tasks"].'
            ],
            generated_test_paths=["tests/test_engine.py"],
        )

        self.assertEqual(gated["case_type"], "product_bug")
        self.assertEqual(gated["proposition_gate"]["status"], "pass")

    def test_generated_oracle_gate_corrects_unsupported_return_value_receiver(self):
        fact = (
            "tests/test_engine.py:test_resume calls report.run() at line 5 on an alias of "
            "the syntactic return value of Engine.run(); source syntax does not establish "
            "that this value is the original constructor receiver."
        )
        gated = validate_project_policy_triage_proposition(
            {
                "trigger": "generated_test_oracle_conflict",
                "case_type": "product_bug",
                "selected_hypothesis": "H_product",
                "product_violation_evidence": ["the returned report should continue execution"],
                "test_contradiction_evidence": [],
                "receiver_lineage_analysis": {
                    "mechanical_lineage": "method_return_value",
                    "method_defined_on_return_value": False,
                    "contract_evidence": [],
                },
                "safe_next_action": "root_cause_analysis",
                "editable_paths": ["engine.py"],
            },
            receiver_lineage_facts=[fact],
            generated_test_paths=["tests/test_engine.py"],
        )

        self.assertEqual(gated["case_type"], "test_harness")
        self.assertEqual(gated["selected_hypothesis"], "H_test")
        self.assertEqual(gated["safe_next_action"], "edit_test_harness")
        self.assertEqual(gated["editable_paths"], ["tests/test_engine.py"])
        self.assertEqual(
            gated["proposition_gate"]["reason"],
            "unsupported_return_value_receiver",
        )

    def test_generated_oracle_gate_accepts_explicit_fluent_return_contract(self):
        gated = validate_project_policy_triage_proposition(
            {
                "trigger": "generated_test_oracle_conflict",
                "case_type": "product_bug",
                "selected_hypothesis": "H_product",
                "product_violation_evidence": ["SPEC says advance returns the same workflow"],
                "test_contradiction_evidence": [],
                "receiver_lineage_analysis": {
                    "mechanical_lineage": "method_return_value",
                    "method_defined_on_return_value": True,
                    "contract_evidence": ["SPEC: advance() returns self for fluent continuation"],
                },
                "safe_next_action": "root_cause_analysis",
                "editable_paths": ["workflow.py"],
            },
            receiver_lineage_facts=[
                "tests/test_workflow.py:test_chain calls result.advance() at line 5 on an alias "
                "of the syntactic return value of Workflow.advance(); source syntax does not "
                "establish that this value is the original constructor receiver."
            ],
            generated_test_paths=["tests/test_workflow.py"],
        )

        self.assertEqual(gated["case_type"], "product_bug")
        self.assertEqual(gated["proposition_gate"]["status"], "pass")

    def test_generated_oracle_gate_rejects_conditional_persistence_missing_from_calls(self):
        gated = validate_project_policy_triage_proposition(
            {
                "trigger": "generated_test_oracle_conflict",
                "case_type": "product_bug",
                "selected_hypothesis": "H_product",
                "product_violation_evidence": ["SPEC resumes a new instance with a checkpoint"],
                "test_contradiction_evidence": [],
                "receiver_scope_analysis": {
                    "mechanical_identity": "distinct_fresh",
                    "requires_cross_instance_continuity": True,
                    "continuity_witness": "explicit_shared_persistence",
                    "continuity_witness_expression": "checkpoint_path",
                    "witness_evidence": ["SPEC: a new instance with the same checkpoint resumes"],
                },
            },
            receiver_identity_facts=[
                "tests/test_engine.py:test_resume calls Engine(...).run(...) on 2 distinct "
                "fresh constructor expressions at lines 4, 5; these receivers are not the "
                'same instance; shared constructor arguments JSON: ["handlers", "tasks"].'
            ],
            generated_test_paths=["tests/test_engine.py"],
        )

        self.assertEqual(gated["case_type"], "test_harness")
        self.assertEqual(gated["safe_next_action"], "edit_test_harness")
        self.assertEqual(gated["proposition_gate"]["status"], "corrected")

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
