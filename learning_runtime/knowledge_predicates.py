"""Version-one applicability predicate schema and scope constraints."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from sdlc_events import canonical_json

from .schema_validation import (
    KnowledgeValidationError,
    require_choice,
    require_identifier,
    require_mapping,
    require_sequence,
    require_slug,
    require_string,
    require_technology_name,
)


PREDICATE_FIELDS = {
    "role_present": ({"role"}, set()),
    "relation_present": ({"source_role", "relation", "target_role"}, set()),
    "structural_signature_is": ({"signature"}, set()),
    "technology_present": ({"ecosystem", "name"}, {"version"}),
    "project_is": ({"project_fingerprint"}, set()),
    "episode_is": ({"episode_id"}, set()),
}
STRUCTURAL_PREDICATES = {
    "role_present",
    "relation_present",
    "structural_signature_is",
}
SCOPE_PREDICATES = {
    "structural": STRUCTURAL_PREDICATES,
    "technology": STRUCTURAL_PREDICATES | {"technology_present"},
    "project": STRUCTURAL_PREDICATES | {"technology_present", "project_is"},
    "case": STRUCTURAL_PREDICATES
    | {"technology_present", "project_is", "episode_is"},
}
SCOPE_ANCHORS = {
    "structural": STRUCTURAL_PREDICATES,
    "technology": {"technology_present"},
    "project": {"project_is"},
    "case": {"episode_is"},
}


@dataclass(frozen=True)
class ApplicabilityPredicate:
    predicate_type: str
    parameters: tuple[tuple[str, str], ...]

    def get(self, name: str, default: str = "") -> str:
        return dict(self.parameters).get(name, default)

    def to_dict(self) -> dict[str, object]:
        return {"type": self.predicate_type, **dict(self.parameters)}

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ApplicabilityPredicate":
        predicate_type = require_choice(
            payload.get("type"),
            "applicability predicate type",
            set(PREDICATE_FIELDS),
        )
        required, optional = PREDICATE_FIELDS[predicate_type]
        fields = set(payload) - {"type"}
        missing = required - fields
        unknown = fields - required - optional
        if missing:
            raise KnowledgeValidationError(
                f"{predicate_type} missing fields: " + ", ".join(sorted(missing))
            )
        if unknown:
            raise KnowledgeValidationError(
                f"{predicate_type} unknown fields: " + ", ".join(sorted(unknown))
            )

        normalized: dict[str, str] = {}
        for name in sorted(fields):
            field = f"{predicate_type}.{name}"
            if name in {"role", "source_role", "target_role", "relation", "ecosystem"}:
                normalized[name] = require_slug(payload[name], field)
            elif name == "name":
                normalized[name] = require_technology_name(payload[name], field)
            elif name in {"signature", "project_fingerprint", "episode_id"}:
                normalized[name] = require_identifier(payload[name], field)
            else:
                normalized[name] = require_string(payload[name], field)
        return cls(predicate_type=predicate_type, parameters=tuple(normalized.items()))


@dataclass(frozen=True)
class Applicability:
    operator: str
    predicates: tuple[ApplicabilityPredicate, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "operator": self.operator,
            "predicates": [item.to_dict() for item in self.predicates],
        }

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, object],
        *,
        scope: str,
    ) -> "Applicability":
        unknown = set(payload) - {"operator", "predicates"}
        if unknown:
            raise KnowledgeValidationError(
                "unknown applicability fields: " + ", ".join(sorted(unknown))
            )
        operator = require_choice(
            payload.get("operator"),
            "applicability operator",
            {"all"},
        )
        raw_predicates = require_sequence(
            payload.get("predicates"),
            "applicability predicates",
        )
        if not raw_predicates:
            raise KnowledgeValidationError("applicability predicates are required")
        predicates = tuple(
            ApplicabilityPredicate.from_dict(
                require_mapping(item, f"applicability predicates[{index}]")
            )
            for index, item in enumerate(raw_predicates)
        )
        predicate_types = {item.predicate_type for item in predicates}
        invalid = predicate_types - SCOPE_PREDICATES[scope]
        if invalid:
            raise KnowledgeValidationError(
                f"{scope} scope cannot use predicates: "
                + ", ".join(sorted(invalid))
            )
        if not predicate_types.intersection(SCOPE_ANCHORS[scope]):
            required = ", ".join(sorted(SCOPE_ANCHORS[scope]))
            raise KnowledgeValidationError(
                f"{scope} scope requires at least one of: {required}"
            )
        normalized = tuple(
            sorted(predicates, key=lambda item: canonical_json(item.to_dict()))
        )
        if len({canonical_json(item.to_dict()) for item in normalized}) != len(
            normalized
        ):
            raise KnowledgeValidationError("duplicate applicability predicate")
        return cls(operator=operator, predicates=normalized)
