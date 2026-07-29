import tempfile
from pathlib import Path

from local_sdlc.harnesses.html_browser import HtmlBrowserHarness, run_html_smoke_evidence
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
