# Artifact Stream Guard Mathematical Contract

## Objective

Prevent a local LLM from wasting a full API call on artifact output that is
already provably invalid while it is still streaming.

This is not a prompt-only rule. It is a deterministic supervisor transition.

## Symbols

- Let `r` be an agent repair round.
- Let `t` be a streaming observation index.
- Let `y_t` be the partial assistant output observed after `t` content chunks.
- Let `P` be the set of permitted artifact protocols for the current round.
- Let `F(y_t)` be extracted artifact features from the partial output.
- Let `A(y_t)` be an anomaly score.
- Let `theta` be the abort threshold.

## Feature Definition

For JSON search/replace artifacts, extract tuples:

```text
q_i = (path_i, search_i, replace_i)
```

from every partial JSON object matching:

```text
{"type":"search_replace","path":...,"search":...,"replace":...}
```

Define:

```text
dup(y_t)   = max_q count(q in y_t)
total(y_t) = number of extracted search_replace objects in y_t
```

The current guard uses:

```text
A_dup(y_t)   = dup(y_t)
A_total(y_t) = total(y_t)
```

## Abort Predicate

Abort the current streaming API call if either predicate becomes true:

```text
A_dup(y_t)   >= 8
A_total(y_t) >= 40
```

This means:

```text
abort(y_t) = (dup(y_t) >= 8) OR (total(y_t) >= 40)
```

## Supervisor Transition

If `abort(y_t)` is true during round `r`, write a stream-abort document and
transition to round `r + 1` with a stricter artifact protocol set:

```text
P_{r+1} = {BEGIN_FILE, unified_diff}
```

For this failure mode, JSON and search_replace are removed:

```text
JSON notin P_{r+1}
search_replace notin P_{r+1}
```

## Implementation Mapping

- `repeated_json_search_replace_score(y_t)` computes `dup(y_t)` and `total(y_t)`.
- `artifact_stream_guard(y_t)` computes `abort(y_t)`.
- `LLMStreamAbortError` stops the active stream and preserves `y_t`.
- `03-rXX-stream-abort.md` records the evidence.
- `artifact_failure_modes_from_documents(...)` maps that evidence into the next round.
- `strict_artifact_output_instruction(...)` sets `P_{r+1}`.

## Acceptance Criteria

- A repeated JSON artifact stream aborts before the model consumes the full
  configured output budget.
- The abort is recorded as a document, not hidden state.
- The next coder round sees a document-based constraint and a stricter output
  contract.
- Non-streaming mode remains compatible and falls back to post-generation lint.

