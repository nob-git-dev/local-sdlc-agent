# Artifact protocol control model

Date: 2026-07-05

## Reason

MiniSQLite reruns showed that semantic repair hardening alone is not enough.
The run can fail before semantic repair is reached when ordinary artifact
generation or `format_repair` emits malformed output.

The control model therefore applies to every artifact-producing function:

```text
generate_artifact
repair_artifact
format_repair
semantic_repair
stream_guard
```

## Formal model

Let:

```text
Y_r = LLM output in round r
P_r = permitted artifact protocols for round r
F(Y_r) = protocol features extracted from Y_r
T(Y_r) = touched files extracted from Y_r
E_r = executable evidence after applying Y_r
```

The runner distinguishes two failure classes:

```text
ProtocolFailure(Y_r):
  Y_r cannot be safely interpreted as an artifact.

FunctionalFailure(Y_r, E_r):
  Y_r is a valid artifact, but executable evidence fails.
```

Protocol failures consume `protocol_repair_rounds`.
Functional failures consume `max_rounds`.

This prevents malformed artifact output from exhausting the functional repair
budget before real test evidence can be used.

## Implemented controls

### 1. Format repair grammar

`format_repair` now has explicit classifications:

- `format_repair_missing_context`
- `format_repair_missing_path`
- `format_repair_prose_mixed`
- `format_repair_markdown_fence`
- `format_repair_unbalanced_file_artifact`
- `format_repair_no_artifact`

These mirror the semantic repair classifications, but allow the broader valid
artifact set used by ordinary repair:

```text
JSON artifact
BEGIN_FILE / END_FILE
BEGIN_SEARCH_REPLACE / END_SEARCH_REPLACE
unified diff
```

### 2. Generic stream repetition guard

The stream guard now detects:

- repeated JSON `search_replace`
- excessive total JSON `search_replace`
- repeated token or repeated line runaway output

The observed MiniSQLite failure mode was a repeated `REINDEX` stream. This is
now classified as:

```text
stream_repeated_text_runaway
```

### 3. Separate repair budgets

`agent` accepts:

```text
--max-rounds N
--protocol-repair-rounds M
```

`--max-rounds` is the functional repair budget.
`--protocol-repair-rounds` is the artifact/protocol repair budget.

`run-stages` forwards `--protocol-repair-rounds` to each child `agent`; its
default is `2`.

## Validation

Targeted tests:

```text
test_format_repair_format_issues_are_specific
test_format_repair_lint_rejects_no_artifact_output
test_artifact_stream_guard_aborts_repeated_text_runaway
test_agent_protocol_budget_does_not_consume_functional_round
test_stage_agent_args_propagate_function_api_profiles
```

Full validation should run:

```bash
python3 -m py_compile local_sdlc.py tests/test_local_sdlc.py
python3 -m unittest discover -s tests
```
