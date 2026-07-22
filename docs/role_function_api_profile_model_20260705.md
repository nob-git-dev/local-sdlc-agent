# Role / Function / API Profile Model

Date: 2026-07-05

## Purpose

Keep agent responsibilities understandable while selecting LLM API settings by
the actual cognitive operation being performed.

The project should not grow one ad hoc setting flag per new agent. It should
use a small mathematical model:

```text
role            = viewpoint, responsibility, and authority
call_function   = cognitive operation performed by one API call
api_profile     = decoding/context settings used for that operation
```

## Current Role Separation

The current runner has meaningful role separation:

| Role | Responsibility | Must not do |
|---|---|---|
| `pm` | define goals, constraints, acceptance checks, and handoff documents | write implementation code |
| `coder` | produce executable artifacts from visible documents and file context | judge its own work as complete |
| `judge` | review coder claims against SPEC, command evidence, and fixed constraints | trust hidden coder context |
| `supervisor` | route phases, detect danger signals, and choose next work state | implement code directly |

This separation is adequate as the outer control model. The weak point was API
configuration: `coder` is too broad because generating a new file, repairing a
small failure, and repairing output format are different operations.

## Mathematical Model

Let:

```text
R = set of roles
F = set of call functions
P = set of API profiles
B = base profile
rho: R -> P_role
phi: F -> P_function
omega: F -> P_override
```

For each API call `c`, the supervisor chooses:

```text
c = (r, f, D, I)
```

where:

- `r in R` is the role.
- `f in F` is the function.
- `D` is the visible document set.
- `I` is the current instruction.

The effective API profile is:

```text
profile(c) = B ⊕ rho(r) ⊕ phi(f) ⊕ omega(f)
```

`⊕` means field-wise override from left to right. Therefore function settings
override role defaults, and explicit function overrides override built-ins.

## Built-in Function Profiles

| Function | Intended operation | Temperature | Max tokens | Thinking |
|---|---|---:|---:|---|
| `route_task` | classify request and choose phase gates | 0.2 | 8192 | off |
| `plan_work` | PM/spec/architecture planning | 0.2 | 8192 | off |
| `explore_code` | evidence gathering and code reading | 0.0 | 8192 | off |
| `generate_artifact` | create executable patch/file artifacts | 0.1 | 65536 | off |
| `repair_artifact` | repair after failing evidence | 0.05 | 65536 | off |
| `semantic_repair` | satisfy extracted semantic contracts with one atomic artifact | 0.0 | 8192 | off |
| `format_repair` | fix malformed artifact protocol only | 0.0 | 8192 | off |
| `judge_review` | objective review of coder claims | 0.0 | 8192 | off |
| `verify_acceptance` | final evidence/acceptance verification | 0.0 | 8192 | off |

## Operational Rule

The role is still visible in prompts and logs, but API configuration should be
selected primarily by `call_function`.

Examples:

```text
(coder, generate_artifact) -> large token budget, low temperature
(coder, repair_artifact)   -> large token budget, lower temperature
(coder, semantic_repair)   -> short strict budget, temperature 0
(coder, format_repair)     -> short strict budget, temperature 0
(judge, judge_review)      -> evidence-only, temperature 0
```

## Software Engineering Abstraction

To avoid configuration sprawl:

1. Keep built-in function profiles in one table.
2. Normalize aliases such as `review -> judge_review`.
3. Infer a default function from `(role, skill)` when the caller does not
   specify one.
4. Allow a generic CLI override:

```bash
--api-profile repair_artifact:max_tokens=32768,temperature=0,thinking=off
```

This gives a stable extension point when new functions are added.
