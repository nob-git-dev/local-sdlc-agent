"""Artifact protocol contracts and semantic evidence helpers."""

from __future__ import annotations

from collections import Counter
import json
import re
import textwrap
from pathlib import Path
from typing import Sequence

from .artifact_ops import *
from .models import *
from .python_project_analysis import *
from .utils import truncate_text, unique_ordered
from .verification import parse_command_result_document
from .workspace import normalize_new_files, normalize_project_relative_paths

def first_artifact_marker_offset(text: str) -> int:
    markers = [
        "BEGIN_FILE:",
        "BEGIN_FILE\n",
        "BEGIN_FILE\r\n",
        "BEGIN_APPEND_FILE:",
        "BEGIN_APPEND_FILE\n",
        "BEGIN_APPEND_FILE\r\n",
        "BEGIN_SEARCH_REPLACE:",
        "BEGIN_SEARCH_REPLACE",
        "diff --git ",
        "--- ",
    ]
    offsets = [text.find(marker) for marker in markers if text.find(marker) >= 0]
    offsets.extend(
        match.start()
        for match in re.finditer(
            r"(?m)^BEGIN_(?:APPEND_)?FILE[ \t]+"
            r"[A-Za-z0-9_.][A-Za-z0-9_./-]*[ \t]*$",
            text,
        )
    )
    offsets.extend(json_artifact_marker_offsets(text))
    return min(offsets) if offsets else -1

def has_artifact_signature(text: str) -> bool:
    return first_artifact_marker_offset(text) >= 0

def is_non_artifact_output(text: str, budget_bytes: int = ARTIFACT_OUTPUT_BUDGET_BYTES) -> bool:
    offset = first_artifact_marker_offset(text)
    if offset < 0:
        return len(text.encode("utf-8")) > budget_bytes
    return len(text[:offset].encode("utf-8")) > budget_bytes

def extract_missing_context_requests(text: str) -> list[MissingContextRequest]:
    requests: list[MissingContextRequest] = []
    for match in re.finditer(r"(?im)^\s*MISSING_CONTEXT\s*:?\s*(?P<body>.+)$", text):
        body = match.group("body").strip()
        paths = normalize_new_files(re.findall(r"[\w./-]+\.[A-Za-z0-9_]+", body))
        if paths:
            requests.append(MissingContextRequest(paths=tuple(paths), reason=body))
    for match in re.finditer(r"(?ims)^\s*MISSING_CONTEXT\b(?P<body>.*?)(?=^\s*(?:BEGIN_|diff --git|\{|\[)|\Z)", text):
        body = match.group("body").strip()
        paths = normalize_new_files(re.findall(r"[\w./-]+\.[A-Za-z0-9_]+", body))
        if paths:
            requests.append(MissingContextRequest(paths=tuple(paths), reason=truncate_text(body, 300)))
    for candidate in _json_candidates(text):
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        items = payload.get("artifacts") if isinstance(payload, dict) else payload
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict) or str(item.get("type", "")).strip() != "missing_context":
                continue
            raw_paths = item.get("paths") or item.get("path") or []
            if isinstance(raw_paths, str):
                raw_paths = [raw_paths]
            if not isinstance(raw_paths, list):
                continue
            paths = normalize_new_files(str(path) for path in raw_paths if isinstance(path, str))
            if paths:
                requests.append(MissingContextRequest(paths=tuple(paths), reason=str(item.get("reason") or "")))
    unique: list[MissingContextRequest] = []
    seen: set[tuple[str, ...]] = set()
    for request in requests:
        if request.paths in seen:
            continue
        seen.add(request.paths)
        unique.append(request)
    return unique

def artifact_failure_type(error: str, default: str = "artifact_invalid") -> str:
    lowered = error.lower()
    if "read-only" in lowered and "tests/" in lowered:
        return "test_edit_attempt"
    if "missing_context" in lowered:
        return "missing_context"
    if "corrupt patch" in lowered or "git apply --numstat failed" in lowered or "patch does not apply" in lowered:
        return "corrupt_unified_diff"
    return default

