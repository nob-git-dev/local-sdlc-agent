# Project Policy Triage - 2026-07-06

## Context

Three recurring issues showed that fixed runner rules alone are not enough:

- generated tests can be wrong, but external acceptance tests should remain
  read-only evidence
- malformed artifacts can sometimes be safely normalized, but ambiguous edits
  must be rejected
- repeated failures should be detected by failure family, not brittle assertion
  wording

The user explicitly agreed that LLMs should not receive direct apply authority.
The resulting design separates classification from execution.

## Three-Layer Control Model

Let:

- `U`: universal safety invariants
- `P`: project policy from `SPEC.md` and run documents
- `E`: executable/document evidence
- `T`: LLM triage classification
- `A`: action/artifact to execute

Rules:

- `U(A)` is machine-enforced and cannot be overridden by `T`.
- `T` is advisory classification only: `T = classify(P, E)`.
- `A` may proceed only if `valid(T) and U(A)` and normal artifact/path checks
  pass.
- If `T` is missing, invalid, low-confidence, or conflicts with `U`, the runner
  falls back to conservative behavior.

## Implemented Behavior

`local_sdlc.py agent` now supports:

```text
--project-policy-triage auto|always|never
```

Default `auto` calls the judge-level `project_policy_triage` function only for
context-dependent boundary cases such as generated test-harness ownership.

The triage call returns JSON:

```json
{
  "trigger": "test_harness_ownership",
  "case_type": "test_harness",
  "confidence": "high",
  "project_policy_basis": ["SPEC.md or document evidence"],
  "safe_next_action": "edit_test_harness",
  "editable_paths": ["tests/test_btree.py"],
  "readonly_paths": ["minisqlite/storage/pager.py"],
  "forbidden_actions": ["do not add compatibility APIs solely for a bad generated test"],
  "rationale": "one sentence"
}
```

The runner stores the result in:

- `05-rXX-project-policy-triage.json`
- `run.json.project_policy_triages`

The result can update repair advice for the next round, but it cannot directly
apply a patch.

## Verification

Added coverage:

```bash
python3 -m unittest tests.test_local_sdlc.LocalSDLCTest.test_agent_runs_project_policy_triage_for_generated_test_harness_ownership
python3 -m unittest discover -s tests
```

Observed targeted result:

- project policy triage test: OK
- full suite after implementation: 180 tests OK
