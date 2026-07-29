import json
import tempfile
from pathlib import Path

from tests.helpers import ROOT, LocalSDLCTestCase


class CancelControlTests(LocalSDLCTestCase):
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
