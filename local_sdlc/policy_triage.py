"""Deterministic interpretation of project-policy triage records."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Sequence

from .models import RepairAdvice
from .utils import truncate_text, unique_ordered
from .workspace import normalize_new_files, resolve_project_path


PROJECT_POLICY_TRIAGE_TRIGGERS = frozenset(
    {
        "test_harness_ownership",
        "test_edit_attempt",
        "artifact_policy_boundary",
        "generated_test_oracle_conflict",
    }
)


JUDGE_OWNERSHIP_VALUES = frozenset(
    {"test_harness", "product_bug", "spec_conflict", "insufficient_context", "not_applicable"}
)


def patch_plan_requests_generated_test_oracle_triage(document: str) -> bool:
    """Accept only an explicit, unambiguous planner escalation pair."""
    patch_types = re.findall(
        r"(?mi)^\s*-\s*patch_type\s*:\s*([a-z_]+)\s*$",
        document,
    )
    escalations = re.findall(
        r"(?mi)^\s*-\s*escalation\s*:\s*([a-z_]+)\s*$",
        document,
    )
    return patch_types == ["missing_context"] and escalations == [
        "generated_test_oracle_triage"
    ]


def judge_ownership_classification(document: str) -> str:
    """Extract the judge's explicit ownership vote without interpreting prose."""
    match = re.search(
        r"(?mi)^\s*(?:[-*]\s*)?OWNERSHIP\s*:\s*([a-z_]+)\s*$",
        document,
    )
    if not match:
        return "not_applicable"
    value = match.group(1).lower()
    return value if value in JUDGE_OWNERSHIP_VALUES else "not_applicable"


def generated_test_receiver_identity_facts(source: str, path: str) -> list[str]:
    """Extract only mechanically certain fresh-receiver facts from Python tests."""

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    facts: list[str] = []
    for function in (
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test")
    ):
        fresh_calls: dict[tuple[str, str], list[int]] = {}
        for node in ast.walk(function):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            receiver = node.func.value
            if not isinstance(receiver, ast.Call):
                continue
            constructor = receiver.func
            if isinstance(constructor, ast.Name):
                constructor_name = constructor.id
            elif isinstance(constructor, ast.Attribute):
                constructor_name = constructor.attr
            else:
                continue
            if not constructor_name[:1].isupper():
                continue
            fresh_calls.setdefault((constructor_name, node.func.attr), []).append(node.lineno)
        for (constructor_name, method_name), lines in sorted(fresh_calls.items()):
            if len(lines) < 2:
                continue
            facts.append(
                f"{path}:{function.name} calls {constructor_name}(...).{method_name}(...) "
                f"on {len(lines)} distinct fresh constructor expressions at lines "
                f"{', '.join(str(line) for line in lines)}; these receivers are not the same instance."
            )
    return facts


def generated_test_oracle_evidence_document(
    project: Path,
    spec: str,
    test_paths: Sequence[str],
    command_docs: Sequence[tuple[str, str]],
    prior_judge_document: str = "",
    *,
    max_test_chars: int = 16000,
    max_spec_chars: int = 24000,
) -> str:
    """Build neutral primary evidence for generated-test ownership triage.

    Deliberately exclude repair advice and prior failure-analysis conclusions.
    Those documents are hypotheses under review and caused circular ownership
    decisions when they were presented as evidence.
    """
    normalized_tests = [
        path for path in normalize_new_files(test_paths) if path.startswith("tests/")
    ]
    sections = [
        "## Ownership Facts",
        "",
        "- SPEC.md is fixed external policy.",
        "- acceptance_tests/ is fixed read-only evidence.",
        "- The paths below are machine-verified stage-owned generated tests.",
        "- A generated assertion is provisional until its setup, action, and expected proposition agree with SPEC.md.",
        "- Repair advice and prior failure-analysis conclusions are intentionally excluded from this evidence packet.",
        "- stage_owned_generated_tests: " + (", ".join(normalized_tests) or "(none)"),
        "",
        "## Fixed Specification",
        "",
        truncate_text(spec, max_spec_chars),
    ]
    for path in normalized_tests:
        source_path = resolve_project_path(project, path)
        try:
            source = source_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            source = "(unavailable)"
        identity_facts = generated_test_receiver_identity_facts(source, path)
        sections.extend(
            [
                "",
                f"## Mechanical Receiver Identity Facts: {path}",
                "",
                *(f"- {fact}" for fact in identity_facts),
                *(["- (none)"] if not identity_facts else []),
                "",
                f"## Generated Test Source: {path}",
                "",
                truncate_text(source, max_test_chars),
            ]
        )
    sections.extend(["", "## Executable Command Evidence", ""])
    sections.extend(truncate_text(document, 12000) for _name, document in command_docs)
    if prior_judge_document:
        sections.extend(
            [
                "",
                "## Independent Prior Judge Vote (advisory)",
                "",
                truncate_text(prior_judge_document, 12000),
            ]
        )
    return "\n".join(sections)


