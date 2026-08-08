"""Supervisor routing and legacy supervise commands."""

from __future__ import annotations

import argparse
import json
import textwrap

from .models import *
from .utils import *
from .workspace import *
from .llm_client import *
from .skills import *
from .routing import *
from .verification import *
from .artifacts import *
from .run_state import *
from .stages import *
from .control import *
from .safety import *
from .budget import *
from .progress_monitor import *
from .action_gate import *
from .learning_context import (
    bind_learning_snapshot_from_args,
    knowledge_binding_manifest,
    knowledge_binding_path,
    knowledge_context_document,
)


def _write_supervisor_budget_stop(run_dir: Path, stop: dict[str, object]) -> None:
    partial = {
        "status": "budget_exhausted",
        "final_verdict": "budget_exhausted",
        "budget_stop": stop,
        "budget": budget_status(run_dir),
        "progress": progress_status(run_dir, evaluate=False),
        "action_gate_audit": action_gate_audit(run_dir),
        "pending_safety_decisions": pending_safety_decisions(run_dir),
        "blocked_safety_decisions": blocked_safety_decisions(run_dir),
    }
    write_run_document(
        run_dir,
        "run.partial.json",
        json.dumps(partial, ensure_ascii=False, indent=2),
    )


def _write_supervisor_stall(run_dir: Path, stall: dict[str, object]) -> None:
    partial = {
        "status": "stalled",
        "final_verdict": "stalled",
        "stall": stall,
        "progress": progress_status(run_dir, evaluate=False),
        "budget": budget_status(run_dir),
        "action_gate_audit": action_gate_audit(run_dir),
        "pending_safety_decisions": pending_safety_decisions(run_dir),
        "blocked_safety_decisions": blocked_safety_decisions(run_dir),
    }
    write_run_document(
        run_dir,
        "run.partial.json",
        json.dumps(partial, ensure_ascii=False, indent=2),
    )


def _begin_supervisor_action(
    client: LocalLLMClient,
    run_dir: Path,
    action: str,
    *,
    action_type: str,
    risk_class: str,
) -> None:
    try:
        begin_action(
            run_dir,
            action,
            action_type=action_type,
            risk_class=risk_class,
        )
    except BudgetExceeded as exc:
        _write_supervisor_budget_stop(run_dir, dict(exc.stop))
        raise
    except ProgressStalled as exc:
        _write_supervisor_stall(run_dir, dict(exc.stall))
        raise
    if action_type != "api_call":
        return
    set_timeout_limit = getattr(client, "set_runtime_timeout_limit", None)
    set_progress_callback = getattr(client, "set_runtime_progress_callback", None)
    set_completion_fallback = getattr(
        client,
        "set_runtime_completion_fallback_callback",
        None,
    )

    def enforce_api_deadline() -> None:
        try:
            enforce_wall_budget(run_dir, action, action_type=action_type)
        except BudgetExceeded as exc:
            _write_supervisor_budget_stop(run_dir, dict(exc.stop))
            raise
        try:
            enforce_progress_deadline(run_dir, action)
        except ProgressStalled as exc:
            _write_supervisor_stall(run_dir, dict(exc.stall))
            raise

    def bind_timeout() -> None:
        wall_remaining = remaining_wall_seconds(run_dir)
        progress_remaining = remaining_progress_seconds(run_dir)
        limits = [
            value
            for value in (wall_remaining, progress_remaining)
            if value is not None
        ]
        remaining = min(limits) if limits else None
        if callable(set_timeout_limit):
            set_timeout_limit(remaining, enforce_api_deadline)

    def record_stream_progress(stats: LLMStreamStats) -> None:
        observe_progress(
            run_dir,
            {
                "current_function": action,
                "stream_chunks": stats.chunks_received,
                "stream_bytes": stats.bytes_received,
                "reasoning_chunks": stats.reasoning_chunks,
            },
            source="llm_stream",
        )
        bind_timeout()

    if callable(set_progress_callback):
        set_progress_callback(record_stream_progress)
    if callable(set_completion_fallback):
        def authorize_completion_fallback(
            agent_level: str,
            call_function: str,
            reason: str,
        ) -> None:
            _begin_supervisor_action(
                client,
                run_dir,
                f"{action}:reasoning_condensation_fallback:{agent_level}:{call_function}:{reason}",
                action_type="api_call",
                risk_class="read_only",
            )

        set_completion_fallback(authorize_completion_fallback)
    if callable(set_timeout_limit):
        bind_timeout()


