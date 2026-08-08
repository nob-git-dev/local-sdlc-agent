# Attempt 04 Harness Failure

## Scope

- stages: S02-S05
- external product-code edits: none
- learning context: disabled
- run directory: `.sdlc-runner/runs/full-autonomy-04`

## Observed progress

- The original S02 failure was autonomously split into S02.1 and S02.2.
- S02.1 generated `minigit/index.py` and `minigit/repository.py` and was approved.
- A reasoning-only PM result was recovered by one bounded thinking-off retry.
- A reasoning-only Judge result was recovered by the same bounded policy.
- Zero-test unittest output was rejected as non-vacuous evidence.

## Harness failure

The path-only split placed product files in S02.1 and their tests in S02.2.
S02.2 then proved that `minigit/repository.py` required repair, but that file was
read-only in the child slice. The parent replayed the same impossible recovery
scope, including repeated PM/DDD work, without adding a new admissible action.

Formally, for parent authorization `W`, active slice `W_i`, and evidence focus
`F(E)`, the missing transition was:

```text
Delta_i = (F(E) intersect W) - W_i
Delta_i != empty  =>  W_i' = W_i union Delta_i
```

Expansion outside `W` must remain forbidden. A recovery with unchanged scope,
evidence family, and artifact policy must not be replayed.

## General repair

- Keep conventional test files beside their lexically matching product files.
- Preserve the parent stage writable set as a separate repair authorization.
- Expand an active slice only to evidence-named paths inside that authorization.
- Reject repeated root-cause recovery when it introduces no new action.
- Infer repair paths from files that actually exist in the current project
  before using any legacy domain fallback.
- Stream DDD status into the same observable partial manifest as other calls.

The run was stopped after the repeated impossible state was proven. No Mini Git
product code was manually changed.
