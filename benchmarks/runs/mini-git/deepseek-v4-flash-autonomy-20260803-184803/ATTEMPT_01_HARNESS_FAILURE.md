# Mini Git Attempt 01: Harness Contract Failure

- Frozen harness commit: `e4b7cf3`
- Product code generated: no
- LLM API calls started: no
- External mid-run intervention: no
- Result: failed before the first child API call

## Observed Failure

The versioned stage-plan contract accepted bare function labels in
`api_profile`, while the child execution layer interpreted each value as an
executable `function:key=value,...` override. The first stage therefore stopped
with `--api-profile must use function:key=value,...`.

## Generalized Correction

1. Validate every stage-level function override while parsing the stage plan.
2. Reject an invalid contract before any child Agent or LLM call starts.
3. Convert any otherwise unhandled child Runner error into a structured child
   manifest and a parent `fail_closed` decision instead of abandoning the run.
4. Keep this failed run as evidence; do not count the correction as a product
   implementation intervention.
