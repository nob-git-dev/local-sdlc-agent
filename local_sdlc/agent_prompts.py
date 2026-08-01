"""System-role task prompts used by the agent application loop."""

from __future__ import annotations

import json
import textwrap
from typing import Sequence


FAILURE_ANALYSIS_OUTPUT_CONTRACT = (
    "Return ONLY one compact JSON object matching the requested failure-analysis schema. "
    "observed_facts <= 6, attempted_actions <= 4, rejected_hypotheses <= 3, "
    "active_constraints <= 5, formal_constraints <= 4. "
    "next_required_action.required_paths, readonly_paths, forbidden_paths, next_patch_type, "
    "and minimal_patch_goal are mandatory. "
    "Mechanical Probe observations override inferred facts. "
    "No markdown fences. No prose. No code artifacts."
)

PATCH_PLANNER_OUTPUT_CONTRACT = (
    "Return ONLY PATCH_PLAN in the requested schema. No markdown fences. "
    "No code. No artifacts. No prose outside the schema."
)

PROJECT_POLICY_TRIAGE_OUTPUT_CONTRACT = (
    "Return ONLY one JSON object matching the project-policy triage schema. "
    "No markdown fences. No prose. No code artifacts."
)

JUDGE_REVIEW_OUTPUT_CONTRACT = (
    "Return Markdown with Verdict, Proposition Ledger, Graph Edges, Findings, "
    "Required fixes, and Evidence gaps."
)


def failure_analysis_instruction(
    brief: str,
    round_index: int,
    failure_type: str,
    failure_signature: str | None,
    repeated_count: int,
    state_transitions: Sequence[dict[str, object]],
    prior_analyses: Sequence[dict[str, object]],
    evidence_text: str,
) -> str:
    transition_text = json.dumps(list(state_transitions)[-8:], ensure_ascii=False, indent=2)
    prior_text = json.dumps(list(prior_analyses)[-5:], ensure_ascii=False, indent=2)
    return textwrap.dedent(
        f"""
        Act as the failure-analysis role for this coding-agent run.

        Request:
        {brief}

        Trigger:
        A failed executable check was observed. Your job is to convert the
        failure history into machine-readable propositions for the supervisor.
        Do not write code and do not propose an artifact.

        Current failure:
        - round: {round_index}
        - failure_type: {failure_type}
        - failure_signature: {failure_signature or "(unknown)"}
        - repeated_same_failure_count: {repeated_count}

        Recent state transitions:
        {transition_text}

        Prior failure analyses:
        {prior_text}

        Latest command evidence:
        {evidence_text}

        Mechanical evidence rule:
        If a Mechanical Probe document is present, its observations are
        authoritative runtime facts. Do not infer contradictory values for
        page IDs, method names, byte headers, parsed symbols, or persisted
        state. If prior analyses contradict a probe, mark the prior hypothesis
        rejected and base next_required_action on the probe.

        Mathematical model:
        - F_t is the current failure signature.
        - A_i is an attempted action or patch.
        - H_i is the hypothesis that justified A_i.
        - R_i is executable evidence after A_i.
        - If same(F_i, F_t) and applied(A_i), then reject H_i unless
          new evidence strictly refines H_i.
        - The next action must satisfy:
          changes_behavior(A) and not touches_tests(A) and
          not based_on(rejected_hypothesis).

        Return exactly one JSON object with this schema:
        {{
          "failure_id": "Sxx-Ryy-Fzz or short stable id",
          "round": {round_index},
          "failure_type": "{failure_type}",
          "failure_signature": "{failure_signature or ''}",
          "observed_facts": ["1-6 facts grounded in command or Mechanical Probe evidence"],
          "attempted_actions": [
            {{"round": 1, "action": "what changed or failed", "result": "same/improved/worse/unknown"}}
          ],
          "rejected_hypotheses": [
            {{"hypothesis": "1-3 precise hypotheses", "reason": "why evidence rejects it"}}
          ],
          "active_constraints": [
            "1-5 constraints the next role must obey"
          ],
          "next_required_action": {{
            "role": "root_cause_analysis|format_repair|repair_artifact",
            "goal": "one sentence",
            "required_paths": ["project-relative writable product path required for the next patch"],
            "readonly_paths": ["project-relative evidence/context path that must not be edited"],
            "forbidden_paths": ["project-relative path that must not be edited"],
            "next_patch_type": "search_replace|unified_diff|missing_context",
            "minimal_patch_goal": "one smallest behavior change the next patch must implement",
            "forbidden_focus": ["hypothesis or edit that must not be repeated"],
            "required_focus": ["file/function/invariant to inspect next"]
          }},
          "formal_constraints": [
            "same(F_i,F_t) && applied(A_i) => reject(H_i)"
          ]
        }}
        """
    ).strip()


def patch_planner_instruction(brief: str, role_label: str, analysis_doc: str) -> str:
    return textwrap.dedent(
        f"""
        Act as the patch-planner role for this coding-agent run.

        Request:
        {brief}

        Current repair role:
        {role_label}

        Structured/root-cause analysis input:
        {analysis_doc}

        Your job is to select exactly one minimal next patch proposition.
        Do not write code. Do not emit artifacts. Do not edit tests.

        Return exactly this schema:
        PATCH_PLAN
        - proposition: one sentence of the form "If P, change A so C"
        - required_path: one project-relative writable product file
        - readonly_paths: comma-separated evidence paths or "(none)"
        - forbidden_paths: comma-separated paths that must not be edited
        - patch_type: search_replace|unified_diff|missing_context
        - minimal_patch_goal: one smallest behavior change
        - stop_rule: when the artifact writer must stop instead of guessing

        Validity rule:
        The plan is valid only if required_path is not under tests/ and the
        minimal_patch_goal can be implemented as one atomic product-code edit.
        If that is impossible, use patch_type=missing_context.
        """
    ).strip()


