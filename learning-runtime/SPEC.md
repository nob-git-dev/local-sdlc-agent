# Experience Learning Runtime Specification

## Status

- Status: Domain Map and Knowledge Schema implemented; Integration Gate C passed
- Created: 2026-08-01
- Parent system: Local SDLC Agent
- Authority: this document is authoritative for the learning control plane;
  the repository root `SPEC.md` remains authoritative for the execution plane
- Implemented propositions: `EL01` through `EL08`
- Deferred propositions: `EL09` through `EL12`

## Decision and Development Order

The learning project starts before further autonomous-recovery work, but only
its capture foundation is a blocking predecessor.

```text
L01-L04: event contract, ledger/outbox, completeness, legacy adapters
          (acceptance propositions EL01-EL06)
    -> Integration Gate A
    -> Supervisor P04/P05 plus real recovery capture
    -> L05: normalization, redaction, causal episode builder (EL07)
    -> Integration Gate B
    -> L06: Domain Map and Knowledge Schema (EL08)
    -> Integration Gate C
    -> L07-L11: abstraction, validation, registry, retrieval
                (acceptance propositions EL09-EL12)
```

Building the full learner before observing real P04/P05 recovery events would
create a synthetic-data overfitting risk. Running P04/P05 before the capture
foundation would permanently lose learning evidence. Therefore the project uses
foundation-first, then alternating integration rather than either pure parallel
development or a complete sequential rewrite.

### Integration Gate A Result

Completed on 2026-08-01:

| Slice | Result | Mechanical evidence |
|---|---|---|
| L00 | PASS | Executable mutation inventory covers progress, safety, approval, budget, stall, manifest, and failure-analysis persistence paths. |
| L01 | PASS | The transition enum and contract registry are exhaustively checked; missing registrations fail the test suite. |
| L02 | PASS | Run-local SQLite commits transition, immutable event, and outbox atomically; fault-before and fault-after-commit cases are covered. |
| L03 | PASS | Collector replay is idempotent, closure/integrity auditing is explicit, and the learning CLI operates independently. |
| L04 | PASS | Frozen P01/P02/P03/P09-style evidence imports with provenance; malformed legacy records become findings rather than disappearing. |

Verification evidence:

- `tests.test_learning_runtime`: 22 tests passed;
- execution-plane integration selection: 88 tests passed;
- `python3 -m unittest discover -s tests`: 459 tests passed;
- a copied legacy fixture produced 15 canonical events, passed audit, delivered
  15 events idempotently, and left zero pending outbox records.

This result authorizes resuming P04/P05 while collecting real recovery events.
At that gate, L05-L11 and `EL07`-`EL12` remained incomplete.

### Integration Follow-up: P04/P05

Completed on 2026-08-01: the execution plane now emits canonical
`RECOVERY_PLANNED`, `RECOVERY_STARTED`, and `RECOVERY_COMPLETED` events with
causation identifiers and immutable stall-evidence references. Event emission
alone does not promote knowledge; it supplied the real input required by L05.

### Integration Gate B Result

Completed on 2026-08-02:

| Slice | Result | Mechanical evidence |
|---|---|---|
| P04/P05 capture | PASS | Two production-control recovery episodes use distinct failure families and select `failure_analysis` and `root_cause_recovery` respectively. |
| Completion evidence | PASS | Target manifest hash, acceptance status counts, changed paths, atomicity, isolation, concurrent paths, and verification outcome are persisted and hash-referenced. |
| Causal episode graph | PASS | Exactly one planned, started, and completed event with valid causation links produces decision -> intervention -> outcome. |
| Confounding control | PASS | Unverified, non-atomic, unisolated, concurrent, incomplete, duplicate, or causally broken episodes remain `case_only` with structured reason codes. |
| Normalization/privacy | PASS | Concrete filenames, absolute home paths, email addresses, source bodies, and evidence paths do not enter normalized episode records; hashes and opaque provenance remain. |

Eligibility is deliberately conjunctive. A recovery episode is eligible only
when all of the following are mechanically true:

