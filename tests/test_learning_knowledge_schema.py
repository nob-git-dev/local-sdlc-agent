import copy
import unittest

from learning_runtime.applicability import evaluate_applicability
from learning_runtime.domain_map import (
    ComponentObservation,
    DomainMap,
    TechnologyObservation,
)
from learning_runtime.knowledge_schema import (
    EvidenceAnchor,
    KnowledgeItem,
    KnowledgeValidationError,
)
from tests.learning_knowledge_fixtures import (
    evidence,
    knowledge_payload,
    renamed_domain_map,
)


class KnowledgeSchemaTests(unittest.TestCase):
    def test_scope_authority_applicability_evidence_and_effect_are_required(self):
        required = ("scope", "authority", "applicability", "evidence_refs", "effect")
        for field in required:
            with self.subTest(field=field):
                payload = knowledge_payload()
                payload.pop(field)
                with self.assertRaises(KnowledgeValidationError):
                    KnowledgeItem.from_dict(payload)

    def test_scope_and_authority_are_independent_axes(self):
        structural_hypothesis = KnowledgeItem.from_dict(
            knowledge_payload(authority="llm_hypothesis")
        )
        project_observation = KnowledgeItem.from_dict(
            knowledge_payload(
                scope="project",
                authority="mechanical_observation",
                applicability={
                    "operator": "all",
                    "predicates": [
                        {"type": "project_is", "project_fingerprint": "project-a"}
                    ],
                },
            )
        )

        self.assertEqual(structural_hypothesis.scope, "structural")
        self.assertEqual(structural_hypothesis.authority, "llm_hypothesis")
        self.assertEqual(project_observation.scope, "project")
        self.assertEqual(project_observation.authority, "mechanical_observation")

    def test_scope_rejects_an_incompatible_or_ambiguous_predicate(self):
        incompatible = knowledge_payload(
            applicability={
                "operator": "all",
                "predicates": [
                    {"type": "technology_present", "ecosystem": "python", "name": "sqlite3"}
                ],
            }
        )
        with self.assertRaises(KnowledgeValidationError):
            KnowledgeItem.from_dict(incompatible)

        ambiguous = knowledge_payload()
        ambiguous["applicability"] = {"required_path": "src/parser.py"}
        with self.assertRaises(KnowledgeValidationError):
            KnowledgeItem.from_dict(ambiguous)

    def test_shared_knowledge_rejects_sensitive_values(self):
        payload = knowledge_payload()
        payload["conclusion"] = {"owner": "private@example.com"}
        with self.assertRaises(KnowledgeValidationError):
            KnowledgeItem.from_dict(payload)

    def test_evidence_anchor_has_one_canonical_path_free_form(self):
        anchor = EvidenceAnchor(
            sha256="A" * 64,
            media_type=" application/json ",
            role="Verification",
            episode_id=" EP-001 ",
        )
        self.assertEqual(anchor.sha256, "a" * 64)
        self.assertEqual(anchor.media_type, "application/json")
        self.assertEqual(anchor.role, "verification")
        self.assertEqual(anchor.episode_id, "EP-001")
        self.assertNotIn("path", anchor.to_dict())
        self.assertEqual(EvidenceAnchor.from_dict(anchor.to_dict()), anchor)


