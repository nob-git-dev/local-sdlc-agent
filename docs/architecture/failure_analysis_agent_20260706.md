# Failure Analysis Agent

## Context

Repeated repair failures showed that the agent could observe test failures and
enter a root-cause loop, but it still needed a separate role that converts
failure history into explicit constraints.

The user proposed a failure-analysis group that structures failed attempts and
analyzes them mathematically. The implementation adds that group as an
independent judge-level API call.

## Design

When a command check fails with the same exact failure signature, or the same
coarser failure-family signature, as the previous round, the runner now calls
`failure_analysis` before root-cause repair.

This intentionally separates two questions:

- `F_t`: "is this byte-level failure essentially unchanged?"
- `G_t`: "is this the same family of failing checks even if assertion payloads
  drifted?"

The call uses:

- `agent_level`: `judge`
- `call_function`: `failure_analysis`
- temperature: `0.0`
- max tokens: PM budget
- thinking disabled

The output is written to:

- `05-rXX-failure-analysis.json`
- `run.json.failure_analyses`

The next root-cause prompt receives that document through the normal document
exchange path. No hidden conversation memory is shared.

## Formal Control Model

Let:

- `F_t`: current failure signature
- `G_t`: current failure-family signature, based on command, failure type, and
  failing test identifiers
- `A_i`: a previously attempted action
- `H_i`: the hypothesis that justified `A_i`
- `R_i`: executable evidence after `A_i`

If `same(F_i, F_t)` and `applied(A_i)`, then `H_i` is rejected unless the next
analysis refines it with stronger executable evidence.

If assertion payload text changes but `same(G_i, G_t)`, the runner still treats
the run as repeated failure. This prevents small assertion-message drift from
hiding a stuck repair loop.

For unittest output, `G_t` is based on the command, high-level failure type,
and failing test identifiers. It deliberately ignores volatile assertion text
such as object representations, line numbers, or message wording when the same
test functions keep failing.

The next action must satisfy:

- `changes_behavior(A_next)`
- `not touches_tests(A_next)` unless the failure is classified as a harness
  defect
- `not based_on(rejected_hypothesis)`

## Intended Effect

This does not make the local LLM smarter by itself. It makes the supervisor
state less ambiguous:

- repeated failure becomes a first-class event
- prior wrong hypotheses are explicitly listed
- the next role receives binding constraints as a document
- `run.json` preserves structured failure history for later review

## Verification

Added coverage:

```bash
python3 -m unittest tests.test_local_sdlc.LocalSDLCTest.test_command_failure_family_signature_ignores_assertion_payload_drift
python3 -m unittest tests.test_local_sdlc.LocalSDLCTest.test_agent_routes_repeated_same_failure_to_root_cause_repair
python3 -m unittest discover -s tests
```

Observed result:

- targeted test: OK
- full suite after artifact normalization follow-up: 179 tests OK