def project_policy_triage_instruction(
    brief: str,
    trigger: str,
    candidate_action: str,
    state_transitions: Sequence[dict[str, object]],
    prior_triages: Sequence[dict[str, object]],
    evidence_doc: str,
) -> str:
    transition_text = json.dumps(list(state_transitions)[-8:], ensure_ascii=False, indent=2)
    prior_text = json.dumps(list(prior_triages)[-5:], ensure_ascii=False, indent=2)
    return textwrap.dedent(
        f"""
        Act as the project-policy triage role for this coding-agent run.

        Request:
        {brief}

        Trigger:
        {trigger}

        Candidate action under consideration:
        {candidate_action}

        Your job is to classify a context-dependent ownership or policy
        question. Do not write code. Do not emit artifacts. Do not approve
        an edit directly; the runner will validate your classification
        against path policy and artifact grammar.

        Project policy sources:
        - SPEC.md is the primary project policy.
        - PM/Judge documents and command evidence may clarify whether a
          generated test harness is mutable or whether tests are external
          acceptance evidence.
        - Universal safety constraints remain binding even if project
          policy is permissive: no path traversal, no project-root escape,
          no conflict-marker patch application, no ambiguous artifact
          application.

        Recent state transitions:
        {transition_text}

        Prior project-policy triages:
        {prior_text}

        Evidence to classify:
        {evidence_doc}

        Formal model:
        - U = universal safety invariant. U is always machine-enforced.
        - P = project policy from SPEC.md and run documents.
        - E = executable/document evidence.
        - T = your triage classification.
        - T may select a next role/action, but T cannot directly apply an edit.
        - valid(T) requires basis(T) subset of P union E and no violation of U.

        Return exactly one JSON object:
        {{
          "trigger": "{trigger}",
          "case_type": "artifact_format|test_harness|product_bug|spec_conflict|insufficient_context|reject",
          "confidence": "high|medium|low",
          "project_policy_basis": ["SPEC.md or document evidence line"],
          "safe_next_action": "format_repair|root_cause_analysis|repair_artifact|edit_test_harness|reject|ask_user",
          "editable_paths": ["project-relative paths that may be writable"],
          "readonly_paths": ["project-relative paths that must remain context/evidence only"],
          "forbidden_actions": ["actions the next role must not take"],
          "rationale": "one concise sentence"
        }}
        """
    ).strip()


def pm_control_instruction(brief: str) -> str:
    return textwrap.dedent(
        f"""
        Act as the PM-level controller for this coding-agent run.

        Request:
        {brief}

        Produce a compact control document for the coder and judge. Include:
        - Proposition Ledger with short P/C/G/E/A/V items
        - Graph Edges with supports/constrains/satisfies/verifies/blocks
        - intended outcome
        - fixed requirements
        - files that may be changed
        - acceptance checks
        - hallucination traps

        Do not write implementation code.
        """
    ).strip()


def deterministic_pm_control(
    brief: str,
    change_targets: Sequence[str],
    allow_extra_new_files: bool,
    readonly_context_files: Sequence[str],
) -> str:
    return textwrap.dedent(
        f"""
        ## Deterministic PM Control

        Request:
        {brief}

        ## Proposition Ledger
        - P1: The user request is the visible task definition for this run.
        - C1: SPEC.md fixed requirements must be preserved.
        - C2: Only writable targets and safe allowed new files may be changed.
        - G1: Produce implementation artifacts that satisfy executable evidence.
        - E1: Command results and smoke checks are the only completion evidence.
        - V1: Approval is allowed only when required artifacts and checks pass.

        Explicit change targets:
        {", ".join(change_targets) if change_targets else "(none)"}

        Additional new files:
        {"allowed when they are project-relative, safe, and did not exist before this run" if allow_extra_new_files else "disabled"}

        Read-only context files:
        {", ".join(readonly_context_files) or "(none)"}

        Acceptance checks:
        - Apply only changes needed for the request.
        - Preserve SPEC.md fixed requirements.
        - Use command results and smoke checks as objective evidence.
        - Do not weaken meaningful tests to hide implementation failures.
        """
    ).strip()


def judge_review_instruction(brief: str, round_index: int, final_round: int) -> str:
    return textwrap.dedent(
        f"""
        Act as the judge-level agent for this coding-agent run.

        Request:
        {brief}

        Round:
        {round_index} of {final_round}

        Review the applied patch, command results, SPEC.md, and PM control
        document. Treat coder output as a claim. Use included current file
        contents as evidence; do not claim file content is missing when it
        is present in Included file contents. If command evidence and file
        contents disagree for a static/proxy check, identify the mismatch
        owner instead of blindly repeating the same product-code diagnosis.
        If the result is acceptable
        and command evidence passes, start with "判定: 承認". If not, start
        with "判定: 修正依頼" and list concrete required fixes.
        Express major review points as short P/C/E/A/V propositions and
        Graph Edges before Required fixes.
        """
    ).strip()
