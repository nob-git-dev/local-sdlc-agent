import tempfile
import unittest
from pathlib import Path

from learning_runtime.candidate_protocol import (
    CandidateAbstraction,
    CandidateProtocolError,
    CandidateSerialization,
    parse_candidate_json,
)
from learning_runtime.candidate_store import CandidateStore
from learning_runtime.knowledge_schema import KnowledgeItem
from tests.learning_candidate_fixtures import (
    abstraction_payload,
    scope_payload,
    serialization_payload,
)
from tests.learning_knowledge_fixtures import knowledge_payload


class CandidateProtocolTests(unittest.TestCase):
    def test_nested_schema_errors_are_reported_as_protocol_errors(self):
        payload = abstraction_payload("EP-case-001")
        payload["kind"] = "pattern"

        with self.assertRaises(CandidateProtocolError):
            CandidateAbstraction.from_dict(payload)

    def test_abstraction_rejects_lifecycle_and_evidence_fields(self):
        for protected_field, value in (
            ("state", "active"),
            ("authority", "fixed_specification"),
            ("evidence_refs", [{"sha256": "f" * 64}]),
        ):
            with self.subTest(field=protected_field):
                payload = abstraction_payload("EP-case-001")
                payload[protected_field] = value
                with self.assertRaisesRegex(CandidateProtocolError, "unknown"):
                    CandidateAbstraction.from_dict(payload)

    def test_strict_json_rejects_fences_and_non_objects(self):
        with self.assertRaises(CandidateProtocolError):
            parse_candidate_json("```json\n{}\n```", "candidate")
        with self.assertRaises(CandidateProtocolError):
            parse_candidate_json("[]", "candidate")

    def test_short_proposition_limits_are_enforced(self):
        payload = abstraction_payload("EP-case-001")
        payload["antecedents"] = [{"fact": str(index)} for index in range(6)]
        with self.assertRaisesRegex(CandidateProtocolError, "at most 5"):
            CandidateAbstraction.from_dict(payload)

    def test_serialization_must_equal_prior_validated_documents(self):
        abstraction_dict = abstraction_payload("EP-case-001")
        scope_dict = scope_payload("case", "EP-case-001", "EP-case-001")
        abstraction = CandidateAbstraction.from_dict(abstraction_dict)
        serialized = serialization_payload(abstraction_dict, scope_dict)
        serialized["conclusion"] = {"recommendation": "unrelated_change"}

        candidate = CandidateSerialization.from_dict(serialized)

        with self.assertRaisesRegex(CandidateProtocolError, "drift"):
            candidate.assert_matches(abstraction, scope_dict)


class CandidateStoreTests(unittest.TestCase):
    def test_candidate_store_exposes_no_activation_operation(self):
        with tempfile.TemporaryDirectory() as temp:
            store = CandidateStore(Path(temp))

        self.assertFalse(hasattr(store, "activate"))
        self.assertFalse(hasattr(store, "activate_candidate"))
        self.assertFalse(hasattr(store, "promote_candidate"))

    def test_store_accepts_only_candidate_llm_hypotheses(self):
        with tempfile.TemporaryDirectory() as temp:
            store = CandidateStore(Path(temp))
            active_payload = knowledge_payload(authority="llm_hypothesis")
            active_payload.update(
                {
                    "state": "active",
                    "created_by": "llm-assisted",
                }
            )
            active = KnowledgeItem.from_dict(active_payload)

            with self.assertRaisesRegex(ValueError, "candidate state"):
                store.put_candidate(active, ("EP-case-001",), ("a" * 64,))
            self.assertEqual(store.candidate_count(), 0)

    def test_attempt_records_hashes_and_reason_but_not_raw_output(self):
        with tempfile.TemporaryDirectory() as temp:
            store = CandidateStore(Path(temp))
            inserted = store.record_attempt(
                batch_id="CB-test",
                status="rejected",
                reason_code="candidate_serialization_invalid",
                source_episode_ids=("EP-case-001",),
                response_hashes=("b" * 64,),
            )
            attempts = store.attempts()

        self.assertTrue(inserted)
        self.assertEqual(attempts[0]["reason_code"], "candidate_serialization_invalid")
        self.assertEqual(attempts[0]["response_hashes"], ["b" * 64])
        self.assertNotIn("raw_output", attempts[0])


if __name__ == "__main__":
    unittest.main()
