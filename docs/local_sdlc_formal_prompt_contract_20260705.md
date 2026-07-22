# Local SDLC formal prompt contract notes

Date: 2026-07-05

## Problem

The local LLM can produce plausible but unsupported implementation claims when
the prompt leaves requirements implicit. Long natural-language instructions are
not enough for agentic coding because PM, coder, and judge calls are separate
API calls and can drift unless each handoff carries short, explicit claims.
The current revision also requires typed dependency edges so the runner can
treat handoffs as a small evidence graph, not just prose.

## Abstraction

Each agent call is modeled as:

```text
O = F(role, skill, function, D, I, profile)
```

Where:

- `role` is supervisor, PM, coder, or judge.
- `skill` is the SKILL.md body placed in the system prompt.
- `function` is the cognitive operation such as `plan_work`,
  `generate_artifact`, `semantic_repair`, or `judge_review`.
- `D` is the visible document set: SPEC.md, prior agent documents, file context,
  runtime facts, and command output.
- `I` is the current instruction.
- `profile` is the effective API setting selected for the function.
- `O` is the response.

Hidden memory is not part of `D`. A claim in `O` is valid only when it can be
supported by visible propositions from `D`.

## Proposition notation

- `Pn`: supplied premise
- `Cn`: fixed constraint
- `Gn`: goal
- `En`: observable evidence
- `An`: action or artifact
- `Vn`: supported verdict

## Graph notation

Each call is also modeled as:

```text
G = (V, E)
```

`V` contains propositions and document nodes. `E` contains:

- `supports(X, Y)`
- `constrains(C, A)`
- `satisfies(A, G)`
- `verifies(E, V)`
- `blocks(C, A)`

An action or verdict is valid only when:

```text
P* and C* and G* and E* |- A or V
```

The intended prompt behavior is to make PM and judge documents carry a
`Proposition Ledger` and `Graph Edges`, while coder calls use the same
reduction internally and emit only the required artifact format.

## Implemented changes

- Added a shared Formal reasoning and graph contract to every skill system
  prompt.
- Added Proposition discipline to the user-side document exchange prompt.
- Added Graph discipline to the user-side document exchange prompt.
- Required PM control documents to include a Proposition Ledger.
- Required PM/Judge documents to include Graph Edges when prose output is
  allowed.
- Required judge reviews to state major findings as P/C/E/A/V propositions.
- Kept coder output artifact-only, but instructed the coder to internally map
  visible P/C/G/E facts and graph edges to the smallest valid action.
- Added regression tests so the prompt contract remains present.

## Expected effect

This should reduce:

- hidden-context assumptions between independent API calls
- unsupported judge approvals
- coder claims that are not backed by file context or command output
- vague repair loops where the same failed patch is repeated

It does not replace executable checks. Proposition discipline is a prompt-level
guardrail; command results, artifact linting, and smoke tests remain the final
source of completion evidence.
