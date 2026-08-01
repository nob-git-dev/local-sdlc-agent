import contextlib
import io
import json
import tempfile
import threading
from pathlib import Path

from tests.helpers import ROOT, LocalSDLCTestCase


class CancelControlTests(LocalSDLCTestCase):
    def test_cancel_cli_persists_absorbing_state(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            args = self.local_sdlc.build_parser().parse_args(
                ["cancel", "--run-dir", str(run_dir), "--reason", "operator_stop"]
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = args.func(args)

            payload = json.loads(output.getvalue())

        self.assertEqual(result, 0)
        self.assertEqual(payload["reason"], "operator_stop")
        self.assertEqual(payload["status"], "cancelled")

    def test_cancel_is_absorbing_for_every_autonomous_action_kind(self):
        actions = (
            ("api_call", "api_call", "read_only"),
            ("command", "command", "generated_code_execution"),
            ("resume", "resume", "read_only"),
            ("retry", "recovery", "read_only"),
            ("stage_split", "recovery", "read_only"),
            ("artifact_apply", "artifact_apply", "project_write"),
            ("copy_back", "copy_back", "project_write"),
        )
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            self.local_sdlc.request_cancel(run_dir, source="test", reason="absorbing")

            for action, action_type, risk_class in actions:
                with self.subTest(action=action):
                    with self.assertRaises(self.local_sdlc.RunnerError):
                        self.local_sdlc.begin_action(
                            run_dir,
                            action,
                            action_type=action_type,
                            risk_class=risk_class,
                        )

            self.assertEqual(self.local_sdlc.work_starts_after_cancel(run_dir), [])
            self.assertEqual(
                [event["event"] for event in self.local_sdlc.read_progress_events(run_dir)],
                ["cancel_requested"],
            )

    def test_parent_cancel_blocks_child_stage_action_and_mirrors_prior_work(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            parent = root / "parent"
            child = root / "child"
            self.local_sdlc.begin_action(
                child,
                "coder_api_call",
                action_type="api_call",
                risk_class="read_only",
                control_dirs=(parent,),
            )
            self.local_sdlc.request_cancel(parent, source="test", reason="stop_parent")

            with self.assertRaises(self.local_sdlc.RunnerError):
                self.local_sdlc.begin_action(
                    child,
                    "artifact_apply",
                    action_type="artifact_apply",
                    risk_class="project_write",
                    control_dirs=(parent,),
                )

            parent_events = self.local_sdlc.read_progress_events(parent)
            self.assertEqual([item["event"] for item in parent_events], ["work_start", "cancel_requested"])
            self.assertTrue(parent_events[0]["metadata"]["mirrored"])
            self.assertEqual(self.local_sdlc.work_starts_after_cancel(parent), [])
            self.assertEqual(self.local_sdlc.work_starts_after_cancel(child), [])

    def test_cancel_and_action_start_are_serialized(self):
        for index in range(20):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as temp:
                run_dir = Path(temp) / "run"
                barrier = threading.Barrier(2)
                outcomes = []

                def start_action():
                    barrier.wait()
                    try:
                        self.local_sdlc.begin_action(
                            run_dir,
                            "coder_api_call",
                            action_type="api_call",
                            risk_class="read_only",
                        )
                        outcomes.append("started")
                    except self.local_sdlc.RunnerError:
                        outcomes.append("cancelled")

                def cancel():
                    barrier.wait()
                    self.local_sdlc.request_cancel(run_dir, source="test", reason="race")

                threads = [threading.Thread(target=start_action), threading.Thread(target=cancel)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

                self.assertEqual(len(outcomes), 1)
                self.assertEqual(self.local_sdlc.work_starts_after_cancel(run_dir), [])

    def test_request_cancel_writes_cancel_json(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            run_dir.mkdir()

            state = self.local_sdlc.request_cancel(run_dir, source="test", reason="user_cancelled")

            cancel_path = run_dir / "cancel.json"
            persisted = json.loads(cancel_path.read_text(encoding="utf-8"))
            self.assertTrue(self.local_sdlc.cancel_requested(run_dir))
            self.assertEqual(state["status"], "cancelled")
            self.assertEqual(persisted["status"], "cancelled")
            self.assertEqual(persisted["source"], "test")
            self.assertEqual(persisted["reason"], "user_cancelled")
            self.assertIn("requested_at", persisted)

    def test_work_start_progress_is_blocked_after_cancel(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            run_dir.mkdir()

            first = self.local_sdlc.record_work_start(run_dir, "pm_api_call")
            cancel_state = self.local_sdlc.request_cancel(run_dir, source="test", reason="stop")

            with self.assertRaises(self.local_sdlc.RunnerError):
                self.local_sdlc.record_work_start(run_dir, "coder_api_call")

            events = self.local_sdlc.read_progress_events(run_dir)
            violating = self.local_sdlc.work_starts_after_cancel(run_dir)
            self.assertEqual(first["sequence"], 1)
            self.assertEqual(cancel_state["progress_sequence"], 1)
            self.assertEqual([event["event"] for event in events], ["work_start", "cancel_requested"])
            self.assertEqual(violating, [])

    def test_agent_refuses_cancelled_resume_before_llm_call(self):
        calls = []

        class FakeClient:
            def __init__(self, _config):
                pass

            def complete(self, _messages, **_kwargs):
                calls.append(_kwargs)
                return "BEGIN_FILE: app.py\nprint('changed')\nEND_FILE"

        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            project, skills_dir = self.make_agent_project(Path(temp))
            (project / "app.py").write_text("print('original')\n", encoding="utf-8")
            run_dir = project / "run"
            run_dir.mkdir()
            (run_dir / "run.json").write_text(
                json.dumps({"command": "agent", "documents": [], "api_calls": 0, "completed_rounds": 0}),
                encoding="utf-8",
            )
            self.local_sdlc.request_cancel(run_dir, source="test", reason="stop_before_resume")

            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "app.pyを修正して",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--include",
                        "app.py",
                        "--resume",
                        str(run_dir),
                        "--run-dir",
                        str(run_dir),
                        "--apply",
                    ]
                )
                with self.assertRaises(self.local_sdlc.RunnerError) as caught:
                    self.local_sdlc.command_agent(args)
                app_content = (project / "app.py").read_text(encoding="utf-8")
            finally:
                self.local_sdlc.LocalLLMClient = original_client

        self.assertIn("cancelled", str(caught.exception))
        self.assertEqual(calls, [])
        self.assertEqual(app_content, "print('original')\n")
        self.assertEqual(self.local_sdlc.work_starts_after_cancel(run_dir), [])

    def test_cancel_after_coder_output_prevents_artifact_apply(self):
        class FakeClient:
            def __init__(self, _config):
                pass

            def complete(self, _messages, **_kwargs):
                self_module.request_cancel(run_dir, source="test", reason="before_apply")
                return "BEGIN_FILE: app.py\nprint('changed')\nEND_FILE"

        self_module = self.local_sdlc
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            project, skills_dir = self.make_agent_project(Path(temp))
            (project / "app.py").write_text("print('original')\n", encoding="utf-8")
            run_dir = project / "run"
            original_client = self.local_sdlc.LocalLLMClient
            self.local_sdlc.LocalLLMClient = FakeClient
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "change app",
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
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                with self.assertRaises(self.local_sdlc.RunnerError):
                    self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client

            content = (project / "app.py").read_text(encoding="utf-8")

        self.assertEqual(content, "print('original')\n")
        self.assertEqual(self.local_sdlc.work_starts_after_cancel(run_dir), [])

    def test_cancel_after_isolated_test_prevents_copy_back(self):
        calls = []

        class FakeClient:
            def __init__(self, _config):
                pass

            def complete(self, _messages, **_kwargs):
                calls.append(True)
                return "BEGIN_FILE: app.py\nprint('changed')\nEND_FILE"

        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            project, skills_dir = self.make_agent_project(Path(temp))
            (project / "app.py").write_text("print('original')\n", encoding="utf-8")
            run_dir = project / "run"

            def passing_command(*_args, **_kwargs):
                self.local_sdlc.request_cancel(run_dir, source="test", reason="before_copy_back")
                return self.local_sdlc.command_result_document("test", 0, "", "", 0.0), True

            original_client = self.local_sdlc.LocalLLMClient
            original_command = self.local_sdlc._agent_runner.run_checked_command
            self.local_sdlc.LocalLLMClient = FakeClient
            self.local_sdlc._agent_runner.run_checked_command = passing_command
            try:
                args = self.local_sdlc.build_parser().parse_args(
                    [
                        "agent",
                        "change app",
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
                        "python3 -m py_compile app.py",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                with self.assertRaises(self.local_sdlc.RunnerError):
                    self.local_sdlc.command_agent(args)
            finally:
                self.local_sdlc.LocalLLMClient = original_client
                self.local_sdlc._agent_runner.run_checked_command = original_command

            content = (project / "app.py").read_text(encoding="utf-8")

        self.assertEqual(len(calls), 1)
        self.assertEqual(content, "print('original')\n")
        self.assertEqual(self.local_sdlc.work_starts_after_cancel(run_dir), [])
