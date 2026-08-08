# Attempt 12 Harness Failure

## Scope

- stages: S04-S05 plus S99 final integration repair
- external product-code edits: none
- learning context: disabled
- run directory: `.sdlc-runner/runs/full-autonomy-12`
- outcome: fixed acceptance failures improved from three to two, then the
  repair budget ended after an incomplete implementation of a correct plan

## Observed progress

- The normal Judge transition correctly routed repeated failures through
  `root_cause_analysis -> patch_planner -> artifact_writer`.
- A status-classification repair reduced the fixed acceptance suite from three
  failures to two; the adaptive budget recognized this monotone improvement.
- The second structured analysis correctly identified checkout/index
  synchronization as the owner of both remaining failures.
- Fixed acceptance tests and benchmark product files received no external edit.

## Harness failures

### Executable evidence contradiction

The configured acceptance command failed while `acceptance-evidence-gate`
reported `ok: true`. The runner still refused final approval because
`command_ok` was false, but the contradictory document polluted Failure
Analysis and Judge context.

### Patch-plan conformance gap

The binding plan required checkout to make the index exactly match the target
tree. The generated candidate removed current-tree paths that were absent from
the target, but did not add target-tree paths that were absent from the current
index. The artifact was syntactically valid and touched the correct file, so it
was applied even though it implemented only one direction of exact
synchronization. Both executable failures remained unchanged.

## General repair

- Any failed configured executable command must create a fail-closed
  acceptance blocker, independent of requirement-to-evidence mapping.
- Add an independent `patch_conformance` API call between artifact generation
  and application for binding root-cause plans.
- Require obligation-level evidence and counterexample search; touching the
  planned path or applying one branch is not semantic conformance.
- Reject a non-conforming candidate before application and retain the same
  patch plan for a bounded artifact-writer retry.
- Keep application authority in deterministic path, grammar, safety, and test
  gates; the conformance role remains advisory and cannot apply an edit.
- Bound DeepSeek reasoning-only analysis before the existing one-shot
  no-thinking condensation to keep progress observable without removing the
  analysis role.

No benchmark product or fixed acceptance-test file was manually edited.
