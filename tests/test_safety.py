import sys
import tempfile
from pathlib import Path

from tests.helpers import LocalSDLCTestCase


class SafetyTests(LocalSDLCTestCase):
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
