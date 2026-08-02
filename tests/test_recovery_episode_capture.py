import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from benchmarks.capture_recovery_episodes import capture_recovery_episodes
from learning_runtime.storage import ExperienceStore


class RecoveryEpisodeCaptureTests(unittest.TestCase):
    def test_direct_script_entrypoint_resolves_project_packages(self):
        script = Path(__file__).resolve().parents[1] / "benchmarks" / (
            "capture_recovery_episodes.py"
        )
        with tempfile.TemporaryDirectory() as temp:
            result = subprocess.run(
                [sys.executable, str(script), "--help"],
                cwd=temp,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--output", result.stdout)

    def test_capture_runs_two_distinct_failure_families_through_production_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "capture"
            report = capture_recovery_episodes(output)
            episodes = report["episodes"]
            store = ExperienceStore(output / "learning-store")
            stored_events = store.events()
            report_path = output / "capture-report.json"
            self.assertTrue(report_path.is_file())
            persisted = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(report["status"], "pass")
        self.assertEqual(len(episodes), 2)
        self.assertEqual(
            {episode["strategy"] for episode in episodes},
            {"failure_analysis", "root_cause_recovery"},
        )
        self.assertEqual(len({episode["failure_family"] for episode in episodes}), 2)
        for episode in episodes:
            self.assertEqual(episode["source_test_status"], "FAIL")
            self.assertEqual(episode["recovery_test_status"], "PASS")
            self.assertEqual(
                episode["event_types"],
                ["recovery_planned", "recovery_started", "recovery_completed"],
            )
            self.assertTrue(episode["causal_chain_valid"])
            self.assertTrue(episode["completion_verified"])
            self.assertEqual(episode["audit_status"], "pass")
            self.assertEqual(episode["outbox"]["pending"], 0)
            self.assertGreaterEqual(episode["outbox"]["delivered"], 4)
            self.assertTrue(episode["atomic_change"])
            self.assertEqual(episode["change_isolation"], "isolated")
        recovery_types = {
            str(event.get("event_type"))
            for event in stored_events
            if str(event.get("event_type", "")).startswith("recovery_")
        }
        self.assertEqual(
            recovery_types,
            {"recovery_planned", "recovery_started", "recovery_completed"},
        )
        self.assertEqual(persisted["status"], "pass")

    def test_capture_refuses_to_mix_with_an_existing_output_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "capture"
            output.mkdir()
            unrelated = output / "unrelated.txt"
            unrelated.write_text("keep", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                capture_recovery_episodes(output)
