"""Strict inputs and policy for deterministic candidate validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .domain_map import DomainMap
from .knowledge_schema import EvidenceAnchor
from .privacy import sensitive_values
from .schema_validation import (
    KnowledgeValidationError,
    require_choice,
    require_identifier,
    require_mapping,
    require_sequence,
)


VALIDATION_SUITES = {
    "replay",
    "metamorphic",
    "negative",
    "holdout",
    "counterexample",
}
VALIDATION_CASE_FIELDS = {
    "schema_version",
    "case_id",
    "suite",
    "domain_map",
    "expected_applies",
    "verified",
    "critical",
    "authored_from_candidate",
    "episode_id",
    "evidence_refs",
}


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise KnowledgeValidationError(f"{field} must be a boolean")
    return value


@dataclass(frozen=True)
class ValidationCase:
    case_id: str
    suite: str
    domain_map: DomainMap
    expected_applies: bool
    verified: bool
    critical: bool
    authored_from_candidate: bool
    episode_id: str = ""
    evidence_refs: tuple[EvidenceAnchor, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "case_id", require_identifier(self.case_id, "case_id"))
        object.__setattr__(
            self,
            "suite",
            require_choice(self.suite, "suite", VALIDATION_SUITES),
        )
        for field in (
            "expected_applies",
            "verified",
            "critical",
            "authored_from_candidate",
        ):
            _boolean(getattr(self, field), field)
        episode_id = self.episode_id.strip()
        if episode_id:
            episode_id = require_identifier(episode_id, "episode_id")
        object.__setattr__(self, "episode_id", episode_id)
        evidence = tuple(self.evidence_refs)
        if not evidence:
            raise KnowledgeValidationError("validation case requires evidence_refs")
        object.__setattr__(self, "evidence_refs", evidence)
        unsafe = sensitive_values(self.to_evaluation_dict())
        if unsafe:
            raise KnowledgeValidationError(
                "validation case contains sensitive values: " + ", ".join(unsafe)
            )

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ValidationCase":
        missing = VALIDATION_CASE_FIELDS - set(payload)
        unknown = set(payload) - VALIDATION_CASE_FIELDS
        if missing:
            raise KnowledgeValidationError(
                "missing validation case fields: " + ", ".join(sorted(missing))
            )
        if unknown:
            raise KnowledgeValidationError(
                "unknown validation case fields: " + ", ".join(sorted(unknown))
            )
        if payload.get("schema_version") != 1:
            raise KnowledgeValidationError("unsupported validation case schema_version")
        raw_evidence = require_sequence(payload["evidence_refs"], "evidence_refs")
        return cls(
            case_id=str(payload["case_id"]),
            suite=str(payload["suite"]),
            domain_map=DomainMap.from_dict(
                require_mapping(payload["domain_map"], "domain_map")
            ),
            expected_applies=_boolean(payload["expected_applies"], "expected_applies"),
            verified=_boolean(payload["verified"], "verified"),
            critical=_boolean(payload["critical"], "critical"),
            authored_from_candidate=_boolean(
                payload["authored_from_candidate"],
                "authored_from_candidate",
            ),
            episode_id=str(payload["episode_id"]),
            evidence_refs=tuple(
                EvidenceAnchor.from_dict(
                    require_mapping(item, f"evidence_refs[{index}]")
                )
                for index, item in enumerate(raw_evidence)
            ),
        )

    def to_evaluation_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "suite": self.suite,
            "project_fingerprint": self.domain_map.project_fingerprint,
            "structural_signature": self.domain_map.structural_signature,
            "expected_applies": self.expected_applies,
            "verified": self.verified,
            "critical": self.critical,
            "authored_from_candidate": self.authored_from_candidate,
            "episode_id": self.episode_id,
            "evidence_refs": [item.to_dict() for item in self.evidence_refs],
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "case_id": self.case_id,
            "suite": self.suite,
            "domain_map": self.domain_map.to_local_dict(),
            "expected_applies": self.expected_applies,
            "verified": self.verified,
            "critical": self.critical,
            "authored_from_candidate": self.authored_from_candidate,
            "episode_id": self.episode_id,
            "evidence_refs": [item.to_dict() for item in self.evidence_refs],
        }


@dataclass(frozen=True)
class ValidationPolicy:
    minimum_evidence_quality: float = 1.0
    minimum_precision_lower_bound: float = 0.6
    confidence_z: float = 1.2816
    minimum_structural_support: int = 2
    minimum_technology_support: int = 2
    minimum_project_support: int = 1
    minimum_case_support: int = 1

    def __post_init__(self) -> None:
        for field in ("minimum_evidence_quality", "minimum_precision_lower_bound"):
            value = float(getattr(self, field))
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{field} must be between 0 and 1")
        if not 0.0 < float(self.confidence_z) <= 5.0:
            raise ValueError("confidence_z must be between 0 and 5")
        for field in (
            "minimum_structural_support",
            "minimum_technology_support",
            "minimum_project_support",
            "minimum_case_support",
        ):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{field} must be a positive integer")

    def minimum_support(self, scope: str) -> int:
        return {
            "structural": self.minimum_structural_support,
            "technology": self.minimum_technology_support,
            "project": self.minimum_project_support,
            "case": self.minimum_case_support,
        }[scope]

    def to_dict(self) -> dict[str, object]:
        return {
            "minimum_evidence_quality": self.minimum_evidence_quality,
            "minimum_precision_lower_bound": self.minimum_precision_lower_bound,
            "confidence_z": self.confidence_z,
            "minimum_support": {
                scope: self.minimum_support(scope)
                for scope in ("structural", "technology", "project", "case")
            },
        }
