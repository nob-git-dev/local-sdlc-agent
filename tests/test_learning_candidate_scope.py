import tempfile
import unittest
from pathlib import Path

from learning_runtime.candidate_miner import mine_candidates
from learning_runtime.candidate_store import CandidateStore
from learning_runtime.domain_map import (
    ComponentObservation,
    DomainMap,
    TechnologyObservation,
)
from learning_runtime.storage import ExperienceStore
from tests.learning_candidate_fixtures import (
    ScriptedCandidateLLM,
    abstraction_payload,
    eligible_episode,
    scope_payload,
    serialization_payload,
    valid_case_responses,
)
from tests.learning_knowledge_fixtures import evidence, renamed_domain_map


class CandidateScopeBoundaryTests(unittest.TestCase):
    def stores(self, root: Path) -> tuple[ExperienceStore, CandidateStore]:
        return ExperienceStore(root), CandidateStore(root)

    def test_confounded_case_only_episode_is_retained_but_never_mined(self):
        with tempfile.TemporaryDirectory() as temp:
            experience, candidates = self.stores(Path(temp))
            episode = eligible_episode()
            episode["eligibility"] = "case_only"
            episode["reason_codes"] = ["concurrent_changes"]
            experience.put_episode(episode)
            llm = ScriptedCandidateLLM(valid_case_responses())

            report = mine_candidates(experience, candidates, llm, max_batches=1)
            episode_count = experience.episode_count()
            candidate_count = candidates.candidate_count()

        self.assertEqual(report["batch_count"], 0)
        self.assertEqual(llm.calls, [])
        self.assertEqual(episode_count, 1)
        self.assertEqual(candidate_count, 0)

    def test_one_incident_cannot_claim_structural_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            experience, candidates = self.stores(Path(temp))
            episode = eligible_episode()
            experience.put_episode(episode)
            abstraction = abstraction_payload(str(episode["episode_id"]))
            scope = scope_payload(
                "structural",
                str(episode["structural_signature"]),
                str(episode["episode_id"]),
            )
            responses = {
                "candidate_abstraction": abstraction,
                "scope_classification": scope,
                "candidate_serialization": serialization_payload(abstraction, scope),
            }

            report = mine_candidates(
                experience,
                candidates,
                ScriptedCandidateLLM(responses),
                max_batches=1,
            )
            attempt = candidates.attempts()[0]

        self.assertEqual(report["accepted_count"], 0)
        self.assertEqual(attempt["reason_code"], "scope_not_allowed")

    def test_scope_rejects_a_true_but_unsupplied_predicate(self):
        with tempfile.TemporaryDirectory() as temp:
            experience, candidates = self.stores(Path(temp))
            episode = eligible_episode()
            experience.put_episode(episode)
            abstraction = abstraction_payload(str(episode["episode_id"]))
            scope = scope_payload(
                "case",
                str(episode["episode_id"]),
                str(episode["episode_id"]),
            )
            scope["applicability"]["predicates"].append(
                {"type": "role_present", "role": "persistence"}
            )
            responses = {
                "candidate_abstraction": abstraction,
                "scope_classification": scope,
                "candidate_serialization": serialization_payload(abstraction, scope),
            }

            report = mine_candidates(
                experience,
                candidates,
                ScriptedCandidateLLM(responses),
                max_batches=1,
            )
            attempt = candidates.attempts()[0]

        self.assertEqual(report["accepted_count"], 0)
        self.assertEqual(attempt["reason_code"], "scope_not_allowed")

    def test_sanitized_episode_projection_never_sends_a_local_path(self):
        with tempfile.TemporaryDirectory() as temp:
            experience, candidates = self.stores(Path(temp))
            episode = eligible_episode()
            episode["context"]["diagnostic_path"] = "/home/example/private/source.py"
            experience.put_episode(episode)
            llm = ScriptedCandidateLLM(valid_case_responses())

            report = mine_candidates(experience, candidates, llm, max_batches=1)
            sent_document = llm.calls[0]["document"]

        self.assertEqual(report["accepted_count"], 1)
        self.assertNotIn("/home/example", str(sent_document))
        self.assertIn("<redacted-absolute-path>", str(sent_document))

    def test_two_renamed_projects_can_propose_structural_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            experience, candidates = self.stores(Path(temp))
            first = eligible_episode("EP-alpha", project_fingerprint="project-a")
            second = eligible_episode("EP-beta", project_fingerprint="project-b")
            experience.put_episode(first)
            experience.put_episode(second)
            first_map = renamed_domain_map("alpha", project_fingerprint="project-a")
            second_map = renamed_domain_map("beta", project_fingerprint="project-b")
            episode_ids = ("EP-alpha", "EP-beta")
            abstraction = abstraction_payload(*episode_ids)
            scope = scope_payload(
                "structural",
                first_map.structural_signature,
                *episode_ids,
            )
            responses = {
                "candidate_abstraction": abstraction,
                "scope_classification": scope,
                "candidate_serialization": serialization_payload(abstraction, scope),
            }

            report = mine_candidates(
                experience,
                candidates,
                ScriptedCandidateLLM(responses),
                domain_maps={"project-a": first_map, "project-b": second_map},
                max_batches=1,
            )
            record = candidates.candidates()[0]

        self.assertEqual(report["accepted_count"], 1)
        self.assertEqual(record["scope"], "structural")
        self.assertEqual(record["supporting_projects"], ["project-a", "project-b"])

    def test_technology_scope_requires_common_evidenced_observation(self):
        def technology_map(project: str, component: str) -> DomainMap:
            return DomainMap(
                project_fingerprint=project,
                components=(
                    ComponentObservation(
                        component_id=component,
                        path=f"src/{component}.py",
                        roles=("persistence",),
                        technologies=(
                            TechnologyObservation(
                                ecosystem="python",
                                name="sqlite3",
                                version="stdlib",
                                evidence_refs=(evidence(),),
                            ),
                        ),
                    ),
                ),
            )

        with tempfile.TemporaryDirectory() as temp:
            experience, candidates = self.stores(Path(temp))
            first = eligible_episode("EP-tech-a", project_fingerprint="project-a")
            second = eligible_episode("EP-tech-b", project_fingerprint="project-b")
            experience.put_episode(first)
            experience.put_episode(second)
            first_map = technology_map("project-a", "store-a")
            second_map = technology_map("project-b", "store-b")
            episode_ids = ("EP-tech-a", "EP-tech-b")
            abstraction = abstraction_payload(*episode_ids)
            scope = {
                "schema_version": 1,
                "scope": "technology",
                "applicability": {
                    "operator": "all",
                    "predicates": [
                        {
                            "type": "technology_present",
                            "ecosystem": "python",
                            "name": "sqlite3",
                            "version": "stdlib",
                        }
                    ],
                },
                "source_episode_ids": list(episode_ids),
            }
            responses = {
                "candidate_abstraction": abstraction,
                "scope_classification": scope,
                "candidate_serialization": serialization_payload(abstraction, scope),
            }

            report = mine_candidates(
                experience,
                candidates,
                ScriptedCandidateLLM(responses),
                domain_maps={"project-a": first_map, "project-b": second_map},
                max_batches=1,
            )
            record = candidates.candidates()[0]

        self.assertEqual(report["accepted_count"], 1)
        self.assertEqual(record["scope"], "technology")


if __name__ == "__main__":
    unittest.main()
