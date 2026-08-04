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
| P13 | A behaviorally worse candidate is rejected reversibly. | With an unchanged command vector, a higher failure score causes exact pre-round restoration, verified byte equality, a persisted `candidate_regression`, and a bounded different retry. |
| P14 | Recovery does not forget hard evidence. | A strategy change preserves prior mechanical constraints and owner-file focus; the next manifest contains both rollback evidence and the earlier probe facts. |
| P15 | A mechanically absent API cannot be introduced as an unresolved call. | Typed-receiver lint rejects a call when the probed owner lacks the method and the same candidate transaction does not define it on that owner. |
| P16 | Periodic artifact runaway changes protocol before size exhaustion. | A repeated multi-line block is classified as `stream_repeated_text_runaway` before the generic byte limit, and the next bounded retry requires one JSON search/replace envelope. |
| P17 | A newly executable test harness may expose more failures without being a regression. | In copy-worktree mode only, the missing required-path set strictly shrinks, a changed `tests/` path becomes present, the candidate is marked `candidate_provisional_progress`, and no copy-back occurs before all gates pass. |
| P18 | Parent recovery follows the terminal child failure. | The stage summary prefers `final_failure_type`, preserves the earlier acceptance failure as evidence, and admits candidate-derived focus paths only inside the declared repair scope. |
| P19 | A product-impossible generated-test conflict requires independent authorization. | An exact planner pair (`patch_type=missing_context`, `escalation=generated_test_oracle_triage`) triggers primary-evidence policy triage; only a medium/high-confidence `test_harness` verdict can make the intersecting stage-owned failing test paths writable. |
| P20 | Semantic context shortage is not an artifact transport failure. | Bare `missing_context` never selects format repair; only explicitly prefixed format/semantic repair protocol failures enter protocol recovery. |
| P21 | Candidate quality is compared on one stable verification vector. | Initial and repair checks contain the same required-path, smoke, and configured-command identities; the derived acceptance gate is excluded from the failure score. |
| P22 | A recovery protocol controls both prompting and parsing for its round. | A JSON recovery contract parses JSON even under a legacy parent profile, while a marker-only recovery contract does not silently accept JSON. |
| P23 | A planner cannot mint write authority. | A missing path resolves only when it is already present in the runner-authorized writable set; an undeclared path remains unresolved. |
| P24 | An absent declared generated test is a harness-construction obligation. | Declared stage ownership, path absence, and zero-test discovery route to `create_test_harness` before product-only root-cause planning. |
| P25 | Unsupported artifact JSON stops at schema identification. | Once the first top-level key is known and is neither `artifacts` nor `type`, streaming aborts with `stream_json_schema_mismatch`. |

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

### 2026-08-03 P06-P10 Supervisor-owned Autonomy Loop

The parent staged runtime now owns reversible implementation decisions that
were previously made by an outside operator. The decision boundary is fixed as:

```text
HumanRequired(d) :=
  SpecConflict(d)
  or ExternalValueChoice(d)
  or IrreversibleHighImpact(d)
  or ExternalResourceRequired(d)
  or BudgetExtensionRequired(d)

Autonomous(d) := Internal(d) and Reversible(d) and not HumanRequired(d)
```

Task sizing, artifact-format repair, missing-context collection, repeated
failure analysis, generated-test provenance, safe evidence collection, model
function routing, and acceptance closure are internal decisions. They must not
be sent to a person merely because the runtime has reached a failed attempt.

The parent recovery selector is bounded and changes strategy:

```text
Recover(stage, failure, history) :=
  format_repair       if ArtifactProtocolFailure and not Tried(format_repair)
  split_stage         if WritablePathCount > 1 and not Tried(split_stage)
  root_cause_recovery if a smaller split is unavailable
  fail_closed         if RecoveryBudgetExhausted
```

Persistent `STALLED` goal runs remain immutable evidence. The CLI parent
creates an evidence-bound recovery plan and starts a new parent run rather than
deleting `stall.json` or reviving the old liveness clock.

Completion is decided only by the goal-level gate:

```text
Complete :=
  every acceptance item has status=pass
  and no pending or blocked safety decision exists
  and no budget stop exists
  and no persistent stall exists
```

