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
  and BudgetRemaining(goal, stage, recovery, api, wall)
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
  "command_output_bytes": 0,
  "documents_count": 19,
  "evidence_count": 6,
  "acceptance_pass_count": 4,
  "changed_paths_hash": "stable-hash",
  "evidence_hash": "stable-hash"
}
```

Wall-clock duration, status-file mtimes, and monitor writes are intentionally
not vector dimensions: observing the monitor itself must not refresh the
deadline. Thresholds are configurable and must be tuned from run evidence.

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

## Implementation Log

### 2026-07-29 P01a Cancel Token and Resume Guard

Implemented:

- `cancel.json` persistence through `request_cancel()`
- fail-closed cancel-state loading for malformed cancel files
- `agent` setup guard that refuses a cancelled run directory before PM/Coder/Judge API calls
- action-boundary guards before agent API calls, artifact apply, executable checks, and copy-back
- `run-stages` parent-run guards before stage start, final checks, and integration repair
- Web stop writes `cancel.json` to both the web job log directory and the inferred run directory
- Web-created agent/supervisor/run-stages jobs receive an explicit run directory so cancellation and partial progress can be correlated before final stdout appears

Verified:

- `test_request_cancel_writes_cancel_json`
- `test_agent_refuses_cancelled_resume_before_llm_call`
- full test suite: `python3 -m unittest tests.test_local_sdlc`

Still open at this slice (completed by the 2026-08-01 P01/P02 slice):

- append-only `progress.jsonl`
- proof that no progress event after cancellation starts work
- direct tests for cancelled `run-stages` stage start, final command, and copy-back boundaries

### 2026-07-29 P01b Progress Event Proof

Implemented:

- append-only `progress.jsonl`
- `record_work_start()` for action-boundary work-start events
- `work_starts_after_cancel()` to mechanically detect any work-start event after the cancel sequence
- `agent` manifests include `progress_log` and `progress_event_count`
- parent `run-stages` records stage/final-check work-start events through the same mechanism

Verified:

- `test_work_start_progress_is_blocked_after_cancel`
- `test_agent_refuses_cancelled_resume_before_llm_call`
- `test_run_stages_refuses_cancelled_run_before_stage_agent_call`

Still open at this slice (completed by the 2026-08-01 P01/P02 slice):

- direct tests for cancelled `run-stages` final command and copy-back boundaries
- optional Web integration test proving stop writes cancel before a later resume attempt

### 2026-07-29 P02a Command Safety Gate

Implemented:

- `SafetyDecision` domain record
- append-only `safety_decisions.jsonl`
- command safety classification into `allow`, `require_approval`, or `block`
- `run_checked_command()` records a SafetyDecision before executing allowed commands
- approval-required and blocked commands return a blocked command document without subprocess execution
- agent/run-stages command execution paths pass their run directory to the safety gate
- agent/run manifests include `safety_decisions_log` and `safety_decision_count`

Verified:

- `test_run_checked_command_records_allowed_safety_decision`
- `test_run_checked_command_records_approval_required_safety_decision`
- `test_run_checked_command_records_blocked_safety_decision`
- `test_run_checked_command_requires_approval_for_risky_class_without_legacy_block_reason`
- `test_agent_applies_patch_and_runs_test_command`

Still open at this slice (completed by the 2026-08-01 P01/P02 slice):

- approval-token model for explicit human approval
- safety decisions for artifact apply, copy-back, service control, Docker control, git operations, and network exposure
- Web UI display for `APPROVAL_REQUIRED` and `SAFETY_BLOCKED`

### 2026-08-01 P01/P02 Complete Action Gate

Implemented:

- one `begin_action()` boundary with the invariant `cancel check -> persisted SafetyDecision -> work_start -> execution`
- file-lock serialization between cancellation and work-start so their order is mechanically decidable under races
- parent control scopes for staged runs; a parent cancellation blocks every later child-stage action
- action guards for API calls, commands, resume/retry/stage boundaries, artifact/patch apply, worktree creation, and copy-back
- queued Web stop persists cancellation before a child process exists; a running Web process is terminated after cancellation is recorded
- `action_gate_audit()` detects both post-cancel work and work without an authorizing prior SafetyDecision
- explicit one-time human approvals in `safety_approvals.jsonl`; approval is fingerprint-bound and consumed atomically by one matching retry
- `block` decisions cannot be approved, and `llm` is not an accepted approval source
- CLI `cancel`, `safety-status`, and `approve` commands
- Web states for `APPROVAL_REQUIRED` / `SAFETY_BLOCKED` and an explicit one-time approval button
- child-stage approval requirements propagate to the parent run manifest and Web result without entering coder retry loops
- `block` is a terminal `SAFETY_BLOCKED` state, not an ordinary test failure or coder-retry trigger; child blocks propagate to parent, CLI, and Web
- unregistered future risk classes fail closed to `require_approval` instead of silently defaulting to `allow`
- Web approval targets are restricted to the job run directory and its child stages

Verified:

- cancellation is absorbing across every declared autonomous action class
- cancellation/work-start race tests preserve the absorbing invariant
- cancellation between coder output and artifact apply prevents mutation
- cancellation after isolated verification prevents copy-back
- risky commands do not start before approval; one approval authorizes exactly one matching retry
- parent and child stage manifests preserve approval-required state and the exact approval target
- blocked commands terminate before coder retry, and parent/child manifests preserve the blocking decision
- focused P01/P02 suite passes, including CLI and Web control paths

P01 and P02 are complete. Later propositions must call `begin_action()` rather
than adding an independent execution preflight.

### 2026-08-01 P09 Persistent Runtime Budget Gate

P09 models one autonomous run with the usage vector:

```text
U = (G, S, R, A, T)
L = (Gmax, Smax, Rmax, Amax, Tmax)

