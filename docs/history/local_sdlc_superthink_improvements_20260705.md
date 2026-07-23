# local_sdlc Superthink Improvements - 2026-07-05

## Goal

Move the runner from "observable long-running agent" toward "agent that changes work size and repair strategy based on observations."

This follows the Mini SQLite streaming retest findings:

- large one-shot coder calls are observable but still too coarse
- generated tests can mismatch the configured runner test command
- repeated failures need root-cause repair rather than repeated patch attempts
- search/replace is fragile when a generated file needs broad conversion

## Implemented

### 1. Deterministic Stage Queue

Planning command:

```bash
python3 local_sdlc.py stage-plan --format markdown
python3 local_sdlc.py stage-plan --format json
```

The command reads `SPEC.md` and emits a small-stage queue without calling the LLM.

Current deterministic patterns:

- Mini SQLite / SQL:
  - core errors/result
  - SQL lexer
  - SQL parser/AST
  - record codec
  - pager/file header
  - B+Tree leaf operations
  - B+Tree split operations
  - connection CRUD
  - CLI/README
- Redis / RESP:
  - RESP parser
  - store
  - command dispatch
  - TCP server
  - docs/process notes
- Browser/HTML:
  - app shell
  - interaction logic
  - polish/accessibility

Purpose:

- Give the supervisor a bounded work queue before asking a coder to generate code.
- Avoid 100KB+ single responses for large specs.

### 2. Executable Stage Queue

New command:

```bash
python3 local_sdlc.py run-stages "implement this SPEC" --apply
```

The command runs the synthesized queue stage-by-stage. Each stage is an independent `agent` sub-run, so PM/coder/judge separation and per-role system prompts remain intact.

Current behavior:

- infers stage writable paths from `StageWorkItem.suggested_paths`
- existing stage paths become `--include`
- missing stage paths become `--new-file`
- all stage paths become `--require-path`
- prior stage changed/required paths are passed as read-only context to later stages
- each stage writes its own `run.json`
- the top-level staged run writes `00-stage-queue.md`, `run.partial.json`, and `run.json`
- failed stages stop the queue by default
- final gates rerun configured commands and all required-path checks after all stages pass
- if final gates fail, `--final-repair-rounds` can launch a final integration repair agent
- final integration repair reads test files as read-only context instead of writable targets

Useful controls:

```bash
python3 local_sdlc.py run-stages "mini sqlite" \
  --from-stage S03 \
  --to-stage S05 \
  --stage-max-rounds 3 \
  --judge-mode command-only \
  --test-command "python3 -m unittest discover -s tests"
```

Retest finding from Mini SQLite:

- Running full `unittest discover` inside every small stage is wrong: early stages try to repair failures from later unimplemented stages.
- `run-stages` now separates checks:
  - per-stage: auto `python3 -m py_compile ...` for the stage's Python files, or explicit `--stage-test-command`
  - final gate: user `--test-command`
- The full 9-stage run completed all stages, then failed only at the final integration gate.
- The final integration repair agent still needs stronger output control. In observed runs it produced long analysis/prose instead of valid artifacts, and once attempted to rewrite tests. The runner now makes tests read-only context, but additional controls are needed.

### 3. Artifact Linter

The runner now lints coder output before applying it.

Current checks:

- If runner tests use `unittest`, generated content must not depend on:
  - `import pytest`
  - `pytest.raises`
  - `tmp_path`
- `BEGIN_SEARCH_REPLACE` with identical search and replacement is blocked.
- Unbalanced `BEGIN_FILE` / `END_FILE` markers are blocked.
- Very large search/replace blocks are warned because they are fragile.

Important detail:

- The linter checks generated file content and the REPLACE side of search/replace.
- It does not block legitimate repairs where pytest appears only in the SEARCH side.

### 4. Repair Strategy Advice

After failed command checks, the runner now creates `05-rXX-repair-advice.md` when it can infer a strategy.

Current strategies:

- `replace_test_harness`
  - Used when `unittest` is configured but pytest-specific tests are observed.
  - Advises full `BEGIN_FILE` rewrite of affected test file.
- `whole_file_or_shorter_search`
  - Used when search/replace exact matching fails.
  - Advises shorter exact snippets or full generated-file rewrite.
- `root_cause_patch`
  - Used when repeated exception patterns appear.
  - Advises fixing the shared implementation root cause first.
