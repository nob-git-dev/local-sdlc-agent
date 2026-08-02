import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from learning_runtime.candidate_store import CandidateStore
from learning_runtime.cli import main as learning_main
from learning_runtime.domain_map import ComponentObservation, DomainMap
from learning_runtime.evaluation_store import EvaluationStore
from learning_runtime.knowledge_schema import KnowledgeItem
from learning_runtime.storage import ExperienceStore
from learning_runtime.validation import validate_and_store
from learning_runtime.validation_models import ValidationCase, ValidationPolicy
from tests.learning_candidate_fixtures import eligible_episode
from tests.learning_knowledge_fixtures import evidence, knowledge_payload, renamed_domain_map


def structural_candidate() -> KnowledgeItem:
    payload = knowledge_payload(authority="llm_hypothesis")
    payload.update(
        {
            "supporting_projects": ["project-a", "project-b"],
            "evidence_refs": [
                {
                    **evidence().to_dict(),
                    "episode_id": "EP-source-a",
                },
                {
                    "sha256": "b" * 64,
                    "media_type": "application/json",
                    "role": "verification",
                    "episode_id": "EP-source-b",
                },
            ],
            "state": "candidate",
            "created_by": "llm-assisted",
        }
    )
    return KnowledgeItem.from_dict(payload)


def unrelated_map(project: str = "project-negative") -> DomainMap:
    return DomainMap(
        project_fingerprint=project,
        components=(
            ComponentObservation(
                component_id="view",
                path="web/view.py",
                roles=("presentation",),
            ),
        ),
    )


def validation_case(
    case_id: str,
    suite: str,
    domain_map: DomainMap,
    expected: bool,
    *,
    episode_id: str = "",
    critical: bool = True,
    authored: bool = False,
) -> ValidationCase:
    return ValidationCase(
        case_id=case_id,
        suite=suite,
        domain_map=domain_map,
        expected_applies=expected,
        verified=True,
        critical=critical,
        authored_from_candidate=authored,
        episode_id=episode_id,
        evidence_refs=(evidence(),),
    )


