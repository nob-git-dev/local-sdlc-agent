import sys
import tempfile
from pathlib import Path

from local_sdlc.harnesses.html_browser import HtmlBrowserHarness, run_html_smoke_evidence
from local_sdlc.harnesses.python_cli import PythonCliHarness, run_command_evidence
from local_sdlc.harnesses.python_probes import PythonProbeHarness
from tests.helpers import LocalSDLCTestCase


class HarnessPluginTests(LocalSDLCTestCase):
    def test_html_browser_harness_returns_evidence_without_approval_verdict(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            run_dir = project / "run"
            run_dir.mkdir()
            (project / "broken.html").write_text(
                "<!doctype html><html><body><script>",
                encoding="utf-8",
            )

            evidence = HtmlBrowserHarness().run_html_smoke(
                project,
                ["broken.html"],
                run_dir,
                timeout=1.0,
            )

        self.assertEqual(len(evidence), 1)
        item = evidence[0]
        self.assertEqual(item.kind, "html_smoke")
        self.assertEqual(item.status, "fail")
        self.assertFalse(item.ok)
        self.assertEqual(item.command, "html-smoke broken.html")
        self.assertIn("inline script tag is not closed", item.document)
        self.assertNotIn("approved", item.__dict__)
        self.assertNotIn("final_verdict", item.__dict__)

    def test_html_smoke_evidence_can_project_to_legacy_result(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            run_dir = project / "run"
            run_dir.mkdir()
            (project / "index.html").write_text(
                "<!doctype html><html><body><script>const ok = true;</script></body></html>",
                encoding="utf-8",
            )

            evidence = run_html_smoke_evidence(project, ["index.html"], run_dir, timeout=1.0)

        self.assertEqual(len(evidence), 1)
        document, ok = evidence[0].to_legacy_result()
        self.assertTrue(ok)
        self.assertIn("html-smoke index.html", document)

    def test_python_cli_harness_returns_evidence_for_passing_command(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            run_dir = project / "run"
            run_dir.mkdir()

            evidence = PythonCliHarness().run_command(
                project,
                f"{sys.executable} -c \"print('ok')\"",
                run_dir,
                timeout=5.0,
            )

        self.assertEqual(evidence.kind, "command")
        self.assertEqual(evidence.status, "pass")
        self.assertTrue(evidence.ok)
        self.assertEqual(evidence.exit_code, 0)
        self.assertIn("status: PASS", evidence.document)
        self.assertNotIn("approved", evidence.__dict__)
        self.assertNotIn("final_verdict", evidence.__dict__)

    def test_python_cli_harness_records_failure_type(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            run_dir = project / "run"
            run_dir.mkdir()

            evidence = run_command_evidence(
                project,
                f"{sys.executable} -c \"import sys; print('bad', file=sys.stderr); sys.exit(7)\"",
                run_dir,
                timeout=5.0,
            )

        self.assertEqual(evidence.status, "fail")
        self.assertFalse(evidence.ok)
        self.assertEqual(evidence.exit_code, 7)
        self.assertEqual(evidence.failure_type, "command_failed")
        self.assertIn("bad", evidence.document)

    def test_python_cli_harness_preserves_safety_decisions(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            run_dir = project / "run"
            run_dir.mkdir()

            evidence = PythonCliHarness().run_command(
                project,
                "sudo echo should-not-run",
                run_dir,
                timeout=5.0,
            )
            decisions = self.local_sdlc.read_safety_decisions(run_dir)

        self.assertEqual(evidence.status, "fail")
        self.assertEqual(evidence.failure_type, "blocked_command")
        self.assertIn("status: BLOCKED", evidence.document)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["decision"], "require_approval")

    def test_python_cli_harness_evidence_can_project_to_legacy_result(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            run_dir = project / "run"
            run_dir.mkdir()

            evidence = PythonCliHarness().run_command(
                project,
                f"{sys.executable} -c \"print('legacy')\"",
                run_dir,
                timeout=5.0,
            )

        document, ok = evidence.to_legacy_result()
        self.assertTrue(ok)
        self.assertIn("legacy", document)

    def test_python_probe_harness_returns_mechanical_probe_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            target = project / "storage.py"
            target.write_text(
                "import struct\nHEADER_FORMAT = 'II'\nHEADER_SIZE = 16\nstruct.pack('II', 1, 2)\n",
                encoding="utf-8",
            )
            doc = self.local_sdlc.command_result_document(
                "python3 -m unittest",
                1,
                "",
                f'File "{target}", line 2, in <module>\nheader size mismatch\n',
                0.01,
            )

            evidence = PythonProbeHarness().run(project, [("Command result", doc)])

        self.assertEqual(len(evidence), 1)
        item = evidence[0]
        self.assertEqual(item.kind, "mechanical_probe")
        self.assertEqual(item.status, "pass")
        self.assertEqual(item.command, "mechanical-probe struct")
        self.assertIn("python_struct", item.covers)
        self.assertIn("calcsize=8", item.document)
        self.assertNotIn("approved", item.__dict__)
        self.assertNotIn("final_verdict", item.__dict__)
