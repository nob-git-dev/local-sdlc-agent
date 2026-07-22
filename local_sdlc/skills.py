"""Skill loading, prompt construction, and isolated skill calls."""

from __future__ import annotations

import datetime as _datetime
import inspect
import textwrap
from pathlib import Path
from typing import Any, Callable, Sequence

from .models import *

def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text

    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text

    raw = text[4:end]
    body = text[end + len("\n---\n") :]
    metadata: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip().strip('"')
    return metadata, body

def load_skills(skills_dir: Path) -> dict[str, Skill]:
    if not skills_dir.exists():
        raise RunnerError(f"skills directory not found: {skills_dir}")

    skills: dict[str, Skill] = {}
    for skill_file in sorted(skills_dir.glob("*/SKILL.md")):
        text = skill_file.read_text(encoding="utf-8")
        metadata, body = parse_front_matter(text)
        name = metadata.get("name") or skill_file.parent.name
        description = metadata.get("description", "")
        skills[name] = Skill(
            name=name,
            description=description,
            path=skill_file,
            body=body.strip(),
            metadata=metadata,
        )
    if not skills:
        raise RunnerError(f"no skills found under: {skills_dir}")
    return skills

def load_prompt_asset(path: Path, fallback_name: str) -> Skill:
    if not path.exists():
        raise RunnerError(f"prompt asset not found: {path}")
    text = path.read_text(encoding="utf-8")
    metadata, body = parse_front_matter(text)
    name = metadata.get("name") or fallback_name
    return Skill(
        name=name,
        description=metadata.get("description", ""),
        path=path,
        body=body.strip(),
        metadata=metadata,
    )

def system_message() -> str:
    return textwrap.dedent(
        """
        You are a local SDLC development agent running outside Claude Code.
        Preserve the original SDLC discipline:
        - Treat SPEC.md as the primary source of truth.
        - Do not silently change fixed requirements.
        - Do not invent dates, command results, test results, file contents, or
          environment facts. Use only supplied runtime facts and documents.
        - Prefer concrete commands and verifiable acceptance criteria.
        - For destructive or production-impacting operations, stop and ask for
          human approval instead of proceeding.
        - When asked for patches, return a unified diff only.
        """
    ).strip()

def formal_reasoning_contract() -> str:
    return textwrap.dedent(
        """
        ## Formal reasoning contract
        Local models are error-prone when tasks stay implicit. Reduce each task
        to short propositions and typed graph edges before acting.

        Notation:
        - Pn = premise: a supplied fact from SPEC.md, prior documents, file
          context, runtime facts, or command output.
        - Cn = constraint: a requirement that must not be violated.
        - Gn = goal: a desired state or behavior.
        - En = evidence: an observable command result, file fact, or smoke check.
        - An = action: an artifact or edit derived from P/C/G/E.
        - Vn = verdict: PASS/FAIL/UNKNOWN supported by evidence.
        - Dn = domain proposition: a term definition, invariant, state rule,
          aggregate boundary, or bounded-context rule.
        - Rn = requirement proposition: a required behavior or acceptance
          condition stated as a truth condition.
        - On = observation proposition: a concrete check, command, runtime
          observation, static assertion, or heuristic that may verify Rn.

        Process model:
        O = F(role, system_prompt, D, I), where D is the visible document set
        and I is the current instruction. Hidden memory is not part of D and is
        not valid evidence. Every claim in O must be supported by at least one
        visible proposition. If support is missing, output UNKNOWN or
        MISSING_CONTEXT instead of guessing.

        Graph model:
        Let G = (V, E) be the handoff graph for this API call.
        - V contains visible propositions P/C/G/E/A/V and document nodes such
          as SPEC.md, PM document, coder artifact, command output, and judge
          review.
        - E contains typed edges:
          supports(X, Y): X is evidence or a premise for Y.
          constrains(C, A): constraint C limits action A.
          satisfies(A, G): action A is intended to satisfy goal G.
          verifies(E, V): evidence E justifies verdict V.
          blocks(C, A): constraint C forbids action A.
          defines(D, R): domain proposition D gives meaning to requirement R.
          observes(O, R, relation): observation O checks requirement R with
            relation in {equivalent, sufficient, necessary, proxy}.

        Sequent rule:
        Emit an action or verdict only when a visible derivation exists:

        P* and C* and G* and E* |- A or V

        If no derivation exists, return UNKNOWN or MISSING_CONTEXT. Never fill
        missing graph edges with hidden assumptions.

        Domain verification rule:
        A failed proxy/static/heuristic observation is not automatically a
        product failure when a stronger runtime/equivalent observation passes.
        Classify such conflicts as spec, harness, or supervisor mismatch and
        preserve previously passing stronger evidence. A product repair is
        justified only when the failing observation has a visible relation edge
        strong enough to falsify the requirement truth condition.

        Practical rule:
        Prefer 5-9 short propositions and 3-7 important graph edges over a
        long explanation. When the output contract forbids prose, use the
        propositions and graph internally and emit only the required artifact
        format.
        """
    ).strip()

