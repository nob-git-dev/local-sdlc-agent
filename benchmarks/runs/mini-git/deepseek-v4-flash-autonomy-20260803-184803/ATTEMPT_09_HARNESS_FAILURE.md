# Attempt 09 Harness Failure

## Scope

- stages: S04-S05 plus S99 final integration repair
- external product-code edits: none
- learning context: disabled
- run directory: `.sdlc-runner/runs/full-autonomy-09`
- outcome: planned stages completed; S99 stopped after a repair candidate
  increased executable failures and exhausted the applicable recovery branch

## Observed progress

- S04 generated and approved worktree/status behavior in an isolated worktree.
- S05 passed its stage precheck without an unnecessary edit.
- The goal-level gate correctly rejected completion when three fixed acceptance
  tests still failed and started S99 automatically.
- Repeated-failure analysis identified three concrete shared causes and selected
  the two relevant product paths.
- The root-cause candidate then called methods that were absent from the current
  index interface. Unit failures increased from zero to nine and acceptance
  failures changed from three assertions to two errors plus one failure.
- All S99 edits remained isolated and were not copied into the benchmark.

## Harness failures

The runner could detect that the candidate was worse, but failure-score
comparison was used only to reward improvement. A behaviorally worse candidate
remained in the isolated workspace, consumed the final root-cause repair round,
and had no bounded rollback-and-replan transition. The Judge also repeated the
same evidence until reaching its token limit, delaying each failed round.

The first S04 coder response contained complete JSON artifact objects but was
missing only an unambiguous terminal container closer. The parser rejected it
instead of applying a syntax-only recovery.

## General repair

- Define `CandidateRegression(i)` when an applied candidate is evaluated by the
  unchanged command vector and its failure count is greater than the previous
  state.
- Snapshot every artifact transaction before apply. On candidate regression,
  restore exact pre-round bytes, verify equality mechanically, persist the
  rejected candidate, and use one bounded adaptive retry with a different plan.
- Fail closed when byte restoration cannot be verified; never send that state
  to Judge or copy-back.
- Carry the rejected paths and interface-assumption evidence into the next
  repair prompt.
- Recover JSON only when a lexer-level bracket stack proves that missing
  terminal `]`/`}` bytes are the sole syntax defect. Truncated strings and
  ambiguous mismatches remain rejected.
- Bound Judge sections and duplicate-free entries; use a smaller no-thinking
  output budget for the DeepSeek profile.

No benchmark product or fixed acceptance-test file was manually edited.