def command_supervisor(args: argparse.Namespace) -> int:
    project = args.project.resolve()
    skills = load_skills(args.skills_dir)
    agents_dir = args.agents_dir
    if not agents_dir.is_absolute():
        agents_dir = project / agents_dir
    supervisor_skill = load_prompt_asset(agents_dir / "supervisor.md", "supervisor")
    spec_path = project / "SPEC.md"
    spec = read_text_if_exists(spec_path)
    manifest_text = project_manifest(project)
    route = recommended_sdlc_phases(args.brief, spec)
    selected_phases = list(route.phases)
    if args.phases != "auto":
        selected_phases = [phase.strip() for phase in args.phases.split(",") if phase.strip()]
        unknown = [phase for phase in selected_phases if phase not in SDLC_PHASES]
        if unknown:
            raise RunnerError(f"unknown SDLC phase(s): {', '.join(unknown)}")
        route = dataclasses.replace(route, phases=tuple(selected_phases), reason=route.reason + "; phases=manual")

    if args.execute and "ambiguous" in route.danger_signals and not args.allow_ambiguous:
        raise RunnerError("ambiguous request detected; rerun with a clearer brief or --allow-ambiguous")

    client = LocalLLMClient(build_config(args))
    run_dir = make_run_dir(project, args.run_dir)
    initialize_budget(run_dir, budget_limits_from_args(args), scope_kind="goal_stage")
    initialize_progress_monitor(
        run_dir,
        progress_policy_from_args(args),
        scope_kind="goal_stage",
    )
    _begin_supervisor_action(
        client,
        run_dir,
        "supervisor_setup",
        action_type="orchestration",
        risk_class="read_only",
    )
    knowledge_binding = bind_learning_snapshot_from_args(args, project, run_dir)
    written: list[Path] = [knowledge_binding_path(run_dir)]
    documents: list[tuple[str, str]] = [
        ("Run-bound validated knowledge", knowledge_context_document(knowledge_binding))
    ]
    api_calls = 0

    def effective_api_calls() -> int:
        return max(api_calls, llm_generation_request_count(client))

    deterministic_route_doc = route_document(route)
    path = write_run_document(run_dir, "00-deterministic-route.md", deterministic_route_doc)
    written.append(path)
    documents.append(("Deterministic supervisor route", deterministic_route_doc))

    supervisor_instruction = textwrap.dedent(
        f"""
        Classify this user request and review the deterministic route.

        Request:
        {args.brief}

        Your job is supervisor-level routing only. Do not implement code.
        Confirm the task type, danger signals, recommended skill flow, and any
        gate that must block execution.
        """
    ).strip()
    _begin_supervisor_action(
        client,
        run_dir,
        "route_task_api_call",
        action_type="api_call",
        risk_class="read_only",
    )
    supervisor_doc = run_skill_call(
        client=client,
        skill=supervisor_skill,
        spec=spec,
        instruction=supervisor_instruction,
        agent_level="supervisor",
        project_manifest_text=manifest_text,
        documents=documents,
        output_contract="Return a supervisor routing document. Do not write implementation code.",
        call_function="route_task",
    )
    api_calls += 1
    path = write_run_document(run_dir, "01-supervisor-routing.md", supervisor_doc)
    written.append(path)
    documents.append(("Supervisor routing", supervisor_doc))

    if not args.execute:
        final_verdict = "planned"
        completed_phases: list[str] = []
    else:
        final_verdict = "completed"
        completed_phases = []
        file_context = collect_file_context(project, args.include, args.max_context_chars) if args.include else ""
        for index, phase in enumerate(selected_phases, start=1):
            skill = required_skill(skills, phase)
            _begin_supervisor_action(
                client,
                run_dir,
                f"phase_{phase}_api_call",
                action_type="api_call",
                risk_class="read_only",
            )
            output = run_skill_call(
                client=client,
                skill=skill,
                spec=spec,
                instruction=phase_instruction(phase, args.brief, route),
                agent_level=default_agent_level(phase),
                project_manifest_text=manifest_text,
                file_context=file_context,
                documents=documents[-8:],
                output_contract=(
                    f"Return the /{phase} phase document only. "
                    "No hidden-state assumptions. List missing context explicitly."
                ),
                call_function=default_call_function_for(default_agent_level(phase), phase),
            )
            api_calls += 1
            filename = f"{index + 1:02d}-{phase}.md"
            path = write_run_document(run_dir, filename, output)
            written.append(path)
            documents.append((f"/{phase} output", output))
            completed_phases.append(phase)
            if phase == "spec":
                spec = output
                if args.apply_spec:
                    _begin_supervisor_action(
                        client,
                        run_dir,
                        "apply_spec",
                        action_type="document_write",
                        risk_class="project_write",
                    )
                    spec_path.write_text(output.rstrip() + "\n", encoding="utf-8")
            elif args.append_phase_output:
                _begin_supervisor_action(
                    client,
                    run_dir,
                    f"append_{phase}_to_spec",
                    action_type="document_write",
                    risk_class="project_write",
                )
                append_to_spec(spec_path, phase, output)

    manifest_doc = {
        "brief": args.brief,
        "command": "supervisor",
        "task_type": route.task_type,
        "danger_signals": list(route.danger_signals),
        "recommended_phases": list(route.phases),
        "execute": bool(args.execute),
        "completed_phases": completed_phases,
        "final_verdict": final_verdict,
        "api_calls": effective_api_calls(),
        "llm_settings": llm_settings_manifest(client),
        "reasoning_records": llm_reasoning_manifest(client),
        "completion_recoveries": llm_completion_recovery_manifest(client),
        "health_recoveries": llm_health_recovery_manifest(client),
        "action_gate_audit": action_gate_audit(run_dir),
        "pending_safety_decisions": pending_safety_decisions(run_dir),
        "blocked_safety_decisions": blocked_safety_decisions(run_dir),
        "budget": budget_status(run_dir),
        "progress": progress_status(run_dir, evaluate=False),
        "knowledge_snapshot": knowledge_binding_manifest(knowledge_binding),
        "documents": [display_path(path, project) for path in written],
    }
    manifest_path = write_run_document(run_dir, "run.json", json.dumps(manifest_doc, ensure_ascii=False, indent=2))
    written.append(manifest_path)

    print(f"run_dir: {run_dir}")
    print(f"api_calls: {effective_api_calls()}")
    print(f"final_verdict: {final_verdict}")
    print(f"recommended_phases: {' -> '.join(route.phases) if route.phases else '(none)'}")
    for path in written:
        print(f"wrote: {path}")
    return 0

