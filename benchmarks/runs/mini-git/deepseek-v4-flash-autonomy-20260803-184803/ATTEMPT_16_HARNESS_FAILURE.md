# Attempt 16 Harness Failure

## Outcome

- The isolated baseline remained at 48 generated tests passing and 2 fixed acceptance tests failing.
- Two regressing product candidates were restored byte-for-byte.
- No failed candidate was copied back.
- External product-code interventions: 0.
- Fixed acceptance-test interventions: 0.

## Observed Runtime Behavior

The runtime rejected a replay of an earlier regressing candidate before application. It then invoked the deeper root-cause profile with an 8,192-token output budget. The analysis correctly declined to guess and requested the fixed acceptance-test file because its content was truncated and the executable failure details were no longer visible in the recent document window.

The requested file was already declared as read-only context. The old handler therefore added no path and routed the next round through the generic coder path. This discarded a valid request for refocused evidence. A later candidate regressed and was rolled back; subsequent malformed outputs exhausted the remaining recovery budget.

## General Harness Finding

Context membership and context visibility are different propositions. A file may belong to the context set while its contents are absent from the actual prompt because an earlier file consumed the global character budget. Likewise, executable evidence may exist in run history but fall outside a recency-only document window.

## General Repair Propositions

Let `C_t` be the ordered context vector at round `t`, `visible(p, C_t)` mean that the relevant content of path `p` is present, and `E_fail` be the latest executable failure evidence.

```text
requested(p) and exists(p) and not visible(p, C_t)
  -> prioritize(p, C_(t+1))
  and retry(root_cause_analysis)

root_cause_analysis(t)
  -> E_fail is in input(t), independent of document recency
```

The rule is project-neutral: it depends on path existence, ordered context visibility, and evidence type rather than domain vocabulary or a known implementation.