FAILURE_TRANSITIONS: dict[str, FailureTransition] = {
    "artifact_valid": FailureTransition("artifact_valid", "runner", "apply_then_stage_test", "runner"),
    "artifact_invalid": FailureTransition(
        "artifact_invalid",
        "format_repair",
        "rewrite_previous_output_as_valid_artifact",
        "supervisor",
        ("Do not change semantic intent.", "Fix only artifact structure."),
    ),
    "corrupt_unified_diff": FailureTransition(
        "corrupt_unified_diff",
        "format_repair",
        "replace_corrupt_diff_with_atomic_search_replace",
        "runner",
        (
            "The previous unified diff could not be parsed or applied.",
            "Do not emit another unified diff.",
            "For existing-file repairs, use exactly one BEGIN_SEARCH_REPLACE block.",
            "Use BEGIN_FILE only when the target file is missing or explicitly generated.",
        ),
    ),
    "stage_scope_violation": FailureTransition(
        "stage_scope_violation",
        "format_repair",
        "rewrite_artifacts_to_current_stage_scope",
        "runner",
        (
            "The previous artifact included future-stage or broader subsystem behavior outside the current stage goal.",
            "Rewrite the offending product or test artifact so every public API, class, assertion, helper, and comment belongs to the current stage only.",
            "Remove future-stage implementation concepts instead of broadening product scope to satisfy them.",
        ),
    ),
    "non_artifact_output": FailureTransition(
        "non_artifact_output",
        "pm_planner",
        "semantic_salvage_then_format_or_repair",
        "pm",
        ("Extract usable intent from the failed output.", "Route to format_repair or repair_coder."),
    ),
    "missing_context": FailureTransition("missing_context", "runner", "collect_context_then_retry", "runner"),
    "generated_test_oracle_triage_authorized": FailureTransition(
        "generated_test_oracle_triage_authorized",
        "repair_coder",
        "edit_authorized_generated_test_only",
        "supervisor",
        (
            "Edit only the generated test paths authorized by independent project-policy triage.",
            "Use SPEC.md and executable evidence as the oracle; do not weaken fixed acceptance tests.",
            "Keep product files and every non-authorized test path read-only.",
        ),
    ),
    "root_cause_context_refocus": FailureTransition(
        "root_cause_context_refocus",
        "root_cause_repair",
        "prioritize_requested_context_and_retry_root_cause",
        "runner",
        (
            "Place every requested existing path before the general context set.",
            "Keep executable failure evidence pinned even when newer diagnostic documents exist.",
            "Retry root-cause analysis before generating another artifact.",
        ),
    ),
    "semantic_repair_missing_context": FailureTransition(
        "semantic_repair_missing_context",
        "runner",
        "collect_semantic_focus_context_then_retry",
        "runner",
    ),
    "semantic_repair_missing_path": FailureTransition(
        "semantic_repair_missing_path",
        "format_repair",
        "rewrite_semantic_repair_as_valid_path_qualified_artifact",
        "supervisor",
        ("Preserve semantic edit intent.", "Add the missing artifact path.", "Return only one valid artifact."),
    ),
    "semantic_repair_prose_mixed": FailureTransition(
        "semantic_repair_prose_mixed",
        "format_repair",
        "strip_prose_and_emit_valid_semantic_artifact",
        "supervisor",
        ("Do not recalculate the solution.", "Remove prose/headings/fences.", "Return only one valid artifact."),
    ),
    "semantic_repair_markdown_fence": FailureTransition(
        "semantic_repair_markdown_fence",
        "format_repair",
        "remove_markdown_fence_and_emit_valid_artifact",
        "supervisor",
        ("Preserve semantic edit intent.", "Remove markdown fences.", "Return only one valid artifact."),
    ),
    "semantic_repair_malformed_search_replace": FailureTransition(
        "semantic_repair_malformed_search_replace",
        "format_repair",
        "rewrite_malformed_semantic_search_replace",
        "runner",
        (
            "Preserve semantic edit intent.",
            "After `BEGIN_SEARCH_REPLACE: path`, the next line must be `<<<<<<< SEARCH`.",
            "Do not prefix code lines with `:`.",
        ),
    ),
    "semantic_repair_multiple_artifacts": FailureTransition(
        "semantic_repair_multiple_artifacts",
        "format_repair",
        "choose_single_atomic_semantic_artifact",
        "supervisor",
        ("Preserve semantic edit intent.", "Choose the one product-code edit required by the semantic contract."),
    ),
    "semantic_repair_not_atomic": FailureTransition(
        "semantic_repair_not_atomic",
        "format_repair",
        "rewrite_as_one_atomic_semantic_artifact",
        "supervisor",
        ("Preserve semantic edit intent.", "Return one valid BEGIN_SEARCH_REPLACE with path or one one-file unified diff."),
    ),
    "semantic_repair_forbidden_artifact": FailureTransition(
        "semantic_repair_forbidden_artifact",
        "format_repair",
        "rewrite_forbidden_semantic_artifact_as_atomic_edit",
        "supervisor",
        ("Do not emit JSON, BEGIN_FILE, or BEGIN_APPEND_FILE.", "Return one valid product-code atomic artifact."),
    ),
    "semantic_repair_test_edit": FailureTransition(
        "semantic_repair_test_edit",
        "format_repair",
        "rewrite_semantic_repair_to_product_code_only",
        "runner",
        ("Tests are semantic evidence only.", "Move the repair to the product-code focus file."),
    ),
    "semantic_repair_too_large": FailureTransition(
        "semantic_repair_too_large",
        "format_repair",
        "shrink_semantic_repair_to_one_atomic_edit",
        "supervisor",
        ("Keep only the smallest product-code edit that satisfies one semantic contract.",),
    ),
    "artifact_plan_mismatch": FailureTransition(
        "artifact_plan_mismatch",
        "artifact_plan_repair",
        "rewrite_artifact_to_satisfy_binding_patch_plan",
        "supervisor",
        (
            "Do not apply the previous candidate.",
            "Keep the binding patch plan unchanged.",
            "Implement every missing behavioral obligation named by the independent conformance review.",
            "Return one replacement artifact; touching the planned path alone is not conformance.",
        ),
    ),
    "artifact_plan_review_invalid": FailureTransition(
        "artifact_plan_review_invalid",
        "artifact_plan_repair",
        "regenerate_artifact_after_fail_closed_conformance_review",
        "supervisor",
        (
            "Do not apply a candidate without valid independent conformance evidence.",
            "Keep the binding patch plan unchanged and return one replacement artifact.",
        ),
    ),
    "patch_plan_infeasible": FailureTransition(
        "patch_plan_infeasible",
        "root_cause_repair",
        "discard_infeasible_plan_and_replan_writable_scope",
        "supervisor",
        (
            "Two candidates failed the same obligations under the same binding plan.",
            "Discard that plan and align every behavioral owner with a writable required path.",
        ),
    ),
    "candidate_no_effect": FailureTransition(
        "candidate_no_effect",
        "root_cause_repair",
        "reject_no_effect_candidate_and_reconsider_root_cause",
        "supervisor",
        (
            "The candidate is mechanically valid enough to inspect but changes no source behavior.",
            "Do not spend format-repair rounds preserving a zero-effect semantic intent.",
            "Use current executable failures to choose a causal invariant before generating another candidate.",
        ),
    ),
    "stream_identical_search_replace": FailureTransition(
        "stream_identical_search_replace",
        "root_cause_repair",
        "reject_streamed_no_effect_candidate_and_reconsider_root_cause",
        "supervisor",
        (
            "The streamed candidate proved that search and replacement are identical.",
            "Reject it before completion, application, and tests.",
            "Treat this as zero semantic effect and reconsider the root cause, not the artifact grammar.",
        ),
    ),
    "root_cause_plan_non_actionable": FailureTransition(
        "root_cause_plan_non_actionable",
        "root_cause_repair",
        "discard_non_actionable_plan_and_reconsider_root_cause",
        "supervisor",
        (
            "The binding plan produced a mechanically no-op artifact, so the plan is not actionable against the visible source.",
            "Discard the binding plan instead of spending format-repair rounds on it.",
            "Choose a different root cause that explains every current failing observation, then create a new binding plan.",
        ),
    ),
    "format_repair_missing_context": FailureTransition(
        "format_repair_missing_context",
        "runner",
        "collect_context_then_retry_format_repair",
        "runner",
    ),
    "format_repair_missing_path": FailureTransition(
        "format_repair_missing_path",
        "format_repair",
        "rewrite_format_repair_as_path_qualified_artifact",
        "supervisor",
        ("Preserve the previous edit intent.", "Add the missing artifact path.", "Return only valid artifacts."),
    ),
    "format_repair_prose_mixed": FailureTransition(
        "format_repair_prose_mixed",
        "format_repair",
        "strip_prose_and_emit_valid_artifact",
        "supervisor",
        ("Do not recalculate the solution.", "Remove prose/headings/fences.", "Return only valid artifacts."),
    ),
    "format_repair_markdown_fence": FailureTransition(
        "format_repair_markdown_fence",
        "format_repair",
        "remove_markdown_fence_and_emit_valid_artifact",
        "supervisor",
        ("Preserve the previous edit intent.", "Remove markdown fences.", "Return only valid artifacts."),
    ),
    "format_repair_malformed_search_replace": FailureTransition(
        "format_repair_malformed_search_replace",
        "format_repair",
        "switch_malformed_search_replace_to_json_atomic_artifact",
        "runner",
        (
            "Preserve the previous edit intent.",
            "Switch protocol and return one JSON search_replace artifact envelope.",
            "Do not retry BEGIN_SEARCH_REPLACE markers in this round.",
        ),
    ),
    "artifact_orphan_search_replace": FailureTransition(
        "artifact_orphan_search_replace",
        "format_repair",
        "switch_orphan_search_replace_to_json_atomic_artifact",
        "runner",
        (
            "The output contained `<<<<<<< SEARCH` without `BEGIN_SEARCH_REPLACE: path`.",
            "Preserve the edit intent and return one path-qualified JSON search_replace artifact.",
        ),
    ),
    "format_repair_unbalanced_file_artifact": FailureTransition(
        "format_repair_unbalanced_file_artifact",
        "format_repair",
        "close_or_rewrite_file_artifacts",
        "supervisor",
        ("Preserve the previous edit intent.", "Return balanced BEGIN_FILE/END_FILE blocks or a valid diff."),
    ),
    "format_repair_no_artifact": FailureTransition(
        "format_repair_no_artifact",
        "format_repair",
        "rewrite_as_valid_artifact_only",
        "supervisor",
        ("Preserve the previous edit intent.", "Start immediately with a valid artifact marker."),
    ),
    "stream_repeated_text_runaway": FailureTransition(
        "stream_repeated_text_runaway",
        "format_repair",
        "abort_repeated_text_and_switch_to_json_atomic_artifact",
        "runner",
        (
            "The marker-based stream repeated text excessively.",
            "Switch protocol and return one concise JSON search_replace artifact; no long enumerations.",
        ),
    ),
    "stream_repeated_json_search_replace": FailureTransition(
        "stream_repeated_json_search_replace",
        "format_repair",
        "abort_repeated_json_and_rewrite_as_non_json_artifact",
        "runner",
        ("Do not emit JSON search_replace in the next attempt.",),
    ),
    "stream_multiple_json_search_replace": FailureTransition(
        "stream_multiple_json_search_replace",
        "format_repair",
        "abort_excess_or_cross_path_json_search_replace",
        "runner",
        (
            "The stream emitted too many JSON search_replace edits or touched more than one path.",
            "A repair round may use a small same-file edit set, but must not fan out across paths.",
            "For the next attempt, emit one target file only.",
            "Prefer BEGIN_SEARCH_REPLACE for one local edit, or a bounded same-file JSON edit set when coupled hunks are required.",
        ),
    ),
    "stream_json_search_replace_excess": FailureTransition(
        "stream_json_search_replace_excess",
        "format_repair",
        "abort_excess_json_and_rewrite_as_concise_artifact",
        "runner",
        ("Do not emit large JSON search_replace lists in the next attempt.",),
    ),
    "stream_markdown_fence_before_artifact": FailureTransition(
        "stream_markdown_fence_before_artifact",
        "format_repair",
        "abort_markdown_fence_and_emit_valid_artifact",
        "runner",
        ("Do not wrap artifacts in Markdown fences.", "Start immediately with BEGIN_FILE, BEGIN_SEARCH_REPLACE, or diff --git."),
    ),
    "stream_prose_before_artifact": FailureTransition(
        "stream_prose_before_artifact",
        "format_repair",
        "abort_prose_prefix_and_emit_valid_artifact",
        "runner",
        ("Do not explain, plan, summarize, or include propositions before the artifact.", "Start immediately with a valid artifact marker."),
    ),
    "stream_non_artifact_output": FailureTransition(
        "stream_non_artifact_output",
        "format_repair",
        "abort_non_artifact_stream_and_emit_valid_artifact",
        "runner",
        (
            "The stream spent too many bytes without any valid artifact marker.",
            "Do not analyze in the response; start immediately with BEGIN_SEARCH_REPLACE, BEGIN_FILE, or diff --git.",
        ),
    ),
    "stream_json_plan_before_artifact": FailureTransition(
        "stream_json_plan_before_artifact",
        "format_repair",
        "abort_json_plan_and_emit_valid_artifact",
        "runner",
        ("Do not emit JSON plans/propositions.", "Return only the requested file artifacts."),
    ),
    "stream_json_schema_mismatch": FailureTransition(
        "stream_json_schema_mismatch",
        "format_repair",
        "abort_unsupported_json_and_emit_artifact_schema",
        "runner",
        (
            "Do not emit JSON plans, propositions, graphs, or diagnostic objects.",
            "Use a supported artifact envelope whose first key is `artifacts`, or switch to one valid marker artifact.",
        ),
    ),
    "stream_mixed_artifact_formats": FailureTransition(
        "stream_mixed_artifact_formats",
        "format_repair",
        "abort_mixed_artifact_formats_and_choose_one_protocol",
        "runner",
        (
            "The stream mixed JSON file artifacts with BEGIN_FILE artifacts.",
            "Choose exactly one artifact protocol for the next attempt.",
            "Prefer balanced BEGIN_FILE/END_FILE blocks for generated multi-file output.",
            "Do not emit JSON artifacts in the next attempt.",
        ),
    ),
    "stream_multiple_file_artifacts_in_repair": FailureTransition(
        "stream_multiple_file_artifacts_in_repair",
        "format_repair",
        "abort_multi_file_repair_and_emit_single_artifact",
        "runner",
        (
            "The stream emitted multiple file artifacts during a repair round.",
            "A repair round must preserve one semantic edit intent.",
            "Return exactly one artifact for one writable target file.",
        ),
    ),
    "stream_artifact_too_large": FailureTransition(
        "stream_artifact_too_large",
        "format_repair",
        "abort_oversized_artifact_and_emit_atomic_patch",
        "runner",
        (
            "The streamed artifact exceeded the size budget.",
            "Return one small atomic edit focused on one failing assertion or exception family.",
            "For existing-file repairs, prefer exactly one BEGIN_SEARCH_REPLACE block.",
            "Do not emit JSON search_replace lists, prose, or multi-function rewrites.",
        ),
    ),
    "stream_python_file_artifact_too_large": FailureTransition(
        "stream_python_file_artifact_too_large",
        "format_repair",
        "abort_oversized_python_file_artifact_and_split_to_one_file",
        "runner",
        (
            "The streamed Python file artifact exceeded the size budget.",
            "Split the stage into one missing/generated writable file per attempt.",
            "For a missing generated file, return exactly one BEGIN_FILE/END_FILE block.",
            "If the target file already exists, return one focused BEGIN_SEARCH_REPLACE block.",
        ),
    ),
    "stream_readonly_artifact_path": FailureTransition(
        "stream_readonly_artifact_path",
        "format_repair",
        "abort_readonly_artifact_and_patch_product_code",
        "runner",
        (
            "The stream attempted to edit a read-only evidence path.",
            "Tests and readonly context are executable evidence, not repair targets.",
            "Return exactly one product-code artifact for a writable path.",
            "Do not emit artifacts for tests, README, or readonly evidence files.",
        ),
    ),
    "stream_python_diff_artifact_too_large": FailureTransition(
        "stream_python_diff_artifact_too_large",
        "format_repair",
        "abort_oversized_python_diff_and_emit_file_artifacts",
        "runner",
        (
            "The streamed unified diff for a Python file exceeded the size budget.",
            "For missing generated Python files, use balanced BEGIN_FILE/END_FILE blocks instead of a large diff.",
        ),
    ),
    "stream_artifact_process_narration": FailureTransition(
        "stream_artifact_process_narration",
        "format_repair",
        "abort_process_narration_and_emit_artifact_only",
        "runner",
        ("Do not include reasoning, self-talk, or process narration inside artifacts.", "Return only executable code/data changes."),
    ),
    "stream_artifact_malformed_search_replace": FailureTransition(
        "stream_artifact_malformed_search_replace",
        "format_repair",
        "abort_malformed_search_replace_and_switch_to_json_atomic_artifact",
        "runner",
        (
            "The streamed search/replace grammar was malformed.",
            "Switch protocol and return one JSON search_replace artifact envelope.",
        ),
    ),
    "stream_orphan_search_replace": FailureTransition(
        "stream_orphan_search_replace",
        "format_repair",
        "abort_orphan_search_replace_and_switch_to_json_atomic_artifact",
        "runner",
        (
            "The stream started a search/replace body without a file path header.",
            "Switch protocol and return one path-qualified JSON search_replace artifact.",
        ),
    ),
    "stream_root_cause_too_large": FailureTransition(
        "stream_root_cause_too_large",
        "root_cause_repair",
        "shrink_root_cause_report_before_patch",
        "runner",
        (
            "The diagnostic report exceeded its budget.",
            "Return a compact root-cause report with one chosen hypothesis and one patch target.",
        ),
    ),
    "mechanical_probe_contradiction": FailureTransition(
        "mechanical_probe_contradiction",
        "root_cause_repair",
        "reject_plan_contradicting_mechanical_probe",
        "runner",
        (
            "The patch plan contradicted deterministic Mechanical Probe observations.",
            "Treat the probe observations as fixed propositions.",
            "Choose a different root-cause patch or request missing context; do not repeat the rejected formula.",
        ),
    ),
    "unresolved_absent_api_call": FailureTransition(
        "unresolved_absent_api_call",
        "root_cause_repair",
        "reject_unresolved_absent_api_and_use_verified_interface",
        "runner",
        (
            "The candidate called an API that a deterministic probe proved absent.",
            "Use an existing owner-class method, or define the required method on its owner in the same candidate only when the specification requires that interface.",
            "Do not repeat the rejected call-site-only hypothesis.",
        ),
    ),
    "candidate_regression": FailureTransition(
        "candidate_regression",
        "repair_coder",
        "retain_prior_evidence_and_choose_non_regressing_hypothesis",
        "supervisor",
        (
            "The previous candidate increased failures and was rolled back byte-for-byte.",
            "Retain all earlier mechanical constraints and reject the rolled-back hypothesis.",
            "Choose a different smallest action against the restored state.",
        ),
    ),
    "candidate_provisional_progress": FailureTransition(
        "candidate_provisional_progress",
        "repair_coder",
        "retain_quarantined_candidate_and_repair_exposed_failure",
        "supervisor",
        (
            "A missing required test harness became executable in an isolated worktree.",
            "Keep the generated tests as read-only evidence and repair the newly exposed product boundary.",
            "Do not copy the provisional candidate back until every executable gate passes.",
        ),
    ),
    "replayed_regressing_candidate": FailureTransition(
        "replayed_regressing_candidate",
        "root_cause_repair",
        "reject_before_apply_and_reconsider_root_cause",
        "supervisor",
        (
            "The candidate contains no changed-line hypothesis beyond a previously regressing candidate.",
            "Do not apply or test the replayed candidate.",
            "Use independent root-cause analysis and a binding patch plan before another artifact is generated.",
        ),
    ),
    "stage_test_failed": FailureTransition("stage_test_failed", "repair_coder", "repair_stage_failure", "supervisor"),
    "final_test_failed": FailureTransition(
        "final_test_failed",
        "final_integration_repair",
        "parse_failure_focus_and_repair_product_code",
        "supervisor",
    ),
    "final_repair_artifact_invalid": FailureTransition(
        "final_repair_artifact_invalid",
        "format_repair",
        "semantic_salvage_then_format_repair",
        "pm",
    ),
    "test_edit_attempt": FailureTransition(
        "test_edit_attempt",
        "pm_planner",
        "reject_test_edit_and_reissue_product_code_repair",
        "runner",
        ("Tests are read-only in final integration repair.",),
    ),
    "repeated_same_failure": FailureTransition(
        "repeated_same_failure",
        "root_cause_repair",
        "reconsider_root_cause_before_next_patch",
        "supervisor",
        (
            "The executable failure signature did not change after the previous patch.",
            "Do not repeat the same edit or edit only formatting around the same code.",
            "Re-read the current file context and identify a different root-cause branch/data invariant before emitting one smallest patch.",
        ),
    ),
}