def validate_project_policy_triage_proposition(
    record: dict[str, object],
) -> dict[str, object]:
    """Fail closed when a generated-oracle verdict lacks positive evidence."""
    normalized = dict(record)
    if str(normalized.get("trigger", "")) != "generated_test_oracle_conflict":
        return normalized
    case_type = str(normalized.get("case_type", ""))
    selected = str(normalized.get("selected_hypothesis", ""))
    product_evidence = normalized.get("product_violation_evidence", [])
    test_evidence = normalized.get("test_contradiction_evidence", [])
    product_items = [item for item in product_evidence if isinstance(item, str) and item.strip()] if isinstance(product_evidence, list) else []
    test_items = [item for item in test_evidence if isinstance(item, str) and item.strip()] if isinstance(test_evidence, list) else []
    valid = True
    reason = ""
    if case_type == "product_bug" and (selected != "H_product" or not product_items):
        valid = False
        reason = "product_bug requires selected_hypothesis=H_product and positive product_violation_evidence"
    elif case_type == "test_harness" and (selected != "H_test" or not test_items):
        valid = False
        reason = "test_harness requires selected_hypothesis=H_test and positive test_contradiction_evidence"
    if valid:
        normalized["proposition_gate"] = {"status": "pass"}
        return normalized
    normalized.update(
        {
            "case_type": "insufficient_context",
            "confidence": "low",
            "safe_next_action": "reject",
            "editable_paths": [],
            "proposition_gate": {"status": "reject", "reason": reason},
            "rationale": reason,
        }
    )
    return normalized


def project_policy_triage_enabled(mode: str, trigger: str) -> bool:
    if mode == "never":
        return False
    if mode == "always":
        return True
    return trigger in PROJECT_POLICY_TRIAGE_TRIGGERS


def triage_string_list(record: dict[str, object] | None, key: str) -> list[str]:
    if not record:
        return []
    raw = record.get(key, [])
    if not isinstance(raw, list):
        return []
    return normalize_new_files(str(item) for item in raw if isinstance(item, str))


def triage_allows_test_harness_edit(record: dict[str, object] | None) -> bool:
    if not record:
        return False
    return (
        str(record.get("case_type", "")).strip() == "test_harness"
        and str(record.get("safe_next_action", "")).strip() == "edit_test_harness"
        and str(record.get("confidence", "")).strip() != "low"
    )


def authorized_test_edit_paths(records: Sequence[dict[str, object]]) -> list[str]:
    paths: list[str] = []
    for record in records:
        if triage_allows_test_harness_edit(record):
            paths.extend(triage_string_list(record, "editable_paths"))
    return unique_ordered(path for path in paths if path.startswith("tests/"))


def generated_test_oracle_triage_needed(
    records: Sequence[dict[str, object]],
    failure_signature: str | None,
) -> bool:
    """Re-triage only when the concrete executable counterexample changed."""
    if not failure_signature:
        return not any(
            str(record.get("trigger", "")) == "generated_test_oracle_conflict"
            for record in records
        )
    return not any(
        str(record.get("trigger", "")) == "generated_test_oracle_conflict"
        and str(record.get("failure_signature", "")) == failure_signature
        for record in records
    )


def enforce_test_harness_triage_gate(
    record: dict[str, object],
    stage_owned_test_paths: Sequence[str],
) -> dict[str, object]:
    """Limit advisory LLM decisions to machine-owned generated test paths."""
    normalized = dict(record)
    if not triage_allows_test_harness_edit(normalized):
        return normalized
    owned = {
        path
        for path in normalize_new_files(stage_owned_test_paths)
        if path.startswith("tests/")
    }
    requested = triage_string_list(normalized, "editable_paths")
    approved = [path for path in requested if path in owned]
    rejected = [path for path in requested if path not in owned]
    normalized["editable_paths"] = approved
    normalized["action_gate"] = {
        "stage_owned_test_paths": sorted(owned),
        "approved_editable_paths": approved,
        "rejected_editable_paths": rejected,
    }
    if approved:
        return normalized
    normalized["safe_next_action"] = "reject"
    normalized["confidence"] = "low"
    normalized["rationale"] = (
        "Test edit denied because no requested path is a machine-verified stage-owned test harness."
    )
    return normalized


def apply_project_policy_triage_to_advice(
    advice: RepairAdvice,
    triage: dict[str, object] | None,
    existing_project_paths: Sequence[str],
    test_harness_write_strategies: Sequence[str],
) -> RepairAdvice:
    if advice.strategy not in test_harness_write_strategies or not triage:
        return advice
    editable_paths = triage_string_list(triage, "editable_paths")
    readonly_paths = triage_string_list(triage, "readonly_paths")
    forbidden_actions = [
        str(item)
        for item in triage.get("forbidden_actions", [])
        if isinstance(item, str)
    ] if isinstance(triage.get("forbidden_actions", []), list) else []
    if triage_allows_test_harness_edit(triage):
        return RepairAdvice(
            strategy=advice.strategy,
            focus_files=tuple(unique_ordered([*editable_paths, *advice.focus_files, *readonly_paths])),
            instructions=tuple(
                unique_ordered(
                    [
                        *advice.instructions,
                        "Project-policy triage classified the relevant generated test harness as writable for this repair.",
                        *[f"Forbidden by project-policy triage: {item}" for item in forbidden_actions],
                    ]
                )
            ),
            evidence=tuple(
                unique_ordered(
                    [
                        *advice.evidence,
                        f"project_policy_triage={triage.get('case_type')}:{triage.get('safe_next_action')}",
                    ]
                )
            ),
        )
    product_focus = [
        path
        for path in [*advice.focus_files, *readonly_paths]
        if path in existing_project_paths and not path.startswith("tests/")
    ]
    return RepairAdvice(
        strategy="root_cause_patch",
        focus_files=tuple(unique_ordered(product_focus)),
        instructions=tuple(
            unique_ordered(
                [
                    "Project-policy triage did not authorize editing tests; treat tests as read-only evidence.",
                    "Repair product code or request missing context instead of changing the test harness.",
                    *[f"Forbidden by project-policy triage: {item}" for item in forbidden_actions],
                ]
            )
        ),
        evidence=tuple(
            unique_ordered(
                [
                    *advice.evidence,
                    f"project_policy_triage={triage.get('case_type')}:{triage.get('safe_next_action')}",
                ]
            )
        ),
    )
