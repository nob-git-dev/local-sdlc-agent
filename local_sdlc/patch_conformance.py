"""Validation helpers for independent patch-plan conformance reviews."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Sequence

from .models import RunnerError
from .utils import strip_markdown_fence, truncate_text


VALID_REVIEW_STATUSES = {"pass", "fail", "insufficient_context"}
VALID_OBLIGATION_STATUSES = {"satisfied", "not_satisfied", "uncertain"}
VALID_NEXT_ACTIONS = {"apply", "repair_artifact", "collect_context"}


def patch_plan_signature(document: str) -> str:
    """Return a stable identity for one binding plan."""

    normalized = "\n".join(line.rstrip() for line in document.strip().splitlines())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def repeated_patch_conformance_failure(
    reviews: Sequence[dict[str, object]],
) -> bool:
    """Detect two consecutive candidates failing the same plan obligations."""

    if len(reviews) < 2:
        return False
    previous, current = reviews[-2:]
    if str(previous.get("status")) != "fail" or str(current.get("status")) != "fail":
        return False
    previous_missing = tuple(_string_list(previous.get("missing_obligations")))
    current_missing = tuple(_string_list(current.get("missing_obligations")))
    return bool(
        previous.get("patch_plan_signature")
        and previous.get("patch_plan_signature") == current.get("patch_plan_signature")
        and previous_missing
        and previous_missing == current_missing
    )


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if isinstance(item, str) and item.strip()]


def parse_patch_conformance_review(document: str) -> dict[str, object]:
    """Parse and fail-close a patch-conformance review.

    The LLM is advisory. A malformed review, a pass without obligation-level
    evidence, or a pass containing an unresolved obligation is never allowed
    to authorize artifact application.
    """

    raw = strip_markdown_fence(document.strip())
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RunnerError(f"patch conformance review is not valid JSON: {exc}") from None
    if not isinstance(payload, dict):
        raise RunnerError("patch conformance review must be one JSON object")

    status = str(payload.get("status") or "").strip().lower()
    if status not in VALID_REVIEW_STATUSES:
        raise RunnerError(f"patch conformance review has invalid status: {status or '(missing)'}")

    raw_obligations = payload.get("obligations")
    if not isinstance(raw_obligations, list) or not raw_obligations:
        raise RunnerError("patch conformance review must include at least one obligation")

    obligations: list[dict[str, str]] = []
    unresolved: list[str] = []
    for index, raw_item in enumerate(raw_obligations, start=1):
        if not isinstance(raw_item, dict):
            raise RunnerError("patch conformance obligation must be an object")
        obligation_status = str(raw_item.get("status") or "").strip().lower()
        if obligation_status not in VALID_OBLIGATION_STATUSES:
            raise RunnerError(
                "patch conformance obligation has invalid status: "
                + (obligation_status or "(missing)")
            )
        requirement = str(raw_item.get("requirement") or "").strip()
        if not requirement:
            raise RunnerError("patch conformance obligation is missing requirement")
        candidate_evidence = str(raw_item.get("candidate_evidence") or "").strip()
        counterexample = str(raw_item.get("counterexample") or "").strip()
        item = {
            "id": str(raw_item.get("id") or f"O{index}"),
            "requirement": requirement,
            "status": obligation_status,
            "candidate_evidence": candidate_evidence or "(none)",
            "counterexample": counterexample or "(none)",
        }
        obligations.append(item)
        if obligation_status != "satisfied":
            unresolved.append(requirement)
        elif not candidate_evidence or candidate_evidence == "(none)":
            unresolved.append(requirement)

    missing_obligations = _string_list(payload.get("missing_obligations"))
    missing_context_paths = _string_list(payload.get("missing_context_paths"))
    next_action = str(payload.get("safe_next_action") or "").strip().lower()
    if next_action not in VALID_NEXT_ACTIONS:
        raise RunnerError(
            "patch conformance review has invalid safe_next_action: "
            + (next_action or "(missing)")
        )

    # Fail closed when the summary contradicts obligation-level evidence.
    if status == "pass" and (unresolved or missing_obligations or missing_context_paths):
        status = "fail"
        missing_obligations = list(dict.fromkeys([*missing_obligations, *unresolved]))
        next_action = "repair_artifact"
    elif status == "pass" and next_action != "apply":
        status = "fail"
        missing_obligations = ["review summary did not authorize apply"]
        next_action = "repair_artifact"
    elif status == "fail":
        missing_obligations = list(dict.fromkeys([*missing_obligations, *unresolved]))
        if not missing_obligations:
            raise RunnerError("failed patch conformance review must name a missing obligation")
        next_action = "repair_artifact"
    elif status == "insufficient_context":
        if not missing_context_paths:
            raise RunnerError(
                "insufficient-context patch conformance review must name missing_context_paths"
            )
        next_action = "collect_context"

    return {
        "status": status,
        "obligations": obligations,
        "missing_obligations": missing_obligations,
        "missing_context_paths": missing_context_paths,
        "safe_next_action": next_action,
        "repair_instruction": truncate_text(
            str(payload.get("repair_instruction") or "").strip(),
            1200,
        ),
    }


def failed_patch_conformance_review(reason: str) -> dict[str, object]:
    """Return a deterministic fail-closed record for an invalid review."""

    concise = truncate_text(reason.strip() or "review unavailable", 1200)
    return {
        "status": "fail",
        "obligations": [
            {
                "id": "O1",
                "requirement": "independent conformance evidence must be valid",
                "status": "uncertain",
                "candidate_evidence": "(none)",
                "counterexample": concise,
            }
        ],
        "missing_obligations": ["valid independent patch-plan conformance evidence"],
        "missing_context_paths": [],
        "safe_next_action": "repair_artifact",
        "repair_instruction": "Regenerate one artifact that explicitly satisfies every binding patch-plan obligation.",
        "review_error": concise,
    }