An acceptance item with no mechanically mapped coverage is still a blocker.
The former behavior, where unmapped conditions could be ignored, is retained
only inside stage-local repair and cannot authorize goal completion.

The evidence mapping and the executable vector are conjunctive gates. A mapped
requirement cannot cancel a failed command from the same verification cycle:

```text
ExecutableGatePass := ForAll(current_command, status = PASS)
CompletionGatePass := AcceptanceMatrixPass and ExecutableGatePass
```

When root-cause analysis produces a patch plan, the plan remains binding until
one candidate satisfies every named obligation. A separate judge API call
reviews the candidate before application:

```text
PlanConformant(candidate, plan) :=
  ForAll(obligation in plan,
    candidate_evidence(obligation)
    and not counterexample(candidate, obligation))
```

Malformed reviews, evidence-free approval, unresolved obligations, and missing
review context fail closed. The reviewer can recommend repair or context
collection but cannot apply a patch. A rejected candidate is never written to
the worktree; the supervisor retains the same plan and requests a replacement
artifact from a fresh coder API call.

Standalone worktree recovery also restores its change ledger mechanically.
Only declared writable files whose bytes differ from the original project are
seeded as resumed baseline changes, and they are copied back only after the
ordinary completion and safety gates pass.

`BLOCKED` is reserved for a real human decision boundary. Its manifest must
contain `blocked_reason`, `supporting_evidence`, and
`required_human_input`. Internal recovery exhaustion is `fail_closed`, not a
manufactured request for a person to choose an implementation tactic.

Autonomous staged work now defaults to isolated copy-worktree execution. A
child run copies back only paths from an approved attempt.

#### External intervention migration map

The 18 operator decisions observed during the DeepSeek Mini SQLite run are
represented as general runtime responsibilities, not SQLite-specific rules:

| # | Former external decision | Runtime owner |
|---:|---|---|
| 1 | Start the next bounded stage | stage queue controller |
| 2 | Reduce an oversized task | stage-plan contract and pre-splitter |
| 3 | Split a failed multi-path stage | parent recovery selector |
| 4 | Preserve successful earlier work | isolated copy-back and prior-path context |
| 5 | Resume from failed attempt evidence | child run resume contract |
| 6 | Detect malformed artifact output | stream guard and artifact lint |
| 7 | Force a stricter artifact format | format-repair transition |
| 8 | Avoid repeating an identical patch | failure-family/root-cause transition |
| 9 | Add explicitly requested missing context | agent context collector |
| 10 | Distinguish product failure from generated-test error | project-policy triage |
| 11 | Keep fixed tests read-only | artifact path policy |
| 12 | Reject unsafe or risky probes | Action Gate safety policy |
| 13 | Select a safe replacement check | executable evidence planner |
| 14 | Re-run the fixed final suite | goal acceptance gate |
| 15 | Repair final integration failure | S99 bounded integration repair |
| 16 | Re-run evidence after final repair | post-repair acceptance cycle |
| 17 | Refuse completion with evidence gaps | strict completion predicate |
| 18 | Escalate only true human decisions | HumanRequired policy and actionable BLOCKED state |

Generated-test assertions enter the evidence graph as
`provisional_test_oracle`, not as binding product contracts. Ownership triage
must use the fixed specification, complete generated-test source, executable
command evidence, and an explicit independent Judge vote. Repair advice and
failure-analysis conclusions under review are excluded from that packet. A
product or test verdict is actionable only when its selected hypothesis has
positive evidence and the path Action Gate accepts the exact owner. Conflicting
Judge and triage votes invoke a separate bounded `policy_arbitration` API call;
an invalid arbitration fails closed without granting write authority.

Every Supervisor choice is appended to `autonomy_decisions.jsonl`. The final
manifest includes an audit with `unauthorized_external_intervention_count`.
Stage-level `api_profile` entries are executable function overrides, not labels;
the stage-plan boundary validates every entry as `function:key=value,...`
before any child agent or LLM API call starts.
They are workload defaults. Explicit runtime `--api-profile` entries are
applied afterward and therefore win for the same function, allowing
model-specific tuning without rewriting the product specification.

#### Candidate regression control

Let `W(i-1)` be the workspace immediately before round `i`, `A(i)` the
candidate artifact transaction, and `s(F)` the total unittest failures and
errors observed by the unchanged command vector.

