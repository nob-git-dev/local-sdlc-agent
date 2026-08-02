"""Validated Domain Map and rename-invariant structural queries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from sdlc_events import canonical_json, stable_identifier

from .domain_models import (
    ComponentObservation,
    DomainRelation,
    TechnologyObservation,
    normalize_domain_slug,
)
from .privacy import sensitive_values
from .schema_validation import (
    require_identifier,
    require_sequence,
    require_technology_name,
)


@dataclass(frozen=True)
class DomainMap:
    project_fingerprint: str
    components: tuple[ComponentObservation, ...]
    relations: tuple[DomainRelation, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        project_fingerprint = require_identifier(
            self.project_fingerprint,
            "project_fingerprint",
        )
        unsafe = sensitive_values({"project_fingerprint": project_fingerprint})
        if unsafe:
            raise ValueError("project_fingerprint contains sensitive data")
        object.__setattr__(self, "project_fingerprint", project_fingerprint)

        components = tuple(sorted(self.components, key=lambda item: item.component_id))
        if not components:
            raise ValueError("domain map requires at least one component")
        component_ids = {item.component_id for item in components}
        if len(component_ids) != len(components):
            raise ValueError("duplicate component_id")
        object.__setattr__(self, "components", components)

        relations = tuple(
            sorted(
                self.relations,
                key=lambda item: (
                    item.source_component_id,
                    item.relation,
                    item.target_component_id,
                ),
            )
        )
        for relation in relations:
            if (
                relation.source_component_id not in component_ids
                or relation.target_component_id not in component_ids
            ):
                raise ValueError("domain relation references an unknown component")
        if len({canonical_json(item.to_dict()) for item in relations}) != len(
            relations
        ):
            raise ValueError("duplicate domain relation")
        object.__setattr__(self, "relations", relations)

    def _components_by_id(self) -> dict[str, ComponentObservation]:
        return {item.component_id: item for item in self.components}

    def structural_projection(self) -> dict[str, object]:
        """Remove paths, symbols, IDs, project identity, and technologies."""
        components = sorted([list(item.roles) for item in self.components])
        by_id = self._components_by_id()
        relations = sorted(
            (
                {
                    "source_roles": list(by_id[item.source_component_id].roles),
                    "relation": item.relation,
                    "target_roles": list(by_id[item.target_component_id].roles),
                }
                for item in self.relations
            ),
            key=canonical_json,
        )
        return {"component_roles": components, "relations": relations}

    @property
    def structural_signature(self) -> str:
        return stable_identifier("DM", canonical_json(self.structural_projection()))

    def has_role(self, role: str) -> bool:
        normalized = normalize_domain_slug(role, "role")
        return any(normalized in item.roles for item in self.components)

    def has_relation(
        self,
        source_role: str,
        relation: str,
        target_role: str,
    ) -> bool:
        source = normalize_domain_slug(source_role, "source_role")
        kind = normalize_domain_slug(relation, "relation")
        target = normalize_domain_slug(target_role, "target_role")
        by_id = self._components_by_id()
        return any(
            item.relation == kind
            and source in by_id[item.source_component_id].roles
            and target in by_id[item.target_component_id].roles
            for item in self.relations
        )

    def has_technology(self, ecosystem: str, name: str, version: str = "") -> bool:
        identity = (
            normalize_domain_slug(ecosystem, "ecosystem"),
            require_technology_name(name, "name"),
        )
        expected_version = version.strip()
        return any(
            (technology.ecosystem, technology.name) == identity
            and (not expected_version or technology.version == expected_version)
            and bool(technology.evidence_refs)
            for component in self.components
            for technology in component.technologies
        )

    def to_local_dict(self) -> dict[str, object]:
        return {
            "project_fingerprint": self.project_fingerprint,
            "components": [item.to_dict() for item in self.components],
            "relations": [item.to_dict() for item in self.relations],
            "structural_signature": self.structural_signature,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "DomainMap":
        unknown = set(payload) - {
            "project_fingerprint",
            "components",
            "relations",
            "structural_signature",
        }
        if unknown:
            raise ValueError(
                "unknown domain map fields: " + ", ".join(sorted(unknown))
            )
        components = require_sequence(payload.get("components"), "components")
        relations = require_sequence(payload.get("relations", []), "relations")
        if any(not isinstance(item, Mapping) for item in components):
            raise ValueError("components must contain objects")
        if any(not isinstance(item, Mapping) for item in relations):
            raise ValueError("relations must contain objects")
        domain_map = cls(
            project_fingerprint=str(payload.get("project_fingerprint") or ""),
            components=tuple(
                ComponentObservation.from_dict(item)
                for item in components
                if isinstance(item, Mapping)
            ),
            relations=tuple(
                DomainRelation.from_dict(item)
                for item in relations
                if isinstance(item, Mapping)
            ),
        )
        expected_signature = str(payload.get("structural_signature") or "")
        if expected_signature and expected_signature != domain_map.structural_signature:
            raise ValueError(
                "domain map structural_signature does not match its content"
            )
        return domain_map


def validate_domain_map(payload: Mapping[str, object]) -> DomainMap:
    """Validate explicit facts without inferring roles from names."""
    return DomainMap.from_dict(payload)


__all__ = [
    "ComponentObservation",
    "DomainMap",
    "DomainRelation",
    "TechnologyObservation",
    "validate_domain_map",
]