1. its planned, started, and completed events form one complete causal chain;
2. the target run is approved and verification passed;
3. exactly one changed path is attributed to the intervention;
4. the change was explicitly isolated; and
5. no concurrent changed path was observed.

Failure of any condition does not erase the incident. The learner persists it
as case memory with reason codes, but candidate mining cannot treat it as causal
evidence.

Verification evidence:

- `tests.test_learning_episodes`: renamed-structure, privacy, causality,
  confounding, persistence, idempotency, and independent-CLI cases passed;
- focused learning/recovery selection: 41 tests passed;
- `python3 -m unittest discover -s tests`: 478 tests passed;
- an ignored local evidence run produced two eligible episodes, zero case-only
  episodes, then replayed with zero inserts and two duplicates.

This result completed L05 and `EL07`. At that gate, L06/`EL08`, candidate
mining, activation, and Supervisor retrieval remained unavailable.

### Integration Gate C Result

Completed on 2026-08-02:

| Slice | Result | Mechanical evidence |
|---|---|---|
| Project-local Domain Map | PASS | Relative component paths and symbols map to explicit abstract roles; unresolved relations, traversal paths, duplicate facts, unknown fields, and tampered signatures fail closed. |
| Rename-invariant projection | PASS | Component IDs, paths, symbols, project identity, and technologies are removed before structural hashing; two renamed isomorphic fixtures produce the same projection and signature. |
| Knowledge Schema | PASS | Scope, authority, applicability, evidence, and effect are required independently; unknown fields, invalid enums, sensitive values, duplicate predicates, and ambiguous scope predicates are rejected. |
| Mechanical applicability | PASS | `all` predicates cover structural roles/relations/signatures, evidenced technologies, exact projects, and exact episodes without changing knowledge state. |
| Technology boundary | PASS | Technology scope requires at least one `technology_present` predicate and a matching Domain Map observation with a path-free evidence hash; the same structure without that observation does not match. |

For knowledge item `k`, Domain Map `m`, and optional case `c`, L06 fixes these
propositions:

```text
SchemaValid(k) =
  RequiredFields(k)
  and ScopePredicatesAreAllowed(k.scope, k.applicability)
  and HasScopeAnchor(k.scope, k.applicability)
  and HasEvidence(k)
  and ContainsNoSharedSecret(k)

Applicable(k, m, c) =
  SchemaValid(k)
  and every predicate p in k.applicability is mechanically true for (m, c)
```

`scope(k)` is never inferred from `authority(k)`, and authority never grants an
effect or activation right. Domain roles are explicit observations; L06 does
not guess them from filenames. The evaluator returns a decision document only.

Verification evidence:

- `tests.test_learning_domain_map` and
  `tests.test_learning_knowledge_schema`: 16 tests passed;
- focused learning, episode, schema, and Domain Map selection: 43 tests passed;
- `python3 -B -m unittest discover -s tests`: 494 tests passed;
- all new production modules and test modules are below 300 lines.

This result completes L06 and `EL08`. L07/`EL09` is the next learning slice;
candidate mining, activation, registry publication, and Supervisor retrieval
remain unavailable.

## Purpose

Create a local, independent program that learns from verified software-agent
runs without changing model weights. It shall:

1. capture every meaningful Supervisor state transition through a mandatory,
   durable event contract;
2. preserve evidence provenance and causal links among observation, decision,
   intervention, and outcome;
3. compare experiences across projects while keeping project-specific facts
   scoped;
4. propose structural, technology-specific, and project-specific knowledge;
5. validate candidates against replays, counterexamples, and holdout tasks;
6. publish immutable, versioned knowledge snapshots for later runs; and
7. prevent an LLM or one successful benchmark from directly changing active
   execution or safety policy.

## Problem Statement

The execution plane currently stores useful but separate records such as
`progress.jsonl`, `safety_decisions.jsonl`, `budget_events.jsonl`, `stall.json`,
run manifests, failure analyses, and project-local regression memory. These
records support P01, P02, P03, and P09, but they do not yet provide:

