# Agent Repair Control Plan - 2026-07-06

## Decision

The eight proposed repair-loop improvements should all be implemented. There is no strong technical reason to stop at the first three. The main risk is attribution loss: if all controls are changed and a later benchmark improves or regresses, it becomes harder to know which control caused the change.

Therefore the implementation policy is:

1. Implement all eight improvements.
2. Keep each improvement observable in run artifacts and `run.json`.
3. Preserve mechanical enforcement in the runner; LLM roles may classify and plan, but must not directly apply edits.
4. Run unit tests after each structural change, then rerun staged benchmarks separately.

## Priority Controls

1. Artifact-only generation enforcement
   - Artifact-writing calls must output only `BEGIN_SEARCH_REPLACE` or unified diff for repair patches.
   - If a planning role returns `missing_context`, artifact writing stops before code generation.

2. Tests as readonly evidence by default
   - After repair advice for product-code repair, test files are frozen as readonly evidence.
   - Test editing is allowed only through explicit test-harness ownership triage.

3. Failure analysis emits required paths
   - `failure_analysis.next_required_action` now requires:
     - `required_paths`
     - `readonly_paths`
     - `forbidden_paths`
     - `next_patch_type`
     - `minimal_patch_goal`

## Function-Split Flow

Root-cause repair now follows a narrower chain:

```text
failure_analysis
  -> root_cause_analysis
  -> patch_planner
  -> artifact_writer
  -> artifact_normalizer / runner checks
  -> command verification
```

`patch_planner` produces one bounded `PATCH_PLAN`. `artifact_writer` receives that plan as a binding proposition and writes only the executable artifact. The runner parses the plan and mechanically updates writable/readonly path policy before the artifact writer is called.

## API Profile Policy

Current default function profile policy:

- `failure_analysis`: temperature 0.0, thinking enabled
- `root_cause_analysis`: temperature 0.0, thinking enabled
- `patch_planner`: temperature 0.0, thinking enabled
- `project_policy_triage`: temperature 0.0, thinking enabled
- `artifact_writer`: temperature 0.0, thinking disabled
- `generate_artifact` / `repair_artifact`: low temperature, thinking disabled
- `format_repair` / `semantic_repair`: temperature 0.0, thinking disabled

Rationale:

- Diagnostic and planning roles may use hidden reasoning because they output bounded propositions, not executable code.
- Artifact-writing roles must keep thinking disabled so reasoning text cannot leak into patches.

## Mathematical Contract

Let:

- `F_t` = current failure signature
- `A_i` = previous attempted action
- `H_i` = hypothesis behind `A_i`
- `P` = patch plan proposition
- `W` = writable product path set
- `R` = readonly evidence path set
- `B` = forbidden path set

Required transition:

```text
same(F_i, F_t) && applied(A_i) => reject(H_i)
valid(P) => required_path(P) in W && forbidden_paths(P) subset R
patch_type(P) = missing_context && required_path(P) = none => stop_before_artifact_writer
artifact_writer_input = P + visible file context + policy(W, R)
artifact_writer_output must be artifact_only
```

This keeps LLM judgment useful for ambiguous classification while preserving deterministic enforcement at the application boundary.

## Verification

Unit tests after this change:

```text
python3 -m unittest tests.test_local_sdlc
Ran 191 tests in 1.930s
OK
```

## Next Benchmark Step

The next Mini SQLite run should use the staged resume path, preferably from `S03`, to learn from later-stage failures without repeatedly spending budget on already validated early-stage behavior.
