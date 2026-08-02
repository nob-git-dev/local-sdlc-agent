"""Strict LLM document contracts for candidate mining."""

from __future__ import annotations

from dataclasses import dataclass
from functools import wraps
import json
from typing import Mapping

from sdlc_events import canonical_json

from .candidate_contracts import (
    ABSTRACTION_FIELDS,
    MAX_ANTECEDENT_BYTES,
    MAX_ANTECEDENTS,
    MAX_CANDIDATE_RESPONSE_BYTES,
    MAX_CONCLUSION_BYTES,
    MAX_COUNTEREXAMPLES,
    MAX_LABEL_BYTES,
    MAX_RATIONALE_BYTES,
    MAX_REGRESSION_TESTS,
    SCOPE_FIELDS,
    SERIALIZATION_FIELDS,
)
from .knowledge_predicates import Applicability
from .knowledge_schema import KNOWLEDGE_EFFECTS, KNOWLEDGE_KINDS
from .privacy import sensitive_values
from .schema_validation import (
    KnowledgeValidationError,
    identifier_values,
    json_object,
    json_objects,
    json_values,
    require_choice,
    require_mapping,
    require_sequence,
    require_string,
    string_values,
)


class CandidateProtocolError(KnowledgeValidationError):
    """Raised when an LLM candidate document violates its bounded contract."""


def _candidate_errors(function):
    """Expose every nested schema failure through the LLM protocol boundary."""

    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except CandidateProtocolError:
            raise
        except KnowledgeValidationError as exc:
            raise CandidateProtocolError(str(exc)) from exc

    return wrapped


def _bounded_json(value: object, field: str, limit: int) -> None:
    if len(canonical_json(value).encode("utf-8")) > limit:
        raise CandidateProtocolError(f"{field} exceeds {limit} bytes")


def _exact_fields(
    payload: Mapping[str, object],
    required: set[str],
    label: str,
) -> None:
    missing = required - set(payload)
    unknown = set(payload) - required
    if missing:
        raise CandidateProtocolError(
            f"missing {label} fields: " + ", ".join(sorted(missing))
        )
    if unknown:
        raise CandidateProtocolError(
            f"unknown {label} fields: " + ", ".join(sorted(unknown))
        )
    if payload.get("schema_version") != 1:
        raise CandidateProtocolError(f"unsupported {label} schema_version")
    unsafe = sensitive_values(payload)
    if unsafe:
        raise CandidateProtocolError(
            f"{label} contains sensitive values: " + ", ".join(unsafe)
        )


def parse_candidate_json(text: str, label: str) -> dict[str, object]:
    if not isinstance(text, str):
        raise CandidateProtocolError(f"{label} response must be text")
    if len(text.encode("utf-8")) > MAX_CANDIDATE_RESPONSE_BYTES:
        raise CandidateProtocolError(f"{label} response is too large")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CandidateProtocolError(f"{label} response is not strict JSON") from exc
    if not isinstance(payload, dict):
        raise CandidateProtocolError(f"{label} response must be an object")
    return payload


