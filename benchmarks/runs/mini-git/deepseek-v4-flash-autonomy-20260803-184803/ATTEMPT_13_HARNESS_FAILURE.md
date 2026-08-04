# Attempt 13 Harness Failure

## Outcome

- Product implementation was not completed.
- The resumed baseline remained isolated; no failed candidate was copied back.
- Generated tests remained 48/48 passing before each candidate.
- Fixed acceptance remained at 2 failures before each candidate.
- External product-code interventions: 0.
- Fixed acceptance-test interventions: 0.

## Observed Runtime Behavior

Four API-generated candidates modified `minigit/status.py`. Every candidate
increased the executable failure score and was restored byte-for-byte by the
candidate-regression transaction. The second and fourth candidate documents
were byte-identical. The second, third, and fourth candidates also represented
the same changed-line hypothesis despite different amounts of unchanged
context.

## General Harness Finding

The runtime remembered that a candidate regressed behavior, but its next-round
constraint existed only as prompt text. It did not mechanically compare a new
candidate with rejected hypotheses before application. Consequently, bounded
adaptive rounds were consumed by semantic replays of one failed action.

## General Repair Proposition

```text
DeltaSignature(A) := hash(path, removed_changed_lines, added_changed_lines)

RejectedReplay(A) :=
  Exists(R in RegressingCandidates,
    DeltaSignature(A) intersects DeltaSignature(R))

RejectedReplay(A) ->
  not Apply(A)
  and Transition(root_cause_repair)
```

Unchanged search/replace context must not affect `DeltaSignature`; otherwise a
model can replay the same edit by adding or removing neighboring lines. This
gate remains project-neutral because it compares emitted edit structure and
observed regression, not domain names or expected solutions.
