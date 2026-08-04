# Attempt 15 Harness Failure

## Outcome

- The isolated baseline remained at 48 generated tests passing and 2 fixed acceptance tests failing.
- One regressing product candidate was restored byte-for-byte.
- No failed candidate was copied back.
- External product-code interventions: 0.
- Fixed acceptance-test interventions: 0.

## Observed Runtime Behavior

The first candidate was a JSON search/replace whose search and replacement were identical. The runtime rejected it, but classified it as `artifact_invalid` and routed to format repair. A later streamed identical replacement was also counted as a stream/protocol failure. Between them, a non-empty `status.py` candidate increased failures from 2 to 5 and was rolled back. A final identical stream exhausted protocol-repair budget before root-cause analysis ran.

## General Harness Finding

An identical replacement is not merely malformed serialization. It is a semantic candidate with provably zero effect. Preserving its intent in format repair cannot produce evidence of progress and can consume every protocol round without reaching causal analysis.

## General Repair Proposition

Let `effect(A, S)` be the source-state delta produced by candidate `A` in state `S`.

```text
effect(A, S) = empty
  -> not apply(A)
  and not test(A)
  and transition(root_cause_repair)
```

This classification is project-neutral: it depends only on the candidate delta, not on domain vocabulary or an expected solution.
