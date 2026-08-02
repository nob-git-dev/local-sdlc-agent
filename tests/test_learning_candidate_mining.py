import copy
import tempfile
import unittest
from pathlib import Path

from learning_runtime.candidate_miner import mine_candidates
from learning_runtime.candidate_store import CandidateStore
from learning_runtime.storage import ExperienceStore
from local_sdlc.models import RunnerError
from tests.learning_candidate_fixtures import (
    ScriptedCandidateLLM,
    abstraction_payload,
    eligible_episode,
    scope_payload,
    serialization_payload,
    valid_case_responses,
)


class CandidateMiningTests(unittest.TestCase):
    def stores(self, root: Path) -> tuple[ExperienceStore, CandidateStore]:
        return ExperienceStore(root), CandidateStore(root)

    def test_three_independent_calls_produce_only_a_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            experience, candidates = self.stores(Path(temp))
            episode = eligible_episode()
            experience.put_episode(episode)
            llm = ScriptedCandidateLLM(valid_case_responses())

            report = mine_candidates(experience, candidates, llm, max_batches=1)
            records = candidates.candidates()
            episode_count = experience.episode_count()

        self.assertEqual(report["accepted_count"], 1)
        self.assertEqual([call["function_name"] for call in llm.calls], [
            "candidate_abstraction",
            "scope_classification",
            "candidate_serialization",
        ])
        self.assertEqual(len({call["system_prompt"] for call in llm.calls}), 3)
        self.assertEqual(records[0]["state"], "candidate")
        self.assertEqual(records[0]["authority"], "llm_hypothesis")
        self.assertEqual(records[0]["created_by"], "llm-assisted")
        self.assertEqual(records[0]["version"], 1)
        self.assertEqual(records[0]["evidence_refs"][0]["episode_id"], episode["episode_id"])
        self.assertEqual(episode_count, 1)

    def test_hostile_activation_field_is_rejected_without_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            experience, candidates = self.stores(Path(temp))
            experience.put_episode(eligible_episode())
            responses = valid_case_responses()
            hostile = copy.deepcopy(responses["candidate_serialization"])
            hostile["state"] = "active"
            hostile["authority"] = "fixed_specification"
            responses["candidate_serialization"] = hostile

            report = mine_candidates(
                experience,
                candidates,
                ScriptedCandidateLLM(responses),
                max_batches=1,
            )
            candidate_count = candidates.candidate_count()
            attempts = candidates.attempts()

        self.assertEqual(report["accepted_count"], 0)
        self.assertEqual(report["rejected_count"], 1)
        self.assertEqual(candidate_count, 0)
        self.assertEqual(
            attempts[0]["reason_code"],
            "candidate_serialization_invalid",
        )

    def test_malformed_output_stops_the_batch_without_followup_calls(self):
        with tempfile.TemporaryDirectory() as temp:
            experience, candidates = self.stores(Path(temp))
            experience.put_episode(eligible_episode())
            responses = valid_case_responses()
            responses["candidate_abstraction"] = "not json"
            llm = ScriptedCandidateLLM(responses)

            report = mine_candidates(experience, candidates, llm, max_batches=1)
            candidate_count = candidates.candidate_count()

        self.assertEqual(report["rejected_count"], 1)
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(candidate_count, 0)

    def test_invalid_abstraction_enum_is_classified_at_its_protocol_stage(self):
        with tempfile.TemporaryDirectory() as temp:
            experience, candidates = self.stores(Path(temp))
            experience.put_episode(eligible_episode())
            responses = valid_case_responses()
            responses["candidate_abstraction"]["kind"] = "pattern"

            report = mine_candidates(
                experience,
                candidates,
                ScriptedCandidateLLM(responses),
                max_batches=1,
            )
            attempt = candidates.attempts()[0]

        self.assertEqual(report["rejected_count"], 1)
        self.assertEqual(attempt["reason_code"], "candidate_abstraction_invalid")

    def test_each_call_receives_a_machine_readable_output_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            experience, candidates = self.stores(Path(temp))
            experience.put_episode(eligible_episode())
            llm = ScriptedCandidateLLM(valid_case_responses())

            mine_candidates(experience, candidates, llm, max_batches=1)

        documents = {call["function_name"]: call["document"] for call in llm.calls}
        abstraction = documents["candidate_abstraction"]["output_contract"]
        scope = documents["scope_classification"]["output_contract"]
        serialization = documents["candidate_serialization"]["output_contract"]
        self.assertEqual(abstraction["constraints"]["kind"]["enum"], [
            "descriptive",
            "heuristic",
            "normative",
        ])
        self.assertEqual(
            scope["constraints"]["applicability"][
                "allowed_scope_applicability_pairs"
            ],
            [
                {
                    "scope": "case",
                    "applicability": {
                        "operator": "all",
                        "predicates": [
                            {"type": "episode_is", "episode_id": "EP-case-001"}
                        ],
                    },
                }
            ],
        )
        self.assertFalse(serialization["additional_fields"])
        self.assertEqual(
            serialization["expected_output"]["source_episode_ids"],
            ["EP-case-001"],
        )

    def test_expected_llm_call_failure_is_recorded_without_hiding_the_stage(self):
        with tempfile.TemporaryDirectory() as temp:
            experience, candidates = self.stores(Path(temp))
            experience.put_episode(eligible_episode())
            responses = valid_case_responses()
            responses["candidate_abstraction"] = RunnerError("fixture unavailable")

            report = mine_candidates(
                experience,
                candidates,
                ScriptedCandidateLLM(responses),
                max_batches=1,
            )
            attempt = candidates.attempts()[0]

        self.assertEqual(report["rejected_count"], 1)
        self.assertEqual(attempt["reason_code"], "candidate_abstraction_call_failure")

    def test_unexpected_internal_failure_is_recorded_and_raised(self):
        with tempfile.TemporaryDirectory() as temp:
            experience, candidates = self.stores(Path(temp))
            experience.put_episode(eligible_episode())
            responses = valid_case_responses()
            responses["candidate_abstraction"] = TypeError("fixture programming error")

            with self.assertRaisesRegex(TypeError, "programming error"):
                mine_candidates(
                    experience,
                    candidates,
                    ScriptedCandidateLLM(responses),
                    max_batches=1,
                )
            attempt = candidates.attempts()[0]

        self.assertEqual(
            attempt["reason_code"],
            "candidate_abstraction_internal_failure",
        )

    def test_high_impact_llm_proposal_remains_an_inactive_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            experience, candidates = self.stores(Path(temp))
            experience.put_episode(eligible_episode())
            responses = valid_case_responses()
            responses["candidate_abstraction"]["effect"] = "require"
            responses["candidate_serialization"] = serialization_payload(
                responses["candidate_abstraction"],
                responses["scope_classification"],
            )

            report = mine_candidates(
                experience,
                candidates,
                ScriptedCandidateLLM(responses),
                max_batches=1,
            )
            record = candidates.candidates()[0]

        self.assertEqual(report["accepted_count"], 1)
        self.assertEqual(record["effect"], "require")
        self.assertEqual(record["state"], "candidate")

if __name__ == "__main__":
    unittest.main()
