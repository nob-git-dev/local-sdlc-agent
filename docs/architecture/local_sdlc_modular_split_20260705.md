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
| `local_sdlc/artifacts.py` | compatibility facade, linting, stream guards, semantic probes, repair advice |
| `local_sdlc/verification.py` | command execution, command result documents, smoke checks, evidence helpers |
| `local_sdlc/stages.py` | deterministic stage queue, stage-specific paths/tests, acceptance summaries |
| `local_sdlc/stage_runner.py` | `stage-plan` and `run-stages` command execution |
| `local_sdlc/routing.py` | supervisor task classification, phase routing, judge approval parsing |
| `local_sdlc/supervisor_runner.py` | `supervisor` and legacy `supervise` command execution |
| `local_sdlc/phase_runner.py` | `spec`, `phase`, and `implement` command execution |
| `local_sdlc/agent_runner.py` | application-level coding agent loop |
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
