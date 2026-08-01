import importlib
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from tests.helpers import ENTRYPOINT_PATH, ROOT, LocalSDLCTestCase, product_name_pattern


class LocalSDLCTest(LocalSDLCTestCase):
    def test_parse_front_matter(self):
        metadata, body = self.local_sdlc.parse_front_matter(
            "---\nname: spec\ndescription: Example\n---\n# Body\n"
        )
        self.assertEqual(metadata["name"], "spec")
        self.assertEqual(metadata["description"], "Example")
        self.assertEqual(body, "# Body\n")

    def test_load_skills_reads_front_matter(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            skills_dir = Path(temp)
            skill_dir = skills_dir / "spec"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: spec\ndescription: 仕様\n---\n# Spec Skill\n",
                encoding="utf-8",
            )

            skills = self.local_sdlc.load_skills(skills_dir)

        self.assertIn("spec", skills)
        self.assertEqual(skills["spec"].description, "仕様")
        self.assertIn("# Spec Skill", skills["spec"].body)

    def test_dangerous_command_reason_blocks_destructive_commands(self):
        reason = self.local_sdlc.dangerous_command_reason("DELETE FROM users")
        self.assertIsNotNone(reason)
        self.assertIn("DELETE", reason)

        allowed = self.local_sdlc.dangerous_command_reason("DELETE FROM users WHERE id = 1")
        self.assertIsNone(allowed)

    def test_command_failure_score_counts_unittest_summary(self):
        doc = self.local_sdlc.command_result_document(
            "python3 -m unittest",
            1,
            "",
            "FAILED (failures=1, errors=2)\n",
            0.01,
        )

        score = self.local_sdlc.command_failure_score([("Command result", doc)])

        self.assertEqual(score, 3)

    def test_command_failure_score_counts_error_fail_markers(self):
        doc = self.local_sdlc.command_result_document(
            "python3 -m unittest",
            1,
            "",
            "ERROR: test_a\nFAIL: test_b\n",
            0.01,
        )

        score = self.local_sdlc.command_failure_score([("Command result", doc)])

        self.assertEqual(score, 2)

    def test_command_failure_signature_normalizes_paths_and_lines(self):
        doc1 = self.local_sdlc.command_result_document(
            "python3 -m unittest",
            1,
            "",
            'FAIL: test_root\n  File "/tmp/a/project/tests/test_app.py", line 10, in test_root\nAssertionError: Row 195 not found\nFAILED (failures=1)\n',
            0.01,
        )
        doc2 = self.local_sdlc.command_result_document(
            "python3 -m unittest",
            1,
            "",
            'FAIL: test_root\n  File "/tmp/b/project/tests/test_app.py", line 99, in test_root\nAssertionError: Row 195 not found\nFAILED (failures=1)\n',
            0.01,
        )

        sig1 = self.local_sdlc.command_failure_signature([("round1", doc1)])
        sig2 = self.local_sdlc.command_failure_signature([("round2", doc2)])

        self.assertEqual(sig1, sig2)
        self.assertIn("Row 195 not found", sig1 or "")

    def test_command_failure_family_signature_ignores_assertion_payload_drift(self):
        doc1 = self.local_sdlc.command_result_document(
            "python3 -m unittest discover -s tests -p test_lexer.py",
            1,
            "",
            textwrap.dedent(
                """
                FAIL: test_mixed_case_keywords (test_lexer.TestLexerEdgeCases.test_mixed_case_keywords)
                AssertionError: 'primary' not found in ['create', 'table', 'integer', 'PRIMARY', 'key']
                FAIL: test_delete_statement (test_lexer.TestLexerFullStatements.test_delete_statement)
                AssertionError: <TokenType.OPERATOR: 6> not found
                FAILED (failures=2)
                """
            ),
            0.01,
        )
        doc2 = self.local_sdlc.command_result_document(
            "python3 -m unittest discover -s tests -p test_lexer.py",
            1,
            "",
            textwrap.dedent(
                """
                FAIL: test_mixed_case_keywords (test_lexer.TestLexerEdgeCases.test_mixed_case_keywords)
                AssertionError: 'create' not found in ['CREATE', 'TABLE', 'INTEGER', 'PRIMARY', 'KEY']
                FAIL: test_delete_statement (test_lexer.TestLexerFullStatements.test_delete_statement)
                AssertionError: <TokenType.OPERATOR: 6> not found
                FAILED (failures=2)
                """
            ),
            0.01,
        )

        sig1 = self.local_sdlc.command_failure_family_signature([("round1", doc1)])
        sig2 = self.local_sdlc.command_failure_family_signature([("round2", doc2)])

        self.assertEqual(sig1, sig2)
        self.assertIn("test_lexer.TestLexerEdgeCases.test_mixed_case_keywords", sig1 or "")
        self.assertNotIn("'primary' not found", sig1 or "")

    def test_make_run_dir_avoids_double_project_prefix_for_cwd_relative_project_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "benchmarks" / "case"
            project.mkdir(parents=True)
            old_cwd = Path.cwd()
            try:
                os.chdir(root)
                run_dir = self.local_sdlc.make_run_dir(
                    project,
                    Path("benchmarks/case/.sdlc-runner/runs/full"),
                )
            finally:
                os.chdir(old_cwd)

        self.assertEqual(run_dir, project / ".sdlc-runner" / "runs" / "full")

    def test_agent_parser_accepts_adaptive_rounds(self):
        args = self.local_sdlc.build_parser().parse_args(
            ["agent", "task", "--include", "app.py", "--adaptive-rounds", "4"]
        )

        self.assertEqual(args.adaptive_rounds, 4)
        self.assertEqual(args.domain_modeling, "auto")
        self.assertEqual(args.domain_skill, "ddd")

    def test_run_skill_call_forwards_stream_parameters(self):
        class FakeClient:
            def __init__(self):
                self.kwargs = None

            def complete(self, messages, agent_level="default", call_function="default", stream_output_path=None, stream_callback=None, stream_guard=None):
                self.kwargs = {
                    "agent_level": agent_level,
                    "call_function": call_function,
                    "stream_output_path": stream_output_path,
                    "stream_callback": stream_callback,
                    "stream_guard": stream_guard,
                    "message_count": len(messages),
                }
                return "ok"

        callback = object()
        guard = object()
        stream_path = Path("/tmp/partial.md")
        client = FakeClient()

        result = self.local_sdlc.run_skill_call(
            client,
            self.local_sdlc.Skill("sdlc", "desc", Path("/tmp/SKILL.md"), "body", {}),
            spec="# SPEC",
            instruction="do it",
            agent_level="judge",
            stream_output_path=stream_path,
            stream_callback=callback,
            stream_guard=guard,
            call_function="judge_review",
        )

        self.assertEqual(result, "ok")
        self.assertEqual(client.kwargs["agent_level"], "judge")
        self.assertEqual(client.kwargs["call_function"], "judge_review")
        self.assertEqual(client.kwargs["stream_output_path"], stream_path)
        self.assertIs(client.kwargs["stream_callback"], callback)
        self.assertIs(client.kwargs["stream_guard"], guard)

    def test_unittest_timeout_diagnostic_identifies_hanging_method(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            tests_dir = project / "tests"
            tests_dir.mkdir()
            (tests_dir / "__init__.py").write_text("", encoding="utf-8")
            (tests_dir / "test_lexer.py").write_text(
                textwrap.dedent(
                    """
                    import time
                    import unittest

                    class TestLexer(unittest.TestCase):
                        def test_fast(self):
                            self.assertTrue(True)

                        def test_hangs(self):
                            time.sleep(2)
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            diagnostic = self.local_sdlc.unittest_timeout_diagnostic(
                project,
                "python3 -m unittest discover -s tests -p test_lexer.py",
                timeout=0.2,
            )

        self.assertIn("Timeout Localization", diagnostic)
        self.assertIn("tests.test_lexer.TestLexer.test_fast: PASS", diagnostic)
        self.assertIn("tests.test_lexer.TestLexer.test_hangs: TIMEOUT", diagnostic)

    def test_skill_messages_put_skill_body_in_system_role(self):
        skill = self.local_sdlc.Skill(
            name="spec",
            description="Example skill",
            path=Path("skills/spec/SKILL.md"),
            body="# Skill Body\nDo spec work.",
            metadata={},
        )

        messages = self.local_sdlc.skill_messages(
            skill=skill,
            spec="# SPEC\nAcceptance",
            instruction="Run the phase.",
            agent_level="pm",
        )

        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("# Skill Body", messages[0]["content"])
        self.assertIn("PM-level agent", messages[0]["content"])
        self.assertIn("Formal reasoning contract", messages[0]["content"])
        self.assertIn("O = F(role, system_prompt, D, I)", messages[0]["content"])
        self.assertIn("Graph model", messages[0]["content"])
        self.assertIn("P* and C* and G* and E* |- A or V", messages[0]["content"])
        self.assertIn("Role invariant", messages[0]["content"])
        self.assertIn("standalone local SDLC development agent", messages[0]["content"])
        self.assertNotRegex(messages[0]["content"], product_name_pattern())
        self.assertEqual(messages[1]["role"], "user")
        self.assertIn("# SPEC", messages[1]["content"])
        self.assertIn("Proposition discipline", messages[1]["content"])
        self.assertIn("Graph discipline", messages[1]["content"])
        self.assertNotIn("# Skill Body", messages[1]["content"])

    def test_skill_system_prompt_uses_neutral_source_asset_verbatim(self):
        skill = self.local_sdlc.Skill(
            name="portable",
            description="A portable SDLC skill.",
            path=Path("/tmp/local-agent/skills/portable/SKILL.md"),
            body="Read the project instructions and follow the SDLC contract.",
            metadata={},
        )

        prompt = self.local_sdlc.skill_system_prompt(skill, "pm")

        self.assertNotRegex(prompt, product_name_pattern())
        self.assertIn(skill.description, prompt)
        self.assertIn(skill.body, prompt)
        self.assertIn("Source: bundled/portable/SKILL.md", prompt)
        self.assertNotIn(str(skill.path), prompt)
        self.assertFalse(hasattr(self.local_sdlc, "portable_prompt_asset_text"))

    def test_bundled_runtime_system_prompts_are_product_neutral(self):
        skills = self.local_sdlc.load_skills(ROOT / "sdlc-skills" / "skills")
        supervisor = self.local_sdlc.load_prompt_asset(
            ROOT / "sdlc-skills" / "agents" / "supervisor.md",
            "supervisor",
        )

        for skill in [*skills.values(), supervisor]:
            with self.subTest(skill=skill.name):
                prompt = self.local_sdlc.skill_system_prompt(
                    skill,
                    self.local_sdlc.default_agent_level(skill.name),
                )
                self.assertNotRegex(prompt, product_name_pattern())

    def test_repository_text_assets_are_product_neutral(self):
        text_suffixes = {
            ".cfg",
            ".css",
            ".html",
            ".ini",
            ".js",
            ".json",
            ".md",
            ".py",
            ".rst",
            ".sh",
            ".txt",
            ".toml",
            ".xml",
            ".yaml",
            ".yml",
        }
        ignored_parts = {".git", ".sdlc-runner", "__pycache__"}
        violations: list[str] = []

        for path in ROOT.rglob("*"):
            if not path.is_file() or ignored_parts.intersection(path.parts):
                continue
            if path.suffix.lower() not in text_suffixes and path.name != ".gitignore":
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if re.search(product_name_pattern(), text):
                violations.append(str(path.relative_to(ROOT)))

        self.assertEqual(violations, [], "product-specific names found in: " + ", ".join(violations))

    def test_document_exchange_prompt_defines_short_propositions(self):
        prompt = self.local_sdlc.document_exchange_prompt(
            spec="# SPEC\nMust pass tests.",
            instruction="Review the result.",
            output_contract="Return review.",
        )

        self.assertIn("Pn: supplied premise", prompt)
        self.assertIn("Cn: fixed constraint", prompt)
        self.assertIn("An: proposed action or artifact", prompt)
        self.assertIn("Vn: supported verdict", prompt)
        self.assertIn("Do not create propositions from hidden memory", prompt)
        self.assertIn("supports(Pn or En, Gn or Vn)", prompt)
        self.assertIn("satisfies(An, Gn)", prompt)

    def test_parse_supervisor_steps(self):
        self.assertEqual(
            self.local_sdlc.parse_supervisor_steps("pm,coder,judge"),
            ["pm", "coder", "judge"],
        )
        self.assertEqual(
            self.local_sdlc.parse_supervisor_steps("all"),
            ["pm", "coder", "judge"],
        )
        with self.assertRaises(self.local_sdlc.RunnerError):
            self.local_sdlc.parse_supervisor_steps("pm,unknown")

    def test_recommended_sdlc_phases_force_security_for_production_db(self):
        route = self.local_sdlc.recommended_sdlc_phases(
            "本番DBのカラムを追加してデプロイしたい",
            "",
        )

        self.assertEqual(route.task_type, "infrastructure")
        self.assertIn("production", route.danger_signals)
        self.assertIn("database", route.danger_signals)
        self.assertEqual(route.phases[0], "spec")
        self.assertIn("architect", route.phases)
        self.assertIn("security", route.phases)
        self.assertIn("deploy", route.phases)

    def test_recommended_sdlc_phases_includes_ddd_for_new_feature(self):
        route = self.local_sdlc.recommended_sdlc_phases(
            "新しい検索機能を実装して",
            "",
        )

        self.assertEqual(
            route.phases,
            ("spec", "ddd", "architect", "tdd", "review"),
        )
        self.assertIn("domain_modeling=yes", route.reason)

    def test_recommended_sdlc_phases_includes_ddd_for_interactive_ui(self):
        route = self.local_sdlc.recommended_sdlc_phases(
            "HTMLで操作できるゲーム画面を作って",
            "",
        )

        self.assertEqual(
            route.phases,
            ("spec", "ddd", "tdd", "ui", "review"),
        )

    def test_ddd_phase_instruction_defines_verification_contract(self):
        route = self.local_sdlc.recommended_sdlc_phases(
            "ブラウザで遊べるテトリスを作って",
            "",
        )

        instruction = self.local_sdlc.phase_instruction("ddd", "ブラウザで遊べるテトリスを作って", route)

        self.assertIn("DDD phase contract", instruction)
        self.assertIn("Verification Proposition Contract", instruction)
        self.assertIn("relation", instruction)
        self.assertIn("fail_owner", instruction)
        self.assertIn("Do not implement product code", instruction)
        self.assertNotRegex(instruction, product_name_pattern())

    def test_redis_smoke_mode_overrides_auto_detection(self):
        self.assertTrue(self.local_sdlc.is_redis_request("partial protocol work", ["resp.py"]))
        self.assertTrue(self.local_sdlc.should_run_redis_smoke("auto", "partial protocol work", ["resp.py"]))
        self.assertFalse(self.local_sdlc.should_run_redis_smoke("auto", "plain task", ["server.py"]))
        self.assertFalse(self.local_sdlc.should_run_redis_smoke("never", "redis task", ["resp.py"]))
        self.assertTrue(self.local_sdlc.should_run_redis_smoke("always", "plain task", []))

    def test_supervisor_executes_dynamic_sdlc_phases_as_separate_calls(self):
        calls = []

        class FakeClient:
            def __init__(self, config):
                self.config = config

            def complete(self, messages):
                calls.append(messages)
                return f"output-{len(calls)}"

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            skills_dir = project / "skills"
            agents_dir = project / "agents"
            agents_dir.mkdir()
            (agents_dir / "supervisor.md").write_text(
                "---\nname: supervisor\ndescription: route\n---\n# supervisor\n",
                encoding="utf-8",
            )
            for name in ("spec", "ddd", "architect", "tdd", "review"):
                skill_dir = skills_dir / name
                skill_dir.mkdir(parents=True)
                (skill_dir / "SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: {name} skill\n---\n# {name}\n",
                    encoding="utf-8",
                )
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "supervisor",
                        "新しい検索機能を実装して",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--agents-dir",
                        str(agents_dir),
                        "--execute",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_supervisor(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 6)
        self.assertIn("# supervisor", calls[0][0]["content"])
        self.assertIn("# spec", calls[1][0]["content"])
        self.assertIn("# ddd", calls[2][0]["content"])
        self.assertIn("# architect", calls[3][0]["content"])
        self.assertIn("# tdd", calls[4][0]["content"])
        self.assertIn("# review", calls[5][0]["content"])
        self.assertIn("PM-level agent", calls[2][0]["content"])
        self.assertIn("coder-level agent", calls[4][0]["content"])
        self.assertIn("judge-level agent", calls[5][0]["content"])
        self.assertEqual(
            manifest["recommended_phases"],
            ["spec", "ddd", "architect", "tdd", "review"],
        )
        self.assertEqual(
            manifest["completed_phases"],
            ["spec", "ddd", "architect", "tdd", "review"],
        )
        self.assertEqual(manifest["api_calls"], 6)

    def test_supervise_runs_pm_coder_judge_as_separate_calls(self):
        calls = []

        class FakeClient:
            def __init__(self, config):
                self.config = config

            def complete(self, messages):
                calls.append(messages)
                return f"output-{len(calls)}"

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            skills_dir = project / "skills"
            for name in ("sdlc", "tdd", "review"):
                skill_dir = skills_dir / name
                skill_dir.mkdir(parents=True)
                (skill_dir / "SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: {name} skill\n---\n# {name}\n",
                    encoding="utf-8",
                )
            (project / "SPEC.md").write_text("# SPEC\n", encoding="utf-8")
            (project / "app.py").write_text("print('ok')\n", encoding="utf-8")

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "supervise",
                        "test brief",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--steps",
                        "pm,coder,judge",
                        "--include",
                        "app.py",
                        "--run-dir",
                        str(project / "run"),
                    ]
                )
                result = self.local_sdlc.command_supervise(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 3)
        self.assertIn("PM-level agent", calls[0][0]["content"])
        self.assertIn("coder-level agent", calls[1][0]["content"])
        self.assertIn("judge-level agent", calls[2][0]["content"])
        self.assertIn("PM control document", calls[1][1]["content"])
        self.assertIn("Coder output", calls[2][1]["content"])

    def test_supervise_requires_file_context_for_coder(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            skills_dir = project / "skills"
            skill_dir = skills_dir / "tdd"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: tdd\ndescription: coder\n---\n# tdd\n",
                encoding="utf-8",
            )
            (project / "SPEC.md").write_text("# SPEC\n", encoding="utf-8")

            args = self.local_sdlc.build_parser().parse_args(
                [
                    "supervise",
                    "test brief",
                    "--project",
                    str(project),
                    "--skills-dir",
                    str(skills_dir),
                    "--steps",
                    "coder",
                ]
            )

            with self.assertRaises(self.local_sdlc.RunnerError):
                self.local_sdlc.command_supervise(args)

    def test_supervise_allows_new_file_without_existing_context(self):
        calls = []

        class FakeClient:
            def __init__(self, config):
                self.config = config

            def complete(self, messages):
                calls.append(messages)
                return "diff --git a/tetris.html b/tetris.html\n"

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            skills_dir = project / "skills"
            skill_dir = skills_dir / "tdd"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: tdd\ndescription: coder\n---\n# tdd\n",
                encoding="utf-8",
            )
            (project / "SPEC.md").write_text("# SPEC\n", encoding="utf-8")

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "supervise",
                        "build tetris",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--steps",
                        "coder",
                        "--new-file",
                        "tetris.html",
                    ]
                )
                result = self.local_sdlc.command_supervise(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("Create these new project-relative file(s): tetris.html", calls[0][1]["content"])

    def test_supervise_auto_fix_loops_until_judge_approval(self):
        calls = []
        outputs = [
            "coder round 1",
            "判定: 修正依頼\nMust fix the CDN choices.",
            "coder round 2 fixed",
            "判定: 承認\nAll checks passed.",
        ]

        class FakeClient:
            def __init__(self, config):
                self.config = config

            def complete(self, messages):
                calls.append(messages)
                return outputs[len(calls) - 1]

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            skills_dir = project / "skills"
            for name in ("tdd", "review"):
                skill_dir = skills_dir / name
                skill_dir.mkdir(parents=True)
                (skill_dir / "SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: {name}\n---\n# {name}\n",
                    encoding="utf-8",
                )
            (project / "SPEC.md").write_text("# SPEC\n", encoding="utf-8")
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "supervise",
                        "build tetris",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--steps",
                        "coder,judge",
                        "--new-file",
                        "tetris.html",
                        "--auto-fix",
                        "--max-rounds",
                        "3",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_supervise(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 4)
        self.assertEqual(manifest["completed_rounds"], 2)
        self.assertEqual(manifest["final_verdict"], "approved")
        self.assertTrue(any(path.endswith("03-r01-coder-output.md") for path in manifest["documents"]))
        self.assertTrue(any(path.endswith("04-r02-judge-review.md") for path in manifest["documents"]))
        self.assertIn("Judge review round 1", calls[2][1]["content"])

    def test_new_file_rejects_unsafe_paths(self):
        with self.assertRaises(self.local_sdlc.RunnerError):
            self.local_sdlc.normalize_new_files(["../escape.html"])

    def test_context_slice_includes_only_selected_lines(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "app.py").write_text(
                "line1\nline2\nline3\nline4\nline5\n",
                encoding="utf-8",
            )
            slices = self.local_sdlc.parse_context_slices(["app.py:2:4"])

            context = self.local_sdlc.collect_file_context(project, ["app.py"], 1000, slices)

        self.assertIn("app.py (selected line ranges)", context)
        self.assertIn("@@ lines 2-4", context)
        self.assertIn("line2", context)
        self.assertIn("line4", context)
        self.assertNotIn("line1", context)
        self.assertNotIn("line5", context)

    def test_python_public_symbol_ledger_lists_existing_api(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "record.py").write_text(
                "TYPE_INTEGER = 1\n\n"
                "class Codec:\n    pass\n\n"
                "def encode(values):\n    return b''\n\n"
                "def decode(payload):\n    return []\n",
                encoding="utf-8",
            )

            ledger = self.local_sdlc.python_public_symbol_ledger(project, ["record.py"])

        self.assertIn("Existing Python API symbol ledger", ledger)
        self.assertIn("functions=decode, encode", ledger)
        self.assertIn("classes=Codec", ledger)
        self.assertIn("constants=TYPE_INTEGER", ledger)

    def test_context_slice_rejects_bad_ranges(self):
        with self.assertRaises(self.local_sdlc.RunnerError):
            self.local_sdlc.parse_context_slices(["app.py:5:2"])

    def test_extract_unified_diff_from_fenced_output(self):
        output = """Here is the patch:
```diff
diff --git a/a.txt b/a.txt
--- a/a.txt
+++ b/a.txt
@@ -1 +1 @@
-old
+new
```
extra prose
"""
        patch = self.local_sdlc.extract_unified_diff(output)

        self.assertTrue(patch.startswith("diff --git"))
        self.assertIn("+new", patch)
        self.assertNotIn("extra prose", patch)

    def test_changed_paths_from_unified_diff(self):
        patch = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-old
+new
diff --git a/new.py b/new.py
--- /dev/null
+++ b/new.py
@@ -0,0 +1 @@
+created
"""

        self.assertEqual(self.local_sdlc.changed_paths_from_unified_diff(patch), ["app.py", "new.py"])

    def test_missing_changed_paths_after_patch_reports_absent_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "app.py").write_text("print('ok')\n", encoding="utf-8")

            missing = self.local_sdlc.missing_changed_paths_after_patch(
                project,
                ["app.py", "new.py"],
            )

        self.assertEqual(missing, ["new.py"])

    def test_apply_patch_file_rejects_no_content_change_patch(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            patch = project / "empty.patch"
            patch.write_text(
                textwrap.dedent(
                    """
                    diff --git a/app.py b/app.py
                    new file mode 100644
                    index 0000000..a1b2c3d
                    --- /dev/null
                    +++ b/app.py
                    """
                ).lstrip(),
                encoding="utf-8",
            )

            with self.assertRaises(self.local_sdlc.RunnerError) as ctx:
                self.local_sdlc.apply_patch_file(project, patch)

        self.assertIn("would not change", str(ctx.exception))

    def test_artifact_failure_type_classifies_corrupt_unified_diff(self):
        error = "git apply --numstat failed:\nerror: corrupt patch at line 270"

        failure_type = self.local_sdlc.artifact_failure_type(error, "patch_apply_failed")

        self.assertEqual(failure_type, "corrupt_unified_diff")

    def test_stage_scope_lint_blocks_future_btree_split_tests(self):
        brief = textwrap.dedent(
            """
            # Work

            ## Current Stage
            - id: S06
            - title: B+Tree leaf operations
            - goal: Implement single-page insert/search/scan/delete.

            ## Required Stage Test Paths
            - tests/test_btree.py
            """
        ).strip()
        output = textwrap.dedent(
            """
            BEGIN_FILE: tests/test_btree.py
            import unittest
            from minisqlite.storage.btree import BTree, InternalPage

            class TestBTree(unittest.TestCase):
                def test_large_inserts_create_internal_root(self):
                    tree = BTree()
                    self.assertIsNotNone(InternalPage)
            END_FILE
            """
        ).strip()

        findings = self.local_sdlc.lint_stage_scope_output(output, brief, ["tests/test_btree.py"])
        codes = {finding.code for finding in findings}

        self.assertIn("stage_scope_violation", codes)
        self.assertEqual(
            self.local_sdlc.artifact_lint_failure_type(findings),
            "stage_scope_violation",
        )

    def test_stage_scope_lint_allows_current_leaf_tests(self):
        brief = textwrap.dedent(
            """
            ## Current Stage
            - id: S06
            - title: B+Tree leaf operations
            - goal: Implement single-page insert/search/scan/delete.
            """
        ).strip()
        output = textwrap.dedent(
            """
            BEGIN_FILE: tests/test_btree.py
            import unittest

            class TestBTree(unittest.TestCase):
                def test_insert_search_scan_delete_leaf_page(self):
                    self.assertTrue(True)
            END_FILE
            """
        ).strip()

        findings = self.local_sdlc.lint_stage_scope_output(output, brief, ["tests/test_btree.py"])

        self.assertEqual(findings, [])

    def test_stage_scope_lint_allows_negated_future_terms(self):
        brief = textwrap.dedent(
            """
            ## Current Stage
            - id: S06
            - title: B+Tree leaf operations
            - goal: Implement single-page insert/search/scan/delete.
            """
        ).strip()
        output = textwrap.dedent(
            """
            BEGIN_FILE: tests/test_btree.py
            import unittest

            class TestBTree(unittest.TestCase):
                \"\"\"Verify single-page operations, no split/multi-page behavior.\"\"\"

                def test_multiple_inserts_ordered(self):
                    self.assertTrue(True)
            END_FILE
            """
        ).strip()

        findings = self.local_sdlc.lint_stage_scope_output(output, brief, ["tests/test_btree.py"])

        self.assertEqual(findings, [])

    def test_stage_scope_lint_allows_negated_product_future_terms(self):
        brief = textwrap.dedent(
            """
            ## Current Stage
            - id: S06
            - title: B+Tree leaf operations
            - goal: Implement single-page insert/search/scan/delete.
            """
        ).strip()
        output = textwrap.dedent(
            """
            BEGIN_FILE: minisqlite/storage/btree.py
            class BPlusTree:
                # Leaf pages only. No page splitting or internal page support in this stage.
                # No split, no internal pages, no multi-page support.

                def insert(self, rowid, payload):
                    if False:
                        raise RuntimeError("split is out of MVP scope")
            END_FILE
            """
        ).strip()

        findings = self.local_sdlc.lint_stage_scope_output(
            output,
            brief,
            ["tests/test_btree.py"],
            check_product_paths=True,
        )

        self.assertEqual(findings, [])

    def test_stage_scope_lint_blocks_product_split_edit_when_enabled(self):
        brief = textwrap.dedent(
            """
            ## Current Stage
            - id: S06
            - title: B+Tree leaf operations
            - goal: Implement single-page insert/search/scan/delete.
            """
        ).strip()
        output = textwrap.dedent(
            """
            BEGIN_SEARCH_REPLACE: minisqlite/storage/btree.py
            : minisqlite/storage/btree.py
            ```python
                def _split(self):
                    return None
            ```
            """
        ).strip()

        findings = self.local_sdlc.lint_stage_scope_output(
            output,
            brief,
            ["tests/test_btree.py"],
            check_product_paths=True,
        )
        codes = {finding.code for finding in findings}

        self.assertIn("stage_scope_violation", codes)

    def test_format_repair_allows_bare_begin_file_marker(self):
        output = "BEGIN_FILE\npath=app.py\n---\nprint('ok')\n---\nEND_FILE"

        issues = self.local_sdlc.format_repair_format_issues(output)

        self.assertEqual(issues, [])

    def test_format_repair_allows_fenced_content_inside_begin_file(self):
        output = "BEGIN_FILE: app.py\n```python\nprint('ok')\n```\nEND_FILE"

        artifact = self.local_sdlc.extract_file_artifact(output, ["app.py"])
        findings = self.local_sdlc.lint_artifact_output(output, [], [], format_repair_mode=True)

        self.assertEqual(artifact.content, "print('ok')")
        self.assertNotIn("format_repair_markdown_fence", {finding.code for finding in findings})
        self.assertNotIn("format_repair_no_artifact", {finding.code for finding in findings})

    def test_format_repair_lint_accepts_recoverable_unclosed_file_artifacts(self):
        output = """BEGIN_FILE: server.py
print("server")
BEGIN_FILE: README.md
# Redis mini server
"""
        findings = self.local_sdlc.lint_artifact_output(output, [], [], format_repair_mode=True)

        self.assertNotIn("unbalanced_file_artifact", {finding.code for finding in findings})
        self.assertNotIn("format_repair_unbalanced_file_artifact", {finding.code for finding in findings})
        self.assertNotIn("format_repair_no_artifact", {finding.code for finding in findings})

    def test_format_repair_lint_accepts_path_headed_fenced_file_artifacts(self):
        output = """```python
# minisqlite/result.py
class Result:
    pass
```
"""
        findings = self.local_sdlc.lint_artifact_output(output, [], [], format_repair_mode=True)

        self.assertEqual({finding.code for finding in findings}, set())

    def test_demote_writable_paths_to_readonly(self):
        allowed, readonly = self.local_sdlc.demote_writable_paths_to_readonly(
            allowed_paths=["minisqlite/cli.py", "minisqlite/storage/pager.py"],
            readonly_paths=["tests/test_cli.py"],
            demoted_paths=["minisqlite/cli.py"],
        )

        self.assertEqual(allowed, ["minisqlite/storage/pager.py"])
        self.assertEqual(readonly, ["tests/test_cli.py", "minisqlite/cli.py"])

    def test_freeze_test_paths_as_readonly_demotes_generated_tests(self):
        allowed, readonly = self.local_sdlc.freeze_test_paths_as_readonly(
            allowed_paths=["minisqlite/sql/parser.py", "tests/test_parser.py"],
            readonly_paths=["minisqlite/sql/lexer.py"],
        )

        self.assertEqual(allowed, ["minisqlite/sql/parser.py"])
        self.assertEqual(readonly, ["minisqlite/sql/lexer.py", "tests/test_parser.py"])

    def test_format_repair_accepts_loose_python_function_replacement(self):
        output = """BEGIN_SEARCH_REPLACE: app.py
```python
    def run(self):
        return "new"
```
"""

        findings = self.local_sdlc.lint_artifact_output(output, [], [], format_repair_mode=True)
        artifact = self.local_sdlc.extract_search_replace_artifact(
            output,
            self.local_sdlc.ArtifactPathPolicy(allowed_paths=("app.py",), allow_extra_new_files=True),
        )

        self.assertEqual({finding.code for finding in findings}, set())
        self.assertEqual(artifact.path, "app.py")
        self.assertEqual(artifact.search, "__PY_FUNCTION_REPLACE__:run")
        self.assertIn('return "new"', artifact.replace)

    def test_format_repair_accepts_loose_python_function_with_duplicate_path_line(self):
        output = """BEGIN_SEARCH_REPLACE: app.py
: app.py
```
    def run(self):
        return "new"
```
"""

        findings = self.local_sdlc.lint_artifact_output(output, [], [], format_repair_mode=True)
        artifact = self.local_sdlc.extract_search_replace_artifact(
            output,
            self.local_sdlc.ArtifactPathPolicy(allowed_paths=("app.py",), allow_extra_new_files=True),
        )

        self.assertEqual({finding.code for finding in findings}, set())
        self.assertEqual(artifact.path, "app.py")
        self.assertEqual(artifact.search, "__PY_FUNCTION_REPLACE__:run")
        self.assertIn('return "new"', artifact.replace)

    def test_format_repair_accepts_loose_python_function_with_extra_colon_path(self):
        output = """BEGIN_SEARCH_REPLACE: : app.py
```python
    def run(self):
        return "new"
```
"""

        findings = self.local_sdlc.lint_artifact_output(output, [], [], format_repair_mode=True)
        artifact = self.local_sdlc.extract_search_replace_artifact(
            output,
            self.local_sdlc.ArtifactPathPolicy(allowed_paths=("app.py",), allow_extra_new_files=True),
        )

        self.assertEqual({finding.code for finding in findings}, set())
        self.assertEqual(artifact.path, "app.py")
        self.assertEqual(artifact.search, "__PY_FUNCTION_REPLACE__:run")
        self.assertIn('return "new"', artifact.replace)

    def test_loose_python_function_replacement_rejects_multiple_function_fragment(self):
        output = """BEGIN_SEARCH_REPLACE: app.py
```python
    def run(self):
        return "new"

    def helper(self):
        return "helper"
```
"""

        artifacts = self.local_sdlc.loose_python_function_replacement_artifacts(
            output,
            self.local_sdlc.ArtifactPathPolicy(allowed_paths=("app.py",), allow_extra_new_files=True),
        )
        files = self.local_sdlc.extract_file_artifacts(
            output,
            self.local_sdlc.ArtifactPathPolicy(allowed_paths=("app.py",), allow_extra_new_files=True),
        )

        self.assertEqual(artifacts, [])
        self.assertEqual(files, [])

    def test_loose_python_function_replacement_does_not_swallow_conflict_markers(self):
        output = """BEGIN_SEARCH_REPLACE: app.py
```python
<<<<<<< SEARCH
def run():
    return "old"
=======
def run():
    return "new"
>>>>>>> REPLACE
```
"""

        artifact = self.local_sdlc.extract_search_replace_artifact(
            output,
            self.local_sdlc.ArtifactPathPolicy(allowed_paths=("app.py",), allow_extra_new_files=True),
        )

        self.assertEqual(artifact.path, "app.py")
        self.assertEqual(artifact.search, 'def run():\n    return "old"')
        self.assertEqual(artifact.replace, 'def run():\n    return "new"')

    def test_apply_loose_python_function_replacement(self):
        artifact = self.local_sdlc.SearchReplaceArtifact(
            path="app.py",
            search="__PY_FUNCTION_REPLACE__:run",
            replace='    def run(self):\n        return "new"\n',
        )

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            run_dir = project / "run"
            run_dir.mkdir()
            (project / "app.py").write_text(
                "class App:\n"
                "    def run(self):\n"
                "        return \"old\"\n\n"
                "    def other(self):\n"
                "        return \"other\"\n",
                encoding="utf-8",
            )
            doc = self.local_sdlc.apply_search_replace_artifact(project, artifact, run_dir, 1)
            text = (project / "app.py").read_text(encoding="utf-8")

        self.assertIn("PASS", doc)
        self.assertIn('return "new"', text)
        self.assertIn("def other", text)
        self.assertNotIn('return "old"', text)

    def test_loose_python_function_replacement_trims_sibling_functions(self):
        artifact = self.local_sdlc.SearchReplaceArtifact(
            path="app.py",
            search="__PY_FUNCTION_REPLACE__:run",
            replace='    def run(self):\n        return "new"\n\n    def helper(self):\n        return "helper"\n',
        )

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            run_dir = project / "run"
            run_dir.mkdir()
            (project / "app.py").write_text(
                "class App:\n"
                "    def run(self):\n"
                "        return \"old\"\n\n"
                "    def other(self):\n"
                "        return \"other\"\n",
                encoding="utf-8",
            )

            doc = self.local_sdlc.apply_search_replace_artifact(project, artifact, run_dir, 1)
            text = (project / "app.py").read_text(encoding="utf-8")

        self.assertIn("PASS", doc)
        self.assertIn('return "new"', text)
        self.assertNotIn("def helper", text)
        self.assertIn("def other", text)

    def test_stage_test_paths_in_command_docs_detects_stage_owned_failure(self):
        docs = [("command", 'File "/tmp/project/tests/test_record.py", line 256, in test_x\nAssertionError')]

        paths = self.local_sdlc.stage_test_paths_in_command_docs(
            docs,
            ("tests/test_record.py", "tests/test_parser.py"),
        )

        self.assertEqual(paths, ["tests/test_record.py"])

    def test_salvage_rejects_incomplete_prefix_before_readonly_path(self):
        output = """BEGIN_FILE: app.py
```python
print("partial")
```

BEGIN_FILE: tests/test_app.py
```python
raise AssertionError("do not edit")
```
"""
        policy = self.local_sdlc.ArtifactPathPolicy(
            allowed_paths=("app.py",),
            readonly_paths=("tests/test_app.py",),
            existing_paths=("app.py", "tests/test_app.py"),
        )

        salvaged = self.local_sdlc.salvage_completed_artifact_prefix_before_readonly_path(output, policy)

        self.assertIsNone(salvaged)

    def test_stream_guard_allows_recoverable_multi_pair_end_marker_typo(self):
        output = """BEGIN_SEARCH_REPLACE: app.py
<<<<<<< SEARCH
def one():
    return "old"
=======
def one():
    return "new"
>>>>>>> SEARCH

<<<<<<< SEARCH
def two():
    return "old"
=======
def two():
    return "new"
>>>>>>> REPLACE
"""

        result = self.local_sdlc.artifact_stream_guard(output)

        self.assertFalse(result.should_abort)

    def test_apply_loose_python_function_replacement_preserves_method_scope_after_dedent(self):
        output = """BEGIN_SEARCH_REPLACE: app.py
```python
    def run(self):
        return "new"
```
"""
        artifact = self.local_sdlc.extract_search_replace_artifact(
            output,
            self.local_sdlc.ArtifactPathPolicy(allowed_paths=("app.py",), allow_extra_new_files=True),
        )

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            run_dir = project / "run"
            run_dir.mkdir()
            (project / "app.py").write_text(
                "class App:\n"
                "    def run(self):\n"
                "        return \"old\"\n\n"
                "    def other(self):\n"
                "        return \"other\"\n",
                encoding="utf-8",
            )
            self.local_sdlc.apply_search_replace_artifact(project, artifact, run_dir, 1)
            text = (project / "app.py").read_text(encoding="utf-8")

        self.assertNotIn("\ndef run", text)
        self.assertIn("    def run(self):\n        return \"new\"", text)
        self.assertIn("    def other(self):\n        return \"other\"", text)

    def test_extract_multiple_search_replace_artifacts(self):
        output = """BEGIN_SEARCH_REPLACE: app.py
<<<<<<< SEARCH
old1
=======
new1
>>>>>>> REPLACE

BEGIN_SEARCH_REPLACE: app.py
<<<<<<< SEARCH
old2
=======
new2
>>>>>>> REPLACE
END_SEARCH_REPLACE"""

        artifacts = self.local_sdlc.extract_search_replace_artifacts(output, ["app.py"])

        self.assertEqual(len(artifacts), 2)
        self.assertEqual(artifacts[0].search, "old1")
        self.assertEqual(artifacts[1].replace, "new2")

    def test_extract_multiple_search_replace_pairs_under_one_path_header(self):
        output = """BEGIN_SEARCH_REPLACE: app.py
<<<<<<< SEARCH
old1
=======
new1
>>>>>>> REPLACE
<<<<<<< SEARCH
old2
=======
new2
>>>>>>> REPLACE"""

        artifacts = self.local_sdlc.extract_search_replace_artifacts(output, ["app.py"])

        self.assertEqual(len(artifacts), 2)
        self.assertEqual([artifact.path for artifact in artifacts], ["app.py", "app.py"])
        self.assertEqual([artifact.search for artifact in artifacts], ["old1", "old2"])
        self.assertEqual([artifact.replace for artifact in artifacts], ["new1", "new2"])

    def test_partition_noop_replacements_keeps_actionable_edits(self):
        noop = self.local_sdlc.SearchReplaceArtifact("app.py", "same", "same")
        action = self.local_sdlc.SearchReplaceArtifact("app.py", "old", "new")

        actionable, skipped = self.local_sdlc.partition_noop_replacements([noop, action])

        self.assertEqual(actionable, [action])
        self.assertEqual(skipped, [noop])

    def test_extract_search_replace_dedents_indented_marker_body(self):
        output = """BEGIN_SEARCH_REPLACE: app.py
<<<<<<< SEARCH
                            import pytest
import unittest
=======
                            import unittest
>>>>>>> REPLACE
END_SEARCH_REPLACE"""

        artifact = self.local_sdlc.extract_search_replace_artifact(output, ["app.py"])

        self.assertEqual(artifact.search, "import pytest\nimport unittest")
        self.assertEqual(artifact.replace, "import unittest")

    def test_artifact_lint_blocks_pytest_when_unittest_is_configured(self):
        output = "BEGIN_FILE: tests/test_app.py\nimport pytest\n\ndef test_x(tmp_path):\n    pass\nEND_FILE"

        findings = self.local_sdlc.lint_artifact_output(output, ["python3 -m unittest discover -s tests"])

        codes = {finding.code for finding in findings}
        self.assertIn("pytest_import", codes)
        self.assertIn("pytest_fixture", codes)
        self.assertTrue(any(finding.severity == "error" for finding in findings))

    def test_artifact_lint_catches_identical_search_replace(self):
        output = """BEGIN_SEARCH_REPLACE: app.py
<<<<<<< SEARCH
VALUE = 1
=======
VALUE = 1
>>>>>>> REPLACE
END_SEARCH_REPLACE"""

        findings = self.local_sdlc.lint_artifact_output(output, [])

        self.assertEqual(findings[0].code, "identical_search_replace")

    def test_artifact_lint_catches_nested_search_replace_markers(self):
        output = """BEGIN_SEARCH_REPLACE: app.py
<<<<<<< SEARCH
def value():
    return 1
=======
def value():
<<<<<<< SEARCH
    return 1
=======
    return 2
>>>>>>> REPLACE
>>>>>>> REPLACE
END_SEARCH_REPLACE"""

        findings = self.local_sdlc.lint_artifact_output(output, [])

        codes = {finding.code for finding in findings}
        self.assertIn("search_replace_conflict_markers", codes)
        self.assertEqual(
            self.local_sdlc.artifact_lint_failure_type(findings),
            "artifact_invalid",
        )

    def test_artifact_failure_modes_treats_conflict_marker_apply_failure_as_bad_search_replace(self):
        docs = [
            (
                "apply",
                "FAIL applying `app.py`:\n\n```text\nreplacement for app.py contains conflict markers\n```",
            )
        ]

        modes = self.local_sdlc.artifact_failure_modes_from_documents(docs, 10)

        self.assertIn("bad_search_replace", modes)

    def test_artifact_failure_modes_classify_json_plan_before_artifact(self):
        docs = [
            (
                "stream abort",
                "- code: stream_json_plan_before_artifact\n"
                "- reason: non-artifact JSON plan was mixed with file artifacts while streaming\n",
            )
        ]

        modes = self.local_sdlc.artifact_failure_modes_from_documents(docs, 10)

        self.assertIn("format_repair_protocol", modes)
        self.assertIn("json_plan_before_artifact", modes)

    def test_artifact_failure_modes_classify_mixed_artifact_formats(self):
        docs = [
            (
                "stream abort",
                "- code: stream_mixed_artifact_formats\n"
                "- reason: stream mixed JSON file artifacts with BEGIN_FILE artifacts; choose exactly one artifact protocol\n",
            )
        ]

        modes = self.local_sdlc.artifact_failure_modes_from_documents(docs, 10)

        self.assertIn("format_repair_protocol", modes)
        self.assertIn("mixed_artifact_formats", modes)

    def test_extract_file_artifacts_defers_to_loose_function_search_replace(self):
        output = """BEGIN_SEARCH_REPLACE: app.py
```python
    def run(self):
        return "new"
```
"""

        replacements = self.local_sdlc.extract_search_replace_artifacts(output, ["app.py"])
        files = self.local_sdlc.extract_file_artifacts(output, ["app.py"])

        self.assertEqual(len(replacements), 1)
        self.assertEqual(replacements[0].search, "__PY_FUNCTION_REPLACE__:run")
        self.assertEqual(files, [])

    def test_artifact_failure_modes_classify_single_artifact_required(self):
        docs = [
            (
                "stream abort",
                "- code: stream_multiple_file_artifacts_in_repair\n"
                "- reason: multiple BEGIN_FILE artifacts appeared in one repair stream\n",
            )
        ]

        modes = self.local_sdlc.artifact_failure_modes_from_documents(docs, 10)

        self.assertIn("format_repair_protocol", modes)
        self.assertIn("single_artifact_required", modes)

    def test_artifact_failure_modes_classify_atomic_search_replace_required(self):
        docs = [
            (
                "stream abort",
                "- code: stream_multiple_json_search_replace\n"
                "- reason: multiple JSON search_replace artifacts appeared in one repair stream\n",
            )
        ]

        modes = self.local_sdlc.artifact_failure_modes_from_documents(docs, 10)

        self.assertIn("format_repair_protocol", modes)
        self.assertIn("atomic_search_replace_required", modes)

    def test_artifact_failure_instruction_handles_ambiguous_function_replacement(self):
        docs = [
            (
                "apply failure",
                "function replacement target `_split` must occur exactly once in minisqlite/storage/btree.py; found 2",
            )
        ]

        modes = self.local_sdlc.artifact_failure_modes_from_documents(docs, 10)
        instruction = self.local_sdlc.artifact_failure_instruction_from_documents(docs, 10)

        self.assertIn("ambiguous_function_replacement", modes)
        self.assertIn("function name occurs multiple times", instruction)
        self.assertIn("surrounding context", instruction)

    def test_artifact_failure_modes_classify_oversized_python_file_artifact(self):
        docs = [
            (
                "stream abort",
                "- code: stream_python_file_artifact_too_large\n"
                "- reason: Python file artifact exceeded the stream size budget\n",
            )
        ]

        modes = self.local_sdlc.artifact_failure_modes_from_documents(docs, 10)

        self.assertIn("format_repair_protocol", modes)
        self.assertIn("oversized_python_file_artifact", modes)

    def test_artifact_failure_modes_classify_oversized_python_diff_artifact(self):
        docs = [
            (
                "stream abort",
                "- code: stream_python_diff_artifact_too_large\n"
                "- reason: Python unified diff artifact exceeded the stream size budget\n",
            )
        ]

        modes = self.local_sdlc.artifact_failure_modes_from_documents(docs, 10)

        self.assertIn("format_repair_protocol", modes)
        self.assertIn("oversized_python_diff_artifact", modes)

    def test_artifact_failure_modes_classify_empty_or_skipped_patch(self):
        docs = [
            (
                "apply",
                "git apply would not change any files; the patch was empty or skipped\n",
            )
        ]

        modes = self.local_sdlc.artifact_failure_modes_from_documents(docs, 10)

        self.assertIn("empty_or_skipped_patch", modes)

    def test_artifact_lint_allows_search_replace_that_removes_pytest(self):
        output = """BEGIN_SEARCH_REPLACE: tests/test_app.py
<<<<<<< SEARCH
import pytest

def test_x(tmp_path):
    pass
=======
import unittest

class TestX(unittest.TestCase):
    def test_x(self):
        self.assertTrue(True)
>>>>>>> REPLACE
END_SEARCH_REPLACE"""

        findings = self.local_sdlc.lint_artifact_output(output, ["python3 -m unittest discover -s tests"])

        self.assertEqual(findings, [])

    def test_extract_semantic_contracts_from_unittest_traceback(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            tests_dir = project / "tests"
            tests_dir.mkdir()
            test_file = tests_dir / "test_lexer.py"
            test_file.write_text(
                "\n".join(
                    [
                        "import unittest",
                        "",
                        "class TestLexer(unittest.TestCase):",
                        "    def test_type_contract(self):",
                        "        self.assertEqual(tokens[0].type, \"CREATE\")",
                        "    def test_count_contract(self):",
                        "        self.assertEqual(len(tokens), 1)",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            stderr = f"""Traceback
  File \"{test_file}\", line 5, in test_type_contract
    self.assertEqual(tokens[0].type, \"CREATE\")
AssertionError: 'CREATE' != <TokenType.CREATE: 1>
AttributeError: 'Token' object has no attribute 'line'
minisqlite.errors.SQLSyntaxError: Unexpected character: '*' at position 8
"""
            doc = self.local_sdlc.command_result_document(
                "python3 -m unittest discover -s tests",
                1,
                "",
                stderr,
                0.01,
            )

            contracts = self.local_sdlc.extract_semantic_contracts_from_command_docs(
                [("Command result", doc)],
                project,
            )

        texts = [contract.text for contract in contracts]
        self.assertTrue(any("Token.type must be string-compatible" in text for text in texts))
        self.assertTrue(any("must expose a `line` attribute" in text for text in texts))
        self.assertTrue(any("must tokenize '*' as a STAR token" in text for text in texts))
        self.assertTrue(any("token.type against string literals" in text for text in texts))

    def test_extract_semantic_contracts_classifies_keyword_mapping_separately(self):
        stderr = """Traceback
AssertionError: <TokenType.INSERT: 3> not found in [<TokenType.IDENTIFIER: 19>, <TokenType.IDENTIFIER: 19>]
AssertionError: <TokenType.IDENTIFIER: 19> != <TokenType.DELETE: 8>
AssertionError: '10' != '-10'
minisqlite.errors.SQLSyntaxError: Unexpected character '*' at L1:8
"""
        doc = self.local_sdlc.command_result_document(
            "python3 -m unittest discover -s tests",
            1,
            "",
            stderr,
            0.01,
        )

        contracts = self.local_sdlc.extract_semantic_contracts_from_command_docs(
            [("Command result", doc)]
        )

        texts = [contract.text for contract in contracts]
        self.assertTrue(any("keywords must map" in text.lower() for text in texts))
        self.assertTrue(any("Negative integer literals" in text for text in texts))
        self.assertTrue(any("must tokenize '*' as a STAR token" in text for text in texts))
        self.assertFalse(any("string-compatible" in text for text in texts))

    def test_extract_semantic_contracts_infers_product_from_test_traceback(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            tests_dir = project / "tests"
            tests_dir.mkdir()
            test_file = tests_dir / "test_pager.py"
            test_file.write_text(
                "\n".join(
                    [
                        "import unittest",
                        "",
                        "class TestPager(unittest.TestCase):",
                        "    def test_header_round_trip(self):",
                        "        self.assertEqual(pager2.format_version, 1)",
                        "    def test_next_page_id_persists(self):",
                        "        self.assertEqual(pager2.next_page_id, 3)",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            stderr = f"""Traceback
  File \"{test_file}\", line 5, in test_header_round_trip
    self.assertEqual(pager2.format_version, 1)
AttributeError: 'Pager' object has no attribute 'format_version'
Traceback
  File \"{test_file}\", line 7, in test_next_page_id_persists
    self.assertEqual(pager2.next_page_id, 3)
AssertionError: 1 != 3
"""
            doc = self.local_sdlc.command_result_document(
                "python3 -m unittest discover -s tests -p test_pager.py",
                1,
                "",
                stderr,
                0.01,
            )

            contracts = self.local_sdlc.extract_semantic_contracts_from_command_docs(
                [("Command result", doc)],
                project,
            )

        texts = [contract.text for contract in contracts]
        focus_sets = [contract.focus_files for contract in contracts]
        self.assertTrue(any("Pager objects must expose public `format_version`" in text for text in texts))
        self.assertTrue(any("Pager.next_page_id must persist" in text for text in texts))
        self.assertTrue(any("minisqlite/storage/pager.py" in focus for focus in focus_sets))
        self.assertTrue(any("tests/test_pager.py" in focus for focus in focus_sets))

    def test_extract_semantic_contracts_detects_pager_raw_page_io(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            tests_dir = project / "tests"
            tests_dir.mkdir()
            test_file = tests_dir / "test_pager.py"
            test_file.write_text(
                "\n".join(
                    [
                        "import unittest",
                        "PAGE_SIZE = 4096",
                        "",
                        "class TestPager(unittest.TestCase):",
                        "    def test_write_and_read_page(self):",
                        "        page_data = b'A' * PAGE_SIZE",
                        "        pager.write_page(1, page_data)",
                        "        read_data = pager.read_page(1)",
                        "        self.assertEqual(read_data, page_data)",
                        "",
                        "    def test_allocate_page(self):",
                        "        page_id = pager.allocate_page()",
                        "        page_data = pager.read_page(page_id)",
                        "        self.assertEqual(page_data, b'\\x00' * PAGE_SIZE)",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            stderr = (
                "Traceback\n"
                f"  File \"{test_file}\", line 7, in test_write_and_read_page\n"
                "    pager.write_page(1, page_data)\n"
                "minisqlite.errors.CorruptDatabaseError: Invalid page type for page 1: 65\n"
            )
            doc = self.local_sdlc.command_result_document(
                "python3 -m unittest discover -s tests -p test_pager.py",
                1,
                "",
                stderr,
                0.01,
            )

            contracts = self.local_sdlc.extract_semantic_contracts_from_command_docs(
                [("Command result", doc)],
                project,
            )
            advice = self.local_sdlc.repair_advice_from_command_docs(
                [("Command result", doc)],
                ["python3 -m unittest discover -s tests -p test_pager.py"],
                project,
                ["tests/test_pager.py"],
            )

        texts = [contract.text for contract in contracts]
        self.assertTrue(any("round-trip exact PAGE_SIZE bytes" in text for text in texts))
        self.assertTrue(any("zero-filled PAGE_SIZE page" in text for text in texts))
        self.assertIsNotNone(advice)
        self.assertEqual(advice.strategy, "semantic_contract_patch")
        self.assertTrue(any("does not enforce B+Tree page_type" in item for item in advice.instructions))

    def test_artifact_lint_blocks_pager_raw_page_contract_violation(self):
        contract = self.local_sdlc.SemanticContract(
            contract_id="C01",
            kind="api_contract",
            text="Pager.write_page(page_id, data) and Pager.read_page(page_id) must round-trip exact PAGE_SIZE bytes; pager.py must not interpret B+Tree page_type bytes in raw page IO. Pager.allocate_page() must create a zero-filled PAGE_SIZE page readable through read_page().",
            source="test",
            focus_files=("minisqlite/storage/pager.py",),
            evidence=(),
        )
        output = json.dumps(
            {
                "artifacts": [
                    {
                        "type": "replace_file",
                        "path": "minisqlite/storage/pager.py",
                        "content": "from minisqlite.errors import CorruptDatabaseError\nPAGE_TYPE_LEAF = 1\nPAGE_TYPE_INTERNAL = 2\n\ndef write_page(page_id, data):\n    page_type = data[0]\n    if page_type not in (PAGE_TYPE_LEAF, PAGE_TYPE_INTERNAL):\n        raise CorruptDatabaseError('bad page')\n\ndef read_page(page_id):\n    page_data = b''\n    page_type = page_data[0]\n    if page_type not in (PAGE_TYPE_LEAF, PAGE_TYPE_INTERNAL):\n        raise CorruptDatabaseError('bad page')\n    return page_data\n\ndef allocate_page():\n    return bytes([PAGE_TYPE_LEAF]) + b'\\x00' * 4095\n",
                    }
                ]
            }
        )

        findings = self.local_sdlc.lint_artifact_output(
            output,
            ["python3 -m unittest discover -s tests -p test_pager.py"],
            [contract],
        )

        codes = {finding.code for finding in findings}
        self.assertIn("semantic_contract_pager_raw_page_io", codes)
        self.assertIn("semantic_contract_pager_zero_allocation", codes)

    def test_artifact_lint_blocks_semantic_contract_violation(self):
        contract = self.local_sdlc.SemanticContract(
            contract_id="C01",
            kind="api_contract",
            text="Token.type must be string-compatible with the tested token names, not a raw Enum value. tokenize must not append an extra EOF token when tests expect only source tokens.",
            source="test",
            focus_files=("minisqlite/sql/lexer.py",),
            evidence=(),
        )
        output = json.dumps(
            {
                "artifacts": [
                    {
                        "type": "replace_file",
                        "path": "minisqlite/sql/lexer.py",
                        "content": "class TokenType: pass\nclass Token:\n    type: TokenType\n\ndef tokenize(sql):\n    tokens=[]\n    tokens.append(Token(TokenType.EOF, ''))\n    return tokens\n",
                    }
                ]
            }
        )

        findings = self.local_sdlc.lint_artifact_output(
            output,
            ["python3 -m unittest discover -s tests"],
            [contract],
        )

        codes = {finding.code for finding in findings}
        self.assertIn("semantic_contract_token_type_string", codes)
        self.assertIn("semantic_contract_no_extra_eof", codes)

    def test_artifact_lint_detects_indented_identical_search_replace(self):
        output = textwrap.dedent(
            """
                BEGIN_SEARCH_REPLACE: app.py
                    <<<<<<< SEARCH
                    VALUE = 1
                    =======
                    VALUE = 1
                    >>>>>>> REPLACE
                END_SEARCH_REPLACE
            """
        )

        findings = self.local_sdlc.lint_artifact_output(output, [], [])

        self.assertIn("identical_search_replace", {finding.code for finding in findings})

    def test_semantic_repair_lint_requires_one_atomic_product_artifact(self):
        contract = self.local_sdlc.SemanticContract(
            contract_id="C01",
            kind="api_contract",
            text="Negative integer literals must preserve the leading '-' in Token.value.",
            source="test",
            focus_files=("minisqlite/sql/lexer.py",),
            evidence=(),
        )
        output = textwrap.dedent(
            """
            BEGIN_FILE: minisqlite/sql/lexer.py
            def tokenize(sql):
                return []
            END_FILE
            BEGIN_SEARCH_REPLACE: tests/test_lexer.py
            <<<<<<< SEARCH
            self.assertEqual("10", "-10")
            =======
            self.assertEqual("10", "10")
            >>>>>>> REPLACE
            END_SEARCH_REPLACE
            """
        )

        findings = self.local_sdlc.lint_artifact_output(
            output,
            ["python3 -m unittest discover -s tests"],
            [contract],
            semantic_repair_mode=True,
        )

        codes = {finding.code for finding in findings}
        self.assertIn("semantic_repair_forbidden_artifact", codes)
        self.assertIn("semantic_repair_not_atomic", codes)
        self.assertIn("semantic_repair_test_edit", codes)

    def test_semantic_repair_allows_policy_authorized_generated_test_edit(self):
        output = textwrap.dedent(
            """
            BEGIN_SEARCH_REPLACE: tests/test_cli.py
            <<<<<<< SEARCH
                    self.assertEqual(lines[1], \"id|name|age\"  # separator line
            =======
                    self.assertEqual(lines[1], \"id|name|age\")  # separator line
            >>>>>>> REPLACE
            END_SEARCH_REPLACE
            """
        )

        findings = self.local_sdlc.lint_artifact_output(
            output,
            ["python3 -m unittest discover -s tests"],
            semantic_repair_mode=True,
            authorized_test_edit_paths=("tests/test_cli.py",),
        )

        codes = {finding.code for finding in findings}
        self.assertNotIn("semantic_repair_test_edit", codes)

    def test_extract_missing_context_requests_reads_bulleted_paths(self):
        output = textwrap.dedent(
            """
            MISSING_CONTEXT: Required file content not provided.

            Missing file paths:
            - minisqlite/sql/lexer.py
            - minisqlite/sql/token.py

            ## Output
            """
        )

        requests = self.local_sdlc.extract_missing_context_requests(output)

        paths = [path for request in requests for path in request.paths]
        self.assertIn("minisqlite/sql/lexer.py", paths)
        self.assertIn("minisqlite/sql/token.py", paths)

    def test_semantic_repair_format_issues_are_specific(self):
        output = textwrap.dedent(
            """
            Here is the fix:
            ```text
            BEGIN_SEARCH_REPLACE
            <<<<<<< SEARCH
            VALUE = "old"
            =======
            VALUE = "new"
            >>>>>>> REPLACE
            END_SEARCH_REPLACE
            ```
            """
        )

        issues = self.local_sdlc.semantic_repair_format_issues(output)
        codes = {issue.code for issue in issues}

        self.assertIn("semantic_repair_missing_path", codes)
        self.assertIn("semantic_repair_prose_mixed", codes)
        self.assertIn("semantic_repair_markdown_fence", codes)

    def test_format_repair_format_issues_are_specific(self):
        output = textwrap.dedent(
            """
            Here is the repaired artifact:
            ```text
            BEGIN_SEARCH_REPLACE
            <<<<<<< SEARCH
            VALUE = "old"
            =======
            VALUE = "new"
            >>>>>>> REPLACE
            ```
            """
        )

        issues = self.local_sdlc.format_repair_format_issues(output)
        codes = {issue.code for issue in issues}

        self.assertIn("format_repair_missing_path", codes)
        self.assertIn("format_repair_prose_mixed", codes)
        self.assertIn("format_repair_markdown_fence", codes)

    def test_format_repair_recovers_file_header_fenced_search_replace(self):
        output = textwrap.dedent(
            """
            BEGIN_SEARCH_REPLACE
            File: app.py
            ```python
            <<<<<<< SEARCH
            VALUE = "old"
            =======
            VALUE = "new"
            >>>>>>> REPLACE
            ```
            """
        )

        issues = self.local_sdlc.format_repair_format_issues(output)
        codes = {issue.code for issue in issues}
        self.assertNotIn("format_repair_missing_path", codes)
        self.assertNotIn("format_repair_markdown_fence", codes)
        self.assertNotIn("format_repair_no_artifact", codes)

        artifacts = self.local_sdlc.extract_search_replace_artifacts(
            output,
            self.local_sdlc.ArtifactPathPolicy(allowed_paths=("app.py",), existing_paths=("app.py",)),
        )
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0].path, "app.py")
        self.assertEqual(artifacts[0].search, 'VALUE = "old"')
        self.assertEqual(artifacts[0].replace, 'VALUE = "new"')

    def test_format_repair_lint_accepts_recoverable_fenced_diff(self):
        output = textwrap.dedent(
            """
            ```diff
            diff --git a/app.py b/app.py
            --- a/app.py
            +++ b/app.py
            @@ -1 +1 @@
            -VALUE = "old"
            +VALUE = "new"
            ```
            """
        )

        findings = self.local_sdlc.lint_artifact_output(output, [], [], format_repair_mode=True)

        self.assertEqual(findings, [])

    def test_format_repair_lint_rejects_no_artifact_output(self):
        findings = self.local_sdlc.lint_artifact_output(
            "plain code with no artifact marker",
            [],
            [],
            format_repair_mode=True,
        )

        self.assertIn("format_repair_no_artifact", {finding.code for finding in findings})

    def test_format_repair_lint_accepts_path_label_file_artifact(self):
        output = "BEGIN_FILE\npath: app.py\nprint('ok')\nEND_FILE"
        findings = self.local_sdlc.lint_artifact_output(output, [], [], format_repair_mode=True)

        self.assertNotIn("unbalanced_file_artifact", {finding.code for finding in findings})
        self.assertNotIn("format_repair_no_artifact", {finding.code for finding in findings})

    def test_artifact_lint_failure_type_prioritizes_semantic_repair_format(self):
        findings = [
            self.local_sdlc.ArtifactLintFinding(
                "error",
                "semantic_repair_not_atomic",
                "semantic repair must be exactly one artifact",
            ),
            self.local_sdlc.ArtifactLintFinding(
                "error",
                "semantic_repair_missing_path",
                "missing path",
            ),
        ]

        failure_type = self.local_sdlc.artifact_lint_failure_type(findings)

        self.assertEqual(failure_type, "semantic_repair_missing_path")

    def test_semantic_contract_focus_paths_split_product_and_tests(self):
        contracts = [
            self.local_sdlc.SemanticContract(
                contract_id="C01",
                kind="api_contract",
                text="Token objects must expose token_type.",
                source="test",
                focus_files=("minisqlite/sql/lexer.py", "tests/test_lexer.py", "missing.py"),
                evidence=(),
            )
        ]

        writable, readonly = self.local_sdlc.semantic_contract_focus_paths(
            contracts,
            ["minisqlite/sql/lexer.py", "tests/test_lexer.py"],
        )

        self.assertEqual(writable, ["minisqlite/sql/lexer.py"])
        self.assertEqual(readonly, ["tests/test_lexer.py"])

    def test_artifact_failure_instruction_bans_repeated_bad_formats(self):
        docs = [
            (
                "Patch apply",
                "FAIL applying `app.py`: search text must occur exactly once in app.py; found 0",
            ),
            (
                "Patch failure",
                "File artifact extraction also failed: invalid JSON artifact: Expecting ',' delimiter",
            ),
        ]

        instruction = self.local_sdlc.artifact_failure_instruction_from_documents(docs, 4)

        self.assertIn("Do not emit search_replace", instruction)
        self.assertIn("Do not emit JSON artifacts", instruction)
        self.assertIn("BEGIN_FILE", instruction)

    def test_repeated_json_search_replace_is_blocked(self):
        repeated = ",".join(
            '{"type":"search_replace","path":"app.py","search":"A","replace":"B"}'
            for _index in range(13)
        )
        output = '{"artifacts":[' + repeated + "]}"

        findings = self.local_sdlc.lint_artifact_output(output, [], [])

        self.assertIn("repeated_json_search_replace", {finding.code for finding in findings})

    def test_artifact_stream_guard_aborts_repeated_search_replace(self):
        repeated = ",".join(
            '{"type":"search_replace","path":"app.py","search":"A","replace":"B"}'
            for _index in range(8)
        )
        result = self.local_sdlc.artifact_stream_guard('{"artifacts":[' + repeated)

        self.assertTrue(result.should_abort)
        self.assertEqual(result.code, "stream_repeated_json_search_replace")
        self.assertEqual(result.score, 8)

    def test_artifact_stream_guard_aborts_repeated_text_runaway(self):
        output = " ".join("REINDEX" for _index in range(90))

        result = self.local_sdlc.artifact_stream_guard(output)

        self.assertTrue(result.should_abort)
        self.assertEqual(result.code, "stream_repeated_text_runaway")
        self.assertGreaterEqual(result.score, 80)

    def test_artifact_stream_guard_allows_common_code_identifiers(self):
        output = "\n".join(
            f"def check_{index}(token):\n    return token.type == 'KEYWORD' and self.value == token.value"
            for index in range(90)
        )

        result = self.local_sdlc.artifact_stream_guard(output)

        self.assertFalse(result.should_abort)

    def test_artifact_stream_guard_aborts_markdown_fence_without_artifact(self):
        output = "```artifact\n" + ("print('x')\n" * 300)

        result = self.local_sdlc.artifact_stream_guard(output)

        self.assertTrue(result.should_abort)
        self.assertEqual(result.code, "stream_markdown_fence_before_artifact")

    def test_artifact_stream_guard_aborts_markdown_fence_wrapping_diff(self):
        output = "```diff\n" + "diff --git a/app.py b/app.py\n"

        result = self.local_sdlc.artifact_stream_guard(output)

        self.assertTrue(result.should_abort)
        self.assertEqual(result.code, "stream_markdown_fence_before_artifact")

    def test_artifact_stream_guard_aborts_json_plan_mixed_with_file_artifact(self):
        output = (
            '{"propositions": ["P1", "P2"], "edges": ["supports(P1,G1)"]}\n\n'
            + "\n".join(f"# planning text {index}" for index in range(180))
            + "\n"
            + "BEGIN_FILE: app.py\nprint('ok')\nEND_FILE"
        )

        result = self.local_sdlc.artifact_stream_guard(output)

        self.assertTrue(result.should_abort)
        self.assertEqual(result.code, "stream_json_plan_before_artifact")

    def test_artifact_stream_guard_aborts_json_file_artifact_mixed_with_begin_file(self):
        output = (
            '{"artifacts":[{"type":"replace_file","path":"app.py","content":"print(1)"}]}\n'
            "BEGIN_FILE: other.py\n"
            "print(2)\n"
            "END_FILE\n"
        )

        result = self.local_sdlc.artifact_stream_guard(output)

        self.assertTrue(result.should_abort)
        self.assertEqual(result.code, "stream_mixed_artifact_formats")

    def test_artifact_stream_guard_aborts_multiple_begin_file_in_single_artifact_mode(self):
        output = (
            "BEGIN_FILE: app.py\n"
            "print(1)\n"
            "END_FILE\n"
            "BEGIN_FILE: tests/test_app.py\n"
            "print(2)\n"
        )

        result = self.local_sdlc.artifact_stream_guard(output, single_artifact_mode=True)

        self.assertTrue(result.should_abort)
        self.assertEqual(result.code, "stream_multiple_file_artifacts_in_repair")
        self.assertEqual(result.score, 2)

    def test_artifact_stream_guard_allows_multiple_begin_file_without_single_artifact_mode(self):
        output = (
            "BEGIN_FILE: app.py\n"
            "print(1)\n"
            "END_FILE\n"
            "BEGIN_FILE: tests/test_app.py\n"
            "print(2)\n"
            "END_FILE\n"
        )

        result = self.local_sdlc.artifact_stream_guard(output)

        self.assertFalse(result.should_abort)

    def test_artifact_stream_guard_aborts_multi_file_diff_in_single_artifact_mode(self):
        output = (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1 +1 @@\n"
            "-a\n"
            "+b\n"
            "diff --git a/other.py b/other.py\n"
            "--- a/other.py\n"
            "+++ b/other.py\n"
        )

        result = self.local_sdlc.artifact_stream_guard(output, single_artifact_mode=True)

        self.assertTrue(result.should_abort)
        self.assertEqual(result.code, "stream_multiple_file_artifacts_in_repair")

    def test_artifact_stream_guard_aborts_long_prose_before_artifact(self):
        output = ("This is explanation before any artifact.\n" * 120) + "BEGIN_FILE: app.py\nprint('ok')\nEND_FILE"

        result = self.local_sdlc.artifact_stream_guard(output)

        self.assertTrue(result.should_abort)
        self.assertEqual(result.code, "stream_prose_before_artifact")

    def test_artifact_stream_guard_aborts_long_non_artifact_output(self):
        output = "\n".join(f"Analysis sentence {index}: tracing the current failure path." for index in range(120))

        result = self.local_sdlc.artifact_stream_guard(output)

        self.assertTrue(result.should_abort)
        self.assertEqual(result.code, "stream_non_artifact_output")

    def test_artifact_stream_guard_aborts_oversized_search_replace(self):
        output = (
            '{"artifacts":[{"type":"search_replace","path":"app.py","search":"old","replace":"'
            + ("x" * (self.local_sdlc.ARTIFACT_OUTPUT_BUDGET_BYTES * 2))
        )

        result = self.local_sdlc.artifact_stream_guard(output)

        self.assertTrue(result.should_abort)
        self.assertEqual(result.code, "stream_artifact_too_large")

    def test_artifact_stream_guard_allows_small_same_file_json_search_replace_set(self):
        output = (
            '{"artifacts":['
            '{"type":"search_replace","path":"app.py","search":"old1","replace":"new1"},'
            '{"type":"search_replace","path":"app.py","search":"old2","replace":"new2"}'
        )

        result = self.local_sdlc.artifact_stream_guard(output)

        self.assertFalse(result.should_abort)

    def test_artifact_stream_guard_aborts_json_search_replace_across_paths_in_single_mode(self):
        output = (
            '{"artifacts":['
            '{"type":"search_replace","path":"app.py","search":"old1","replace":"new1"},'
            '{"type":"search_replace","path":"other.py","search":"old2","replace":"new2"}'
        )

        result = self.local_sdlc.artifact_stream_guard(output, single_artifact_mode=True)

        self.assertTrue(result.should_abort)
        self.assertEqual(result.code, "stream_multiple_json_search_replace")

    def test_artifact_stream_guard_aborts_too_many_json_search_replace_edits(self):
        output = (
            '{"artifacts":['
            '{"type":"search_replace","path":"app.py","search":"old1","replace":"new1"},'
            '{"type":"search_replace","path":"app.py","search":"old2","replace":"new2"},'
            '{"type":"search_replace","path":"app.py","search":"old3","replace":"new3"},'
            '{"type":"search_replace","path":"app.py","search":"old4","replace":"new4"},'
            '{"type":"search_replace","path":"app.py","search":"old5","replace":"new5"},'
            '{"type":"search_replace","path":"app.py","search":"old6","replace":"new6"},'
            '{"type":"search_replace","path":"app.py","search":"old7","replace":"new7"}'
        )

        result = self.local_sdlc.artifact_stream_guard(output)

        self.assertTrue(result.should_abort)
        self.assertEqual(result.code, "stream_multiple_json_search_replace")

    def test_artifact_stream_guard_aborts_readonly_json_artifact_path(self):
        output = (
            '{"artifacts":['
            '{"type":"search_replace","path":"tests/test_cli.py","search":"old","replace":"new"}'
        )
        policy = self.local_sdlc.ArtifactPathPolicy(
            allowed_paths=("minisqlite/cli.py",),
            readonly_paths=("tests/test_cli.py",),
            existing_paths=("minisqlite/cli.py", "tests/test_cli.py"),
        )

        result = self.local_sdlc.artifact_stream_guard(output, artifact_policy=policy)

        self.assertTrue(result.should_abort)
        self.assertEqual(result.code, "stream_readonly_artifact_path")

    def test_artifact_stream_guard_aborts_process_analysis_before_quoted_marker(self):
        output = (
            "Looking at the evidence, the next output should use `BEGIN_SEARCH_REPLACE: path`.\n"
            "I need to trace the issue before writing the patch.\n"
            + ("analysis " * 80)
        )

        result = self.local_sdlc.artifact_stream_guard(output)

        self.assertTrue(result.should_abort)
        self.assertEqual(result.code, "stream_prose_before_artifact")

    def test_artifact_stream_guard_aborts_oversized_python_file_artifact(self):
        output = (
            "BEGIN_FILE: minisqlite/storage/btree.py\n"
            + ("x" * (self.local_sdlc.ARTIFACT_OUTPUT_BUDGET_BYTES * 3))
        )

        result = self.local_sdlc.artifact_stream_guard(output)

        self.assertTrue(result.should_abort)
        self.assertEqual(result.code, "stream_python_file_artifact_too_large")

    def test_artifact_stream_guard_keeps_python_file_budget_in_single_artifact_mode(self):
        output = (
            "BEGIN_FILE: minisqlite/storage/pager.py\n"
            + ("x" * (self.local_sdlc.ARTIFACT_OUTPUT_BUDGET_BYTES + 1))
        )

        normal_result = self.local_sdlc.artifact_stream_guard(output)
        repair_result = self.local_sdlc.artifact_stream_guard(output, single_artifact_mode=True)

        self.assertFalse(normal_result.should_abort)
        self.assertFalse(repair_result.should_abort)

    def test_artifact_stream_guard_budgets_python_file_blocks_individually(self):
        output = (
            "BEGIN_FILE: minisqlite/storage/btree.py\n"
            + ("x" * (self.local_sdlc.ARTIFACT_OUTPUT_BUDGET_BYTES * 2))
            + "\nEND_FILE\n"
            + "BEGIN_FILE: tests/test_btree.py\n"
            + ("y" * (self.local_sdlc.ARTIFACT_OUTPUT_BUDGET_BYTES * 2))
        )

        result = self.local_sdlc.artifact_stream_guard(output)

        self.assertFalse(result.should_abort)

    def test_artifact_stream_guard_aborts_oversized_python_diff_artifact(self):
        output = (
            "diff --git a/minisqlite/storage/btree.py b/minisqlite/storage/btree.py\n"
            "--- /dev/null\n"
            "+++ b/minisqlite/storage/btree.py\n"
            "@@ -0,0 +1,999 @@\n"
            + ("+x\n" * (self.local_sdlc.ARTIFACT_OUTPUT_BUDGET_BYTES * 3))
        )

        result = self.local_sdlc.artifact_stream_guard(output)

        self.assertTrue(result.should_abort)
        self.assertEqual(result.code, "stream_python_diff_artifact_too_large")

    def test_artifact_stream_guard_aborts_process_narration_inside_artifact(self):
        output = (
            'BEGIN_SEARCH_REPLACE: app.py\n'
            '<<<<<<< SEARCH\nold\n=======\n'
            '# Let me reconsider the flow before changing this.\n'
        )

        result = self.local_sdlc.artifact_stream_guard(output)

        self.assertTrue(result.should_abort)
        self.assertEqual(result.code, "stream_artifact_process_narration")

    def test_artifact_stream_guard_aborts_malformed_colon_search_replace_body(self):
        output = (
            "BEGIN_SEARCH_REPLACE: app.py\n"
            ":    def run(self):\n"
            "        return 'new'\n"
        )

        result = self.local_sdlc.artifact_stream_guard(output)

        self.assertTrue(result.should_abort)
        self.assertEqual(result.code, "stream_artifact_malformed_search_replace")

    def test_artifact_stream_guard_aborts_orphan_search_replace_body(self):
        output = (
            "<<<<<<< SEARCH\n"
            "VALUE = 'old'\n"
            "=======\n"
            "VALUE = 'new'\n"
        )

        result = self.local_sdlc.artifact_stream_guard(output)

        self.assertTrue(result.should_abort)
        self.assertEqual(result.code, "stream_orphan_search_replace")

    def test_artifact_stream_guard_allows_duplicate_path_before_fenced_function(self):
        output = (
            "BEGIN_SEARCH_REPLACE: app.py\n"
            ": app.py\n"
            "```python\n"
            "def run():\n"
            "    return 'new'\n"
            "```\n"
        )

        result = self.local_sdlc.artifact_stream_guard(output)

        self.assertFalse(result.should_abort)

    def test_artifact_stream_guard_aborts_identical_search_replace(self):
        output = (
            "BEGIN_SEARCH_REPLACE: app.py\n"
            "<<<<<<< SEARCH\n"
            "def run():\n"
            "    return 1\n"
            "=======\n"
            "def run():\n"
            "    return 1\n"
            ">>>>>>> REPLACE\n"
        )

        result = self.local_sdlc.artifact_stream_guard(output)

        self.assertTrue(result.should_abort)
        self.assertEqual(result.code, "stream_identical_search_replace")

    def test_format_repair_lint_rejects_malformed_colon_search_replace_body(self):
        output = (
            "BEGIN_SEARCH_REPLACE: app.py\n"
            ":    def run(self):\n"
            "        return 'new'\n"
        )

        findings = self.local_sdlc.lint_artifact_output(output, [], [], format_repair_mode=True)
        codes = {finding.code for finding in findings}

        self.assertIn("format_repair_malformed_search_replace", codes)
        self.assertEqual(
            self.local_sdlc.artifact_lint_failure_type(findings),
            "format_repair_malformed_search_replace",
        )

    def test_artifact_lint_rejects_orphan_search_replace_body(self):
        output = (
            "<<<<<<< SEARCH\n"
            "VALUE = 'old'\n"
            "=======\n"
            "VALUE = 'new'\n"
            ">>>>>>> REPLACE\n"
        )

        findings = self.local_sdlc.lint_artifact_output(output, [], [])
        codes = {finding.code for finding in findings}

        self.assertIn("artifact_orphan_search_replace", codes)
        self.assertEqual(
            self.local_sdlc.artifact_lint_failure_type(findings),
            "artifact_orphan_search_replace",
        )

    def test_artifact_lint_blocks_forbidden_absent_api_addition(self):
        output = (
            "BEGIN_SEARCH_REPLACE: pkg/pager.py\n"
            "<<<<<<< SEARCH\n"
            "    def read_page(self, page_id):\n"
            "=======\n"
            "    def read_schema_metadata(self):\n"
            "        return None\n"
            "\n"
            "    def read_page(self, page_id):\n"
            ">>>>>>> REPLACE\n"
        )

        findings = self.local_sdlc.lint_artifact_output(
            output,
            [],
            [],
            forbidden_actions=[
                "absent API from mechanical probe: Pager.read_schema_metadata",
                "Forbidden by project-policy triage: Do not add read_schema_metadata to Pager solely to satisfy the broken call site",
            ],
        )
        codes = {finding.code for finding in findings}

        self.assertIn("forbidden_absent_api_addition", codes)

    def test_artifact_lint_allows_product_regression_absent_api_restore(self):
        output = (
            "BEGIN_SEARCH_REPLACE: pkg/pager.py\n"
            "<<<<<<< SEARCH\n"
            "    def read_page(self, page_id):\n"
            "=======\n"
            "    def read_schema_metadata(self):\n"
            "        return None\n"
            "\n"
            "    def read_page(self, page_id):\n"
            ">>>>>>> REPLACE\n"
        )

        findings = self.local_sdlc.lint_artifact_output(
            output,
            [],
            [],
            forbidden_actions=[
                "absent API from mechanical probe: Pager.read_schema_metadata",
                (
                    "Treat missing `Pager.read_schema_metadata` as a product API/call-site "
                    "inconsistency, not as a generated test-harness mismatch, because product code references it."
                ),
            ],
        )
        codes = {finding.code for finding in findings}

        self.assertNotIn("forbidden_absent_api_addition", codes)

    def test_artifact_lint_blocks_repair_advice_forbidden_symbol_edit(self):
        output = (
            '{"artifacts":['
            '{"type":"search_replace","path":"minisqlite/storage/pager.py",'
            '"search":"    def _write_header(self):\\n        pass",'
            '"replace":"    def _write_header(self):\\n        self._file.flush()"}'
            ']}'
        )

        findings = self.local_sdlc.lint_artifact_output(
            output,
            [],
            [],
            forbidden_actions=[
                "For row persistence loss with surviving schema metadata, do not edit _write_header, write_schema_metadata, read_schema_metadata, CLI output, README, or tests.",
            ],
        )
        codes = {finding.code for finding in findings}

        self.assertIn("forbidden_repair_target_edit", codes)

    def test_forbidden_edit_symbol_extraction_ignores_prepositions(self):
        symbols = self.local_sdlc.forbidden_edit_symbols_from_texts(
            [
                "For row persistence loss with surviving schema metadata, "
                "do not edit _write_header for this repair round.",
            ]
        )

        self.assertEqual(symbols, ["_write_header"])

    def test_artifact_lint_blocks_unknown_self_attribute_reference(self):
        output = json.dumps(
            {
                "artifacts": [
                    {
                        "type": "search_replace",
                        "path": "connection.py",
                        "search": "    def close(self):\n        self.pager.close()\n",
                        "replace": "    def close(self):\n        if self._pager:\n            self._pager.close()\n",
                    }
                ]
            }
        )

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "connection.py").write_text(
                "class Connection:\n"
                "    def __init__(self):\n"
                "        self.pager = None\n"
                "\n"
                "    def close(self):\n"
                "        self.pager.close()\n",
                encoding="utf-8",
            )

            findings = self.local_sdlc.lint_artifact_output(output, [], [], project=project)

        codes = {finding.code for finding in findings}
        self.assertIn("unknown_self_attribute_reference", codes)

    def test_artifact_lint_allows_known_self_attribute_reference(self):
        output = json.dumps(
            {
                "artifacts": [
                    {
                        "type": "search_replace",
                        "path": "connection.py",
                        "search": "    def close(self):\n        self.pager.close()\n",
                        "replace": "    def close(self):\n        if self.pager:\n            self.pager.close()\n",
                    }
                ]
            }
        )

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "connection.py").write_text(
                "class Connection:\n"
                "    def __init__(self):\n"
                "        self.pager = None\n"
                "\n"
                "    def close(self):\n"
                "        self.pager.close()\n",
                encoding="utf-8",
            )

            findings = self.local_sdlc.lint_artifact_output(output, [], [], project=project)

        codes = {finding.code for finding in findings}
        self.assertNotIn("unknown_self_attribute_reference", codes)

    def test_artifact_lint_blocks_product_path_with_test_body(self):
        output = json.dumps(
            {
                "artifacts": [
                    {
                        "type": "search_replace",
                        "path": "pkg/cli.py",
                        "search": "    def test_cli(self):\n        self.assertIn('ok', output)\n",
                        "replace": "    def test_cli(self):\n        self.assertIn('ok', output)\n",
                    }
                ]
            }
        )

        findings = self.local_sdlc.lint_artifact_output(output, [], [])
        codes = {finding.code for finding in findings}

        self.assertIn("artifact_path_content_mismatch", codes)
        self.assertIn("identical_search_replace", codes)

    def test_root_cause_stream_guard_aborts_oversized_report(self):
        output = "ROOT_CAUSE_REPORT\n" + ("- detail\n" * 2000)

        result = self.local_sdlc.root_cause_stream_guard(output)

        self.assertTrue(result.should_abort)
        self.assertEqual(result.code, "stream_root_cause_too_large")

    def test_root_cause_stream_guard_aborts_unbounded_self_revision(self):
        output = (
            "ROOT_CAUSE_REPORT\n"
            "- chosen_root_cause: Wait, this should work. Actually, unless the layout differs, "
            "let me re-examine it. The only explanation is still unclear. "
            + ("more analysis " * 400)
        )

        result = self.local_sdlc.root_cause_stream_guard(output)

        self.assertTrue(result.should_abort)
        self.assertEqual(result.code, "stream_root_cause_too_large")

    def test_strict_artifact_output_instruction_overrides_bad_json(self):
        instruction, contract = self.local_sdlc.strict_artifact_output_instruction({"bad_json"})

        self.assertIsNotNone(instruction)
        self.assertIsNotNone(contract)
        self.assertIn("Do not return JSON artifacts", instruction)
        self.assertIn("Do not return JSON artifacts", contract)
        self.assertIn("BEGIN_FILE", contract)

    def test_strict_artifact_output_instruction_blocks_json_plan_prefix(self):
        instruction, contract = self.local_sdlc.strict_artifact_output_instruction({"json_plan_before_artifact"})

        self.assertIsNotNone(instruction)
        self.assertIsNotNone(contract)
        self.assertIn("Do not return JSON", instruction)
        self.assertIn("No JSON plans", contract)
        self.assertNotIn("`{`", instruction)

    def test_strict_artifact_output_instruction_blocks_mixed_artifact_formats(self):
        instruction, contract = self.local_sdlc.strict_artifact_output_instruction({"mixed_artifact_formats"})

        self.assertIsNotNone(instruction)
        self.assertIsNotNone(contract)
        self.assertIn("MIXED ARTIFACT PROTOCOL REPAIR MODE", instruction)
        self.assertIn("BEGIN_SEARCH_REPLACE", contract)
        self.assertIn("existing-file repair", instruction)
        self.assertIn("Use BEGIN_FILE only", instruction)
        self.assertIn("No JSON", contract)
        self.assertIn("No mixed protocols", contract)

    def test_strict_artifact_output_instruction_forces_atomic_search_replace(self):
        instruction, contract = self.local_sdlc.strict_artifact_output_instruction({"atomic_search_replace_required"})

        self.assertIsNotNone(instruction)
        self.assertIsNotNone(contract)
        self.assertIn("ATOMIC SEARCH_REPLACE REPAIR MODE", instruction)
        self.assertIn("BEGIN_SEARCH_REPLACE", contract)
        self.assertIn("One file", contract)
        self.assertIn("No JSON", contract)
        self.assertIn("No diff", contract)

    def test_strict_artifact_output_instruction_forces_single_artifact_repair(self):
        instruction, contract = self.local_sdlc.strict_artifact_output_instruction({"single_artifact_required"})

        self.assertIsNotNone(instruction)
        self.assertIsNotNone(contract)
        self.assertIn("SINGLE ARTIFACT REPAIR MODE", instruction)
        self.assertIn("exactly one artifact", instruction)
        self.assertIn("No multiple files", contract)
        self.assertIn("No JSON", contract)

    def test_strict_artifact_output_instruction_blocks_begin_file_after_oversized_python(self):
        instruction, contract = self.local_sdlc.strict_artifact_output_instruction({"oversized_python_file_artifact"})

        self.assertIsNotNone(instruction)
        self.assertIsNotNone(contract)
        self.assertIn("ONE-FILE PYTHON ARTIFACT BUDGET MODE", instruction)
        self.assertIn("BEGIN_FILE", contract)
        self.assertIn("BEGIN_SEARCH_REPLACE", contract)
        self.assertIn("No diff", contract)
        self.assertIn("No multiple files", contract)

    def test_strict_artifact_output_instruction_uses_begin_file_after_skipped_diff(self):
        instruction, contract = self.local_sdlc.strict_artifact_output_instruction({"empty_or_skipped_patch", "oversized_python_file_artifact"})

        self.assertIsNotNone(instruction)
        self.assertIsNotNone(contract)
        self.assertIn("SKIPPED/OVERSIZED DIFF REPAIR MODE", instruction)
        self.assertIn("BEGIN_FILE", contract)
        self.assertIn("No diff", contract)

    def test_strict_artifact_output_instruction_uses_begin_file_after_oversized_diff(self):
        instruction, contract = self.local_sdlc.strict_artifact_output_instruction({"oversized_python_diff_artifact"})

        self.assertIsNotNone(instruction)
        self.assertIsNotNone(contract)
        self.assertIn("SKIPPED/OVERSIZED DIFF REPAIR MODE", instruction)
        self.assertIn("BEGIN_FILE", contract)
        self.assertIn("No diff", contract)

    def test_strict_artifact_output_instruction_uses_search_replace_after_corrupt_diff(self):
        instruction, contract = self.local_sdlc.strict_artifact_output_instruction({"corrupt_unified_diff"})

        self.assertIsNotNone(instruction)
        self.assertIsNotNone(contract)
        self.assertIn("CORRUPT DIFF EXISTING-FILE REPAIR MODE", instruction)
        self.assertIn("BEGIN_SEARCH_REPLACE", contract)
        self.assertIn("No BEGIN_FILE", contract)
        self.assertIn("No diff", contract)

    def test_strict_artifact_output_instruction_forbids_malformed_search_replace_retry(self):
        instruction, contract = self.local_sdlc.strict_artifact_output_instruction({"malformed_search_replace"})

        self.assertIsNotNone(instruction)
        self.assertIsNotNone(contract)
        self.assertIn("SEARCH_REPLACE FAILURE MODE", instruction)
        self.assertIn("After the path line", instruction)
        self.assertIn("BEGIN_SEARCH_REPLACE", contract)
        self.assertIn("No JSON", contract)

    def test_strict_artifact_output_instruction_handles_stage_scope_violation(self):
        instruction, contract = self.local_sdlc.strict_artifact_output_instruction({"stage_scope_violation"})

        self.assertIsNotNone(instruction)
        self.assertIsNotNone(contract)
        self.assertIn("STAGE SCOPE REPAIR MODE", instruction)
        self.assertIn("current stage", instruction)
        self.assertIn("No future-stage predicates", contract)

    def test_repair_advice_detects_unittest_pytest_mismatch(self):
        doc = self.local_sdlc.command_result_document(
            "python3 -m unittest discover -s tests",
            1,
            "",
            "ModuleNotFoundError: No module named 'pytest'\nFile \"/tmp/project/tests/test_minisqlite.py\", line 5, in <module>",
            0.01,
        )

        advice = self.local_sdlc.repair_advice_from_command_docs(
            [("Command result", doc)],
            ["python3 -m unittest discover -s tests"],
        )

        self.assertIsNotNone(advice)
        self.assertEqual(advice.strategy, "replace_test_harness")
        self.assertIn("tests/test_minisqlite.py", advice.focus_files)
        self.assertTrue(any("BEGIN_FILE" in item for item in advice.instructions))

    def test_repair_advice_focuses_shared_exception_definition(self):
        doc = """
        - repeated_failure_patterns:
          - count=4: exception: TypeError: SQLSyntaxError() takes no keyword arguments
        """

        advice = self.local_sdlc.repair_advice_from_command_docs(
            [("Observation summary", doc)],
            ["python3 -m unittest discover -s tests"],
        )

        self.assertIsNotNone(advice)
        assert advice is not None
        self.assertEqual(advice.strategy, "root_cause_patch")
        self.assertIn("minisqlite/errors.py", advice.focus_files)
        self.assertTrue(any("SQLSyntaxError" in item for item in advice.instructions))

    def test_repair_advice_policy_paths_allow_product_focus_only(self):
        advice = self.local_sdlc.RepairAdvice(
            strategy="root_cause_patch",
            focus_files=("tests/test_lexer.py", "minisqlite/errors.py", "missing.py"),
            instructions=(),
            evidence=(),
        )

        writable, readonly = self.local_sdlc.repair_advice_policy_paths(
            advice,
            ["minisqlite/errors.py", "tests/test_lexer.py"],
        )

        self.assertEqual(writable, ["minisqlite/errors.py"])
        self.assertEqual(readonly, ["tests/test_lexer.py"])

    def test_infers_product_focus_from_stage_test_path(self):
        focus = self.local_sdlc.inferred_product_focus_from_test_path("tests/test_pager.py")

        self.assertEqual(focus, ["minisqlite/storage/pager.py"])

    def test_repair_advice_infers_product_focus_from_test_traceback(self):
        doc = self.local_sdlc.command_result_document(
            "python3 -m unittest discover -s tests -p test_pager.py",
            1,
            "",
            (
                'Traceback (most recent call last):\n'
                '  File "/tmp/project/tests/test_pager.py", line 115, in test_next_page_id_persists\n'
                "AssertionError: 1 != 3\n"
            ),
            0.01,
        )

        advice = self.local_sdlc.repair_advice_from_command_docs(
            [("Command result", doc)],
            ["python3 -m unittest discover -s tests -p test_pager.py"],
        )

        self.assertIsNotNone(advice)
        assert advice is not None
        self.assertIn("minisqlite/storage/pager.py", advice.focus_files)
        self.assertNotIn("tests/test_pager.py", advice.focus_files)
        self.assertTrue(any("read-only executable evidence" in item for item in advice.instructions))

    def test_semantic_contracts_skip_cross_stage_test_harness_attribute_error(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            pager_path = project / "minisqlite" / "storage" / "pager.py"
            pager_path.parent.mkdir(parents=True)
            pager_path.write_text(
                "class Pager:\n"
                "    def create(self):\n"
                "        pass\n",
                encoding="utf-8",
            )
            doc = self.local_sdlc.command_result_document(
                "python3 -m unittest discover -s tests -p test_btree.py",
                1,
                "",
                (
                    'Traceback (most recent call last):\n'
                    '  File "/tmp/project/tests/test_btree.py", line 24, in setUp\n'
                    "    self.pager.init_db()\n"
                    "AttributeError: 'Pager' object has no attribute 'init_db'\n"
                ),
                0.01,
            )

            contracts = self.local_sdlc.extract_semantic_contracts_from_command_docs(
                [("Command result", doc)],
                project,
            )

        self.assertFalse(any("Pager objects must expose" in contract.text for contract in contracts))

    def test_semantic_contracts_classify_non_lexer_len_assertions_as_cardinality(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            test_path = project / "tests" / "test_btree.py"
            test_path.parent.mkdir(parents=True)
            test_path.write_text(
                "import unittest\n\n"
                "class TestBTree(unittest.TestCase):\n"
                "    def test_large_insert_all_searchable(self):\n"
                "        results = []\n"
                "        num_rows = 200\n"
                "        self.assertEqual(len(results), num_rows)\n",
                encoding="utf-8",
            )
            doc = self.local_sdlc.command_result_document(
                "python3 -m unittest discover -s tests -p test_btree.py",
                1,
                "",
                (
                    'Traceback (most recent call last):\n'
                    f'  File "{test_path}", line 7, in test_large_insert_all_searchable\n'
                    "AssertionError: 151 != 200\n"
                ),
                0.01,
            )

            contracts = self.local_sdlc.extract_semantic_contracts_from_command_docs(
                [("Command result", doc)],
                project,
            )

        self.assertTrue(any("collection cardinality" in contract.text for contract in contracts))
        self.assertFalse(any("token counts" in contract.text for contract in contracts))

    def test_repair_advice_detects_cross_stage_test_harness_api_mismatch(self):
        doc = self.local_sdlc.command_result_document(
            "python3 -m unittest discover -s tests -p test_btree.py",
            1,
            "",
            (
                'Traceback (most recent call last):\n'
                '  File "/tmp/project/tests/test_btree.py", line 24, in setUp\n'
                "    self.pager.init_db()\n"
                "AttributeError: 'Pager' object has no attribute 'init_db'\n"
            ),
            0.01,
        )
        summary = """
        - repeated_failure_patterns:
          - count=9: exception: AttributeError: 'Pager' object has no attribute 'init_db'
        """

        advice = self.local_sdlc.repair_advice_from_command_docs(
            [("Command result", doc), ("Observation summary", summary)],
            ["python3 -m unittest discover -s tests -p test_btree.py"],
        )

        self.assertIsNotNone(advice)
        assert advice is not None
        self.assertEqual(advice.strategy, "test_harness_api_mismatch")
        self.assertIn("tests/test_btree.py", advice.focus_files)
        self.assertIn("minisqlite/storage/pager.py", advice.focus_files)
        self.assertIn("minisqlite/storage/btree.py", advice.focus_files)
        self.assertTrue(any("test-harness API mismatch" in item for item in advice.instructions))
        self.assertTrue(any("do not add compatibility methods" in item for item in advice.instructions))

    def test_repair_advice_treats_product_attribute_reference_as_product_regression(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "minisqlite" / "storage").mkdir(parents=True)
            (project / "tests").mkdir()
            (project / "minisqlite" / "connection.py").write_text(
                "def load(conn):\n    return conn.pager.read_schema_metadata()\n",
                encoding="utf-8",
            )
            (project / "minisqlite" / "storage" / "pager.py").write_text(
                "class Pager:\n    pass\n",
                encoding="utf-8",
            )
            (project / "tests" / "test_cli.py").write_text("def test_cli(): pass\n", encoding="utf-8")
            doc = self.local_sdlc.command_result_document(
                "python3 -m unittest discover -s tests",
                1,
                "",
                (
                    'Traceback (most recent call last):\n'
                    '  File "/tmp/project/tests/test_cli.py", line 30, in test_create_table\n'
                    '  File "/tmp/project/minisqlite/connection.py", line 2, in load\n'
                    "AttributeError: 'Pager' object has no attribute 'read_schema_metadata'\n"
                ),
                0.01,
            )

            advice = self.local_sdlc.repair_advice_from_command_docs(
                [("Command result", doc)],
                ["python3 -m unittest discover -s tests"],
                project=project,
            )

        self.assertIsNotNone(advice)
        assert advice is not None
        self.assertEqual(advice.strategy, "root_cause_patch")
        self.assertIn("minisqlite/connection.py", advice.focus_files)
        self.assertIn("minisqlite/storage/pager.py", advice.focus_files)
        self.assertTrue(any("product API/call-site inconsistency" in item for item in advice.instructions))
        self.assertTrue(any("Do not edit tests" in item for item in advice.instructions))

    def test_repair_advice_policy_paths_allows_test_harness_strategy_to_edit_tests(self):
        advice = self.local_sdlc.RepairAdvice(
            strategy="test_harness_api_mismatch",
            focus_files=("tests/test_btree.py", "minisqlite/storage/pager.py", "minisqlite/storage/btree.py"),
            instructions=(),
            evidence=(),
        )

        writable, readonly = self.local_sdlc.repair_advice_policy_paths(
            advice,
            ["tests/test_btree.py", "minisqlite/storage/pager.py", "minisqlite/storage/btree.py"],
        )

        self.assertEqual(writable, ["tests/test_btree.py"])
        self.assertEqual(readonly, ["minisqlite/storage/pager.py", "minisqlite/storage/btree.py"])

    def test_repair_advice_specializes_lexer_operator_failures(self):
        doc = """
        ERROR: test_comparison_op_not_equals (test_lexer.TestLexer.test_comparison_op_not_equals)
        minisqlite.errors.SQLSyntaxError: Unexpected character '!' at position 0
        FAIL: test_multiple_statements (test_lexer.TestLexer.test_multiple_statements)
        AssertionError: False is not true
        """

        advice = self.local_sdlc.repair_advice_from_command_docs(
            [("Command result", doc)],
            ["python3 -m unittest discover -s tests -p test_lexer.py"],
        )

        self.assertIsNotNone(advice)
        assert advice is not None
        self.assertEqual(advice.strategy, "root_cause_patch")
        self.assertIn("minisqlite/sql/lexer.py", advice.focus_files)
        self.assertTrue(any("two-character comparison" in item for item in advice.instructions))
        self.assertTrue(any("SELECT *" in item for item in advice.instructions))

    def test_repair_advice_routes_residual_equals_after_multi_character_operator_to_lexer(self):
        doc = """
        ERROR: test_select_with_where_greater_equals (test_parser.TestParserSelect.test_select_with_where_greater_equals)
        minisqlite.errors.SQLSyntaxError: Expected a value (integer or text literal) but got EQUALS (value='=') at position 31
        ERROR: test_select_with_where_less_equals (test_parser.TestParserSelect.test_select_with_where_less_equals)
        minisqlite.errors.SQLSyntaxError: Expected a value (integer or text literal) but got EQUALS (value='=') at position 31
        ERROR: test_select_with_where_not_equals (test_parser.TestParserSelect.test_select_with_where_not_equals)
        minisqlite.errors.SQLSyntaxError: Expected a value (integer or text literal) but got EQUALS (value='=') at position 32
        """

        advice = self.local_sdlc.repair_advice_from_command_docs(
            [("Command result", doc)],
            ["python3 -m unittest discover -s tests -p test_parser.py"],
        )

        self.assertIsNotNone(advice)
        assert advice is not None
        self.assertEqual(advice.strategy, "root_cause_patch")
        self.assertIn("minisqlite/sql/lexer.py", advice.focus_files)
        self.assertTrue(any("multi-character operators consume both" in item for item in advice.instructions))

    def test_failure_analysis_focus_resolves_unique_basename(self):
        analysis = {
            "next_required_action": {
                "required_focus": ["lexer.py _emit() method", "tests are read-only"],
            }
        }

        paths = self.local_sdlc.focus_paths_from_failure_analysis(
            analysis,
            [
                "minisqlite/sql/lexer.py",
                "minisqlite/sql/parser.py",
                "tests/test_parser.py",
            ],
        )

        self.assertEqual(paths, ["minisqlite/sql/lexer.py"])

    def test_failure_analysis_focus_reads_active_constraints_and_goal(self):
        analysis = {
            "active_constraints": [
                "C2: The fix must be in minisqlite/sql/lexer.py - _emit advances by one for >=.",
            ],
            "next_required_action": {
                "goal": "Fix minisqlite/sql/lexer.py _emit position advancement.",
                "required_focus": [
                    "The _emit method's position advancement logic is wrong for multi-character string values.",
                ],
            },
        }

        paths = self.local_sdlc.focus_paths_from_failure_analysis(
            analysis,
            [
                "minisqlite/sql/lexer.py",
                "minisqlite/sql/parser.py",
                "tests/test_parser.py",
            ],
        )

        self.assertEqual(paths, ["minisqlite/sql/lexer.py"])

    def test_deterministic_replacement_builds_full_file_for_safe_all_occurrences(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            project = Path(temp)
            target = project / "tests" / "test_btree.py"
            target.parent.mkdir()
            target.write_text(
                "pager._allocate_page()\nself.assertEqual(pager._allocate_page(), 1)\n",
                encoding="utf-8",
            )
            analysis = {
                "next_required_action": {
                    "required_paths": ["tests/test_btree.py"],
                    "next_patch_type": "search_replace",
                    "minimal_patch_goal": "Replace all occurrences of pager._allocate_page() with pager.allocate_page() in tests/test_btree.py",
                }
            }
            policy = self.local_sdlc.ArtifactPathPolicy(
                allowed_paths=("tests/test_btree.py",),
                existing_paths=("tests/test_btree.py",),
            )

            result = self.local_sdlc.deterministic_replacement_artifact_from_failure_analysis(
                analysis,
                project,
                policy,
            )

        self.assertIsNotNone(result)
        artifact, summary = result
        self.assertTrue(artifact.startswith("BEGIN_FILE: tests/test_btree.py\n"))
        self.assertIn("pager.allocate_page()", artifact)
        self.assertNotIn("pager._allocate_page()", artifact)
        self.assertIn("occurrences: 2", summary)

    def test_deterministic_replacement_builds_search_replace_for_single_occurrence(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            project = Path(temp)
            target = project / "app.py"
            target.write_text("value = old_name()\n", encoding="utf-8")
            analysis = {
                "next_required_action": {
                    "required_paths": ["app.py"],
                    "next_patch_type": "search_replace",
                    "minimal_patch_goal": "Use new_name() instead of old_name() in app.py",
                }
            }
            policy = self.local_sdlc.ArtifactPathPolicy(
                allowed_paths=("app.py",),
                existing_paths=("app.py",),
            )

            result = self.local_sdlc.deterministic_replacement_artifact_from_failure_analysis(
                analysis,
                project,
                policy,
            )

        self.assertIsNotNone(result)
        artifact, _summary = result
        self.assertTrue(artifact.startswith("BEGIN_SEARCH_REPLACE: app.py\n"))
        self.assertIn("old_name()", artifact)
        self.assertIn("new_name()", artifact)

    def test_deterministic_replacement_builds_from_test_harness_repair_advice(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            project = Path(temp)
            target = project / "tests" / "test_btree.py"
            target.parent.mkdir()
            target.write_text(
                "page_id = pager._allocate_page()\nself.assertEqual(pager._allocate_page(), 1)\n",
                encoding="utf-8",
            )
            advice = {
                "strategy": "test_harness_api_mismatch",
                "focus_files": [
                    "tests/test_btree.py",
                    "minisqlite/storage/pager.py",
                ],
                "instructions": [
                    "Treat `Pager._allocate_page` as a generated test-harness API mismatch.",
                    "Multiple checks share one exception pattern: AttributeError: 'Pager' object has no attribute '_allocate_page'. Did you mean: 'allocate_page'?",
                ],
                "evidence": [
                    "project_policy_triage=test_harness:edit_test_harness",
                ],
            }
            policy = self.local_sdlc.ArtifactPathPolicy(
                allowed_paths=("tests/test_btree.py",),
                readonly_paths=("minisqlite/storage/pager.py",),
                existing_paths=("tests/test_btree.py", "minisqlite/storage/pager.py"),
            )

            result = self.local_sdlc.deterministic_replacement_artifact_from_repair_advice(
                advice,
                project,
                policy,
            )

        self.assertIsNotNone(result)
        artifact, summary = result
        self.assertTrue(artifact.startswith("BEGIN_FILE: tests/test_btree.py\n"))
        self.assertIn("pager.allocate_page()", artifact)
        self.assertNotIn("pager._allocate_page()", artifact)
        self.assertIn("repair_advice_strategy: test_harness_api_mismatch", summary)

    def test_deterministic_replacement_respects_readonly_policy(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            project = Path(temp)
            target = project / "tests" / "test_btree.py"
            target.parent.mkdir()
            target.write_text("pager._allocate_page()\n", encoding="utf-8")
            analysis = {
                "next_required_action": {
                    "required_paths": ["tests/test_btree.py"],
                    "next_patch_type": "search_replace",
                    "minimal_patch_goal": "Replace all occurrences of pager._allocate_page() with pager.allocate_page() in tests/test_btree.py",
                }
            }
            policy = self.local_sdlc.ArtifactPathPolicy(
                allowed_paths=(),
                readonly_paths=("tests/test_btree.py",),
                existing_paths=("tests/test_btree.py",),
            )

            result = self.local_sdlc.deterministic_replacement_artifact_from_failure_analysis(
                analysis,
                project,
                policy,
            )

        self.assertIsNone(result)

    def test_deterministic_python_syntax_repair_builds_exact_line_patch(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            project = Path(temp)
            target = project / "tests" / "test_cli.py"
            target.parent.mkdir()
            target.write_text(
                'import unittest\n'
                'class T(unittest.TestCase):\n'
                '    def test_output(self):\n'
                '        self.assertEqual(lines[1], "id|name|age"  # separator line\n',
                encoding="utf-8",
            )
            policy = self.local_sdlc.ArtifactPathPolicy(
                allowed_paths=("tests/test_cli.py",),
                existing_paths=("tests/test_cli.py",),
            )

            result = self.local_sdlc.deterministic_python_syntax_repair_artifact(
                project,
                policy,
                ("tests/test_cli.py",),
            )

        self.assertIsNotNone(result)
        artifact, summary = result
        self.assertTrue(artifact.startswith("BEGIN_SEARCH_REPLACE: tests/test_cli.py\n"))
        self.assertIn('self.assertEqual(lines[1], "id|name|age")  # separator line', artifact)
        self.assertIn("Deterministic Python Syntax Repair", summary)

    def test_deterministic_python_syntax_repair_respects_artifact_policy(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            project = Path(temp)
            target = project / "tests" / "test_cli.py"
            target.parent.mkdir()
            target.write_text(
                'def test_output():\n'
                '    self.assertEqual(lines[1], "id|name|age"  # separator line\n',
                encoding="utf-8",
            )
            policy = self.local_sdlc.ArtifactPathPolicy(
                allowed_paths=(),
                readonly_paths=("tests/test_cli.py",),
                existing_paths=("tests/test_cli.py",),
            )

            result = self.local_sdlc.deterministic_python_syntax_repair_artifact(
                project,
                policy,
                ("tests/test_cli.py",),
            )

        self.assertIsNone(result)

    def test_patch_plan_paths_resolves_required_and_evidence_paths(self):
        plan = textwrap.dedent(
            """
            PATCH_PLAN
            - proposition: If lexer emits split operators, change _emit so operators stay atomic
            - required_path: lexer.py
            - readonly_paths: tests/test_parser.py, minisqlite/sql/parser.py
            - forbidden_paths: tests/test_lexer.py
            - patch_type: search_replace
            - minimal_patch_goal: advance by token length for multi-character operators
            - stop_rule: stop if lexer.py is not visible
            """
        )

        paths = self.local_sdlc.patch_plan_paths_from_text(
            plan,
            [
                "minisqlite/sql/lexer.py",
                "minisqlite/sql/parser.py",
                "tests/test_parser.py",
                "tests/test_lexer.py",
            ],
        )

        self.assertEqual(paths["required_paths"], ["minisqlite/sql/lexer.py"])
        self.assertEqual(paths["readonly_paths"], ["tests/test_parser.py", "minisqlite/sql/parser.py"])
        self.assertEqual(paths["forbidden_paths"], ["tests/test_lexer.py"])

    def test_patch_plan_paths_ignores_ambiguous_basename(self):
        plan = textwrap.dedent(
            """
            PATCH_PLAN
            - proposition: If parser fails, change parser.py so it accepts the expression
            - required_path: parser.py
            - readonly_paths: (none)
            - forbidden_paths: (none)
            - patch_type: search_replace
            - minimal_patch_goal: parse expression
            - stop_rule: stop if parser.py is ambiguous
            """
        )

        paths = self.local_sdlc.patch_plan_paths_from_text(
            plan,
            [
                "pkg_a/parser.py",
                "pkg_b/parser.py",
            ],
        )

        self.assertEqual(paths["required_paths"], [])

    def test_repair_advice_classifies_cross_stage_constructor_shape_mismatch(self):
        doc = """
        ERROR: test_basic_create_table (test_parser.TestParserCreateTable.test_basic_create_table)
        Traceback (most recent call last):
          File "/tmp/project/tests/test_parser.py", line 10, in test_basic_create_table
            tokens = Lexer().tokenize(sql)
        TypeError: Lexer.__init__() missing 1 required positional argument: 'sql'
        """

        advice = self.local_sdlc.repair_advice_from_command_docs(
            [("Command result", doc)],
            ["python3 -m unittest discover -s tests -p test_parser.py"],
        )

        self.assertIsNotNone(advice)
        assert advice is not None
        self.assertEqual(advice.strategy, "test_harness_api_mismatch")
        self.assertIn("tests/test_parser.py", advice.focus_files)
        self.assertIn("minisqlite/sql/lexer.py", advice.focus_files)
        self.assertTrue(any("existing cross-stage API contract" in item for item in advice.instructions))

    def test_repair_advice_detects_missing_test_harness(self):
        doc = self.local_sdlc.command_result_document(
            "python3 -m unittest discover -s tests",
            1,
            "",
            "ImportError: Start directory is not importable: 'tests'\n",
            0.01,
        )

        failure_type = self.local_sdlc.classify_failure(1, "", "ImportError: Start directory is not importable: 'tests'")
        advice = self.local_sdlc.repair_advice_from_command_docs(
            [("Command result", doc)],
            ["python3 -m unittest discover -s tests"],
        )

        self.assertEqual(failure_type, "missing_test_harness")
        self.assertIsNotNone(advice)
        self.assertEqual(advice.strategy, "create_test_harness")
        self.assertIn("tests/__init__.py", advice.focus_files)
        self.assertTrue(any("unittest-compatible tests directory" in item for item in advice.instructions))

    def test_classify_assertion_not_found_as_test_assertion(self):
        stderr = """FAIL: test_root_split_creates_new_root (test_btree.TestBTree.test_root_split_creates_new_root)
Traceback (most recent call last):
  File "/tmp/project/tests/test_btree.py", line 101, in test_root_split_creates_new_root
    self.assertIsNotNone(result, f"Row {rowid} not found after root split")
AssertionError: unexpectedly None : Row 195 not found after root split

FAILED (failures=1)
"""

        failure_type = self.local_sdlc.classify_failure(1, "", stderr)

        self.assertEqual(failure_type, "test_assertion_failed")

    def test_repair_advice_detects_browser_public_api_missing(self):
        browser_doc = self.local_sdlc.command_result_document(
            "browser-tetris-smoke",
            1,
            "{\n  \"ok\": false\n}",
            "missing function startGame\nmissing function gameOver\ngameOver did not show GAME OVER",
            0.1,
        )
        html_doc = self.local_sdlc.command_result_document(
            "html-smoke tetris.html",
            1,
            "file: tetris.html",
            "initial board render is missing initBoard(); add initBoard(); immediately before the final startup renderBoard(); call",
            0.1,
        )

        advice = self.local_sdlc.repair_advice_from_command_docs(
            [("HTML smoke", html_doc), ("Browser smoke", browser_doc)],
            [],
        )

        self.assertIsNotNone(advice)
        assert advice is not None
        self.assertIn("tetris.html", advice.focus_files)
        self.assertTrue(any("window.startGame = startGame" in item for item in advice.instructions))
        self.assertTrue(any("startup initializes the DOM board" in item for item in advice.instructions))

    def test_acceptance_criteria_parse_plain_bullets_and_gate_unverified(self):
        spec = textwrap.dedent(
            """
            # Tetris

            ## Acceptance Conditions
            - Opening `tetris.html` shows the game screen.
            - Keyboard controls move, rotate, and drop the active piece.
            - Score, level, and line count update.

            ## Verification
            - HTML smoke passes.
            - browser-tetris-smoke passes.
            """
        )

        criteria = self.local_sdlc.parse_acceptance_criteria(spec)
        self.assertEqual([item["id"] for item in criteria], ["A01", "A02", "A03", "A04", "A05"])

        matrix = self.local_sdlc.build_acceptance_matrix(
            criteria,
            [
                {
                    "id": "E01",
                    "status": "pass",
                    "covers": [
                        "static_html",
                        "browser_smoke",
                        "html_visible",
                        "active_piece_visible",
                        "keyboard_interaction",
                    ],
                }
            ],
        )

        blockers = self.local_sdlc.acceptance_blockers(matrix)
        self.assertTrue(any(item["id"] == "A03" for item in blockers))
        self.assertEqual(next(item for item in matrix if item["id"] == "A02")["status"], "pass")

    def test_repair_advice_converts_acceptance_gate_blockers_to_actions(self):
        payload = {
            "ok": False,
            "blockers": [
                {
                    "id": "A01",
                    "text": "Keyboard operation moves the current piece",
                    "status": "unverified",
                    "required_covers": ["keyboard_interaction"],
                },
                {
                    "id": "A02",
                    "text": "Score, level, and line count update",
                    "status": "unverified",
                    "required_covers": ["score_update", "line_clear"],
                },
            ],
        }
        doc = self.local_sdlc.command_result_document(
            "acceptance-evidence-gate",
            1,
            json.dumps(payload, ensure_ascii=False),
            "A01: unverified\nA02: unverified",
            0.0,
        )

        advice = self.local_sdlc.repair_advice_from_command_docs([("gate", doc)], [])

        self.assertIsNotNone(advice)
        assert advice is not None
        self.assertEqual(advice.strategy, "acceptance_gap_patch")
        self.assertIn("tetris.html", advice.focus_files)
        self.assertTrue(any("Acceptance evidence gate failed" in item for item in advice.instructions))
        self.assertTrue(any("Keyboard operation moves" in item for item in advice.instructions))
        self.assertTrue(any("score, level, and line counters" in item for item in advice.instructions))
        actions = self.local_sdlc.repair_actions_from_advice(advice, [("gate", doc)])
        self.assertEqual(len(actions), 2)
        self.assertEqual(actions[0].kind, "produce_acceptance_evidence")
        self.assertEqual(actions[0].source, "acceptance_gate")
        self.assertEqual(actions[0].required_covers, ("keyboard_interaction",))
        self.assertIn("Keyboard operation moves", actions[0].instruction)
        manifest = self.local_sdlc.repair_advice_to_manifest(advice, [("gate", doc)])
        self.assertEqual(manifest["repair_actions"][0]["id"], "R01")
        self.assertEqual(manifest["repair_actions"][1]["required_covers"], ["score_update", "line_clear"])

    def test_browser_tetris_smoke_requires_visible_active_piece(self):
        if not (
            self.local_sdlc.shutil.which("chromium")
            or self.local_sdlc.shutil.which("google-chrome")
            or self.local_sdlc.shutil.which("chromium-browser")
        ):
            self.skipTest("chromium is not available")
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            run_dir = project / "run"
            run_dir.mkdir()
            (project / "tetris.html").write_text(
                textwrap.dedent(
                    """
                    <!doctype html>
                    <html>
                    <body>
                      <button id="start-btn">Start</button>
                      <div id="score">0</div><div id="level">1</div><div id="lines">0</div>
                      <div id="game-board"></div>
                      <div class="overlay-title"></div>
                      <script>
                        const board = document.getElementById('game-board');
                        for (let i = 0; i < 200; i++) board.appendChild(document.createElement('div')).className = 'cell';
                        function startGame() {}
                        function gameLoop() {}
                        function movePiece() {}
                        function rotate() {}
                        function softDrop() {}
                        function hardDrop() {}
                        function clearLines() {}
                        function gameOver() { document.querySelector('.overlay-title').textContent = 'GAME OVER'; }
                        Object.assign(window, {startGame, gameLoop, movePiece, rotate, softDrop, hardDrop, clearLines, gameOver});
                      </script>
                    </body>
                    </html>
                    """
                ).strip(),
                encoding="utf-8",
            )

            result = self.local_sdlc.run_browser_tetris_check(project, "tetris.html", run_dir, 10.0)

        self.assertIsNotNone(result)
        doc, ok = result
        self.assertFalse(ok)
        self.assertIn("active piece is not visible after start", doc)

    def test_repair_advice_detects_binary_struct_layout_mismatch(self):
        doc = self.local_sdlc.command_result_document(
            "python3 -m unittest discover -s tests -p test_record.py",
            1,
            "",
            """
            FAIL: test_decode_integer_negative
            AssertionError: 156 != -100
            FAIL: test_roundtrip_integer (value=-1)
            AssertionError: 255 != -1
            FAIL: test_decode_text
            AssertionError: '\\x00\\x00\\x00wo' != 'world'
            ERROR: test_roundtrip_utf8_japanese
            UnicodeDecodeError: 'utf-8' codec can't decode byte 0xf0 in position 3: unexpected end of data
            """,
            0.1,
        )

        advice = self.local_sdlc.repair_advice_from_command_docs(
            [("Command result", doc)],
            ["python3 -m unittest discover -s tests -p test_record.py"],
        )

        self.assertIsNotNone(advice)
        assert advice is not None
        self.assertEqual(advice.strategy, "binary_struct_layout_patch")
        self.assertIn("minisqlite/storage/record.py", advice.focus_files)
        self.assertTrue(any(">Bq" in item for item in advice.instructions))
        self.assertTrue(any(">BI" in item for item in advice.instructions))
        self.assertTrue(any("do not reverse" in item for item in advice.instructions))

    def test_python_struct_probe_records_authoritative_calcsize_facts(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            target = project / "minisqlite" / "storage"
            target.mkdir(parents=True)
            (target / "btree.py").write_text(
                textwrap.dedent(
                    """
                    import struct

                    LEAF_HEADER_SIZE = 16

                    def serialize():
                        return struct.pack(
                            ">BBHHHHxx",
                            1,
                            1,
                            0,
                            0,
                            0,
                            LEAF_HEADER_SIZE,
                        )

                    def deserialize(data):
                        return struct.unpack(
                            ">BBHHIHxx",
                            data[:LEAF_HEADER_SIZE],
                        )
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            doc = self.local_sdlc.command_result_document(
                "python3 -m unittest discover -s tests -p test_btree.py",
                1,
                "",
                f"""
                struct.error: unpack requires a buffer of 14 bytes
                  File "{target / "btree.py"}", line 9, in deserialize
                    return struct.unpack(">BBHHIHxx", data[:LEAF_HEADER_SIZE])
                """,
                0.1,
            )

            probe = self.local_sdlc.python_struct_probe_document(project, [("Command result", doc)])

        self.assertIsNotNone(probe)
        assert probe is not None
        self.assertIn("Mechanical Probe: Python struct formats", probe)
        self.assertIn("struct.pack format `>BBHHHHxx` calcsize=12", probe)
        self.assertIn("struct.unpack format `>BBHHIHxx` calcsize=14", probe)
        self.assertIn("LEAF_HEADER_SIZE=16", probe)
        self.assertIn("authoritative", probe)

    def test_repair_advice_uses_mechanical_struct_probe_as_product_evidence(self):
        command_doc = self.local_sdlc.command_result_document(
            "python3 -m unittest discover -s tests -p test_btree.py",
            1,
            "",
            """
            ERROR: test_serialize_deserialize
              File "/tmp/project/tests/test_btree.py", line 115, in test_serialize_deserialize
                restored = LeafPage.deserialize(data, 1)
              File "/tmp/project/minisqlite/storage/btree.py", line 69, in deserialize
                struct.unpack(">BBHHIHxx", data[:LEAF_HEADER_SIZE])
            struct.error: unpack requires a buffer of 14 bytes
            """,
            0.1,
        )
        probe_doc = """
        ## Mechanical Probe: Python struct formats

        - status: PASS
        - rule: `struct.calcsize(format)` is authoritative for Python struct byte sizes.
        - calcsize_facts:
        - minisqlite/storage/btree.py:45 struct.pack format `>BBHHHHxx` calcsize=14
        - minisqlite/storage/btree.py:69 struct.unpack format `>BBHHIHxx` calcsize=14
        - size_constants:
        - minisqlite/storage/btree.py:8 LEAF_HEADER_SIZE=16
        """

        advice = self.local_sdlc.repair_advice_from_command_docs(
            [("Command result", command_doc), ("Mechanical struct probe", probe_doc)],
            ["python3 -m unittest discover -s tests -p test_btree.py"],
        )

        self.assertIsNotNone(advice)
        assert advice is not None
        self.assertEqual(advice.strategy, "root_cause_patch")
        self.assertIn("minisqlite/storage/btree.py", advice.focus_files)
        self.assertTrue(any("Mechanical Probe calcsize facts" in item for item in advice.instructions))
        self.assertTrue(any("pack/unpack formats" in item for item in advice.instructions))
        self.assertTrue(any("mechanical struct probe" in item for item in advice.evidence))

    def test_python_struct_probe_runs_for_header_size_mismatch_without_struct_trace_text(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            target = project / "pkg"
            target.mkdir()
            (target / "page.py").write_text(
                textwrap.dedent(
                    """
                    import struct

                    HEADER_SIZE = 16

                    def serialize():
                        header = struct.pack(
                            ">BBHIIH",
                            1,
                            1,
                            0,
                            0,
                            0,
                            HEADER_SIZE,
                        )
                        if len(header) != HEADER_SIZE:
                            raise ValueError(f"header size mismatch: {len(header)} != {HEADER_SIZE}")
                        return header
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            doc = self.local_sdlc.command_result_document(
                "python3 -m unittest discover",
                1,
                "",
                f"""
                ValueError: header size mismatch: 14 != 16
                  File "{target / "page.py"}", line 15, in serialize
                    raise ValueError("header size mismatch")
                """,
                0.1,
            )

            probe = self.local_sdlc.python_struct_probe_document(project, [("Command result", doc)])

        self.assertIsNotNone(probe)
        assert probe is not None
        self.assertIn("pkg/page.py", probe)
        self.assertIn("struct.pack format `>BBHIIH` calcsize=14", probe)
        self.assertIn("HEADER_SIZE=16", probe)

    def test_python_api_probe_records_class_surface_for_attribute_error(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            target = project / "pkg"
            target.mkdir()
            (target / "tree.py").write_text(
                textwrap.dedent(
                    """
                    class BPlusTree:
                        def __init__(self) -> None:
                            pass

                        def search(self, rowid: int):
                            return None

                        def scan_all(self):
                            return []
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            doc = self.local_sdlc.command_result_document(
                "python3 -m unittest",
                1,
                "",
                f"""
                Traceback (most recent call last):
                  File "{target / "tree.py"}", line 2, in <module>
                AttributeError: 'BPlusTree' object has no attribute 'init_from_page'
                """,
                0.1,
            )

            probe = self.local_sdlc.python_api_probe_document(project, [("Command result", doc)])

        self.assertIsNotNone(probe)
        assert probe is not None
        self.assertIn("Mechanical Probe: Python API surface", probe)
        self.assertIn("class `BPlusTree`", probe)
        self.assertIn("`BPlusTree.__init__(self)`", probe)
        self.assertIn("`search(self, rowid)`", probe)
        self.assertIn("`BPlusTree.init_from_page` is absent", probe)

    def test_python_api_probe_records_embedded_error_and_public_attrs(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            target = project / "pkg"
            target.mkdir()
            (target / "connection.py").write_text(
                textwrap.dedent(
                    """
                    class Connection:
                        def __init__(self) -> None:
                            self.schema = object()
                            self.executor = object()

                        def execute(self, sql: str):
                            return None
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            doc = self.local_sdlc.command_result_document(
                "python3 -m unittest",
                1,
                "",
                "AssertionError: \"ERROR: 'Connection' object has no attribute '_schema'\" not expected",
                0.1,
            )

            probe = self.local_sdlc.python_api_probe_document(project, [("Command result", doc)])

        self.assertIsNotNone(probe)
        assert probe is not None
        self.assertIn("class `Connection`", probe)
        self.assertIn("public_attrs", probe)
        self.assertIn("`schema`", probe)
        self.assertIn("`Connection._schema` is absent", probe)

    def test_python_api_probe_records_constructor_signature_for_type_error(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            target = project / "pkg"
            target.mkdir()
            (target / "tree.py").write_text(
                textwrap.dedent(
                    """
                    class BPlusTree:
                        def __init__(self) -> None:
                            pass
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            doc = self.local_sdlc.command_result_document(
                "python3 -m unittest",
                1,
                "",
                f"""
                Traceback (most recent call last):
                  File "{target / "tree.py"}", line 2, in <module>
                TypeError: BPlusTree.__init__() takes 1 positional argument but 3 were given
                """,
                0.1,
            )

            probe = self.local_sdlc.python_api_probe_document(project, [("Command result", doc)])

        self.assertIsNotNone(probe)
        assert probe is not None
        self.assertIn("`BPlusTree.__init__(self)`", probe)

    def test_python_cli_probe_detects_dot_command_normalization_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            package = project / "pkg"
            package.mkdir()
            (package / "cli.py").write_text(
                textwrap.dedent(
                    '''
                    def _handle_dot_command(args):
                        cmd = args[0].lower()
                        if cmd == ".exit":
                            return "exit"
                        if cmd == ".tables":
                            return "tables"
                        return ""

                    def main():
                        line = ".tables"
                        args = line[1:].split()
                        return _handle_dot_command(args)
                    '''
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            doc = self.local_sdlc.command_result_document(
                "python3 -m unittest",
                1,
                "",
                "AssertionError: 't' not found in 'Unknown dot command: tables\\nUnknown dot command: exit\\n'",
                0.1,
            )

            probe = self.local_sdlc.python_cli_probe_document(project, [("Command result", doc)])

        self.assertIsNotNone(probe)
        assert probe is not None
        self.assertIn("Mechanical Probe: CLI command contracts", probe)
        self.assertIn("line[1:].split()", probe)
        self.assertIn("dot-prefixed literals", probe)
        self.assertIn("tables", probe)

    def test_python_cli_probe_detects_same_db_sequence_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            tests_dir = project / "tests"
            tests_dir.mkdir()
            package = project / "minisqlite"
            package.mkdir()
            (package / "cli.py").write_text("def main(argv=None):\n    pass\n", encoding="utf-8")
            (package / "connection.py").write_text("class Connection:\n    pass\n", encoding="utf-8")
            (tests_dir / "test_cli.py").write_text(
                textwrap.dedent(
                    """
                    def test_insert_and_select(self):
                        main([self.db_file, "CREATE TABLE users (id INTEGER);"])
                        main([self.db_file, "INSERT INTO users (id) VALUES (1);"])
                        main([self.db_file, "SELECT * FROM users;"])

                    def test_schema_command(self):
                        conn = Connection(self.db_file)
                        conn.execute("CREATE TABLE t (x INTEGER);")
                        conn.close()
                    """
                ),
                encoding="utf-8",
            )
            doc = self.local_sdlc.command_result_document(
                "python3 -m unittest",
                1,
                "",
                """
                FAIL: test_insert_and_select
                AssertionError: 'id' not found in "ERROR: Table 'users' does not exist"
                FAIL: test_schema_command
                AssertionError: 'CREATE TABLE' not found in "ERROR: Table 't' does not exist"
                """,
                0.1,
            )

            probe = self.local_sdlc.python_cli_probe_document(project, [("Command result", doc)])

        self.assertIsNotNone(probe)
        assert probe is not None
        self.assertIn("same database path CLI calls", probe)
        self.assertIn("minisqlite/connection.py", probe)
        self.assertIn("dot commands must observe schema", probe)

    def test_python_cli_state_probe_observes_reopen_state_loss(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            package = project / "minisqlite"
            storage = package / "storage"
            storage.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
            (storage / "__init__.py").write_text("", encoding="utf-8")
            (storage / "pager.py").write_text(
                textwrap.dedent(
                    """
                    class Pager:
                        def __init__(self, path):
                            self.path = path
                            self.payload = None
                        def open(self):
                            pass
                        def write_schema_metadata(self, payload):
                            self.payload = payload
                        def read_schema_metadata(self):
                            return self.payload
                        def flush(self):
                            self.payload = None
                        def close(self):
                            pass
                    """
                ),
                encoding="utf-8",
            )
            (package / "connection.py").write_text(
                textwrap.dedent(
                    """
                    class Schema:
                        def __init__(self):
                            self.tables = {}

                    class Pager:
                        def __init__(self):
                            self.payload = None
                        def read_schema_metadata(self):
                            return self.payload

                    class Connection:
                        def __init__(self, path):
                            self.path = path
                            self._schema = Schema()
                            self.pager = Pager()
                        def execute(self, sql):
                            self._schema.tables["users"] = object()
                            self.pager.payload = b"{users: 1}"
                        def close(self):
                            pass
                    """
                ),
                encoding="utf-8",
            )
            doc = self.local_sdlc.command_result_document(
                "python3 -m unittest",
                1,
                "",
                "AssertionError: 'id|name' not found in \"ERROR: Table 'users' does not exist\"",
                0.1,
            )

            probe = self.local_sdlc.python_cli_state_probe_document(project, [("Command result", doc)])

        self.assertIsNotNone(probe)
        assert probe is not None
        self.assertIn("before_close_tables: ['users']", probe)
        self.assertIn("after_reopen_tables: []", probe)
        self.assertIn("after_reopen_schema_payload_len: 0", probe)
        self.assertIn("direct_after_write_schema_payload_len: 12", probe)
        self.assertIn("direct_after_flush_schema_payload_len: 0", probe)

    def test_python_storage_state_probe_observes_page_allocation_and_reopen_search(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            package = project / "minisqlite"
            storage = package / "storage"
            storage.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
            (storage / "__init__.py").write_text("", encoding="utf-8")
            (storage / "file_format.py").write_text("PAGE_SIZE = 256\n", encoding="utf-8")
            (storage / "record.py").write_text(
                textwrap.dedent(
                    """
                    class RecordCodec:
                        def encode(self, values):
                            return b"payload"
                    """
                ),
                encoding="utf-8",
            )
            (storage / "pager.py").write_text(
                textwrap.dedent(
                    """
                    from minisqlite.storage.file_format import PAGE_SIZE

                    class Pager:
                        def __init__(self, path):
                            self.path = path
                            self._file = open(path, "r+b") if __import__("os").path.exists(path) else open(path, "w+b")
                            self.next_page_id = 1
                            self._file.seek(0)
                            if not self._file.read(1):
                                self._file.write(b"\\0" * PAGE_SIZE)
                                self._file.flush()
                        def allocate_page(self):
                            page_id = self.next_page_id
                            self.next_page_id += 1
                            self.write_page(page_id, b"\\0" * PAGE_SIZE)
                            return page_id
                        def read_page(self, page_id):
                            self._file.seek(page_id * PAGE_SIZE)
                            data = self._file.read(PAGE_SIZE)
                            return data + b"\\0" * (PAGE_SIZE - len(data))
                        def write_page(self, page_id, data):
                            self._file.seek(page_id * PAGE_SIZE)
                            self._file.write(data[:PAGE_SIZE].ljust(PAGE_SIZE, b"\\0"))
                            self._file.flush()
                        def close(self):
                            self._file.close()
                    """
                ),
                encoding="utf-8",
            )
            (storage / "btree.py").write_text(
                textwrap.dedent(
                    """
                    from minisqlite.storage.file_format import PAGE_SIZE

                    class BPlusTree:
                        def __init__(self, pager, root_page_id):
                            self.pager = pager
                            self.root_page_id = root_page_id
                        def insert(self, rowid, payload):
                            self.pager.write_page(self.root_page_id, b"R" + payload)
                        def search(self, rowid):
                            data = self.pager.read_page(self.root_page_id)
                            if data[:1] == b"R":
                                return data[1:8]
                            return None
                    """
                ),
                encoding="utf-8",
            )
            doc = self.local_sdlc.command_result_document(
                "python3 -m unittest discover -s tests -p test_btree.py",
                1,
                "",
                "FAIL: test_persistence_after_close_reopen\nAssertionError: unexpectedly None",
                0.1,
            )

            probe = self.local_sdlc.python_storage_state_probe_document(project, [("Command result", doc)])

        self.assertIsNotNone(probe)
        assert probe is not None
        self.assertIn("first_allocate_page_id: 1", probe)
        self.assertIn("second_allocate_root_page_id: 2", probe)
        self.assertIn("search_page1_is_none: True", probe)
        self.assertIn("search_root_is_none: False", probe)
        self.assertIn("Root-cause reports must use the probed page IDs", probe)

    def test_deterministic_project_policy_triage_detects_generated_btree_test_oracle_conflict(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            tests_dir = project / "tests"
            tests_dir.mkdir()
            (tests_dir / "test_btree.py").write_text(
                textwrap.dedent(
                    """
                    def test_persistence_after_close_reopen():
                        pager1.allocate_page()  # header
                        root_page_id = pager1.allocate_page()
                        root_page_id_2 = 1
                    """
                ),
                encoding="utf-8",
            )
            (tests_dir / "test_pager.py").write_text(
                "self.assertEqual(page_ids, [1, 2, 3, 4, 5])\n",
                encoding="utf-8",
            )
            evidence = """
            ## Mechanical Probe: Python storage state

            - observations:
              - second_allocate_root_page_id: 2
              - reopen_next_page_id: 1
              - search_page1_is_none: True
              - search_root_is_none: False
            """

            triage = self.local_sdlc.deterministic_project_policy_triage_from_evidence(
                "generated_test_oracle_conflict",
                evidence,
                project,
                ["tests/test_btree.py"],
            )

        self.assertIsNotNone(triage)
        assert triage is not None
        self.assertEqual(triage["case_type"], "test_harness")
        self.assertEqual(triage["safe_next_action"], "edit_test_harness")
        self.assertEqual(triage["editable_paths"], ["tests/test_btree.py"])
        self.assertTrue(any("tests/test_pager.py" in item for item in triage["project_policy_basis"]))

    def test_patch_plan_rejects_formula_contradicted_by_storage_probe(self):
        probe = """
        ## Mechanical Probe: Python storage state

        - observations:
          - second_allocate_root_page_id: 2
          - reopen_next_page_id: 1
        """
        patch_plan = """
        PATCH_PLAN
        - proposition: use next_page_id - 1 as the root page
        - required_path: minisqlite/storage/btree.py
        - patch_type: search_replace
        """

        doc = self.local_sdlc.patch_plan_mechanical_probe_contradiction_document(
            patch_plan,
            [("Mechanical storage state probe", probe)],
        )

        self.assertIsNotNone(doc)
        assert doc is not None
        self.assertIn("mechanical_probe_contradiction", doc)
        self.assertIn("`reopen_next_page_id - 1` = 0", doc)
        self.assertIn("observed_root_page_id: 2", doc)

    def test_patch_plan_allows_formula_when_storage_probe_confirms_it(self):
        probe = """
        ## Mechanical Probe: Python storage state

        - observations:
          - second_allocate_root_page_id: 2
          - reopen_next_page_id: 3
        """
        patch_plan = """
        PATCH_PLAN
        - proposition: use next_page_id - 1 as the root page
        - required_path: minisqlite/storage/btree.py
        - patch_type: search_replace
        """

        doc = self.local_sdlc.patch_plan_mechanical_probe_contradiction_document(
            patch_plan,
            [("Mechanical storage state probe", probe)],
        )

        self.assertIsNone(doc)

    def test_repair_advice_uses_cli_probe_as_authoritative(self):
        doc = """
        ## Command Result

        ### stderr
        ```text
        AssertionError: 'CREATE TABLE' not found in 'Unknown dot command: schema\\nUnknown dot command: exit\\n'
        ```

        ## Mechanical Probe: CLI command contracts

        - source_files:
          - minisqlite/cli.py
        - facts:
          - minisqlite/cli.py strips the leading dot with `line[1:].split()` before dispatch, but handler comparisons use dot-prefixed literals ['.exit', '.tables']
          - observed unrecognized dot commands after stripping: schema, exit
        """

        advice = self.local_sdlc.repair_advice_from_command_docs(
            [("Command result", doc)],
            ["python3 -m unittest discover -s tests -p test_cli.py"],
        )

        self.assertIsNotNone(advice)
        assert advice is not None
        self.assertEqual(advice.strategy, "root_cause_patch")
        self.assertIn("minisqlite/cli.py", advice.focus_files)
        self.assertTrue(any("CLI command contracts" in item for item in advice.instructions))

    def test_repair_advice_demotes_cli_when_state_probe_points_below_cli(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "minisqlite" / "storage").mkdir(parents=True)
            for rel_path in (
                "minisqlite/cli.py",
                "minisqlite/__main__.py",
                "minisqlite/__init__.py",
                "minisqlite/connection.py",
                "minisqlite/storage/pager.py",
            ):
                (project / rel_path).write_text("", encoding="utf-8")
            doc = """
            AssertionError: 'id|name' not found in "ERROR: Table 'users' does not exist"

            ## Mechanical Probe: CLI state persistence

            - observations:
              - before_close_tables: ['users']
              - before_close_schema_payload_len: 248
              - after_reopen_schema_payload_len: 0
              - after_reopen_tables: []
              - direct_after_write_schema_payload_len: 12
              - direct_after_flush_schema_payload_len: 0
              - direct_after_reopen_schema_payload_len: 0
            """

            advice = self.local_sdlc.repair_advice_from_command_docs(
                [("Command result", doc)],
                ["python3 -m unittest discover -s tests -p test_cli.py"],
                project,
            )

        self.assertIsNotNone(advice)
        assert advice is not None
        self.assertIn("minisqlite/connection.py", advice.focus_files)
        self.assertIn("minisqlite/storage/pager.py", advice.focus_files)
        self.assertLess(
            advice.focus_files.index("minisqlite/storage/pager.py"),
            advice.focus_files.index("minisqlite/connection.py"),
        )
        self.assertNotIn("minisqlite/cli.py", advice.focus_files)
        self.assertTrue(any("_write_header or flush" in item for item in advice.instructions))
        self.assertTrue(any("Forbidden non-fixes" in item for item in advice.instructions))
        self.assertTrue(any("direct Pager probe" in item for item in advice.evidence))

    def test_repair_advice_routes_row_persistence_loss_below_cli(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "minisqlite" / "storage").mkdir(parents=True)
            (project / "minisqlite" / "engine").mkdir(parents=True)
            for rel_path in (
                "minisqlite/cli.py",
                "minisqlite/connection.py",
                "minisqlite/storage/pager.py",
                "minisqlite/storage/btree.py",
                "minisqlite/engine/executor.py",
            ):
                (project / rel_path).write_text("", encoding="utf-8")
            doc = """
            AssertionError: '1|Alice' not found in 'id|name\\n'

            ## Mechanical Probe: CLI state persistence

            - observations:
              - before_close_tables: ['users']
              - before_close_rows: [[1, 'Alice']]
              - before_close_schema_payload_len: 248
              - after_reopen_schema_payload_len: 248
              - after_reopen_tables: ['users']
              - after_reopen_rows: []
            """

            advice = self.local_sdlc.repair_advice_from_command_docs(
                [("Command result", doc)],
                ["python3 -m unittest discover -s tests -p test_cli.py"],
                project,
            )

        self.assertIsNotNone(advice)
        assert advice is not None
        self.assertIn("minisqlite/storage/btree.py", advice.focus_files)
        self.assertIn("minisqlite/storage/pager.py", advice.focus_files)
        self.assertNotIn("minisqlite/cli.py", advice.focus_files)
        self.assertTrue(any("row/page persistence" in item for item in advice.instructions))
        self.assertTrue(any("do not edit _write_header" in item for item in advice.instructions))
        self.assertFalse(any("_write_header or flush" in item for item in advice.instructions))
        self.assertTrue(any("row persistence probe" in item for item in advice.evidence))
        self.assertTrue(any("CLI-layer focus demoted" in item for item in advice.evidence))

    def test_repair_advice_uses_mechanical_api_probe_as_authoritative(self):
        doc = """
        ## Command Result

        ### stderr
        ```text
        AttributeError: 'BPlusTree' object has no attribute 'init_from_page'
        ```

        ## Mechanical Probe: Python API surface

        - source_files:
          - minisqlite/storage/btree.py
        - class_facts:
        - minisqlite/storage/btree.py:1 class `BPlusTree`
          - constructor: `BPlusTree.__init__(self)`
          - public_methods:
            - `search(self, rowid)`
        - absent_api_facts:
        - `BPlusTree.init_from_page` is absent from `minisqlite/storage/btree.py`
        """

        advice = self.local_sdlc.repair_advice_from_command_docs([("Command result", doc)], [], None)

        self.assertIsNotNone(advice)
        assert advice is not None
        self.assertEqual(advice.strategy, "root_cause_patch")
        rendered = self.local_sdlc.repair_advice_document(advice)
        self.assertIn("Mechanical Probe: Python API surface is authoritative", rendered)
        self.assertIn("Do not call methods listed as absent", rendered)
        self.assertIn("BPlusTree.init_from_page", rendered)

    def test_expected_exception_precondition_probe_extracts_sql_identifier_fact(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            tests_dir = project / "tests"
            tests_dir.mkdir()
            test_file = tests_dir / "test_connection.py"
            test_file.write_text(
                textwrap.dedent(
                    """
                    import unittest
                    from minisqlite.errors import SchemaError

                    class TestConnectionCRUD(unittest.TestCase):
                        def test_column_not_exists_error(self):
                            self.conn.execute(
                                "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);"
                            )
                            with self.assertRaises(SchemaError):
                                self.conn.execute("SELECT * FROM users WHERE badcol = 1;")
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            doc = self.local_sdlc.command_result_document(
                "python3 -m unittest discover -s tests -p test_connection.py",
                1,
                "",
                f"""
                FAIL: test_column_not_exists_error (test_connection.TestConnectionCRUD.test_column_not_exists_error)
                Traceback (most recent call last):
                  File "{test_file}", line 9, in test_column_not_exists_error
                    with self.assertRaises(SchemaError):
                AssertionError: SchemaError not raised
                """,
                0.1,
            )

            probe = self.local_sdlc.expected_exception_precondition_probe_document(
                project,
                [("Command result", doc)],
            )

        self.assertIsNotNone(probe)
        assert probe is not None
        self.assertIn("Mechanical Probe: Precondition validation", probe)
        self.assertIn("WHERE column `badcol` absent", probe)
        self.assertIn("empty table must still reject an invalid column", probe)

    def test_expected_exception_precondition_probe_detects_type_oracle_conflict(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            tests_dir = project / "tests"
            tests_dir.mkdir()
            test_file = tests_dir / "test_record.py"
            test_file.write_text(
                textwrap.dedent(
                    """
                    import unittest
                    from minisqlite.errors import TypeMismatchError
                    from minisqlite.storage.record import RecordCodec

                    class TestTypeValidation(unittest.TestCase):
                        def test_multiple_columns_invalid(self):
                            values = [1, 30, "Alice"]
                            expected_types = ["INTEGER", "INTEGER", "TEXT"]
                            with self.assertRaises(TypeMismatchError):
                                RecordCodec.validate_types(values, expected_types)
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            doc = self.local_sdlc.command_result_document(
                "python3 -m unittest discover -s tests -p test_record.py",
                1,
                "",
                f"""
                FAIL: test_multiple_columns_invalid (test_record.TestTypeValidation.test_multiple_columns_invalid)
                Traceback (most recent call last):
                  File "{test_file}", line 10, in test_multiple_columns_invalid
                    with self.assertRaises(TypeMismatchError):
                AssertionError: TypeMismatchError not raised
                """,
                0.1,
            )

            probe = self.local_sdlc.expected_exception_precondition_probe_document(
                project,
                [("Command result", doc)],
            )

        self.assertIsNotNone(probe)
        assert probe is not None
        self.assertIn("TEST_ORACLE_CONFLICT", probe)
        self.assertIn("all literal pairs satisfy declared type predicates", probe)

    def test_repair_advice_routes_type_oracle_conflict_to_test_harness(self):
        doc = """
        ## Command Result

        ### stderr
        ```text
        AssertionError: TypeMismatchError not raised
        ```

        ## Mechanical Probe: Precondition validation

        - status: PASS
        - precondition_facts:
        - tests/test_record.py:test_multiple_columns_invalid: TEST_ORACLE_CONFLICT candidate: assertRaises(TypeMismatchError) wraps RecordCodec.validate_types(values=[1, 30, 'Alice'], expected_types=['INTEGER', 'INTEGER', 'TEXT']), but all literal pairs satisfy declared type predicates: ["0:1->INTEGER", "1:30->INTEGER", "2:'Alice'->TEXT"]
        """

        advice = self.local_sdlc.repair_advice_from_command_docs(
            [("Command result", doc)],
            [],
            None,
            ("tests/test_record.py",),
        )

        self.assertIsNotNone(advice)
        assert advice is not None
        self.assertEqual(advice.strategy, "replace_test_harness")
        rendered = self.local_sdlc.repair_advice_document(advice)
        self.assertIn("generated test-oracle conflict", rendered)
        self.assertIn("Do not patch product code to reject inputs", rendered)

    def test_repair_advice_uses_precondition_probe_as_authoritative(self):
        doc = """
        ## Command Result

        ### stderr
        ```text
        AssertionError: SchemaError not raised
        ```

        ## Mechanical Probe: Precondition validation

        - status: PASS
        - precondition_facts:
        - tests/test_connection.py:test_column_not_exists_error: expects SchemaError for SQL WHERE column `badcol` absent from `users` columns ['age', 'id', 'name']
        - invariant:
          - For SQL-like WHERE predicates, validate referenced columns against schema before scanning rows; an empty table must still reject an invalid column.
        """

        advice = self.local_sdlc.repair_advice_from_command_docs([("Command result", doc)], [], None)

        self.assertIsNotNone(advice)
        assert advice is not None
        self.assertEqual(advice.strategy, "root_cause_patch")
        rendered = self.local_sdlc.repair_advice_document(advice)
        self.assertIn("Mechanical Probe: Precondition validation is authoritative", rendered)
        self.assertIn("validate referenced columns against schema before scanning rows", rendered)
        self.assertIn("Python comprehensions propagate exceptions", rendered)

    def test_repair_advice_detects_generated_binary_test_native_struct_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            test_dir = project / "tests"
            test_dir.mkdir()
            (test_dir / "test_record.py").write_text(
                textwrap.dedent(
                    """
                    import struct

                    def test_encode_positive_integer():
                        result = encode_value(42)
                        assert struct.unpack_from("q", result, 1)[0] == 42

                    def test_decode_ascii_text():
                        data = struct.pack("BI", 0x02, 5) + b"hello"
                        assert decode_value(data)[0] == "hello"
                    """
                ).strip(),
                encoding="utf-8",
            )
            doc = self.local_sdlc.command_result_document(
                "python3 -m unittest discover -s tests -p test_record.py",
                1,
                "",
                """
                FAIL: test_encode_positive_integer
                AssertionError: 3026418949592973312 != 42
                FAIL: test_encode_ascii_text
                AssertionError: 83886080 != 5
                FAIL: test_decode_ascii_text
                AssertionError: '\\x00\\x00\\x00he' != 'hello'
                """,
                0.1,
            )

            advice = self.local_sdlc.repair_advice_from_command_docs(
                [("Command result", doc)],
                ["python3 -m unittest discover -s tests -p test_record.py"],
                project,
                ["tests/test_record.py"],
            )

        self.assertIsNotNone(advice)
        assert advice is not None
        self.assertEqual(advice.strategy, "generated_binary_contract_alignment")
        self.assertIn("tests/test_record.py", advice.focus_files)
        self.assertIn("minisqlite/storage/record.py", advice.focus_files)
        self.assertTrue(any("stage-generated binary tests" in item for item in advice.instructions))
        self.assertTrue(any("explicit byte order" in item for item in advice.instructions))

    def test_repair_advice_policy_allows_generated_binary_product_and_test(self):
        advice = self.local_sdlc.RepairAdvice(
            strategy="generated_binary_contract_alignment",
            focus_files=("tests/test_record.py", "minisqlite/storage/record.py"),
            instructions=(),
            evidence=(),
        )

        writable, readonly = self.local_sdlc.repair_advice_policy_paths(
            advice,
            ["tests/test_record.py", "minisqlite/storage/record.py"],
        )

        self.assertEqual(writable, ["tests/test_record.py", "minisqlite/storage/record.py"])
        self.assertEqual(readonly, [])

    def test_repair_advice_detects_generated_test_import_api_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            record_dir = project / "minisqlite" / "storage"
            record_dir.mkdir(parents=True)
            (record_dir / "record.py").write_text(
                "def encode(values):\n    return b''\n\n"
                "def decode(payload):\n    return []\n",
                encoding="utf-8",
            )
            test_dir = project / "tests"
            test_dir.mkdir()
            (test_dir / "test_btree.py").write_text(
                "from minisqlite.storage.record import encode_record, decode_record\n",
                encoding="utf-8",
            )
            doc = self.local_sdlc.command_result_document(
                "python3 -m unittest discover -s tests -p test_btree.py",
                1,
                "",
                (
                    "Traceback (most recent call last):\n"
                    '  File "/tmp/project/tests/test_btree.py", line 14, in <module>\n'
                    "    from minisqlite.storage.record import encode_record, decode_record\n"
                    "ImportError: cannot import name 'encode_record' from "
                    "'minisqlite.storage.record' (/tmp/project/minisqlite/storage/record.py)\n"
                ),
                0.01,
            )

            advice = self.local_sdlc.repair_advice_from_command_docs(
                [("command", doc)],
                ["python3 -m unittest discover -s tests -p test_btree.py"],
                project,
                ["tests/test_btree.py"],
            )

        self.assertIsNotNone(advice)
        assert advice is not None
        self.assertEqual(advice.strategy, "generated_test_import_api_mismatch")
        self.assertIn("tests/test_btree.py", advice.focus_files)
        self.assertIn("minisqlite/storage/btree.py", advice.focus_files)
        self.assertNotIn("minisqlite/storage/record.py", advice.focus_files)
        self.assertTrue(any("encode as encode_record" in item for item in advice.instructions))
        self.assertTrue(any("decode as decode_record" in item for item in advice.instructions))

    def test_repair_advice_treats_same_stage_import_missing_as_product_regression(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            package_dir = project / "minisqlite"
            package_dir.mkdir(parents=True)
            (package_dir / "cli.py").write_text(
                "def _run_sql():\n    pass\n",
                encoding="utf-8",
            )
            test_dir = project / "tests"
            test_dir.mkdir()
            (test_dir / "test_cli.py").write_text(
                "from minisqlite.cli import main, _run_sql\n",
                encoding="utf-8",
            )
            doc = self.local_sdlc.command_result_document(
                "python3 -m unittest discover -s tests -p test_cli.py",
                1,
                "",
                (
                    "Traceback (most recent call last):\n"
                    '  File "/tmp/project/tests/test_cli.py", line 1, in <module>\n'
                    "    from minisqlite.cli import main, _run_sql\n"
                    "ImportError: cannot import name 'main' from "
                    "'minisqlite.cli' (/tmp/project/minisqlite/cli.py)\n"
                ),
                0.01,
            )

            advice = self.local_sdlc.repair_advice_from_command_docs(
                [("command", doc)],
                ["python3 -m unittest discover -s tests -p test_cli.py"],
                project,
                ["tests/test_cli.py"],
            )

        self.assertIsNotNone(advice)
        assert advice is not None
        self.assertEqual(advice.strategy, "root_cause_patch")
        self.assertIn("minisqlite/cli.py", advice.focus_files)
        self.assertNotIn("tests/test_cli.py", advice.focus_files)
        self.assertTrue(any("product public-API regression" in item for item in advice.instructions))

    def test_repair_advice_policy_allows_generated_test_import_fix(self):
        advice = self.local_sdlc.RepairAdvice(
            strategy="generated_test_import_api_mismatch",
            focus_files=("tests/test_btree.py", "minisqlite/storage/btree.py"),
            instructions=(),
            evidence=(),
        )

        writable, readonly = self.local_sdlc.repair_advice_policy_paths(
            advice,
            ["tests/test_btree.py", "minisqlite/storage/btree.py", "minisqlite/storage/record.py"],
        )

        self.assertEqual(writable, ["tests/test_btree.py", "minisqlite/storage/btree.py"])
        self.assertEqual(readonly, [])

    def test_normalize_legacy_file_artifact_path_removes_path_slash_prefix(self):
        self.assertEqual(
            self.local_sdlc.normalize_legacy_file_artifact_path("path/minisqlite/storage/btree.py"),
            "minisqlite/storage/btree.py",
        )

    def test_repair_advice_detects_attribute_didyoumean_product_patch(self):
        doc = self.local_sdlc.command_result_document(
            "python3 -m unittest discover -s tests -p test_btree.py",
            1,
            "",
            (
                'Traceback (most recent call last):\n'
                '  File "/tmp/project/tests/test_btree.py", line 65, in test_insert\n'
                '  File "/tmp/project/minisqlite/storage/btree.py", line 149, in insert\n'
                "    self._write_leaf_page(self.root_page_id, is_root=True, cells=[])\n"
                "AttributeError: 'BPlusTree' object has no attribute '_write_leaf_page'. "
                "Did you mean: '_build_leaf_page'?\n"
            ),
            0.01,
        )

        advice = self.local_sdlc.repair_advice_from_command_docs(
            [("command", doc)],
            ["python3 -m unittest discover -s tests -p test_btree.py"],
        )

        self.assertIsNotNone(advice)
        assert advice is not None
        self.assertEqual(advice.strategy, "attribute_didyoumean_patch")
        self.assertIn("minisqlite/storage/btree.py", advice.focus_files)
        self.assertTrue(any("_write_leaf_page" in item and "_build_leaf_page" in item for item in advice.instructions))
        self.assertTrue(any("identical search_replace" in item for item in advice.instructions))

    def test_repair_advice_demotes_test_focus_for_product_first_strategy(self):
        doc = self.local_sdlc.command_result_document(
            "python3 -m unittest discover -s tests -p test_cli.py",
            1,
            "",
            (
                "Traceback (most recent call last):\n"
                '  File "/tmp/project/tests/test_cli.py", line 81, in test_interactive_mode\n'
                '  File "/tmp/project/minisqlite/cli.py", line 106, in main\n'
                '    line = input("minisqlite> ")\n'
                "StopIteration\n"
            ),
            0.01,
        )

        advice = self.local_sdlc.repair_advice_from_command_docs(
            [("command", doc)],
            ["python3 -m unittest discover -s tests -p test_cli.py"],
        )

        self.assertIsNotNone(advice)
        assert advice is not None
        self.assertNotIn("tests/test_cli.py", advice.focus_files)
        self.assertIn("minisqlite/cli.py", advice.focus_files)
        self.assertTrue(any("readonly evidence" in item for item in advice.instructions))









    def test_non_artifact_output_detects_late_marker(self):
        output = "説明\n" + ("x" * (self.local_sdlc.ARTIFACT_OUTPUT_BUDGET_BYTES + 10)) + "\nBEGIN_FILE: app.py\nx\nEND_FILE"

        self.assertTrue(self.local_sdlc.is_non_artifact_output(output))

    def test_artifact_marker_ignores_prose_braces(self):
        output = (
            "Let me analyze why {type(payload).__name__} appears in the traceback.\n"
            + ("x" * (self.local_sdlc.ARTIFACT_OUTPUT_BUDGET_BYTES + 10))
        )

        self.assertEqual(self.local_sdlc.first_artifact_marker_offset(output), -1)
        self.assertTrue(self.local_sdlc.is_non_artifact_output(output))

    def test_stream_guard_aborts_long_prose_with_braces(self):
        output = (
            "Let me analyze why {type(payload).__name__} appears in the traceback.\n"
            + ("x" * 2200)
        )

        result = self.local_sdlc.artifact_stream_guard(output)

        self.assertTrue(result.should_abort)
        self.assertEqual(result.code, "stream_non_artifact_output")

    def test_artifact_marker_detects_json_artifact_envelope(self):
        output = 'preface\n{"artifacts":[{"type":"replace_file","path":"app.py","content":"x"}]}'

        self.assertEqual(self.local_sdlc.first_artifact_marker_offset(output), len("preface\n"))

    def test_failure_transition_routes_artifact_invalid_to_format_repair(self):
        transition = self.local_sdlc.transition_for_failure("artifact_invalid")

        self.assertEqual(transition.next_role, "format_repair")
        self.assertEqual(transition.owner, "supervisor")

    def test_failure_transition_routes_mixed_artifact_formats_to_format_repair(self):
        transition = self.local_sdlc.transition_for_failure("stream_mixed_artifact_formats")

        self.assertEqual(transition.next_role, "format_repair")
        self.assertEqual(transition.action, "abort_mixed_artifact_formats_and_choose_one_protocol")
        self.assertEqual(transition.owner, "runner")

    def test_failure_transition_routes_multiple_json_search_replace_to_atomic_repair(self):
        transition = self.local_sdlc.transition_for_failure("stream_multiple_json_search_replace")

        self.assertEqual(transition.next_role, "format_repair")
        self.assertEqual(transition.action, "abort_excess_or_cross_path_json_search_replace")
        self.assertEqual(transition.owner, "runner")

    def test_final_failure_focus_parses_product_and_test_paths(self):
        doc = self.local_sdlc.command_result_document(
            "python3 -m unittest discover -s tests",
            1,
            "",
            (
                'Traceback (most recent call last):\n'
                '  File "/tmp/project/tests/test_minisqlite.py", line 8, in test_select\n'
                '  File "/tmp/project/minisqlite/connection.py", line 42, in execute\n'
                "NameError: name 'Parser' is not defined\n"
            ),
            0.01,
        )

        advice = self.local_sdlc.final_failure_focus_from_command_docs(
            [("final", doc)],
            ["python3 -m unittest discover -s tests"],
        )

        self.assertIsNotNone(advice)
        self.assertIn("minisqlite/connection.py", advice.focus_files)
        self.assertIn("tests/test_minisqlite.py", advice.focus_files)
        self.assertTrue(any("product code" in item for item in advice.instructions))

    def test_judge_approved_uses_verdict_line_before_body_keywords(self):
        text = """## 判定: 承認

        Evidence passed.

        修正依頼時の再実行手順: 不要。
        """

        self.assertTrue(self.local_sdlc.judge_approved(text))

    def test_judge_approved_rejects_explicit_verdict(self):
        text = """## 判定: 修正依頼

        Required fixes:
        - Add the missing browser check.
        """

        self.assertFalse(self.local_sdlc.judge_approved(text))

    def test_judge_approved_reads_verdict_section_next_line(self):
        text = """## Verdict

判定: 承認

## Required fixes

なし。全要件が満たされている。

## Summary

次のステージへ進める。
"""

        self.assertTrue(self.local_sdlc.judge_approved(text))

    def test_judge_approved_does_not_reject_empty_required_fixes_heading(self):
        text = """判定: 承認

Evidence passed.

Required fixes:
なし
"""

        self.assertTrue(self.local_sdlc.judge_approved(text))

    def test_agent_short_circuits_when_initial_html_smoke_passes(self):
        calls = []

        class FakeClient:
            def __init__(self, config):
                self.config = config

            def complete(self, messages):
                calls.append(messages)
                return "pm control"

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            skills_dir = project / "skills"
            for name in ("sdlc", "tdd", "review"):
                skill_dir = skills_dir / name
                skill_dir.mkdir(parents=True)
                (skill_dir / "SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: {name}\n---\n# {name}\n",
                    encoding="utf-8",
                )
            (project / "SPEC.md").write_text("# SPEC\n", encoding="utf-8")
            (project / "index.html").write_text(
                "<!doctype html><html><body><script>const ok = true;</script></body></html>\n",
                encoding="utf-8",
            )
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "verify index.html",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--include",
                        "index.html",
                        "--apply",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(manifest["completed_rounds"], 0)
        self.assertEqual(manifest["final_verdict"], "approved")
        self.assertTrue(any(path.endswith("00-initial-html-smoke-01.md") for path in manifest["documents"]))

    def test_agent_skip_pm_uses_deterministic_control_doc(self):
        calls = []
        outputs = [
            "BEGIN_FILE: app.py\nprint('fixed')\nEND_FILE",
            "## 判定: 承認\n",
        ]

        class FakeClient:
            def __init__(self, config):
                self.config = config

            def complete(self, messages):
                calls.append(messages)
                return outputs[len(calls) - 1]

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            skills_dir = project / "skills"
            for name in ("sdlc", "tdd", "review"):
                skill_dir = skills_dir / name
                skill_dir.mkdir(parents=True)
                (skill_dir / "SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: {name}\n---\n# {name}\n",
                    encoding="utf-8",
                )
            (project / "SPEC.md").write_text("# SPEC\n", encoding="utf-8")
            (project / "app.py").write_text("print('old')\n", encoding="utf-8")
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "fix app",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--include",
                        "app.py",
                        "--apply",
                        "--skip-pm",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            pm_doc = (run_dir / "01-pm-control.md").read_text(encoding="utf-8")

        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(manifest["api_calls"], 2)
        self.assertIn("Deterministic PM Control", pm_doc)
        self.assertIn("Proposition Ledger", pm_doc)
        self.assertIn("C1: SPEC.md fixed requirements must be preserved", pm_doc)
        self.assertIn("coder-level agent", calls[0][0]["content"])

    def test_agent_runs_domain_contract_when_ddd_skill_exists(self):
        calls = []
        outputs = [
            "## Ubiquitous Language\n\n## Verification Proposition Contract\n",
            "BEGIN_FILE: app.py\nprint('fixed')\nEND_FILE",
            "## 判定: 承認\n",
        ]

        class FakeClient:
            def __init__(self, config):
                self.config = config

            def complete(self, messages):
                calls.append(messages)
                return outputs[len(calls) - 1]

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            skills_dir = project / "skills"
            for name in ("sdlc", "ddd", "tdd", "review"):
                skill_dir = skills_dir / name
                skill_dir.mkdir(parents=True)
                (skill_dir / "SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: {name}\n---\n# {name}\n",
                    encoding="utf-8",
                )
            (project / "SPEC.md").write_text("# SPEC\n", encoding="utf-8")
            (project / "app.py").write_text("print('old')\n", encoding="utf-8")
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "新しい検索機能を実装して",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--include",
                        "app.py",
                        "--apply",
                        "--skip-pm",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 3)
        self.assertIn("# ddd", calls[0][0]["content"])
        self.assertIn("# tdd", calls[1][0]["content"])
        self.assertIn("# review", calls[2][0]["content"])
        self.assertIn("Domain contract document", calls[1][1]["content"])
        self.assertEqual(manifest["api_calls"], 3)
        self.assertTrue(manifest["domain_modeling"]["ran"])
        self.assertEqual(manifest["domain_modeling"]["skill"], "ddd")

    def test_agent_command_only_judge_accepts_passing_commands(self):
        calls = []

        class FakeClient:
            def __init__(self, config):
                self.config = config

            def complete(self, messages):
                calls.append(messages)
                return "BEGIN_FILE: app.py\nprint('fixed')\nEND_FILE"

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            skills_dir = project / "skills"
            for name in ("sdlc", "tdd", "review"):
                skill_dir = skills_dir / name
                skill_dir.mkdir(parents=True)
                (skill_dir / "SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: {name}\n---\n# {name}\n",
                    encoding="utf-8",
                )
            (project / "SPEC.md").write_text("# SPEC\n", encoding="utf-8")
            (project / "app.py").write_text("print('old')\n", encoding="utf-8")
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "fix app",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--include",
                        "app.py",
                        "--apply",
                        "--skip-pm",
                        "--judge-mode",
                        "command-only",
                        "--test-command",
                        f"{sys.executable} -m py_compile app.py",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(manifest["api_calls"], 1)
        self.assertEqual(manifest["final_verdict"], "approved")

    def test_agent_repair_round_includes_generated_new_file_context(self):
        calls = []
        outputs = [
            "BEGIN_FILE: app.py\nif True print('broken')\nEND_FILE",
            "BEGIN_FILE: app.py\nprint('fixed')\nEND_FILE",
        ]

        class FakeClient:
            def __init__(self, config):
                self.config = config

            def complete(self, messages):
                calls.append(messages)
                return outputs[len(calls) - 1]

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            project.mkdir()
            skills_dir = Path(temp) / "skills"
            for name in ("sdlc", "tdd", "review"):
                skill_dir = skills_dir / name
                skill_dir.mkdir(parents=True)
                (skill_dir / "SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: {name}\n---\n# {name}\n",
                    encoding="utf-8",
                )
            (project / "SPEC.md").write_text("# SPEC\n", encoding="utf-8")
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "create and repair app",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--new-file",
                        "app.py",
                        "--require-path",
                        "app.py",
                        "--allow-no-context",
                        "--apply",
                        "--skip-pm",
                        "--judge-mode",
                        "command-only",
                        "--domain-modeling",
                        "never",
                        "--test-command",
                        f"{sys.executable} -m py_compile app.py",
                        "--max-rounds",
                        "2",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 2)
        self.assertIn("### app.py", calls[1][1]["content"])
        self.assertIn("if True print('broken')", calls[1][1]["content"])
        self.assertEqual(manifest["final_verdict"], "approved")

    def test_agent_precheck_adds_initial_failure_to_coder_context(self):
        calls = []

        class FakeClient:
            def __init__(self, config):
                self.config = config

            def complete(self, messages):
                calls.append(messages)
                return "BEGIN_FILE: app.py\nprint('fixed')\nEND_FILE"

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root)
            (project / "app.py").write_text("if True print('broken')\n", encoding="utf-8")
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "fix syntax",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--include",
                        "app.py",
                        "--apply",
                        "--skip-pm",
                        "--judge-mode",
                        "command-only",
                        "--precheck",
                        "--test-command",
                        f"{sys.executable} -m py_compile app.py",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertTrue(manifest["precheck"])
        self.assertTrue(any(path.endswith("00-initial-command-01.md") for path in manifest["documents"]))
        self.assertEqual(manifest["evidence"][0]["failure_type"], "syntax_error")
        self.assertIn("Initial command 1", calls[0][1]["content"])
        self.assertIn("SyntaxError", calls[0][1]["content"])

    def test_agent_precheck_pass_skips_coder(self):
        class FakeClient:
            def __init__(self, _config):
                pass

            def complete(self, _messages):
                raise AssertionError("coder should not be called when precheck already passes")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root)
            (project / "app.py").write_text("print('already ok')\n", encoding="utf-8")
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "verify app",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--include",
                        "app.py",
                        "--apply",
                        "--skip-pm",
                        "--judge-mode",
                        "command-only",
                        "--precheck",
                        "--test-command",
                        f"{sys.executable} -m py_compile app.py",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertEqual(manifest["final_verdict"], "approved")
        self.assertEqual(manifest["completed_rounds"], 0)
        self.assertTrue(any(path.endswith("01-initial-verification.md") for path in manifest["documents"]))

    def test_agent_accepts_json_artifacts_when_requested(self):
        calls = []

        class FakeClient:
            def __init__(self, config):
                self.config = config

            def complete(self, messages):
                calls.append(messages)
                return json.dumps(
                    {
                        "artifacts": [
                            {"type": "replace_file", "path": "app.py", "content": "print('fixed')\n"}
                        ]
                    }
                )

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root)
            (project / "app.py").write_text("print('old')\n", encoding="utf-8")
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "fix app",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--include",
                        "app.py",
                        "--apply",
                        "--skip-pm",
                        "--judge-mode",
                        "command-only",
                        "--artifact-format",
                        "json",
                        "--test-command",
                        f"{sys.executable} -m py_compile app.py",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            fixed = (project / "app.py").read_text(encoding="utf-8")

        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(fixed, "print('fixed')\n")
        self.assertEqual(manifest["artifact_format"], "json")
        self.assertEqual(manifest["final_verdict"], "approved")

    def test_agent_rejects_edits_to_readonly_context(self):
        calls = []

        class FakeClient:
            def __init__(self, config):
                self.config = config

            def complete(self, messages):
                calls.append(messages)
                return json.dumps(
                    {
                        "artifacts": [
                            {"type": "replace_file", "path": "base.py", "content": "VALUE = 'bad'\n"}
                        ]
                    }
                )

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root)
            (project / "app.py").write_text("from base import VALUE\n", encoding="utf-8")
            (project / "base.py").write_text("VALUE = 'base'\n", encoding="utf-8")
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "fix app using base as readonly context",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--include",
                        "app.py",
                        "--context",
                        "base.py",
                        "--apply",
                        "--skip-pm",
                        "--judge-mode",
                        "command-only",
                        "--max-rounds",
                        "1",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            base_text = (project / "base.py").read_text(encoding="utf-8")
            patch_failure = (run_dir / "03-r01-patch-failure.md").read_text(encoding="utf-8")

        self.assertEqual(result, 1)
        self.assertEqual(base_text, "VALUE = 'base'\n")
        self.assertEqual(manifest["context_paths"], ["base.py"])
        self.assertIn("JSON artifact path is read-only: base.py", patch_failure)

    def test_agent_small_patch_instruction_is_prompted(self):
        calls = []

        class FakeClient:
            def __init__(self, config):
                self.config = config

            def complete(self, messages):
                calls.append(messages)
                return json.dumps(
                    {
                        "artifacts": [
                            {
                                "type": "search_replace",
                                "path": "app.py",
                                "search": "VALUE = 'old'\n",
                                "replace": "VALUE = 'new'\n",
                            }
                        ]
                    }
                )

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root)
            (project / "app.py").write_text("VALUE = 'old'\n", encoding="utf-8")
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "fix app with a small patch",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--include",
                        "app.py",
                        "--apply",
                        "--skip-pm",
                        "--judge-mode",
                        "command-only",
                        "--small-patch",
                        "--test-command",
                        f"{sys.executable} -m py_compile app.py",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            fixed = (project / "app.py").read_text(encoding="utf-8")

        self.assertEqual(result, 0)
        self.assertEqual(fixed, "VALUE = 'new'\n")
        self.assertTrue(manifest["small_patch"])
        self.assertIn("Small patch mode:", calls[0][1]["content"])
        self.assertIn("Writable targets:\n            app.py", calls[0][1]["content"])

    def test_agent_no_replace_file_rejects_whole_file_json_artifact(self):
        calls = []

        class FakeClient:
            def __init__(self, config):
                self.config = config

            def complete(self, messages):
                calls.append(messages)
                return json.dumps(
                    {
                        "artifacts": [
                            {"type": "replace_file", "path": "app.py", "content": "print('new')\n"}
                        ]
                    }
                )

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root)
            (project / "app.py").write_text("print('old')\n", encoding="utf-8")
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "fix app locally",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--include",
                        "app.py",
                        "--apply",
                        "--skip-pm",
                        "--judge-mode",
                        "command-only",
                        "--artifact-format",
                        "json",
                        "--no-replace-file",
                        "--max-rounds",
                        "1",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            patch_failure = (run_dir / "03-r01-patch-failure.md").read_text(encoding="utf-8")

        self.assertEqual(result, 1)
        self.assertTrue(manifest["no_replace_file"])
        self.assertIn("replace_file artifacts are disabled", patch_failure)
        self.assertIn("Replace-file artifacts are disabled", calls[0][1]["content"])

    def test_agent_manifest_records_acceptance_matrix_and_failure_classifier(self):
        calls = []

        class FakeClient:
            def __init__(self, config):
                self.config = config

            def complete(self, messages):
                calls.append(messages)
                return "BEGIN_FILE: app.py\nif True print('broken')\nEND_FILE"

        spec = "# SPEC\n\n## 受け入れ条件\n- [ ] `py_compile app.py` が PASS する\n"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root, spec)
            (project / "app.py").write_text("print('old')\n", encoding="utf-8")
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "break app",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--include",
                        "app.py",
                        "--apply",
                        "--skip-pm",
                        "--judge-mode",
                        "command-only",
                        "--max-rounds",
                        "1",
                        "--test-command",
                        f"{sys.executable} -m py_compile app.py",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(result, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(manifest["final_verdict"], "test_failed")
        self.assertEqual(manifest["failure_summary"]["failure_type"], "syntax_error")
        self.assertEqual(manifest["evidence"][0]["failure_type"], "syntax_error")
        self.assertEqual(manifest["acceptance_matrix"][0]["status"], "fail")
        self.assertEqual(manifest["acceptance_matrix"][0]["evidence_ids"], ["E01"])

    def test_agent_records_acceptance_repair_actions_for_next_round(self):
        calls = []

        class FakeClient:
            def __init__(self, config):
                self.config = config

            def complete(self, messages):
                calls.append(messages)
                return (
                    "BEGIN_FILE: tetris.html\n"
                    "<!doctype html><html><body><button id=\"start-btn\">Start</button></body></html>\n"
                    "END_FILE"
                )

        spec = "\n".join(
            [
                "# SPEC",
                "",
                "## 受け入れ条件",
                "- Keyboard operation moves the current piece",
                "- Score, level, and line count update",
            ]
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root, spec)
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "create tetris",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--new-file",
                        "tetris.html",
                        "--require-path",
                        "tetris.html",
                        "--apply",
                        "--skip-pm",
                        "--judge-mode",
                        "command-only",
                        "--max-rounds",
                        "2",
                        "--protocol-repair-rounds",
                        "0",
                        "--adaptive-rounds",
                        "0",
                        "--root-cause-patch-rounds",
                        "0",
                        "--domain-modeling",
                        "never",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(result, 1)
        self.assertGreaterEqual(len(calls), 2)
        actions = manifest["repair_advice"]["repair_actions"]
        self.assertTrue(any(action["kind"] == "produce_acceptance_evidence" for action in actions))
        self.assertTrue(any("keyboard_interaction" in action["required_covers"] for action in actions))
        self.assertIn("R01 produce_acceptance_evidence", calls[1][1]["content"])
        self.assertIn("Keyboard operation moves", calls[1][1]["content"])

    def test_agent_records_python_probe_evidence_after_command_failure(self):
        calls = []

        class FakeClient:
            def __init__(self, config):
                self.config = config

            def complete(self, messages):
                calls.append(messages)
                return (
                    "BEGIN_FILE: storage.py\n"
                    "import struct\n"
                    "HEADER_FORMAT = 'II'\n"
                    "HEADER_SIZE = 16\n"
                    "struct.pack('II', 1, 2)\n"
                    "END_FILE"
                )

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root)
            storage_path = project / "storage.py"
            storage_path.write_text(
                "import struct\nHEADER_FORMAT = 'II'\nHEADER_SIZE = 16\nstruct.pack('II', 1, 2)\n",
                encoding="utf-8",
            )
            (project / "fail_struct.py").write_text(
                textwrap.dedent(
                    """
                    import pathlib
                    import sys

                    path = pathlib.Path("storage.py").resolve()
                    print(f'File "{path}", line 2, in <module>', file=sys.stderr)
                    print("header size mismatch", file=sys.stderr)
                    sys.exit(1)
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "fix storage",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--include",
                        "storage.py",
                        "--apply",
                        "--skip-pm",
                        "--judge-mode",
                        "command-only",
                        "--test-command",
                        f"{sys.executable} fail_struct.py",
                        "--max-rounds",
                        "1",
                        "--protocol-repair-rounds",
                        "0",
                        "--adaptive-rounds",
                        "0",
                        "--root-cause-patch-rounds",
                        "0",
                        "--domain-modeling",
                        "never",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

        mechanical = [item for item in manifest["evidence"] if item.get("kind") == "mechanical_probe"]
        self.assertEqual(result, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(manifest["final_verdict"], "test_failed")
        self.assertTrue(any("python_struct" in item.get("covers", []) for item in mechanical))
        self.assertTrue(any(item["document"].endswith("mechanical-probe-struct.md") for item in mechanical))
        self.assertTrue(any(path.endswith("mechanical-probe-struct.md") for path in manifest["documents"]))

    def test_required_path_checks_missing_artifact(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "empty.txt").write_text("", encoding="utf-8")

            results = self.local_sdlc.run_required_path_checks(project, ["missing.txt", "empty.txt"])

        self.assertEqual(len(results), 2)
        self.assertFalse(results[0][1])
        self.assertFalse(results[1][1])
        self.assertIn("required path missing", results[0][0])
        self.assertIn("required path is empty", results[1][0])
        self.assertEqual(
            self.local_sdlc.evidence_from_command_document(
                "required_path",
                "required",
                False,
                Path("missing.md"),
                Path("."),
                results[0][0],
            )["failure_type"],
            "missing_artifact",
        )

    def test_observation_summary_extracts_repeated_failure_patterns(self):
        doc = self.local_sdlc.command_result_document(
            "redis-smoke",
            1,
            "",
            "\n".join(
                [
                    "PING: expected b'+PONG\\r\\n', got b\"-ERR internal error\\r\\n\"",
                    "GET: expected b'$-1\\r\\n', got b\"-ERR internal error\\r\\n\"",
                    "RESP parse error: invalid array count: b'*1'",
                    "RESP parse error: invalid array count: b'*2'",
                ]
            ),
            0.1,
        )

        summary = self.local_sdlc.observation_summary_document(1, [("Redis smoke", doc)])

        self.assertIn("repeated_failure_patterns", summary)
        self.assertIn("observed response", summary)
        self.assertIn("Fix the shared implementation root cause", summary)

    def test_agent_required_path_gate_blocks_approval_when_artifact_missing(self):
        calls = []

        class FakeClient:
            def __init__(self, config):
                self.config = config

            def complete(self, messages):
                calls.append(messages)
                return "BEGIN_FILE: app.py\nprint('fixed')\nEND_FILE"

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root)
            (project / "app.py").write_text("print('old')\n", encoding="utf-8")
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "fix app and require docs",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--include",
                        "app.py",
                        "--apply",
                        "--skip-pm",
                        "--judge-mode",
                        "command-only",
                        "--max-rounds",
                        "1",
                        "--require-path",
                        "README.md",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(result, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(manifest["final_verdict"], "test_failed")
        self.assertEqual(manifest["failure_summary"]["failure_type"], "missing_artifact")
        self.assertEqual(manifest["required_paths"], ["README.md"])

    def test_agent_new_file_is_automatically_required(self):
        calls = []

        class FakeClient:
            def __init__(self, config):
                self.config = config

            def complete(self, messages):
                calls.append(messages)
                return "BEGIN_FILE: app.py\nprint('fixed')\nEND_FILE"

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root)
            (project / "app.py").write_text("print('old')\n", encoding="utf-8")
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "fix app and create README",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--include",
                        "app.py",
                        "--new-file",
                        "README.md",
                        "--apply",
                        "--skip-pm",
                        "--judge-mode",
                        "command-only",
                        "--max-rounds",
                        "1",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(result, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(manifest["final_verdict"], "test_failed")
        self.assertEqual(manifest["required_paths"], ["README.md"])
        self.assertEqual(manifest["auto_required_paths"], ["README.md"])
        self.assertEqual(manifest["failure_summary"]["failure_type"], "missing_artifact")

    def test_agent_writes_partial_manifest_on_patch_extraction_failure(self):
        calls = []

        class FakeClient:
            def __init__(self, config):
                self.config = config

            def complete(self, messages):
                calls.append(messages)
                return "not a patch and not an artifact"

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root)
            (project / "app.py").write_text("print('old')\n", encoding="utf-8")
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "fix app",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--include",
                        "app.py",
                        "--apply",
                        "--skip-pm",
                        "--judge-mode",
                        "command-only",
                        "--worktree-mode",
                        "copy",
                        "--max-rounds",
                        "1",
                        "--protocol-repair-rounds",
                        "0",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            partial = json.loads((run_dir / "run.partial.json").read_text(encoding="utf-8"))
            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(result, 1)
        self.assertEqual(manifest["final_verdict"], "patch_failed")
        self.assertEqual(partial["status"], "patch_extraction_failed")
        self.assertEqual(partial["final_failure_type"], "artifact_invalid")
        self.assertTrue(partial["worktree_path"])

    def test_agent_protocol_budget_does_not_consume_functional_round(self):
        calls = []

        class FakeClient:
            def __init__(self, _config):
                pass

            def complete(self, _messages, agent_level="default", call_function="default", **_kwargs):
                calls.append((agent_level, call_function))
                if len(calls) == 1:
                    return "not an artifact"
                return "BEGIN_FILE: app.py\nVALUE = 'ok'\nEND_FILE"

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root)
            (project / "app.py").write_text("VALUE = 'old'\n", encoding="utf-8")
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "fix app",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--include",
                        "app.py",
                        "--apply",
                        "--skip-pm",
                        "--judge-mode",
                        "command-only",
                        "--max-rounds",
                        "1",
                        "--protocol-repair-rounds",
                        "1",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertEqual(manifest["final_verdict"], "approved")
        self.assertIsNone(manifest["final_failure_type"])
        self.assertEqual(manifest["protocol_rounds_used"], 1)
        self.assertEqual(manifest["functional_rounds_used"], 0)
        self.assertEqual(calls[1], ("coder", "format_repair"))

    def test_agent_uses_adaptive_round_when_command_failures_shrink(self):
        calls = []
        outputs = [
            "BEGIN_FILE: app.py\n\ndef value():\n    return 0\n\ndef other():\n    return 0\nEND_FILE",
            "BEGIN_FILE: app.py\n\ndef value():\n    return 1\n\ndef other():\n    return 0\nEND_FILE",
            "BEGIN_FILE: app.py\n\ndef value():\n    return 1\n\ndef other():\n    return 2\nEND_FILE",
        ]

        class FakeClient:
            def __init__(self, _config):
                pass

            def complete(self, _messages, agent_level="default", call_function="default", **_kwargs):
                calls.append((agent_level, call_function))
                return outputs[len(calls) - 1]

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root)
            (project / "app.py").write_text(
                "def value():\n    return 0\n\ndef other():\n    return 0\n",
                encoding="utf-8",
            )
            (project / "test_app.py").write_text(
                textwrap.dedent(
                    """
                    import unittest
                    import app

                    class AppTests(unittest.TestCase):
                        def test_value(self):
                            self.assertEqual(app.value(), 1)

                        def test_other(self):
                            self.assertEqual(app.other(), 2)

                    if __name__ == "__main__":
                        unittest.main()
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "fix app",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--include",
                        "app.py",
                        "--include",
                        "test_app.py",
                        "--apply",
                        "--skip-pm",
                        "--judge-mode",
                        "command-only",
                        "--max-rounds",
                        "2",
                        "--adaptive-rounds",
                        "1",
                        "--test-command",
                        f"{sys.executable} -B -m unittest test_app.py",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertEqual(manifest["final_verdict"], "approved")
        self.assertEqual(manifest["completed_rounds"], 3)
        self.assertEqual(manifest["functional_rounds_used"], 2)
        self.assertEqual(manifest["adaptive_rounds_used"], 1)
        self.assertEqual(
            calls,
            [
                ("coder", "generate_artifact"),
                ("coder", "repair_artifact"),
                ("coder", "repair_artifact"),
            ],
        )

    def test_agent_reserves_root_cause_patch_round_after_failure_analysis(self):
        calls = []

        class FakeClient:
            def __init__(self, _config):
                pass

            def complete(self, _messages, agent_level="default", call_function="default", **_kwargs):
                calls.append((agent_level, call_function))
                if call_function == "failure_analysis":
                    return json.dumps(
                        {
                            "failure_id": "R01-F01",
                            "round": 1,
                            "failure_type": "repeated_same_failure",
                            "failure_signature": "same fail",
                            "observed_facts": ["VALUE mid still failed"],
                            "attempted_actions": [
                                {"round": 1, "action": "changed VALUE to mid", "result": "same"}
                            ],
                            "rejected_hypotheses": [
                                {"hypothesis": "mid is enough", "reason": "same fail remained"}
                            ],
                            "active_constraints": ["do not edit tests"],
                            "next_required_action": {
                                "role": "repair_artifact",
                                "goal": "set VALUE to new",
                                "required_paths": ["app.py"],
                                "readonly_paths": [],
                                "forbidden_paths": [],
                                "next_patch_type": "search_replace",
                                "minimal_patch_goal": "set VALUE to new",
                                "forbidden_focus": ["VALUE=mid"],
                                "required_focus": ["app.py VALUE assignment"],
                            },
                            "formal_constraints": ["same(F_i,F_t) && applied(A_i) => reject(H_i)"],
                        }
                    )
                if call_function == "root_cause_analysis":
                    return textwrap.dedent(
                        """
                        ROOT_CAUSE_REPORT
                        - repeated_failure_signature: same fail
                        - failing_observation: command fails until VALUE is new
                        - rejected_hypotheses:
                          - VALUE mid is insufficient
                        - chosen_root_cause: VALUE must be new
                        - patch_target: app.py VALUE assignment
                        - patch_rule: replace VALUE = "mid" with VALUE = "new"
                        - stop_rule: stop if app.py is not visible
                        """
                    ).strip()
                if call_function == "patch_planner":
                    return textwrap.dedent(
                        """
                        PATCH_PLAN
                        - proposition: If app.py VALUE remains mid, change VALUE so command passes
                        - required_path: app.py
                        - readonly_paths: (none)
                        - forbidden_paths: tests/test_app.py
                        - patch_type: search_replace
                        - minimal_patch_goal: set VALUE to new
                        - stop_rule: stop if app.py is not visible
                        """
                    ).strip()
                if len([call for call in calls if call[1] in {"generate_artifact", "artifact_writer"}]) == 1:
                    return textwrap.dedent(
                        """
                        BEGIN_SEARCH_REPLACE: app.py
                        <<<<<<< SEARCH
                        VALUE = "old"
                        =======
                        VALUE = "mid"
                        >>>>>>> REPLACE
                        END_SEARCH_REPLACE
                        """
                    ).strip()
                return textwrap.dedent(
                    """
                    BEGIN_SEARCH_REPLACE: app.py
                    <<<<<<< SEARCH
                    VALUE = "mid"
                    =======
                    VALUE = "new"
                    >>>>>>> REPLACE
                    END_SEARCH_REPLACE
                    """
                ).strip()

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root)
            (project / "app.py").write_text('VALUE = "old"\n', encoding="utf-8")
            run_dir = project / "run"
            command = (
                f"{sys.executable} -c \"import pathlib, sys; "
                "text = pathlib.Path('app.py').read_text(); "
                "sys.exit(0 if 'VALUE = \\\\\\\"new\\\\\\\"' in text else sys.stderr.write('same fail\\\\n') or 1)\""
            )

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "fix app",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--include",
                        "app.py",
                        "--apply",
                        "--skip-pm",
                        "--judge-mode",
                        "command-only",
                        "--precheck",
                        "--max-rounds",
                        "1",
                        "--adaptive-rounds",
                        "0",
                        "--test-command",
                        command,
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertEqual(manifest["final_verdict"], "approved")
        self.assertEqual(manifest["root_cause_patch_rounds_used"], 1)
        self.assertIn(("judge", "failure_analysis"), calls)
        self.assertIn(("judge", "root_cause_analysis"), calls)
        self.assertIn(("judge", "patch_planner"), calls)
        self.assertIn(("coder", "artifact_writer"), calls)

    def test_agent_streams_pm_coder_and_judge_partials(self):
        calls = []
        outputs = {
            "plan_work": "## PM\n- implement app",
            "generate_artifact": "BEGIN_FILE: app.py\nprint('fixed')\nEND_FILE",
            "judge_review": "## Verdict\napproved\n",
        }

        class FakeClient:
            def __init__(self, _config):
                pass

            def complete(
                self,
                _messages,
                agent_level="default",
                call_function="default",
                stream_output_path=None,
                stream_callback=None,
                **_kwargs,
            ):
                calls.append((agent_level, call_function, stream_output_path is not None))
                if stream_output_path is not None:
                    stream_output_path.write_text("partial", encoding="utf-8")
                if stream_callback is not None:
                    stream_callback(
                        self_local_sdlc.LLMStreamStats(
                            chunks_received=1,
                            content_chunks=1,
                            reasoning_chunks=0,
                            bytes_received=7,
                            first_chunk_at=1.0,
                            last_chunk_at=1.0,
                            duration_seconds=0.1,
                        )
                    )
                return outputs[call_function]

        self_local_sdlc = self.local_sdlc
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root)
            (project / "app.py").write_text("print('old')\n", encoding="utf-8")
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "fix app",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--include",
                        "app.py",
                        "--apply",
                        "--stream",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertEqual(manifest["final_verdict"], "approved")
        self.assertEqual(
            calls,
            [
                ("pm", "plan_work", True),
                ("coder", "generate_artifact", True),
                ("judge", "judge_review", True),
            ],
        )
        self.assertTrue(any(path.endswith("01-pm-control.partial.md") for path in manifest["documents"]))
        self.assertTrue(any(path.endswith("02-r01-coder-output.partial.md") for path in manifest["documents"]))
        self.assertTrue(any(path.endswith("06-r01-judge-review.partial.md") for path in manifest["documents"]))

    def test_agent_final_manifest_records_reasoning_records(self):
        reasoning_records = [
            {
                "agent_level": "judge",
                "call_function": "judge_review",
                "model": "test-model",
                "chars": 17,
                "truncated": False,
                "reasoning_content": "separated-analysis",
            }
        ]
        outputs = {
            "plan_work": "## PM\n- implement app",
            "generate_artifact": "BEGIN_FILE: app.py\nprint('fixed')\nEND_FILE",
            "judge_review": "## Verdict\napproved\n",
        }

        class FakeClient:
            def __init__(self, _config):
                self.reasoning_records = reasoning_records

            def complete(self, _messages, agent_level="default", call_function="default", **_kwargs):
                return outputs[call_function]

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root)
            (project / "app.py").write_text("print('old')\n", encoding="utf-8")
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "fix app",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--include",
                        "app.py",
                        "--apply",
                        "--domain-modeling",
                        "never",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertEqual(manifest["final_verdict"], "approved")
        self.assertEqual(manifest["reasoning_records"], reasoning_records)

    def test_agent_resume_continues_from_previous_run_documents(self):
        calls = []

        class FakeClient:
            def __init__(self, config):
                self.config = config

            def complete(self, messages):
                calls.append(messages)
                return "BEGIN_FILE: app.py\nprint('fixed')\nEND_FILE"

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root)
            (project / "app.py").write_text("if True print('broken')\n", encoding="utf-8")
            run_dir = project / "run"
            run_dir.mkdir()
            command_doc = self.local_sdlc.command_result_document(
                f"{sys.executable} -m py_compile app.py",
                1,
                "",
                "SyntaxError: invalid syntax\n",
                0.01,
            )
            (run_dir / "05-r01-command-01.md").write_text(command_doc, encoding="utf-8")
            (run_dir / "run.json").write_text(
                json.dumps(
                    {
                        "api_calls": 1,
                        "completed_rounds": 1,
                        "documents": ["05-r01-command-01.md"],
                        "evidence": [
                            {
                                "id": "E01",
                                "kind": "command",
                                "name": "Command result round 1.1",
                                "status": "fail",
                                "failure_type": "syntax_error",
                                "document": "05-r01-command-01.md",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "fix app",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--include",
                        "app.py",
                        "--apply",
                        "--judge-mode",
                        "command-only",
                        "--resume",
                        str(run_dir),
                        "--max-rounds",
                        "1",
                        "--test-command",
                        f"{sys.executable} -m py_compile app.py",
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            fixed = (project / "app.py").read_text(encoding="utf-8")
            r02_exists = (run_dir / "02-r02-coder-output.md").exists()

        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(fixed, "print('fixed')")
        self.assertEqual(manifest["completed_rounds"], 2)
        self.assertEqual(manifest["api_calls"], 2)
        self.assertTrue(r02_exists)
        self.assertIn("Resume context: 05-r01-command-01.md", calls[0][1]["content"])

    def test_load_resume_context_adds_observation_summary(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            run_dir = project / "run"
            run_dir.mkdir()
            command_doc = self.local_sdlc.command_result_document(
                "redis-smoke",
                1,
                "",
                "PING: expected b'+PONG\\r\\n', got b\"-ERR same\\r\\n\"\n"
                "GET: expected b'$-1\\r\\n', got b\"-ERR same\\r\\n\"\n",
                0.1,
            )
            (run_dir / "05-r01-redis-smoke.md").write_text(command_doc, encoding="utf-8")
            (run_dir / "run.json").write_text(
                json.dumps(
                    {
                        "completed_rounds": 1,
                        "documents": ["05-r01-redis-smoke.md"],
                    }
                ),
                encoding="utf-8",
            )

            _manifest, documents, _paths = self.local_sdlc.load_resume_context(run_dir, project)

        self.assertTrue(any(title == "Resume observation summary" for title, _text in documents))
        self.assertTrue(any("repeated_failure_patterns" in text for _title, text in documents))

    def test_agent_copy_worktree_copies_allowed_files_back_after_approval(self):
        calls = []

        class FakeClient:
            def __init__(self, config):
                self.config = config

            def complete(self, messages):
                calls.append(messages)
                return "BEGIN_FILE: app.py\nprint('fixed')\nEND_FILE"

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root)
            (project / "app.py").write_text("print('old')\n", encoding="utf-8")
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "fix app",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--include",
                        "app.py",
                        "--apply",
                        "--skip-pm",
                        "--judge-mode",
                        "command-only",
                        "--worktree-mode",
                        "copy",
                        "--test-command",
                        f"{sys.executable} -m py_compile app.py",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            fixed = (project / "app.py").read_text(encoding="utf-8")

        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(fixed, "print('fixed')")
        self.assertEqual(manifest["worktree_mode"], "copy")
        self.assertEqual(manifest["copied_back"], ["app.py"])

    def test_agent_copy_worktree_does_not_copy_readonly_context_back(self):
        calls = []

        class FakeClient:
            def __init__(self, config):
                self.config = config

            def complete(self, messages):
                calls.append(messages)
                return "BEGIN_FILE: new.py\nVALUE = 'new'\nEND_FILE"

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root)
            (project / "base.py").write_text("VALUE = 'base'\n", encoding="utf-8")
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "create new file using base as context",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--include",
                        "base.py",
                        "--new-file",
                        "new.py",
                        "--apply",
                        "--skip-pm",
                        "--judge-mode",
                        "command-only",
                        "--worktree-mode",
                        "copy",
                        "--test-command",
                        f"{sys.executable} -m py_compile new.py",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            base_text = (project / "base.py").read_text(encoding="utf-8")

        self.assertEqual(result, 0)
        self.assertEqual(manifest["changed_paths"], ["new.py"])
        self.assertEqual(manifest["copied_back"], ["new.py"])
        self.assertEqual(base_text, "VALUE = 'base'\n")

    def test_agent_resume_worktree_continues_failed_worktree_state(self):
        calls = []

        class FakeClient:
            def __init__(self, config):
                self.config = config

            def complete(self, messages):
                calls.append(messages)
                return json.dumps(
                    {
                        "artifacts": [
                            {
                                "type": "search_replace",
                                "path": "app.py",
                                "search": "VALUE = 'partial'\n",
                                "replace": "VALUE = 'final'\n",
                            }
                        ]
                    }
                )

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root)
            (project / "app.py").write_text("VALUE = 'old'\n", encoding="utf-8")
            previous_worktree = root / "previous-worktree"
            previous_worktree.mkdir()
            (previous_worktree / "SPEC.md").write_text("# SPEC\n", encoding="utf-8")
            (previous_worktree / "app.py").write_text("VALUE = 'partial'\n", encoding="utf-8")
            run_dir = project / "run"
            run_dir.mkdir()
            (run_dir / "run.json").write_text(
                json.dumps(
                    {
                        "api_calls": 1,
                        "completed_rounds": 1,
                        "worktree_path": str(previous_worktree),
                        "changed_paths": ["app.py"],
                        "documents": [],
                    }
                ),
                encoding="utf-8",
            )

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "finish app from previous worktree",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--include",
                        "app.py",
                        "--apply",
                        "--skip-pm",
                        "--judge-mode",
                        "command-only",
                        "--resume",
                        str(run_dir),
                        "--resume-worktree",
                        "--worktree-mode",
                        "copy",
                        "--test-command",
                        f"{sys.executable} -m py_compile app.py",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            fixed = (project / "app.py").read_text(encoding="utf-8")

        self.assertEqual(result, 0)
        self.assertEqual(fixed, "VALUE = 'final'\n")
        self.assertEqual(manifest["copied_back"], ["app.py"])
        self.assertEqual(manifest["resumed_worktree_from"], str(previous_worktree))

    def test_agent_resume_worktree_path_without_run_manifest(self):
        calls = []

        class FakeClient:
            def __init__(self, config):
                self.config = config

            def complete(self, messages):
                calls.append(messages)
                return json.dumps(
                    {
                        "artifacts": [
                            {
                                "type": "search_replace",
                                "path": "app.py",
                                "search": "VALUE = 'partial'\n",
                                "replace": "VALUE = 'final'\n",
                            }
                        ]
                    }
                )

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root)
            (project / "app.py").write_text("VALUE = 'old'\n", encoding="utf-8")
            previous_worktree = root / "interrupted-worktree"
            previous_worktree.mkdir()
            (previous_worktree / "SPEC.md").write_text("# SPEC\n", encoding="utf-8")
            (previous_worktree / "app.py").write_text("VALUE = 'partial'\n", encoding="utf-8")
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "finish app from interrupted worktree",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--include",
                        "app.py",
                        "--apply",
                        "--skip-pm",
                        "--judge-mode",
                        "command-only",
                        "--resume-worktree-path",
                        str(previous_worktree),
                        "--worktree-mode",
                        "copy",
                        "--test-command",
                        f"{sys.executable} -m py_compile app.py",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            fixed = (project / "app.py").read_text(encoding="utf-8")

        self.assertEqual(result, 0)
        self.assertEqual(fixed, "VALUE = 'final'\n")
        self.assertEqual(manifest["copied_back"], ["app.py"])
        self.assertEqual(manifest["resumed_worktree_from"], str(previous_worktree.resolve()))

    def test_agent_applies_patch_and_runs_test_command(self):
        calls = []
        patch = """diff --git a/hello.txt b/hello.txt
new file mode 100644
index 0000000..45b983b
--- /dev/null
+++ b/hello.txt
@@ -0,0 +1 @@
+hi
"""
        outputs = [
            "pm control",
            patch,
            "判定: 承認\nEvidence passed.",
        ]

        class FakeClient:
            def __init__(self, config):
                self.config = config

            def complete(self, messages):
                calls.append(messages)
                return outputs[len(calls) - 1]

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            skills_dir = project / "skills"
            for name in ("sdlc", "tdd", "review"):
                skill_dir = skills_dir / name
                skill_dir.mkdir(parents=True)
                (skill_dir / "SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: {name}\n---\n# {name}\n",
                    encoding="utf-8",
                )
            (project / "SPEC.md").write_text("# SPEC\n", encoding="utf-8")
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "create hello",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--new-file",
                        "hello.txt",
                        "--apply",
                        "--test-command",
                        f"{sys.executable} -c \"from pathlib import Path; assert Path('hello.txt').read_text() == 'hi\\n'\"",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            safety_decisions = self.local_sdlc.read_safety_decisions(run_dir)
            hello_text = (project / "hello.txt").read_text(encoding="utf-8")

        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 3)
        self.assertEqual(hello_text, "hi\n")
        self.assertEqual(manifest["final_verdict"], "approved")
        self.assertEqual(manifest["safety_decision_count"], len(safety_decisions))
        self.assertGreater(manifest["safety_decision_count"], 1)
        self.assertTrue(manifest["safety_decisions_log"].endswith("safety_decisions.jsonl"))
        self.assertTrue(all(item["decision"] in {"allow", "allow_in_worktree"} for item in safety_decisions))
        self.assertEqual(manifest["action_gate_audit"]["status"], "pass")
        self.assertTrue(any(path.endswith("05-r01-command-01.md") for path in manifest["documents"]))

    def test_agent_applies_multiple_file_artifacts(self):
        calls = []
        artifacts = """BEGIN_FILE: server.py
VALUE = "ok"
END_FILE
BEGIN_FILE: README.md
# Generated
END_FILE"""
        outputs = [
            "pm control",
            artifacts,
            "判定: 承認\nEvidence passed.",
        ]

        class FakeClient:
            def __init__(self, config):
                self.config = config

            def complete(self, messages):
                calls.append(messages)
                return outputs[len(calls) - 1]

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            skills_dir = project / "skills"
            for name in ("sdlc", "tdd", "review"):
                skill_dir = skills_dir / name
                skill_dir.mkdir(parents=True)
                (skill_dir / "SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: {name}\n---\n# {name}\n",
                    encoding="utf-8",
                )
            (project / "SPEC.md").write_text("# SPEC\n", encoding="utf-8")
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "create multiple files",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--new-file",
                        "server.py",
                        "--new-file",
                        "README.md",
                        "--apply",
                        "--test-command",
                        f"{sys.executable} -c \"from pathlib import Path; assert Path('server.py').exists(); assert Path('README.md').exists()\"",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            server_text = (project / "server.py").read_text(encoding="utf-8")
            readme_text = (project / "README.md").read_text(encoding="utf-8")

        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 3)
        self.assertEqual(server_text, 'VALUE = "ok"')
        self.assertEqual(readme_text, "# Generated")
        self.assertEqual(manifest["final_verdict"], "approved")
        self.assertTrue(any(path.endswith("03-r01-01-server.py") for path in manifest["documents"]))
        self.assertTrue(any(path.endswith("03-r01-02-README.md") for path in manifest["documents"]))

    def test_agent_rolls_back_artifact_transaction_when_later_artifact_fails(self):
        calls = []
        artifacts = """BEGIN_SEARCH_REPLACE: app.py
<<<<<<< SEARCH
VALUE = "old"
=======
VALUE = "mid"
>>>>>>> REPLACE
END_SEARCH_REPLACE

BEGIN_SEARCH_REPLACE: app.py
<<<<<<< SEARCH
MISSING_TEXT
=======
VALUE = "bad"
>>>>>>> REPLACE
END_SEARCH_REPLACE"""

        class FakeClient:
            def __init__(self, _config):
                pass

            def complete(self, messages, **_kwargs):
                calls.append(messages)
                return artifacts

        with tempfile.TemporaryDirectory() as temp:
            project, skills_dir = self.make_agent_project(Path(temp))
            (project / "app.py").write_text('VALUE = "old"\n', encoding="utf-8")
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "repair app",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--skip-pm",
                        "--judge-mode",
                        "command-only",
                        "--include",
                        "app.py",
                        "--apply",
                        "--max-rounds",
                        "1",
                        "--test-command",
                        f"{sys.executable} -c \"print('ok')\"",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            app_text = (project / "app.py").read_text(encoding="utf-8")
            apply_doc = (run_dir / "04-r01-apply.md").read_text(encoding="utf-8")

        self.assertEqual(result, 1)
        self.assertEqual(app_text, 'VALUE = "old"\n')
        self.assertEqual(manifest["changed_paths"], [])
        self.assertIn("Artifact Transaction Rollback", apply_doc)
        self.assertIn("restored: `app.py`", apply_doc)

    def test_agent_artifact_lint_blocks_pytest_with_unittest_command(self):
        class FakeClient:
            def __init__(self, _config):
                pass

            def complete(self, _messages, agent_level="default"):
                return "BEGIN_FILE: tests/test_app.py\nimport pytest\n\ndef test_app(tmp_path):\n    pass\nEND_FILE"

        with tempfile.TemporaryDirectory() as temp:
            project, skills_dir = self.make_agent_project(Path(temp))
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "create tests",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--skip-pm",
                        "--judge-mode",
                        "command-only",
                        "--new-file",
                        "tests/test_app.py",
                        "--apply",
                        "--artifact-format",
                        "legacy",
                        "--test-command",
                        f"{sys.executable} -m unittest discover -s tests",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            lint_doc = (run_dir / "03-r01-artifact-lint.md").read_text(encoding="utf-8")

        self.assertEqual(result, 1)
        self.assertEqual(manifest["final_failure_type"], "artifact_lint_failed")
        self.assertIn("pytest_import", lint_doc)
        self.assertFalse((project / "tests" / "test_app.py").exists())

    def test_agent_routes_contract_followup_to_semantic_repair_function(self):
        calls = []

        class FakeClient:
            def __init__(self, _config):
                pass

            def complete(self, _messages, agent_level="default", call_function="default", **_kwargs):
                calls.append((agent_level, call_function))
                if len(calls) == 1:
                    return textwrap.dedent(
                        """
                        BEGIN_SEARCH_REPLACE: minisqlite/sql/lexer.py
                        <<<<<<< SEARCH
                        VALUE = "old"
                        =======
                        VALUE = "mid"
                        >>>>>>> REPLACE
                        END_SEARCH_REPLACE
                        """
                    ).strip()
                return textwrap.dedent(
                    """
                    BEGIN_SEARCH_REPLACE: minisqlite/sql/lexer.py
                    <<<<<<< SEARCH
                    VALUE = "mid"
                    =======
                    VALUE = "new"
                    >>>>>>> REPLACE
                    END_SEARCH_REPLACE
                    """
                ).strip()

        with tempfile.TemporaryDirectory() as temp:
            project, skills_dir = self.make_agent_project(Path(temp))
            lexer_dir = project / "minisqlite" / "sql"
            lexer_dir.mkdir(parents=True)
            (lexer_dir / "lexer.py").write_text('VALUE = "old"\n', encoding="utf-8")
            tests_dir = project / "tests"
            tests_dir.mkdir()
            (tests_dir / "test_lexer.py").write_text(
                textwrap.dedent(
                    """
                    import unittest

                    class TestLexer(unittest.TestCase):
                        def test_negative_contract(self):
                            self.assertEqual("10", "-10")
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "repair lexer negative integer handling",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--skip-pm",
                        "--judge-mode",
                        "command-only",
                        "--include",
                        "minisqlite/sql/lexer.py",
                        "--apply",
                        "--max-rounds",
                        "2",
                        "--test-command",
                        f"{sys.executable} -m unittest discover -s tests",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(result, 1)
        self.assertEqual(calls[0], ("coder", "generate_artifact"))
        self.assertEqual(calls[1], ("coder", "semantic_repair"))
        self.assertTrue(
            any("Negative integer literals" in contract["text"] for contract in manifest["semantic_contracts"])
        )

    def test_agent_routes_repeated_same_failure_to_root_cause_repair(self):
        calls = []

        class FakeClient:
            def __init__(self, _config):
                pass

            def complete(self, messages, agent_level="default", call_function="default", **_kwargs):
                joined = "\n".join(str(message.get("content", "")) for message in messages)
                calls.append((agent_level, call_function, joined))
                if len(calls) == 1:
                    return textwrap.dedent(
                        """
                        BEGIN_SEARCH_REPLACE: app.py
                        <<<<<<< SEARCH
                        VALUE = "old"
                        =======
                        VALUE = "mid"
                        >>>>>>> REPLACE
                        END_SEARCH_REPLACE
                        """
                    ).strip()
                if call_function == "failure_analysis":
                    return json.dumps(
                        {
                            "failure_id": "R01-F01",
                            "round": 1,
                            "failure_type": "repeated_same_failure",
                            "failure_signature": "same fail",
                            "observed_facts": ["same fail remained after VALUE old to mid"],
                            "attempted_actions": [
                                {"round": 1, "action": "changed VALUE old to mid", "result": "same"}
                            ],
                            "rejected_hypotheses": [
                                {"hypothesis": "VALUE=mid is sufficient", "reason": "same failure remained"}
                            ],
                            "active_constraints": ["do not repeat VALUE=mid"],
                            "next_required_action": {
                                "role": "root_cause_analysis",
                                "goal": "choose a different value invariant",
                                "required_paths": ["app.py"],
                                "readonly_paths": [],
                                "forbidden_paths": [],
                                "next_patch_type": "search_replace",
                                "minimal_patch_goal": "set VALUE to new",
                                "forbidden_focus": ["VALUE=mid"],
                                "required_focus": ["app.py VALUE assignment"],
                            },
                            "formal_constraints": [
                                "same(F_i,F_t) && applied(A_i) => reject(H_i)"
                            ],
                        }
                    )
                if call_function == "root_cause_analysis":
                    return textwrap.dedent(
                        """
                        ROOT_CAUSE_REPORT
                        - repeated_failure_signature: same fail
                        - failing_observation: command still exits with same fail
                        - rejected_hypotheses:
                          - VALUE old to mid did not change the failure
                        - chosen_root_cause: VALUE must be new
                        - patch_target: app.py VALUE assignment
                        - patch_rule: set VALUE to new
                        - stop_rule: stop if app.py context is missing
                        """
                    ).strip()
                if call_function == "patch_planner":
                    return textwrap.dedent(
                        """
                        PATCH_PLAN
                        - proposition: If VALUE=mid keeps the same failure, change app.py VALUE so the invariant is new
                        - required_path: app.py
                        - readonly_paths: (none)
                        - forbidden_paths: tests/test_app.py
                        - patch_type: search_replace
                        - minimal_patch_goal: set VALUE to new
                        - stop_rule: stop if app.py context is missing
                        """
                    ).strip()
                return textwrap.dedent(
                    """
                    BEGIN_SEARCH_REPLACE: app.py
                    <<<<<<< SEARCH
                    VALUE = "mid"
                    =======
                    VALUE = "new"
                    >>>>>>> REPLACE
                    END_SEARCH_REPLACE
                    """
                ).strip()

        with tempfile.TemporaryDirectory() as temp:
            project, skills_dir = self.make_agent_project(Path(temp))
            (project / "app.py").write_text('VALUE = "old"\n', encoding="utf-8")
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "repair persistent failure",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--skip-pm",
                        "--judge-mode",
                        "command-only",
                        "--include",
                        "app.py",
                        "--apply",
                        "--precheck",
                        "--max-rounds",
                        "2",
                        "--test-command",
                        f"{sys.executable} -c \"import sys; sys.stderr.write('same fail\\\\n'); sys.exit(1)\"",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(result, 1)
        self.assertGreaterEqual(len(calls), 4)
        self.assertEqual(calls[1][0], "judge")
        self.assertEqual(calls[1][1], "failure_analysis")
        self.assertIn("Return exactly one JSON object", calls[1][2])
        self.assertEqual(calls[2][0], "judge")
        self.assertEqual(calls[2][1], "root_cause_analysis")
        self.assertIn("Failure\nAnalysis document", calls[2][2])
        self.assertIn("VALUE=mid", calls[2][2])
        self.assertEqual(calls[3][0], "judge")
        self.assertEqual(calls[3][1], "patch_planner")
        self.assertIn("Structured/root-cause analysis input", calls[3][2])
        self.assertIn("VALUE=mid", calls[3][2])
        self.assertEqual(calls[4][0], "coder")
        self.assertEqual(calls[4][1], "artifact_writer")
        self.assertIn("current_role: root_cause_repair", calls[4][2])
        self.assertIn("Root cause analysis round 2", calls[4][2])
        self.assertIn("Patch plan round 2", calls[4][2])
        self.assertIn("Latest PATCH_PLAN", calls[4][2])
        self.assertIn("Current supervisor transition rule", calls[4][2])
        self.assertIn("repeated_same_failure", {item["failure_type"] for item in manifest["state_transitions"]})
        self.assertGreaterEqual(manifest["repeated_same_failure_count"], 1)
        self.assertEqual(manifest["failure_analyses"][0]["call_function"], "failure_analysis")
        self.assertEqual(manifest["failure_analyses"][0]["next_required_action"]["forbidden_focus"], ["VALUE=mid"])

    def test_agent_runs_project_policy_triage_for_generated_test_harness_ownership(self):
        calls = []

        class FakeClient:
            def __init__(self, _config):
                pass

            def complete(self, messages, agent_level="default", call_function="default", **_kwargs):
                joined = "\n".join(str(message.get("content", "")) for message in messages)
                calls.append((agent_level, call_function, joined))
                if call_function == "project_policy_triage":
                    return json.dumps(
                        {
                            "trigger": "test_harness_ownership",
                            "case_type": "test_harness",
                            "confidence": "high",
                            "project_policy_basis": [
                                "stage-generated tests are mutable when they contradict an already approved API"
                            ],
                            "safe_next_action": "edit_test_harness",
                            "editable_paths": ["tests/test_btree.py"],
                            "readonly_paths": [
                                "minisqlite/storage/pager.py",
                                "minisqlite/storage/btree.py",
                            ],
                            "forbidden_actions": [
                                "do not add Pager.init_db solely for the generated BTree test"
                            ],
                            "rationale": "The failing generated BTree test calls a Pager setup API outside the BTree stage owner.",
                        }
                    )
                return textwrap.dedent(
                    """
                    BEGIN_SEARCH_REPLACE: minisqlite/storage/btree.py
                    <<<<<<< SEARCH
                    VALUE = "old"
                    =======
                    VALUE = "mid"
                    >>>>>>> REPLACE
                    END_SEARCH_REPLACE
                    """
                ).strip()

        with tempfile.TemporaryDirectory() as temp:
            project, skills_dir = self.make_agent_project(Path(temp))
            storage_dir = project / "minisqlite" / "storage"
            storage_dir.mkdir(parents=True)
            tests_dir = project / "tests"
            tests_dir.mkdir(exist_ok=True)
            for package in (project / "minisqlite", storage_dir, tests_dir):
                (package / "__init__.py").write_text("", encoding="utf-8")
            (storage_dir / "pager.py").write_text(
                "class Pager:\n    pass\n",
                encoding="utf-8",
            )
            (storage_dir / "btree.py").write_text(
                'VALUE = "old"\n\nclass BTree:\n    pass\n',
                encoding="utf-8",
            )
            (tests_dir / "test_btree.py").write_text(
                textwrap.dedent(
                    """
                    import unittest
                    from minisqlite.storage.pager import Pager

                    class TestBTree(unittest.TestCase):
                        def test_generated_setup_uses_wrong_cross_stage_api(self):
                            Pager().init_db()
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "repair generated btree stage",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--skip-pm",
                        "--judge-mode",
                        "command-only",
                        "--include",
                        "minisqlite/storage/btree.py",
                        "--context",
                        "minisqlite/storage/pager.py",
                        "--apply",
                        "--max-rounds",
                        "1",
                        "--test-command",
                        f"{sys.executable} -m unittest discover -s tests",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(result, 1)
        self.assertIn("project_policy_triage", [call[1] for call in calls])
        triage = manifest["project_policy_triages"][0]
        self.assertEqual(triage["call_function"], "project_policy_triage")
        self.assertEqual(triage["case_type"], "test_harness")
        self.assertEqual(triage["safe_next_action"], "edit_test_harness")
        self.assertEqual(manifest["repair_advice"]["strategy"], "test_harness_api_mismatch")
        self.assertIn("tests/test_btree.py", manifest["repair_advice"]["focus_files"])

    def test_agent_routes_malformed_semantic_repair_to_format_repair(self):
        calls = []

        class FakeClient:
            def __init__(self, _config):
                pass

            def complete(self, _messages, agent_level="default", call_function="default", **_kwargs):
                calls.append((agent_level, call_function))
                if len(calls) == 1:
                    return textwrap.dedent(
                        """
                        BEGIN_SEARCH_REPLACE: minisqlite/sql/lexer.py
                        <<<<<<< SEARCH
                        VALUE = "old"
                        =======
                        VALUE = "mid"
                        >>>>>>> REPLACE
                        END_SEARCH_REPLACE
                        """
                    ).strip()
                if len(calls) == 2:
                    return textwrap.dedent(
                        """
                        Here is the semantic edit:
                        BEGIN_SEARCH_REPLACE
                        <<<<<<< SEARCH
                        VALUE = "mid"
                        =======
                        VALUE = "new"
                        >>>>>>> REPLACE
                        END_SEARCH_REPLACE
                        """
                    ).strip()
                return textwrap.dedent(
                    """
                    BEGIN_SEARCH_REPLACE: minisqlite/sql/lexer.py
                    <<<<<<< SEARCH
                    VALUE = "mid"
                    =======
                    VALUE = "new"
                    >>>>>>> REPLACE
                    END_SEARCH_REPLACE
                    """
                ).strip()

        with tempfile.TemporaryDirectory() as temp:
            project, skills_dir = self.make_agent_project(Path(temp))
            lexer_dir = project / "minisqlite" / "sql"
            lexer_dir.mkdir(parents=True)
            (lexer_dir / "lexer.py").write_text('VALUE = "old"\n', encoding="utf-8")
            tests_dir = project / "tests"
            tests_dir.mkdir()
            (tests_dir / "test_lexer.py").write_text(
                textwrap.dedent(
                    """
                    import unittest

                    class TestLexer(unittest.TestCase):
                        def test_negative_contract(self):
                            self.assertEqual("10", "-10")
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "repair lexer negative integer handling",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--skip-pm",
                        "--judge-mode",
                        "command-only",
                        "--include",
                        "minisqlite/sql/lexer.py",
                        "--apply",
                        "--max-rounds",
                        "3",
                        "--test-command",
                        f"{sys.executable} -m unittest discover -s tests",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(result, 1)
        self.assertEqual(calls[0], ("coder", "generate_artifact"))
        self.assertEqual(calls[1], ("coder", "semantic_repair"))
        self.assertEqual(calls[2], ("coder", "format_repair"))
        self.assertTrue(
            any(
                transition["failure_type"] == "semantic_repair_missing_path"
                and transition["next_role"] == "format_repair"
                for transition in manifest["state_transitions"]
            )
        )

    def test_agent_classifies_long_prose_as_non_artifact_output(self):
        class FakeClient:
            def __init__(self, _config):
                pass

            def complete(self, _messages, agent_level="default"):
                return "説明です。\n" + ("x" * (self_module.ARTIFACT_OUTPUT_BUDGET_BYTES + 50))

        self_module = self.local_sdlc
        with tempfile.TemporaryDirectory() as temp:
            project, skills_dir = self.make_agent_project(Path(temp))
            (project / "app.py").write_text("VALUE = 'old'\n", encoding="utf-8")
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "fix app.py",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--skip-pm",
                        "--judge-mode",
                        "command-only",
                        "--include",
                        "app.py",
                        "--apply",
                        "--max-rounds",
                        "1",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            transition_doc = (run_dir / "03-r01-failure-transition.md").read_text(encoding="utf-8")

        self.assertEqual(result, 1)
        self.assertEqual(manifest["final_failure_type"], "non_artifact_output")
        self.assertEqual(manifest["state_transitions"][0]["next_role"], "pm_planner")
        self.assertIn("non_artifact_output", transition_doc)

    def test_agent_rejects_readonly_test_edit_attempt(self):
        class FakeClient:
            def __init__(self, _config):
                pass

            def complete(self, _messages, agent_level="default"):
                return "BEGIN_FILE: tests/test_app.py\nprint('changed')\nEND_FILE"

        with tempfile.TemporaryDirectory() as temp:
            project, skills_dir = self.make_agent_project(Path(temp))
            (project / "app.py").write_text("VALUE = 'old'\n", encoding="utf-8")
            tests_dir = project / "tests"
            tests_dir.mkdir()
            (tests_dir / "test_app.py").write_text("import unittest\n", encoding="utf-8")
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "fix app.py using tests as evidence",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--skip-pm",
                        "--judge-mode",
                        "command-only",
                        "--include",
                        "app.py",
                        "--context",
                        "tests/test_app.py",
                        "--apply",
                        "--artifact-format",
                        "legacy",
                        "--max-rounds",
                        "1",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            test_text = (tests_dir / "test_app.py").read_text(encoding="utf-8")

        self.assertEqual(result, 1)
        self.assertEqual(manifest["final_failure_type"], "test_edit_attempt")
        self.assertEqual(manifest["state_transitions"][0]["next_role"], "pm_planner")
        self.assertEqual(test_text, "import unittest\n")

    def test_agent_allows_safe_additional_new_file_artifacts(self):
        artifacts = """BEGIN_FILE: package/__init__.py
VALUE = "ok"
END_FILE
BEGIN_FILE: package/extra.py
EXTRA = "ok"
END_FILE"""

        class FakeClient:
            def __init__(self, _config):
                pass

            def complete(self, _messages, agent_level="default"):
                return artifacts

        with tempfile.TemporaryDirectory() as temp:
            project, skills_dir = self.make_agent_project(Path(temp))
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "create package with helper",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--skip-pm",
                        "--judge-mode",
                        "command-only",
                        "--new-file",
                        "package/__init__.py",
                        "--apply",
                        "--artifact-format",
                        "legacy",
                        "--test-command",
                        f"{sys.executable} -m py_compile package/__init__.py package/extra.py",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            init_text = (project / "package" / "__init__.py").read_text(encoding="utf-8")
            extra_text = (project / "package" / "extra.py").read_text(encoding="utf-8")

        self.assertEqual(result, 0)
        self.assertEqual(init_text, 'VALUE = "ok"')
        self.assertEqual(extra_text, 'EXTRA = "ok"')
        self.assertTrue(manifest["allow_extra_new_files"])
        self.assertIn("package/extra.py", manifest["changed_paths"])

    def test_agent_applies_mixed_search_replace_and_file_artifacts(self):
        class FakeClient:
            def __init__(self, _config):
                pass

            def complete(self, _messages, agent_level="default"):
                return (
                    "BEGIN_FILE: new.py\n"
                    "VALUE = 'new'\n"
                    "END_FILE\n\n"
                    "BEGIN_SEARCH_REPLACE: app.py\n"
                    "<<<<<<< SEARCH\n"
                    "VALUE = 'old'\n"
                    "=======\n"
                    "VALUE = 'updated'\n"
                    ">>>>>>> REPLACE\n"
                    "END_SEARCH_REPLACE\n"
                )

        with tempfile.TemporaryDirectory() as temp:
            project, skills_dir = self.make_agent_project(Path(temp))
            run_dir = project / "run"
            (project / "app.py").write_text("VALUE = 'old'\n", encoding="utf-8")

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "apply mixed artifacts",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--skip-pm",
                        "--judge-mode",
                        "command-only",
                        "--include",
                        "app.py",
                        "--new-file",
                        "new.py",
                        "--apply",
                        "--artifact-format",
                        "legacy",
                        "--test-command",
                        f"{sys.executable} -m py_compile app.py new.py",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            app_text = (project / "app.py").read_text(encoding="utf-8")
            new_text = (project / "new.py").read_text(encoding="utf-8")

        self.assertEqual(result, 0)
        self.assertEqual(app_text, "VALUE = 'updated'\n")
        self.assertEqual(new_text, "VALUE = 'new'")

    def test_agent_can_use_external_spec_file(self):
        calls = []
        patch = """diff --git a/hello.txt b/hello.txt
new file mode 100644
index 0000000..45b983b
--- /dev/null
+++ b/hello.txt
@@ -0,0 +1 @@
+hi
"""
        outputs = [
            "pm control",
            patch,
            "判定: 承認\nEvidence passed.",
        ]

        class FakeClient:
            def __init__(self, config):
                self.config = config

            def complete(self, messages):
                calls.append(messages)
                return outputs[len(calls) - 1]

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            project.mkdir()
            spec_file = Path(temp) / "attached_spec.md"
            spec_file.write_text("# External SPEC\nRedis benchmark\n", encoding="utf-8")
            skills_dir = Path(temp) / "skills"
            for name in ("sdlc", "tdd", "review"):
                skill_dir = skills_dir / name
                skill_dir.mkdir(parents=True)
                (skill_dir / "SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: {name}\n---\n# {name}\n",
                    encoding="utf-8",
                )
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "create hello",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--spec-file",
                        str(spec_file),
                        "--new-file",
                        "hello.txt",
                        "--apply",
                        "--test-command",
                        f"{sys.executable} -c \"from pathlib import Path; assert Path('hello.txt').read_text() == 'hi\\n'\"",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

        self.assertEqual(result, 0)
        self.assertIn("# External SPEC", calls[0][1]["content"])


    def test_agent_recovers_from_bad_patch_with_file_artifact(self):
        calls = []
        bad_patch = """--- a/index.html
+++ b/index.html
@@ -1 +1 @@
-broken
"""
        artifact = """BEGIN_FILE: index.html
<!doctype html>
<html>
<body>fixed</body>
</html>
END_FILE"""
        outputs = [
            "pm control",
            bad_patch,
            artifact,
            "判定: 承認\nEvidence passed.",
        ]

        class FakeClient:
            def __init__(self, config):
                self.config = config

            def complete(self, messages):
                calls.append(messages)
                return outputs[len(calls) - 1]

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            skills_dir = project / "skills"
            for name in ("sdlc", "tdd", "review"):
                skill_dir = skills_dir / name
                skill_dir.mkdir(parents=True)
                (skill_dir / "SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: {name}\n---\n# {name}\n",
                    encoding="utf-8",
                )
            (project / "SPEC.md").write_text("# SPEC\n", encoding="utf-8")
            (project / "index.html").write_text("broken\n", encoding="utf-8")
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "fix index",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--include",
                        "index.html",
                        "--apply",
                        "--max-rounds",
                        "2",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            fixed_text = (project / "index.html").read_text(encoding="utf-8")

        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 4)
        self.assertEqual(manifest["completed_rounds"], 2)
        self.assertEqual(manifest["final_verdict"], "approved")
        self.assertIn("<body>fixed</body>", fixed_text)
        self.assertIn("Patch apply round 1", calls[2][1]["content"])
        self.assertIn("BEGIN_APPEND_FILE", calls[2][1]["content"])

    def test_agent_mixed_same_path_artifacts_write_transition_without_crashing(self):
        calls = []
        coder_doc = """{"artifacts": [
  {"type": "search_replace", "path": "app.py", "search": "print('old')\\n", "replace": "print('new')\\n"},
  {"type": "replace_file", "path": "app.py", "content": "print('new')\\n"}
]}"""

        class FakeClient:
            def __init__(self, config):
                self.config = config

            def complete(self, messages):
                calls.append(messages)
                return coder_doc

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            skills_dir = project / "skills"
            for name in ("sdlc", "tdd", "review"):
                skill_dir = skills_dir / name
                skill_dir.mkdir(parents=True)
                (skill_dir / "SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: {name}\n---\n# {name}\n",
                    encoding="utf-8",
                )
            (project / "SPEC.md").write_text("# SPEC\n", encoding="utf-8")
            (project / "app.py").write_text("print('old')\n", encoding="utf-8")
            run_dir = project / "run"

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "fix app",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--include",
                        "app.py",
                        "--apply",
                        "--skip-pm",
                        "--max-rounds",
                        "1",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            lint_doc = (run_dir / "03-r01-artifact-lint.md").read_text(encoding="utf-8")
            transition_doc = (run_dir / "03-r01-failure-transition.md").read_text(encoding="utf-8")
            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(result, 1)
        self.assertGreaterEqual(len(calls), 1)
        self.assertIn("stream_mixed_artifact_formats", lint_doc)
        self.assertIn("- failure_type: stream_mixed_artifact_formats", transition_doc)
        self.assertIn(manifest["final_verdict"], {"needs_changes", "not_judged"})

    def test_html_smoke_flags_broken_tetris_file(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            run_dir = project / "run"
            run_dir.mkdir()
            (project / "tetris.html").write_text(
                "<!doctype html><html><body><div id='game-board'></div><script>",
                encoding="utf-8",
            )

            results = self.local_sdlc.run_html_smoke_checks(
                project,
                ["tetris.html"],
                run_dir,
                timeout=1.0,
                tetris_checks=True,
            )

        self.assertTrue(results)
        self.assertFalse(all(ok for _doc, ok in results))
        self.assertIn("inline script tag is not closed", results[0][0])

    def test_tetris_initial_render_sequence_ignores_indentation(self):
        self.assertTrue(
            self.local_sdlc.has_tetris_initial_render_sequence(
                "  initBoard();\n  renderBoard();\n"
            )
        )
        self.assertTrue(
            self.local_sdlc.has_tetris_initial_render_sequence(
                "initBoard();\n        renderBoard();\n"
            )
        )
        self.assertTrue(
            self.local_sdlc.has_tetris_initial_render_sequence(
                "function createBoardCells() {}\ncreateBoardCells();\n"
            )
        )
        self.assertFalse(self.local_sdlc.has_tetris_initial_render_sequence("renderBoard();\ninitBoard();"))

    def test_timeout_error_reports_health_probe_result(self):
        config = self.local_sdlc.LLMConfig(
            base_url="http://localhost:30000/v1",
            api_key="dummy-local",
            model="test-model",
            timeout=1.0,
            health_timeout=0.1,
            temperature=0.0,
            max_tokens=16,
            disable_thinking=True,
        )
        client = self.local_sdlc.LocalLLMClient(config)
        probes = []

        def fake_request(method, path, payload=None, timeout=None):
            if path == "/models":
                probes.append((method, path, timeout))
                return {"data": [{"id": "alive-model"}]}
            if path == "/chat/completions":
                raise self.local_sdlc.LLMTimeoutError(path, config.timeout)
            raise AssertionError(f"unexpected path: {path}")

        client._request = fake_request

        with self.assertRaises(self.local_sdlc.RunnerError) as caught:
            client.complete([{"role": "user", "content": "hello"}])

        self.assertIn("timed out", str(caught.exception))
        self.assertIn("API health after timeout: alive", str(caught.exception))
        self.assertEqual(
            probes,
            [
                ("GET", "/models", config.health_timeout),
                ("GET", "/models", config.health_timeout),
            ],
        )

    def test_complete_preflights_health_before_generation_timeout(self):
        config = self.local_sdlc.LLMConfig(
            base_url="http://localhost:30000/v1",
            api_key="dummy-local",
            model="test-model",
            timeout=10.0,
            health_timeout=0.2,
            temperature=0.0,
            max_tokens=16,
            disable_thinking=True,
        )
        client = self.local_sdlc.LocalLLMClient(config)
        calls = []

        def fake_request(method, path, payload=None, timeout=None):
            calls.append((method, path, timeout))
            if path == "/models":
                return {"data": [{"id": "alive-model"}]}
            return {"choices": [{"message": {"content": "ok"}}]}

        client._request = fake_request
        result = client.complete([{"role": "user", "content": "hello"}])

        self.assertEqual(result, "ok")
        self.assertEqual(
            calls,
            [
                ("GET", "/models", config.health_timeout),
                ("POST", "/chat/completions", None),
            ],
        )

    def test_complete_uses_role_specific_payload_settings(self):
        config = self.local_sdlc.LLMConfig(
            base_url="http://localhost:30000/v1",
            api_key="dummy-local",
            model="test-model",
            timeout=10.0,
            health_timeout=0.2,
            temperature=0.2,
            max_tokens=4096,
            disable_thinking=True,
            role_overrides={
                "coder": self.local_sdlc.LLMRoleOverride(
                    temperature=0.1,
                    max_tokens=65536,
                    disable_thinking=True,
                ),
                "judge": self.local_sdlc.LLMRoleOverride(
                    temperature=0.0,
                    max_tokens=2048,
                    disable_thinking=True,
                ),
            },
        )
        client = self.local_sdlc.LocalLLMClient(config)
        payloads = []

        def fake_request(method, path, payload=None, timeout=None):
            if path == "/models":
                return {"data": [{"id": "test-model"}]}
            payloads.append(payload)
            return {"choices": [{"message": {"content": "ok"}}]}

        client._request = fake_request
        result = client.complete([{"role": "user", "content": "hello"}], agent_level="coder")

        self.assertEqual(result, "ok")
        self.assertEqual(payloads[0]["temperature"], 0.1)
        self.assertEqual(payloads[0]["max_tokens"], 65536)
        self.assertEqual(payloads[0]["chat_template_kwargs"], {"enable_thinking": False})

    def test_complete_uses_function_profile_over_role_profile(self):
        config = self.local_sdlc.LLMConfig(
            base_url="http://localhost:30000/v1",
            api_key="dummy-local",
            model="test-model",
            timeout=10.0,
            health_timeout=0.2,
            temperature=0.2,
            max_tokens=4096,
            disable_thinking=True,
            role_overrides={
                "coder": self.local_sdlc.LLMRoleOverride(
                    temperature=0.2,
                    max_tokens=1234,
                    disable_thinking=True,
                ),
            },
            function_overrides={
                "repair_artifact": self.local_sdlc.LLMRoleOverride(
                    temperature=0.0,
                    max_tokens=7777,
                    disable_thinking=True,
                ),
            },
        )
        client = self.local_sdlc.LocalLLMClient(config)
        payloads = []

        def fake_request(method, path, payload=None, timeout=None):
            if path == "/models":
                return {"data": [{"id": "test-model"}]}
            payloads.append(payload)
            return {"choices": [{"message": {"content": "ok"}}]}

        client._request = fake_request
        result = client.complete(
            [{"role": "user", "content": "hello"}],
            agent_level="coder",
            call_function="repair_artifact",
        )

        self.assertEqual(result, "ok")
        self.assertEqual(payloads[0]["temperature"], 0.0)
        self.assertEqual(payloads[0]["max_tokens"], 7777)

    def test_complete_records_reasoning_content_without_mixing_into_result(self):
        config = self.local_sdlc.LLMConfig(
            base_url="http://localhost:30000/v1",
            api_key="dummy-local",
            model="test-model",
            timeout=10.0,
            health_timeout=0.2,
            temperature=0.0,
            max_tokens=16,
            disable_thinking=True,
            function_overrides={
                "failure_analysis": self.local_sdlc.LLMRoleOverride(disable_thinking=False),
            },
        )
        client = self.local_sdlc.LocalLLMClient(config)
        payloads = []

        def fake_request(method, path, payload=None, timeout=None):
            if path == "/models":
                return {"data": [{"id": "test-model"}]}
            payloads.append(payload)
            return {
                "choices": [
                    {
                        "message": {
                            "reasoning_content": "private chain used for analysis",
                            "content": '{"verdict":"ok"}',
                        }
                    }
                ]
            }

        client._request = fake_request
        result = client.complete(
            [{"role": "user", "content": "analyze"}],
            agent_level="judge",
            call_function="failure_analysis",
        )

        self.assertEqual(result, '{"verdict":"ok"}')
        self.assertNotIn("chat_template_kwargs", payloads[0])
        records = self.local_sdlc.llm_reasoning_manifest(client)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["call_function"], "failure_analysis")
        self.assertEqual(records[0]["reasoning_content"], "private chain used for analysis")

    def test_complete_streams_content_to_partial_file(self):
        config = self.local_sdlc.LLMConfig(
            base_url="http://localhost:30000/v1",
            api_key="dummy-local",
            model="test-model",
            timeout=10.0,
            health_timeout=0.2,
            temperature=0.2,
            max_tokens=4096,
            disable_thinking=True,
            stream=True,
        )
        client = self.local_sdlc.LocalLLMClient(config)

        def fake_request(method, path, payload=None, timeout=None):
            self.assertEqual(path, "/models")
            return {"data": [{"id": "test-model"}]}

        class FakeStreamResponse:
            status = 200
            headers = {"content-type": "text/event-stream; charset=utf-8"}

            def __enter__(self):
                return self

            def __exit__(self, _exc_type, _exc, _tb):
                return False

            def __iter__(self):
                events = [
                    {"choices": [{"delta": {"role": "assistant", "content": ""}}]},
                    {"choices": [{"delta": {"content": "hello"}}]},
                    {"choices": [{"delta": {"content": " world"}}]},
                ]
                for event in events:
                    yield ("data: " + json.dumps(event) + "\n\n").encode("utf-8")
                yield b"data: [DONE]\n\n"

        captured_payloads = []

        def fake_urlopen(request, timeout=None):
            captured_payloads.append(json.loads(request.data.decode("utf-8")))
            return FakeStreamResponse()

        client._request = fake_request
        original_urlopen = self.local_sdlc.urllib.request.urlopen
        self.local_sdlc.urllib.request.urlopen = fake_urlopen
        callbacks = []
        try:
            with tempfile.TemporaryDirectory() as temp:
                partial = Path(temp) / "partial.md"
                result = client.complete(
                    [{"role": "user", "content": "hello"}],
                    agent_level="coder",
                    stream_output_path=partial,
                    stream_callback=lambda stats: callbacks.append(stats),
                )
                partial_text = partial.read_text(encoding="utf-8")
        finally:
            self.local_sdlc.urllib.request.urlopen = original_urlopen

        self.assertEqual(result, "hello world")
        self.assertEqual(partial_text, "hello world")
        self.assertTrue(captured_payloads[0]["stream"])
        self.assertEqual(captured_payloads[0]["chat_template_kwargs"], {"enable_thinking": False})
        self.assertGreaterEqual(callbacks[-1].content_chunks, 2)
        self.assertEqual(callbacks[-1].bytes_received, len("hello world".encode("utf-8")))

    def test_complete_stream_aborts_on_guard(self):
        config = self.local_sdlc.LLMConfig(
            base_url="http://localhost:30000/v1",
            api_key="dummy-local",
            model="test-model",
            timeout=10.0,
            health_timeout=0.2,
            temperature=0.2,
            max_tokens=4096,
            disable_thinking=True,
            stream=True,
        )
        client = self.local_sdlc.LocalLLMClient(config)

        def fake_request(method, path, payload=None, timeout=None):
            self.assertEqual(path, "/models")
            return {"data": [{"id": "test-model"}]}

        class FakeStreamResponse:
            headers = {"content-type": "text/event-stream; charset=utf-8"}

            def __enter__(self):
                return self

            def __exit__(self, _exc_type, _exc, _tb):
                return False

            def __iter__(self):
                chunk = '{"type":"search_replace","path":"app.py","search":"A","replace":"B"},'
                for _index in range(9):
                    yield ("data: " + json.dumps({"choices": [{"delta": {"content": chunk}}]}) + "\n\n").encode("utf-8")
                yield b"data: [DONE]\n\n"

        def fake_urlopen(_request, timeout=None):
            return FakeStreamResponse()

        client._request = fake_request
        original_urlopen = self.local_sdlc.urllib.request.urlopen
        self.local_sdlc.urllib.request.urlopen = fake_urlopen
        try:
            with tempfile.TemporaryDirectory() as temp:
                partial = Path(temp) / "partial.md"
                with self.assertRaises(self.local_sdlc.LLMStreamAbortError) as ctx:
                    client.complete(
                        [{"role": "user", "content": "hello"}],
                        agent_level="coder",
                        stream_output_path=partial,
                        stream_guard=self.local_sdlc.artifact_stream_guard,
                    )
                partial_text = partial.read_text(encoding="utf-8")
        finally:
            self.local_sdlc.urllib.request.urlopen = original_urlopen

        self.assertIn("too many JSON search_replace", ctx.exception.reason)
        self.assertIn('"type":"search_replace"', partial_text)

    def test_build_config_sets_default_role_profiles_from_cli(self):
        args = self.local_sdlc.build_parser().parse_args(["doctor", "--skip-llm"])
        config = self.local_sdlc.build_config(args)
        client = self.local_sdlc.LocalLLMClient(config)

        self.assertEqual(client.call_settings("pm").max_tokens, 8192)
        self.assertEqual(client.call_settings("coder").max_tokens, 65536)
        self.assertEqual(client.call_settings("judge").max_tokens, 8192)
        self.assertEqual(client.call_settings("judge").temperature, 0.0)
        self.assertTrue(client.call_settings("coder").disable_thinking)

    def test_build_config_reads_project_json_api_config(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "local_sdlc.json").write_text(
                json.dumps(
                    {
                        "llm": {
                            "base_url": "https://api.example.test/v1",
                            "api_key_env": "TEST_LOCAL_SDLC_KEY",
                            "model_profile": "qwen-agent",
                            "model": "configured-model",
                            "timeout": 123,
                            "health_timeout": 4,
                            "stream": True,
                            "function_profiles": {
                                "failure_analysis": {
                                    "model": "analysis-model",
                                    "max_tokens": 7777,
                                    "temperature": 0,
                                    "thinking": "off",
                                }
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            args = self.local_sdlc.build_parser().parse_args(
                ["doctor", "--project", str(project), "--skip-llm"]
            )
            with self.scrub_llm_env({"TEST_LOCAL_SDLC_KEY": "secret-from-env"}):
                config = self.local_sdlc.build_config(args)

        client = self.local_sdlc.LocalLLMClient(config)
        analysis = client.call_settings("judge", "failure_analysis")
        generated = client.call_settings("coder", "generate_artifact")
        self.assertEqual(config.base_url, "https://api.example.test/v1")
        self.assertEqual(config.api_key, "secret-from-env")
        self.assertEqual(config.model, "configured-model")
        self.assertEqual(config.model_profile, "qwen-agent")
        self.assertEqual(config.timeout, 123)
        self.assertEqual(config.health_timeout, 4)
        self.assertTrue(config.stream)
        self.assertEqual(analysis.model, "analysis-model")
        self.assertEqual(analysis.max_tokens, 7777)
        self.assertTrue(analysis.disable_thinking)
        self.assertEqual(generated.max_tokens, 49152)

    def test_build_config_reads_yaml_api_config(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "local_sdlc.yaml").write_text(
                textwrap.dedent(
                    """
                    llm:
                      base_url: https://yaml.example/v1
                      api_key_env: YAML_SDLC_KEY
                      model_profile: ornith-agent
                      role_profiles:
                        coder:
                          max_tokens: 1234
                          temperature: 0.03
                          thinking: off
                      api_profile:
                        - repair_artifact:max_tokens=4321,temperature=0,thinking=off
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            args = self.local_sdlc.build_parser().parse_args(
                ["doctor", "--project", str(project), "--skip-llm"]
            )
            with self.scrub_llm_env({"YAML_SDLC_KEY": "yaml-secret"}):
                config = self.local_sdlc.build_config(args)

        client = self.local_sdlc.LocalLLMClient(config)
        coder = client.call_settings("coder")
        repair = client.call_settings("coder", "repair_artifact")
        self.assertEqual(config.base_url, "https://yaml.example/v1")
        self.assertEqual(config.api_key, "yaml-secret")
        self.assertEqual(config.model_profile, "ornith-agent")
        self.assertEqual(coder.max_tokens, 1234)
        self.assertEqual(coder.temperature, 0.03)
        self.assertTrue(coder.disable_thinking)
        self.assertEqual(repair.max_tokens, 4321)

    def test_build_config_cli_overrides_project_config(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "custom.json").write_text(
                json.dumps(
                    {
                        "llm": {
                            "base_url": "https://config.example/v1",
                            "api_key": "config-secret",
                            "model_profile": "qwen-agent",
                            "model": "config-model",
                            "timeout": 999,
                            "api_profile": [
                                "failure_analysis:model=config-analysis,max_tokens=111,temperature=0,thinking=off"
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )
            args = self.local_sdlc.build_parser().parse_args(
                [
                    "doctor",
                    "--project",
                    str(project),
                    "--config-file",
                    "custom.json",
                    "--base-url",
                    "https://cli.example/v1",
                    "--api-key",
                    "cli-secret",
                    "--model",
                    "cli-model",
                    "--model-profile",
                    "ornith-agent",
                    "--timeout",
                    "12",
                    "--api-profile",
                    "failure_analysis:model=cli-analysis,max_tokens=222,temperature=0,thinking=off",
                    "--skip-llm",
                ]
            )
            with self.scrub_llm_env():
                config = self.local_sdlc.build_config(args)

        client = self.local_sdlc.LocalLLMClient(config)
        analysis = client.call_settings("judge", "failure_analysis")
        self.assertEqual(config.base_url, "https://cli.example/v1")
        self.assertEqual(config.api_key, "cli-secret")
        self.assertEqual(config.model, "cli-model")
        self.assertEqual(config.model_profile, "ornith-agent")
        self.assertEqual(config.timeout, 12)
        self.assertEqual(analysis.model, "cli-analysis")
        self.assertEqual(analysis.max_tokens, 222)

    def test_build_config_allows_role_overrides_from_cli(self):
        args = self.local_sdlc.build_parser().parse_args(
            [
                "doctor",
                "--skip-llm",
                "--coder-max-tokens",
                "32768",
                "--coder-temperature",
                "0.05",
                "--coder-thinking",
                "on",
            ]
        )
        config = self.local_sdlc.build_config(args)
        client = self.local_sdlc.LocalLLMClient(config)

        settings = client.call_settings("coder")
        self.assertEqual(settings.max_tokens, 32768)
        self.assertEqual(settings.temperature, 0.05)
        self.assertFalse(settings.disable_thinking)

    def test_build_config_allows_function_profile_overrides_from_cli(self):
        args = self.local_sdlc.build_parser().parse_args(
            [
                "doctor",
                "--skip-llm",
                "--api-profile",
                "repair_artifact:max_tokens=32768,temperature=0,thinking=off",
            ]
        )
        config = self.local_sdlc.build_config(args)
        client = self.local_sdlc.LocalLLMClient(config)

        settings = client.call_settings("coder", "repair_artifact")
        self.assertEqual(settings.call_function, "repair_artifact")
        self.assertEqual(settings.max_tokens, 32768)
        self.assertEqual(settings.temperature, 0.0)
        self.assertTrue(settings.disable_thinking)

    def test_build_config_applies_qwen_model_profile_before_cli_overrides(self):
        args = self.local_sdlc.build_parser().parse_args(
            [
                "doctor",
                "--skip-llm",
                "--model-profile",
                "qwen-agent",
                "--api-profile",
                "repair_artifact:max_tokens=12345,temperature=0.02,thinking=off",
            ]
        )
        config = self.local_sdlc.build_config(args)
        client = self.local_sdlc.LocalLLMClient(config)

        generated = client.call_settings("coder", "generate_artifact")
        repaired = client.call_settings("coder", "repair_artifact")
        self.assertEqual(config.model, "qwen3.5-122b")
        self.assertEqual(generated.model, "qwen3.5-122b")
        self.assertEqual(generated.max_tokens, 49152)
        self.assertEqual(generated.temperature, 0.05)
        self.assertTrue(generated.disable_thinking)
        self.assertEqual(repaired.max_tokens, 12345)
        self.assertEqual(repaired.temperature, 0.02)

    def test_qwen_agent_uses_thinking_only_for_analysis_functions(self):
        args = self.local_sdlc.build_parser().parse_args(
            ["doctor", "--skip-llm", "--model-profile", "qwen-agent"]
        )
        config = self.local_sdlc.build_config(args)
        client = self.local_sdlc.LocalLLMClient(config)

        analysis_functions = [
            "route_task",
            "plan_work",
            "explore_code",
            "failure_analysis",
            "patch_planner",
            "project_policy_triage",
            "root_cause_analysis",
            "judge_review",
            "verify_acceptance",
        ]
        artifact_functions = [
            "generate_artifact",
            "repair_artifact",
            "root_cause_patch",
            "artifact_writer",
            "semantic_repair",
            "format_repair",
        ]

        for function_name in analysis_functions:
            with self.subTest(function_name=function_name):
                self.assertFalse(client.call_settings("default", function_name).disable_thinking)
        for function_name in artifact_functions:
            with self.subTest(function_name=function_name):
                self.assertTrue(client.call_settings("default", function_name).disable_thinking)

    def test_llm_model_profile_manifest_reports_overrides(self):
        args = self.local_sdlc.build_parser().parse_args(
            ["doctor", "--skip-llm", "--model-profile", "qwen-agent"]
        )
        manifest = self.local_sdlc.llm_model_profile_manifest(args)

        self.assertEqual(manifest["profile"], "qwen-agent")
        self.assertEqual(manifest["default_model"], "qwen3.5-122b")
        self.assertIn("generate_artifact", manifest["function_overrides"])
        self.assertEqual(
            manifest["function_overrides"]["generate_artifact"]["max_tokens"],
            49152,
        )

    def test_build_config_supports_ornith_model_profile(self):
        args = self.local_sdlc.build_parser().parse_args(
            ["doctor", "--skip-llm", "--model-profile", "ornith-agent"]
        )
        config = self.local_sdlc.build_config(args)
        client = self.local_sdlc.LocalLLMClient(config)

        settings = client.call_settings("coder", "generate_artifact")
        self.assertEqual(config.model, "Ornith-1.0-35B")
        self.assertEqual(settings.model, "Ornith-1.0-35B")
        self.assertEqual(settings.max_tokens, 65536)
        self.assertEqual(settings.temperature, 0.1)

    def test_build_config_supports_nemotron3_super_model_profile(self):
        args = self.local_sdlc.build_parser().parse_args(
            ["doctor", "--skip-llm", "--model-profile", "nemotron3-super-agent"]
        )
        config = self.local_sdlc.build_config(args)
        client = self.local_sdlc.LocalLLMClient(config)

        settings = client.call_settings("coder", "generate_artifact")
        self.assertEqual(config.model, "nemotron-3-super")
        self.assertEqual(settings.model, "nemotron-3-super")
        self.assertEqual(settings.max_tokens, 32768)
        self.assertEqual(settings.temperature, 0.05)
        self.assertTrue(settings.disable_thinking)

    def test_build_config_supports_nemotron_labs_puzzle_model_profile(self):
        args = self.local_sdlc.build_parser().parse_args(
            ["doctor", "--skip-llm", "--model-profile", "nemotron-puzzle"]
        )
        config = self.local_sdlc.build_config(args)
        client = self.local_sdlc.LocalLLMClient(config)

        settings = client.call_settings("coder", "generate_artifact")
        self.assertEqual(config.model, "nemotron-labs-3-puzzle-75b-a9b")
        self.assertEqual(settings.model, "nemotron-labs-3-puzzle-75b-a9b")
        self.assertEqual(settings.max_tokens, 6144)
        self.assertEqual(settings.temperature, 0.05)
        self.assertTrue(settings.disable_thinking)

    def test_api_profile_can_override_model_for_one_function(self):
        args = self.local_sdlc.build_parser().parse_args(
            [
                "doctor",
                "--skip-llm",
                "--model-profile",
                "qwen-agent",
                "--api-profile",
                "failure_analysis:model=Ornith-1.0-35B,max_tokens=9999,temperature=0,thinking=off",
            ]
        )
        config = self.local_sdlc.build_config(args)
        client = self.local_sdlc.LocalLLMClient(config)

        analysis = client.call_settings("judge", "failure_analysis")
        artifact = client.call_settings("coder", "generate_artifact")
        self.assertEqual(analysis.model, "Ornith-1.0-35B")
        self.assertEqual(analysis.max_tokens, 9999)
        self.assertEqual(artifact.model, "qwen3.5-122b")

    def test_llm_capability_probes_detect_reasoning_only_default(self):
        config = self.local_sdlc.LLMConfig(
            base_url="http://localhost:30000/v1",
            api_key="dummy-local",
            model="test-model",
            timeout=10.0,
            health_timeout=0.2,
            temperature=0.0,
            max_tokens=16,
            disable_thinking=True,
        )
        client = self.local_sdlc.LocalLLMClient(config)
        payloads = []

        def fake_request(method, path, payload=None, timeout=None):
            self.assertEqual(method, "POST")
            self.assertEqual(path, "/chat/completions")
            payloads.append(payload)
            if payload.get("chat_template_kwargs") == {"enable_thinking": False} and payload["max_tokens"] == 16:
                return {"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]}
            if payload.get("chat_template_kwargs") == {"enable_thinking": False} and payload["max_tokens"] == 64:
                return {
                    "choices": [
                        {
                            "message": {"content": '{"ok":true,"files":["a.py"]}'},
                            "finish_reason": "stop",
                        }
                    ]
                }
            return {
                "choices": [
                    {
                        "message": {"content": None, "reasoning": "thinking"},
                        "finish_reason": "length",
                    }
                ]
            }

        client._request = fake_request
        results = self.local_sdlc.run_llm_capability_probes(client, "test-model", timeout=3.0)

        statuses = {result.name: result.status for result in results}
        self.assertEqual(statuses["no_thinking_content"], "PASS")
        self.assertEqual(statuses["default_thinking_behavior"], "WARN")
        self.assertEqual(statuses["json_artifact_content"], "PASS")
        self.assertEqual(payloads[0]["chat_template_kwargs"], {"enable_thinking": False})
        self.assertNotIn("chat_template_kwargs", payloads[1])

    def test_llm_role_recommendations_include_ornith_vllm_settings(self):
        recommendations = "\n".join(self.local_sdlc.llm_role_recommendations("Ornith-1.0-35B"))

        self.assertIn("--reasoning-parser qwen3", recommendations)
        self.assertIn("--tool-call-parser qwen3_xml", recommendations)
        self.assertIn("enable_thinking=false", recommendations)

    def test_module_imports_without_external_dependencies(self):
        self.assertTrue(hasattr(self.local_sdlc, "LocalLLMClient"))

    def test_entrypoint_help_delegates_to_cli_module(self):
        result = subprocess.run(
            [sys.executable, str(ENTRYPOINT_PATH), "--help"],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Run SDLC skills with a configurable OpenAI-compatible LLM API", result.stdout)
        self.assertNotRegex(result.stdout, product_name_pattern())
        self.assertIn("agent", result.stdout)

if __name__ == "__main__":
    unittest.main()
