"""Reusable renamed-structure fixtures for learning-schema tests."""

from learning_runtime.domain_map import (
    ComponentObservation,
    DomainMap,
    DomainRelation,
)
from learning_runtime.knowledge_schema import EvidenceAnchor


EVIDENCE_HASH = "a" * 64


def evidence() -> EvidenceAnchor:
    return EvidenceAnchor(
        sha256=EVIDENCE_HASH,
        media_type="application/json",
        role="verification",
    )


def renamed_domain_map(prefix: str, *, project_fingerprint: str) -> DomainMap:
    parser_id = f"{prefix}-reader"
    store_id = f"{prefix}-repository"
    return DomainMap(
        project_fingerprint=project_fingerprint,
        components=(
            ComponentObservation(
                component_id=parser_id,
                path=f"{prefix}/renamed_reader.py",
                symbols=(f"{prefix}_read",),
                roles=("parser", "state_machine"),
            ),
            ComponentObservation(
                component_id=store_id,
                path=f"{prefix}/renamed_store.py",
                symbols=(f"{prefix}_save",),
                roles=("persistence",),
            ),
        ),
        relations=(
            DomainRelation(
                source_component_id=parser_id,
                relation="writes_to",
                target_component_id=store_id,
            ),
        ),
    )


def knowledge_payload(
    *,
    scope: str = "structural",
    authority: str = "mechanical_observation",
    applicability: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "knowledge_id": "K-structural-parser-recovery",
        "version": 1,
        "kind": "heuristic",
        "scope": scope,
        "applicability": applicability
        or {
            "operator": "all",
            "predicates": [
                {"type": "role_present", "role": "parser"},
                {
                    "type": "relation_present",
                    "source_role": "parser",
                    "relation": "writes_to",
                    "target_role": "persistence",
                },
            ],
        },
        "antecedents": [{"fact": "failure_family_repeated"}],
        "conclusion": {"recommendation": "inspect_boundary_contract"},
        "effect": "recommend",
        "evidence_refs": [evidence().to_dict()],
        "supporting_projects": ["project-fixture-a"],
        "counterexamples": [],
        "generalization_rationale": "Depends on component roles, not names.",
        "regression_tests": ["renamed-structure"],
        "authority": authority,
        "confidence": 0.75,
        "state": "candidate",
        "supersedes": [],
        "created_by": "deterministic",
    }