def transition_for_failure(failure_type: str | None) -> FailureTransition:
    if not failure_type:
        return FailureTransition("unknown", "repair_coder", "continue_with_latest_evidence", "supervisor")
    if failure_type in FAILURE_TRANSITIONS:
        return FAILURE_TRANSITIONS[failure_type]
    if failure_type in {"test_assertion_failed", "test_error", "syntax_error", "command_failed"}:
        return FAILURE_TRANSITIONS["stage_test_failed"]
    return FailureTransition(failure_type, "repair_coder", "continue_with_latest_evidence", "supervisor")

def is_protocol_failure_type(failure_type: str | None) -> bool:
    if not failure_type:
        return False
    if failure_type == "artifact_plan_mismatch":
        return False
    if failure_type.startswith(("format_repair_", "semantic_repair_", "artifact_plan_", "stream_")):
        return True
    return failure_type in {
        "artifact_invalid",
        "artifact_orphan_search_replace",
        "artifact_lint_failed",
        "patch_extraction_failed",
        "patch_apply_failed",
        "non_artifact_output",
        "mechanical_probe_contradiction",
        "test_edit_attempt",
        "unbalanced_file_artifact",
    }

def failure_transition_document(transition: FailureTransition, round_index: int, evidence: str = "") -> str:
    lines = [
        "## Failure Transition",
        "",
        f"- round: {round_index}",
        f"- failure_type: {transition.failure_type}",
        f"- owner: {transition.owner}",
        f"- next_role: {transition.next_role}",
        f"- action: {transition.action}",
    ]
    if transition.instructions:
        lines.append("- instructions:")
        lines.extend(f"  - {item}" for item in transition.instructions)
    if evidence:
        lines.extend(["", "### evidence", "```text", truncate_text(evidence, 2000), "```"])
    return "\n".join(lines)