Admit(action) :=
  not CancelRequested
  and SafetyAllowed(action)
  and U_counted + Charge(action) <= L_counted
  and elapsed_wall_time < Tmax
```

`G` is every goal-level action, `S` every action in a direct or child stage,
`R` every recovery/retry/resume/stage-split start, `A` every LLM API action,
and `T` elapsed wall-clock time. A recovery or API action also consumes its
containing goal/stage action count; the dimensions are independent upper
bounds, not mutually exclusive categories.

Implemented invariants:

- the Action Gate order is `cancel -> persisted SafetyDecision -> consumed budget -> work_start -> execution`
- a cancelled or safety-denied action consumes no budget
- `budget.json` fixes the run policy, `budget_events.jsonl` is the append-only ledger, and `budget_stop.json` is the canonical stop reason
- `budget_stop.json` is absorbing; reinitializing or resuming the same run cannot raise limits or clear the stop
- a child stage consumes its local stage budget and parent goal budget under ordered file locks; evaluation completes before either ledger is changed
- any child or parent exhaustion is propagated to both scopes and becomes parent status `budget_exhausted`
- concurrent starts cannot exceed a count limit; denied actions never receive a `work_start`
- wall-clock remaining time bounds command and LLM request timeouts; an in-flight deadline overrun persists `exhausted_during_action`
- direct `agent`, `supervise`, and `supervisor` runs use a combined goal/stage scope; `run-stages` uses a parent goal scope plus one stage scope per child
- CLI flags and the Web advanced settings configure all five limits; `budget-status`, run manifests, and Web results expose usage, remaining capacity, and stop reason

The default limits are intentionally finite but generous: 1000 goal actions,
200 actions per stage, 100 recovery actions, 250 API calls, and 86400 seconds.
Existing algorithm-local limits such as `--max-rounds` remain in force beneath
P09; they do not replace the cross-cutting runtime budget.

Verified by `tests.test_budget.RuntimeBudgetTests`, including exact-limit,
absorbing-stop, independent dimensions, cancel/safety precedence, injected
wall deadlines, in-flight command timeout, parent/child propagation,
20-thread start races, immutable resume policy, Action Gate audit, agent API
cutoff, staged propagation, CLI reporting, and Web reporting.

### 2026-08-01 P03 Persistent No-Progress Monitor

P03 uses a deliberately small, canonical progress vector `V(t)`. Let `H` be a
stable JSON hash and `Imax` the immutable idle threshold for one run:

```text
Changed(t) := H(V(t)) != H(V(last))

last_progress(t) :=
  t             if Changed(t)
  last_progress otherwise

