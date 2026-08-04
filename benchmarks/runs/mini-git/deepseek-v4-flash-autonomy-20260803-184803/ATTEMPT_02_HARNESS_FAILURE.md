# Mini Git Attempt 02: Generic Test Harness Boundary Failure

- Frozen harness commit: `e4b7cf3`
- Product code generated: no
- LLM API calls observed: PM and DDD, two separate calls
- External mid-run intervention: no
- Result: failed before Coder artifact generation

## Observed Failure

The stage precheck correctly observed that the new project's test directory did
not exist. A legacy domain repair rule then replaced that fact with Mini SQLite
paths (`tests/__init__.py` and `tests/test_minisqlite.py`). One nonexistent path
was subsequently promoted into read context, so the child stopped with
`include file not found`.

The parent did preserve a fail-closed manifest, but its first implementation
overwrote the partial API count and lost the two completed calls.

## Generalized Correction

1. Derive test paths only from the stage's declared writable paths and concrete
   test commands; never invent a benchmark-specific filename.
2. Under `--no-extra-files`, repair advice cannot expand writable scope.
3. Add only existing files to read context; a declared new file remains an
   artifact target until it exists.
4. Preserve partial API counts, documents, changed paths, and required paths
   when converting an unhandled child error into a structured manifest.
5. Keep this failed run as harness evidence, not as a product-code attempt.
