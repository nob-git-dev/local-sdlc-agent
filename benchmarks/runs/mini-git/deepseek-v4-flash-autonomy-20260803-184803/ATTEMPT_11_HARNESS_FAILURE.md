# Attempt 11 Harness Failure

## Scope

- stages: S04-S05 plus S99 final integration repair
- external product-code edits: none
- learning context: disabled
- run directory: `.sdlc-runner/runs/full-autonomy-11`
- outcome: planned stages completed; S99 retained three fixed acceptance
  failures after four ineffective repair candidates

## Observed progress

- S04 and S05 passed prechecks without unnecessary product changes.
- S99 established an executable baseline failure score of three.
- Independent failure analysis correctly kept fixed acceptance tests read-only,
  named the product owner paths, and rejected the repeated local approach.
- The product worktree remained isolated because the final acceptance gate did
  not pass; none of the four candidates were copied back.

## Harness failure

The normal LLM-judge path recorded a `repeated_same_failure` transition but
left `final_failure_type` set to the underlying assertion failure. The next
round therefore selected the ordinary repair role instead of the declared
`root_cause_repair` role. Root-cause analysis and patch planning were skipped.

The coder consequently ignored the structured analysis and successively added
an unrelated status branch, an index synchronization helper, and duplicate
`index.save()` / synchronization calls. The same three acceptance failures
remained throughout all four rounds.

## General repair

- Make the supervisor transition selected after Judge review the executable
  state consumed by the next round.
- Route an unchanged executable failure family through independent root-cause
  analysis and one binding patch plan before another artifact may be emitted.
- Cover this behavior in normal LLM-judge mode, not only command-only mode.
- Preserve the rule that advisory roles propose actions while deterministic
  path policy, artifact grammar, and executable tests retain application and
  approval authority.

No benchmark product or fixed acceptance-test file was manually edited.
