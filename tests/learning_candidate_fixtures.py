"""Reusable candidate-mining fixtures with no project source content."""

from __future__ import annotations

import json


def eligible_episode(
    episode_id: str = "EP-case-001",
    *,
    project_fingerprint: str = "project-a",
    structural_signature: str = "ES-shared-recovery",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "episode_id": episode_id,
        "episode_kind": "stalled_recovery",
        "source_run_id": f"run-{episode_id.lower()}",
        "project_fingerprint": project_fingerprint,
        "eligibility": "eligible",
        "reason_codes": [],
        "structural_signature": structural_signature,
        "context": {
            "failure_type": "test_assertion_failed",
            "failure_subject_count": 1,
            "repeat_class": "repeated",
            "plateau_detected": True,
        },
        "hypothesis": {
            "analysis_available": True,
            "strategy_class": "analytic",
        },
        "intervention": {
            "strategy": "root_cause_recovery",
            "change_isolation": "isolated",
        },
        "change": {
            "count": 1,
            "atomic": True,
            "files": [{"id": "file_1", "suffix": ".py"}],
        },
        "outcome": {
            "status": "completed",
            "verdict": "approved",
            "verified": True,
        },
        "causal_graph": {
            "complete": True,
            "nodes": [],
            "edges": [],
        },
        "evidence": [],
        "provenance": {
            "source_event_ids": [f"event-{episode_id.lower()}"],
            "source_event_hashes": ["a" * 64],
        },
    }


def abstraction_payload(*episode_ids: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "heuristic",
        "antecedents": [{"fact": "verified_repeated_failure"}],
        "conclusion": {"recommendation": "inspect_boundary_contract"},
        "effect": "recommend",
        "generalization_rationale": "The proposition uses observed structure, not names.",
        "counterexamples": [],
        "regression_tests": ["renamed-structure"],
        "confidence": 0.7,
        "source_episode_ids": list(episode_ids),
    }


def scope_payload(scope: str, anchor: str, *episode_ids: str) -> dict[str, object]:
    if scope == "case":
        predicates = [{"type": "episode_is", "episode_id": anchor}]
    elif scope == "project":
        predicates = [{"type": "project_is", "project_fingerprint": anchor}]
    elif scope == "structural":
        predicates = [{"type": "structural_signature_is", "signature": anchor}]
    else:
        raise ValueError(f"unsupported fixture scope: {scope}")
    return {
        "schema_version": 1,
        "scope": scope,
        "applicability": {"operator": "all", "predicates": predicates},
        "source_episode_ids": list(episode_ids),
    }


def serialization_payload(
    abstraction: dict[str, object],
    scope: dict[str, object],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": abstraction["kind"],
        "scope": scope["scope"],
        "applicability": scope["applicability"],
        "antecedents": abstraction["antecedents"],
        "conclusion": abstraction["conclusion"],
        "effect": abstraction["effect"],
        "generalization_rationale": abstraction["generalization_rationale"],
        "counterexamples": abstraction["counterexamples"],
        "regression_tests": abstraction["regression_tests"],
        "confidence": abstraction["confidence"],
        "source_episode_ids": abstraction["source_episode_ids"],
    }


def valid_case_responses(episode_id: str = "EP-case-001") -> dict[str, object]:
    abstraction = abstraction_payload(episode_id)
    scope = scope_payload("case", episode_id, episode_id)
    return {
        "candidate_abstraction": abstraction,
        "scope_classification": scope,
        "candidate_serialization": serialization_payload(abstraction, scope),
    }


class ScriptedCandidateLLM:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def complete(
        self,
        function_name: str,
        system_prompt: str,
        document: dict[str, object],
    ) -> str:
        self.calls.append(
            {
                "function_name": function_name,
                "system_prompt": system_prompt,
                "document": document,
            }
        )
        response = self.responses[function_name]
        if isinstance(response, Exception):
            raise response
        return response if isinstance(response, str) else json.dumps(response)
