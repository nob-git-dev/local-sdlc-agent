# Attempt 06 Harness Failure

## Scope

- stages: S04-S05
- external product-code edits: none
- learning context: disabled
- run directory: `.sdlc-runner/runs/full-autonomy-06`

## Observed progress

- The HTML evidence false positive did not recur.
- LLM Judge mode detected the repeated failure and wrote structured failure analyses.
- Reasoning-only Judge, failure-analysis, and policy-triage calls were recovered by
  same-call condensation with a 2,048-token final-answer budget.
- Project Policy Triage ran independently and the Action Gate kept the generated
  test read-only after the first `product_bug` classification.

## Harness failure

The generated test named `test_checkout_removes_tracked_path_absent_from_target`
constructed the opposite history: its first commit contained `remove.txt`, its
second commit deleted that path, and it then checked out the first commit while
expecting `remove.txt` to stay deleted. The test name, setup, target revision,
assertion, and checkout specification were inconsistent.

The first policy triage did not symbolically follow the setup sequence and
classified the failure as a product bug. Later product patches changed the
concrete assertion from a missing deletion to wrong restored content, but the
runner suppressed another triage solely because the same test family had
already been classified once. One repair was also a trailing-newline-only
search/replace that passed the textual no-op guard.

## General repair

- Require generated-test triage to derive pre-action state, target state, and
  asserted state before assigning ownership.
- Treat prior readonly repair advice as a hypothesis under review, not evidence
  that the product owns the failure.
- Bind each triage to the exact executable failure signature. Reuse it for the
  same signature, but re-triage when a new concrete counterexample appears in
  the same test family.
- Reject search/replace artifacts whose only difference is trailing whitespace.

The obsolete run was stopped during round 4 after the contradictory proposition
and triage-suppression rule were proven. No benchmark product or test file was
manually edited.