class ApplicabilityTests(unittest.TestCase):
    def test_structural_knowledge_applies_to_both_renamed_fixtures(self):
        item = KnowledgeItem.from_dict(knowledge_payload())
        first = renamed_domain_map("alpha", project_fingerprint="project-a")
        second = renamed_domain_map("beta", project_fingerprint="project-b")

        self.assertTrue(evaluate_applicability(item, first).applies)
        self.assertTrue(evaluate_applicability(item, second).applies)

        unrelated = DomainMap(
            project_fingerprint="project-c",
            components=(
                ComponentObservation(
                    component_id="view",
                    path="web/view.py",
                    roles=("presentation",),
                ),
            ),
        )
        decision = evaluate_applicability(item, unrelated)
        self.assertFalse(decision.applies)
        self.assertEqual(len(decision.failed_predicates), 2)

    def test_technology_scope_requires_the_observed_technology(self):
        technology = TechnologyObservation(
            ecosystem="python",
            name="sqlite3",
            version="stdlib",
            evidence_refs=(evidence(),),
        )
        with_sqlite = DomainMap(
            project_fingerprint="project-sql",
            components=(
                ComponentObservation(
                    component_id="store",
                    path="src/store.py",
                    roles=("persistence",),
                    technologies=(technology,),
                ),
            ),
        )
        without_sqlite = DomainMap(
            project_fingerprint="project-memory",
            components=(
                ComponentObservation(
                    component_id="store",
                    path="src/store.py",
                    roles=("persistence",),
                ),
            ),
        )
        payload = knowledge_payload(
            scope="technology",
            applicability={
                "operator": "all",
                "predicates": [
                    {"type": "role_present", "role": "persistence"},
                    {
                        "type": "technology_present",
                        "ecosystem": "python",
                        "name": "sqlite3",
                        "version": "stdlib",
                    },
                ],
            },
        )
        item = KnowledgeItem.from_dict(payload)

        self.assertTrue(evaluate_applicability(item, with_sqlite).applies)
        self.assertFalse(evaluate_applicability(item, without_sqlite).applies)

    def test_scoped_package_name_is_a_mechanical_technology_key(self):
        technology = TechnologyObservation(
            ecosystem="npm",
            name="@scope/package",
            version="1.2.3",
            evidence_refs=(evidence(),),
        )
        domain_map = DomainMap(
            project_fingerprint="7abc123",
            components=(
                ComponentObservation(
                    component_id="adapter",
                    path="src/adapter.js",
                    roles=("transport",),
                    technologies=(technology,),
                ),
            ),
        )
        item = KnowledgeItem.from_dict(
            knowledge_payload(
                scope="technology",
                applicability={
                    "operator": "all",
                    "predicates": [
                        {
                            "type": "technology_present",
                            "ecosystem": "npm",
                            "name": "@scope/package",
                            "version": "1.2.3",
                        }
                    ],
                },
            )
        )
        self.assertTrue(evaluate_applicability(item, domain_map).applies)

    def test_project_scope_matches_only_the_declared_fingerprint(self):
        payload = knowledge_payload(
            scope="project",
            applicability={
                "operator": "all",
                "predicates": [
                    {"type": "project_is", "project_fingerprint": "project-a"}
                ],
            },
        )
        item = KnowledgeItem.from_dict(payload)

        self.assertTrue(
            evaluate_applicability(
                item,
                renamed_domain_map("alpha", project_fingerprint="project-a"),
            ).applies
        )
        self.assertFalse(
            evaluate_applicability(
                item,
                renamed_domain_map("alpha", project_fingerprint="project-b"),
            ).applies
        )

    def test_case_scope_matches_only_the_declared_episode(self):
        item = KnowledgeItem.from_dict(
            knowledge_payload(
                scope="case",
                applicability={
                    "operator": "all",
                    "predicates": [
                        {"type": "episode_is", "episode_id": "EP-observed"}
                    ],
                },
            )
        )
        domain_map = renamed_domain_map(
            "alpha",
            project_fingerprint="project-a",
        )

        self.assertTrue(
            evaluate_applicability(
                item,
                domain_map,
                episode_id="EP-observed",
            ).applies
        )
        self.assertFalse(
            evaluate_applicability(
                item,
                domain_map,
                episode_id="EP-unrelated",
            ).applies
        )

    def test_predicate_order_does_not_change_normalized_knowledge(self):
        first = knowledge_payload()
        second = copy.deepcopy(first)
        second["applicability"]["predicates"].reverse()

        first_item = KnowledgeItem.from_dict(first)
        second_item = KnowledgeItem.from_dict(second)

        self.assertEqual(first_item.to_dict(), second_item.to_dict())

    def test_input_mutation_does_not_change_the_normalized_record(self):
        payload = knowledge_payload()
        payload["antecedents"] = [{"fact": {"kind": "repeated"}}]
        item = KnowledgeItem.from_dict(payload)

        payload["antecedents"][0]["fact"]["kind"] = "changed"
        payload["conclusion"]["recommendation"] = "changed"

        self.assertEqual(item.antecedents[0]["fact"]["kind"], "repeated")
        self.assertEqual(
            item.conclusion["recommendation"],
            "inspect_boundary_contract",
        )


if __name__ == "__main__":
    unittest.main()
