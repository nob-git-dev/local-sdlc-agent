# DAG Job Engine Implementation Specification

## Purpose

Build a deterministic, resumable directed-acyclic-graph job engine named
`dagrunner`. The benchmark evaluates whether a coding agent can derive graph
invariants, failure propagation, retry behavior, and durable resume semantics
from a new specification without domain-specific repair rules.

## Fixed Constraints

- Target Python 3.10 or later.
- Use only the Python standard library.
- Product code belongs in `dagrunner/`; agent-authored tests belong in `tests/`.
- `SPEC.md` and `acceptance_tests/` are immutable external evidence.
- Execute task handlers in the current process; do not use shell commands,
  subprocesses, threads, processes, networking, or third-party schedulers.
- Scheduling must be deterministic for the same graph and checkpoint.
- Persist checkpoints as UTF-8 JSON with atomic replacement.
- Do not use process-global mutable state.

## Public API

These imports are required:

```python
from dagrunner import (
    CycleError,
    DagError,
    Engine,
    Graph,
    GraphValidationError,
    RunReport,
    StateMismatchError,
    Task,
)
```

### Task

```python
Task(task_id: str, dependencies=(), max_attempts: int = 1)
```

- `task_id` must match `[A-Za-z][A-Za-z0-9_-]*`.
- `dependencies` is normalized to an immutable tuple of task IDs.
- Duplicate dependencies are rejected.
- `max_attempts` must be an integer of at least 1. Boolean values are not
  valid integers for this contract.
- Task definitions are immutable after construction.

### Graph

```python
Graph(tasks)
Graph.topological_order() -> list[str]
```

- Task IDs are unique.
- Every dependency names another task in the same graph.
- Self-dependencies and cycles raise `CycleError`.
- Other invalid definitions raise `GraphValidationError`.
- The topological order is deterministic. Whenever multiple tasks are ready,
  select task IDs in ascending lexical order.
- Constructing a graph does not execute handlers or access a checkpoint.

### Engine

```python
Engine(tasks, handlers, state_path=None)
Engine.run(max_tasks=None) -> RunReport
```

- `handlers` maps every task ID to a callable. Missing or non-callable handlers
  raise `GraphValidationError` before any handler executes.
- A handler receives one read-only mapping from direct dependency ID to that
  dependency's successful return value.
- A handler returning normally succeeds and its return value becomes available
  to direct dependents.
- A handler raising `Exception` is retried until it succeeds or reaches the
  task's `max_attempts`.
- The final error string for an exhausted task contains both the exception
  class name and message.
- A task runs only after every dependency succeeds.
- If any dependency is `failed` or `blocked`, the task becomes `blocked`, its
  handler is not called, its attempt count remains zero, and its error names
  every failed or blocked direct dependency in sorted order.
- Failure in one branch does not stop independent ready branches.
- Each scheduling choice uses ascending lexical task ID order.
- `max_tasks`, when supplied, must be an integer of at least 1 and limits the
  number of newly terminal task transitions (`succeeded`, `failed`, or
  `blocked`) in that call. Reaching the limit returns a partial report rather
  than raising.
- Calling `run()` again continues pending work. Already terminal tasks and
  their handlers are not repeated.

## RunReport

`RunReport` exposes these read-only attributes:

```python
report.statuses   # dict[str, str]
report.attempts   # dict[str, int]
report.values     # dict[str, object], successful tasks only
report.errors     # dict[str, str], failed or blocked tasks only
report.completed  # bool
```

Allowed status strings are exactly `pending`, `succeeded`, `failed`, and
`blocked`. Every graph task appears in `statuses` and `attempts`. `completed`
is true only when no task is pending. Returned dictionaries must be snapshots;
mutating one report must not mutate engine or checkpoint state.

## Durable Checkpoint And Resume

When `state_path` is supplied:

- The parent directory is created when needed.
- The checkpoint is written after every newly terminal task transition and
  before `run()` returns.
- Use a temporary sibling file, flush it, and atomically replace the target.
- The final target is valid UTF-8 JSON. No temporary sibling remains after a
  successful write.
- The JSON object contains `schema_version` equal to `1`, a deterministic
  64-character lowercase `graph_fingerprint`, and task state sufficient to
  reconstruct statuses, attempts, values, and errors.
- The fingerprint is derived from task IDs, normalized dependencies, and
  `max_attempts`, not from handler identity or process-specific values.
- A new `Engine` with the same graph and checkpoint resumes automatically on
  `run()` and never re-executes succeeded, failed, or blocked tasks.
