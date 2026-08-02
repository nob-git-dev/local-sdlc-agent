"""Deterministic candidate batches and scope ceilings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from sdlc_events import canonical_json, stable_identifier

from .candidate_contracts import (
    abstraction_output_contract,
    scope_output_contract,
)
from .candidate_protocol import CandidateScope
from .domain_map import DomainMap
from .privacy import sensitive_values
from .schema_validation import require_identifier


class ScopeBoundaryError(ValueError):
    """Raised when an LLM proposes broader applicability than evidence allows."""


@dataclass(frozen=True)
class CandidateBatch:
    batch_id: str
    episodes: tuple[dict[str, object], ...]
    allowed_scopes: tuple[str, ...]
    domain_maps: tuple[DomainMap, ...] = ()

    @property
    def source_episode_ids(self) -> tuple[str, ...]:
        return tuple(sorted(str(item["episode_id"]) for item in self.episodes))

    @property
    def project_fingerprints(self) -> tuple[str, ...]:
        return tuple(sorted({str(item["project_fingerprint"]) for item in self.episodes}))

    def abstraction_document(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "batch_id": self.batch_id,
            "allowed_scopes": list(self.allowed_scopes),
            "output_contract": abstraction_output_contract(self.source_episode_ids),
            "episodes": list(self.episodes),
        }

    def _scope_options(self) -> list[dict[str, object]]:
        options: list[dict[str, object]] = []
        for scope in self.allowed_scopes:
            predicates: list[dict[str, object]] = []
            if scope == "case":
                predicates.append(
                    {"type": "episode_is", "episode_id": self.source_episode_ids[0]}
                )
            elif scope == "project":
                predicates.append(
                    {
                        "type": "project_is",
                        "project_fingerprint": self.project_fingerprints[0],
                    }
                )
            elif scope == "structural":
                predicates.extend(
                    {
                        "type": "structural_signature_is",
                        "signature": signature,
                    }
                    for signature in sorted(
                        {item.structural_signature for item in self.domain_maps}
                    )
                )
            elif scope == "technology":
                predicates.extend(
                    {
                        "type": "technology_present",
                        "ecosystem": ecosystem,
                        "name": name,
                        "version": version,
                    }
                    for ecosystem, name, version in _common_technologies(self.domain_maps)
                )
            options.append({"scope": scope, "predicate_options": predicates})
        return options

    def scope_document(self, abstraction: Mapping[str, object]) -> dict[str, object]:
        structures = [
            {
                "structural_signature": domain_map.structural_signature,
                "projection": domain_map.structural_projection(),
            }
            for domain_map in self.domain_maps
        ]
        technologies = [
            {"ecosystem": ecosystem, "name": name, "version": version}
            for ecosystem, name, version in _common_technologies(self.domain_maps)
        ]
        return {
            "schema_version": 1,
            "batch_id": self.batch_id,
            "allowed_scopes": list(self.allowed_scopes),
            "episode_ids": list(self.source_episode_ids),
            "project_fingerprints": list(self.project_fingerprints),
            "domain_structures": structures,
            "common_evidenced_technologies": technologies,
            "output_contract": scope_output_contract(
                self.source_episode_ids,
                self._scope_options(),
            ),
            "abstraction": dict(abstraction),
        }

    def validate_scope(self, proposal: CandidateScope) -> None:
        if proposal.source_episode_ids != self.source_episode_ids:
            raise ScopeBoundaryError("scope source episodes do not match the batch")
        if proposal.scope not in self.allowed_scopes:
            raise ScopeBoundaryError("scope is broader than the batch evidence allows")
        predicates = proposal.applicability.predicates
        if len(predicates) != 1:
            raise ScopeBoundaryError("scope must select exactly one supplied predicate")
        option = next(
            item for item in self._scope_options() if item["scope"] == proposal.scope
        )
        allowed_predicates = {canonical_json(item) for item in option["predicate_options"]}
        proposed_predicates = {canonical_json(item.to_dict()) for item in predicates}
        if not proposed_predicates.issubset(allowed_predicates):
            raise ScopeBoundaryError("scope uses a predicate outside the supplied options")
        if proposal.scope == "case":
            expected = self.source_episode_ids[0]
            anchors = [item.get("episode_id") for item in predicates if item.predicate_type == "episode_is"]
            if len(self.episodes) != 1 or anchors != [expected]:
                raise ScopeBoundaryError("case scope must anchor the one source episode")
        elif proposal.scope == "project":
            expected = self.project_fingerprints[0]
            anchors = [item.get("project_fingerprint") for item in predicates if item.predicate_type == "project_is"]
            if len(self.project_fingerprints) != 1 or anchors != [expected]:
                raise ScopeBoundaryError("project scope must anchor the source project")
        else:
            if len(self.domain_maps) != len(self.project_fingerprints):
                raise ScopeBoundaryError("broad scope requires every source Domain Map")
            for domain_map in self.domain_maps:
                if not all(_predicate_matches(item, domain_map) for item in predicates):
                    raise ScopeBoundaryError("applicability is not true for every source Domain Map")


def _predicate_matches(predicate, domain_map: DomainMap) -> bool:
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
    return False


def _common_technologies(domain_maps: Sequence[DomainMap]) -> tuple[tuple[str, str, str], ...]:
    if not domain_maps:
        return ()
    observed = [
        {
            (technology.ecosystem, technology.name, technology.version)
            for component in domain_map.components
            for technology in component.technologies
            if technology.evidence_refs
        }
        for domain_map in domain_maps
    ]
    return tuple(sorted(set.intersection(*observed))) if observed else ()


def _validated_episode(raw: Mapping[str, object]) -> dict[str, object] | None:
    if raw.get("eligibility") != "eligible":
        return None
    required = {"episode_id", "project_fingerprint", "structural_signature"}
    if not required.issubset(raw):
        raise ValueError("eligible episode is missing candidate-mining fields")
    episode = dict(raw)
    require_identifier(episode["episode_id"], "episode_id")
    require_identifier(episode["project_fingerprint"], "project_fingerprint")
    require_identifier(episode["structural_signature"], "structural_signature")
    unsafe = sensitive_values(episode)
    if unsafe:
        raise ValueError("eligible episode contains sensitive values: " + ", ".join(unsafe))
    return episode


def _batch(
    episodes: Sequence[dict[str, object]],
    scopes: Sequence[str],
    domain_maps: Sequence[DomainMap] = (),
) -> CandidateBatch:
    ordered = tuple(sorted(episodes, key=lambda item: str(item["episode_id"])))
    batch_id = stable_identifier("CB", canonical_json([item["episode_id"] for item in ordered]))
    return CandidateBatch(
        batch_id=batch_id,
        episodes=ordered,
        allowed_scopes=tuple(scopes),
        domain_maps=tuple(sorted(domain_maps, key=lambda item: item.project_fingerprint)),
    )


def build_candidate_batches(
    episodes: Sequence[Mapping[str, object]],
    domain_maps: Mapping[str, DomainMap] | None = None,
) -> list[CandidateBatch]:
    maps = dict(domain_maps or {})
    eligible = [episode for raw in episodes if (episode := _validated_episode(raw)) is not None]
    by_structure: dict[str, list[dict[str, object]]] = {}
    for episode in eligible:
        by_structure.setdefault(str(episode["structural_signature"]), []).append(episode)

    batches: list[CandidateBatch] = []
    for group in by_structure.values():
        projects = sorted({str(item["project_fingerprint"]) for item in group})
        project_maps = [maps[project] for project in projects if project in maps]
        cross_project_structure = (
            len(projects) > 1
            and len(project_maps) == len(projects)
            and len({item.structural_signature for item in project_maps}) == 1
        )
        if cross_project_structure:
            scopes = ("technology", "structural") if _common_technologies(project_maps) else ("structural",)
            batches.append(_batch(group, scopes, project_maps))
            continue

        by_project: dict[str, list[dict[str, object]]] = {}
        for episode in group:
            by_project.setdefault(str(episode["project_fingerprint"]), []).append(episode)
        for project_group in by_project.values():
            if len(project_group) > 1:
                batches.append(_batch(project_group, ("project",)))
            else:
                batches.append(_batch(project_group, ("case",)))
    return sorted(batches, key=lambda item: item.batch_id)
