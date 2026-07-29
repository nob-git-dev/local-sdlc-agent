# local_sdlc modular split note - 2026-07-05

## Decision

`local_sdlc.py` remains the single user-facing CLI entrypoint, but the implementation must not return to a single large source file. The package now separates CLI wiring, LLM transport, skill prompting, artifact protocol, verification, routing, stage planning, run state, and command runners.

## Module Map

| Module | Responsibility |
|---|---|
| `local_sdlc/cli.py` | argparse wiring, lightweight diagnostics, compatibility wrappers |
| `local_sdlc/models.py` | dataclasses, defaults, enum-like constants, API profile normalization |
| `local_sdlc/llm_client.py` | OpenAI-compatible HTTP client, health checks, streaming, role/function API profiles |
| `local_sdlc/skills.py` | skill loading and system-prompt construction |
| `local_sdlc/artifact_ops.py` | artifact extraction and artifact application primitives |
| `local_sdlc/artifact_protocol.py` | artifact failure transitions, semantic contracts, proposition and observation documents |
| `local_sdlc/artifact_lint.py` | artifact lint, stage-scope lint, stream guards, and protocol repair checks |
| `local_sdlc/repair_advice.py` | repair advice and deterministic repair artifact helpers |
| `local_sdlc/python_project_analysis.py` | Python project symbol, class-owner, and test-focus inference helpers |
| `local_sdlc/artifacts.py` | backward-compatible facade for artifact-related public functions |
| `local_sdlc/verification.py` | command execution, command result documents, smoke checks, evidence helpers |
| `local_sdlc/stages.py` | deterministic stage queue, stage-specific paths/tests, acceptance summaries |
| `local_sdlc/stage_runner.py` | `stage-plan` and `run-stages` command execution |
| `local_sdlc/routing.py` | supervisor task classification, phase routing, judge approval parsing |
| `local_sdlc/supervisor_runner.py` | `supervisor` and legacy `supervise` command execution |
| `local_sdlc/phase_runner.py` | `spec`, `phase`, and `implement` command execution |
| `local_sdlc/agent_runner.py` | application-level coding agent loop |
| `local_sdlc/harnesses/base.py` | harness plugin evidence contract |
| `local_sdlc/harnesses/html_browser.py` | HTML static smoke and browser/Tetris DOM behavior smoke |
| `local_sdlc/harnesses/python_cli.py` | safety-preserving Python/CLI command checks |
| `local_sdlc/harnesses/python_probes.py` | deterministic Python/API/CLI/storage mechanical probes |
| `local_sdlc/run_state.py` | run directories, resume context, worktree copy-back |
| `local_sdlc/workspace.py` | project file discovery, context collection, safe path resolution |
| `local_sdlc/utils.py` | small pure helpers shared across layers |

## Boundaries

- CLI modules should call application modules; application modules should not import `cli.py`.
- Skill prompts and API calls stay independent per role/function. Do not collapse PM/Coder/Judge into one conversation history.
- Handoff remains document-based: SPEC, run documents, command evidence, and artifact files.
- Filesystem mutation belongs to run state, verification, artifact application, or explicit command runners. Pure routing/stage functions should stay side-effect free.
- Compatibility wrappers in `cli.py` intentionally keep the old monkeypatch surface for tests that replace `LocalLLMClient`.

## Verification

After the split:

- `python3 -m py_compile local_sdlc.py local_sdlc/*.py tests/test_local_sdlc.py`
- `python3 -m unittest discover -s tests`

Both checks passed during the split.

## 2026-07-29 Refactor Slice S07a

`local_sdlc/artifacts.py` had grown past 6,800 lines. The first behavior-preserving
split moved JSON/BEGIN_FILE/diff/search_replace extraction and artifact apply
primitives into `local_sdlc/artifact_ops.py`.

Compatibility rule:

- `local_sdlc/artifacts.py` re-exports `artifact_ops` symbols, including helper
  names needed by the remaining stream guard and repair-advice code.
- Existing callers that import from `local_sdlc.artifacts` keep working.
- No feature behavior changed in this slice.

Verification:

- `python3 -m py_compile local_sdlc.py local_sdlc/*.py`
- artifact extraction/apply focused tests
- `python3 -m unittest tests.test_local_sdlc`

## 2026-07-29 Refactor Slice S07b

`tests/test_local_sdlc.py` had grown past 10,000 lines. The second
behavior-preserving split moved common test fixtures and the most clearly bounded
test groups into focused modules.

Test module map:

- `tests/helpers.py`: shared module loader, project fixture, and LLM env scrubber.
- `tests/test_safety.py`: command safety decision and command blocking tests.
- `tests/test_cancel_control.py`: cancel token and cancelled-resume guard tests.
- `tests/test_artifact_ops.py`: artifact extraction and application primitive tests.
- `tests/test_local_sdlc.py`: remaining integration, routing, agent loop, profile,
  web, stage, and higher-level artifact behavior tests.

Compatibility rule:

- Tests continue to load the public compatibility surface via `local_sdlc.cli`.
- Test discovery must work through both direct module execution and
  `python3 -m unittest discover -s tests`.
- This slice changes test organization only; production behavior is unchanged.

Verification:

