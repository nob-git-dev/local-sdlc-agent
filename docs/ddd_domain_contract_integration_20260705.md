# DDD Domain Contract Integration

Date: 2026-07-05

## Purpose

The DDD skill existed as a selectable SDLC phase, but the supervisor did not
route work to it automatically. That made domain language, invariants, and
verification semantics depend on later agents guessing from SPEC.md alone.

This change makes DDD a first-class domain-contract phase for tasks where
behavior, state, rules, or acceptance checks matter.

## Routing Rule

The supervisor inserts `ddd` after `spec` and before implementation planning
when domain modeling is needed.

Common routes:

- New feature: `spec -> ddd -> architect -> tdd -> review`
- Interactive UI/game: `spec -> ddd -> tdd -> ui -> review`
- Database/domain-sensitive work: `spec -> ddd -> architect -> security -> deploy`

DDD is not inserted for simple questions. Bugfix and refactor work use DDD only
when the brief or SPEC indicates domain terms, state rules, acceptance
criteria, verification propositions, parsers, protocols, databases, or similar
behavioral semantics.

The implementation agent also supports the same concept:

- `agent --domain-modeling auto` runs a separate DDD API call when the DDD skill
  exists and the task needs domain modeling.
- `agent --domain-modeling always` forces the DDD API call and fails if the
  configured domain skill is missing.
- `agent --domain-modeling never` disables it.
- `run-stages` passes the same settings into every stage-level `agent` call.

The generated domain document is written as `01-domain-contract.md` and passed
to later coder/judge calls as `Domain contract document`.

## Mathematical Model

Let:

- `D_i` = domain proposition: term definition, invariant, state rule, aggregate
  boundary, or bounded-context rule.
- `R_j` = requirement proposition: a truth condition that must hold in the
  finished software.
- `O_k` = observation proposition: a command, runtime check, static assertion,
  structural check, or heuristic that observes a requirement.

Typed edges:

- `defines(D_i, R_j)`: the domain proposition gives meaning to the requirement.
- `observes(O_k, R_j, relation)`: the observation checks the requirement.
- `relation ∈ {equivalent, sufficient, necessary, proxy}`.

Precedence:

If `O_runtime` observes `R_j` with `equivalent` relation and passes, but
`O_static` observes `R_j` with `proxy` relation and fails, the correct next
classification is not automatically product repair. The supervisor or judge
must classify the conflict as `spec`, `harness`, or `supervisor` mismatch unless
there is a stronger visible edge proving product failure.

## DDD Output Contract

The DDD phase must produce:

- Ubiquitous Language
- Bounded Contexts
- Domain Model
- Domain Invariants
- Verification Proposition Contract
- Precedence Rules
- Handoff Requirements for architect, TDD, coder, and judge

The Verification Proposition Contract table uses:

`R_id | Domain term | Truth condition | Check_id | relation | observation | fail_owner`

This table is the bridge between human-readable SPEC.md and executable or
machine-checkable verification.

## Expected Effect

Later agents should no longer treat every failing check as the same kind of
failure. They should first ask:

1. Which requirement truth condition does this check observe?
2. Is the observation equivalent, sufficient, necessary, or only a proxy?
3. Who owns the failure: product, spec, harness, supervisor, or unknown?
4. Would the proposed repair preserve stronger previously passing evidence?

This generalizes the Tetris lesson without overfitting to Tetris: static harness
conditions and runtime acceptance conditions can disagree, and the SDLC runner
must model the relation explicitly before repairing artifacts.
