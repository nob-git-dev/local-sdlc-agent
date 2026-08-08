# Attempt 07 Harness Failure

## Scope

- stages: S04-S05 plus final integration repair
- external product-code edits: none
- learning context: disabled
- run directory: `.sdlc-runner/runs/full-autonomy-07`

## Observed progress

- The Supervisor split an oversized stage before artifact generation.
- An independent Judge identified that helper-only tests did not prove the
  public checkout path.
- After the bounded child failed, the Supervisor expanded the writable scope to
  include the shared root-cause module without human intervention.
- All 38 generated tests passed, while the fixed seven-test acceptance suite
  correctly withheld completion on three integration failures.
- Identical search/replace output was rejected before application.

## Harness failures

The resumed recovery child was allowed to short-circuit on a local helper test.
That test did not replay the prior semantic Judge failure, so the recovery was
approved without proving that the newly authorized root-cause path had been
integrated into the public API.

The final repair later emitted an otherwise unambiguous search/replace block
whose final conflict marker was replaced by `END_SEARCH_REPLACE`. The parser
rejected the envelope even though its path, search payload, replacement
payload, and terminal boundary were deterministic. Because the same protocol
failure family had exhausted its bounded budget, the run ended with
`acceptance_failed`.

## General repair

- A resumed semantic recovery must not use local precheck success as a complete
  substitute for an independent repair round and review.
- A Supervisor-authorized recovery receives a fresh inner repair allowance,
  while the outer recovery count remains bounded.
- Normalize a missing `>>>>>>> REPLACE` only when exactly one complete block
  ends in a unique terminal `END_SEARCH_REPLACE` marker.
- Reject whole-file replacements that are unchanged from the current file.

No benchmark product or fixed acceptance-test file was manually edited.