- one exhaustive transition/event contract;
- proof that a newly added transition cannot omit its event;
- a project-independent experience store;
- separation of facts, heuristics, and normative requirements;
- candidate, shadow, active, challenged, and retired knowledge states;
- cross-project generalization and counterexample evaluation; or
- immutable knowledge snapshots for deterministic Supervisor runs.

### Known Baseline Findings

- `local_sdlc/history.py` stores regression memory inside each project and uses
  concrete path overlap for applicability; it is not a cross-project knowledge
  registry.
- The current `_memory_from_failure_analysis()` path returns no memory for a
  populated completed analysis. Its intended construction block is unreachable
  after `_memory_from_run_failure()` returns. L00 must freeze this reproduction,
  and L04 must repair or replace the adapter before treating legacy failure
  analyses as migrated.
- Existing JSONL readers may skip malformed lines for runtime tolerance. The
  learning collector must instead preserve the source location and report a
  malformed or missing event as an explicit integrity finding.
- Progress, safety, approval, and budget ledgers currently have independent
  sequences and persistence paths. They require correlation and causation IDs
  before one cross-ledger episode can claim causal completeness.

## Terms

- **Execution plane**: the Supervisor Runtime, agents, Action Gate, tests, and
  file-changing processes that attempt to complete one project goal.
- **Learning control plane**: the separate program that collects, normalizes,
  evaluates, and publishes knowledge. It has no project mutation authority.
- **Transition**: one committed change of a goal, stage, action, or verification
  state through the canonical transition gateway.
- **Event envelope**: the versioned, immutable record emitted for a transition.
- **Outbox**: the run-local durable queue written in the same transaction as a
  canonical transition.
- **Episode**: a causally linked tuple of context, evidence, hypothesis,
  intervention, change, and verified outcome.
- **Domain Map**: a project-local mapping from concrete paths and symbols to
  abstract roles such as presentation, parser, state machine, or persistence.
- **Knowledge candidate**: an inactive proposition proposed from episodes.
- **Knowledge pack**: a versioned set of propositions with scope,
  applicability, provenance, counterexamples, and permitted effect.
- **Knowledge snapshot**: an immutable set of active pack versions selected at
  run start.
- **Structural knowledge**: reusable when the same architecture or failure
  structure exists, independent of concrete package names.
- **Technology knowledge**: restricted to a mechanically verified package,
  runtime, protocol, format, or version range.
- **Project knowledge**: a requirement or fact valid only for one project.
- **Case memory**: an individual incident retained for retrieval and evaluation;
  it is not itself an executable rule.

## Scope

### Included

- event schema and transition contract registry;
- run-local ledger/outbox and asynchronous collection;
- legacy log adapters and hook-completeness audits;
- privacy filtering, normalization, and evidence references;
- episode construction and causal attribution;
- Domain Map and applicability evaluation;
- independent LLM calls for candidate abstraction;
- replay, metamorphic, counterexample, and holdout validation;
- knowledge lifecycle, promotion, challenge, retirement, and rollback;
- snapshot-based read-only Supervisor retrieval; and
- CLI inspection and machine-readable reports.

### Excluded

- model weight training, fine-tuning, or reinforcement learning;
- direct modification of an active project by the learner;
- direct activation of LLM-generated rules;
- relaxation of cancel, safety, approval, or budget invariants;
- cloud storage or mandatory network services;
- embedding an LLM server;
- treating similarity to one incident as proof of causality; and
- replacing the Supervisor, Judge, Failure Analysis, or Safety Harness.

## Fixed Requirements

1. The learning control plane is a separate process and entry point from the
   Supervisor, even when both ship in the same repository.
2. The execution plane remains usable when the learner is stopped, provided its
   run-local event ledger can be written.
3. A canonical transition cannot commit without its required event and outbox
   record.
4. No component may bypass the transition gateway to mutate canonical state.
5. Raw events are immutable; corrections are compensating events.
6. Collection is idempotent and supports at-least-once delivery.
7. An active run uses one immutable knowledge snapshot. New knowledge applies
   only to later runs.