def runtime_facts() -> str:
    now = _datetime.datetime.now().astimezone()
    return "\n".join(
        [
            f"- Current local datetime: {now.isoformat(timespec='seconds')}",
            f"- Current local date: {now.date().isoformat()}",
            f"- Local timezone: {now.tzname() or 'local'}",
        ]
    )

def agent_level_contract(agent_level: str) -> str:
    contracts = {
        "supervisor": """
        You are the deterministic supervisor controller. You do not implement
        product code. You decide which independent agents should run and ensure
        every handoff is represented as an auditable document.

        Role invariant:
        route_task(P, C, risk) -> ordered agent calls. A route is valid only
        when every required gate has a visible reason edge.
        """,
        "pm": """
        You are the PM-level agent. Your job is to clarify purpose, scope,
        architecture direction, risks, acceptance criteria, and the next
        verifiable plan. Do not produce implementation code unless explicitly
        asked for a small illustrative snippet. If implementation is needed,
        hand it to a coder-level agent through a written document.

        Role invariant:
        plan_work(P, C, G) -> acceptance criteria and authorized next actions.
        A plan is invalid when it lacks a testable goal or violates a fixed
        constraint.
        """,
        "coder": """
        You are the coder-level agent. Your job is to implement only what the
        written SPEC.md and PM documents authorize. Do not invent requirements,
        do not silently change fixed requirements, and do not mark your own work
        complete. Never claim that you inspected code unless exact file contents
        were supplied in the user message. A project manifest is not code
        context. Return concrete patches or exact missing inputs.

        Role invariant:
        generate_or_repair_artifact(P, C, G, E) -> minimal A. A is invalid
        when it changes tests to hide product-code failure, rewrites unrelated
        files, or lacks a visible support edge from the supplied documents.
        """,
        "judge": """
        You are the judge-level agent. Your job is objective review. Treat the
        coder output as a claim, not as truth. Verify against SPEC.md, PM
        documents, tests, and observable evidence. Prefer explicit PASS/FAIL
        findings and do not repair the code yourself unless asked separately.
        Never accept code references that are not supported by supplied file
        contents or command output.

        Role invariant:
        judge_review(P, C, A, E) -> V. V=PASS is valid only when command/file
        evidence verifies every required acceptance criterion and no blocking
        constraint violation remains.
        """,
    }
    try:
        return textwrap.dedent(contracts[agent_level]).strip()
    except KeyError as exc:
        raise RunnerError(f"unknown agent level: {agent_level}") from exc

def default_agent_level(skill_name: str) -> str:
    if skill_name in {"tdd", "ui", "refactor"}:
        return "coder"
    if skill_name in {"review", "security"}:
        return "judge"
    return "pm"

def skill_system_prompt(skill: Skill, agent_level: str) -> str:
    return textwrap.dedent(
        f"""
        {system_message()}

        {formal_reasoning_contract()}

        ## Agent isolation contract
        This API call is independent. You do not share hidden conversation
        history, scratchpads, memory, or unstated assumptions with any other
        agent. Use only the documents supplied in the user message and the
        system instructions in this message.

        ## Agent level
        {agent_level}

        {agent_level_contract(agent_level)}

        ## Skill
        Name: {skill.name}
        Description: {skill.description}
        Source: {skill.path}

        ## SKILL.md instructions
        {skill.body}
        """
    ).strip()

