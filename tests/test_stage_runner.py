import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

from tests.helpers import LocalSDLCTestCase


class StageRunnerTests(LocalSDLCTestCase):
    def test_stage_summary_routes_on_terminal_failure_and_executable_focus(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = root / "run"
            run_dir.mkdir()
            stage = self.local_sdlc.StageWorkItem(
                stage_id="S02.2",
                title="executor slice",
                goal="implement executor",
                suggested_paths=("dagrunner/executor.py", "tests/test_executor.py"),
                test_focus=("executor tests pass",),
                writable_paths=("dagrunner/executor.py", "tests/test_executor.py"),
                repair_scope_paths=(
                    "dagrunner/__init__.py",
                    "dagrunner/executor.py",
                    "tests/test_executor.py",
                ),
            )
            (run_dir / "run.json").write_text(
                json.dumps(
                    {
                        "final_verdict": "patch_failed",
                        "final_failure_type": "format_repair_markdown_fence",
                        "failure_summary": {"failure_type": "missing_test_harness"},
                        "candidate_regressions": [
                            {
                                "failure_signature": (
                                    "ImportError: cannot import name 'Engine' from 'dagrunner' "
                                    "(<project>/dagrunner/__init__.py)"
                                )
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            summary = self.local_sdlc.read_stage_agent_manifest(stage, run_dir, 1, root)

        self.assertEqual(summary.failure_summary["failure_type"], "format_repair_markdown_fence")
        self.assertEqual(summary.failure_summary["acceptance_failure_type"], "missing_test_harness")
        self.assertIn("dagrunner/__init__.py", summary.repair_focus_paths)

    def test_run_stages_defaults_to_isolated_autonomous_recovery(self):
        args = self.local_sdlc.build_parser().parse_args(["run-stages", "task"])

        self.assertEqual(args.worktree_mode, "copy")
        self.assertTrue(args.autonomous_recovery)
        self.assertEqual(args.max_stage_recoveries, 3)

    def test_run_stages_parser_accepts_adaptive_rounds(self):
        args = self.local_sdlc.build_parser().parse_args(
            ["run-stages", "task", "--adaptive-rounds", "5", "--domain-modeling", "never"]
        )

        self.assertEqual(args.adaptive_rounds, 5)
        self.assertEqual(args.domain_modeling, "never")

    def test_stage_queue_synthesizes_sqlite_work_items(self):
        stages = self.local_sdlc.synthesize_stage_queue("Mini SQLite Engine with SQL parser and B+Tree")

        titles = [stage.title for stage in stages]
        self.assertIn("SQL lexer", titles)
        self.assertIn("B+Tree split operations", titles)
        self.assertTrue(any("minisqlite/storage/btree.py" in stage.suggested_paths for stage in stages))
        lexer_stage = next(stage for stage in stages if stage.title == "SQL lexer")
        self.assertEqual(self.local_sdlc.stage_test_paths(lexer_stage), ("tests/test_lexer.py",))
        manifest = self.local_sdlc.stage_work_item_manifest(lexer_stage)
        self.assertIn("required_observables", manifest)
        self.assertIn("writable_paths", manifest)
        self.assertIn("readonly_evidence_paths", manifest)
        self.assertIn("command:python3 -m unittest discover -s tests -p test_lexer.py", manifest["required_observables"])
        self.assertIn("tests/test_lexer.py", manifest["writable_paths"])

    def test_stage_agent_args_propagate_function_api_profiles(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root, "# Mini SQLite Engine\nSQL parser and B+Tree\n")
            run_dir = project / "run"
            args = self.local_sdlc.build_parser().parse_args(
                [
                    "run-stages",
                    "build mini sqlite",
                    "--project",
                    str(project),
                    "--skills-dir",
                    str(skills_dir),
                    "--api-profile",
                    "repair_artifact:max_tokens=32768,temperature=0,thinking=off",
                    "--model-profile",
                    "qwen-agent",
                    "--protocol-repair-rounds",
                    "3",
                ]
            )
            stage = self.local_sdlc.synthesize_stage_queue("Mini SQLite Engine")[0]

            stage_args = self.local_sdlc.build_stage_agent_args(args, stage, run_dir, [], [])

        self.assertEqual(
            stage_args.api_profile,
            ["repair_artifact:max_tokens=32768,temperature=0,thinking=off"],
        )
        self.assertEqual(stage_args.model_profile, "qwen-agent")
        self.assertEqual(stage_args.protocol_repair_rounds, 3)

    def test_final_integration_repair_prechecks_baseline_failure_score(self):
        with tempfile.TemporaryDirectory() as temp:
            project, skills_dir = self.make_agent_project(Path(temp), "# Project\n")
            (project / "app.py").write_text("VALUE = 0\n", encoding="utf-8")
            args = self.local_sdlc.build_parser().parse_args(
                [
                    "run-stages",
                    "repair final acceptance",
                    "--project",
                    str(project),
                    "--skills-dir",
                    str(skills_dir),
                    "--test-command",
                    f"{sys.executable} -c pass",
                ]
            )
            stage = self.local_sdlc.StageWorkItem(
                stage_id="S01",
                title="Core",
                goal="Implement core behavior.",
                suggested_paths=("app.py",),
                test_focus=("final",),
            )

            repair_args = self.local_sdlc.build_integration_repair_args(
                args,
                [stage],
                [],
                project / "run",
            )

        self.assertTrue(repair_args.precheck)

    def test_stage_work_item_policy_flows_to_agent_args(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root, "# Custom staged project\n")
            (project / "app.py").write_text("print('ok')\n", encoding="utf-8")
            (project / "tests").mkdir()
            (project / "tests" / "test_app.py").write_text("import unittest\n", encoding="utf-8")
            args = self.local_sdlc.build_parser().parse_args(
                [
                    "run-stages",
                    "custom stage",
                    "--project",
                    str(project),
                    "--skills-dir",
                    str(skills_dir),
                    "--api-profile",
                    "judge_review:max_tokens=2048",
                ]
            )
            stage = self.local_sdlc.StageWorkItem(
                stage_id="S10",
                title="Custom API stage",
                goal="Exercise custom stage metadata.",
                suggested_paths=("app.py",),
                test_focus=("custom smoke",),
                test_commands=("python3 app.py",),
                required_observables=("command:python3 app.py",),
                writable_paths=("app.py",),
                readonly_evidence_paths=("tests/test_app.py",),
                api_profile=("generate_artifact:max_tokens=4096",),
                max_rounds=7,
            )

            stage_args = self.local_sdlc.build_stage_agent_args(args, stage, project / "run", [], [])

        self.assertEqual(stage_args.include, ["app.py"])
        self.assertEqual(stage_args.context, ["tests/test_app.py"])
        self.assertEqual(stage_args.new_file, [])
        self.assertEqual(stage_args.require_path, ["app.py"])
        self.assertEqual(
            stage_args.api_profile,
            ["generate_artifact:max_tokens=4096", "judge_review:max_tokens=2048"],
        )
        self.assertEqual(stage_args.max_rounds, 7)
        self.assertIn("## Required Observables", stage_args.brief)
        self.assertIn("command:python3 app.py", stage_args.brief)
        self.assertIn("## Readonly Evidence Paths", stage_args.brief)
        self.assertIn("tests/test_app.py", stage_args.brief)

    def test_runtime_api_profile_overrides_same_stage_function(self):
        with tempfile.TemporaryDirectory() as temp:
            project, skills_dir = self.make_agent_project(Path(temp), "# Project\n")
            args = self.local_sdlc.build_parser().parse_args(
                [
                    "run-stages",
                    "task",
                    "--project",
                    str(project),
                    "--skills-dir",
                    str(skills_dir),
                    "--api-profile",
                    "failure_analysis:max_tokens=4096,temperature=0,thinking=off",
                ]
            )
            stage = self.local_sdlc.StageWorkItem(
                stage_id="S01",
                title="Stage",
                goal="Goal",
                suggested_paths=("app.py",),
                test_focus=("unit",),
                writable_paths=("app.py",),
                api_profile=("failure_analysis:max_tokens=8192,temperature=1,thinking=on",),
            )

            stage_args = self.local_sdlc.build_stage_agent_args(
                args, stage, project / "run", [], []
            )
            overrides = self.local_sdlc.build_function_overrides(stage_args)

        self.assertEqual(overrides["failure_analysis"].max_tokens, 4096)
        self.assertEqual(overrides["failure_analysis"].temperature, 0.0)
        self.assertTrue(overrides["failure_analysis"].disable_thinking)

    def test_stage_agent_args_pass_absolute_project_and_spec_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root, "# Mini SQLite Engine\nSQL parser and B+Tree\n")
            old_cwd = Path.cwd()
            try:
                os.chdir(root)
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "run-stages",
                        "build mini sqlite",
                        "--project",
                        "project",
                        "--skills-dir",
                        str(skills_dir),
                    ]
                )
                stage = self.local_sdlc.synthesize_stage_queue("Mini SQLite Engine")[0]

                stage_args = self.local_sdlc.build_stage_agent_args(args, stage, project / "run", [], [])
            finally:
                os.chdir(old_cwd)

        self.assertEqual(stage_args.project, project.resolve())
        self.assertEqual(stage_args.spec_file, (project / "SPEC.md").resolve())

    def test_stage_plan_command_outputs_json(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "SPEC.md").write_text("# Mini SQLite Engine\nSQL parser and B+Tree\n", encoding="utf-8")
            args = self.local_sdlc.build_parser().parse_args(
                [
                    "stage-plan",
                    "--project",
                    str(project),
                    "--format",
                    "json",
                ]
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = self.local_sdlc.command_stage_plan(args)

        payload = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertTrue(any(stage["title"] == "SQL lexer" for stage in payload["stages"]))
        lexer_stage = next(stage for stage in payload["stages"] if stage["title"] == "SQL lexer")
        self.assertIn("required_observables", lexer_stage)
        self.assertIn("writable_paths", lexer_stage)
        self.assertIn("readonly_evidence_paths", lexer_stage)

    def test_run_stages_dry_run_writes_queue_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root, "# Mini SQLite Engine\nSQL parser and B+Tree\n")
            run_dir = project / "run"
            args = self.local_sdlc.build_parser().parse_args(
                [
                    "run-stages",
                    "build mini sqlite",
                    "--project",
                    str(project),
                    "--skills-dir",
                    str(skills_dir),
                    "--from-stage",
                    "S01",
                    "--to-stage",
                    "S02",
                    "--dry-run",
                    "--run-dir",
                    str(run_dir),
                ]
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = self.local_sdlc.command_run_stages(args)
            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            queue_exists = (run_dir / "00-stage-queue.md").exists()

        self.assertEqual(result, 0)
        self.assertEqual(manifest["status"], "dry_run")
        self.assertEqual(manifest["stage_count"], 2)
        self.assertIn("required_observables", manifest["stages"][0])
        self.assertIn("writable_paths", manifest["stages"][0])
        self.assertTrue(queue_exists)

    def test_mini_sqlite_can_resume_from_s03_with_prior_stage_context(self):
        calls = []

        def fake_command_agent(stage_args):
            calls.append(stage_args)
            stage_args.run_dir.mkdir(parents=True, exist_ok=True)
            (stage_args.run_dir / "run.json").write_text(
                json.dumps({"api_calls": 0, "final_verdict": "approved"}),
                encoding="utf-8",
            )
            return 0

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root, "# Mini SQLite Engine\nSQL parser and B+Tree\n")
            for relative in (
                "minisqlite/errors.py",
                "minisqlite/result.py",
                "minisqlite/sql/lexer.py",
                "tests/test_core.py",
                "tests/test_lexer.py",
            ):
                path = project / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# prior stage output\n", encoding="utf-8")
            args = self.local_sdlc.build_parser().parse_args(
                [
                    "run-stages",
                    "resume mini sqlite",
                    "--project",
                    str(project),
                    "--skills-dir",
                    str(skills_dir),
                    "--from-stage",
                    "S03",
                    "--to-stage",
                    "S03",
                    "--run-dir",
                    str(project / "run"),
                ]
            )
            original_cli = self.local_sdlc.command_agent
            original = self.local_sdlc._stage_runner.command_agent
            self.local_sdlc.command_agent = fake_command_agent
            self.local_sdlc._stage_runner.command_agent = fake_command_agent
            try:
                result = self.local_sdlc.command_run_stages(args)
            finally:
                self.local_sdlc.command_agent = original_cli
                self.local_sdlc._stage_runner.command_agent = original

        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("minisqlite/sql/ast.py", calls[0].new_file)
        self.assertIn("minisqlite/errors.py", calls[0].context)
        self.assertIn("minisqlite/sql/lexer.py", calls[0].context)

    def test_run_stages_refuses_cancelled_run_before_stage_agent_call(self):
        calls = []

        def fake_command_agent(_args):
            calls.append(_args)
            return 0

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root, "# Mini SQLite Engine\nSQL parser and B+Tree\n")
            run_dir = project / "run"
            self.local_sdlc.request_cancel(run_dir, source="test", reason="stop_before_stage")
            args = self.local_sdlc.build_parser().parse_args(
                [
                    "run-stages",
                    "build mini sqlite",
                    "--project",
                    str(project),
                    "--skills-dir",
                    str(skills_dir),
                    "--from-stage",
                    "S01",
                    "--to-stage",
                    "S01",
                    "--apply",
                    "--run-dir",
                    str(run_dir),
                ]
            )

            original_command_agent = self.local_sdlc._stage_runner.command_agent
            self.local_sdlc._stage_runner.command_agent = fake_command_agent
            try:
                with self.assertRaises(self.local_sdlc.RunnerError) as caught:
                    self.local_sdlc.command_run_stages(args)
            finally:
                self.local_sdlc._stage_runner.command_agent = original_command_agent

        self.assertIn("cancelled", str(caught.exception))
        self.assertEqual(calls, [])
        self.assertEqual(self.local_sdlc.work_starts_after_cancel(run_dir), [])

    def test_run_stages_cancel_after_stage_prevents_final_command(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root, "# Mini SQLite Engine\n")
            run_dir = project / "run"
            marker = project / "must-not-run.txt"

            def fake_command_agent(stage_args):
                stage_args.run_dir.mkdir(parents=True, exist_ok=True)
                (stage_args.run_dir / "run.json").write_text(
                    json.dumps({"api_calls": 0, "final_verdict": "approved"}),
                    encoding="utf-8",
                )
                self.local_sdlc.request_cancel(run_dir, source="test", reason="before_final_command")
                return 0

            args = self.local_sdlc.build_parser().parse_args(
                [
                    "run-stages",
                    "cancel before final",
                    "--project",
                    str(project),
                    "--skills-dir",
                    str(skills_dir),
                    "--from-stage",
                    "S01",
                    "--to-stage",
                    "S01",
                    "--apply",
                    "--test-command",
                    f"{sys.executable} -c \"from pathlib import Path; Path('must-not-run.txt').write_text('bad')\"",
                    "--run-dir",
                    str(run_dir),
                ]
            )
            original_cli = self.local_sdlc.command_agent
            original = self.local_sdlc._stage_runner.command_agent
            self.local_sdlc.command_agent = fake_command_agent
            self.local_sdlc._stage_runner.command_agent = fake_command_agent
            try:
                with self.assertRaises(self.local_sdlc.RunnerError):
                    self.local_sdlc.command_run_stages(args)
            finally:
                self.local_sdlc.command_agent = original_cli
                self.local_sdlc._stage_runner.command_agent = original

        self.assertFalse(marker.exists())
        self.assertEqual(self.local_sdlc.work_starts_after_cancel(run_dir), [])

    def test_run_stages_propagates_child_approval_required_to_parent_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root, "# Mini SQLite Engine\n")
            run_dir = project / "run"

            def fake_command_agent(stage_args):
                stage_args.run_dir.mkdir(parents=True, exist_ok=True)
                decision = self.local_sdlc.action_safety_decision(
                    action="initial_test_command_1",
                    action_type="command",
                    risk_class="docker_control",
                    command="docker ps",
                )
                persisted = self.local_sdlc.authorize_safety_decision(stage_args.run_dir, decision)
                (stage_args.run_dir / "run.partial.json").write_text(
                    json.dumps(
                        {
                            "api_calls": 0,
                            "final_verdict": "approval_required",
                            "final_failure_type": "approval_required",
                            "pending_safety_decisions": [persisted],
                        }
                    ),
                    encoding="utf-8",
                )
                raise self.local_sdlc.RunnerError("approval required")

            args = self.local_sdlc.build_parser().parse_args(
                [
                    "run-stages",
                    "pause for safety approval",
                    "--project",
                    str(project),
                    "--skills-dir",
                    str(skills_dir),
                    "--from-stage",
                    "S01",
                    "--to-stage",
                    "S01",
                    "--apply",
                    "--run-dir",
                    str(run_dir),
                ]
            )
            original_cli = self.local_sdlc.command_agent
            original = self.local_sdlc._stage_runner.command_agent
            self.local_sdlc.command_agent = fake_command_agent
            self.local_sdlc._stage_runner.command_agent = fake_command_agent
            try:
                result = self.local_sdlc.command_run_stages(args)
            finally:
                self.local_sdlc.command_agent = original_cli
                self.local_sdlc._stage_runner.command_agent = original

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(result, 1)
        self.assertEqual(manifest["status"], "approval_required")
        self.assertEqual(manifest["completed_stages"][0]["final_verdict"], "approval_required")
        self.assertEqual(len(manifest["pending_safety_decisions"]), 1)
        self.assertEqual(manifest["pending_safety_decisions"][0]["stage_id"], "S01")
        self.assertEqual(
            manifest["pending_safety_decisions"][0]["run_dir"],
            str((run_dir / "s01-core-errors-and-result-objects").resolve()),
        )

    def test_run_stages_propagates_child_safety_blocked_to_parent_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root, "# Mini SQLite Engine\n")
            run_dir = project / "run"

            def fake_command_agent(stage_args):
                decision = self.local_sdlc.action_safety_decision(
                    "initial_test_command_1",
                    action_type="command",
                    risk_class="git_history_rewrite",
                    command="git reset --hard",
                )
                persisted = self.local_sdlc.authorize_safety_decision(stage_args.run_dir, decision)
                (stage_args.run_dir / "run.partial.json").write_text(
                    json.dumps(
                        {
                            "api_calls": 0,
                            "final_verdict": "safety_blocked",
                            "blocked_safety_decisions": [persisted],
                        }
                    ),
                    encoding="utf-8",
                )
                raise self.local_sdlc.RunnerError("safety blocked")

            args = self.local_sdlc.build_parser().parse_args(
                [
                    "run-stages",
                    "stop for safety block",
                    "--project",
                    str(project),
                    "--skills-dir",
                    str(skills_dir),
                    "--from-stage",
                    "S01",
                    "--to-stage",
                    "S01",
                    "--apply",
                    "--run-dir",
                    str(run_dir),
                ]
            )
            original_cli = self.local_sdlc.command_agent
            original = self.local_sdlc._stage_runner.command_agent
            self.local_sdlc.command_agent = fake_command_agent
            self.local_sdlc._stage_runner.command_agent = fake_command_agent
            try:
                result = self.local_sdlc.command_run_stages(args)
            finally:
                self.local_sdlc.command_agent = original_cli
                self.local_sdlc._stage_runner.command_agent = original

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(result, 1)
        self.assertEqual(manifest["status"], "safety_blocked")
        self.assertEqual(manifest["completed_stages"][0]["final_verdict"], "safety_blocked")
        self.assertEqual(manifest["blocked_safety_decisions"][0]["stage_id"], "S01")

    def test_run_stages_records_unhandled_child_runner_error_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root, "# Mini SQLite Engine\n")
            run_dir = project / "run"

            def fake_command_agent(_stage_args):
                _stage_args.run_dir.mkdir(parents=True, exist_ok=True)
                (_stage_args.run_dir / "run.partial.json").write_text(
                    json.dumps(
                        {
                            "api_calls": 2,
                            "documents": ["01-pm-control.md", "01-domain-contract.md"],
                            "changed_paths": [],
                            "required_paths": ["minisqlite/errors.py"],
                        }
                    ),
                    encoding="utf-8",
                )
                raise self.local_sdlc.RunnerError("invalid function profile")

            args = self.local_sdlc.build_parser().parse_args(
                [
                    "run-stages",
                    "fail closed on invalid child configuration",
                    "--project",
                    str(project),
                    "--skills-dir",
                    str(skills_dir),
                    "--from-stage",
                    "S01",
                    "--to-stage",
                    "S01",
                    "--apply",
                    "--run-dir",
                    str(run_dir),
                ]
            )
            original_cli = self.local_sdlc.command_agent
            original = self.local_sdlc._stage_runner.command_agent
            self.local_sdlc.command_agent = fake_command_agent
            self.local_sdlc._stage_runner.command_agent = fake_command_agent
            try:
                result = self.local_sdlc.command_run_stages(args)
            finally:
                self.local_sdlc.command_agent = original_cli
                self.local_sdlc._stage_runner.command_agent = original

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            child_dir = run_dir / "s01-core-errors-and-result-objects"
            child_manifest = json.loads((child_dir / "run.json").read_text(encoding="utf-8"))
            decisions = self.local_sdlc.read_autonomy_decisions(run_dir)
            error_document_exists = (child_dir / "00-runner-error.md").exists()

        self.assertEqual(result, 1)
        self.assertEqual(manifest["status"], "stage_failed")
        self.assertEqual(manifest["execution_attempt_count"], 1)
        self.assertEqual(manifest["api_calls"], 2)
        self.assertEqual(
            manifest["completed_stages"][0]["final_verdict"],
            "runner_configuration_error",
        )
        self.assertEqual(
            child_manifest["failure_summary"]["failure_type"],
            "runner_configuration_error",
        )
        self.assertEqual(child_manifest["api_calls"], 2)
        self.assertIn("01-domain-contract.md", child_manifest["documents"])
        self.assertIn("00-runner-error.md", child_manifest["documents"])
        self.assertTrue(error_document_exists)
        self.assertEqual([item["action"] for item in decisions], ["fail_closed"])

    def test_run_stages_executes_each_stage_as_agent_run(self):
        calls = []
        outputs = [
            json.dumps(
                {
                    "artifacts": [
                        {"type": "replace_file", "path": "minisqlite/errors.py", "content": "class MiniSQLiteError(Exception):\n    pass\n"},
                        {"type": "replace_file", "path": "minisqlite/result.py", "content": "class Result:\n    pass\n"},
                        {
                            "type": "replace_file",
                            "path": "tests/test_core.py",
                            "content": "import unittest\n\nclass TestCore(unittest.TestCase):\n    def test_smoke(self):\n        self.assertTrue(True)\n",
                        },
                    ]
                }
            ),
            json.dumps(
                {
                    "artifacts": [
                        {"type": "replace_file", "path": "minisqlite/sql/lexer.py", "content": "def tokenize(sql):\n    return sql.split()\n"},
                        {
                            "type": "replace_file",
                            "path": "tests/test_lexer.py",
                            "content": "import unittest\nfrom minisqlite.sql.lexer import tokenize\n\nclass TestLexer(unittest.TestCase):\n    def test_tokenize_smoke(self):\n        self.assertEqual(tokenize('select 1'), ['select', '1'])\n",
                        },
                    ]
                }
            ),
        ]

        class FakeClient:
            def __init__(self, config):
                self.config = config

            def complete(self, messages):
                calls.append(messages)
                return outputs[len(calls) - 1]

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root, "# Mini SQLite Engine\nSQL parser and B+Tree\n")
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "run-stages",
                        "build mini sqlite",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--from-stage",
                        "S01",
                        "--to-stage",
                        "S02",
                        "--apply",
                        "--skip-stage-pm",
                        "--judge-mode",
                        "command-only",
                        "--artifact-format",
                        "json",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_run_stages(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            lexer = (project / "minisqlite" / "sql" / "lexer.py").read_text(encoding="utf-8")
            s01_manifest_exists = (run_dir / "s01-core-errors-and-result-objects" / "run.json").exists()
            s02_manifest_exists = (run_dir / "s02-sql-lexer" / "run.json").exists()

        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(manifest["status"], "approved")
        self.assertEqual(manifest["completed_stage_count"], 2)
        self.assertEqual(manifest["api_calls"], 2)
        self.assertIn("def tokenize", lexer)
        self.assertIn("S01 Core errors and result objects", calls[1][1]["content"])
        self.assertIn("tests/test_core.py", calls[0][1]["content"])
        self.assertIn("tests/test_lexer.py", calls[1][1]["content"])
        self.assertIn("python3 -m py_compile minisqlite/errors.py minisqlite/result.py tests/test_core.py", calls[0][1]["content"])
        self.assertIn("python3 -m py_compile minisqlite/sql/lexer.py tests/test_lexer.py", calls[1][1]["content"])
        self.assertIn("python3 -m unittest discover -s tests -p test_core.py", calls[0][1]["content"])
        self.assertIn("python3 -m unittest discover -s tests -p test_lexer.py", calls[1][1]["content"])
        self.assertTrue(s01_manifest_exists)
        self.assertTrue(s02_manifest_exists)

    def test_run_stages_writes_recovery_plan_when_stage_fails(self):
        calls = []

        def fake_command_agent(stage_args):
            calls.append(stage_args)
            stage_args.run_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "api_calls": 1,
                "final_verdict": "rejected",
                "changed_paths": ["minisqlite/result.py"],
                "required_paths": [
                    "minisqlite/errors.py",
                    "minisqlite/result.py",
                    "tests/test_core.py",
                ],
                "failure_summary": {
                    "failure_type": "command_failed",
                    "document": "05-r01-command-01.md",
                },
            }
            (stage_args.run_dir / "run.json").write_text(json.dumps(payload), encoding="utf-8")
            return 1

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root, "# Mini SQLite Engine\nSQL parser and B+Tree\n")
            run_dir = project / "run"
            args = self.local_sdlc.build_parser().parse_args(
                [
                    "run-stages",
                    "build mini sqlite",
                    "--project",
                    str(project),
                    "--skills-dir",
                    str(skills_dir),
                    "--from-stage",
                    "S01",
                    "--to-stage",
                    "S03",
                    "--apply",
                    "--no-autonomous-recovery",
                    "--run-dir",
                    str(run_dir),
                ]
            )

            original_command_agent = self.local_sdlc.command_agent
            original_stage_command_agent = self.local_sdlc._stage_runner.command_agent
            self.local_sdlc.command_agent = fake_command_agent
            try:
                result = self.local_sdlc.command_run_stages(args)
            finally:
                self.local_sdlc.command_agent = original_command_agent
                self.local_sdlc._stage_runner.command_agent = original_stage_command_agent

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            recovery_doc = json.loads((run_dir / "01-stage-recovery-plan.json").read_text(encoding="utf-8"))
            memory_doc = json.loads((run_dir / "02-regression-memory.json").read_text(encoding="utf-8"))
            memory_store = json.loads(
                (project / ".sdlc-runner" / "regression-memory.json").read_text(encoding="utf-8")
            )

        self.assertEqual(result, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(manifest["status"], "stage_failed")
        self.assertEqual(manifest["stage_recovery_plan"], recovery_doc)
        self.assertEqual(recovery_doc["failed_stage_id"], "S01")
        self.assertEqual(recovery_doc["failure_type"], "command_failed")
        self.assertEqual(recovery_doc["recommended_resume"]["from_stage"], "S01")
        self.assertEqual(recovery_doc["recommended_resume"]["to_stage"], "S03")
        self.assertEqual(recovery_doc["completed_ok_stage_ids"], [])
        self.assertEqual(recovery_doc["remaining_stage_ids"], ["S01", "S02", "S03"])
        action = recovery_doc["next_required_action"]
        self.assertEqual(action["kind"], "repair_failed_stage")
        self.assertIn("minisqlite/result.py", action["writable_paths"])
        self.assertIn("tests/test_core.py", action["required_paths"])
        self.assertIn(
            "command:python3 -m py_compile minisqlite/errors.py minisqlite/result.py tests/test_core.py",
            action["required_observables"],
        )
        self.assertTrue(recovery_doc["retry_policy"]["do_not_skip_failed_stage"])
        self.assertEqual(memory_doc["record_count"], 1)
        self.assertEqual(memory_store["record_count"], 1)
        self.assertEqual(manifest["regression_memory"]["record_count"], 1)
        self.assertEqual(
            memory_doc["records"][0]["required_future_observables"],
            action["required_observables"],
        )

    def test_run_stages_parent_runtime_recovers_without_external_intervention(self):
        calls = []

        def fake_command_agent(stage_args):
            calls.append(stage_args)
            stage_args.run_dir.mkdir(parents=True, exist_ok=True)
            if len(calls) == 1:
                payload = {
                    "api_calls": 1,
                    "final_verdict": "test_failed",
                    "failure_summary": {"failure_type": "command_failed"},
                    "functional_rounds_used": 3,
                    "protocol_rounds_used": 2,
                    "adaptive_rounds_used": 1,
                    "root_cause_patch_rounds_used": 1,
                }
                exit_code = 1
            else:
                payload = {"api_calls": 1, "final_verdict": "approved"}
                exit_code = 0
            (stage_args.run_dir / "run.json").write_text(json.dumps(payload), encoding="utf-8")
            return exit_code

        spec = """
# Generic Project
## Implementation Stages
```json
{
  "stage_plan_schema": 1,
  "stages": [{
    "stage_id": "S01",
    "title": "Core behavior",
    "goal": "Implement one bounded behavior.",
    "writable_paths": ["src/core.py"],
    "readonly_evidence_paths": [],
    "test_commands": [],
    "required_observables": []
  }]
}
```
"""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root, spec)
            run_dir = project / "run"
            args = self.local_sdlc.build_parser().parse_args(
                [
                    "run-stages",
                    "build generic project",
                    "--project",
                    str(project),
                    "--skills-dir",
                    str(skills_dir),
                    "--run-dir",
                    str(run_dir),
                ]
            )
            original_cli = self.local_sdlc.command_agent
            original = self.local_sdlc._stage_runner.command_agent
            self.local_sdlc.command_agent = fake_command_agent
            self.local_sdlc._stage_runner.command_agent = fake_command_agent
            try:
                result = self.local_sdlc.command_run_stages(args)
            finally:
                self.local_sdlc.command_agent = original_cli
                self.local_sdlc._stage_runner.command_agent = original
            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            actions = [
                item["action"]
                for item in self.local_sdlc.read_autonomy_decisions(run_dir)
            ]

        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1].resume, calls[0].run_dir)
        self.assertTrue(calls[1].small_patch)
        self.assertTrue(calls[1].skip_pm)
        self.assertEqual(calls[1].max_rounds, calls[0].max_rounds + 3)
        self.assertEqual(calls[1].protocol_repair_rounds, calls[0].protocol_repair_rounds + 2)
        self.assertEqual(calls[1].adaptive_rounds, calls[0].adaptive_rounds + 1)
        self.assertEqual(calls[1].root_cause_patch_rounds, calls[0].root_cause_patch_rounds + 1)
        self.assertEqual(manifest["status"], "approved")
        self.assertTrue(manifest["autonomy"]["zero_unauthorized_external_interventions"])
        self.assertIn("root_cause_recovery", actions)
        self.assertIn("complete", actions)

    def test_cli_parent_runtime_restarts_a_persistently_stalled_goal(self):
        calls = []

        def fake_stage_runner(run_args):
            calls.append(argparse.Namespace(**vars(run_args)))
            run_dir = Path(run_args.run_dir)
            run_dir.mkdir(parents=True, exist_ok=True)
            run_args.completed_run_dir = run_dir
            if len(calls) == 1:
                (run_dir / "stall.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "status": "STALLED",
                            "reason": "no observable progress",
                            "scope_run_dir": str(run_dir.resolve()),
                        }
                    ),
                    encoding="utf-8",
                )
                payload = {
                    "status": "stalled",
                    "stages": [{"stage_id": "S02"}],
                    "child_stalls": [{"stage_id": "S02"}],
                }
                result = 1
            else:
                payload = {"status": "approved"}
                result = 0
            (run_dir / "run.json").write_text(json.dumps(payload), encoding="utf-8")
            return result

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root, "# Generic Project\n")
            source = project / "run"
            args = self.local_sdlc.build_parser().parse_args(
                [
                    "run-stages",
                    "build generic project",
                    "--project",
                    str(project),
                    "--skills-dir",
                    str(skills_dir),
                    "--run-dir",
                    str(source),
                ]
            )
            original = self.local_sdlc._stage_runner.command_run_stages
            self.local_sdlc._stage_runner.command_run_stages = fake_stage_runner
            try:
                result = self.local_sdlc.command_run_stages(args)
            finally:
                self.local_sdlc._stage_runner.command_run_stages = original
            decisions = self.local_sdlc.read_autonomy_decisions(source)

        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1].resume_run, source.resolve())
        self.assertEqual(calls[1].from_stage, "S02")
        self.assertTrue(Path(calls[1].recovery_plan).is_absolute())
        self.assertIn("resume", [item["action"] for item in decisions])

    def test_run_stages_protocol_failure_forces_format_repair(self):
        calls = []

        def fake_command_agent(stage_args):
            calls.append(stage_args)
            stage_args.run_dir.mkdir(parents=True, exist_ok=True)
            payload = (
                {
                    "api_calls": 1,
                    "final_verdict": "patch_failed",
                    "failure_summary": {
                        "failure_type": "format_repair_malformed_search_replace"
                    },
                }
                if len(calls) == 1
                else {"api_calls": 1, "final_verdict": "approved"}
            )
            (stage_args.run_dir / "run.json").write_text(json.dumps(payload), encoding="utf-8")
            return 1 if len(calls) == 1 else 0

        spec = """
# Generic Project
## Implementation Stages
```json
{"stage_plan_schema":1,"stages":[{"stage_id":"S01","title":"One file","goal":"Write it.","writable_paths":["app.py"],"readonly_evidence_paths":[],"test_commands":[],"required_observables":[]}]}
```
"""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root, spec)
            run_dir = project / "run"
            args = self.local_sdlc.build_parser().parse_args(
                [
                    "run-stages",
                    "build generic project",
                    "--project",
                    str(project),
                    "--skills-dir",
                    str(skills_dir),
                    "--run-dir",
                    str(run_dir),
                ]
            )
            original_cli = self.local_sdlc.command_agent
            original = self.local_sdlc._stage_runner.command_agent
            self.local_sdlc.command_agent = fake_command_agent
            self.local_sdlc._stage_runner.command_agent = fake_command_agent
            try:
                result = self.local_sdlc.command_run_stages(args)
            finally:
                self.local_sdlc.command_agent = original_cli
                self.local_sdlc._stage_runner.command_agent = original
            actions = [
                item["action"]
                for item in self.local_sdlc.read_autonomy_decisions(run_dir)
            ]

        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1].artifact_format, "legacy")
        self.assertIn("format_repair", actions)

    def test_run_stages_does_not_complete_with_unverified_acceptance(self):
        def fake_command_agent(stage_args):
            stage_args.run_dir.mkdir(parents=True, exist_ok=True)
            target = stage_args.project / "app.py"
            target.write_text("print('ok')\n", encoding="utf-8")
            (stage_args.run_dir / "run.json").write_text(
                json.dumps({"api_calls": 0, "final_verdict": "approved"}),
                encoding="utf-8",
            )
            return 0

        spec = """
# Generic Project
## Acceptance Criteria
- A user can observe the complete domain result.
## Implementation Stages
```json
{"stage_plan_schema":1,"stages":[{"stage_id":"S01","title":"One file","goal":"Write it.","writable_paths":["app.py"],"readonly_evidence_paths":[],"test_commands":[],"required_observables":[]}]}
```
"""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root, spec)
            run_dir = project / "run"
            args = self.local_sdlc.build_parser().parse_args(
                [
                    "run-stages",
                    "build generic project",
                    "--project",
                    str(project),
                    "--skills-dir",
                    str(skills_dir),
                    "--apply",
                    "--final-repair-rounds",
                    "0",
                    "--worktree-mode",
                    "off",
                    "--run-dir",
                    str(run_dir),
                ]
            )
            original_cli = self.local_sdlc.command_agent
            original = self.local_sdlc._stage_runner.command_agent
            self.local_sdlc.command_agent = fake_command_agent
            self.local_sdlc._stage_runner.command_agent = fake_command_agent
            try:
                result = self.local_sdlc.command_run_stages(args)
            finally:
                self.local_sdlc.command_agent = original_cli
                self.local_sdlc._stage_runner.command_agent = original
            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(result, 1)
        self.assertEqual(manifest["status"], "acceptance_failed")
        self.assertEqual(manifest["acceptance_matrix"][0]["status"], "unverified")
        self.assertFalse(manifest["completion_gate"]["completed"])

    def test_run_stages_uses_stage_tests_before_final_gate(self):
        calls = []
        outputs = [
            json.dumps(
                {
                    "artifacts": [
                        {"type": "replace_file", "path": "minisqlite/errors.py", "content": "class MiniSQLiteError(Exception):\n    pass\n"},
                        {"type": "replace_file", "path": "minisqlite/result.py", "content": "class Result:\n    pass\n"},
                        {
                            "type": "replace_file",
                            "path": "tests/test_core.py",
                            "content": "import unittest\n\nclass TestCore(unittest.TestCase):\n    def test_smoke(self):\n        self.assertTrue(True)\n",
                        },
                    ]
                }
            ),
        ]

        class FakeClient:
            def __init__(self, config):
                self.config = config

            def complete(self, messages):
                calls.append(messages)
                return outputs[len(calls) - 1]

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root, "# Mini SQLite Engine\nSQL parser and B+Tree\n")
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "run-stages",
                        "build mini sqlite",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--from-stage",
                        "S01",
                        "--to-stage",
                        "S01",
                        "--apply",
                        "--skip-stage-pm",
                        "--judge-mode",
                        "command-only",
                        "--artifact-format",
                        "json",
                        "--test-command",
                        f"{sys.executable} -m unittest discover -s tests",
                        "--final-repair-rounds",
                        "0",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_run_stages(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            stage_command = (run_dir / "s01-core-errors-and-result-objects" / "05-r01-command-01.md").read_text(encoding="utf-8")
            stage_unittest = (run_dir / "s01-core-errors-and-result-objects" / "05-r01-command-02.md").read_text(encoding="utf-8")
            final_command = (run_dir / "99-final-command-01.md").read_text(encoding="utf-8")

        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("python3 -m py_compile minisqlite/errors.py minisqlite/result.py tests/test_core.py", stage_command)
        self.assertIn("unittest discover -s tests -p test_core.py", stage_unittest)
        self.assertIn("unittest discover", final_command)
        self.assertEqual(manifest["status"], "approved")
        self.assertEqual(manifest["final_test_commands"], [f"{sys.executable} -m unittest discover -s tests"])

    def test_stage_plan_resolves_relative_spec_file_against_project(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "project"
            project.mkdir()
            (project / "SPEC.md").write_text("# Project SPEC\nMini SQLite Engine\n", encoding="utf-8")
            skills_dir = root / "skills"
            skills_dir.mkdir()

            args = self.local_sdlc.build_parser().parse_args(
                [
                    "stage-plan",
                    "--project",
                    str(project),
                    "--skills-dir",
                    str(skills_dir),
                    "--spec-file",
                    "SPEC.md",
                    "--format",
                    "json",
                ]
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = self.local_sdlc.command_stage_plan(args)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["stages"][0]["title"], "Core errors and result objects")
