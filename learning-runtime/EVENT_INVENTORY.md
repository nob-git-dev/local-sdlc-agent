# Runtime Event Mutation Inventory

This inventory is the L00 baseline for Integration Gate A. The executable copy
is `learning_runtime/inventory.py`; this document explains the ownership
boundaries for review.

| Existing projection/evidence | Current writer | Required canonical transition family | Migration |
|---|---|---|---|
| `cancel.json` | `control.request_cancel` | cancellation requested | runtime hook plus ledger fallback |
| `progress.jsonl` | `control._append_progress_event_unlocked` | action admitted, progress, stalled, cancellation | runtime hook and JSONL adapter |
| `safety_decisions.jsonl` | `safety._record_safety_decision_unlocked` | safety decision, blocked, approval required | runtime hook and JSONL adapter |
| `safety_approvals.jsonl` | `safety._append_approval_event_unlocked` | approval granted/consumed | runtime hook and JSONL adapter |
| `budget_events.jsonl` | `budget._append_budget_event_unlocked` | consumed, refunded, exhausted | runtime hook and JSONL adapter |
| `budget_stop.json` | `budget._persist_stop_unlocked` | budget exhausted | derived from the preceding budget event |
| `progress_policy.json` / `progress_state.json` | `progress_monitor` | run/stage started and progressed | runtime hook; files remain projections |
| `stall.json` | `progress_monitor._persist_stall_unlocked` | goal/stage stalled | stalled progress hook and JSON adapter |
| `run.partial.json` / `run.json` | `utils.write_run_document` | run started, verification, stage/run closed | manifest hook and JSON adapter |
| Failure Analysis JSON / manifest entries | `agent_runner` | failure classified, hypothesis rejected | runtime hook and JSON adapter |
| Regression memory JSON | `history.persist_regression_memories_for_manifest` | regression memory recorded | runtime hook and JSON adapter |

## Baseline Findings

1. Existing ledgers have independent sequence spaces and no common causation ID.
2. Existing JSONL readers tolerate malformed lines by skipping them. The learning
   adapter reports their location instead.
3. Existing regression memory is project-local and matches concrete paths.
4. `_memory_from_failure_analysis()` returned `None` for a populated completed
   analysis before this integration work; L04 includes a regression repair.
5. Existing files remain compatibility or human-readable projections. New
   canonical transition/event/outbox records live in `runtime-events.sqlite3`.

## Change Rule

Adding a canonical state mutation requires all of the following in one change:

1. a `TransitionKind` and `EventType` contract;
2. a `MutationContract` inventory entry;
3. a runtime transition-gateway call;
4. a legacy adapter or an explicit `not_applicable` migration decision; and
5. contract, hook, fault-injection, and closure tests.

The event contract and mutation inventory tests fail when their registered
entries are incomplete. Stage/run closure audits catch missing events in actual
run data.