def semantic_contract_to_dict(contract: SemanticContract) -> dict[str, object]:
    return {
        "id": contract.contract_id,
        "kind": contract.kind,
        "text": contract.text,
        "source": contract.source,
        "focus_files": list(contract.focus_files),
        "evidence": list(contract.evidence),
    }

def semantic_contracts_document(contracts: Sequence[SemanticContract]) -> str:
    lines = ["## Semantic Contracts", ""]
    if not contracts:
        lines.append("No semantic contracts extracted.")
        return "\n".join(lines)
    for contract in contracts:
        lines.append(f"- {contract.contract_id} [{contract.kind}]: {contract.text}")
        lines.append(f"  - source: {contract.source}")
        if contract.focus_files:
            lines.append("  - focus_files: " + ", ".join(contract.focus_files))
        if contract.evidence:
            lines.append("  - evidence: " + "; ".join(contract.evidence))
    return "\n".join(lines)


def authoritative_semantic_contracts(
    contracts: Sequence[SemanticContract],
) -> list[SemanticContract]:
    """Return contracts that may constrain product repair without triage.

    Assertions from stage-owned generated tests are hypotheses until their
    propositions have been checked against the fixed specification.  Keeping
    them in the ledger is useful evidence, but treating them as binding would
    let an agent-generated oracle override external acceptance criteria.
    """
    return [
        contract
        for contract in contracts
        if contract.kind != "provisional_test_oracle"
    ]

