# Attempt 08 Harness Failure

## Scope

- stages: S04-S05
- external product-code edits: none
- learning context: disabled
- run directory: `.sdlc-runner/runs/full-autonomy-08`
- outcome: evaluator stopped the isolated attempt after the same unsupported
  ownership hypothesis was independently reproduced twice

## Observed progress

- The Judge correctly found two contradictions in a stage-generated test: a
  deleted newly staged path was expected to be absent from `staged`, and a
  non-force checkout was attempted after deleting a tracked file.
- The product implementation also lacked target-object prevalidation. The next
  coder round repaired that real product defect separately.
- The policy classifier later cited the correct specification propositions but
  selected `product_bug`, contradicting both its own cited facts and the prior
  Judge finding.
- Subsequent rounds modified product behavior to fit the generated assertions.
  The isolated worktree prevented those changes from reaching the benchmark
  project.
- Analysis functions repeatedly consumed their complete 8,192-token reasoning
  budget, returned no content, and required a second no-thinking condensation
  request. Streaming kept the work observable but did not make it efficient.

## Harness failures

An assertion extracted from a stage-owned generated test was promoted to a
binding semantic contract before its proposition was validated against the
fixed specification. Repair advice and failure-analysis conclusions then
entered the policy classifier's evidence packet, creating circular evidence:
the classifier was asked to decide ownership using documents that had already
assumed product ownership.

The short structured classifiers also inherited a deep-thinking stage override.
On this endpoint they spent the full response budget on hidden reasoning rather
than returning their bounded JSON or Markdown decision.

## General repair

- Mark stage-generated assertions as `provisional_test_oracle`; only fixed or
  independently validated contracts may constrain product repair.
- Build ownership packets from primary sources only: fixed specification,
  complete generated-test source, executable command evidence, and an explicit
  independent Judge vote. Exclude repair conclusions under review.
- Require positive evidence for both `H_product` and `H_test`, then validate the
  selected hypothesis mechanically before granting any path permission.
- When Judge and policy votes disagree, invoke an independent bounded
  arbitration call and fail closed if it cannot produce a valid proposition.
- Let explicit runtime API profiles override stage defaults so model-specific
  classifier settings do not require editing the product specification.
- Keep open-ended planning and counterexample search capable of thinking, but
  run bounded failure classification, policy classification, arbitration, and
  final Judge formatting without hidden reasoning for this model profile.

No benchmark product or fixed acceptance-test file was manually edited.