8. LLM calls may propose and explain knowledge, but cannot activate knowledge or
   grant execution authority.
9. `require` and `forbid` effects, safety changes, and permission changes require
   explicit human approval. Low-impact `recommend` knowledge may use a configured
   mechanical promotion policy.
10. Normative requirements, observed facts, and heuristic conclusions are
    different kinds and cannot overwrite one another.
11. Project and technology applicability must be mechanically established before
    scoped knowledge affects retrieval.
12. Secrets, personal identifiers, absolute home paths, and unnecessary source
    bodies cannot enter the shared knowledge store.
13. The first implementation uses Python standard-library facilities and adds no
    mandatory external service.
14. Existing P01, P02, P03, and P09 behavior remains valid during migration;
    legacy JSON/JSONL records remain compatibility projections or import sources
    until an approved specification retires them.

## Separation and Dependency Direction

```text
Supervisor / Action Gate / Verifier
              |
              v
       Transition Gateway
              |
              v
 Run-local Event Ledger + Outbox
              |
              v
      Experience Collector
              |
              v
 Normalize -> Episode Builder -> Candidate Miner
              |
              v
 Validator -> Knowledge Registry -> Immutable Snapshot
                                      |
                                      v
                       Supervisor read-only Retriever
```

Allowed dependencies are `execution -> event contract`, `collector -> outbox`,
`learner -> collected events`, `registry -> validated candidates`, and
`supervisor -> read-only snapshot`.

The learner cannot mutate active run state, the candidate miner cannot activate
registry entries, a snapshot cannot grant Safety approval, and a project pack
cannot apply to an unrelated project.

## Canonical Event Contract

### Event Envelope

Every event contains at least:

```yaml
schema_version: 1
event_id: globally-unique-id
transition_id: globally-unique-id
run_id: stable-run-id
project_fingerprint: privacy-preserving-project-id
aggregate_type: goal|stage|action|verification|knowledge
aggregate_id: stable-aggregate-id
sequence: monotonic-per-aggregate
event_type: registered-event-name
occurred_at: UTC-timestamp
source_component: registered-component-name
correlation_id: goal-or-operation-id
causation_id: preceding-event-id-or-null
knowledge_eligibility: unknown|eligible|ineligible
propositions: []
evidence_refs: []
payload: {}
sensitivity: public|project|restricted
previous_hash: prior-event-hash-or-null
event_hash: canonical-event-hash
```

Large documents and source bodies are referenced by content hash, relative
evidence path, media type, and redaction status rather than copied into events.

### Required Event Families

- `run_planned`, `run_started`, `run_terminated`;
- `stage_started`, `stage_progressed`, `stage_stalled`, `stage_closed`;
- `evidence_observed`, `failure_classified`, `hypothesis_rejected`;
- `action_admitted`, `action_blocked`, `approval_required`;
- `intervention_applied`, `verification_completed`;
- `recovery_planned`, `recovery_started`, `recovery_completed`;
- `budget_consumed`, `budget_refunded`, `budget_exhausted`;
- `cancellation_requested`, `cancellation_enforced`; and
- `event_contract_violation`.

One declarative registry owns the transition-to-event mapping. Adding a
transition enum without a contract entry fails the test suite.

### Completeness Invariants

Let `Tcommit` be committed canonical transitions and `L` be ledger events:

```text
EL-C1: for every t in Tcommit, exactly one e in L has e.transition_id = t.id
EL-C2: required_events(t.kind) is a subset of events(t.id)
EL-C3: sequence(e[n+1]) = sequence(e[n]) + 1 per aggregate
EL-C4: hash(e[n+1]).previous_hash = hash(e[n])
EL-C5: stage_closed implies completeness(stage) = pass
EL-C6: run_terminated implies completeness(run) = pass or an explicit
       event_contract_violation is persisted
```

A ledger write failure prevents transition commit. Learner unavailability does
not prevent execution; undelivered records remain in the outbox.

