# Attempt 10 Harness Failure

## Scope

- stages: S04-S05 plus S99 final integration repair
- external product-code edits: none
- learning context: disabled
- run directory: `.sdlc-runner/runs/full-autonomy-10`
- outcome: planned stages completed; S99 preserved the safe baseline but did
  not resolve the three fixed acceptance failures

## Observed progress

- S04 and S05 passed their prechecks without unnecessary coder calls.
- S99 established an executable baseline failure score of three.
- Two candidates introduced a call to absent `Index.replace_with_tree`, raising
  the failure score from three to 23.
- Both regressions were rejected, rolled back to exact pre-round bytes, and
  verified before another round. The bad candidates were never copied back.
- Judge output stayed within the reduced budget and returned materially faster
  than in Attempt 09.
- The benchmark base remained at 48 passing generated tests and three failing
  fixed acceptance tests.

## Harness failures

Candidate rollback replaced the previous repair advice instead of composing
with it. That discarded the earlier mechanical API fact and its owner-file
focus, so the next coder could repeat the same absent-interface hypothesis.

Later marker-based search/replace streams entered a periodic loop consisting
of the same delimiter and replacement body. The generic byte limit stopped the
stream, but only after substantial output and under the less precise
`stream_artifact_too_large` classification. The following retry again emitted
malformed marker grammar.

## General repair

- Make recovery knowledge monotone: a rollback may change the active strategy,
  but it must merge earlier focus files, evidence, and hard constraints.
- Promote mechanically observed absent APIs to candidate-transaction lint. A
  typed call to such an API is rejected unless the owner already defines it or
  the same transaction defines it on the owner class.
- Put mechanical API owner files first in retry context and retain the exact
  rejected proposition.
- Detect periodic multi-line stream loops before the generic artifact-size
  limit so the transition records the real failure family.
- After malformed or periodically repeated marker output, switch the next
  format-only retry to one bounded JSON search/replace envelope.
- Deduplicate changed paths before recording regression evidence.

No benchmark product or fixed acceptance-test file was manually edited.
