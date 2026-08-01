# Experience Learning Runtime

Experience Learning Runtime is the independent learning control plane for Local
SDLC Agent. It receives durable runtime events, builds evidence-backed
episodes, proposes reusable knowledge, validates candidates, and publishes
versioned knowledge snapshots.

It is not a model-training service and it does not modify an active project or
an in-progress Supervisor run. The Supervisor remains the execution plane; this
runtime is a separate process and storage boundary.

The authoritative project specification is [SPEC.md](SPEC.md).

## Entry Point

```text
python3 local_sdlc_learning.py <command>
```

The implemented foundation covers the event contract, durable
run-local outbox, collection, and completeness audit. Knowledge mining and
promotion are later slices and cannot bypass validation or human approval.

## Foundation Commands

```text
python3 local_sdlc_learning.py doctor
python3 local_sdlc_learning.py import-legacy --run-dir <run-dir>
python3 local_sdlc_learning.py audit --run-dir <run-dir>
python3 local_sdlc_learning.py collect --run-dir <run-dir>
python3 local_sdlc_learning.py status --run-dir <run-dir>
```

Use `--data-dir <path>` with `collect` or `doctor` to select an isolated shared
experience store. Without it, the runtime uses its local user-data location.
`--no-legacy` prevents `collect` or `audit` from importing compatibility logs
before processing the canonical ledger.

The foundation is deliberately useful without an LLM. The execution process
commits events to `<run-dir>/runtime-events.sqlite3`; the separate collector can
be stopped and restarted without blocking a coding run or duplicating events.

Integration Gate A (`EL01`-`EL06`) passed on 2026-08-01. Candidate mining,
cross-project generalization, promotion, snapshots, and rollback remain planned
under `EL07`-`EL12` and are not presented as implemented features.