## Storage Model

Each run owns a standard-library SQLite ledger for canonical transitions and
outbox records. Existing JSON/JSONL records remain human-readable projections
during migration.

```text
.sdlc-runner/runs/<run-id>/runtime-events.sqlite3
```

Required logical tables are `transitions`, `events`, `outbox`,
`projection_status`, and `contract_audits`.

The shared learning store is outside target projects and configurable:

```text
$XDG_DATA_HOME/local-sdlc/learning/
  experience.sqlite3
  knowledge.sqlite3
  snapshots/
  evaluations/
```

It contains normalized events and evidence references, not unfiltered project
files.

## Episode Model and Causality

```text
episode = (context, evidence, hypothesis, intervention, delta, outcome)
```

An episode is eligible for causal learning only when pre-intervention evidence,
an identified intervention, known changed paths, post-intervention verification,
and stable comparable acceptance evidence exist, with no unresolved concurrent
intervention. Confounded changes remain case memory. Generated tests are labeled
by ownership and cannot independently establish product correctness.

## Knowledge Model

Each knowledge item records two independent dimensions:

- Scope: `structural`, `technology`, `project`, or `case`.
- Authority: fixed specification, mechanical observation, source analysis,
  verified official contract, repeated empirical pattern, case, or LLM
  hypothesis.

Required fields include:

```yaml
knowledge_id: stable-id
version: 1
kind: normative|descriptive|heuristic
scope: structural|technology|project|case
applicability: {}
antecedents: []
conclusion: {}
effect: observe|recommend|require|forbid
evidence_refs: []
supporting_projects: []
counterexamples: []
generalization_rationale: ""
regression_tests: []
authority: mechanical_observation
confidence: 0.0
state: candidate|shadow|active|challenged|retired
supersedes: []
created_by: deterministic|llm-assisted|human
```

Specificity does not imply authority. A project SPEC can be a strong normative
source while a general-looking LLM rule remains a weak hypothesis.

## Knowledge Lifecycle

```text
observed -> candidate -> shadow -> active
                         |          |
                         v          v
                      rejected   challenged -> revised|retired
```

The collector stores events without deciding final learning value. The episode
builder establishes evidence quality, the miner proposes the narrowest scope,
the validator searches for counterexamples, the promotion gate enforces policy,
and the registry publishes an immutable snapshot. Contradicting evidence
challenges rather than silently overwrites a rule.

For candidate `r`, the system tracks independent supporting domains `D(r)`,
precision lower bound `P(r)`, counterexamples `C(r)`, critical regressions
`R(r)`, and evidence quality `Q(r)`. A configurable low-impact structural
recommendation may be promoted only when every configured threshold passes,
including `R(r) = 0`.

## Anti-overfitting Evaluation

Every proposed general rule is evaluated with:

- original incident replay;
- renamed-path and renamed-symbol isomorphic cases;
- irrelevant-log and evidence-order perturbations;
- at least one non-matching negative case;
- known counterexamples;
- applicable Tetris, Mini SQLite, Redis, and unknown-task regressions; and
- holdout cases not used to author the rule.

Validation compares structured decisions, not explanatory wording. A rule that
depends on benchmark names remains case, project, or technology knowledge.

## LLM Responsibilities and API Isolation

Each LLM function is an independent API call with its own system prompt and
function profile.

| Function | Responsibility | Thinking | Activation authority |
|---|---|---:|---:|
| `episode_review` | Find missing causal evidence | on | none |
| `candidate_abstraction` | Propose the narrowest reusable proposition | on | none |
| `scope_classification` | Classify structural/technology/project/case | on | none |
| `counterexample_search` | Propose falsifying cases and guards | on | none |
| `candidate_serialization` | Produce schema-valid candidate data | off | none |
| `promotion_review` | Explain validation evidence | on | none |

`reasoning_content` may be restricted audit metadata but is never evidence or
executable knowledge. Only schema-valid `content` reaches mechanical validation.

## Safety, Privacy, and Control

