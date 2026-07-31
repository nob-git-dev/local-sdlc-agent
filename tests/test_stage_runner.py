import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

from tests.helpers import LocalSDLCTestCase


class StageRunnerTests(LocalSDLCTestCase):
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
            ["judge_review:max_tokens=2048", "generate_artifact:max_tokens=4096"],
        )
        self.assertEqual(stage_args.max_rounds, 7)
        self.assertIn("## Required Observables", stage_args.brief)
        self.assertIn("command:python3 app.py", stage_args.brief)
        self.assertIn("## Readonly Evidence Paths", stage_args.brief)
        self.assertIn("tests/test_app.py", stage_args.brief)

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
