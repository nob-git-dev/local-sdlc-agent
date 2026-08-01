import copy
import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.capture_recovery_episodes import capture_recovery_episodes
from learning_runtime.episodes import (
    build_and_store_recovery_episodes,
    build_recovery_episode_documents,
)
from learning_runtime.cli import main as learning_main
from learning_runtime.storage import ExperienceStore


def recovery_fixture(
    prefix: str,
    *,
    path: str,
    test_name: str,
    project_fingerprint: str,
) -> list[dict[str, object]]:
    aggregate_id = f"{prefix}-recovery"
    failure_family = (
        "python3 -B -m unittest discover -s tests"
        f"|test_assertion_failed|package.BehaviorTests.{test_name}.{test_name}"
        "|FAILED (failures=1)"
    )
    common = {
        "schema_version": 1,
        "run_id": f"{prefix}-run",
        "project_fingerprint": project_fingerprint,
        "aggregate_type": "recovery",
        "aggregate_id": aggregate_id,
        "occurred_at": "2026-08-01T00:00:00Z",
        "evidence_refs": [],
    }
    return [
        {
            **common,
            "event_id": f"{prefix}-planned",
            "event_hash": "1" * 64,
            "sequence": 1,
            "event_type": "recovery_planned",
            "causation_id": f"{prefix}-stalled",
            "payload": {
                "owner_email": "private@example.com",
                "recovery_plan": {
                    "strategy": "failure_analysis",
                    "failure_family": failure_family,
                    "failure_family_count": 2,
                    "plateau_detected": True,
                    "analysis_available": False,
                },
            },
        },
        {
            **common,
            "event_id": f"{prefix}-started",
            "event_hash": "2" * 64,
            "sequence": 2,
            "event_type": "recovery_started",
            "causation_id": f"{prefix}-planned",
            "payload": {"strategy": "failure_analysis"},
        },
        {
            **common,
            "event_id": f"{prefix}-completed",
            "event_hash": "3" * 64,
            "sequence": 3,
            "event_type": "recovery_completed",
            "causation_id": f"{prefix}-started",
            "payload": {
                "strategy": "failure_analysis",
                "outcome": "completed",
                "target_final_verdict": "approved",
                "verification_passed": True,
                "changed_paths": [path],
                "atomic_change": True,
                "change_isolation": "isolated",
                "concurrent_changed_paths": [],
            },
        },
    ]


class LearningEpisodeTests(unittest.TestCase):
    def captured_store(self, root: Path) -> ExperienceStore:
        output = root / "capture"
        capture_recovery_episodes(output)
        return ExperienceStore(output / "learning-store")

    def test_verified_isolated_real_recoveries_are_persisted_idempotently(self):
        with tempfile.TemporaryDirectory() as temp:
            store = self.captured_store(Path(temp))
            first = build_and_store_recovery_episodes(store)
            second = build_and_store_recovery_episodes(store)
            episodes = store.episodes()

        self.assertEqual(first["episode_count"], 2)
        self.assertEqual(first["inserted_count"], 2)
        self.assertEqual(second["inserted_count"], 0)
        self.assertEqual(second["duplicate_count"], 2)
        self.assertEqual(len(episodes), 2)
        self.assertEqual({item["eligibility"] for item in episodes}, {"eligible"})
        self.assertEqual(
            {item["intervention"]["change_isolation"] for item in episodes},
            {"isolated"},
        )
        for episode in episodes:
            self.assertEqual(episode["reason_codes"], [])
            self.assertEqual(episode["change"]["count"], 1)
            self.assertTrue(episode["change"]["atomic"])
            self.assertTrue(episode["outcome"]["verified"])
            self.assertEqual(
                [node["role"] for node in episode["causal_graph"]["nodes"]],
                ["decision", "intervention", "outcome"],
            )
            self.assertEqual(
                episode["causal_graph"]["edges"],
                [
                    {"from": "n1", "to": "n2", "kind": "causes"},
                    {"from": "n2", "to": "n3", "kind": "causes"},
                ],
            )
        serialized = json.dumps(episodes, ensure_ascii=False)
        self.assertNotIn("/home/", serialized)
        self.assertNotIn("@", serialized)
        self.assertNotIn("calculator.py", serialized)
        self.assertNotIn("normalizer.py", serialized)

    def test_build_episodes_cli_runs_as_an_independent_learning_process(self):
        with tempfile.TemporaryDirectory() as temp:
            store = self.captured_store(Path(temp))

            result = learning_main(
                ["build-episodes", "--data-dir", str(store.data_dir)]
            )
            episodes = store.episodes()

        self.assertEqual(result, 0)
        self.assertEqual(len(episodes), 2)
        self.assertEqual({item["eligibility"] for item in episodes}, {"eligible"})

    def test_unisolated_multi_file_change_is_retained_only_as_a_case(self):
        with tempfile.TemporaryDirectory() as temp:
            store = self.captured_store(Path(temp))
            events = copy.deepcopy(store.events())
            case_store = ExperienceStore(Path(temp) / "case-store")

            completed = next(
                event for event in events if event.get("event_type") == "recovery_completed"
            )
            payload = completed["payload"]
            payload["changed_paths"] = ["src/first.py", "src/second.py"]
            payload["atomic_change"] = False
            payload["change_isolation"] = "unisolated"
            payload["concurrent_changed_paths"] = ["src/second.py"]
            episodes = build_recovery_episode_documents(events)
            case = next(
                item
                for item in episodes
                if item["intervention"]["strategy"] == "failure_analysis"
            )
            inserted = case_store.put_episode(case)
            persisted_case = case_store.episodes()[0]

        self.assertTrue(inserted)
        self.assertEqual(case["eligibility"], "case_only")
        self.assertEqual(persisted_case["eligibility"], "case_only")
        self.assertEqual(
            set(case["reason_codes"]),
            {"concurrent_changes", "non_atomic_change", "unisolated_change"},
        )
        self.assertEqual(case["change"]["count"], 2)

    def test_broken_causation_is_retained_but_never_eligible(self):
        events = recovery_fixture(
            "broken",
            path="/home/private/project/parser.py",
            test_name="test_parse",
            project_fingerprint="project-a",
        )
        events[1]["causation_id"] = "unrelated-event"

        episode = build_recovery_episode_documents(events)[0]

        self.assertEqual(episode["eligibility"], "case_only")
        self.assertIn("broken_causal_chain", episode["reason_codes"])
        self.assertFalse(episode["causal_graph"]["complete"])

    def test_renamed_structural_cases_normalize_to_the_same_signature(self):
        first = recovery_fixture(
            "alpha",
            path="/home/alice/project/parser.py",
            test_name="test_parse",
            project_fingerprint="project-a",
        )
        second = recovery_fixture(
            "beta",
            path="/Users/bob/code/reader.py",
            test_name="test_read",
            project_fingerprint="project-b",
        )

        first_episode = build_recovery_episode_documents(first)[0]
        second_episode = build_recovery_episode_documents(second)[0]

        self.assertEqual(
            first_episode["structural_signature"],
            second_episode["structural_signature"],
        )
        self.assertEqual(first_episode["context"], second_episode["context"])
        self.assertEqual(first_episode["change"], second_episode["change"])
        serialized = json.dumps([first_episode, second_episode], ensure_ascii=False)
        self.assertNotIn("/home/alice", serialized)
        self.assertNotIn("/Users/bob", serialized)
        self.assertNotIn("private@example.com", serialized)


if __name__ == "__main__":
    unittest.main()
