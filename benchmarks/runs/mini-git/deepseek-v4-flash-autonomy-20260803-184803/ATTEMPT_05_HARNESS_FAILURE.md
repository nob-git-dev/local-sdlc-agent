# Attempt 05 Harness Failure

## Scope

- stages: S02-S05
- external product-code edits: none
- learning context: disabled
- run directory: `.sdlc-runner/runs/full-autonomy-05`

## Observed progress

- S02 and S03 were approved and copied back by the agent.
- The dependency-aware split kept `minigit/worktree.py` beside
  `tests/test_worktree.py` instead of isolating the test from its product owner.
- S04.1 was approved and copied back.
- S04.2 reached 10 of 11 passing generated tests before recovery.

## Harness failure

The remaining generated test corrupted the current commit's content-addressed
tree object in place, then expected checkout of that same commit to reach an
unsafe-path check. Correct object verification failed first with
`CorruptObjectError`, so the test setup contradicted the expected control-flow
proposition. The first Judge identified this as a test-construction problem,
but the runner never promoted the stage-owned test into a safely writable
repair target.

Four general defects prevented recovery:

1. LLM Judge mode did not carry the latest functional failure signature into
   the next round, so unchanged failures were never classified as repeated.
2. Project-policy triage therefore never received the generated-test oracle
   conflict at the required transition.
3. The HTML coverage provider matched `open` inside `reopen`, classifying a
   Mini Git persistence requirement as `html_visible` and adding `tetris.html`
   to repair focus.
4. A reasoning-only Judge retry discarded its useful reasoning and reran the
   original prompt from scratch, producing a weaker conclusion.

An earlier S02 artifact also showed that `tmp_path` was treated as a pytest
fixture even when it was an ordinary local variable in product code.

## General repair

- Persist exact and family failure signatures in both command-only and LLM
  Judge paths.
- Trigger project-policy triage on repeated stage-owned generated-test
  failures, but let the Action Gate authorize only explicitly owned test paths.
- Keep external acceptance tests read-only even when an LLM asks to edit them.
- Match ASCII coverage terms on token boundaries and require local semantic
  context for ambiguous UI terms.
- Condense a bounded tail of same-call reasoning into a short thinking-off
  conclusion; never pass raw reasoning to later roles.
- Treat pytest fixture names as fixture evidence only inside test-shaped
  artifacts.

The obsolete run was stopped after it entered round 10 with the same executable
failure. No Mini Git product or generated test was manually edited.