- The learner has read-only access to run evidence and no target-source write
  authority.
- Collection rejects traversal and references outside configured run roots.
- Redaction occurs before shared persistence.
- Knowledge cannot contain shell commands with execution authority.
- Cancellation stops mining and validation at bounded checkpoints.
- Learner work has API-call, case-count, token, and wall-clock budgets.
- Promotion and rollback are append-only registry events.
- Provenance, cases, counterexamples, scope, effects, and validation results are
  inspectable before approval.

## Propositions and Acceptance Criteria

| ID | Proposition | Mechanical acceptance condition |
|---|---|---|
| EL01 | Every canonical transition has a registered event contract. | Exhaustive test fails for any transition enum missing from the registry. |
| EL02 | Transition and outbox event commit atomically. | Fault before commit leaves neither; fault after commit leaves a replayable outbox record. |
| EL03 | Learner availability is not required for execution. | With collector stopped, a run records outbox events and can otherwise complete. |
| EL04 | Collection is complete and idempotent. | Replaying an outbox produces one normalized event per ID and reports sequence gaps. |
| EL05 | Run and stage closure prove hook completeness. | Closure verifies the contract or persists `event_contract_violation`; silent success is impossible. |
| EL06 | Existing evidence is migratable without semantic loss. | Progress, safety, budget, stall, failure-analysis, and manifest fixtures import with provenance and stable hashes. |
| EL07 | Episodes preserve causality and reject confounded learning. | Atomic verified changes are eligible; concurrent unisolated changes remain case-only with a reason. |
| EL08 | Scope and authority are independent and explicit. | Schema rejects items missing scope, authority, applicability, evidence, or effect. |
| EL09 | LLM candidates cannot activate themselves. | Candidate output remains `candidate`; only the promotion gate can append activation. |
| EL10 | Generalization is tested against counterexamples and holdouts. | Structural activation requires replay, metamorphic, negative, and holdout passes with zero critical regressions. |
| EL11 | Knowledge is snapshot-isolated per run. | Promotion during run A does not change A; run B may select the new version. |
| EL12 | High-impact knowledge is human-controlled and reversible. | `require`, `forbid`, safety, and permission changes need human approval and support rollback. |

## Implementation Plan

### L00 - Baseline and Contract Inventory

Freeze passing execution tests, inventory every state mutation and event writer,
identify gateway bypasses, reproduce the failure-analysis memory gap, and create
P01/P02/P03/P09 legacy fixtures including malformed and missing JSONL records.

Exit: every canonical mutation maps to its current persistence path.

### L01 - Event Schema and Transition Contract

Define immutable transition, event, evidence-reference, and contract models;
define the exhaustive matrix; add the missing-contract test.

Exit: `EL01` passes without changing Supervisor behavior.

### L02 - Run-local Ledger and Transactional Outbox

Implement SQLite persistence, transactional event/outbox commit, idempotent IDs,
sequences, hash chaining, and compatibility projections.

Exit: `EL02` and `EL03` pass under fault injection and collector absence.

### L03 - Collector and Completeness Audit

Implement idempotent collection, gap/duplicate/schema/hash detection, closure
audits, and `collect`, `audit`, and `status` commands.

Exit: `EL04` and `EL05` pass.

### L04 - Legacy Adapters and Integration Gate A

Import progress, safety, budget, stall, manifest, failure-analysis, and
regression-memory fixtures; compare projections; route new P04/P05 transitions
through the gateway.

Exit: `EL06` and all existing tests pass. P04/P05 may resume.

### L05 - Normalization, Redaction, and Episode Builder

Status: completed on 2026-08-02.

Normalize identities, paths, symbols, evidence, and ownership; redact shared
records; build causal episode graphs; classify confounded episodes.

Implemented as an independent deterministic transformation over collected
events. It stores normalized episodes idempotently in the shared SQLite store
and exposes `build-episodes` without granting mutation or promotion authority.

Exit: `EL07` passes.

### L06 - Domain Map and Knowledge Schema

Status: completed on 2026-08-02.

