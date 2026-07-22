# Local SDLC semantic contract enforcement

Date: 2026-07-05

## Reason

MiniSQLite S02 exposed a failure mode where the agent created tests that defined
an API contract, then later repaired the implementation by changing that
contract. The concrete example was:

- tests expected `Token.type == "CREATE"` and no hidden EOF token
- implementation changed `Token.type` to raw `TokenType` enum values
- repair advice only saw repeated `AssertionError: 2 != 1`, which was too
  shallow

## Implemented

### Structured propositions

Agent manifests now include:

- `propositions`: parsed `P/C/G/E/A/V` lines from PM/Judge documents
- `semantic_contracts`: contracts extracted from executable evidence

This moves proposition discipline from prompt-only text into persisted run
state.

### Semantic contract extraction

The runner now extracts API contracts from unittest tracebacks and failing test
source lines. Current supported patterns include:

- missing exported function, e.g. `cannot import name 'tokenize'`
- missing Token attributes, e.g. `Token.line`
- token type mismatch between string assertions and enum implementation
- exact token count assertions, including hidden EOF/sentinel token drift
- lexer `*` handling when tests show `Unexpected character '*'`

### Artifact lint enforcement

Before applying a new artifact, the runner checks generated file content against
known semantic contracts. Example blocked contracts:

- if tests require string-compatible `Token.type`, a raw `TokenType` enum rewrite
  is linted as an error
- if tests require no hidden EOF token, artifacts appending EOF are linted as an
  error
- if tests require `*` tokenization, lexer artifacts that omit `*` handling are
  linted as an error

### Semantic repair mode

After executable evidence has produced semantic contracts, later repair rounds
now switch from generic `repair_artifact` to function-level
`semantic_repair`. This is a separate API call profile:

- temperature: `0`
- max tokens: `8192`
- thinking: `off`

The runner also changes the output contract mechanically. In semantic repair
mode, an acceptable artifact must satisfy:

- `|A| = 1`: exactly one atomic artifact
- `type(A) in {BEGIN_SEARCH_REPLACE, unified_diff}`
- `touched_files(A)` contains exactly one product-code file
- `touched_files(A)` excludes `tests/`
- `A` directly addresses at least one extracted semantic contract

The lint gate rejects JSON artifacts, `BEGIN_FILE`, `BEGIN_APPEND_FILE`,
whole-file rewrites, multiple artifacts, test edits, and large search/replace
blocks in this mode. This turns the MiniSQLite S02 lesson into runner behavior
instead of relying on prompt compliance alone.

### Semantic repair format control

MiniSQLite S03 later exposed a narrower protocol failure: the model understood
the semantic contract, but emitted a malformed artifact such as prose plus
`BEGIN_SEARCH_REPLACE` without `: path`.

The runner now models this mechanically:

```text
Given output y:
  MISSING_CONTEXT(y) -> collect context before artifact lint
  malformed_semantic_artifact(y) -> failure_type in semantic_repair_*
  semantic_repair_* -> next_role = format_repair
```

The concrete classifications include:

- `semantic_repair_missing_path`
- `semantic_repair_prose_mixed`
- `semantic_repair_markdown_fence`
- `semantic_repair_multiple_artifacts`
- `semantic_repair_forbidden_artifact`
- `semantic_repair_test_edit`
- `semantic_repair_too_large`

For these failures, the next prompt is constrained to preserve the intended
semantic edit and rewrite only the artifact envelope. The first non-whitespace
bytes must be `BEGIN_SEARCH_REPLACE: ` or `diff --git `.

Semantic contract `focus_files` are also lifted into the next round context:

- existing product-code focus files become writable targets
- existing `tests/` focus files become read-only evidence
- missing focus files remain unavailable and must be requested through
  `MISSING_CONTEXT`

### Stage queue execution

`run-stages` is available as a first-class command and has tests. It executes
the synthesized stage queue as isolated `agent` runs and keeps full acceptance
tests for the final gate while using smaller per-stage checks during stage work.

## Validation

Commands:

```bash
python3 -m py_compile local_sdlc.py tests/test_local_sdlc.py
python3 -m unittest discover -s tests
```

Result:

```text
Ran 108 tests in 1.842s
OK
```

## Remaining limits

The current semantic extractor is rule-based. It handles the observed lexer
contract drift, but it is not a general theorem prover. The next useful step is
to add more contract extractors for common unittest patterns:

- `assertIsInstance`
- `assertRaises`
- list/dict shape checks
- CLI stdout shape checks
- persistence behavior checks
