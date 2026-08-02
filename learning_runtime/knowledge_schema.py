"""Strict version-one knowledge records for the learning control plane."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from sdlc_events import canonical_json

from .knowledge_predicates import Applicability, ApplicabilityPredicate
from .privacy import sensitive_values
from .schema_validation import (
    KnowledgeValidationError,
    identifier_values,
    json_object,
    json_objects,
    json_values,
    require_choice,
    require_identifier,
    require_mapping,
    require_sequence,
    require_slug,
    require_string,
    string_values,
)


KNOWLEDGE_KINDS = {"normative", "descriptive", "heuristic"}
KNOWLEDGE_SCOPES = {"structural", "technology", "project", "case"}
KNOWLEDGE_AUTHORITIES = {
    "fixed_specification",
    "mechanical_observation",
    "source_analysis",
    "verified_official_contract",
    "repeated_empirical_pattern",
    "case",
    "llm_hypothesis",
}
KNOWLEDGE_EFFECTS = {"observe", "recommend", "require", "forbid"}
KNOWLEDGE_STATES = {"candidate", "shadow", "active", "challenged", "retired"}
KNOWLEDGE_CREATORS = {"deterministic", "llm-assisted", "human"}
REQUIRED_FIELDS = {
    "knowledge_id",
    "version",
    "kind",
    "scope",
    "applicability",
    "antecedents",
    "conclusion",
    "effect",
    "evidence_refs",
    "supporting_projects",
    "counterexamples",
    "generalization_rationale",
    "regression_tests",
    "authority",
    "confidence",
    "state",
    "supersedes",
    "created_by",
}


@dataclass(frozen=True)
class EvidenceAnchor:
    """A path-free evidence reference suitable for cross-project storage."""

    sha256: str
    media_type: str = "application/octet-stream"
    role: str = "support"
    episode_id: str = ""

    def __post_init__(self) -> None:
        sha256 = require_string(self.sha256, "evidence sha256").lower()
        if len(sha256) != 64:
            raise KnowledgeValidationError(
                "evidence sha256 must contain 64 hexadecimal characters"
            )
        try:
            int(sha256, 16)
        except ValueError as exc:
            raise KnowledgeValidationError("evidence sha256 is not hexadecimal") from exc
        object.__setattr__(self, "sha256", sha256)
        object.__setattr__(
            self,
            "media_type",
            require_string(self.media_type, "evidence media_type"),
        )
        object.__setattr__(self, "role", require_slug(self.role, "evidence role"))
        episode_id = require_string(
            self.episode_id,
            "evidence episode_id",
            allow_empty=True,
        )
        if episode_id:
            episode_id = require_identifier(episode_id, "evidence episode_id")
        object.__setattr__(self, "episode_id", episode_id)

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "sha256": self.sha256,
            "media_type": self.media_type,
            "role": self.role,
        }
        if self.episode_id:
            payload["episode_id"] = self.episode_id
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "EvidenceAnchor":
        allowed = {"sha256", "media_type", "role", "episode_id"}
        unknown = set(payload) - allowed
        if unknown:
            raise KnowledgeValidationError(
                "unknown evidence fields: " + ", ".join(sorted(unknown))
            )
        return cls(
            sha256=require_string(payload.get("sha256"), "evidence sha256"),
            media_type=require_string(
                payload.get("media_type", "application/octet-stream"),
                "evidence media_type",
            ),
            role=require_string(payload.get("role", "support"), "evidence role"),
            episode_id=require_string(
                payload.get("episode_id", ""),
                "evidence episode_id",
                allow_empty=True,
            ),
        )


@dataclass(frozen=True)
class KnowledgeItem:
    knowledge_id: str
    version: int
    kind: str
    scope: str
    applicability: Applicability
    antecedents: tuple[dict[str, object], ...]
    conclusion: dict[str, object]
    effect: str
    evidence_refs: tuple[EvidenceAnchor, ...]
    supporting_projects: tuple[str, ...]
    counterexamples: tuple[object, ...]
    generalization_rationale: str
    regression_tests: tuple[str, ...]
    authority: str
    confidence: float
    state: str
    supersedes: tuple[str, ...]
    created_by: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "KnowledgeItem":
        missing = REQUIRED_FIELDS - set(payload)
        unknown = set(payload) - REQUIRED_FIELDS
        if missing:
            raise KnowledgeValidationError(
                "missing knowledge fields: " + ", ".join(sorted(missing))
            )
        if unknown:
            raise KnowledgeValidationError(
                "unknown knowledge fields: " + ", ".join(sorted(unknown))
            )
        unsafe = sensitive_values(payload)
        if unsafe:
            raise KnowledgeValidationError(
                "knowledge contains sensitive values: " + ", ".join(unsafe)
            )

        scope = require_choice(payload["scope"], "scope", KNOWLEDGE_SCOPES)
        version = payload["version"]
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise KnowledgeValidationError("version must be a positive integer")
        raw_confidence = payload["confidence"]
        if isinstance(raw_confidence, bool) or not isinstance(
            raw_confidence,
            (int, float),
        ):
            raise KnowledgeValidationError("confidence must be a number")
        confidence = float(raw_confidence)
        if not 0.0 <= confidence <= 1.0:
            raise KnowledgeValidationError("confidence must be between 0 and 1")

        raw_evidence = require_sequence(payload["evidence_refs"], "evidence_refs")
        if not raw_evidence:
            raise KnowledgeValidationError("evidence_refs must not be empty")
        evidence_refs = tuple(
            sorted(
                (
                    EvidenceAnchor.from_dict(
                        require_mapping(item, f"evidence_refs[{index}]")
                    )
                    for index, item in enumerate(raw_evidence)
                ),
                key=lambda item: canonical_json(item.to_dict()),
            )
        )
        if len({canonical_json(item.to_dict()) for item in evidence_refs}) != len(
            evidence_refs
        ):
            raise KnowledgeValidationError("duplicate evidence reference")
        conclusion = json_object(payload["conclusion"], "conclusion")
        if not conclusion:
            raise KnowledgeValidationError("conclusion must not be empty")

        return cls(
            knowledge_id=require_identifier(payload["knowledge_id"], "knowledge_id"),
            version=version,
            kind=require_choice(payload["kind"], "kind", KNOWLEDGE_KINDS),
            scope=scope,
            applicability=Applicability.from_dict(
                require_mapping(payload["applicability"], "applicability"),
                scope=scope,
            ),
            antecedents=json_objects(payload["antecedents"], "antecedents"),
            conclusion=conclusion,
            effect=require_choice(payload["effect"], "effect", KNOWLEDGE_EFFECTS),
            evidence_refs=evidence_refs,
            supporting_projects=identifier_values(
                payload["supporting_projects"],
                "supporting_projects",
            ),
            counterexamples=json_values(
                payload["counterexamples"],
                "counterexamples",
            ),
            generalization_rationale=require_string(
                payload["generalization_rationale"],
                "generalization_rationale",
                allow_empty=True,
            ),
            regression_tests=string_values(
                payload["regression_tests"],
                "regression_tests",
            ),
            authority=require_choice(
                payload["authority"],
                "authority",
                KNOWLEDGE_AUTHORITIES,
            ),
            confidence=confidence,
            state=require_choice(payload["state"], "state", KNOWLEDGE_STATES),
            supersedes=identifier_values(payload["supersedes"], "supersedes"),
            created_by=require_choice(
                payload["created_by"],
                "created_by",
                KNOWLEDGE_CREATORS,
            ),
        )

    def to_dict(self) -> dict[str, object]:
        payload = {
            "knowledge_id": self.knowledge_id,
            "version": self.version,
            "kind": self.kind,
            "scope": self.scope,
            "applicability": self.applicability.to_dict(),
            "antecedents": [dict(item) for item in self.antecedents],
            "conclusion": dict(self.conclusion),
            "effect": self.effect,
            "evidence_refs": [item.to_dict() for item in self.evidence_refs],
            "supporting_projects": list(self.supporting_projects),
            "counterexamples": list(self.counterexamples),
            "generalization_rationale": self.generalization_rationale,
            "regression_tests": list(self.regression_tests),
            "authority": self.authority,
            "confidence": self.confidence,
            "state": self.state,
            "supersedes": list(self.supersedes),
            "created_by": self.created_by,
        }
        return json_object(payload, "knowledge")
