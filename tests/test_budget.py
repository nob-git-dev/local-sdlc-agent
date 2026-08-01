import contextlib
import io
import json
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

from tests.helpers import ENTRYPOINT_PATH, LocalSDLCTestCase


class RuntimeBudgetTests(LocalSDLCTestCase):
    def limits(self, **overrides):
        values = {
            "max_goal_actions": 100,
            "max_stage_actions": 100,
            "max_recovery_actions": 100,
            "max_api_calls": 100,
            "max_wall_seconds": 3600.0,
        }
        values.update(overrides)
        return self.local_sdlc.BudgetLimits(**values)

    def test_goal_budget_is_absorbing_and_auditable(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            self.local_sdlc.initialize_budget(
                run_dir,
                self.limits(max_goal_actions=2),
                scope_kind="goal_stage",
            )

            for index in range(2):
                self.local_sdlc.begin_action(
                    run_dir,
                    f"action_{index}",
                    action_type="orchestration",
                    risk_class="read_only",
                )
            with self.assertRaises(self.local_sdlc.BudgetExceeded):
                self.local_sdlc.begin_action(
                    run_dir,
                    "action_3",
                    action_type="orchestration",
                    risk_class="read_only",
                )
            with self.assertRaises(self.local_sdlc.BudgetExceeded):
                self.local_sdlc.begin_action(
                    run_dir,
                    "action_4",
                    action_type="orchestration",
                    risk_class="read_only",
                )

            status = self.local_sdlc.budget_status(run_dir)
            work = [item for item in self.local_sdlc.read_progress_events(run_dir) if item.get("starts_work")]
            audit = self.local_sdlc.action_gate_audit(run_dir)

        self.assertEqual(status["state"], "exhausted")
        self.assertEqual(status["usage"]["goal_actions"], 2)
        self.assertEqual(status["stop"]["dimension"], "goal_actions")
        self.assertEqual(len(work), 2)
        self.assertEqual(audit["status"], "pass")
        self.assertTrue(audit["budget_precedes_work"])

    def test_api_recovery_and_stage_budgets_are_independent(self):
        cases = (
            ("goal_stage", "api_call", "api_calls", {"max_api_calls": 1}),
            ("goal_stage", "recovery", "recovery_actions", {"max_recovery_actions": 1}),
            ("stage", "orchestration", "stage_actions", {"max_stage_actions": 1}),
        )
        for scope_kind, action_type, dimension, overrides in cases:
            with self.subTest(dimension=dimension), tempfile.TemporaryDirectory() as temp:
                run_dir = Path(temp) / "run"
                self.local_sdlc.initialize_budget(
                    run_dir,
                    self.limits(**overrides),
                    scope_kind=scope_kind,
                )
                self.local_sdlc.begin_action(
                    run_dir,
                    "first",
                    action_type=action_type,
                    risk_class="read_only",
                )
                with self.assertRaises(self.local_sdlc.BudgetExceeded):
                    self.local_sdlc.begin_action(
                        run_dir,
                        "second",
                        action_type=action_type,
                        risk_class="read_only",
                    )
                status = self.local_sdlc.budget_status(run_dir)

            self.assertEqual(status["stop"]["dimension"], dimension)
            self.assertEqual(status["usage"][dimension], 1)

    def test_zero_count_budget_disables_that_action_class(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            self.local_sdlc.initialize_budget(
                run_dir,
                self.limits(max_api_calls=0, max_recovery_actions=0),
                scope_kind="goal_stage",
            )
            self.local_sdlc.begin_action(
                run_dir,
                "setup",
                action_type="orchestration",
                risk_class="read_only",
            )
            with self.assertRaises(self.local_sdlc.BudgetExceeded):
                self.local_sdlc.begin_action(
                    run_dir,
                    "first_api",
                    action_type="api_call",
                    risk_class="read_only",
                )
            status = self.local_sdlc.budget_status(run_dir)

        self.assertEqual(status["usage"]["api_calls"], 0)
        self.assertEqual(status["stop"]["dimension"], "api_calls")

    def test_cancel_and_safety_denial_do_not_consume_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cancelled = root / "cancelled"
            blocked = root / "blocked"
            for run_dir in (cancelled, blocked):
                self.local_sdlc.initialize_budget(run_dir, self.limits(), scope_kind="goal_stage")

            self.local_sdlc.request_cancel(cancelled, source="test", reason="stop")
            with self.assertRaises(self.local_sdlc.RunnerError):
                self.local_sdlc.begin_action(
                    cancelled,
                    "api",
                    action_type="api_call",
                    risk_class="read_only",
                )
            with self.assertRaises(self.local_sdlc.SafetyGateDenied):
                self.local_sdlc.begin_action(
                    blocked,
                    "rewrite",
                    action_type="command",
                    risk_class="git_history_rewrite",
                    command="git reset --hard",
                )

            cancelled_status = self.local_sdlc.budget_status(cancelled)
            blocked_status = self.local_sdlc.budget_status(blocked)

        self.assertEqual(cancelled_status["usage"]["goal_actions"], 0)
        self.assertEqual(blocked_status["usage"]["goal_actions"], 0)

    def test_cancel_between_budget_and_work_start_refunds_admission(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            self.local_sdlc.initialize_budget(run_dir, self.limits(), scope_kind="goal_stage")
            from local_sdlc import action_gate

            original_record = action_gate.record_work_start

            def cancel_then_record(*args, **kwargs):
                self.local_sdlc.request_cancel(run_dir, source="test", reason="race")
                return original_record(*args, **kwargs)

            with mock.patch("local_sdlc.action_gate.record_work_start", side_effect=cancel_then_record):
                with self.assertRaises(self.local_sdlc.RunnerError):
                    self.local_sdlc.begin_action(
                        run_dir,
                        "racing_action",
                        action_type="api_call",
                        risk_class="read_only",
                    )
            status = self.local_sdlc.budget_status(run_dir)
            outcomes = [item["outcome"] for item in self.local_sdlc.read_budget_events(run_dir)]
            work = [item for item in self.local_sdlc.read_progress_events(run_dir) if item.get("starts_work")]

        self.assertEqual(status["usage"]["goal_actions"], 0)
        self.assertEqual(status["usage"]["api_calls"], 0)
        self.assertEqual(outcomes, ["consumed", "refunded"])
        self.assertEqual(work, [])

    def test_wall_budget_denies_at_deadline_and_stops_inflight_command(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            direct = project / "direct"
            self.local_sdlc.initialize_budget(
                direct,
                self.limits(max_wall_seconds=10.0),
                scope_kind="goal_stage",
                now=100.0,
            )
            self.local_sdlc.consume_action_budget(
                direct,
                "before_deadline",
                action_type="orchestration",
                action_id="A1",
                now=109.0,
            )
            with self.assertRaises(self.local_sdlc.BudgetExceeded):
                self.local_sdlc.consume_action_budget(
                    direct,
                    "at_deadline",
                    action_type="orchestration",
                    action_id="A2",
                    now=110.0,
                )

            command_run = project / "command"
            self.local_sdlc.initialize_budget(
                command_run,
                self.limits(max_wall_seconds=0.1),
                scope_kind="goal_stage",
            )

            def timeout_command(*_args, **_kwargs):
                time.sleep(0.12)
                raise subprocess.TimeoutExpired("slow", 0.1)

            with mock.patch("local_sdlc.verification.subprocess.run", side_effect=timeout_command):
                with self.assertRaises(self.local_sdlc.BudgetExceeded):
                    self.local_sdlc.run_checked_command(
                        project,
                        "python3 slow.py",
                        30.0,
                        command_run,
                    )
            command_status = self.local_sdlc.budget_status(command_run)
            command_events = self.local_sdlc.read_budget_events(command_run)

        self.assertEqual(command_status["stop"]["dimension"], "wall_seconds")
        self.assertEqual(command_events[-1]["outcome"], "exhausted_during_action")

    def test_parent_child_budget_is_atomic_and_propagates_stop(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            parent = root / "parent"
            child = parent / "child"
            self.local_sdlc.initialize_budget(parent, self.limits(), scope_kind="goal")
            self.local_sdlc.initialize_budget(
                child,
                self.limits(max_stage_actions=1),
                scope_kind="stage",
            )
            self.local_sdlc.begin_action(
                child,
                "child_first",
                action_type="api_call",
                risk_class="read_only",
                control_dirs=(parent,),
            )
            with self.assertRaises(self.local_sdlc.BudgetExceeded):
                self.local_sdlc.begin_action(
                    child,
                    "child_second",
                    action_type="api_call",
                    risk_class="read_only",
                    control_dirs=(parent,),
                )

            parent_status = self.local_sdlc.budget_status(parent)
            child_status = self.local_sdlc.budget_status(child)

        self.assertEqual(parent_status["state"], "exhausted")
        self.assertEqual(child_status["state"], "exhausted")
        self.assertEqual(parent_status["usage"]["goal_actions"], 1)
        self.assertEqual(child_status["usage"]["stage_actions"], 1)
        self.assertEqual(parent_status["stop"]["dimension"], "stage_actions")

    def test_concurrent_starts_cannot_overrun_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            self.local_sdlc.initialize_budget(
                run_dir,
                self.limits(max_goal_actions=5),
                scope_kind="goal_stage",
            )
            barrier = threading.Barrier(20)
            outcomes = []

            def start(index):
                barrier.wait()
                try:
                    self.local_sdlc.begin_action(
                        run_dir,
                        f"action_{index}",
                        action_type="orchestration",
                        risk_class="read_only",
                    )
                    outcomes.append("started")
                except self.local_sdlc.BudgetExceeded:
                    outcomes.append("stopped")

            threads = [threading.Thread(target=start, args=(index,)) for index in range(20)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            status = self.local_sdlc.budget_status(run_dir)
            starts = [item for item in self.local_sdlc.read_progress_events(run_dir) if item.get("starts_work")]

        self.assertEqual(outcomes.count("started"), 5)
        self.assertEqual(outcomes.count("stopped"), 15)
        self.assertEqual(status["usage"]["goal_actions"], 5)
        self.assertEqual(len(starts), 5)

    def test_resume_cannot_raise_or_reset_persisted_limits(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            original = self.local_sdlc.initialize_budget(
                run_dir,
                self.limits(max_goal_actions=1),
                scope_kind="goal_stage",
            )
            replaced = self.local_sdlc.initialize_budget(
                run_dir,
                self.limits(max_goal_actions=999),
                scope_kind="goal_stage",
            )
            self.local_sdlc.begin_action(
                run_dir,
                "first",
                action_type="orchestration",
                risk_class="read_only",
            )
            with self.assertRaises(self.local_sdlc.BudgetExceeded):
                self.local_sdlc.begin_action(
                    run_dir,
                    "resume",
                    action_type="resume",
                    risk_class="read_only",
                )
            stopped = self.local_sdlc.initialize_budget(
                run_dir,
                self.limits(max_goal_actions=999),
                scope_kind="goal_stage",
            )

        self.assertEqual(original["limits"]["max_goal_actions"], 1)
        self.assertEqual(replaced["limits"]["max_goal_actions"], 1)
        self.assertEqual(stopped["limits"]["max_goal_actions"], 1)

    def test_budget_audit_detects_work_that_bypassed_budget_gate(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            self.local_sdlc.initialize_budget(run_dir, self.limits(), scope_kind="goal_stage")
            self.local_sdlc.record_work_start(run_dir, "legacy_bypass")

            audit = self.local_sdlc.action_gate_audit(run_dir)

        self.assertEqual(audit["status"], "fail")
        self.assertFalse(audit["budget_precedes_work"])

    def test_cli_configures_and_reports_budget(self):
        args = self.local_sdlc.build_parser().parse_args(
            [
                "agent",
                "task",
                "--allow-no-context",
                "--max-goal-actions",
                "12",
                "--max-stage-actions",
                "7",
                "--max-recovery-actions",
                "3",
                "--max-api-calls",
                "4",
                "--max-wall-seconds",
                "90",
            ]
        )
        self.assertEqual(args.max_goal_actions, 12)
        self.assertEqual(args.max_stage_actions, 7)
        self.assertEqual(args.max_recovery_actions, 3)
        self.assertEqual(args.max_api_calls, 4)
        self.assertEqual(args.max_wall_seconds, 90.0)

        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            self.local_sdlc.initialize_budget(run_dir, self.limits(), scope_kind="goal_stage")
            status_args = self.local_sdlc.build_parser().parse_args(
                ["budget-status", "--run-dir", str(run_dir)]
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = status_args.func(status_args)
            payload = json.loads(output.getvalue())

        self.assertEqual(result, 0)
        self.assertEqual(payload["state"], "active")
        self.assertIn("remaining", payload)

    def test_agent_stops_before_api_call_beyond_budget(self):
        calls = []

        class FakeClient:
            def __init__(self, _config):
                self.timeout_limit = None

            def set_runtime_timeout_limit(self, timeout, callback=None):
                self.timeout_limit = timeout

            def complete(self, _messages, **_kwargs):
                calls.append(_kwargs)
                return "BEGIN_FILE: app.py\nprint('candidate')\nEND_FILE"

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root)
            (project / "app.py").write_text("print('original')\n", encoding="utf-8")
            run_dir = project / "run"
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
                    "--skip-pm",
                    "--max-rounds",
                    "1",
                    "--max-api-calls",
                    "1",
                    "--run-dir",
                    str(run_dir),
                ]
            )
            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                with self.assertRaises(self.local_sdlc.BudgetExceeded):
                    self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client
            status = self.local_sdlc.budget_status(run_dir)
            partial = json.loads((run_dir / "run.partial.json").read_text(encoding="utf-8"))

        self.assertEqual(len(calls), 1)
        self.assertEqual(status["stop"]["dimension"], "api_calls")
        self.assertEqual(partial["status"], "budget_exhausted")
        self.assertEqual(partial["final_verdict"], "budget_exhausted")

    def test_run_stages_promotes_child_budget_stop(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root, "# Custom project\n")
            run_dir = project / "run"
            args = self.local_sdlc.build_parser().parse_args(
                [
                    "run-stages",
                    "build custom project",
                    "--project",
                    str(project),
                    "--skills-dir",
                    str(skills_dir),
                    "--run-dir",
                    str(run_dir),
                    "--max-stage-actions",
                    "1",
                ]
            )

            def exhaust_child(child_args):
                child_dir = child_args.run_dir
                controls = tuple(Path(item) for item in child_args.control_dir)
                self.local_sdlc.initialize_budget(
                    child_dir,
                    self.local_sdlc.budget_limits_from_args(child_args),
                    scope_kind="stage",
                )
                self.local_sdlc.begin_action(
                    child_dir,
                    "first",
                    action_type="orchestration",
                    risk_class="read_only",
                    control_dirs=controls,
                )
                self.local_sdlc.begin_action(
                    child_dir,
                    "second",
                    action_type="orchestration",
                    risk_class="read_only",
                    control_dirs=controls,
                )
                return 0

            with mock.patch("local_sdlc.stage_runner.command_agent", side_effect=exhaust_child):
                result = self.local_sdlc.command_run_stages(args)
            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(result, 1)
        self.assertEqual(manifest["status"], "budget_exhausted")
        self.assertEqual(manifest["budget"]["state"], "exhausted")
        self.assertEqual(manifest["child_budget_stops"][0]["dimension"], "stage_actions")

    def test_web_forwards_and_summarizes_budget(self):
        from local_sdlc.web_jobs import WebConfig, build_cli_command, summarize_job_result

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            config = WebConfig(
                host="127.0.0.1",
                port=0,
                project=project,
                entrypoint=ENTRYPOINT_PATH,
            )
            built = build_cli_command(
                {
                    "mode": "agent",
                    "brief": "fix app",
                    "include": ["app.py"],
                    "max_goal_actions": 9,
                    "max_stage_actions": 8,
                    "max_recovery_actions": 7,
                    "max_api_calls": 6,
                    "max_wall_seconds": 5,
                },
                config,
            )
            self.assertEqual(built.argv[built.argv.index("--max-api-calls") + 1], "6")

            run_dir = project / ".sdlc-runner" / "runs" / "20260101-010101"
            log_dir = project / ".sdlc-runner" / "web" / "jobs" / "20260101-010101-aabbccdd"
            log_dir.mkdir(parents=True)
            self.local_sdlc.initialize_budget(
                run_dir,
                self.limits(max_goal_actions=1),
                scope_kind="goal_stage",
            )
            self.local_sdlc.begin_action(
                run_dir,
                "first",
                action_type="orchestration",
                risk_class="read_only",
            )
            with self.assertRaises(self.local_sdlc.BudgetExceeded):
                self.local_sdlc.begin_action(
                    run_dir,
                    "second",
                    action_type="orchestration",
                    risk_class="read_only",
                )
            result = summarize_job_result(
                {
                    "mode": "agent",
                    "brief": "fix app",
                    "status": "failed",
                    "command": built.display,
                },
                project,
                log_dir,
                [],
            )

        self.assertEqual(result["control_state"], "BUDGET_EXHAUSTED")
        self.assertEqual(result["budget"]["stop"]["dimension"], "goal_actions")
        self.assertIn("予算上限で停止", result["progress"])
        self.assertNotIn("analyze_failure", [item["type"] for item in result["next_actions"]])
