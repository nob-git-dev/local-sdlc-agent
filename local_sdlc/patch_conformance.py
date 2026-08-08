"""Validation helpers for independent patch-plan conformance reviews."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Sequence

from .models import RunnerError
from .utils import strip_markdown_fence, truncate_text


VALID_REVIEW_STATUSES = {"pass", "fail", "insufficient_context"}
VALID_OBLIGATION_STATUSES = {"satisfied", "not_satisfied", "uncertain"}
VALID_NEXT_ACTIONS = {"apply", "repair_artifact", "collect_context"}


def patch_plan_policy_contradictions(document: str) -> list[str]:
    """Find plans that prescribe mutation of their own evidence-only paths."""

    def field(name: str) -> str:
        match = re.search(rf"(?im)^\s*-\s*{name}\s*:\s*(.*?)\s*$", document)
        return match.group(1).strip() if match else ""

    if field("escalation").lower() not in {"", "none"}:
        return []
    required_field = field("required_paths") or field("required_path")
    required = [item.strip().rstrip("/") for item in required_field.split(",")]
    protected = [
        item.strip().rstrip("/")
        for name in ("readonly_paths", "forbidden_paths")
        for item in field(name).split(",")
        if item.strip() and item.strip().lower() != "(none)"
    ]
    required = [item for item in required if item and item.lower() != "(none)"]

    def protected_by(path: str) -> str | None:
        return next(
            (owner for owner in protected if path == owner or path.startswith(owner + "/")),
            None,
        )

    conflicts = [
        f"required path `{path}` is protected by `{owner}`"
        for path in required
        if (owner := protected_by(path)) is not None
    ]
    mutation_verbs = re.compile(
        r"(?i)\b(?:add|adapt|change|convert|edit|modify|move|relocate|remove|replace|rewrite|update)\b"
    )
    path_pattern = re.compile(r"(?<![\w.-])(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+")
    for name in ("proposition", "minimal_patch_goal"):
        value = field(name)
        if not value or not mutation_verbs.search(value):
            continue
        for match in path_pattern.finditer(value):
            path = match.group(0)
            prefix = value[max(0, match.start() - 48) : match.start()].lower()
            if re.search(r"(?:do not|never|without)\s+(?:\w+\s+){0,2}$", prefix):
                continue
            owner = protected_by(path)
            if owner is not None:
                conflicts.append(
                    f"{name} prescribes mutation of `{path}`, protected by `{owner}`"
                )
    return list(dict.fromkeys(conflicts))


def _is_post_apply_verification_requirement(text: str) -> bool:
    """Return whether an item belongs to the runner's post-apply command gate."""

    normalized = " ".join(text.lower().split())
    verification_terms = (
        "test",
        "suite",
        "command",
        "exit",
        "pass",
        "executable evidence",
    )
    if not any(term in normalized for term in verification_terms):
        return False
    return normalized.startswith("stop when") or "executable evidence" in normalized


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
    deferred_verification: list[str] = []
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
        if _is_post_apply_verification_requirement(requirement):
            item["status"] = "satisfied"
            item["candidate_evidence"] = "deferred to the runner post-apply command gate"
            item["counterexample"] = "(none)"
            deferred_verification.append(requirement)
        obligations.append(item)
        if item["status"] != "satisfied":
            unresolved.append(requirement)
        elif item["candidate_evidence"] == "(none)":
            unresolved.append(requirement)

    missing_obligations = [
        item
        for item in _string_list(payload.get("missing_obligations"))
        if not _is_post_apply_verification_requirement(item)
    ]
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
    elif status == "fail" and deferred_verification and not (
        unresolved or missing_obligations or missing_context_paths
    ):
        status = "pass"
        next_action = "apply"
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
        "deferred_verification_obligations": deferred_verification,
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
