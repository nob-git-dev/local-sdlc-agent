import json
import tempfile
from pathlib import Path

from tests.helpers import LocalSDLCTestCase


class AutonomyRuntimeTests(LocalSDLCTestCase):
    def test_live_generation_timeout_extends_bounded_request_window(self):
        stage = self.local_sdlc.StageWorkItem(
            stage_id="S01",
            title="Core",
            goal="Implement core behavior.",
            suggested_paths=("app.py",),
            test_focus=("core tests",),
            writable_paths=("app.py",),
        )
        summary = self.local_sdlc.StageRunSummary(
            stage_id="S01",
            title="Core",
            status="failed",
            run_dir="run/s01",
            exit_code=1,
            failure_summary={
                "failure_type": "llm_generation_timeout",
                "timeout_seconds": 300.0,
                "api_health": "alive",
            },
        )

        decision = self.local_sdlc.decide_stage_recovery(
            stage,
            summary,
            recovery_count=0,
            max_recoveries=3,
        )

        self.assertEqual(decision.action, "extend_llm_timeout")
        self.assertEqual(decision.reason_code, "live_api_generation_timeout")
        self.assertEqual(decision.metadata["timeout_seconds"], 600.0)
        self.assertTrue(decision.resume_failed_worktree)

    def test_generation_timeout_fails_closed_at_bounded_maximum(self):
        stage = self.local_sdlc.StageWorkItem(
            stage_id="S01",
            title="Core",
            goal="Implement core behavior.",
            suggested_paths=("app.py",),
            test_focus=("core tests",),
            writable_paths=("app.py",),
        )
        summary = self.local_sdlc.StageRunSummary(
            stage_id="S01",
            title="Core",
            status="failed",
            run_dir="run/s01",
            exit_code=1,
            failure_summary={
                "failure_type": "llm_generation_timeout",
                "timeout_seconds": 1800.0,
                "api_health": "alive",
            },
        )

        decision = self.local_sdlc.decide_stage_recovery(
            stage,
            summary,
            recovery_count=1,
            max_recoveries=3,
        )

        self.assertEqual(decision.action, "fail_closed")
        self.assertEqual(decision.reason_code, "generation_timeout_exhausted")

    def test_human_decision_boundary_is_narrow_and_explicit(self):
        self.assertTrue(self.local_sdlc.requires_human_decision("spec_conflict"))
        self.assertTrue(self.local_sdlc.requires_human_decision("budget_extension_required"))
        self.assertFalse(self.local_sdlc.requires_human_decision("stage_split"))
        self.assertFalse(self.local_sdlc.requires_human_decision("artifact_format_repair"))
        self.assertFalse(self.local_sdlc.requires_human_decision("failure_analysis"))

    def test_autonomy_audit_counts_unauthorized_external_intervention(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            self.local_sdlc.record_autonomy_decision(
                run_dir,
                scope="stage:S01",
                action="split_stage",
                reason_code="stage_split",
                rationale="The failed stage has two independently verifiable path groups.",
                evidence_paths=("run.json",),
            )
            self.local_sdlc.record_external_intervention(
                run_dir,
                action="manual_retry",
                reason_code="stage_split",
                rationale="An operator retried an internal reversible choice.",
            )

            audit = self.local_sdlc.autonomy_audit(run_dir)

        self.assertEqual(audit["autonomous_decision_count"], 1)
        self.assertEqual(audit["external_intervention_count"], 1)
        self.assertEqual(audit["unauthorized_external_intervention_count"], 1)
        self.assertFalse(audit["zero_unauthorized_external_interventions"])

    def test_actionable_blocked_state_requires_human_reason_and_evidence(self):
        blocked = self.local_sdlc.actionable_blocked_state(
            reason_code="external_resource_required",
            summary="A required signing credential is not available to the runtime.",
            evidence_paths=("run.json", "09-command.md"),
            required_human_input="Provide the credential through the configured secret channel.",
        )

        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertEqual(blocked["blocked_reason"]["code"], "external_resource_required")
        self.assertEqual(blocked["supporting_evidence"], ["run.json", "09-command.md"])
        self.assertIn("credential", blocked["required_human_input"])
        with self.assertRaises(self.local_sdlc.RunnerError):
            self.local_sdlc.actionable_blocked_state(
                reason_code="stage_split",
                summary="Internal choice",
                evidence_paths=("run.json",),
                required_human_input="Choose a split.",
            )

    def test_completion_gate_rejects_every_unverified_acceptance_item(self):
        matrix = [
            {"id": "A01", "status": "pass", "required_covers": ["command:x"]},
            {"id": "A02", "status": "unverified", "required_covers": []},
        ]

        result = self.local_sdlc.evaluate_completion_gate(matrix)

        self.assertEqual(result["status"], "acceptance_failed")
        self.assertEqual([item["id"] for item in result["blockers"]], ["A02"])
        passed = self.local_sdlc.evaluate_completion_gate(
            [{"id": "A01", "status": "pass", "required_covers": []}]
        )
        self.assertEqual(passed["status"], "approved")

    def test_spec_stage_plan_contract_overrides_domain_heuristics(self):
        spec = """
# Pocket Version Control

## Implementation Stages

```json
{
  "stage_plan_schema": 1,
  "stages": [
    {
      "stage_id": "S01",
      "title": "Object identity",
      "goal": "Store and retrieve immutable objects by digest.",
      "writable_paths": ["minigit/objects.py", "tests/test_objects.py"],
      "readonly_evidence_paths": [],
      "test_commands": ["python3 -m unittest tests.test_objects"],
      "required_observables": ["command:python3 -m unittest tests.test_objects"],
      "max_rounds": 4
    }
  ]
}
```
"""

        stages = self.local_sdlc.synthesize_stage_queue(spec)

        self.assertEqual(len(stages), 1)
        self.assertEqual(stages[0].title, "Object identity")
        self.assertEqual(stages[0].writable_paths, ("minigit/objects.py", "tests/test_objects.py"))
        self.assertEqual(stages[0].max_rounds, 4)
        self.assertNotIn("SQL lexer", [stage.title for stage in stages])

    def test_invalid_marked_stage_plan_fails_closed(self):
        spec = """
## Implementation Stages
```json
{"stage_plan_schema": 1, "stages": [{"stage_id": "S02", "title": "bad"}]}
```
"""

        with self.assertRaises(self.local_sdlc.RunnerError):
            self.local_sdlc.synthesize_stage_queue(spec)

    def test_stage_plan_rejects_invalid_function_api_profile_before_execution(self):
        spec = """
## Implementation Stages
```json
{
  "stage_plan_schema": 1,
  "stages": [
    {
      "stage_id": "S01",
      "title": "Core",
      "goal": "Implement the core.",
      "writable_paths": ["core.py"],
      "api_profile": ["generate_artifact"]
    }
  ]
}
```
"""

        with self.assertRaisesRegex(
            self.local_sdlc.RunnerError,
            "S01.api_profile.*function:key=value",
        ):
            self.local_sdlc.synthesize_stage_queue(spec)

    def test_stage_plan_accepts_valid_function_api_profile_override(self):
        spec = """
## Implementation Stages
```json
{
  "stage_plan_schema": 1,
  "stages": [
    {
      "stage_id": "S01",
      "title": "Core",
      "goal": "Implement the core.",
      "writable_paths": ["core.py"],
      "api_profile": ["generate_artifact:max_tokens=8192,temperature=0.05,thinking=off"]
    }
  ]
}
```
"""

        stage = self.local_sdlc.synthesize_stage_queue(spec)[0]

        self.assertEqual(
            stage.api_profile,
            ("generate_artifact:max_tokens=8192,temperature=0.05,thinking=off",),
        )

    def test_verification_commands_are_read_only_from_explicit_section(self):
        spec = """
## Example
Do not execute `python3 dangerous_example.py`.

## Verification Commands
- `python3 -m unittest discover -s tests`
```bash
python3 -m compileall -q package
```

## Notes
`python3 ignored.py`
"""

        commands = self.local_sdlc.verification_commands_from_spec(spec)

        self.assertEqual(
            commands,
            [
                "python3 -m unittest discover -s tests",
                "python3 -m compileall -q package",
            ],
        )

    def test_recovery_selector_changes_strategy_without_human_for_internal_failure(self):
        stage = self.local_sdlc.StageWorkItem(
            stage_id="S03",
            title="Repository operations",
            goal="Implement repository operations.",
            suggested_paths=("minigit/repository.py", "minigit/index.py"),
            test_focus=("repository tests",),
            writable_paths=("minigit/repository.py", "minigit/index.py"),
        )
        protocol_summary = self.local_sdlc.StageRunSummary(
            stage_id="S03",
            title=stage.title,
            status="failed",
            run_dir="run/s03",
            exit_code=1,
            failure_summary={"failure_type": "format_repair_malformed_search_replace"},
        )

        format_decision = self.local_sdlc.decide_stage_recovery(
            stage,
            protocol_summary,
            recovery_count=0,
            max_recoveries=3,
            previous_actions=(),
        )
        split_decision = self.local_sdlc.decide_stage_recovery(
            stage,
            self.local_sdlc.StageRunSummary(
                stage_id="S03",
                title=stage.title,
                status="failed",
                run_dir="run/s03",
                exit_code=1,
                failure_summary={"failure_type": "command_failed"},
            ),
            recovery_count=1,
            max_recoveries=3,
            previous_actions=("format_repair",),
        )

        self.assertEqual(format_decision.action, "format_repair")
        self.assertEqual(format_decision.artifact_format, "legacy")
        self.assertFalse(format_decision.human_required)
        self.assertEqual(split_decision.action, "split_stage")
        self.assertFalse(split_decision.human_required)

    def test_missing_context_is_semantic_recovery_not_format_repair(self):
        stage = self.local_sdlc.StageWorkItem(
            stage_id="S03",
            title="Repository operations",
            goal="Implement repository operations.",
            suggested_paths=("minigit/repository.py", "minigit/index.py"),
            test_focus=("repository tests",),
            writable_paths=("minigit/repository.py", "minigit/index.py"),
        )
        summary = self.local_sdlc.StageRunSummary(
            stage_id=stage.stage_id,
            title=stage.title,
            status="failed",
            run_dir="run/s03",
            exit_code=1,
            failure_summary={"failure_type": "missing_context"},
        )

        decision = self.local_sdlc.decide_stage_recovery(
            stage,
            summary,
            recovery_count=0,
            max_recoveries=3,
            previous_actions=(),
        )

        self.assertFalse(self.local_sdlc.is_protocol_failure_type("missing_context"))
        self.assertTrue(self.local_sdlc.is_protocol_failure_type("format_repair_missing_context"))
        self.assertNotEqual(decision.action, "format_repair")

    def test_stage_split_preserves_paths_and_defers_joint_command_to_last_slice(self):
        stage = self.local_sdlc.StageWorkItem(
            stage_id="S04",
            title="Working tree and commit graph",
            goal="Implement four coupled modules.",
            suggested_paths=("a.py", "b.py", "c.py", "tests/test_graph.py"),
            test_focus=("graph acceptance",),
            test_commands=("python3 -m unittest tests.test_graph",),
            writable_paths=("a.py", "b.py", "c.py", "tests/test_graph.py"),
        )

        children = self.local_sdlc.split_stage_work_item(stage, parts=2)

        self.assertEqual([child.stage_id for child in children], ["S04.1", "S04.2"])
        flattened = [path for child in children for path in child.writable_paths]
        self.assertCountEqual(flattened, stage.writable_paths)
        self.assertEqual(children[0].test_commands, ())
        self.assertEqual(children[1].test_commands, stage.test_commands)

    def test_stage_split_keeps_matching_tests_with_product_paths(self):
        stage = self.local_sdlc.StageWorkItem(
            stage_id="S02",
            title="Repository and index",
            goal="Implement two independently testable modules.",
            suggested_paths=(
                "pkg/index.py",
                "pkg/repository.py",
                "tests/test_index.py",
                "tests/test_repository.py",
            ),
            test_focus=("repository tests",),
            test_commands=("python3 -m unittest discover -s tests",),
            writable_paths=(
                "pkg/index.py",
                "pkg/repository.py",
                "tests/test_index.py",
                "tests/test_repository.py",
            ),
        )

        children = self.local_sdlc.split_stage_work_item(stage, parts=2)

        self.assertEqual(
            [set(child.writable_paths) for child in children],
            [
                {"pkg/index.py", "tests/test_index.py"},
                {"pkg/repository.py", "tests/test_repository.py"},
            ],
        )
        for child in children:
            self.assertEqual(set(child.repair_scope_paths), set(stage.writable_paths))

    def test_stage_split_runs_joint_verification_on_integration_boundary_last(self):
        stage = self.local_sdlc.StageWorkItem(
            stage_id="S02",
            title="Executor exports",
            goal="Implement the executor and public exports.",
            suggested_paths=(
                "pkg/__init__.py",
                "pkg/executor.py",
                "tests/test_executor.py",
            ),
            test_focus=("executor acceptance",),
            test_commands=("python3 -m unittest tests.test_executor",),
            writable_paths=(
                "pkg/__init__.py",
                "pkg/executor.py",
                "tests/test_executor.py",
            ),
        )

        children = self.local_sdlc.split_stage_work_item(stage, parts=2)

        self.assertEqual(
            set(children[0].writable_paths),
            {"pkg/executor.py", "tests/test_executor.py"},
        )
        self.assertEqual(children[0].test_commands, ())
        self.assertEqual(children[-1].writable_paths, ("pkg/__init__.py",))
        self.assertEqual(children[-1].test_commands, stage.test_commands)

    def test_stage_split_keeps_single_test_with_same_package_code_dependencies(self):
        stage = self.local_sdlc.StageWorkItem(
            stage_id="S03",
            title="Checkpoint resume",
            goal="Implement checkpoint storage and integrate it with execution.",
            suggested_paths=(
                "pkg/__init__.py",
                "pkg/checkpoint.py",
                "pkg/executor.py",
                "tests/test_checkpoint.py",
                "README.md",
            ),
            test_focus=("checkpoint acceptance",),
            test_commands=("python3 -m unittest tests.test_checkpoint",),
            writable_paths=(
                "pkg/__init__.py",
                "pkg/checkpoint.py",
                "pkg/executor.py",
                "tests/test_checkpoint.py",
                "README.md",
            ),
        )

        children = self.local_sdlc.split_stage_work_item(stage, parts=2)

        self.assertEqual(
            set(children[0].writable_paths),
            {"pkg/checkpoint.py", "pkg/executor.py", "tests/test_checkpoint.py"},
        )
        self.assertEqual(
            set(children[-1].writable_paths),
            {"pkg/__init__.py", "README.md"},
        )
        self.assertEqual(children[-1].test_commands, stage.test_commands)

    def test_stage_recovery_expands_only_to_evidence_path_inside_parent_scope(self):
        stage = self.local_sdlc.StageWorkItem(
            stage_id="S02.2",
            title="Repository slice",
            goal="Make repository tests pass.",
            suggested_paths=("tests/test_repository.py",),
            test_focus=("repository tests",),
            writable_paths=("tests/test_repository.py",),
            repair_scope_paths=("pkg/repository.py", "tests/test_repository.py"),
        )
        summary = self.local_sdlc.StageRunSummary(
            stage_id=stage.stage_id,
            title=stage.title,
            status="failed",
            run_dir="run/s02.2",
            exit_code=1,
            repair_focus_paths=("pkg/repository.py", "outside.py"),
            failure_summary={"failure_type": "test_error"},
        )

        decision = self.local_sdlc.decide_stage_recovery(
            stage,
            summary,
            recovery_count=1,
            max_recoveries=3,
            previous_actions=("split_stage",),
        )

        self.assertEqual(decision.action, "expand_repair_scope")
        self.assertEqual(decision.additional_writable_paths, ("pkg/repository.py",))

    def test_stage_recovery_rejects_unchanged_root_cause_replay(self):
        stage = self.local_sdlc.StageWorkItem(
            stage_id="S02.2",
            title="Repository slice",
            goal="Make repository tests pass.",
            suggested_paths=("pkg/repository.py",),
            test_focus=("repository tests",),
            writable_paths=("pkg/repository.py",),
        )
        summary = self.local_sdlc.StageRunSummary(
            stage_id=stage.stage_id,
            title=stage.title,
            status="failed",
            run_dir="run/s02.2",
            exit_code=1,
            failure_summary={"failure_type": "test_error"},
        )

        decision = self.local_sdlc.decide_stage_recovery(
            stage,
            summary,
            recovery_count=2,
            max_recoveries=3,
            previous_actions=("split_stage", "root_cause_recovery"),
        )

        self.assertEqual(decision.action, "fail_closed")
        self.assertEqual(decision.reason_code, "no_novel_recovery")


if __name__ == "__main__":
    import unittest

    unittest.main()