- A checkpoint whose schema or graph fingerprint does not match raises
  `StateMismatchError` before any handler executes or checkpoint is changed.
- Malformed JSON or structurally invalid task state also raises
  `StateMismatchError` without executing handlers.
- Successful return values used with persistence must be JSON-serializable.

## Error Hierarchy

- `DagError` is the common public base exception.
- `GraphValidationError`, `CycleError`, and `StateMismatchError` derive from
  `DagError`.
- Public validation and checkpoint failures must not leak raw implementation
  exceptions such as `KeyError` or `JSONDecodeError`.

## Acceptance Criteria

- Graph validation rejects invalid IDs, duplicate IDs and dependencies,
  missing dependencies, and cycles before execution.
- Topological order and scheduling are lexically deterministic.
- Successful dependency values are passed to direct dependents.
- Retry counts are exact and exhausted failures block only downstream tasks.
- Independent branches continue after another branch fails.
- `max_tasks` produces a resumable partial report.
- A fresh engine process-equivalent instance resumes from JSON without
  re-executing terminal handlers.
- Graph/schema mismatch and malformed checkpoints fail closed before work.
- All agent-authored tests and the fixed external acceptance suite pass.

## Implementation Stages

```json
{
  "stage_plan_schema": 1,
  "stages": [
    {
      "stage_id": "S01",
      "title": "Immutable task model and graph validation",
      "goal": "Create the package, public error hierarchy, immutable Task model, deterministic graph validation, cycle detection, and lexical topological ordering.",
      "writable_paths": ["dagrunner/__init__.py", "dagrunner/errors.py", "dagrunner/model.py", "dagrunner/graph.py", "tests/test_graph.py"],
      "readonly_evidence_paths": ["SPEC.md", "acceptance_tests/test_dagrunner_acceptance.py"],
      "test_commands": ["python3 -m unittest discover -s tests -p 'test_graph.py' -v"],
      "required_observables": ["task and graph validation tests pass"],
      "api_profile": ["generate_artifact:max_tokens=8192,temperature=0.05,thinking=off", "root_cause_analysis:max_tokens=8192,temperature=1,thinking=on"],
      "max_rounds": 6
    },
    {
      "stage_id": "S02",
      "title": "Deterministic execution retries and failure propagation",
      "goal": "Implement RunReport and deterministic in-process execution with dependency-value handoff, exact retries, blocked descendants, independent branch continuation, and bounded max_tasks continuation.",
      "writable_paths": ["dagrunner/__init__.py", "dagrunner/executor.py", "tests/test_executor.py"],
      "readonly_evidence_paths": ["SPEC.md", "dagrunner/errors.py", "dagrunner/model.py", "dagrunner/graph.py", "tests/test_graph.py", "acceptance_tests/test_dagrunner_acceptance.py"],
      "test_commands": ["python3 -m unittest discover -s tests -p 'test_executor.py' -v", "python3 -m unittest discover -s tests -v"],
      "required_observables": ["retry, dependency handoff, blocking, and independent branch tests pass"],
      "api_profile": ["generate_artifact:max_tokens=8192,temperature=0.05,thinking=off", "root_cause_analysis:max_tokens=8192,temperature=1,thinking=on"],
      "max_rounds": 7
    },
    {
      "stage_id": "S03",
      "title": "Atomic checkpoint resume and documentation",
      "goal": "Add versioned atomic JSON checkpoints, deterministic graph fingerprints, fail-closed resume validation, terminal-task idempotence, full public exports, generated checkpoint tests, and concise usage documentation.",
      "writable_paths": ["dagrunner/__init__.py", "dagrunner/checkpoint.py", "dagrunner/executor.py", "tests/test_checkpoint.py", "README.md"],
      "readonly_evidence_paths": ["SPEC.md", "dagrunner/errors.py", "dagrunner/model.py", "dagrunner/graph.py", "tests/test_graph.py", "tests/test_executor.py", "acceptance_tests/test_dagrunner_acceptance.py"],
      "test_commands": ["python3 -m unittest discover -s tests -p 'test_checkpoint.py' -v", "python3 -m unittest discover -s tests -v"],
      "required_observables": ["checkpoint, mismatch, malformed-state, and resume idempotence tests pass"],
      "api_profile": ["generate_artifact:max_tokens=8192,temperature=0.05,thinking=off", "root_cause_analysis:max_tokens=8192,temperature=1,thinking=on"],
      "max_rounds": 8
    }
  ]
}
```

## Verification Commands

```bash
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s acceptance_tests -v
```