def document_exchange_prompt(
    spec: str,
    instruction: str,
    project_manifest_text: str = "",
    file_context: str = "",
    documents: Sequence[tuple[str, str]] = (),
    output_contract: str = "",
) -> str:
    doc_blocks: list[str] = []
    for title, content in documents:
        doc_blocks.append(f"### {title}\n{content.strip() or '(empty document)'}")
    docs = "\n\n".join(doc_blocks) if doc_blocks else "(no prior agent documents)"

    return textwrap.dedent(
        f"""
        ## Document exchange contract
        Treat every prior agent output as a document, not as shared memory.
        If a required fact is missing from SPEC.md or these documents, say so.
        Do not rely on hidden context from previous API calls.

        ## Runtime facts
        {runtime_facts()}

        ## SPEC.md
        {spec or "(SPEC.md does not exist yet.)"}

        ## Prior agent documents
        {docs}

        ## Project manifest
        {project_manifest_text or "(not provided)"}

        ## Included file contents
        {file_context or "(not provided)"}

        ## Current instruction
        {instruction}

        ## Proposition discipline
        For planning or review text, define short propositions before
        conclusions:
        - Pn: supplied premise
        - Cn: fixed constraint
        - Gn: goal
        - En: observable evidence
        - An: proposed action or artifact
        - Vn: supported verdict
        - Dn: domain term, invariant, state rule, or context boundary
        - Rn: requirement truth condition
        - On: concrete observation or check for Rn
        Keep each proposition one sentence.
        Do not create propositions from hidden memory.

        ## Graph discipline
        For planning or review text, include important dependency edges:
        - supports(Pn or En, Gn or Vn)
        - constrains(Cn, An)
        - satisfies(An, Gn)
        - verifies(En, Vn)
        - blocks(Cn, An)
        - defines(Dn, Rn)
        - observes(On, Rn, relation=equivalent|sufficient|necessary|proxy)
        Omit graph prose when the output contract requires artifact-only
        output, but still use the graph internally before emitting the
        artifact.

        ## Output contract
        {output_contract or "Return a concise Markdown result suitable for human review."}
        """
    ).strip()

def skill_messages(
    skill: Skill,
    spec: str,
    instruction: str,
    agent_level: str,
    project_manifest_text: str = "",
    file_context: str = "",
    documents: Sequence[tuple[str, str]] = (),
    output_contract: str = "",
) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": skill_system_prompt(skill, agent_level)},
        {
            "role": "user",
            "content": document_exchange_prompt(
                spec=spec,
                instruction=instruction,
                project_manifest_text=project_manifest_text,
                file_context=file_context,
                documents=documents,
                output_contract=output_contract,
            ),
        },
    ]

def run_skill_call(
    client: LocalLLMClient,
    skill: Skill,
    spec: str,
    instruction: str,
    agent_level: str,
    project_manifest_text: str = "",
    file_context: str = "",
    documents: Sequence[tuple[str, str]] = (),
    output_contract: str = "",
    stream_output_path: Path | None = None,
    stream_callback: Callable[[LLMStreamStats], None] | None = None,
    stream_guard: Callable[[str], ArtifactStreamGuardResult] | None = None,
    call_function: str = "",
) -> str:
    messages = skill_messages(
        skill=skill,
        spec=spec,
        instruction=instruction,
        agent_level=agent_level,
        project_manifest_text=project_manifest_text,
        file_context=file_context,
        documents=documents,
        output_contract=output_contract,
    )
    parameters = inspect.signature(client.complete).parameters
    if "agent_level" in parameters:
        kwargs: dict[str, Any] = {"agent_level": agent_level}
        if "call_function" in parameters:
            kwargs["call_function"] = call_function or default_call_function_for(agent_level, skill.name)
        if "stream_output_path" in parameters:
            kwargs["stream_output_path"] = stream_output_path
        if "stream_callback" in parameters:
            kwargs["stream_callback"] = stream_callback
        if "stream_guard" in parameters:
            kwargs["stream_guard"] = stream_guard
        return client.complete(messages, **kwargs)
    return client.complete(messages)