class LearningValidationTests(unittest.TestCase):
    def stores(self, root: Path):
        experience = ExperienceStore(root)
        candidates = CandidateStore(root)
        evaluations = EvaluationStore(root)
        for episode_id, project in (
            ("EP-source-a", "project-a"),
            ("EP-source-b", "project-b"),
        ):
            experience.put_episode(
                eligible_episode(episode_id, project_fingerprint=project)
            )
        candidate = structural_candidate()
        candidates.put_candidate(
            candidate,
            ("EP-source-a", "EP-source-b"),
            ("1" * 64, "2" * 64, "3" * 64),
        )
        return experience, candidates, evaluations, candidate

    def passing_cases(self) -> tuple[ValidationCase, ...]:
        return (
            validation_case(
                "VC-replay",
                "replay",
                renamed_domain_map("source", project_fingerprint="project-a"),
                True,
                episode_id="EP-source-a",
            ),
            validation_case("VC-negative", "negative", unrelated_map(), False),
            validation_case(
                "VC-holdout",
                "holdout",
                renamed_domain_map("holdout", project_fingerprint="project-c"),
                True,
            ),
        )

    def test_structural_candidate_passes_all_required_suites_as_shadow_only(self):
        with tempfile.TemporaryDirectory() as temp:
            experience, candidates, evaluations, candidate = self.stores(Path(temp))

            report = validate_and_store(
                candidate,
                experience,
                evaluations,
                self.passing_cases(),
                policy=ValidationPolicy(),
            )
            stored_candidate = candidates.get_candidate(candidate.knowledge_id)

        self.assertEqual(report["verdict"], "shadow_pass")
        self.assertEqual(
            set(report["suite_coverage"]),
            {"holdout", "metamorphic", "negative", "replay"},
        )
        self.assertEqual(report["critical_regressions"], 0)
        self.assertEqual(stored_candidate.state, "candidate")

    def test_overbroad_rule_is_rejected_by_unrelated_holdout(self):
        cases = list(self.passing_cases())
        cases[-1] = validation_case(
            "VC-unrelated-holdout",
            "holdout",
            renamed_domain_map("unrelated", project_fingerprint="project-z"),
            False,
        )
        with tempfile.TemporaryDirectory() as temp:
            experience, _candidates, evaluations, candidate = self.stores(Path(temp))
            report = validate_and_store(candidate, experience, evaluations, cases)

        self.assertEqual(report["verdict"], "rejected")
        self.assertIn("case_expectation_failed", report["reason_codes"])
        self.assertGreaterEqual(report["critical_regressions"], 1)

    def test_structural_candidate_without_positive_holdout_is_incomplete(self):
        cases = self.passing_cases()[:2]
        with tempfile.TemporaryDirectory() as temp:
            experience, _candidates, evaluations, candidate = self.stores(Path(temp))
            report = validate_and_store(candidate, experience, evaluations, cases)

        self.assertEqual(report["verdict"], "incomplete")
        self.assertIn("positive_holdout_required", report["reason_codes"])

    def test_known_counterexample_requires_a_verified_counterexample_case(self):
        payload = structural_candidate().to_dict()
        payload["counterexamples"] = [{"case": "known-boundary"}]
        candidate = KnowledgeItem.from_dict(payload)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            experience = ExperienceStore(root)
            for episode_id, project in (
                ("EP-source-a", "project-a"),
                ("EP-source-b", "project-b"),
            ):
                experience.put_episode(
                    eligible_episode(episode_id, project_fingerprint=project)
                )
            report = validate_and_store(
                candidate,
                experience,
                EvaluationStore(root),
                self.passing_cases(),
            )

        self.assertEqual(report["verdict"], "incomplete")
        self.assertEqual(report["unresolved_counterexamples"], 1)

    def test_evaluation_persistence_is_idempotent_and_omits_domain_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            experience, _candidates, evaluations, candidate = self.stores(root)
            first = validate_and_store(
                candidate,
                experience,
                evaluations,
                self.passing_cases(),
            )
            second = validate_and_store(
                candidate,
                experience,
                evaluations,
                self.passing_cases(),
            )
            serialized = evaluations.path.read_bytes()
            report_text = evaluations.report_path(first["evaluation_id"]).read_text(
                encoding="utf-8"
            )
            report_count = evaluations.report_count()

        self.assertEqual(first["evaluation_id"], second["evaluation_id"])
        self.assertEqual(report_count, 1)
        self.assertNotIn(b"renamed_reader.py", serialized)
        self.assertNotIn("renamed_reader.py", report_text)

    def test_validation_case_rejects_unverified_or_sensitive_input(self):
        payload = {
            "schema_version": 1,
            "case_id": "VC-invalid",
            "suite": "negative",
            "domain_map": {
                "project_fingerprint": "project-invalid",
                "components": [
                    {
                        "component_id": "view",
                        "path": "/home/example/private.py",
                        "roles": ["presentation"],
                    }
                ],
                "relations": [],
            },
            "expected_applies": False,
            "verified": False,
            "critical": True,
            "authored_from_candidate": False,
            "episode_id": "",
            "evidence_refs": [evidence().to_dict()],
        }

        with self.assertRaises(ValueError):
            ValidationCase.from_dict(payload)

    def test_validate_cli_runs_the_same_mechanical_gate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.stores(root)
            arguments = [
                "validate",
                "--data-dir",
                str(root),
                "--candidate",
                structural_candidate().knowledge_id,
            ]
            for case in self.passing_cases():
                path = root / f"{case.case_id}.json"
                path.write_text(json.dumps(case.to_dict()), encoding="utf-8")
                arguments.extend(("--case", str(path)))
            with contextlib.redirect_stdout(io.StringIO()) as output:
                result = learning_main(arguments)
            report = json.loads(output.getvalue())

        self.assertEqual(result, 0)
        self.assertEqual(report["verdict"], "shadow_pass")


if __name__ == "__main__":
    unittest.main()