def command_supervise(args: argparse.Namespace) -> int:
    project = args.project.resolve()
    skills = load_skills(args.skills_dir)
    steps = parse_supervisor_steps(args.steps)
    spec_path = project / "SPEC.md"
    spec = read_text_if_exists(spec_path)

    if not spec and "spec" not in steps:
        raise RunnerError("SPEC.md is missing; include --steps spec,pm,coder,judge or run the spec command first")

    client = LocalLLMClient(build_config(args))
    run_dir = make_run_dir(project, args.run_dir)
    initialize_budget(run_dir, budget_limits_from_args(args), scope_kind="goal_stage")
    initialize_progress_monitor(
        run_dir,
        progress_policy_from_args(args),
        scope_kind="goal_stage",
    )
    _begin_supervisor_action(
        client,
        run_dir,
        "supervise_setup",
        action_type="orchestration",
        risk_class="read_only",
    )
    knowledge_binding = bind_learning_snapshot_from_args(args, project, run_dir)
    manifest = project_manifest(project)
    documents: list[tuple[str, str]] = [
        ("Run-bound validated knowledge", knowledge_context_document(knowledge_binding))
    ]
    written: list[Path] = [knowledge_binding_path(run_dir)]
    call_count = 0

    def effective_api_calls() -> int:
        return max(call_count, llm_generation_request_count(client))

    final_verdict = "not_judged"
    completed_rounds = 0

    if "spec" in steps:
        skill = required_skill(skills, "spec")
        instruction = textwrap.dedent(
            f"""
            Create or update SPEC.md as a PM-level specification document for this request.

            Request:
            {args.brief}
            """
        ).strip()
        _begin_supervisor_action(
            client,
            run_dir,
            "spec_api_call",
            action_type="api_call",
            risk_class="read_only",
        )
        spec_draft = run_skill_call(
            client=client,
            skill=skill,
            spec=spec,
            instruction=instruction,
            agent_level="pm",
            project_manifest_text=manifest,
            output_contract="Return the complete SPEC.md draft in Markdown.",
            call_function="plan_work",
        )
        call_count += 1
        path = write_run_document(run_dir, "01-spec-draft.md", spec_draft)
        written.append(path)
        documents.append(("SPEC draft", spec_draft))
        spec = spec_draft
        if args.apply_spec:
            _begin_supervisor_action(
                client,
                run_dir,
                "apply_spec",
                action_type="document_write",
                risk_class="project_write",
            )
            spec_path.write_text(spec_draft.rstrip() + "\n", encoding="utf-8")

    if "pm" in steps:
        skill = required_skill(skills, args.pm_skill)
        instruction = textwrap.dedent(
            f"""
            Act as the PM-level agent for this request.

            Request:
            {args.brief}

            Produce a control document for the coder and judge. Include:
            - Proposition Ledger with short P/C/G/E/A/V items
            - Graph Edges with supports/constrains/satisfies/verifies/blocks
            - intended outcome
            - fixed requirements that must not be changed
            - phase plan
            - risks and hallucination traps
            - evidence the coder must provide
            - acceptance checks the judge must use

            Do not write implementation code.
            """
        ).strip()
        _begin_supervisor_action(
            client,
            run_dir,
            "pm_api_call",
            action_type="api_call",
            risk_class="read_only",
        )
        pm_doc = run_skill_call(
            client=client,
            skill=skill,
            spec=spec,
            instruction=instruction,
            agent_level="pm",
            project_manifest_text=manifest,
            documents=documents,
            output_contract="Return a Markdown PM control document.",
            call_function="plan_work",
        )
        call_count += 1
        path = write_run_document(run_dir, "02-pm-control.md", pm_doc)
        written.append(path)
        documents.append(("PM control document", pm_doc))

    run_coder = "coder" in steps
    run_judge = "judge" in steps
    auto_loop = bool(args.auto_fix and run_coder and run_judge)
    rounds = args.max_rounds if auto_loop else 1
    if rounds < 1:
        raise RunnerError("--max-rounds must be at least 1")

    if run_coder:
        coder_skill = required_skill(skills, args.coder_skill)
        new_files = normalize_new_files(args.new_file)
        if not args.include and not args.allow_no_context and not new_files:
            raise RunnerError("coder step requires --include, --new-file, or explicit --allow-no-context")
        file_context = collect_file_context(project, args.include, args.max_context_chars)
        new_file_instruction = ""
        if new_files:
            new_file_instruction = "Create these new project-relative file(s): " + ", ".join(new_files)
    else:
        coder_skill = None
        file_context = ""
        new_file_instruction = ""

    judge_skill = required_skill(skills, args.judge_skill) if run_judge else None

    for round_index in range(1, rounds + 1):
        _begin_supervisor_action(
            client,
            run_dir,
            f"round_{round_index}_start",
            action_type="recovery" if round_index > 1 else "round_start",
            risk_class="read_only",
        )
        completed_rounds = round_index

        if run_coder and coder_skill is not None:
            repair_instruction = ""
            if round_index > 1:
                repair_instruction = textwrap.dedent(
                    f"""
                    This is repair round {round_index}. Read the prior Judge review
                    documents and address every Must / Required fix explicitly.
                    Do not repeat the previous patch unchanged.
                    """
                ).strip()
            instruction = textwrap.dedent(
                f"""
                Act as the coder-level agent for this request.

                Request:
                {args.brief}

                Round:
                {round_index} of {rounds}

                New file targets:
                {new_file_instruction or "(none)"}

                {repair_instruction}

                Use only SPEC.md, prior PM/Judge documents, project manifest,
                and included file contents. Produce a concrete implementation
                patch when enough file context or explicit new-file targets are
                available. If context is insufficient, return an exact
                MISSING_CONTEXT list instead of guessing.
                Internally reduce the edit to P/C/G/E/A propositions before
                producing the patch. Do not print that reasoning if returning a
                patch-only artifact.
                """
            ).strip()
            _begin_supervisor_action(
                client,
                run_dir,
                f"coder_round_{round_index}_api_call",
                action_type="api_call",
                risk_class="read_only",
            )
            coder_doc = run_skill_call(
                client=client,
                skill=coder_skill,
                spec=spec,
                instruction=instruction,
                agent_level="coder",
                project_manifest_text=manifest,
                file_context=file_context,
                documents=documents,
                output_contract="Return unified diff if possible; otherwise return MISSING_CONTEXT with exact file paths.",
                call_function="repair_artifact" if round_index > 1 else "generate_artifact",
            )
            call_count += 1
            path = write_run_document(run_dir, round_filename(round_index, "coder", auto_loop), coder_doc)
            written.append(path)
            documents.append((f"Coder output round {round_index}", coder_doc))

        if run_judge and judge_skill is not None:
            instruction = textwrap.dedent(
                f"""
                Act as the judge-level agent for this request.

                Request:
                {args.brief}

                Round:
                {round_index} of {rounds}

                Objectively review the latest coder output as a claim. Check it
                against SPEC.md, PM control documents, fixed requirements, and
                available evidence. Identify hallucinations, unsupported
                assumptions, missing tests, and incomplete acceptance checks.
                Do not assume the coder is correct because it sounds confident.
                Express major review points as short P/C/E/A/V propositions and
                Graph Edges before Required fixes.

                If this output is acceptable, start your response with
                "判定: 承認". If it needs more work, start with "判定: 修正依頼"
                and list concrete required fixes.
                """
            ).strip()
            _begin_supervisor_action(
                client,
                run_dir,
                f"judge_round_{round_index}_api_call",
                action_type="api_call",
                risk_class="read_only",
            )
            judge_doc = run_skill_call(
                client=client,
                skill=judge_skill,
                spec=spec,
                instruction=instruction,
                agent_level="judge",
                project_manifest_text=manifest,
                documents=documents,
                output_contract="Return Markdown with Verdict, Proposition Ledger, Graph Edges, Findings, Required fixes, and Evidence gaps.",
                call_function="judge_review",
            )
            call_count += 1
            path = write_run_document(run_dir, round_filename(round_index, "judge", auto_loop), judge_doc)
            written.append(path)
            documents.append((f"Judge review round {round_index}", judge_doc))

            if judge_approved(judge_doc):
                final_verdict = "approved"
                break
            final_verdict = "needs_changes"

        if not auto_loop:
            break

    manifest_doc = {
        "brief": args.brief,
        "steps": steps,
        "auto_fix": bool(args.auto_fix),
        "max_rounds": args.max_rounds,
        "completed_rounds": completed_rounds,
        "final_verdict": final_verdict,
        "api_calls": effective_api_calls(),
        "llm_settings": llm_settings_manifest(client),
        "reasoning_records": llm_reasoning_manifest(client),
        "completion_recoveries": llm_completion_recovery_manifest(client),
        "health_recoveries": llm_health_recovery_manifest(client),
        "action_gate_audit": action_gate_audit(run_dir),
        "pending_safety_decisions": pending_safety_decisions(run_dir),
        "blocked_safety_decisions": blocked_safety_decisions(run_dir),
        "budget": budget_status(run_dir),
        "progress": progress_status(run_dir, evaluate=False),
        "knowledge_snapshot": knowledge_binding_manifest(knowledge_binding),
        "documents": [display_path(path, project) for path in written],
    }
    manifest_path = write_run_document(run_dir, "run.json", json.dumps(manifest_doc, ensure_ascii=False, indent=2))
    written.append(manifest_path)

    print(f"run_dir: {run_dir}")
    print(f"api_calls: {effective_api_calls()}")
    for path in written:
        print(f"wrote: {path}")
    return 0
