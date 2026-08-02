"""Mechanical knowledge applicability evaluation with no activation authority."""

from __future__ import annotations

from dataclasses import dataclass

from .domain_map import DomainMap
from .knowledge_schema import ApplicabilityPredicate, KnowledgeItem


@dataclass(frozen=True)
class ApplicabilityDecision:
    knowledge_id: str
    scope: str
    applies: bool
    matched_predicates: tuple[dict[str, object], ...]
    failed_predicates: tuple[dict[str, object], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "knowledge_id": self.knowledge_id,
            "scope": self.scope,
            "applies": self.applies,
            "matched_predicates": list(self.matched_predicates),
            "failed_predicates": list(self.failed_predicates),
        }


def _matches(
    predicate: ApplicabilityPredicate,
    domain_map: DomainMap,
    *,
    episode_id: str,
) -> bool:
    kind = predicate.predicate_type
    if kind == "role_present":
        return domain_map.has_role(predicate.get("role"))
    if kind == "relation_present":
        return domain_map.has_relation(
            predicate.get("source_role"),
            predicate.get("relation"),
            predicate.get("target_role"),
        )
    if kind == "structural_signature_is":
        return domain_map.structural_signature == predicate.get("signature")
    if kind == "technology_present":
        return domain_map.has_technology(
            predicate.get("ecosystem"),
            predicate.get("name"),
            predicate.get("version"),
        )
    if kind == "project_is":
        return (
            domain_map.project_fingerprint
            == predicate.get("project_fingerprint")
        )
    if kind == "episode_is":
        return bool(episode_id) and episode_id == predicate.get("episode_id")
    return False


def evaluate_applicability(
    item: KnowledgeItem,
    domain_map: DomainMap,
    *,
    episode_id: str = "",
) -> ApplicabilityDecision:
    """Evaluate all declared predicates without changing knowledge state."""
    matched: list[dict[str, object]] = []
    failed: list[dict[str, object]] = []
    for predicate in item.applicability.predicates:
        target = (
            matched
            if _matches(predicate, domain_map, episode_id=episode_id)
            else failed
        )
        target.append(predicate.to_dict())
    return ApplicabilityDecision(
        knowledge_id=item.knowledge_id,
        scope=item.scope,
        applies=not failed,
        matched_predicates=tuple(matched),
        failed_predicates=tuple(failed),
    )