```text
CandidateRegression(i) :=
  Applied(A(i))
  and SameCommandVector(i-1, i)
  and s(F(i)) > s(F(i-1))

AdmitNextRound(i) :=
  CandidateRegression(i)
  and Restore(W(i-1))
  and ByteEqual(CurrentWorkspace, Snapshot(i-1))
  and AdaptiveBudgetRemaining
```

When `CandidateRegression(i)` is true, the candidate is evidence, not retained
product state. The runtime records its paths and failure signatures, restores
the complete artifact transaction, verifies exact bytes, and tells the next
planner not to repeat the rejected approach or assume an unobserved interface.
The LLM cannot waive restoration. A mismatch after restoration is terminal
`rollback_verification_failed`; it never reaches Judge or copy-back.

A zero-test or missing-file baseline can make a useful test-harness candidate
look numerically worse. Let `M(i)` be the missing required-path set and `C(i)`
the paths changed by the candidate:

```text
ProvisionalHarnessProgress(i) :=
  CandidateRegression(i)
  and CopyWorktree(i)
  and M(i) proper-subset-of M(i-1)
  and Exists(p, p in (M(i-1) - M(i)) and p in C(i) and TestPath(p))

AdmitWithoutCopyBack(i) :=
  ProvisionalHarnessProgress(i)
  and Quarantine(CurrentWorkspace)
  and FreezeExistingGeneratedTestsAsEvidence

CopyBack(i) := AllExecutableGatesPass(i)
```

Missing test paths remain writable until materialized. Once present, generated
tests become read-only evidence for the next round. This exception cannot run
in an in-place worktree and cannot waive the final executable gate.

Recovery knowledge is monotone for mechanically established facts. Let
`K(i)` be the retained constraints before round `i` and `M(i)` be new
mechanical evidence:

```text
K(i+1) := K(i) union M(i) union RegressionFacts(i)

ResolvedAbsentCall(A, C.m, owner) :=
  CallsTypedReceiver(A, C.m)
  implies Defines(owner, C.m) or DefinesInSameTransaction(A, owner, C.m)

Admissible(A) :=
  ArtifactWellFormed(A)
  and ForAll(absent_api, ResolvedAbsentCall(A, absent_api))
```

A rollback may replace the selected repair strategy, but it may not remove
`K(i)`. Mechanical API owner paths are prioritized in retry context. If a
candidate calls a method proved absent through a receiver visibly bound to its
class, the artifact is rejected before apply unless the owner already defines
the method or the same transaction defines it on that owner. Unknown receiver
types are not guessed by lint and remain subject to executable tests.

For streamed output, a periodic repeated block is a distinct failure family
from a legitimately large artifact:

```text
PeriodicRunaway(S) := Exists(block, repeat_count >= threshold)

PeriodicRunaway(S)
  -> Abort(stream_repeated_text_runaway)
  -> NextProtocol(single_json_search_replace)
```

This transition occurs before the generic byte-budget decision. It preserves
semantic intent while changing the serialization protocol, preventing another
attempt from repeating the same malformed marker grammar.

Root-cause diagnosis uses an ordered context vector rather than set membership
alone. Let `visible(p, C)` mean that the relevant contents of path `p` survive
the global context budget, and let `E_fail` be the latest executable failure
evidence:

```text
requested(p) and exists(p) and not visible(p, C(t))
  -> prioritize(p, C(t+1))
  and retry(root_cause_analysis)

root_cause_analysis(t) -> E_fail in input(t)
```

The second rule is independent of the recent-document window. A bounded set of
latest command results, acceptance gates, observation summaries, mechanical
probes, and rejected-candidate facts is pinned for diagnosis. Requesting a path
already declared as context is therefore actionable: the path is promoted to
the front rather than dismissed as already present.

Zero semantic effect also dominates secondary content classifications:

```text
effect(A, W) = empty
  -> reject(A)
  and not apply(A)
  and not test(A)
  and next(root_cause_analysis)
```

Generated-test ownership is a two-key authorization rather than an inferred
permission from root-cause prose. Let `P` be the patch plan, `T` independent
project-policy triage, `F` machine-owned failing test paths, and `W` the next
round's writable set:

```text
OracleEscalation(P) :=
  Exact(P.patch_type, missing_context)
  and Exact(P.escalation, generated_test_oracle_triage)

AuthorizeTestRepair(P, T, F) :=
  OracleEscalation(P)
  and T.case_type = test_harness
  and T.safe_next_action = edit_test_harness
  and T.confidence in {medium, high}
  and (T.editable_paths intersect F) != empty

AuthorizeTestRepair(P, T, F)
  -> W := T.editable_paths intersect F
  and discard(P)
  and next(repair_coder)

not AuthorizeTestRepair(P, T, F)
  -> no_test_edit
```

`T` receives fixed SPEC, complete generated-test source, current executable
command evidence, and an advisory prior judge vote. It does not receive repair
hypotheses as primary evidence and cannot apply an edit itself. Fixed acceptance
tests never enter `F`. A test-repair strategy also disables product-only
semantic-repair routing for that round.

Failure taxonomy remains disjoint:

```text
ProtocolFailure(missing_context) := false
ProtocolFailure(format_repair_missing_context) := true
ProtocolFailure(semantic_repair_missing_context) := true
```

This prevents a parent supervisor from treating an unresolved proposition or
missing semantic input as malformed artifact serialization.

Candidate comparison and path authority are likewise explicit:

```text
StableVector(V0, Vr) := identities(V0) = identities(Vr)

Regression(A) :=
  StableVector(Vbefore, Vafter)
  and FailureScore(Vafter) > FailureScore(Vbefore)

FailureScore(V) :=
  Sum(failures(c) for c in V if not DerivedAcceptanceGate(c))

PlannerPathAllowed(p) :=
  ExistingProjectPath(p) or RunnerAuthorizedWritablePath(p)

DeclaredHarnessMissing(t) :=
  StageOwnedGeneratedTest(t)
  and not ExistsOnDisk(t)
  and ZeroTestDiscovery(t)
```

`DeclaredHarnessMissing(t)` routes to bounded harness creation. It does not
authorize edits to an existing generated assertion, and it never includes
fixed acceptance tests. `PlannerPathAllowed(p)` is checked by the runner; an
LLM plan is advisory and cannot add a path to the authorized set.

The autonomy benchmark passes only when:

```text
AutonomyPass :=
  ProductPass
  and EvidenceComplete
  and SafetyPass
  and UnauthorizedExternalInterventions = 0
```

### LLM Completion Recovery And Non-vacuous Evidence

分析 call `c` の thinking 出力を `R(c)`、後段へ渡せる結論本文を `C(c)` とする。

```text
ReasoningOnly(c) := R(c) != empty and C(c) = empty

RecoverableCompletion(c) :=
  AnalysisFunction(c)
  and ThinkingEnabled(c)
  and ReasoningOnly(c)
  and FallbackCount(c) = 0

Fallback(c) :=
  SameRole(c)
  and SameSystemPrompt(c)
  and SameInputDocuments(c)
  and CondensationInput = Tail(R(c), 6000 chars)
  and ThinkingDisabled
  and Temperature = 0
  and MaxTokens <= 2048

AcceptContent(c) := C(c) != empty or C(Fallback(c)) != empty
```

`Fallback(c)` は内部処理の続きではなく、Action Gate と API budget を再度通過する
独立 request である。監査用reasoning全体は後続roleへ渡さない。ただし同じcallを完結させる
ためのfallbackに限り、末尾6,000文字以内を「既存推論の圧縮対象」として渡し、thinking off・
最大2,048 tokensで契約どおりの結論だけを生成させる。fallbackも空ならfail closedとし、
2回目のfallbackは行わない。

テスト証拠 `e` には非空性を要求する。

```text
NonVacuousTestEvidence(e) :=
  ExitCode(e) = 0
  and ExecutedTestCount(e) >= 1

TestPass(e) := RecognizedTestRunner(e) -> NonVacuousTestEvidence(e)
```

認識済み `unittest` runner の `Ran 0 tests` は、Python実装ごとの終了コード差に依存せず
`missing_test_harness` へ正規化する。

Verified by `tests.test_agent_components`, `tests.test_autonomy_runtime`, the
planner-to-triage integration test in `tests.test_local_sdlc`, and the full
660-test suite. Cross-domain validation still follows the frozen sequence:
freeze the harness, run Mini Git, generalize only supported findings, freeze
again, then run the held-out DAG job engine without mid-run intervention.

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
