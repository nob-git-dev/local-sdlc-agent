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

PATCH_CONFORMANCE_OUTPUT_CONTRACT = (
    "Return ONLY one compact JSON object matching the patch-conformance schema. "
    "No markdown fences. No prose. No code artifacts."
)

PROJECT_POLICY_TRIAGE_OUTPUT_CONTRACT = (
    "Return ONLY one JSON object matching the project-policy triage schema. "
    "No markdown fences. No prose. No code artifacts."
)

JUDGE_REVIEW_OUTPUT_CONTRACT = (
    "Return compact Markdown with Verdict, one exact OWNERSHIP line, Proposition Ledger, "
    "Graph Edges, Findings, Required fixes, and Evidence gaps. Maximum 3 entries per section, "
    "one line per entry, no duplicated premise/evidence/finding, and no code artifact."
)


def root_cause_evidence_documents(
    documents: Sequence[tuple[str, str]],
    recent_window: int,
) -> list[tuple[str, str]]:
    """Keep recent context plus bounded executable evidence for diagnosis."""
    items = list(documents)
    if not items:
        return []

    recent_window = max(1, recent_window)
    selected: set[int] = set(range(max(0, len(items) - recent_window), len(items)))

    def matching_indices(*needles: str) -> list[int]:
        return [
            index
            for index, (title, _document) in enumerate(items)
            if any(needle in title.lower() for needle in needles)
        ]

    command_indices = matching_indices(
        "initial command",
        "command result",
        "html smoke",
        "redis smoke",
        "required path",
    )
    selected.update(command_indices[-4:])

    for category in (
        ("observation summary",),
        ("acceptance evidence gate",),
        ("failure analysis", "mechanical probe"),
        ("candidate regression rollback", "replayed regressing candidate", "root cause plan rejection"),
    ):
        indices = matching_indices(*category)
        if indices:
            selected.add(indices[-1])

    return [items[index] for index in sorted(selected)]


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
        - Fixed acceptance tests are immutable evidence.
        - Assertions from machine-owned stage-generated tests are provisional
          propositions until project-policy triage validates their setup,
          action, and expected result against SPEC.md.
        - If ownership is unresolved for a stage-generated test, the next
          action must be project_policy_triage. Do not silently promote that
          assertion into a product contract and do not authorize an edit.
        - After ownership is resolved, the next action must satisfy:
          changes_behavior(A) and not based_on(rejected_hypothesis), while the
          machine action gate determines which exact paths are writable.

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
            "role": "root_cause_analysis|format_repair|repair_artifact|project_policy_triage",
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
        - escalation: none|generated_test_oracle_triage|collect_context
        - minimal_patch_goal: one smallest behavior change
        - stop_rule: when the artifact writer must stop instead of guessing

        Validity rule:
        The plan is valid only if required_path is not under tests/ and the
        minimal_patch_goal can be implemented as one atomic product-code edit.
        Select one independent defect or behavioral invariant per plan. When
        the analysis names multiple independent defects, do not join them with
        "and"; choose the one whose repair should produce the next observable
        improvement and leave the others for later evidence-driven rounds.
        Use patch_type=search_replace only when the complete obligation fits in
        one contiguous source span, including any required import. Use
        patch_type=unified_diff when one invariant requires coordinated edits
        in multiple spans of the same product file. A no-op candidate is an
        artifact failure and does not by itself prove that an otherwise
        actionable binding plan is false.
        If that is impossible, use patch_type=missing_context. Use
        escalation=generated_test_oracle_triage only when the executable
        evidence identifies a machine-owned generated test that may contradict
        SPEC.md and no admissible product edit can satisfy both propositions.
        The planner cannot authorize or apply a test edit; independent
        project-policy triage and the runner's path gate must do that. Use
        escalation=collect_context only when a named missing file or symbol
        could make a product-code plan possible. Otherwise use escalation=none.
        """
    ).strip()


def patch_conformance_instruction(
    brief: str,
    patch_plan_doc: str,
    candidate_artifact: str,
) -> str:
    return textwrap.dedent(
        f"""
        Act as the independent patch-plan conformance reviewer for this
        coding-agent run.

        Request:
        {brief}

        Binding patch plan:
        {patch_plan_doc}

        Candidate artifact:
        {candidate_artifact}

        Your only job is to decide whether the candidate artifact implements
        every behavioral obligation in the binding plan. Do not write code,
        repair the artifact, approve application, or rely on tests that have
        not run yet.

        Counterexample procedure:
        1. Extract each behavior required by proposition, minimal_patch_goal,
           and stop_rule. Path selection and syntactic validity alone are not
           behavioral obligations.
        2. Derive the candidate's post-edit behavior from its SEARCH and
           REPLACE text, diff, or file content plus the supplied file context.
        3. For each obligation, try to name one input/state transition for
           which the candidate would still violate the plan.
        4. Mark status=pass only when every obligation is satisfied and each
           satisfied obligation cites concrete candidate evidence.
        5. If the candidate implements only deletion, persistence, validation,
           or one branch of a requested exact synchronization/replacement,
           mark the uncovered state transition not_satisfied.
        6. If context is genuinely insufficient, return
           status=insufficient_context and exact missing_context_paths. Do not
           guess either pass or fail.

        Formal rule:
        Let O be the set of explicit behavioral obligations in the plan and A
        be the candidate artifact. conformance(A, O) is true iff for every
        o in O, implements(A, o) is supported by candidate evidence and no
        derived counterexample remains. Merely touching required_path does not
        imply implements(A, o).

        Return exactly one JSON object:
        {{
          "status": "pass|fail|insufficient_context",
          "obligations": [
            {{
              "id": "O1",
              "requirement": "one explicit behavioral obligation",
              "status": "satisfied|not_satisfied|uncertain",
              "candidate_evidence": "exact candidate operation or (none)",
              "counterexample": "remaining input/state transition or (none)"
            }}
          ],
          "missing_obligations": ["required behavior not implemented"],
          "missing_context_paths": ["project-relative path needed for review"],
          "safe_next_action": "apply|repair_artifact|collect_context",
          "repair_instruction": "one concise instruction; empty when status=pass"
        }}
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
    oracle_obligation = ""
    if trigger == "generated_test_oracle_conflict":
        oracle_obligation = textwrap.dedent(
            """
            Mandatory generated-test counterexample procedure:
            1. Read the complete failing test setup and action sequence from the supplied file context.
            2. Derive the relevant state immediately before the action under test.
            3. Derive the state selected by the action target, revision, key, or input.
            4. Compare the assertion with that derived state and with SPEC.md.
            5. Try to falsify both hypotheses independently:
               H_product = product behavior violates a valid test proposition.
               H_test = generated test setup or assertion contradicts its own name, target state, SPEC.md, or an approved API.
            6. Cite the concrete setup/action/assertion facts in project_policy_basis. A prior repair advice saying tests are readonly is not evidence for H_product; ownership is the question being reviewed.
            7. Fill product_violation_evidence and test_contradiction_evidence
               independently before selecting either hypothesis. An exception
               location proves where execution stopped, not which proposition
               is correct.
            Classify test_harness only when H_test has positive evidence. Otherwise classify product_bug or insufficient_context.
            """
        ).strip()
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
        - A generated test may be classified as the owner only when evidence
          shows that its setup violates an earlier invariant before the
          asserted behavior is reached, or its expected proposition conflicts
          with SPEC.md or an already approved API contract.
        - A product exception that merely differs from the test expectation is
          not enough to classify the test as wrong.
        - Even when you select edit_test_harness, the action gate will permit
          only exact paths mechanically verified as stage-owned generated tests.

        {oracle_obligation}

        Return exactly one JSON object:
        {{
          "trigger": "{trigger}",
          "case_type": "artifact_format|test_harness|product_bug|spec_conflict|insufficient_context|reject",
          "confidence": "high|medium|low",
          "selected_hypothesis": "H_product|H_test|H_spec_conflict|undetermined",
          "product_violation_evidence": ["observed product behavior that contradicts one cited SPEC proposition"],
          "test_contradiction_evidence": ["generated setup/action/assertion fact that contradicts SPEC or its own target state"],
          "project_policy_basis": ["SPEC.md or document evidence line"],
          "safe_next_action": "format_repair|root_cause_analysis|repair_artifact|edit_test_harness|reject|ask_user",
          "editable_paths": ["project-relative paths that may be writable"],
          "readonly_paths": ["project-relative paths that must remain context/evidence only"],
          "forbidden_actions": ["actions the next role must not take"],
          "rationale": "one concise sentence"
        }}
        """
    ).strip()


def project_policy_arbitration_instruction(
    brief: str,
    prior_judge_vote: str,
    primary_triage: dict[str, object],
    evidence_doc: str,
) -> str:
    return textwrap.dedent(
        f"""
        Act as an independent project-policy arbiter for this coding-agent run.

        Request:
        {brief}

        Two advisory roles disagree about ownership:
        - prior_judge_vote: {prior_judge_vote}
        - primary_triage_vote: {primary_triage.get('case_type', 'insufficient_context')}

        Recompute the result from the primary evidence below. Do not decide by
        majority, role seniority, exception location, or prior repair advice.
        For each hypothesis, cite one positive fact or leave its evidence list
        empty. Select product_bug only when observed product behavior contradicts
        a cited SPEC proposition. Select test_harness only when a machine-owned
        generated test's setup/action/assertion contradicts SPEC.md or its own
        selected target state. Otherwise select insufficient_context and reject.

        Primary evidence:
        {evidence_doc}

        Return exactly one JSON object:
        {{
          "trigger": "generated_test_oracle_conflict",
          "case_type": "test_harness|product_bug|spec_conflict|insufficient_context|reject",
          "confidence": "high|medium|low",
          "selected_hypothesis": "H_product|H_test|H_spec_conflict|undetermined",
          "product_violation_evidence": ["positive product counterexample, or empty"],
          "test_contradiction_evidence": ["positive generated-test counterexample, or empty"],
          "project_policy_basis": ["specific SPEC/setup/action/assertion fact"],
          "safe_next_action": "root_cause_analysis|edit_test_harness|reject|ask_user",
          "editable_paths": ["project-relative paths"],
          "readonly_paths": ["project-relative evidence paths"],
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
        Immediately after the verdict line, emit exactly one ownership line:
        `OWNERSHIP: test_harness|product_bug|spec_conflict|insufficient_context|not_applicable`.
        Use `test_harness` only for a machine-owned generated test whose setup
        or assertion conflicts with SPEC.md; fixed acceptance tests are never
        test_harness. Use `not_applicable` when no ownership dispute exists.
        If the result is acceptable
        and command evidence passes, start with "判定: 承認". If not, start
        with "判定: 修正依頼" and list concrete required fixes.
        Express major review points as short P/C/E/A/V propositions and
        Graph Edges before Required fixes.

        Hard output bounds:
        - At most 3 proposition entries, 3 graph edges, 3 findings, 3 required fixes, and 3 evidence gaps.
        - Each entry must be one line and cite an existing evidence identifier or path when available.
        - Never restate the same fact, failure, method, or path under a new item number.
        - Stop immediately after the bounded Evidence gaps section; do not narrate your reasoning.
        """
    ).strip()
