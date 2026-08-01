# Experience Learning Runtime

Experience Learning Runtime is the independent learning control plane for Local
SDLC Agent. It receives durable runtime events, builds evidence-backed
episodes, proposes reusable knowledge, validates candidates, and publishes
versioned knowledge snapshots.

It is not a model-training service and it does not modify an active project or
an in-progress Supervisor run. The Supervisor remains the execution plane; this
runtime is a separate process and storage boundary.

The authoritative project specification is [SPEC.md](SPEC.md).

## Planned Entry Point

```text
python3 local_sdlc_learning.py <command>
```

The first implementation slice covers only the event contract, durable
run-local outbox, collection, and completeness audit. Knowledge mining and
promotion are later slices and cannot bypass validation or human approval.
