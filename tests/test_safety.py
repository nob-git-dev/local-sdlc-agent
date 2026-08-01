import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

from tests.helpers import LocalSDLCTestCase


class SafetyTests(LocalSDLCTestCase):
    def test_cli_approval_and_status_target_existing_decision(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            run_dir = project / "run"
            self.local_sdlc.run_checked_command(project, "docker ps", 5, run_dir)
            decision_id = self.local_sdlc.read_safety_decisions(run_dir)[0]["decision_id"]

            approve_args = self.local_sdlc.build_parser().parse_args(
                [
                    "approve",
                    "--run-dir",
                    str(run_dir),
                    "--decision-id",
                    decision_id,
                    "--note",
                    "reviewed",
                ]
            )
            approve_output = io.StringIO()
            with contextlib.redirect_stdout(approve_output):
                approve_result = approve_args.func(approve_args)

            status_args = self.local_sdlc.build_parser().parse_args(
                ["safety-status", "--run-dir", str(run_dir)]
            )
            status_output = io.StringIO()
            with contextlib.redirect_stdout(status_output):
                status_result = status_args.func(status_args)

            approval = json.loads(approve_output.getvalue())
            status = json.loads(status_output.getvalue())

        self.assertEqual(approve_result, 0)
        self.assertEqual(status_result, 0)
        self.assertEqual(approval["decision_id"], decision_id)
        self.assertEqual(approval["source"], "cli")
        self.assertEqual(status["pending"], [])
        self.assertEqual(status["decision_count"], 1)

    def test_safety_status_prefers_final_manifest_over_stale_partial(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            run_dir.mkdir(parents=True)
            stale = {
                "decision_id": "D000099",
                "decision": "require_approval",
                "run_dir": str(run_dir),
            }
            (run_dir / "run.partial.json").write_text(
                json.dumps({"pending_safety_decisions": [stale]}),
                encoding="utf-8",
            )
            (run_dir / "run.json").write_text(
                json.dumps({"pending_safety_decisions": []}),
                encoding="utf-8",
            )
            args = self.local_sdlc.build_parser().parse_args(
                ["safety-status", "--run-dir", str(run_dir)]
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = args.func(args)
            status = json.loads(output.getvalue())

        self.assertEqual(result, 0)
        self.assertEqual(status["pending"], [])

    def test_safety_status_reports_child_stage_approval_target(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "parent"
            child_dir = run_dir / "s01-child"
            decision = self.local_sdlc.action_safety_decision(
                "docker_check",
                action_type="command",
                risk_class="docker_control",
                command="docker ps",
            )
            persisted = self.local_sdlc.authorize_safety_decision(child_dir, decision)
            blocked = self.local_sdlc.authorize_safety_decision(
                child_dir,
                self.local_sdlc.action_safety_decision(
                    "history_rewrite",
                    action_type="command",
                    risk_class="git_history_rewrite",
                    command="git reset --hard",
                ),
            )
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "run.json").write_text(
                json.dumps(
                    {
                        "pending_safety_decisions": [
                            {**persisted, "run_dir": str(child_dir.resolve()), "stage_id": "S01"}
                        ],
                        "blocked_safety_decisions": [
                            {**blocked, "run_dir": str(child_dir.resolve()), "stage_id": "S01"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            args = self.local_sdlc.build_parser().parse_args(
                ["safety-status", "--run-dir", str(run_dir)]
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = args.func(args)
            status = json.loads(output.getvalue())

        self.assertEqual(result, 0)
        self.assertEqual(status["pending"][0]["stage_id"], "S01")
        self.assertEqual(status["pending"][0]["run_dir"], str(child_dir.resolve()))
        self.assertEqual(status["blocked"][0]["stage_id"], "S01")

    def test_action_gate_records_safety_before_work_and_audits_cleanly(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"

            decision = self.local_sdlc.begin_action(
                run_dir,
                "artifact_apply",
                action_type="artifact_apply",
                risk_class="project_write",
                metadata={"isolated": True},
            )
            events = self.local_sdlc.read_progress_events(run_dir)
            audit = self.local_sdlc.action_gate_audit(run_dir)

        self.assertEqual(decision["decision"], "allow_in_worktree")
        self.assertEqual(events[0]["metadata"]["safety_decision_id"], decision["decision_id"])
        self.assertEqual(audit["status"], "pass")
        self.assertTrue(audit["safety_precedes_work"])

    def test_action_gate_audit_detects_legacy_unauthorized_work_start(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            self.local_sdlc.record_work_start(run_dir, "unsafe_legacy_action")

            audit = self.local_sdlc.action_gate_audit(run_dir)

        self.assertEqual(audit["status"], "fail")
        self.assertFalse(audit["safety_precedes_work"])

    def test_human_approval_is_exact_one_time_and_audited(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            run_dir = project / "run"
            command = "docker version"
            first_document, first_ok = self.local_sdlc.run_checked_command(
                project,
                command,
                timeout=5,
                run_dir=run_dir,
            )
            first_decision = self.local_sdlc.read_safety_decisions(run_dir)[0]
            approval = self.local_sdlc.request_safety_approval(
                run_dir,
                first_decision["decision_id"],
                source="test",
            )
            completed = mock.Mock(returncode=0, stdout="ok\n", stderr="")
            with mock.patch("local_sdlc.verification.subprocess.run", return_value=completed) as run:
                second_document, second_ok = self.local_sdlc.run_checked_command(
                    project,
                    command,
                    timeout=5,
                    run_dir=run_dir,
                )
                third_document, third_ok = self.local_sdlc.run_checked_command(
                    project,
                    command,
                    timeout=5,
                    run_dir=run_dir,
                )
            decisions = self.local_sdlc.read_safety_decisions(run_dir)
            approvals = self.local_sdlc.read_safety_approvals(run_dir)

        self.assertFalse(first_ok)
        self.assertIn("requires human approval", first_document)
        self.assertTrue(second_ok)
        self.assertIn("status: PASS", second_document)
        self.assertFalse(third_ok)
        self.assertIn("requires human approval", third_document)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(decisions[1]["decision"], "allow")
        self.assertEqual(decisions[1]["approval_id"], approval["approval_id"])
        self.assertEqual([item["event"] for item in approvals], ["approved", "consumed"])

    def test_block_decision_cannot_be_human_approved(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            run_dir = project / "run"
            self.local_sdlc.run_checked_command(project, "git reset --hard", 5, run_dir)
            decision_id = self.local_sdlc.read_safety_decisions(run_dir)[0]["decision_id"]

            with self.assertRaises(self.local_sdlc.RunnerError):
                self.local_sdlc.request_safety_approval(run_dir, decision_id, source="test")

    def test_llm_cannot_be_an_approval_source(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            run_dir = project / "run"
            self.local_sdlc.run_checked_command(project, "docker ps", 5, run_dir)
            decision_id = self.local_sdlc.read_safety_decisions(run_dir)[0]["decision_id"]

            with self.assertRaises(self.local_sdlc.RunnerError):
                self.local_sdlc.request_safety_approval(run_dir, decision_id, source="llm")

    def test_unknown_risk_class_fails_closed_to_human_approval(self):
        decision = self.local_sdlc.action_safety_decision(
            "future_action",
            action_type="future_action",
            risk_class="unregistered_future_risk",
        )

        self.assertEqual(decision.decision, "require_approval")
        self.assertIn("unsupported risk class", decision.reason)

    def test_agent_precheck_stops_at_approval_required_without_coder_retry(self):
        calls = []

        class FakeClient:
            def __init__(self, _config):
                pass

            def complete(self, _messages, **_kwargs):
                calls.append(_kwargs)
                return "BEGIN_FILE: app.py\nprint('changed')\nEND_FILE"

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root)
            (project / "app.py").write_text("print('original')\n", encoding="utf-8")
            run_dir = project / "run"
            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "check app",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--include",
                        "app.py",
                        "--apply",
                        "--precheck",
                        "--skip-pm",
                        "--judge-mode",
                        "command-only",
                        "--test-command",
                        "docker ps",
                        "--max-rounds",
                        "3",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                with self.assertRaises(self.local_sdlc.RunnerError):
                    self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            partial = json.loads((run_dir / "run.partial.json").read_text(encoding="utf-8"))
            content = (project / "app.py").read_text(encoding="utf-8")

        self.assertEqual(calls, [])
        self.assertEqual(content, "print('original')\n")
        self.assertEqual(partial["final_verdict"], "approval_required")
        self.assertEqual(len(partial["pending_safety_decisions"]), 1)

    def test_agent_precheck_stops_at_safety_blocked_without_coder_retry(self):
        calls = []

        class FakeClient:
            def __init__(self, _config):
                pass

            def complete(self, _messages, **_kwargs):
                calls.append(_kwargs)
                return "BEGIN_FILE: app.py\nprint('changed')\nEND_FILE"

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root)
            (project / "app.py").write_text("print('original')\n", encoding="utf-8")
            run_dir = project / "run"
            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "check app",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--include",
                        "app.py",
                        "--apply",
                        "--precheck",
                        "--skip-pm",
                        "--judge-mode",
                        "command-only",
                        "--test-command",
                        "git reset --hard",
                        "--max-rounds",
                        "3",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                with self.assertRaises(self.local_sdlc.RunnerError):
                    self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            partial = json.loads((run_dir / "run.partial.json").read_text(encoding="utf-8"))
            content = (project / "app.py").read_text(encoding="utf-8")

        self.assertEqual(calls, [])
        self.assertEqual(content, "print('original')\n")
        self.assertEqual(partial["final_verdict"], "safety_blocked")
        self.assertEqual(len(partial["blocked_safety_decisions"]), 1)

    def test_run_checked_command_rejects_shell_operators(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            document, ok = self.local_sdlc.run_checked_command(
                project,
                f"{sys.executable} --version && {sys.executable} --version",
                timeout=5,
            )

        self.assertFalse(ok)
        self.assertIn("unsupported shell operator", document)
        self.assertIn("separate --test-command", document)

    def test_run_checked_command_records_allowed_safety_decision(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            run_dir = project / "run"
            document, ok = self.local_sdlc.run_checked_command(
                project,
                f"{sys.executable} --version",
                timeout=5,
                run_dir=run_dir,
            )
            decisions = self.local_sdlc.read_safety_decisions(run_dir)

        self.assertTrue(ok)
        self.assertIn("status: PASS", document)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["decision"], "allow")
        self.assertEqual(decisions[0]["risk_class"], "generated_code_execution")

    def test_run_checked_command_records_approval_required_safety_decision(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            run_dir = project / "run"
            document, ok = self.local_sdlc.run_checked_command(
                project,
                "sudo echo should-not-run",
                timeout=5,
                run_dir=run_dir,
            )
            decisions = self.local_sdlc.read_safety_decisions(run_dir)

        self.assertFalse(ok)
        self.assertIn("status: BLOCKED", document)
        self.assertIn("requires human approval", document)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["decision"], "require_approval")
        self.assertEqual(decisions[0]["risk_class"], "privileged_command")

    def test_run_checked_command_records_blocked_safety_decision(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            run_dir = project / "run"
            document, ok = self.local_sdlc.run_checked_command(
                project,
                "git reset --hard",
                timeout=5,
                run_dir=run_dir,
            )
            decisions = self.local_sdlc.read_safety_decisions(run_dir)

        self.assertFalse(ok)
        self.assertIn("status: BLOCKED", document)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["decision"], "block")
        self.assertEqual(decisions[0]["risk_class"], "git_history_rewrite")

    def test_run_checked_command_requires_approval_for_risky_class_without_legacy_block_reason(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            run_dir = project / "run"
            document, ok = self.local_sdlc.run_checked_command(
                project,
                "docker ps",
                timeout=5,
                run_dir=run_dir,
            )
            decisions = self.local_sdlc.read_safety_decisions(run_dir)

        self.assertFalse(ok)
        self.assertIn("requires human approval", document)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["decision"], "require_approval")
        self.assertEqual(decisions[0]["risk_class"], "docker_control")
