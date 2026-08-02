import unittest

from learning_runtime.domain_map import (
    ComponentObservation,
    DomainMap,
    DomainRelation,
    TechnologyObservation,
)
from tests.learning_knowledge_fixtures import renamed_domain_map


class DomainMapTests(unittest.TestCase):
    def test_renamed_structural_fixtures_have_the_same_projection(self):
        first = renamed_domain_map("alpha", project_fingerprint="project-a")
        second = renamed_domain_map("beta", project_fingerprint="project-b")

        self.assertEqual(first.structural_projection(), second.structural_projection())
        self.assertEqual(first.structural_signature, second.structural_signature)
        projection = str(first.structural_projection())
        self.assertNotIn("alpha", projection)
        self.assertNotIn("renamed_reader.py", projection)

    def test_domain_map_rejects_unsafe_or_unresolved_components(self):
        with self.assertRaises(ValueError):
            ComponentObservation(
                component_id="parser",
                path="../outside.py",
                roles=("parser",),
            )
        with self.assertRaises(ValueError):
            DomainMap(
                project_fingerprint="project-a",
                components=(
                    ComponentObservation(
                        component_id="parser",
                        path="src/parser.py",
                        roles=("parser",),
                    ),
                ),
                relations=(
                    DomainRelation(
                        source_component_id="parser",
                        relation="calls",
                        target_component_id="missing",
                    ),
                ),
            )

    def test_technology_observation_requires_mechanical_evidence(self):
        with self.assertRaises(ValueError):
            TechnologyObservation(ecosystem="python", name="sqlite3")

    def test_domain_map_round_trip_rejects_unknown_or_tampered_data(self):
        domain_map = renamed_domain_map("alpha", project_fingerprint="9f00abc")
        restored = DomainMap.from_dict(domain_map.to_local_dict())
        self.assertEqual(restored, domain_map)

        unknown = domain_map.to_local_dict()
        unknown["guessed_role"] = "parser"
        with self.assertRaises(ValueError):
            DomainMap.from_dict(unknown)

        tampered = domain_map.to_local_dict()
        tampered["structural_signature"] = "DM-tampered"
        with self.assertRaises(ValueError):
            DomainMap.from_dict(tampered)


if __name__ == "__main__":
    unittest.main()
