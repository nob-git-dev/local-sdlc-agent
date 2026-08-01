import json
from pathlib import Path

from tests.helpers import LocalSDLCTestCase


class DomainModelTests(LocalSDLCTestCase):
    def test_requirement_observable_evidence_and_verdict_types_serialize(self):
        requirement = self.local_sdlc.Requirement(
            requirement_id="A01",
            text="browser smoke passes",
            source_path="SPEC.md",
            source_section="Acceptance",
            required_observables=("browser_smoke",),
        )
        observable = self.local_sdlc.Observable(
            observable_id="O001",
            kind="coverage",
            target="browser_smoke",
            expected="pass",
            harness="html_browser",
            timeout=30.0,
        )
        evidence = self.local_sdlc.Evidence(
            evidence_id="E001",
            observable_id="O001",
            kind="browser",
            status="pass",
            covers=("browser_smoke",),
        )
        verdict = self.local_sdlc.Verdict(
            verdict_id="V001",
            requirement_id="A01",
            status="pass",
            evidence_ids=("E001",),
            required_covers=("browser_smoke",),
        )

        self.assertEqual(requirement.to_manifest()["id"], "A01")
        self.assertEqual(observable.to_manifest()["target"], "browser_smoke")
        self.assertEqual(evidence.to_manifest()["status"], "pass")
        self.assertEqual(verdict.status, "pass")

    def test_acceptance_matrix_distinguishes_all_blocking_verdicts(self):
        criteria = [{"id": "A01", "text": "browser smoke passes"}]
        for status in ("fail", "blocked", "invalid_evidence"):
            with self.subTest(status=status):
                matrix = self.local_sdlc.build_acceptance_matrix(
                    criteria,
                    [{"id": "E01", "status": status, "covers": ["browser_smoke"]}],
                )
                self.assertEqual(matrix[0]["status"], status)
                self.assertEqual(self.local_sdlc.acceptance_blockers(matrix), matrix)

        unverified = self.local_sdlc.build_acceptance_matrix(criteria, [])
        self.assertEqual(unverified[0]["status"], "unverified")
        self.assertEqual(self.local_sdlc.acceptance_blockers(unverified), unverified)

    def test_tetris_regression_memory_injects_only_matching_stage(self):
        fixture = Path(__file__).parent / "fixtures" / "regression_memory" / "tetris_active_piece.json"
        memory = self.local_sdlc.regression_memory_from_dict(
            json.loads(fixture.read_text(encoding="utf-8"))
        )
        self.assertIsNotNone(memory)
        tetris = self.local_sdlc.StageWorkItem(
            stage_id="S02",
            title="Core interaction logic",
            goal="Implement a playable Tetris game.",
            suggested_paths=("tetris.html",),
            test_focus=("browser smoke",),
        )
        unknown = self.local_sdlc.StageWorkItem(
            stage_id="S02",
            title="Core behavior",
            goal="Implement an unrelated parser.",
            suggested_paths=("src/parser.py",),
            test_focus=("unit test",),
        )

        updated = self.local_sdlc.apply_regression_memories_to_stages([tetris, unknown], [memory])

        self.assertIn("browser:active_piece_visible_after_start", updated[0].required_observables)
        self.assertEqual(updated[1].required_observables, ())

    def test_regression_memory_round_trip_has_stable_id(self):
        fixture = Path(__file__).parent / "fixtures" / "regression_memory" / "tetris_active_piece.json"
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        first = self.local_sdlc.regression_memory_from_dict(payload)
        second = self.local_sdlc.regression_memory_from_dict(first.to_manifest())

        self.assertEqual(first.memory_id, second.memory_id)
        self.assertEqual(second.regression_tests, ("browser-tetris-smoke",))