Map concrete components to abstract roles; implement scope, authority,
applicability, effect, and proposition kinds.

Implemented as explicit project-local observations, a rename-invariant
structural projection, a strict KnowledgeItem schema, path-free evidence
anchors, scope-specific predicate validation, and a side-effect-free
applicability decision. It does not infer roles, mine candidates, persist a
registry, or activate knowledge.

Exit: `EL08` passes on two renamed structural fixtures and one genuinely
technology-specific fixture.

### L07 - Candidate Mining

Add isolated LLM functions, require schema-valid short propositions and evidence
references, retain exact incidents as cases, and forbid direct activation.

Exit: `EL09` passes with malformed and hostile candidate outputs.

### L08 - Validation and Shadow Evaluation

Implement replay, deterministic metamorphic variants, negative/holdout suites,
and counterexample/challenge recording.

Exit: `EL10` passes, including rejection of a benchmark-specific general rule.

### L09 - Registry, Promotion, and Rollback

Implement lifecycle transitions, approval policy, immutable hash-addressed
snapshots, challenge, supersession, retirement, rollback, and provenance.

Exit: `EL12` passes.

### L10 - Supervisor Retrieval Integration

Select a snapshot at run start, retrieve only applicable knowledge, pass short
structured conclusions, and record every retrieved item and effect.

Exit: `EL11` passes and an unknown-task regression receives no unrelated rule.

### L11 - Operations and UX

Add `doctor`, `inspect`, `explain`, `challenge`, and `rollback` views. Report
backlog, contract violations, candidate state, validation coverage, snapshot,
and storage size through CLI and later the Web adapter.

Exit: a user can understand and disable learned behavior without reading raw
database tables.

## Planned CLI

```text
python3 local_sdlc_learning.py collect --run-dir <path>
python3 local_sdlc_learning.py build-episodes --data-dir <path>
python3 local_sdlc_learning.py audit --run-dir <path>
python3 local_sdlc_learning.py consolidate
python3 local_sdlc_learning.py validate --candidate <id>
python3 local_sdlc_learning.py promote --candidate <id>
python3 local_sdlc_learning.py challenge --knowledge <id>
python3 local_sdlc_learning.py rollback --snapshot <id>
python3 local_sdlc_learning.py inspect <id>
python3 local_sdlc_learning.py doctor
```

CLI access alone is not proof of human approval. Approval identity and source
use the parent system's safety contract.

## Test Strategy

- unit tests for schemas, contracts, hashing, redaction, and scope;
- fault injection around transaction boundaries and outbox replay;
- exhaustive contract coverage over transition enum values;
- collector stop/restart integration tests;
- migration tests using frozen P01/P02/P03/P09 fixtures;
- property/metamorphic tests for renaming, ordering, and irrelevant noise;
- malicious LLM output tests;
- snapshot isolation and rollback tests;
- benchmark replay plus unrelated holdout regression; and
- privacy scans proving shared records omit sensitive values.

Tests assert structured reason codes and state, not explanatory prose.

## Risks and Controls

| Risk | Control |
|---|---|
| Hook omitted | Single transition gateway, exhaustive registry, closure audit |
| Learner outage stops work | Durable local outbox and asynchronous collection |
| One patch is treated as causal | Episode eligibility and confounding checks |
| Benchmark rule becomes universal | Scope model, isomorphic negatives, holdouts |
| LLM promotes itself | Candidate-only authority and promotion gate |
| Mid-run behavior changes | Immutable run-start snapshot |
| Sensitive data leaks globally | References, redaction, project fingerprinting |
| Bad active knowledge persists | Challenge, versioning, rollback, counterexamples |
| Schema changes break replay | Versioned schemas and migration adapters |

## Definition of Done

The project is complete only when `EL01` through `EL12` pass, existing
Supervisor tests pass, at least two project families produce episodes, one
structural candidate is correctly generalized, one technology or project
candidate remains scoped, one overfitted candidate is rejected, and a promoted
snapshot can be inspected and rolled back without modifying an active run.