- `python3 -m unittest tests.test_safety tests.test_cancel_control tests.test_artifact_ops`
- `python3 -m unittest tests.test_local_sdlc`
- `python3 -m unittest discover -s tests`
- `python3 -m py_compile local_sdlc.py local_sdlc/*.py tests/*.py`
- `git diff --check`

## 2026-07-29 Refactor Slice S07c

The third behavior-preserving test split moved the stage runner surface into a
focused module.

Moved test scope:

- `stage-plan` JSON output and relative `--spec-file` resolution.
- `run-stages` parser options, dry-run manifest writing, cancellation before a
  stage agent call, per-stage agent execution, and final test gate ordering.
- Stage queue synthesis and `build_stage_agent_args` propagation of function API
  profiles and absolute project/spec paths.

Boundary rule:

- `tests/test_stage_runner.py` owns deterministic stage queue and stage command
  runner behavior.
- Stage-scope lint, semantic repair, and repair advice tests remain outside this
  slice because they belong to higher-level artifact / repair control.
- This slice changes test organization only; production behavior is unchanged.

Verification:

- `python3 -m unittest tests.test_stage_runner`
- `python3 -m unittest tests.test_local_sdlc`
- `python3 -m unittest discover -s tests`
- `python3 -m py_compile local_sdlc.py local_sdlc/*.py tests/*.py`
- `git diff --check`

## 2026-07-29 Implementation Slice S02

The HTML/browser smoke checks were moved out of `verification.py` into the
harness plugin boundary.

Implementation:

- `local_sdlc/harnesses/base.py` defines `HarnessEvidence` and the generic
  harness protocol.
- `local_sdlc/harnesses/html_browser.py` owns static HTML smoke checks,
  browser/Tetris DOM behavior smoke, and the Tetris startup render predicate.
- `verification.py` keeps the previous public functions as compatibility
  wrappers: `run_html_smoke_checks()`, `run_browser_tetris_check()`, and
  `has_tetris_initial_render_sequence()`.

Boundary rule:

- Harnesses return evidence records and legacy command documents.
- Harnesses do not set `approved`, `final_verdict`, or any run-level approval
  state. Approval remains an application-layer decision based on requirements,
  evidence, and judge policy.

Verification:

- `python3 -m unittest tests.test_harnesses`
- HTML smoke regression tests
- browser/Tetris smoke regression test when Chromium is available
- `python3 -m unittest discover -s tests`
- `python3 -m py_compile local_sdlc.py local_sdlc/*.py local_sdlc/harnesses/*.py tests/*.py`

## 2026-07-29 Implementation Slice S03a

Python/CLI command checks now have a harness boundary.

Implementation:

- `local_sdlc/harnesses/base.py` provides `evidence_from_command_result()`, a
  shared conversion from command result documents to `HarnessEvidence`.
- `local_sdlc/harnesses/html_browser.py` reuses that conversion so HTML/browser
  and CLI checks report evidence consistently.
- `local_sdlc/harnesses/python_cli.py` wraps `run_checked_command()` and keeps
  existing command safety decisions, blocked-command behavior, and legacy
  `(document, ok)` projection.
- Convenience helpers cover generic commands, `py_compile`, and `unittest
  discover`.

Boundary rule:

- This slice does not replace every application-layer call site yet. It creates
  the harness contract for safe command evidence first. The next S03 slice can
  swap runner call sites from direct command execution to harness execution with
  smaller regression risk.

Verification:

- `python3 -m unittest tests.test_harnesses`
- `python3 -m py_compile local_sdlc/harnesses/base.py local_sdlc/harnesses/html_browser.py local_sdlc/harnesses/python_cli.py tests/test_harnesses.py`

## 2026-07-29 Refactor Slice S07d

`local_sdlc/artifacts.py` is now a compatibility facade. The former mixed
artifact module was split by responsibility without changing public import
paths.

Implementation:

- `local_sdlc/artifact_protocol.py` owns artifact failure transitions, semantic
  contracts, proposition ledgers, and command observation summaries.
- `local_sdlc/artifact_lint.py` owns artifact lint, stage-scope lint, semantic
  and format repair checks, stream guards, and project-policy probe rejection.
- `local_sdlc/repair_advice.py` owns repair advice rendering and deterministic
  repair artifact helpers.
- `local_sdlc/python_project_analysis.py` owns Python project symbol, class
  owner, import alias, and test-focus inference helpers.
- `local_sdlc/harnesses/python_probes.py` owns deterministic Python struct/API,
  precondition, CLI, CLI-state, and storage-state probes.
- `local_sdlc/artifacts.py` re-exports these modules for compatibility with the
  CLI and existing tests.

Boundary rule:

- `agent_runner.py` remains the application orchestration loop for now. Its
  nested helpers share run-local state heavily, so splitting it before the
  Requirement/Evidence/RepairAction models are fully extracted would create
  more risk than benefit.

Line-count outcome:

- `local_sdlc/artifacts.py`: 5,643 lines -> 10-line facade.
- Largest artifact-related implementation file: `repair_advice.py`, 1,884
  lines.
- Remaining large file: `agent_runner.py`, 3,429 lines, retained as
  orchestration until a safer domain-model split is ready.

Verification:

- `python3 -m py_compile local_sdlc.py local_sdlc/*.py local_sdlc/harnesses/*.py tests/*.py`
- `python3 -m unittest tests.test_artifact_ops tests.test_harnesses`
- `python3 -m unittest discover -s tests`
