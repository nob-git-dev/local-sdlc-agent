# Autonomous Supervisor Runtime and Anti-overfitting Specification

## Purpose

The agent must eventually complete a task or stop with a precise, actionable
blocked reason without depending on a stronger outside agent to notice that it
has stalled.

The runtime must improve completion power without becoming a system that
persistently repeats unsafe actions. Safety and user cancellation are higher
priority than completion.

## Core Concept

The system is a closed control loop:

```text
Goal
  -> Stage
  -> Action
  -> Observation
  -> Evidence
  -> Verdict
  -> Next Action
```

The loop is valid only while all execution predicates hold:

```text
Executable(A) :=
  not CancelRequested(goal)
  and SafetyAllowed(A)
  and BudgetRemaining(goal, stage)
  and HasRequiredContext(A)
```

The supervisor does not trust the child agent to observe its own stall. Stall
detection belongs to the parent runtime.

## Proposition Layers

The proposition set is intentionally two-layered:

```text
P = P_core union P_discovered
```

`P_core` contains invariants that are already stable enough to fix in SPEC.md.
`P_discovered` contains propositions learned during implementation,
benchmarking, and failure analysis.

The project must not claim that the initial proposition set is complete.
Instead, it must define how new propositions are admitted.

## Core Propositions

| ID | Proposition | Truth Condition |
|---|---|---|
| C01 | Safety precedes completion. | A blocked or approval-required safety decision prevents execution even when it would improve completion probability. |
| C02 | Cancel is absorbing for new work. | After cancellation, no new API call, command, resume, retry, stage split, or copy-back starts. |
| C03 | LLMs do not approve risky actions. | Human approval or explicit policy is required for risky action execution. |
| C04 | Every action passes Safety Gate first. | Each action has a persisted SafetyDecision before execution. |
| C05 | Completion is evidence-based. | Completion requires passing acceptance evidence, not natural-language self-report. |
| C06 | Blocked is explicit. | Blocked states include reason, evidence, and next human input required. |
| C07 | Autonomous loops are bounded. | Goal, stage, recovery, API call, and wall-clock budgets are enforced. |
| C08 | Decisions are auditable. | Progress, safety decisions, recovery plans, and final verdicts are persisted. |

## Acceptance Propositions

| ID | Proposition | Required Evidence |
|---|---|---|
| P01 | Cancel prevents future work. | `cancel.json` exists and no later progress events start new work. |
| P02 | Risky action requires approval or blocks. | `safety_decisions.jsonl` records `require_approval` or `block`; no command execution record exists before approval. |
| P03 | No progress becomes stalled. | Progress vector is unchanged for the configured threshold and state becomes `STALLED`. |
| P04 | Stalled can recover when recovery is valid. | `recovery_plan.json` exists and the next run records `resumed_from`, retry, split, or profile switch. |
| P05 | Same failure plateau changes strategy. | Repeated failure family transitions to failure analysis or root-cause recovery, not ordinary retry. |
| P06 | Malformed artifact streams stop early. | Stream guard abort evidence exists and next action is format repair or blocked. |
| P07 | Completion requires acceptance pass. | Acceptance matrix contains no fail or unverified blockers. |
| P08 | Blocked is actionable. | Final manifest includes `blocked_reason`, evidence paths, and required human input. |
| P09 | Budgets stop runaway loops. | Budget counters reach a limit and the runtime stops with a budget reason. |
| P10 | Autonomous edits are isolated by default. | Worktree copy mode is used and only approved changed paths are copied back. |
| P11 | Discovered propositions require admission metadata. | Evidence, scope, counterexamples, rationale, and regression tests are present. |
| P12 | Benchmark-specific rules do not overfire. | A generalization regression shows that unrelated tasks do not trigger the rule. |

## Progress Vector

The first implementation should keep the vector small and observable:

```json
{
  "stage_id": "S03",
  "round": 2,
  "state": "RUNNING",
  "api_calls": 5,
  "current_function": "generate_artifact",
  "stream_bytes": 12345,
  "stream_chunks": 88,
  "reasoning_chunks": 12,
  "documents_count": 19,
  "evidence_count": 6,
  "changed_paths_hash": "stable-hash",
  "partial_manifest_mtime": 1234567890.0,
  "output_log_mtime": 1234567890.0
}
```

