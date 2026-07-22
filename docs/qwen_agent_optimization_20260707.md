# Qwen Agent Optimization 2026-07-07

## Purpose

Make Qwen3.5-122B runs reproducible and comparable for the local SDLC coding
agent, then verify whether the previous Mini SQLite S02 lexer failure improves.

## Runner changes

- Added `--model-profile qwen-agent`.
- Added `--model-profile qwen-agent-deep` as a separate experimental profile
  where only non-artifact diagnostic calls may use thinking.
- Added `--model-profile ornith-agent` and `--model-profile ornith-agent-deep`
  so Qwen/Ornith comparisons can switch one preset instead of rewriting many
  CLI flags.
- Model profiles now carry a default model name. `--model` still overrides the
  preset default.
- `--api-profile` now accepts `model=...`, allowing one function/API call type
  to use a different model, for example:

```text
--api-profile failure_analysis:model=Ornith-1.0-35B,max_tokens=9999,temperature=0,thinking=off
```

- Added model profile settings to `doctor` output and agent `run.json`.
- Added `compare-runs` to compare stage outcomes across run directories.
- Added unittest timeout localization: if `python3 -m unittest discover ...`
  times out, the runner reruns individual test methods with short timeouts and
  appends the hanging method evidence to the command document.

## Profile precedence

The effective API call setting is resolved in this order:

1. built-in defaults,
2. selected `--model-profile` default model and function profile,
3. explicit global `--model`,
4. role-level CLI overrides,
5. `--api-profile FUNCTION:...` overrides.

This keeps common model families as named presets while preserving future
mixed-model operation where a planner, judge, failure analyzer, and artifact
writer may use different models.

## Qwen-agent profile intent

Qwen can produce strong output, but this agent's risk is long reasoning or
narration leaking into executable artifacts. The strict Qwen profile therefore:

- keeps all artifact-producing calls with thinking off,
- lowers artifact temperatures,
- gives diagnostic functions larger structured-output budgets,
- reduces repair artifact budgets compared with full generation to encourage
  smaller patches.

## Verification

Commands:

```text
python3 -m py_compile local_sdlc.py local_sdlc/*.py
python3 -m unittest tests/test_local_sdlc.py
```

Both passed after the implementation.

## Mini SQLite comparison

Baseline Qwen no-stream resume:

```text
benchmarks/qwen-mini-sqlite-full-20260707-074427/.sdlc-runner/runs/qwen-full-s02-s09-nostream
```

- status: `stage_failed`
- completed: `0/8` approved stages
- failed stage: `S02`
- failure type: `timeout`
- api calls: `12`

Optimized Qwen profile:

```text
benchmarks/runs/mini-sqlite/qwen-mini-sqlite-optimized-20260707-185930/.sdlc-runner/runs/qwen-agent-s01-s02
```

- status: `approved`
- completed: `2/2` approved stages
- S01: approved, 4 API calls
- S02: approved, 7 API calls
- profile: `qwen-agent`

The optimized run changed the S02 failure shape from repeated unittest timeout
to a local, repairable lexer error (`*` and `-` token handling). The agent then
fixed the lexer and passed all 46 lexer tests in round 3.

Comparison command:

```text
python3 local_sdlc.py compare-runs \
  benchmarks/qwen-mini-sqlite-full-20260707-074427/.sdlc-runner/runs/qwen-full-s02-s09-nostream \
  benchmarks/runs/mini-sqlite/qwen-mini-sqlite-optimized-20260707-185930/.sdlc-runner/runs/qwen-agent-s01-s02
```

Observed table:

```text
| run | status | stages | failed | failure | api | profile |
| --- | --- | --- | --- | --- | --- | --- |
| .../qwen-full-s02-s09-nostream/run.json | stage_failed | 0/8 | S02 | timeout | 12 | - |
| .../qwen-agent-s01-s02/run.json | approved | 2/2 | - | - | 11 | qwen-agent |
```

## Next step

Resume the optimized project from `S03` using `--model-profile qwen-agent`. If
S03 introduces parser or upstream lexer regressions, compare its failure family
against the S02 improvement before changing the profile again.