STALLED(t) := t - last_progress(t) >= Imax
```

The vector admits only observable workflow dimensions: stage/function/round,
LLM stream bytes and chunks, command output bytes, document/evidence counts,
acceptance progress, and stable hashes of changed paths or evidence. Elapsed
duration, status-file mtimes, action UUIDs, and the monitor's own writes are
excluded, because a self-generated heartbeat must not prove progress.

P03 is liveness detection, not repeated-failure diagnosis. A changing LLM
stream is live even when the semantic quality is not yet known; P05 separately
decides whether completed attempts remain on the same failure plateau.

Implemented invariants:

- `progress_policy.json` fixes `max_idle_seconds`; the default is 900 seconds
- `progress_state.json` stores the latest canonical vector and last meaningful change
- `stall.json` is the canonical, persistent `STALLED` reason and evidence
- identical observations do not refresh `last_progress_at`
- stream bytes/chunks refresh the deadline, including Supervisor calls that do not provide a runner-specific callback
- quiet non-stream API calls and commands are bounded by the smaller of wall-budget remaining time and no-progress remaining time
- the Action Gate performs an early stall check and an atomic stall recheck/work-start under the progress lock
- if STALLED wins after budget admission but before work-start, the unused budget charge is refunded
- child-stage stall and parent-goal propagation occur under ordered progress locks
- Action Gate audit detects any legacy work-start after the persisted stall sequence
- CLI `--max-idle-seconds` and `progress-status`, Web advanced settings, manifests, and Web job results expose the same state
- P03 does not invent a recovery action; ordinary resume/retry cannot clear `STALLED`

Verified by `tests.test_progress_monitor.ProgressMonitorTests`, including exact
threshold behavior, volatile-field rejection, stream deadline refresh,
absorbing stop, budget refund at a stall race, parent/child propagation,
post-stall audit, immutable policy, quiet-command interruption, agent API
interruption, staged promotion, CLI reporting, and Web reporting.

### 2026-08-01 P04/P05 Evidence-bound Stalled Recovery

P04 treats recovery as a new evidence-bound run, never as deletion of the
source `STALLED` state. For a source run `S`, plan `R`, and target run `T`:

```text
ValidRecovery(R, S, T) :=
  status(R) = RECOVERY_PLANNED
  and source(R) = S
  and target(R) = T
  and T != S
  and H(stall.json(S)) = R.source_stall_sha256
  and id(R) = H(S, stall_hash, strategy, target_profile, T)

AdmitRecovery(R) :=
  ValidRecovery(R, S, T)
  and not CancelRequested(S)
  and SafetyAllowed(R)
  and RecoveryBudgetRemaining(S)
  and ValidRecovery(R, S, T) immediately before work_start
```

The second validation closes the interval between admission and work-start.
If the persisted stall evidence changes in that interval, no work starts and
the reserved budget charge is refunded. A metadata flag without a matching
persisted plan and digest is never authorization.

P05 uses only the newest consecutive normalized failure family `F`; a generic
failure type cannot stand in for a project-specific family:

```text
Plateau(F, n) := F != empty and ConsecutiveNewest(F) >= n

Strategy :=
  failure_analysis     if Plateau and no completed analysis for F
  root_cause_recovery  if Plateau and a completed analysis for F exists
  requested strategy   otherwise
```

Implemented invariants:

- `recovery_plan.json` is immutable for one stall digest and fixes source,
  target, strategy, target profile, failure evidence, and causal event
- the source remains `STALLED`; ordinary source actions and simple resume stay
  blocked while the authorized target receives a fresh progress scope
- source cancellation and recovery budget are inherited by the target, so a
  new directory cannot reset user control or autonomous-loop limits
- resume, retry, split, and profile switch are ordinary strategies only before
  a same-family plateau is proven
- profile switch plans enforce the planned model profile; a conflicting CLI
  profile is rejected before creating the target directory or calling the LLM
- failure analysis is an independent API call and precedes root-cause artifact
  generation; its reasoning is handed off through persisted documents
- completed analyses are recognized across same-family resume ancestry, while
  a different family terminates the sequence
- `RECOVERY_PLANNED`, `RECOVERY_STARTED`, and `RECOVERY_COMPLETED` use the
  canonical transition gateway and carry causation identifiers

Verified by `tests.test_recovery_runtime.RecoveryRuntimeTests` together with
P01/P02/P03/P09 focus suites and the complete project test suite.