def semantic_contract_focus_paths(
    contracts: Sequence[SemanticContract],
    existing_paths: Sequence[str],
) -> tuple[list[str], list[str]]:
    existing = set(existing_paths)
    writable_product: list[str] = []
    readonly_context: list[str] = []
    for contract in contracts:
        for path in contract.focus_files:
            if path not in existing:
                continue
            if path.startswith("tests/"):
                readonly_context.append(path)
            else:
                writable_product.append(path)
    return unique_ordered(writable_product), unique_ordered(readonly_context)

def extract_proposition_ledger(text: str, source: str = "document") -> list[dict[str, str]]:
    propositions: list[dict[str, str]] = []
    for raw_line in text.splitlines():
        match = re.match(r"^\s*(?:[-*]\s*)?(?P<id>[PCGEAV]\d+)\s*:\s*(?P<text>.+?)\s*$", raw_line)
        if not match:
            continue
        proposition_id = match.group("id")
        propositions.append(
            {
                "id": proposition_id,
                "kind": proposition_id[0],
                "text": match.group("text").strip(),
                "source": source,
            }
        )
    return propositions

def proposition_manifest_from_documents(documents: Sequence[tuple[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str, str]] = set()
    result: list[dict[str, str]] = []
    for title, content in documents:
        for item in extract_proposition_ledger(content, title):
            key = (item["kind"], item["text"], item["source"])
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
    return result

def extract_semantic_contracts_from_command_docs(
    command_docs: Sequence[tuple[str, str]],
    project: Path | None = None,
    generated_test_paths: Sequence[str] = (),
) -> list[SemanticContract]:
    combined = "\n".join(document for _name, document in command_docs)
    lowered = combined.lower()
    contracts: list[SemanticContract] = []
    existing_product_paths = project_python_product_paths(project)
    generated_test_set = set(normalize_project_relative_paths(generated_test_paths))

    def add(kind: str, text: str, source: str, focus_files: Sequence[str] = (), evidence: Sequence[str] = ()) -> None:
        normalized = normalize_contract_text(text)
        if any(existing.text == normalized for existing in contracts):
            return
        normalized_focus = tuple(unique_ordered(focus_files))
        effective_kind = kind
        if any(path in generated_test_set for path in normalized_focus):
            effective_kind = "provisional_test_oracle"
        contracts.append(
            SemanticContract(
                contract_id=f"C{len(contracts) + 1:02d}",
                kind=effective_kind,
                text=normalized,
                source=source,
                focus_files=normalized_focus,
                evidence=tuple(unique_ordered(evidence)),
            )
        )

    focus_files: list[str] = []
    for raw_path in re.findall(r'File "([^"]+)", line \d+', combined):
        if "/tests/" in raw_path:
            focus_files.append("tests/" + raw_path.split("/tests/", 1)[1])
        elif "/minisqlite/" in raw_path:
            focus_files.append("minisqlite/" + raw_path.split("/minisqlite/", 1)[1])
    inferred_product_focus = unique_ordered(
        product_path
        for test_path in focus_files
        if test_path.startswith("tests/")
        for product_path in inferred_product_focus_from_test_path(test_path)
    )

    if "cannot import name 'tokenize'" in lowered:
        add(
            "api_contract",
            "The lexer module must export a callable tokenize function.",
            "ImportError",
            ["minisqlite/sql/lexer.py", "minisqlite/sql/__init__.py"],
            ["cannot import name 'tokenize'"],
        )

    if "unexpected character '*'" in lowered or "unexpected character: '*'" in lowered or 'unexpected character "*" ' in lowered:
        add(
            "api_contract",
            "The SQL lexer must tokenize '*' as a STAR token instead of raising SQLSyntaxError.",
            "unittest traceback",
            ["minisqlite/sql/lexer.py"],
            ["Unexpected character '*'"],
        )

    for attr in re.findall(r"AttributeError: 'Token' object has no attribute '([^']+)'", combined):
        add(
            "api_contract",
            f"Token objects must expose a `{attr}` attribute used by the tests.",
            "AttributeError",
            ["minisqlite/sql/lexer.py"],
            [f"missing Token.{attr}"],
        )

    for class_name, attr in re.findall(r"AttributeError: '([^']+)' object has no attribute '([^']+)'", combined):
        if class_name == "Token":
            continue
        if not inferred_product_focus:
            continue
        test_focus = [path for path in focus_files if path.startswith("tests/")]
        if attribute_error_is_cross_stage_test_harness_mismatch(
            class_name,
            test_focus,
            project,
            existing_product_paths,
        ):
            continue
        add(
            "api_contract",
            f"{class_name} objects must expose public `{attr}` attribute used by the tests.",
            "AttributeError",
            [*inferred_product_focus, *[path for path in focus_files if path.startswith("tests/")]],
            [f"missing {class_name}.{attr}"],
        )

    if "token.type" in combined or "TokenType." in combined:
        if re.search(r"AssertionError:\s*'?[A-Z_]+'?\s+!=\s+<TokenType\.", combined) or re.search(
            r"AssertionError:\s*'?[A-Z_]+'?\s+not found in \[<TokenType\.",
            combined,
        ):
            add(
                "api_contract",
                "Token.type must be string-compatible with the tested token names, not a raw Enum value.",
                "unittest assertion",
                ["minisqlite/sql/lexer.py"],
                ["TokenType enum compared against string token names"],
            )
        if re.search(r"AssertionError:\s*<TokenType\.IDENTIFIER:[^>]*>\s+!=\s+<TokenType\.[A-Z_]+:", combined) or re.search(
            r"AssertionError:\s*<TokenType\.[A-Z_]+:[^>]*>\s+not found in \[<TokenType\.IDENTIFIER:",
            combined,
        ):
            add(
                "api_contract",
                "SQL keywords must map to their keyword TokenType values instead of IDENTIFIER.",
                "unittest assertion",
                ["minisqlite/sql/lexer.py"],
                ["keyword token assertions produced IDENTIFIER"],
            )

    if re.search(r"AssertionError:\s*'?\d+'?\s+!=\s+'?-\d+'?", combined):
        add(
            "api_contract",
            "Negative integer literals must preserve the leading '-' in Token.value.",
            "unittest assertion",
            ["minisqlite/sql/lexer.py"],
            ["negative integer token lost sign"],
        )

    if (
        "AssertionError: 2 != 1" in combined or "AssertionError: 1 != 0" in combined
    ) and ("token" in lowered or "lexer" in lowered):
        add(
            "api_contract",
            "tokenize must not append an extra EOF token when tests expect only source tokens.",
            "unittest assertion",
            ["minisqlite/sql/lexer.py"],
            ["token count assertions show one extra token"],
        )

    # Read failing assert lines when the file still exists. This turns a raw
    # traceback into a stable repair contract that the next round can obey.
    if project is not None:
        for raw_path, raw_line in re.findall(r'File "([^"]+)", line (\d+)', combined):
            try:
                line_no = int(raw_line)
            except ValueError:
                continue
            path = Path(raw_path)
            if not path.exists():
                continue
            try:
                source_text = path.read_text(encoding="utf-8", errors="replace")
                source_line = source_text.splitlines()[line_no - 1].strip()
            except (OSError, IndexError):
                continue
            rel_path = str(path)
            if "/tests/" in rel_path:
                rel_path = "tests/" + rel_path.split("/tests/", 1)[1]
            product_focus_for_rel_path = inferred_product_focus_from_test_path(rel_path)
            if rel_path == "tests/test_pager.py" and product_focus_for_rel_path:
                if test_source_asserts_raw_page_round_trip(source_text):
                    add(
                        "api_contract",
                        "Pager.write_page(page_id, data) and Pager.read_page(page_id) must round-trip exact PAGE_SIZE bytes; pager.py must not interpret B+Tree page_type bytes in raw page IO.",
                        f"{rel_path}:{line_no}",
                        [*product_focus_for_rel_path, rel_path],
                        ["write_page/read_page raw page round-trip asserted by tests"],
                    )
                if test_source_asserts_zero_allocated_page(source_text):
                    add(
                        "state_contract",
                        "Pager.allocate_page() must create a zero-filled PAGE_SIZE page readable through read_page().",
                        f"{rel_path}:{line_no}",
                        [*product_focus_for_rel_path, rel_path],
                        ["allocated page zero-fill asserted by tests"],
                    )
            if "assertEqual" in source_line and ".type" in source_line and re.search(r'"[A-Z_]+"', source_line):
                add(
                    "api_contract",
                    "Existing tests assert token.type against string literals; preserve that public lexer contract.",
                    f"{rel_path}:{line_no}",
                    ["minisqlite/sql/lexer.py", rel_path],
                    [source_line],
                )
            elif "assertEqual" in source_line and "len(" in source_line:
                product_focus = inferred_product_focus_from_test_path(rel_path)
                if "token" in source_line.lower() or "minisqlite/sql/lexer.py" in product_focus:
                    add(
                        "api_contract",
                        "Existing lexer tests assert exact token counts; do not add hidden/sentinel tokens unless tests expect them.",
                        f"{rel_path}:{line_no}",
                        ["minisqlite/sql/lexer.py", rel_path],
                        [source_line],
                    )
                elif product_focus:
                    add(
                        "state_contract",
                        "Existing tests assert exact collection cardinality; preserve the tested count by repairing product behavior first.",
                        f"{rel_path}:{line_no}",
                        [*product_focus, rel_path],
                        [source_line],
                    )
            elif "assertEqual" in source_line and rel_path.startswith("tests/"):
                product_focus = inferred_product_focus_from_test_path(rel_path)
                if not product_focus:
                    continue
                if "next_page_id" in source_line:
                    add(
                        "state_contract",
                        "Pager.next_page_id must persist allocated page IDs across close/reopen when tests assert it after reopening.",
                        f"{rel_path}:{line_no}",
                        [*product_focus, rel_path],
                        [source_line],
                    )
                elif "format_version" in source_line:
                    add(
                        "api_contract",
                        "Pager must expose public format_version when tests assert header round-trip metadata.",
                        f"{rel_path}:{line_no}",
                        [*product_focus, rel_path],
                        [source_line],
                    )
                else:
                    contract_text = (
                        "A stage-generated test asserts a public product-code value; validate its setup, action, and expectation against SPEC.md before choosing the repair owner."
                        if rel_path in generated_test_set
                        else "Existing tests assert a public product-code value; preserve the tested behavior by repairing product code first."
                    )
                    add(
                        "api_contract",
                        contract_text,
                        f"{rel_path}:{line_no}",
                        [*product_focus, rel_path],
                        [source_line],
                    )

    return contracts

def observation_summary_document(round_index: int, command_docs: Sequence[tuple[str, str]]) -> str:
    failed: list[tuple[str, dict[str, str]]] = []
    patterns: Counter[str] = Counter()
    for name, document in command_docs:
        parsed = parse_command_result_document(document)
        if parsed.get("status") == "PASS":
            continue
        failed.append((name, parsed))
        text = f"{parsed.get('stdout', '')}\n{parsed.get('stderr', '')}"
        for pattern in re.findall(r"got (b[\"'][^\"']{1,160}[\"'])", text):
            patterns[f"observed response: {pattern}"] += 1
        for pattern in re.findall(r"RESP parse error:\s*([^\n]{1,160})", text):
            patterns[f"parse error: {pattern.strip()}"] += 1
        for pattern in re.findall(r"([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception):[^\n]{1,160})", text):
            patterns[f"exception: {pattern.strip()}"] += 1
        for pattern in re.findall(r"(-ERR [^\r\n]{1,160})", text):
            patterns[f"error response: {pattern.strip()}"] += 1

    lines = [
        "## Observation Summary",
        "",
        f"- round: {round_index}",
        f"- failed_checks: {len(failed)}",
    ]
    if failed:
        lines.append("- failed_names:")
        lines.extend(f"  - {name}" for name, _parsed in failed)
    common = [(item, count) for item, count in patterns.most_common(8) if count >= 2]
    if common:
        lines.append("- repeated_failure_patterns:")
        lines.extend(f"  - count={count}: {item}" for item, count in common)
        lines.extend(
            [
                "",
                "Repair guidance:",
                "- Multiple checks share the same failure pattern. Fix the shared implementation root cause first.",
                "- Do not weaken or replace tests while executable smoke evidence shows the implementation returns the wrong value.",
                "- Prefer the file that owns the repeated error path (parser, command dispatch, server loop, or storage) over unrelated docs/tests.",
            ]
        )
    elif failed:
        lines.extend(
            [
                "",
                "Repair guidance:",
                "- Use the failed command documents as executable evidence.",
                "- Change tests only when the test harness itself is the clearly demonstrated root cause.",
            ]
        )
    return "\n".join(lines).strip()
