"""Deterministic interpretation of project-policy triage records."""

from __future__ import annotations

from typing import Sequence

from .models import RepairAdvice
from .utils import unique_ordered
from .workspace import normalize_new_files


PROJECT_POLICY_TRIAGE_TRIGGERS = frozenset(
    {
        "test_harness_ownership",
        "test_edit_attempt",
        "artifact_policy_boundary",
        "generated_test_oracle_conflict",
    }
)


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
