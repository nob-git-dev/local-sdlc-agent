# Tetris Repair Loop Context Fix 2026-07-05

## Context

The Tetris acceptance run failed after four repair rounds even though the generated
UI existed. The final browser smoke reported missing public functions such as
`startGame`, `gameLoop`, `movePiece`, `rotate`, `softDrop`, `hardDrop`,
`clearLines`, and `gameOver`.

## Diagnosis

The failure was not simply a weak Tetris implementation. The runner had two
general control defects:

1. Repair rounds did not include the current generated artifact contents when a
   newly created file became part of the project during the run.
2. The judge was also missing that current file context, so it could not compare
   command evidence with the actual file and repeatedly treated the artifact as
   absent.

This allowed a repair round to rewrite working code and drop required public API
exports that had already been present in earlier versions.

There was also one verification-harness defect: the static Tetris smoke checked
for an exact `initBoard(); renderBoard();` newline/indentation shape, so valid
startup code could be reported as failing if indentation changed.

## General Fix

The agent runner now refreshes file context on every repair round and includes
created target artifacts in both coder and judge prompts. Repair prompts also
tell the coder to preserve existing public APIs, exported symbols, and required
HTML elements unless command evidence proves they are wrong.

The Tetris repair advice now recognizes browser smoke failures for missing
public functions and steers the coder toward a small public API patch rather
than a whole-file rewrite.

The static Tetris startup check now uses a whitespace-tolerant parser rule for
`initBoard();` followed by `renderBoard();`.

## Verification

Unit verification:

```text
python3 -m py_compile local_sdlc.py local_sdlc/*.py tests/test_local_sdlc.py
python3 -m unittest discover -s tests
```

Result: all tests passed.

Agent re-run:

```text
python3 local_sdlc.py agent "ブラウザとキーボードで遊べるテトリスを作って" \
  --project benchmarks/tetris-ddd-rerun-20260705-211000 \
  --include tetris.html \
  --require-path tetris.html \
  --apply \
  --resume benchmarks/tetris-ddd-rerun-20260705-211000/.sdlc-runner/runs/tetris-ddd-agent \
  --domain-modeling auto \
  --small-patch \
  --no-replace-file \
  --max-rounds 8 \
  --protocol-repair-rounds 2 \
  --adaptive-rounds 2 \
  --command-timeout 45 \
  --stream \
  --run-dir .sdlc-runner/runs/tetris-ddd-agent-resume-after-runner-fix
```

Result:

```text
final_verdict: approved
html-smoke tetris.html: PASS
browser-tetris-smoke: PASS
```

## Reusable Rule

For any repair loop:

```text
If artifact A is writable or required, and A exists at the start of round r,
then A's current contents must be supplied to coder and judge for round r.
```

Without this invariant, the agent can regress working behavior while trying to
repair stale or partial evidence.
