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

The implemented runtime covers the event contract, durable run-local outbox,
collection, completeness audit, redaction, causal recovery episodes,
project-local Domain Maps, strict knowledge records, and mechanical
applicability decisions. Knowledge mining and promotion are later slices and
cannot bypass validation or human approval.

## Foundation Commands

```text
python3 local_sdlc_learning.py doctor
python3 local_sdlc_learning.py import-legacy --run-dir <run-dir>
python3 local_sdlc_learning.py audit --run-dir <run-dir>
python3 local_sdlc_learning.py collect --run-dir <run-dir>
python3 local_sdlc_learning.py build-episodes --data-dir <learning-data-dir>
python3 local_sdlc_learning.py status --run-dir <run-dir>
```

Use `--data-dir <path>` with `collect` or `doctor` to select an isolated shared
experience store. Without it, the runtime uses its local user-data location.
`--no-legacy` prevents `collect` or `audit` from importing compatibility logs
before processing the canonical ledger.

`build-episodes` reads only already-collected events. It persists a normalized
causal episode when recovery events can be linked. Verified, atomic, explicitly
isolated changes may be eligible; confounded incidents remain case-only with
reason codes. This command does not modify a project or activate knowledge.

`DomainMap` accepts explicit project-local component roles and evidenced
technology observations. Its structural projection excludes paths, symbols,
component IDs, project identity, and technologies. `KnowledgeItem.from_dict`
rejects incomplete or ambiguous scope records, and `evaluate_applicability`
returns a read-only decision without changing candidate state.

The foundation is deliberately useful without an LLM. The execution process
commits events to `<run-dir>/runtime-events.sqlite3`; the separate collector can
be stopped and restarted without blocking a coding run or duplicating events.

Integration Gate A (`EL01`-`EL06`) passed on 2026-08-01; Integration Gates B
(`EL07`) and C (`EL08`) passed on 2026-08-02. Candidate mining, cross-project
validation, promotion, snapshots, and rollback remain planned under
`EL09`-`EL12` and are not presented as implemented features.
