"""Machine-readable output contracts for isolated candidate-mining calls."""

from __future__ import annotations

from typing import Mapping

from .knowledge_schema import KNOWLEDGE_EFFECTS, KNOWLEDGE_KINDS


MAX_CANDIDATE_RESPONSE_BYTES = 64 * 1024
MAX_ANTECEDENTS = 5
MAX_COUNTEREXAMPLES = 8
MAX_REGRESSION_TESTS = 8
MAX_ANTECEDENT_BYTES = 512
MAX_CONCLUSION_BYTES = 1024
MAX_RATIONALE_BYTES = 1200
MAX_LABEL_BYTES = 160

ABSTRACTION_FIELDS = {
    "schema_version",
    "kind",
    "antecedents",
    "conclusion",
    "effect",
    "generalization_rationale",
    "counterexamples",
    "regression_tests",
    "confidence",
    "source_episode_ids",
}
SCOPE_FIELDS = {"schema_version", "scope", "applicability", "source_episode_ids"}
SERIALIZATION_FIELDS = ABSTRACTION_FIELDS | {"scope", "applicability"}


def abstraction_output_contract(source_episode_ids: tuple[str, ...]) -> dict[str, object]:
    """Return the bounded contract shown to the abstraction call."""

    return {
        "required_fields": sorted(ABSTRACTION_FIELDS),
        "constraints": {
            "schema_version": {"const": 1},
            "kind": {"enum": sorted(KNOWLEDGE_KINDS)},
            "antecedents": {
                "type": "array_of_objects",
                "min_items": 1,
                "max_items": MAX_ANTECEDENTS,
                "max_item_bytes": MAX_ANTECEDENT_BYTES,
            },
            "conclusion": {
                "type": "non_empty_object",
                "max_bytes": MAX_CONCLUSION_BYTES,
            },
            "effect": {"enum": sorted(KNOWLEDGE_EFFECTS)},
            "generalization_rationale": {
                "type": "short_string",
                "max_bytes": MAX_RATIONALE_BYTES,
            },
            "counterexamples": {"type": "array", "max_items": MAX_COUNTEREXAMPLES},
            "regression_tests": {
                "type": "array_of_short_strings",
                "max_items": MAX_REGRESSION_TESTS,
                "max_item_bytes": MAX_LABEL_BYTES,
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "source_episode_ids": {"const": list(source_episode_ids)},
        },
        "additional_fields": False,
    }


def scope_output_contract(
    source_episode_ids: tuple[str, ...],
    scope_options: list[dict[str, object]],
) -> dict[str, object]:
    """Return the only complete scope/applicability pairs the LLM may select."""

    allowed_pairs = [
        {
            "scope": str(option["scope"]),
            "applicability": {
                "operator": "all",
                "predicates": [dict(predicate)],
            },
        }
        for option in scope_options
        for predicate in option["predicate_options"]
    ]
    return {
        "required_fields": sorted(SCOPE_FIELDS),
        "constraints": {
            "schema_version": {"const": 1},
            "scope": {"enum": [str(item["scope"]) for item in scope_options]},
            "applicability": {
                "selection_rule": (
                    "copy one complete scope and applicability pair exactly from "
                    "allowed_scope_applicability_pairs"
                ),
                "allowed_scope_applicability_pairs": allowed_pairs,
            },
            "source_episode_ids": {"const": list(source_episode_ids)},
        },
        "additional_fields": False,
    }


def serialization_output_contract(
    abstraction: Mapping[str, object],
    scope: Mapping[str, object],
) -> dict[str, object]:
    """Return the flatten-only contract for the final non-reasoning call."""

    expected = dict(abstraction)
    expected["scope"] = scope["scope"]
    expected["applicability"] = scope["applicability"]
    return {
        "required_fields": sorted(SERIALIZATION_FIELDS),
        "copy_rule": (
            "copy every value exactly from abstraction and scope_classification; "
            "do not infer, summarize, rename, or add fields"
        ),
        "expected_output": expected,
        "additional_fields": False,
    }
