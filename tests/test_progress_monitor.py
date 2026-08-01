import contextlib
import io
import json
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

from tests.helpers import LocalSDLCTestCase
from tests.helpers import ENTRYPOINT_PATH


class ProgressMonitorTests(LocalSDLCTestCase):
    def policy(self, seconds: float = 10.0):
        return self.local_sdlc.ProgressPolicy(max_idle_seconds=seconds)

    def test_unchanged_vector_becomes_persistently_stalled_at_threshold(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            self.local_sdlc.initialize_progress_monitor(
                run_dir,
                self.policy(),
                scope_kind="goal_stage",
                now=100.0,
            )
            first = self.local_sdlc.observe_progress(
                run_dir,
                {"stage_id": "S03", "current_function": "generate_artifact", "stream_chunks": 1},
                source="stream",
                now=105.0,
            )
            unchanged = self.local_sdlc.observe_progress(
                run_dir,
                {"stage_id": "S03", "current_function": "generate_artifact", "stream_chunks": 1},
                source="stream",
                now=114.999,
            )
            with self.assertRaises(self.local_sdlc.ProgressStalled):
                self.local_sdlc.observe_progress(
                    run_dir,
                    {"stage_id": "S03", "current_function": "generate_artifact", "stream_chunks": 1},
                    source="stream",
                    now=115.0,
                )
            status = self.local_sdlc.progress_status(run_dir, now=116.0)
            events = self.local_sdlc.read_progress_events(run_dir)

        self.assertTrue(first["changed"])
        self.assertFalse(unchanged["changed"])
        self.assertEqual(status["state"], "stalled")
        self.assertEqual(status["runtime_state"], "STALLED")
        self.assertEqual(status["stall"]["last_progress_vector"]["stream_chunks"], 1)
        self.assertEqual([item["event"] for item in events], ["progress_observed", "stalled"])

    def test_meaningful_stream_change_resets_idle_clock_but_duration_does_not(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            self.local_sdlc.initialize_progress_monitor(
                run_dir,
                self.policy(),
                scope_kind="goal_stage",
                now=100.0,
            )
            self.local_sdlc.observe_progress(
                run_dir,
                {"stream_chunks": 10, "stream_bytes": 100, "duration_seconds": 1.0},
                source="stream",
                now=101.0,
            )
            same = self.local_sdlc.observe_progress(
                run_dir,
                {"stream_chunks": 10, "stream_bytes": 100, "duration_seconds": 8.0},
                source="stream",
                now=108.0,
            )
            changed = self.local_sdlc.observe_progress(
                run_dir,
                {"stream_chunks": 11, "stream_bytes": 112, "duration_seconds": 9.0},
                source="stream",
                now=110.0,
            )
            before = self.local_sdlc.progress_status(run_dir, now=119.999)
            after = self.local_sdlc.progress_status(run_dir, now=120.0)

        self.assertFalse(same["changed"])
        self.assertTrue(changed["changed"])
        self.assertEqual(before["state"], "active")
        self.assertEqual(after["state"], "stalled")
        self.assertNotIn("duration_seconds", before["vector"])

    def test_stalled_is_absorbing_and_action_budget_is_refunded_on_race(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            self.local_sdlc.initialize_progress_monitor(
                run_dir,
                self.policy(),
                scope_kind="goal_stage",
            )
            self.local_sdlc.initialize_budget(
                run_dir,
                self.local_sdlc.BudgetLimits(),
                scope_kind="goal_stage",
            )
            from local_sdlc import action_gate

            original_start = action_gate.start_progress_action

            def stall_then_start(*args, **kwargs):
                state = self.local_sdlc.read_progress_state(run_dir)
                deadline = float(state["last_progress_at_epoch"]) + 10.0
                with self.assertRaises(self.local_sdlc.ProgressStalled):
                    self.local_sdlc.enforce_progress_deadline(
                        run_dir,
                        "race_watchdog",
                        now=deadline,
                    )
                return original_start(*args, **kwargs)

            with mock.patch(
                "local_sdlc.action_gate.start_progress_action",
                side_effect=stall_then_start,
            ):
                with self.assertRaises(self.local_sdlc.ProgressStalled):
                    self.local_sdlc.begin_action(
                        run_dir,
                        "coder_api_call",
                        action_type="api_call",
                        risk_class="read_only",
                    )
            with self.assertRaises(self.local_sdlc.ProgressStalled):
                self.local_sdlc.begin_action(
                    run_dir,
                    "later_action",
                    action_type="orchestration",
                    risk_class="read_only",
                )
            budget = self.local_sdlc.budget_status(run_dir)
            work = [
                item
                for item in self.local_sdlc.read_progress_events(run_dir)
                if item.get("starts_work")
            ]

        self.assertEqual(budget["usage"]["goal_actions"], 0)
        self.assertEqual(budget["usage"]["api_calls"], 0)
        self.assertEqual(work, [])

    def test_stall_watchdog_and_action_start_have_a_serialized_order(self):
        for _index in range(12):
            with tempfile.TemporaryDirectory() as temp:
                run_dir = Path(temp) / "run"
                self.local_sdlc.initialize_progress_monitor(
                    run_dir,
                    self.policy(1.0),
                    scope_kind="goal_stage",
                )
                state = self.local_sdlc.read_progress_state(run_dir)
                deadline = float(state["last_progress_at_epoch"]) + 1.0
                barrier = threading.Barrier(2)
                outcomes = []

                def start_action():
                    barrier.wait()
                    try:
                        self.local_sdlc.begin_action(
                            run_dir,
                            "racing_action",
                            action_type="orchestration",
                            risk_class="read_only",
                        )
                        outcomes.append("work")
                    except self.local_sdlc.ProgressStalled:
                        outcomes.append("stalled_action")

                def run_watchdog():
                    barrier.wait()
                    try:
                        self.local_sdlc.enforce_progress_deadline(
                            run_dir,
                            "race_watchdog",
                            now=deadline,
                        )
                        outcomes.append("watchdog_no_stall")
                    except self.local_sdlc.ProgressStalled:
                        outcomes.append("watchdog_stalled")

                threads = [
                    threading.Thread(target=start_action),
                    threading.Thread(target=run_watchdog),
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

                audit = self.local_sdlc.action_gate_audit(run_dir)
                stall = self.local_sdlc.read_stall_state(run_dir)
                work = [
                    event
                    for event in self.local_sdlc.read_progress_events(run_dir)
                    if event.get("starts_work")
                ]

            self.assertTrue(audit["stall_absorbing"], outcomes)
            if stall:
                self.assertLessEqual(len(work), 1)
                if work:
                    self.assertLess(
                        int(work[0]["sequence"]),
                        int(stall["progress_sequence"]),
                    )
            else:
                self.assertIn("work", outcomes)
                self.assertEqual(len(work), 1)

    def test_child_stall_is_atomically_propagated_to_parent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            parent = root / "goal"
            child = parent / "s03"
            self.local_sdlc.initialize_progress_monitor(
                parent,
                self.policy(30.0),
                scope_kind="goal",
                now=100.0,
            )
            self.local_sdlc.initialize_progress_monitor(
                child,
                self.policy(10.0),
                scope_kind="stage",
                now=100.0,
            )
            with self.assertRaises(self.local_sdlc.ProgressStalled):
                self.local_sdlc.observe_progress(
                    child,
                    {"stage_id": "S03", "current_function": "generate_artifact"},
                    source="child",
                    control_dirs=(parent,),
                    now=110.0,
                )
            parent_status = self.local_sdlc.progress_status(parent, now=110.0)
            child_status = self.local_sdlc.progress_status(child, now=110.0)

        self.assertEqual(parent_status["state"], "stalled")
        self.assertEqual(child_status["state"], "stalled")
        self.assertEqual(
            parent_status["stall"]["scope_run_dir"],
            str(child.resolve()),
        )

    def test_action_gate_audit_detects_work_started_after_stall(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            self.local_sdlc.initialize_progress_monitor(
                run_dir,
                self.policy(),
                scope_kind="goal_stage",
                now=100.0,
            )
            with self.assertRaises(self.local_sdlc.ProgressStalled):
                self.local_sdlc.enforce_progress_deadline(
                    run_dir,
                    "watchdog",
                    now=110.0,
                )
            self.local_sdlc.record_work_start(run_dir, "legacy_bypass")
            audit = self.local_sdlc.action_gate_audit(run_dir)

        self.assertEqual(audit["status"], "fail")
        self.assertFalse(audit["stall_absorbing"])
        self.assertEqual(audit["stall_violations"][0]["action"], "legacy_bypass")

    def test_progress_policy_is_immutable_across_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            original = self.local_sdlc.initialize_progress_monitor(
                run_dir,
                self.policy(30.0),
                scope_kind="goal_stage",
                now=100.0,
            )
            repeated = self.local_sdlc.initialize_progress_monitor(
                run_dir,
                self.policy(30.0),
                scope_kind="goal_stage",
                now=200.0,
            )
            with self.assertRaises(self.local_sdlc.RunnerError):
                self.local_sdlc.initialize_progress_monitor(
                    run_dir,
                    self.policy(60.0),
                    scope_kind="goal_stage",
                )

        self.assertEqual(original, repeated)
        self.assertEqual(original["initialized_at_epoch"], 100.0)

    def test_invalid_stall_file_fails_closed_and_non_finite_policy_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            self.local_sdlc.initialize_progress_monitor(
                run_dir,
                self.policy(),
                scope_kind="goal_stage",
            )
            self.local_sdlc.stall_file_path(run_dir).write_text("{broken", encoding="utf-8")
            with self.assertRaises(self.local_sdlc.ProgressStalled):
                self.local_sdlc.begin_action(
                    run_dir,
                    "must_not_start",
                    action_type="orchestration",
                    risk_class="read_only",
                )
            work = [
                item
                for item in self.local_sdlc.read_progress_events(run_dir)
                if item.get("starts_work")
            ]
            with self.assertRaises(self.local_sdlc.RunnerError):
                self.local_sdlc.initialize_progress_monitor(
                    Path(temp) / "nan",
                    self.policy(float("nan")),
                    scope_kind="goal_stage",
                )

        self.assertEqual(work, [])

    def test_progress_cli_exposes_configuration_and_status(self):
        parser = self.local_sdlc.build_parser()
        args = parser.parse_args(
            [
                "agent",
                "repair",
                "--allow-no-context",
                "--max-idle-seconds",
                "42",
            ]
        )
        self.assertEqual(args.max_idle_seconds, 42.0)

        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            self.local_sdlc.initialize_progress_monitor(
                run_dir,
                self.policy(42.0),
                scope_kind="goal_stage",
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = self.local_sdlc.main(
                    ["progress-status", "--run-dir", str(run_dir)]
                )
            payload = json.loads(output.getvalue())

        self.assertEqual(code, 0)
        self.assertEqual(payload["state"], "active")
        self.assertEqual(payload["policy"]["max_idle_seconds"], 42.0)

    def test_new_action_changes_function_vector_and_refreshes_progress(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            self.local_sdlc.initialize_progress_monitor(
                run_dir,
                self.policy(30.0),
                scope_kind="goal_stage",
            )
            before = time.time()
            self.local_sdlc.begin_action(
                run_dir,
                "pm_api_call",
                action_type="api_call",
                risk_class="read_only",
                metadata={"stage_id": "S01", "round": 1},
            )
            status = self.local_sdlc.progress_status(run_dir, evaluate=False)
            state = self.local_sdlc.read_progress_state(run_dir)

        self.assertEqual(status["vector"]["current_function"], "pm_api_call")
        self.assertEqual(status["vector"]["stage_id"], "S01")
        self.assertGreaterEqual(
            state["last_progress_at_epoch"],
            before,
        )

    def test_inflight_quiet_command_is_stopped_by_progress_deadline(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            run_dir = project / "run"
            self.local_sdlc.initialize_progress_monitor(
                run_dir,
                self.policy(0.05),
                scope_kind="goal_stage",
            )
            with self.assertRaises(self.local_sdlc.ProgressStalled):
                self.local_sdlc.run_checked_command(
                    project,
                    r"python3 -c import\ time;time.sleep(1)",
                    2.0,
                    run_dir,
                    action="quiet_test",
                )
            status = self.local_sdlc.progress_status(run_dir, evaluate=False)

        self.assertEqual(status["state"], "stalled")
        self.assertEqual(status["stall"]["action"], "quiet_test")

    def test_command_output_growth_keeps_long_command_live(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            run_dir = project / "run"
            script = project / "chatty.py"
            script.write_text(
                "import time\n"
                "for i in range(6):\n"
                "    print(i, flush=True)\n"
                "    time.sleep(0.04)\n",
                encoding="utf-8",
            )
            self.local_sdlc.initialize_progress_monitor(
                run_dir,
                self.policy(0.1),
                scope_kind="goal_stage",
            )
            document, ok = self.local_sdlc.run_checked_command(
                project,
                "python3 chatty.py",
                2.0,
                run_dir,
                action="chatty_test",
            )
            status = self.local_sdlc.progress_status(run_dir, evaluate=False)

        self.assertTrue(ok, document)
        self.assertEqual(status["state"], "active")
        self.assertGreater(status["vector"]["command_output_bytes"], 0)

    def test_agent_persists_stalled_manifest_when_api_makes_no_progress(self):
        calls = []

        class QuietClient:
            def __init__(self, _config):
                self.timeout_callback = None

            def set_runtime_timeout_limit(self, _timeout, callback=None):
                self.timeout_callback = callback

            def set_runtime_progress_callback(self, _callback):
                pass

            def complete(self, _messages, **kwargs):
                calls.append(kwargs)
                time.sleep(0.12)
                if self.timeout_callback:
                    self.timeout_callback()
                return "BEGIN_FILE: app.py\nprint('late')\nEND_FILE"

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
                    "--max-idle-seconds",
                    "0.1",
                    "--run-dir",
                    str(run_dir),
                ]
            )
            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = QuietClient
            try:
                with self.assertRaises(self.local_sdlc.ProgressStalled):
                    self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client
            partial = json.loads((run_dir / "run.partial.json").read_text(encoding="utf-8"))

        self.assertEqual(len(calls), 1)
        self.assertEqual(partial["status"], "stalled")
        self.assertEqual(partial["final_verdict"], "stalled")
        self.assertEqual(partial["progress"]["state"], "stalled")

    def test_run_stages_promotes_child_stall(self):
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
                    "--max-idle-seconds",
                    "10",
                ]
            )

            def stall_child(child_args):
                child_dir = child_args.run_dir
                controls = tuple(Path(item) for item in child_args.control_dir)
                self.local_sdlc.initialize_progress_monitor(
                    child_dir,
                    self.local_sdlc.progress_policy_from_args(child_args),
                    scope_kind="stage",
                    now=100.0,
                )
                self.local_sdlc.enforce_progress_deadline(
                    child_dir,
                    "child_watchdog",
                    control_dirs=controls,
                    now=110.0,
                )
                return 0

            with mock.patch(
                "local_sdlc.stage_runner.command_agent",
                side_effect=stall_child,
            ):
                result = self.local_sdlc._stage_runner.command_run_stages(args)
            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(result, 1)
        self.assertEqual(manifest["status"], "stalled")
        self.assertEqual(manifest["progress"]["state"], "stalled")
        self.assertEqual(manifest["child_stalls"][0]["action"], "child_watchdog")

    def test_web_forwards_and_summarizes_stall_state(self):
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
                    "max_idle_seconds": 12,
                },
                config,
            )
            self.assertEqual(
                built.argv[built.argv.index("--max-idle-seconds") + 1],
                "12.0",
            )
            run_dir = project / ".sdlc-runner" / "runs" / "20260101-010101"
            log_dir = project / ".sdlc-runner" / "web" / "jobs" / "20260101-010101-aabbccdd"
            log_dir.mkdir(parents=True)
            self.local_sdlc.initialize_progress_monitor(
                run_dir,
                self.policy(12.0),
                scope_kind="goal_stage",
                now=100.0,
            )
            with self.assertRaises(self.local_sdlc.ProgressStalled):
                self.local_sdlc.enforce_progress_deadline(
                    run_dir,
                    "watchdog",
                    now=112.0,
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

        self.assertEqual(result["control_state"], "STALLED")
        self.assertEqual(result["progress_monitor"]["state"], "stalled")
        self.assertIn("進捗が止まったため停止", result["progress"])
        self.assertNotIn(
            "analyze_failure",
            [item["type"] for item in result["next_actions"]],
        )

    def test_stream_client_emits_runtime_progress_without_runner_callback(self):
        config = self.local_sdlc.LLMConfig(
            base_url="http://localhost:30000/v1",
            api_key="dummy-local",
            model="test-model",
            timeout=10.0,
            health_timeout=0.2,
            temperature=0.0,
            max_tokens=64,
            disable_thinking=True,
            stream=True,
        )
        client = self.local_sdlc.LocalLLMClient(config)
        client._request = lambda *_args, **_kwargs: {"data": [{"id": "test-model"}]}
        observed = []
        client.set_runtime_progress_callback(observed.append)

        class FakeResponse:
            headers = {"content-type": "text/event-stream"}

            def __enter__(self):
                return self

            def __exit__(self, _exc_type, _exc, _tb):
                return False

            def __iter__(self):
                yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
                yield b"data: [DONE]\n\n"

        with mock.patch(
            "local_sdlc.llm_client.urllib.request.urlopen",
            return_value=FakeResponse(),
        ):
            result = client.complete(
                [{"role": "user", "content": "hello"}],
                agent_level="coder",
                call_function="generate_artifact",
            )

        self.assertEqual(result, "ok")
        self.assertGreaterEqual(len(observed), 1)
        self.assertEqual(observed[-1].bytes_received, 2)