- `small_patch`
  - Used for direct issues such as missing imports / `NameError`.

The advice is also recorded in `run.partial.json` and `run.json` under `repair_advice`.

### 5. Regression Tests

Runner test suite now covers:

- streaming partial output
- safe extra new files
- read-only context protection
- search/replace body dedent
- artifact lint for pytest/unittest mismatch
- artifact lint allowing pytest removal in SEARCH side
- repair advice for unittest/pytest mismatch
- deterministic SQLite stage queue
- `stage-plan --format json`
- `run-stages --dry-run`
- staged execution through two independent agent sub-runs

Validation:

```bash
python3 -m py_compile local_sdlc.py tests/test_local_sdlc.py
python3 -m unittest discover -s tests
```

Result:

- 103 tests OK after the 2026-07-05 failure-transition update.

## 2026-07-05 Failure-Transition Update

The runner now makes the PM / supervisor / runner / coding-role boundary more explicit.

Implemented:

- `StageWorkItem` has `test_commands`, and automatic stage tests now run the specific stage test file pattern, for example `python3 -m unittest discover -s tests -p test_lexer.py`, instead of full discovery inside every early stage.
- Markdown fenced code blocks are treated as artifact candidates for JSON, `BEGIN_FILE`, and `BEGIN_SEARCH_REPLACE` extraction.
- Artifact-producing coder roles have a non-artifact output budget. Long prose before the first artifact marker is classified as `non_artifact_output`.
- `MISSING_CONTEXT` output is classified and safe existing files are added as read-only context for the retry.
- `failure_type` is mapped through a `FailureTransition` table. Transitions are written to `03-rXX-failure-transition.md` and `run.json`.
- `artifact_invalid`, `non_artifact_output`, and `test_edit_attempt` push the next coder prompt into stricter format-repair behavior.
- Final integration repair parses final command logs for traceback paths, failed test names, exception types, and missing symbols/modules.
- Final integration repair keeps tests as read-only evidence and excludes `tests/` from writable include paths.
- Attempts to write read-only tests are classified as `test_edit_attempt`.

Responsibility boundary:

- runner: artifact extraction, fenced artifact salvage, syntax/path validation, non-artifact classification, missing-context collection, test-edit rejection, command execution, failure transition logging.
- PM: semantic salvage and next-role instruction shaping when output intent is useful but unusable as artifacts.
- supervisor: stage selection, role routing, retry/stop decisions, final integration repair setup.
- coding roles: stage implementation, repair, final integration repair, format repair, judging.

## Remaining Work

### 1. Saved Output Replay

Needed command:

```bash
python3 local_sdlc.py replay-run --run-dir ... --apply --test-command ...
```

Purpose:

- Re-apply saved coder output after runner parser/linter improvements.
- Avoid unnecessary LLM calls.

### 2. Artifact-Complete Early Stop

Streaming showed that the model may continue after all expected artifacts are closed.

Needed behavior:

- Track expected artifacts during stream.
- Stop or close the connection once all required artifacts are complete and trailing non-artifact output begins.
- The post-generation non-artifact budget exists now; a stream-time abort guard is still worth adding.

### 3. Stronger Repair Strategy Selector

Current repair advice is document-level guidance.

Next step:

- Let the runner turn advice into stronger action:
  - force `BEGIN_FILE` for generated test harness rewrite
  - force `--small-patch` for missing import
  - force implementation files when repeated product exceptions occur
- include the failing command document near the end of the document window for integration repair
- keep tests as read-only context unless a dedicated test-harness role is explicitly selected outside final integration repair

### 4. Adaptive Stage Splitting

`run-stages` executes deterministic stages, but it does not yet split a failed stage further.

Next step:

- if a stage repeatedly times out, overflows context, or fails with unrelated broad errors, create a child stage queue
- record parent/child stage ids in manifest
- resume from the failed child stage without redoing approved parent stages

### 5. Structured Judge Output

Judge should optionally emit machine-readable metadata:

```json
{
  "failure_type": "product_code",
  "root_cause_files": ["minisqlite/storage/btree.py"],
  "repair_strategy": "small_patch",
  "do_not_edit": ["tests/test_minisqlite.py"]
}
```

This would make the supervisor less dependent on Markdown parsing.