The exact threshold values are not fixed in this concept spec. They must be
configurable and tuned by tests.

## State Machine

```text
PLANNED
  -> RUNNING
  -> PROGRESSING
  -> STALLED
  -> RECOVERY_PLANNED
  -> RESUMED
  -> VERIFYING
  -> COMPLETED

RUNNING -> USER_CANCELLED
RUNNING -> SAFETY_BLOCKED
RECOVERY_PLANNED -> APPROVAL_REQUIRED
APPROVAL_REQUIRED -> RESUMED
APPROVAL_REQUIRED -> USER_CANCELLED
```

`STALLED`, `APPROVAL_REQUIRED`, and `SAFETY_BLOCKED` are not vague failures.
They are explicit states with evidence.

## Safety Decisions

Each action is classified before execution:

```text
allow
allow_in_worktree
require_approval
block
```

Risk classes:

```text
read_only
project_write
generated_code_execution
filesystem_delete
git_history_rewrite
privileged_command
service_control
docker_control
database_write
production_like
network_exposure
secret_access
```

The initial implementation may support only a subset, but unsupported risky
classes must default to `require_approval` or `block`, not `allow`.

## Anti-overfitting Governance

The project should borrow the general idea of anti-overfitting from machine
learning, without pretending that the same math directly solves software
design.

The equivalent practices are:

- Keep training examples and validation examples separate: a rule learned from
  Tetris, Mini SQLite, or Redis must be checked against an unrelated task before
  it becomes a general rule.
- Prefer simple rules: a rule with fewer project-specific conditions is favored
  when it explains the same evidence.
- Record scope: every discovered rule must say where it applies and where it
  does not apply.
- Keep holdout regressions: unrelated small tasks are used to detect overfiring.
- Use ablation: when a new heuristic is added, compare behavior with and without
  it on at least one previous failure and one unrelated task.
- Separate mechanism from policy: universal safety invariants live in the
  runner; project-dependent judgment is represented as policy or triage data.

## Discovered Proposition Admission Rule

A discovered proposition may be promoted only when it has:

```text
id
text
source_run
supporting_evidence
scope
counterexamples
generalization_rationale
regression_tests
owner
expiry_or_review_condition
```

If any field is missing, the proposition remains a local observation and must
not become a core runner rule.

## Development Loop

Implementation proceeds by proposition, not by broad feature names:

```text
1. Select one proposition.
2. Write a failing test or fixture that falsifies it.
3. Implement the smallest mechanism that makes the proposition true.
4. Persist evidence in run artifacts.
5. Run existing tests and one overfitting guard where applicable.
6. Update SPEC.md or a discovered-proposition record.
7. Move to the next proposition.
```

Recommended order:

1. P01 cancel prevents future work
2. P02 risky action requires approval or blocks
3. P09 budgets stop runaway loops
4. P03 no progress becomes stalled
5. P04 stalled can recover
6. P05 same failure plateau changes strategy
7. P06 malformed artifact streams stop early
8. P07 completion requires acceptance pass
9. P08 blocked is actionable
10. P10 autonomous edits are isolated
11. P11 discovered proposition admission
12. P12 overfitting regression

P01 and P02 come first because autonomous recovery without cancellation and
safety boundaries would increase risk before increasing reliable completion.

## Non-goals

- Do not make a benchmark-specific agent that only solves Tetris, Mini SQLite,
  or Redis.
- Do not let the LLM approve risky operations.
- Do not add model-specific behavior as a universal rule without scope and
  regression evidence.
- Do not treat a long-running thought process as failure when progress evidence
  is still changing.
- Do not treat a passing command as completion unless acceptance evidence also
  passes.

## First Implementation Slice

The first vertical slice should cover:

- `CancellationToken`
- `SafetyDecision`
- `safety_decisions.jsonl`
- one CLI or Web stop path that creates cancel state
- command/action preflight that refuses new work after cancel
- tests for P01 and P02

No autonomous retry is allowed in the first slice. Recovery comes after the
system can stop safely.