@dataclass(frozen=True)
class CandidateAbstraction:
    kind: str
    antecedents: tuple[dict[str, object], ...]
    conclusion: dict[str, object]
    effect: str
    generalization_rationale: str
    counterexamples: tuple[object, ...]
    regression_tests: tuple[str, ...]
    confidence: float
    source_episode_ids: tuple[str, ...]

    @classmethod
    @_candidate_errors
    def from_dict(cls, payload: Mapping[str, object]) -> "CandidateAbstraction":
        _exact_fields(payload, ABSTRACTION_FIELDS, "candidate abstraction")
        antecedents = json_objects(payload["antecedents"], "antecedents")
        if not antecedents:
            raise CandidateProtocolError("antecedents must not be empty")
        if len(antecedents) > MAX_ANTECEDENTS:
            raise CandidateProtocolError(f"antecedents must contain at most {MAX_ANTECEDENTS} items")
        for index, antecedent in enumerate(antecedents):
            _bounded_json(antecedent, f"antecedents[{index}]", MAX_ANTECEDENT_BYTES)

        conclusion = json_object(payload["conclusion"], "conclusion")
        if not conclusion:
            raise CandidateProtocolError("conclusion must not be empty")
        _bounded_json(conclusion, "conclusion", MAX_CONCLUSION_BYTES)

        rationale = require_string(
            payload["generalization_rationale"],
            "generalization_rationale",
        )
        if len(rationale.encode("utf-8")) > MAX_RATIONALE_BYTES:
            raise CandidateProtocolError("generalization_rationale is too long")

        counterexamples = json_values(payload["counterexamples"], "counterexamples")
        if len(counterexamples) > MAX_COUNTEREXAMPLES:
            raise CandidateProtocolError("counterexamples contains too many items")
        regression_tests = string_values(payload["regression_tests"], "regression_tests")
        if len(regression_tests) > MAX_REGRESSION_TESTS:
            raise CandidateProtocolError("regression_tests contains too many items")
        for label in regression_tests:
            if len(label.encode("utf-8")) > MAX_LABEL_BYTES:
                raise CandidateProtocolError("regression test label is too long")

        raw_confidence = payload["confidence"]
        if isinstance(raw_confidence, bool) or not isinstance(raw_confidence, (int, float)):
            raise CandidateProtocolError("confidence must be a number")
        confidence = float(raw_confidence)
        if not 0.0 <= confidence <= 1.0:
            raise CandidateProtocolError("confidence must be between 0 and 1")
        source_ids = identifier_values(payload["source_episode_ids"], "source_episode_ids")
        if not source_ids:
            raise CandidateProtocolError("source_episode_ids must not be empty")

        return cls(
            kind=require_choice(payload["kind"], "kind", KNOWLEDGE_KINDS),
            antecedents=antecedents,
            conclusion=conclusion,
            effect=require_choice(payload["effect"], "effect", KNOWLEDGE_EFFECTS),
            generalization_rationale=rationale,
            counterexamples=counterexamples,
            regression_tests=regression_tests,
            confidence=confidence,
            source_episode_ids=source_ids,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "kind": self.kind,
            "antecedents": [dict(item) for item in self.antecedents],
            "conclusion": dict(self.conclusion),
            "effect": self.effect,
            "generalization_rationale": self.generalization_rationale,
            "counterexamples": list(self.counterexamples),
            "regression_tests": list(self.regression_tests),
            "confidence": self.confidence,
            "source_episode_ids": list(self.source_episode_ids),
        }


@dataclass(frozen=True)
class CandidateScope:
    scope: str
    applicability: Applicability
    source_episode_ids: tuple[str, ...]

    @classmethod
    @_candidate_errors
    def from_dict(cls, payload: Mapping[str, object]) -> "CandidateScope":
        _exact_fields(payload, SCOPE_FIELDS, "scope classification")
        scope = require_choice(
            payload["scope"],
            "scope",
            {"structural", "technology", "project", "case"},
        )
        source_ids = identifier_values(payload["source_episode_ids"], "source_episode_ids")
        if not source_ids:
            raise CandidateProtocolError("source_episode_ids must not be empty")
        return cls(
            scope=scope,
            applicability=Applicability.from_dict(
                require_mapping(payload["applicability"], "applicability"),
                scope=scope,
            ),
            source_episode_ids=source_ids,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "scope": self.scope,
            "applicability": self.applicability.to_dict(),
            "source_episode_ids": list(self.source_episode_ids),
        }


@dataclass(frozen=True)
class CandidateSerialization:
    abstraction: CandidateAbstraction
    scope: CandidateScope

    @classmethod
    @_candidate_errors
    def from_dict(cls, payload: Mapping[str, object]) -> "CandidateSerialization":
        _exact_fields(payload, SERIALIZATION_FIELDS, "candidate serialization")
        abstraction_payload = {key: payload[key] for key in ABSTRACTION_FIELDS}
        scope_payload = {key: payload[key] for key in SCOPE_FIELDS}
        return cls(
            abstraction=CandidateAbstraction.from_dict(abstraction_payload),
            scope=CandidateScope.from_dict(scope_payload),
        )

    def assert_matches(
        self,
        abstraction: CandidateAbstraction,
        scope: CandidateScope | Mapping[str, object],
    ) -> None:
        expected_scope = scope if isinstance(scope, CandidateScope) else CandidateScope.from_dict(scope)
        if self.abstraction != abstraction or self.scope != expected_scope:
            raise CandidateProtocolError("candidate serialization drifted from validated documents")

    def to_dict(self) -> dict[str, object]:
        payload = self.abstraction.to_dict()
        payload.update(
            {
                "scope": self.scope.scope,
                "applicability": self.scope.applicability.to_dict(),
            }
        )
        return payload
