"""Application-level coding agent execution loop."""

from __future__ import annotations

import argparse
import json
import re
import textwrap
from pathlib import Path
from typing import Mapping, Sequence

from .models import *
from .utils import *
from .workspace import *
from .llm_client import *
from .skills import *
from .routing import *
from .verification import *
from .artifacts import *
from .control import *
from .safety import *
from .run_state import *
from .stages import *
from .history import *
from .requirements import requirements_from_spec, observables_for_requirements
from .evidence import verdict_to_manifest, verdicts_from_acceptance_matrix
from .agent_prompts import *
from .artifact_transaction import *
from .domain_modeling import domain_modeling_decision
from .policy_triage import (
    apply_project_policy_triage_to_advice as apply_triage_to_advice,
    authorized_test_edit_paths,
    project_policy_triage_enabled as triage_is_enabled,
    triage_allows_test_harness_edit,
    triage_string_list,
)
from .budget import *
from .progress_monitor import *
from .action_gate import *
from .recovery import *


def attach_regression_memory(
    manifest: dict[str, object],
    run_dir: Path,
    project: Path,
    written: list[Path],
) -> None:
    persisted = persist_regression_memories_for_manifest(project, run_dir, manifest)
    if persisted is None:
        return
    run_path, store_path, record_count, store_record_count = persisted
    written.append(run_path)
    manifest["regression_memory"] = {
        "document": display_path(run_path, project),
        "record_count": record_count,
        "store": display_path(store_path, project),
        "store_record_count": store_record_count,
    }
    manifest["documents"] = [display_path(path, project) for path in written]


def command_agent(args: argparse.Namespace) -> int:
    original_project = args.project.resolve()
    project = original_project
    skills = load_skills(args.skills_dir)
    spec_path = resolve_spec_path(original_project, args.spec_file)
    spec = read_text_if_exists(spec_path)
    if not spec:
        raise RunnerError("SPEC.md is required before running the coding agent; pass --spec-file or create SPEC.md")

    new_files = normalize_new_files(args.new_file)
    context_files = normalize_new_files(getattr(args, "context", []))
    context_slices = parse_context_slices(getattr(args, "context_slice", []))
    slice_context_files = list(context_slices.keys())
    if not args.include and not args.allow_no_context and not new_files:
        raise RunnerError("agent requires --include, --new-file, or explicit --allow-no-context")
    if args.max_rounds < 1:
        raise RunnerError("--max-rounds must be at least 1")
    protocol_repair_rounds = int(getattr(args, "protocol_repair_rounds", 0) or 0)
    if protocol_repair_rounds < 0:
        raise RunnerError("--protocol-repair-rounds must be zero or greater")
    adaptive_round_budget = int(getattr(args, "adaptive_rounds", 0) or 0)
    if adaptive_round_budget < 0:
        raise RunnerError("--adaptive-rounds must be zero or greater")
    if args.document_window < 1:
        raise RunnerError("--document-window must be at least 1")
    explicit_required_paths = normalize_new_files(args.require_path)
    auto_required_paths = list(new_files)
    required_paths = unique_ordered([*explicit_required_paths, *auto_required_paths])

    pm_skill = required_skill(skills, args.pm_skill)
    coder_skill = required_skill(skills, args.coder_skill)
    judge_skill = required_skill(skills, args.judge_skill)
    resume_manifest: dict[str, object] = {}
    resume_documents: list[tuple[str, str]] = []
    resume_paths: list[Path] = []
    recovery_plan: dict[str, object] = {}
    recovery_source: Path | None = None
    if args.resume:
        resume_dir = args.resume.resolve()
        resume_manifest, resume_documents, resume_paths = load_resume_context(resume_dir, original_project)
        raw_recovery_plan = getattr(args, "recovery_plan", None)
        if raw_recovery_plan is not None:
            recovery_plan = validate_recovery_plan(resume_dir, Path(raw_recovery_plan))
        else:
            recovery_plan = require_recovery_plan_for_resume(resume_dir, None)
        if recovery_plan:
            recovery_source = resume_dir
            planned_target = Path(str(recovery_plan["target_run_dir"])).resolve()
            if args.run_dir is not None and resolve_run_dir(original_project, args.run_dir).resolve() != planned_target:
                raise InvalidRecoveryPlan("--run-dir does not match recovery_plan.json target_run_dir")
            target_profile = str(recovery_plan.get("target_profile") or "")
            configured_profile = str(getattr(args, "model_profile", None) or "")
            if target_profile and configured_profile and configured_profile != target_profile:
                raise InvalidRecoveryPlan(
                    "--model-profile does not match recovery_plan.json target_profile"
                )
            if target_profile:
                args.model_profile = target_profile
            run_dir = make_run_dir(original_project, planned_target)
        else:
            run_dir = make_run_dir(original_project, args.run_dir or resume_dir)
    else:
        if getattr(args, "recovery_plan", None) is not None:
            raise InvalidRecoveryPlan("--recovery-plan requires --resume")
        run_dir = make_run_dir(original_project, args.run_dir)
    client = LocalLLMClient(build_config(args))

    control_dirs = tuple(
        Path(raw).resolve()
        for raw in (getattr(args, "control_dir", []) or [])
    )
    explicit_cancel_dirs = getattr(args, "cancel_control_dir", None)
    explicit_budget_dirs = getattr(args, "budget_control_dir", None)
    explicit_progress_dirs = getattr(args, "progress_control_dir", None)
    cancel_control_dirs = tuple(
        Path(raw).resolve()
        for raw in (control_dirs if explicit_cancel_dirs is None else explicit_cancel_dirs)
    )
    budget_control_dirs = tuple(
        Path(raw).resolve()
        for raw in (control_dirs if explicit_budget_dirs is None else explicit_budget_dirs)
    )
    progress_control_dirs = tuple(
        Path(raw).resolve()
        for raw in (control_dirs if explicit_progress_dirs is None else explicit_progress_dirs)
    )
    if recovery_source is not None:
        cancel_control_dirs = tuple(dict.fromkeys((recovery_source, *cancel_control_dirs)))
        budget_control_dirs = tuple(dict.fromkeys((recovery_source, *budget_control_dirs)))
        if explicit_progress_dirs is None:
            # A recovery attempt has a fresh liveness clock. Its stalled source
            # remains immutable evidence and must not block the new attempt.
            progress_control_dirs = ()
    recovery_metadata = (
        {"recovery_authorization": recovery_authorization(recovery_plan)}
        if recovery_plan
        else {}
    )
    initialize_budget(
        run_dir,
        budget_limits_from_args(args),
        scope_kind="stage" if control_dirs else "goal_stage",
    )
    initialize_progress_monitor(
        run_dir,
        progress_policy_from_args(args),
        scope_kind="stage" if control_dirs else "goal_stage",
    )
    try:
        if recovery_source is not None:
            begin_stalled_recovery(
                recovery_source,
                run_dir,
                recovery_plan,
                cancel_dirs=cancel_control_dirs,
                budget_dirs=budget_control_dirs,
                progress_dirs=progress_control_dirs,
            )
        else:
            begin_action(
                run_dir,
                "agent_setup",
                action_type="resume" if args.resume else "orchestration",
                risk_class="read_only",
                cancel_dirs=cancel_control_dirs,
                budget_dirs=budget_control_dirs,
                progress_dirs=progress_control_dirs,
            )
    except ProgressStalled as exc:
        write_run_document(
            run_dir,
            "run.partial.json",
            json.dumps(
                {
                    "brief": args.brief,
                    "command": "agent",
                    "status": "stalled",
                    "final_verdict": "stalled",
                    "stall": dict(exc.stall),
                    "progress": progress_status(run_dir, evaluate=False),
                    "budget": budget_status(run_dir),
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        raise

    resume_worktree_source: Path | None = None
    if args.resume_worktree_path:
        resume_worktree_source = args.resume_worktree_path.resolve()
        if not resume_worktree_source.exists() or not resume_worktree_source.is_dir():
            raise RunnerError(f"resume worktree path is not available: {resume_worktree_source}")
    elif args.resume_worktree:
        if not resume_manifest:
            raise RunnerError("--resume-worktree requires --resume")
        raw_worktree_path = str(resume_manifest.get("worktree_path") or "")
        if not raw_worktree_path:
            raise RunnerError("resumed run does not record a worktree_path")
        resume_worktree_source = Path(raw_worktree_path)
        if not resume_worktree_source.exists() or not resume_worktree_source.is_dir():
            raise RunnerError(f"resumed worktree_path is not available: {resume_worktree_source}")

    worktree_path: Path | None = None
    if args.worktree_mode == "copy":
        begin_action(
            run_dir,
            "worktree_create",
            action_type="worktree_create",
            risk_class="project_write",
            metadata={"isolated": True, **recovery_metadata},
            cancel_dirs=cancel_control_dirs,
            budget_dirs=budget_control_dirs,
            progress_dirs=progress_control_dirs,
        )
        worktree_path = create_copy_worktree(resume_worktree_source or original_project)
        project = worktree_path

    manifest_text = project_manifest(project)
    existing_project_paths = listed_project_files(project)
    stage_scope_test_paths = unique_ordered(
        path for path in [*required_paths, *new_files] if path.startswith("tests/")
    )
    stage_generated_test_paths = unique_ordered(
        path
        for path in [*required_paths, *new_files]
        if path.startswith("tests/") and path not in existing_project_paths
    )

    written: list[Path] = []
    documents: list[tuple[str, str]] = []
    api_calls = int(resume_manifest.get("api_calls", 0)) if resume_manifest else 0
    final_verdict = "not_judged"
    recovery_strategy = str(recovery_plan.get("strategy") or "")
    force_root_cause_recovery = recovery_strategy in ANALYTIC_RECOVERY_STRATEGIES
    previous_completed_rounds = int(resume_manifest.get("completed_rounds", 0)) if resume_manifest else 0
    completed_rounds = previous_completed_rounds
    requirement_records = requirements_from_spec(spec, display_path(spec_path, original_project))
    observable_records = observables_for_requirements(requirement_records)
    acceptance_criteria = [
        {"id": item.requirement_id, "text": item.text}
        for item in requirement_records
    ]
    evidence_records: list[dict[str, object]] = list(resume_manifest.get("evidence", [])) if resume_manifest else []
    final_failure_type: str | None = None
    if force_root_cause_recovery:
        final_failure_type = "repeated_same_failure"
    protocol_rounds_used = int(resume_manifest.get("protocol_rounds_used", 0)) if resume_manifest else 0
    functional_rounds_used = int(resume_manifest.get("functional_rounds_used", 0)) if resume_manifest else 0
    adaptive_rounds_used = int(resume_manifest.get("adaptive_rounds_used", 0)) if resume_manifest else 0
    protocol_failure_types_seen: set[str] = set()
    root_cause_patch_round_budget = int(getattr(args, "root_cause_patch_rounds", 1) or 0)
    if root_cause_patch_round_budget < 0:
        raise RunnerError("--root-cause-patch-rounds must be zero or greater")
    root_cause_patch_rounds_used = (
        int(resume_manifest.get("root_cause_patch_rounds_used", 0)) if resume_manifest else 0
    )
    root_cause_patch_pending = False
    last_functional_failure_score = resume_manifest.get("last_functional_failure_score") if resume_manifest else None
    if not isinstance(last_functional_failure_score, int):
        last_functional_failure_score = None
    last_functional_failure_signature = (
        resume_manifest.get("last_functional_failure_signature") if resume_manifest else None
    )
    if not isinstance(last_functional_failure_signature, str):
        last_functional_failure_signature = None
    last_functional_failure_family_signature = (
        resume_manifest.get("last_functional_failure_family_signature") if resume_manifest else None
    )
    if not isinstance(last_functional_failure_family_signature, str):
        last_functional_failure_family_signature = None
    repeated_same_failure_count = int(resume_manifest.get("repeated_same_failure_count", 0)) if resume_manifest else 0
    copied_back: list[str] = []
    changed_paths: list[str] = list(resume_manifest.get("changed_paths", [])) if resume_manifest else []
    latest_stream_status: dict[str, object] = {}
    latest_repair_advice: dict[str, object] = {}
    pending_deterministic_repair: dict[str, object] | None = None
    failure_analyses: list[dict[str, object]] = [
        item for item in resume_manifest.get("failure_analyses", []) if isinstance(item, dict)
    ] if resume_manifest else []
    project_policy_triages: list[dict[str, object]] = [
        item for item in resume_manifest.get("project_policy_triages", []) if isinstance(item, dict)
    ] if resume_manifest else []
    domain_modeling_state = domain_modeling_decision(args, skills, spec, resume_documents)
    state_transitions: list[dict[str, object]] = list(resume_manifest.get("state_transitions", [])) if resume_manifest else []
    semantic_contracts: list[SemanticContract] = []
    if resume_manifest:
        for item in resume_manifest.get("semantic_contracts", []):
            if not isinstance(item, dict):
                continue
            semantic_contracts.append(
                SemanticContract(
                    contract_id=str(item.get("id") or f"C{len(semantic_contracts) + 1:02d}"),
                    kind=str(item.get("kind") or "api_contract"),
                    text=str(item.get("text") or ""),
                    source=str(item.get("source") or "resume"),
                    focus_files=tuple(str(path) for path in item.get("focus_files", []) if isinstance(path, str)),
                    evidence=tuple(str(value) for value in item.get("evidence", []) if isinstance(value, str)),
                )
            )

    def guard_action(
        action: str,
        *,
        action_type: str = "orchestration",
        risk_class: str = "read_only",
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        nonlocal final_verdict, final_failure_type
        action_metadata = {**recovery_metadata, **dict(metadata or {})}
        try:
            begin_action(
                run_dir,
                action,
                action_type=action_type,
                risk_class=risk_class,
                metadata=action_metadata,
                cancel_dirs=cancel_control_dirs,
                budget_dirs=budget_control_dirs,
                progress_dirs=progress_control_dirs,
            )
        except BudgetExceeded as exc:
            final_verdict = "budget_exhausted"
            final_failure_type = "budget_exhausted"
            write_partial_manifest(
                "budget_exhausted",
                {"budget_stop": dict(exc.stop)},
            )
            raise
        except ProgressStalled as exc:
            final_verdict = "stalled"
            final_failure_type = "stalled"
            write_partial_manifest(
                "stalled",
                {"stall": dict(exc.stall)},
            )
            raise
        if action_type == "api_call":
            def enforce_api_deadline() -> None:
                nonlocal final_verdict, final_failure_type
                try:
                    enforce_wall_budget(
                        run_dir,
                        action,
                        action_type=action_type,
                        budget_dirs=budget_control_dirs,
                    )
                except BudgetExceeded as exc:
                    final_verdict = "budget_exhausted"
                    final_failure_type = "budget_exhausted"
                    write_partial_manifest(
                        "budget_exhausted",
                        {"budget_stop": dict(exc.stop)},
                    )
                    raise
                try:
                    enforce_progress_deadline(
                        run_dir,
                        action,
                        control_dirs=progress_control_dirs,
                    )
                except ProgressStalled as exc:
                    final_verdict = "stalled"
                    final_failure_type = "stalled"
                    write_partial_manifest(
                        "stalled",
                        {"stall": dict(exc.stall)},
                    )
                    raise

            set_timeout_limit = getattr(client, "set_runtime_timeout_limit", None)
            set_progress_callback = getattr(client, "set_runtime_progress_callback", None)

            def bind_timeout() -> None:
                wall_remaining = remaining_wall_seconds(run_dir, budget_control_dirs)
                progress_remaining = remaining_progress_seconds(run_dir, progress_control_dirs)
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
                    control_dirs=progress_control_dirs,
                )
                bind_timeout()

            if callable(set_progress_callback):
                set_progress_callback(record_stream_progress)
            bind_timeout()

    def write_partial_manifest(status: str, extra: dict[str, object] | None = None) -> None:
        if status not in {
            "stalled",
            "budget_exhausted",
            "approval_required",
            "safety_blocked",
            "cancelled",
        }:
            observe_progress(
                run_dir,
                {
                    "round": completed_rounds,
                    "documents_count": len(written),
                    "evidence_count": len(evidence_records),
                    "changed_paths_hash": progress_vector_hash(
                        {"changed_paths_hash": "|".join(sorted(set(changed_paths)))}
                    ),
                },
                source=f"manifest:{status}",
                control_dirs=progress_control_dirs,
            )
        acceptance_matrix = build_acceptance_matrix(acceptance_criteria, evidence_records)
        partial_doc: dict[str, object] = {
            "brief": args.brief,
            "command": "agent",
            "status": status,
            "apply": bool(args.apply),
            "requested_max_rounds": args.max_rounds,
            "max_rounds": args.max_rounds + protocol_repair_rounds,
            "functional_round_budget": args.max_rounds,
            "protocol_repair_round_budget": protocol_repair_rounds,
            "adaptive_round_budget": adaptive_round_budget,
            "root_cause_patch_round_budget": root_cause_patch_round_budget,
            "functional_rounds_used": functional_rounds_used,
            "protocol_rounds_used": protocol_rounds_used,
            "adaptive_rounds_used": adaptive_rounds_used,
            "root_cause_patch_rounds_used": root_cause_patch_rounds_used,
            "last_functional_failure_score": last_functional_failure_score,
            "last_functional_failure_signature": last_functional_failure_signature,
            "last_functional_failure_family_signature": last_functional_failure_family_signature,
            "repeated_same_failure_count": repeated_same_failure_count,
            "resumed_from": str(args.resume.resolve()) if args.resume else None,
            "recovery_plan_id": recovery_plan.get("plan_id") if recovery_plan else None,
            "recovery_strategy": recovery_plan.get("strategy") if recovery_plan else None,
            "artifact_format": args.artifact_format,
            "small_patch": bool(args.small_patch),
            "no_replace_file": bool(args.no_replace_file),
            "allow_extra_new_files": not bool(args.no_extra_files),
            "precheck": bool(args.precheck),
            "worktree_mode": args.worktree_mode,
            "worktree_path": str(worktree_path) if worktree_path else None,
            "resumed_worktree_from": str(resume_worktree_source) if resume_worktree_source else None,
            "copied_back": copied_back,
            "changed_paths": unique_ordered(changed_paths),
            "completed_rounds": completed_rounds,
            "final_verdict": final_verdict,
            "final_failure_type": final_failure_type,
            "api_calls": api_calls,
            "model_profile": llm_model_profile_manifest(args),
            "llm_settings": llm_settings_manifest(client),
            "reasoning_records": llm_reasoning_manifest(client),
            "domain_modeling": domain_modeling_state,
            "test_commands": list(args.test_command or []),
            "context_paths": context_files,
            "context_slices": {path: ranges for path, ranges in context_slices.items()},
            "required_paths": required_paths,
            "explicit_required_paths": explicit_required_paths,
            "auto_required_paths": auto_required_paths,
            "stage_scope_test_paths": stage_scope_test_paths,
            "stage_generated_test_paths": stage_generated_test_paths,
            "acceptance_criteria": acceptance_criteria,
            "requirements": [item.to_manifest() for item in requirement_records],
            "observables": [item.to_manifest() for item in observable_records],
            "propositions": proposition_manifest_from_documents(documents),
            "semantic_contracts": [semantic_contract_to_dict(contract) for contract in semantic_contracts],
            "evidence": evidence_records,
            "acceptance_matrix": acceptance_matrix,
            "verdicts": [
                verdict_to_manifest(item)
                for item in verdicts_from_acceptance_matrix(acceptance_matrix)
            ],
            "acceptance_blockers": acceptance_blockers(acceptance_matrix),
            "failure_summary": failure_summary(final_verdict, evidence_records, final_failure_type),
            "failure_analyses": failure_analyses,
            "project_policy_triages": project_policy_triages,
            "state_transitions": state_transitions,
            "cancel_requested": cancel_requested(run_dir),
            "cancel_state": load_cancel_state(run_dir) or None,
            "progress_log": display_path(progress_file_path(run_dir), original_project),
            "progress_event_count": len(read_progress_events(run_dir)),
            "safety_decisions_log": display_path(safety_decisions_file_path(run_dir), original_project),
            "safety_decision_count": len(read_safety_decisions(run_dir)),
            "safety_approvals_log": display_path(safety_approvals_file_path(run_dir), original_project),
            "safety_approval_event_count": len(read_safety_approvals(run_dir)),
            "pending_safety_decisions": pending_safety_decisions(run_dir),
            "blocked_safety_decisions": blocked_safety_decisions(run_dir),
            "action_gate_audit": action_gate_audit(run_dir),
            "budget": budget_status(run_dir),
            "progress": progress_status(run_dir, evaluate=False),
            "documents": [display_path(path, original_project) for path in written],
        }
        if latest_stream_status:
            partial_doc["streaming"] = dict(latest_stream_status)
        if latest_repair_advice:
            partial_doc["repair_advice"] = dict(latest_repair_advice)
        if pending_deterministic_repair:
            partial_doc["pending_deterministic_repair"] = pending_deterministic_repair
        if extra:
            partial_doc.update(extra)
        write_run_document(run_dir, "run.partial.json", json.dumps(partial_doc, ensure_ascii=False, indent=2))

    def stop_for_command_safety_decision(current_round: int | None = None) -> None:
        pending = pending_safety_decisions(run_dir)
        blocked = blocked_safety_decisions(run_dir)
        if not pending and not blocked:
            return
        status = "safety_blocked" if blocked else "approval_required"
        extra: dict[str, object] = {
            "final_verdict": status,
            "final_failure_type": status,
            "pending_safety_decisions": pending,
            "blocked_safety_decisions": blocked,
        }
        if current_round is not None:
            extra["current_round"] = current_round
        write_partial_manifest(status, extra)
        decision = (blocked or pending)[-1]
        raise RunnerError(
            f"{status} before command execution: "
            + str(decision.get("decision_id") or "unknown")
        )

    def run_agent_checked_command(
        command: str,
        *,
        action: str,
    ) -> tuple[str, bool]:
        nonlocal final_verdict, final_failure_type
        try:
            return run_checked_command(
                project,
                command,
                args.command_timeout,
                run_dir,
                action=action,
                cancel_dirs=cancel_control_dirs,
                budget_dirs=budget_control_dirs,
                progress_dirs=progress_control_dirs,
                metadata=recovery_metadata,
            )
        except ProgressStalled as exc:
            final_verdict = "stalled"
            final_failure_type = "stalled"
            write_partial_manifest("stalled", {"stall": dict(exc.stall)})
            raise
        except BudgetExceeded as exc:
            final_verdict = "budget_exhausted"
            final_failure_type = "budget_exhausted"
            write_partial_manifest(
                "budget_exhausted",
                {"budget_stop": dict(exc.stop)},
            )
            raise

    def record_transition(failure_type: str, round_index: int, evidence: str = "") -> FailureTransition:
        transition = transition_for_failure(failure_type)
        state_transitions.append(
            {
                "round": round_index,
                "failure_type": transition.failure_type,
                "owner": transition.owner,
                "next_role": transition.next_role,
                "action": transition.action,
            }
        )
        transition_doc = failure_transition_document(transition, round_index, evidence)
        path = write_run_document(run_dir, f"03-r{round_index:02d}-failure-transition.md", transition_doc)
        written.append(path)
        documents.append((f"Failure transition round {round_index}", transition_doc))
        return transition

    def record_acceptance_gate(label: str, filename: str, command_docs: list[tuple[str, str]] | None = None) -> bool:
        if not acceptance_criteria:
            return True
        matrix = build_acceptance_matrix(acceptance_criteria, evidence_records)
        blockers = acceptance_blockers(matrix)
        stdout = json.dumps(
            {
                "ok": not blockers,
                "acceptance_matrix": matrix,
                "blockers": blockers,
            },
            ensure_ascii=False,
            indent=2,
        )
        stderr = ""
        if blockers:
            stderr = "\n".join(
                f"{item.get('id')}: {item.get('status')} - {item.get('text')}"
                for item in blockers
            )
        doc = command_result_document(
            "acceptance-evidence-gate",
            0 if not blockers else 1,
            stdout,
            stderr,
            0.0,
        )
        path = write_run_document(run_dir, filename, doc)
        written.append(path)
        documents.append((f"Acceptance evidence gate {label}", doc))
        if command_docs is not None:
            command_docs.append((f"Acceptance evidence gate {label}", doc))
        evidence = evidence_from_command_document(
            "acceptance_gate",
            f"Acceptance evidence gate {label}",
            not blockers,
            path,
            original_project,
            doc,
        )
        evidence["id"] = f"E{len(evidence_records) + 1:02d}"
        if blockers:
            evidence["failure_type"] = "acceptance_unverified"
        evidence_records.append(evidence)
        return not blockers

    def record_python_probe_evidence(
        command_docs: list[tuple[str, str]],
        *,
        filename_prefix: str,
        initial: bool = False,
        round_index: int | None = None,
    ) -> None:
        for item in run_python_probe_evidence(project, command_docs, args.command_timeout):
            slug = str(item.observations.get("slug") or item.command.rsplit(" ", 1)[-1])
            path = write_run_document(run_dir, f"{filename_prefix}-mechanical-probe-{slug}.md", item.document)
            written.append(path)
            if initial:
                title = "Initial " + item.name[:1].lower() + item.name[1:]
            elif round_index is not None:
                title = f"{item.name} round {round_index}"
            else:
                title = item.name
            documents.append((title, item.document))
            command_docs.append((title, item.document))
            evidence = {
                "id": f"E{len(evidence_records) + 1:02d}",
                "kind": item.kind,
                "name": title,
                "status": item.status,
                "command": item.command,
                "exit_code": item.exit_code,
                "duration_seconds": item.duration_seconds,
                "failure_type": item.failure_type,
                "document": display_path(path, original_project),
            }
            if item.covers:
                evidence["covers"] = list(item.covers)
            if item.observations:
                evidence["observations"] = dict(item.observations)
            evidence_records.append(evidence)

    def remember_repair_advice(
        advice: RepairAdvice,
        command_docs: Sequence[tuple[str, str]] = (),
    ) -> None:
        latest_repair_advice.clear()
        latest_repair_advice.update(repair_advice_to_manifest(advice, command_docs))

    def make_stream_callback(
        label: str,
        partial_path: Path,
        current_round: int | None = None,
    ) -> Callable[[LLMStreamStats], None]:
        def update_stream_status(stats: LLMStreamStats) -> None:
            observe_progress(
                run_dir,
                {
                    "round": current_round if current_round is not None else completed_rounds,
                    "current_function": label,
                    "stream_chunks": stats.chunks_received,
                    "stream_bytes": stats.bytes_received,
                    "reasoning_chunks": stats.reasoning_chunks,
                },
                source="llm_stream",
                control_dirs=progress_control_dirs,
            )
            latest_stream_status.clear()
            latest_stream_status.update(
                {
                    "label": label,
                    "status": "streaming",
                    "partial_output": display_path(partial_path, original_project),
                    "chunks_received": stats.chunks_received,
                    "content_chunks": stats.content_chunks,
                    "reasoning_chunks": stats.reasoning_chunks,
                    "bytes_received": stats.bytes_received,
                    "first_chunk_at": stats.first_chunk_at,
                    "last_chunk_at": stats.last_chunk_at,
                    "duration_seconds": round(stats.duration_seconds, 3),
                }
            )
            extra: dict[str, object] = {}
            if current_round is not None:
                extra["current_round"] = current_round
            write_partial_manifest("streaming", extra)

        return update_stream_status

    def failure_analysis_record(
        analysis_doc: str,
        round_index: int,
        failure_type: str,
        failure_signature: str | None,
        analysis_path: Path,
    ) -> dict[str, object]:
        raw = strip_markdown_fence(analysis_doc.strip())
        parsed: dict[str, object]
        try:
            payload = json.loads(raw)
            parsed = payload if isinstance(payload, dict) else {"raw": analysis_doc}
        except json.JSONDecodeError:
            parsed = {"raw": analysis_doc}
        parsed.setdefault("round", round_index)
        parsed.setdefault("failure_type", failure_type)
        if failure_signature:
            parsed.setdefault("failure_signature", failure_signature)
        parsed["document"] = display_path(analysis_path, original_project)
        parsed["call_function"] = "failure_analysis"
        return parsed

    def is_reasoning_only_error(exc: RunnerError) -> bool:
        return "reasoning-only output with empty content" in str(exc)

    def run_failure_analysis(
        round_index: int,
        failure_type: str,
        failure_signature: str | None,
        command_docs: Sequence[tuple[str, str]],
    ) -> str:
        nonlocal api_calls
        evidence_text = "\n\n".join(document for _name, document in command_docs)
        analysis_instruction = failure_analysis_instruction(
            args.brief,
            round_index,
            failure_type,
            failure_signature,
            repeated_same_failure_count,
            state_transitions,
            failure_analyses,
            evidence_text,
        )
        analysis_partial_path = run_dir / f"05-r{round_index:02d}-failure-analysis.partial.json"
        if args.stream:
            written.append(analysis_partial_path)
        update_analysis_stream_status = make_stream_callback(
            f"failure analysis round {round_index}",
            analysis_partial_path,
            current_round=round_index,
        )
        try:
            guard_action("failure_analysis_api_call", action_type="api_call", risk_class="read_only")
            analysis_doc = run_skill_call(
                client=client,
                skill=judge_skill,
                spec=spec,
                instruction=analysis_instruction,
                agent_level="judge",
                project_manifest_text=manifest_text,
                file_context=file_context,
                documents=documents[-args.document_window :],
                output_contract=FAILURE_ANALYSIS_OUTPUT_CONTRACT,
                stream_output_path=analysis_partial_path if args.stream else None,
                stream_callback=update_analysis_stream_status if args.stream else None,
                stream_guard=root_cause_stream_guard if args.stream else None,
                call_function="failure_analysis",
            )
            if latest_stream_status:
                latest_stream_status["status"] = "completed"
            api_calls += 1
            analysis_path = write_run_document(run_dir, f"05-r{round_index:02d}-failure-analysis.json", analysis_doc)
            written.append(analysis_path)
            documents.append((f"Failure analysis round {round_index}", analysis_doc))
            failure_analyses.append(
                failure_analysis_record(
                    analysis_doc,
                    round_index,
                    failure_type,
                    failure_signature,
                    analysis_path,
                )
            )
            write_partial_manifest("failure_analysis_written", {"current_round": round_index})
            return analysis_doc
        except LLMStreamAbortError as exc:
            api_calls += 1
            if latest_stream_status:
                latest_stream_status["status"] = "aborted"
                latest_stream_status["abort_reason"] = exc.reason
                latest_stream_status["abort_code"] = exc.code
            abort_doc = textwrap.dedent(
                f"""
                ## Failure Analysis Stream Abort

                - status: FAIL
                - failure_type: {exc.code}
                - code: {exc.code}
                - reason: {exc.reason}
                - score: {exc.score}
                - threshold: {exc.threshold}
                - partial_output: {display_path(analysis_partial_path, original_project)}
                - chunks_received: {exc.stats.chunks_received}
                - content_chunks: {exc.stats.content_chunks}
                - bytes_received: {exc.stats.bytes_received}

                Failure analysis is advisory. Continue the repair loop, but
                keep this abort as evidence that the analysis role exceeded its
                bounded structured-output contract.
                """
            ).strip()
            abort_path = write_run_document(run_dir, f"05-r{round_index:02d}-failure-analysis-abort.md", abort_doc)
            written.append(abort_path)
            documents.append((f"Failure analysis abort round {round_index}", abort_doc))
            failure_analyses.append(
                {
                    "round": round_index,
                    "failure_type": failure_type,
                    "failure_signature": failure_signature,
                    "analysis_status": "aborted",
                    "abort_code": exc.code,
                    "document": display_path(abort_path, original_project),
                    "call_function": "failure_analysis",
                }
            )
            write_partial_manifest("failure_analysis_aborted", {"current_round": round_index})
            return abort_doc
        except RunnerError as exc:
            if not is_reasoning_only_error(exc):
                raise
            api_calls += 1
            if latest_stream_status:
                latest_stream_status["status"] = "aborted"
                latest_stream_status["abort_reason"] = str(exc)
                latest_stream_status["abort_code"] = "reasoning_only_output"
            abort_doc = textwrap.dedent(
                f"""
                ## Failure Analysis Reasoning-Only Abort

                - status: FAIL
                - failure_type: reasoning_only_output
                - reason: {exc}
                - partial_output: {display_path(analysis_partial_path, original_project)}

                Failure analysis is advisory. Continue the repair loop with
                deterministic repair advice because this model emitted hidden
                reasoning chunks but no machine-readable JSON content.
                """
            ).strip()
            abort_path = write_run_document(run_dir, f"05-r{round_index:02d}-failure-analysis-abort.md", abort_doc)
            written.append(abort_path)
            documents.append((f"Failure analysis abort round {round_index}", abort_doc))
            failure_analyses.append(
                {
                    "round": round_index,
                    "failure_type": failure_type,
                    "failure_signature": failure_signature,
                    "analysis_status": "aborted",
                    "abort_code": "reasoning_only_output",
                    "document": display_path(abort_path, original_project),
                    "call_function": "failure_analysis",
                }
            )
            write_partial_manifest("failure_analysis_aborted", {"current_round": round_index})
            return abort_doc

    def run_patch_planner(
        round_index: int,
        role_label: str,
        analysis_doc: str,
    ) -> str:
        """Ask a judge-level planner for one minimal patch proposition."""
        nonlocal api_calls
        planner_instruction = patch_planner_instruction(args.brief, role_label, analysis_doc)
        planner_partial_path = run_dir / f"02-r{round_index:02d}-patch-plan.partial.md"
        if args.stream:
            written.append(planner_partial_path)
        update_planner_stream_status = make_stream_callback(
            f"patch planner round {round_index}",
            planner_partial_path,
            current_round=round_index,
        )
        try:
            guard_action("patch_planner_api_call", action_type="api_call", risk_class="read_only")
            planner_doc = run_skill_call(
                client=client,
                skill=judge_skill,
                spec=spec,
                instruction=planner_instruction,
                agent_level="judge",
                project_manifest_text=manifest_text,
                file_context=file_context,
                documents=documents[-args.document_window :],
                output_contract=PATCH_PLANNER_OUTPUT_CONTRACT,
                stream_output_path=planner_partial_path if args.stream else None,
                stream_callback=update_planner_stream_status if args.stream else None,
                stream_guard=root_cause_stream_guard if args.stream else None,
                call_function="patch_planner",
            )
            api_calls += 1
            if latest_stream_status:
                latest_stream_status["status"] = "completed"
        except LLMStreamAbortError as exc:
            api_calls += 1
            if latest_stream_status:
                latest_stream_status["status"] = "aborted"
                latest_stream_status["abort_reason"] = exc.reason
                latest_stream_status["abort_code"] = exc.code
            planner_doc = textwrap.dedent(
                f"""
                PATCH_PLAN
                - proposition: If the planner stream violates the bounded schema, request missing context instead of guessing
                - required_path: (none)
                - readonly_paths: (none)
                - forbidden_paths: (none)
                - patch_type: missing_context
                - minimal_patch_goal: stop artifact generation after patch-planner stream abort
                - stop_rule: patch planner stream aborted with {exc.code}: {exc.reason}
                """
            ).strip()
        except RunnerError as exc:
            if not is_reasoning_only_error(exc):
                raise
            api_calls += 1
            if latest_stream_status:
                latest_stream_status["status"] = "aborted"
                latest_stream_status["abort_reason"] = str(exc)
                latest_stream_status["abort_code"] = "reasoning_only_output"
            planner_doc = textwrap.dedent(
                f"""
                PATCH_PLAN
                - proposition: If patch planning produced hidden reasoning without content, request missing context instead of guessing
                - required_path: (none)
                - readonly_paths: (none)
                - forbidden_paths: (none)
                - patch_type: missing_context
                - minimal_patch_goal: stop artifact generation after reasoning-only patch-planner output
                - stop_rule: patch planner returned reasoning-only output: {exc}
                """
            ).strip()
        planner_path = write_run_document(run_dir, f"02-r{round_index:02d}-patch-plan.md", planner_doc)
        written.append(planner_path)
        documents.append((f"Patch plan round {round_index}", planner_doc))
        write_partial_manifest("patch_plan_written", {"current_round": round_index})
        return planner_doc

    def project_policy_triage_enabled(trigger: str) -> bool:
        mode = str(getattr(args, "project_policy_triage", "auto") or "auto")
        return triage_is_enabled(mode, trigger)

    def project_policy_triage_record(
        triage_doc: str,
        round_index: int,
        trigger: str,
        triage_path: Path,
    ) -> dict[str, object]:
        raw = strip_markdown_fence(triage_doc.strip())
        try:
            payload = json.loads(raw)
            parsed = payload if isinstance(payload, dict) else {"raw": triage_doc}
        except json.JSONDecodeError:
            parsed = {"raw": triage_doc}
        parsed.setdefault("round", round_index)
        parsed.setdefault("trigger", trigger)
        parsed["document"] = display_path(triage_path, original_project)
        parsed["call_function"] = "project_policy_triage"
        return parsed

    def run_project_policy_triage(
        round_index: int,
        trigger: str,
        evidence_doc: str,
        candidate_action: str,
    ) -> dict[str, object] | None:
        nonlocal api_calls
        if not project_policy_triage_enabled(trigger):
            return None
        analysis_instruction = project_policy_triage_instruction(
            args.brief,
            trigger,
            candidate_action,
            state_transitions,
            project_policy_triages,
            evidence_doc,
        )
        deterministic_triage = deterministic_project_policy_triage_from_evidence(
            trigger,
            evidence_doc,
            project,
            stage_generated_test_paths,
        )
        if deterministic_triage:
            triage_doc = json.dumps(deterministic_triage, ensure_ascii=False, indent=2)
            triage_path = write_run_document(run_dir, f"05-r{round_index:02d}-project-policy-triage.json", triage_doc)
            written.append(triage_path)
            documents.append((f"Project policy triage round {round_index}", triage_doc))
            record = project_policy_triage_record(triage_doc, round_index, trigger, triage_path)
            record["call_function"] = "project_policy_triage.deterministic"
            project_policy_triages.append(record)
            write_partial_manifest("project_policy_triage_written", {"current_round": round_index})
            return record
        try:
            guard_action("project_policy_triage_api_call", action_type="api_call", risk_class="read_only")
            triage_doc = run_skill_call(
                client=client,
                skill=judge_skill,
                spec=spec,
                instruction=analysis_instruction,
                agent_level="judge",
                project_manifest_text=manifest_text,
                file_context=file_context,
                documents=documents[-args.document_window :],
                output_contract=PROJECT_POLICY_TRIAGE_OUTPUT_CONTRACT,
                call_function="project_policy_triage",
            )
        except LLMStreamAbortError:
            return None
        except RunnerError as exc:
            if not is_reasoning_only_error(exc):
                raise
            return None
        api_calls += 1
        triage_path = write_run_document(run_dir, f"05-r{round_index:02d}-project-policy-triage.json", triage_doc)
        written.append(triage_path)
        documents.append((f"Project policy triage round {round_index}", triage_doc))
        record = project_policy_triage_record(triage_doc, round_index, trigger, triage_path)
        project_policy_triages.append(record)
        write_partial_manifest("project_policy_triage_written", {"current_round": round_index})
        return record

    def authorized_test_edit_paths_from_triages() -> list[str]:
        return authorized_test_edit_paths(project_policy_triages)

    def apply_project_policy_triage_to_advice(
        advice: RepairAdvice,
        triage: dict[str, object] | None,
    ) -> RepairAdvice:
        return apply_triage_to_advice(
            advice,
            triage,
            existing_project_paths,
            TEST_HARNESS_WRITE_STRATEGIES,
        )

    write_partial_manifest("initialized")

    if resume_documents:
        documents.extend(resume_documents)

    if recovery_plan:
        recovery_control_doc = json.dumps(
            {
                "plan_id": recovery_plan.get("plan_id"),
                "strategy": recovery_strategy,
                "rationale": recovery_plan.get("rationale"),
                "failure_family": recovery_plan.get("failure_family"),
                "failure_family_count": recovery_plan.get("failure_family_count"),
                "failure_family_threshold": recovery_plan.get("failure_family_threshold"),
                "resumed_from": recovery_plan.get("source_run_dir"),
            },
            ensure_ascii=False,
            indent=2,
        )
        path = write_run_document(run_dir, "00-recovery-control.json", recovery_control_doc)
        written.append(path)
        documents.append(("Supervisor recovery control", recovery_control_doc))

    recovery_failure_analysis_pending = recovery_strategy == "failure_analysis"
    recovery_failure_analysis_evidence = [
        (name, document)
        for name, document in resume_documents
        if parse_command_result_document(document).get("status") == "FAIL"
    ]
    if recovery_failure_analysis_pending and not recovery_failure_analysis_evidence:
        recovery_failure_analysis_evidence = list(
            resume_documents[-max(1, args.document_window) :]
        )

    if resume_manifest and read_text_if_exists(run_dir / "01-pm-control.md"):
        pm_doc = read_text_if_exists(run_dir / "01-pm-control.md")
    elif args.skip_pm or resume_manifest:
        pm_doc = deterministic_pm_control(
            args.brief,
            [*args.include, *new_files],
            not args.no_extra_files,
            list(
                dict.fromkeys(
                    [
                        *context_files,
                        *[
                            path
                            for path in slice_context_files
                            if path not in args.include and path not in new_files
                        ],
                    ]
                )
            ),
        )
    else:
        pm_instruction = pm_control_instruction(args.brief)
        pm_partial_path = run_dir / "01-pm-control.partial.md"
        if args.stream:
            written.append(pm_partial_path)
            latest_stream_status.clear()
            latest_stream_status.update(
                {
                    "label": "pm control",
                    "status": "starting",
                    "partial_output": display_path(pm_partial_path, original_project),
                }
            )
            write_partial_manifest("pm_streaming")
        guard_action("pm_api_call", action_type="api_call", risk_class="read_only")
        pm_doc = run_skill_call(
            client=client,
            skill=pm_skill,
            spec=spec,
            instruction=pm_instruction,
            agent_level="pm",
            project_manifest_text=manifest_text,
            output_contract="Return a compact Markdown PM control document.",
            stream_output_path=pm_partial_path if args.stream else None,
            stream_callback=make_stream_callback("pm control", pm_partial_path) if args.stream else None,
            call_function="plan_work",
        )
        if args.stream and latest_stream_status:
            latest_stream_status["status"] = "completed"
            write_partial_manifest("pm_stream_completed")
        api_calls += 1
    path = write_run_document(run_dir, "01-pm-control.md", pm_doc)
    written.append(path)
    documents.append(("PM control document", pm_doc))
    write_partial_manifest("pm_ready")

    if domain_modeling_state.get("run"):
        route = recommended_sdlc_phases(args.brief, spec)
        domain_skill = required_skill(skills, str(domain_modeling_state["skill"]))
        guard_action("domain_modeling_api_call", action_type="api_call", risk_class="read_only")
        domain_doc = run_skill_call(
            client=client,
            skill=domain_skill,
            spec=spec,
            instruction=phase_instruction("ddd", args.brief, route),
            agent_level="pm",
            project_manifest_text=manifest_text,
            documents=documents[-args.document_window :],
            output_contract=(
                "Return the DDD domain contract document only. "
                "Do not write implementation code."
            ),
            call_function="plan_work",
        )
        api_calls += 1
        path = write_run_document(run_dir, "01-domain-contract.md", domain_doc)
        written.append(path)
        documents.append(("Domain contract document", domain_doc))
        domain_modeling_state = {**domain_modeling_state, "ran": True}
        write_partial_manifest("domain_contract_ready")
    else:
        domain_modeling_state = {**domain_modeling_state, "ran": False}

    test_commands = list(args.test_command or [])
    html_targets = list(dict.fromkeys([*args.include, *new_files]))
    all_context_targets = list(dict.fromkeys([*args.include, *context_files, *slice_context_files, *new_files]))
    allowed_artifact_paths = list(dict.fromkeys([*args.include, *new_files]))
    readonly_artifact_paths = list(
        dict.fromkeys(
            [
                *context_files,
                *[
                    path
                    for path in slice_context_files
                    if path not in allowed_artifact_paths
                ],
            ]
        )
    )
    artifact_policy = ArtifactPathPolicy(
        allowed_paths=tuple(allowed_artifact_paths),
        readonly_paths=tuple(readonly_artifact_paths),
        existing_paths=tuple(existing_project_paths),
        allow_extra_new_files=not bool(args.no_extra_files),
    )
    tetris_checks = is_tetris_request(args.brief, html_targets)
    redis_checks = should_run_redis_smoke(args.redis_smoke, args.brief, all_context_targets)

    initial_checks: list[tuple[str, str, bool]] = []
    initial_command_docs: list[tuple[str, str]] = []
    if args.apply and html_targets:
        guard_action("initial_html_smoke", action_type="harness", risk_class="generated_code_execution")
        for index, (doc, ok) in enumerate(
            run_html_smoke_checks(project, html_targets, run_dir, args.command_timeout, tetris_checks=tetris_checks),
            start=1,
        ):
            path = write_run_document(run_dir, f"00-initial-html-smoke-{index:02d}.md", doc)
            written.append(path)
            documents.append((f"Initial HTML smoke {index}", doc))
            initial_checks.append((f"Initial HTML smoke {index}", doc, ok))
            evidence = evidence_from_command_document("html_smoke", f"Initial HTML smoke {index}", ok, path, original_project, doc)
            evidence["id"] = f"E{len(evidence_records) + 1:02d}"
            evidence_records.append(evidence)

    if args.apply and required_paths:
        guard_action("initial_required_path_check")
        for index, (doc, ok) in enumerate(run_required_path_checks(project, required_paths), start=1):
            path = write_run_document(run_dir, f"00-initial-required-path-{index:02d}.md", doc)
            written.append(path)
            documents.append((f"Initial required path {index}", doc))
            initial_checks.append((f"Initial required path {index}", doc, ok))
            evidence = evidence_from_command_document("required_path", f"Initial required path {index}", ok, path, original_project, doc)
            evidence["id"] = f"E{len(evidence_records) + 1:02d}"
            evidence_records.append(evidence)

    stage_scope_preflight_findings: list[ArtifactLintFinding] = []
    for test_path in stage_scope_test_paths:
        path = resolve_project_path(project, test_path)
        if not path.is_file():
            continue
        content = read_text_if_exists(path)
        if not content:
            continue
        synthetic_artifact = f"BEGIN_FILE: {test_path}\n{content}\nEND_FILE"
        stage_scope_preflight_findings.extend(
            lint_stage_scope_output(
                synthetic_artifact,
                args.brief,
                [test_path],
            )
        )
    if stage_scope_preflight_findings:
        preflight_doc = textwrap.dedent(
            f"""
            ## Stage Scope Preflight

            Existing current-stage test artifacts contain propositions outside
            the current stage scope. Treat these tests as generated test
            harness material that must be rewritten before product-code repair.

            {artifact_lint_document(stage_scope_preflight_findings)}

            Required runner action for the next coder round:
            - First rewrite the affected current-stage test path(s) so every
              assertion belongs to the Current Stage goal only.
            - Do not broaden product code to satisfy out-of-scope test
              propositions.
            - Keep completed earlier-stage tests meaningful.
            """
        ).strip()
        path = write_run_document(run_dir, "00-stage-scope-preflight.md", preflight_doc)
        written.append(path)
        documents.append(("Stage scope preflight", preflight_doc))
        remember_repair_advice(
            RepairAdvice(
                strategy="rewrite_current_stage_tests_to_scope",
                focus_files=tuple(
                    unique_ordered(finding.path for finding in stage_scope_preflight_findings if finding.path)
                ),
                instructions=(
                    "Existing generated/current-stage tests assert future-stage behavior.",
                    "Rewrite current-stage test artifacts to match only the current stage goal before product-code repair.",
                    "Use one BEGIN_FILE/END_FILE full replacement for each affected test file; do not use search_replace for polluted whole-file test rewrites.",
                    "Remove future-stage tests instead of implementing future-stage product behavior.",
                    "Do not implement future-stage predicates just to satisfy polluted generated tests.",
                ),
                evidence=("stage_scope_preflight",),
            )
        )
        write_partial_manifest("stage_scope_preflight")

    if args.apply and args.precheck:
        if redis_checks:
            guard_action("initial_redis_smoke", action_type="harness", risk_class="generated_code_execution")
            doc, ok = run_redis_smoke_check(project, run_dir, args.command_timeout)
            path = write_run_document(run_dir, "00-initial-redis-smoke.md", doc)
            written.append(path)
            documents.append(("Initial Redis smoke", doc))
            initial_checks.append(("Initial Redis smoke", doc, ok))
            initial_command_docs.append(("Initial Redis smoke", doc))
            evidence = evidence_from_command_document("redis_smoke", "Initial Redis smoke", ok, path, original_project, doc)
            evidence["id"] = f"E{len(evidence_records) + 1:02d}"
            evidence_records.append(evidence)

        for index, command in enumerate(test_commands, start=1):
            doc, ok = run_agent_checked_command(
                command,
                action=f"initial_test_command_{index}",
            )
            path = write_run_document(run_dir, f"00-initial-command-{index:02d}.md", doc)
            written.append(path)
            documents.append((f"Initial command {index}", doc))
            initial_checks.append((f"Initial command {index}", doc, ok))
            initial_command_docs.append((f"Initial command {index}", doc))
            evidence = evidence_from_command_document("command", f"Initial command {index}", ok, path, original_project, doc)
            evidence["id"] = f"E{len(evidence_records) + 1:02d}"
            evidence_records.append(evidence)
            stop_for_command_safety_decision()

        if initial_command_docs and not all(ok for _name, _doc, ok in initial_checks):
            summary_doc = observation_summary_document(0, initial_command_docs)
            path = write_run_document(run_dir, "00-initial-observation-summary.md", summary_doc)
            written.append(path)
            documents.append(("Initial observation summary", summary_doc))
            initial_command_docs.append(("Initial observation summary", summary_doc))
            record_python_probe_evidence(initial_command_docs, filename_prefix="00-initial", initial=True)
            initial_advice = repair_advice_from_command_docs(
                initial_command_docs,
                test_commands,
                project,
                stage_generated_test_paths,
            )
            if initial_advice:
                remember_repair_advice(initial_advice, initial_command_docs)
                advice_doc = repair_advice_document(initial_advice)
                path = write_run_document(run_dir, "00-initial-repair-advice.md", advice_doc)
                written.append(path)
                documents.append(("Initial repair advice", advice_doc))
            last_functional_failure_signature = command_failure_signature(initial_command_docs)
            last_functional_failure_family_signature = command_failure_family_signature(initial_command_docs)

    initial_acceptance_ok = True
    if initial_checks:
        initial_acceptance_ok = record_acceptance_gate(
            "initial",
            "00-initial-acceptance-gate.md",
            initial_command_docs,
        )
    initial_commands_passed = bool(args.precheck and initial_command_docs)
    html_smoke_only_passed = bool(html_targets and not test_commands and not redis_checks)
    if (
        initial_checks
        and all(ok for _name, _doc, ok in initial_checks)
        and initial_acceptance_ok
        and (initial_commands_passed or html_smoke_only_passed)
    ):
        final_verdict = "approved"
        completed_rounds = 0
        verification_doc = textwrap.dedent(
            """
            ## Initial Verification Result

            PASS: initial required-path, smoke, and/or precheck command evidence
            already satisfies the requested verification gate.

            No coder round was run because the requested artifact already met
            the executable acceptance evidence. This prevents a working file
            from being changed again without a failing observation.
            """
        ).strip()
        path = write_run_document(run_dir, "01-initial-verification.md", verification_doc)
        written.append(path)
        documents.append(("Initial verification result", verification_doc))

        manifest_doc = {
            "brief": args.brief,
            "command": "agent",
            "apply": bool(args.apply),
            "requested_max_rounds": args.max_rounds,
            "max_rounds": args.max_rounds + protocol_repair_rounds,
            "functional_round_budget": args.max_rounds,
            "protocol_repair_round_budget": protocol_repair_rounds,
            "adaptive_round_budget": adaptive_round_budget,
            "root_cause_patch_round_budget": root_cause_patch_round_budget,
            "functional_rounds_used": functional_rounds_used,
            "protocol_rounds_used": protocol_rounds_used,
            "adaptive_rounds_used": adaptive_rounds_used,
            "root_cause_patch_rounds_used": root_cause_patch_rounds_used,
            "last_functional_failure_score": last_functional_failure_score,
            "last_functional_failure_signature": last_functional_failure_signature,
            "last_functional_failure_family_signature": last_functional_failure_family_signature,
            "repeated_same_failure_count": repeated_same_failure_count,
            "resumed_from": str(args.resume.resolve()) if args.resume else None,
            "artifact_format": args.artifact_format,
            "small_patch": bool(args.small_patch),
            "no_replace_file": bool(args.no_replace_file),
            "allow_extra_new_files": not bool(args.no_extra_files),
            "precheck": bool(args.precheck),
            "worktree_mode": args.worktree_mode,
            "worktree_path": str(worktree_path) if worktree_path else None,
            "resumed_worktree_from": str(resume_worktree_source) if resume_worktree_source else None,
            "copied_back": copied_back,
            "changed_paths": unique_ordered(changed_paths),
            "completed_rounds": completed_rounds,
            "final_verdict": final_verdict,
            "final_failure_type": final_failure_type,
            "api_calls": api_calls,
            "llm_settings": llm_settings_manifest(client),
            "reasoning_records": llm_reasoning_manifest(client),
            "streaming": dict(latest_stream_status) if latest_stream_status else None,
            "repair_advice": dict(latest_repair_advice) if latest_repair_advice else None,
            "test_commands": test_commands,
            "context_paths": context_files,
            "context_slices": {path: ranges for path, ranges in context_slices.items()},
            "required_paths": required_paths,
            "explicit_required_paths": explicit_required_paths,
            "auto_required_paths": auto_required_paths,
            "stage_generated_test_paths": stage_generated_test_paths,
            "acceptance_criteria": acceptance_criteria,
            "requirements": [item.to_manifest() for item in requirement_records],
            "observables": [item.to_manifest() for item in observable_records],
            "propositions": proposition_manifest_from_documents(documents),
            "semantic_contracts": [semantic_contract_to_dict(contract) for contract in semantic_contracts],
            "evidence": evidence_records,
            "acceptance_matrix": build_acceptance_matrix(acceptance_criteria, evidence_records),
            "verdicts": [
                verdict_to_manifest(item)
                for item in verdicts_from_acceptance_matrix(
                    build_acceptance_matrix(acceptance_criteria, evidence_records)
                )
            ],
            "failure_summary": failure_summary(final_verdict, evidence_records, final_failure_type),
            "project_policy_triages": project_policy_triages,
            "state_transitions": state_transitions,
            "cancel_requested": cancel_requested(run_dir),
            "cancel_state": load_cancel_state(run_dir) or None,
            "progress_log": display_path(progress_file_path(run_dir), original_project),
            "progress_event_count": len(read_progress_events(run_dir)),
            "safety_decisions_log": display_path(safety_decisions_file_path(run_dir), original_project),
            "safety_decision_count": len(read_safety_decisions(run_dir)),
            "safety_approvals_log": display_path(safety_approvals_file_path(run_dir), original_project),
            "safety_approval_event_count": len(read_safety_approvals(run_dir)),
            "pending_safety_decisions": pending_safety_decisions(run_dir),
            "blocked_safety_decisions": blocked_safety_decisions(run_dir),
            "action_gate_audit": action_gate_audit(run_dir),
            "budget": budget_status(run_dir),
            "progress": progress_status(run_dir, evaluate=False),
            "documents": [display_path(path, original_project) for path in written],
        }
        attach_regression_memory(manifest_doc, run_dir, original_project, written)
        manifest_path = write_run_document(run_dir, "run.json", json.dumps(manifest_doc, ensure_ascii=False, indent=2))
        written.append(manifest_path)
        if recovery_source is not None:
            complete_stalled_recovery(
                recovery_source,
                run_dir,
                outcome="completed",
            )

        print(f"run_dir: {run_dir}")
        print(f"api_calls: {api_calls}")
        print(f"final_verdict: {final_verdict}")
        if worktree_path:
            print(f"worktree_path: {worktree_path}")
        for path in written:
            print(f"wrote: {path}")
        return 0

    start_round = previous_completed_rounds + 1
    total_round_budget = args.max_rounds + protocol_repair_rounds + adaptive_round_budget + root_cause_patch_round_budget
    final_round = previous_completed_rounds + total_round_budget if resume_manifest else total_round_budget

    def consume_failure_budget(
        failure_type: str | None,
        round_index: int,
        failure_score: int | None = None,
    ) -> bool:
        nonlocal protocol_rounds_used, functional_rounds_used, adaptive_rounds_used
        nonlocal root_cause_patch_rounds_used, root_cause_patch_pending, last_functional_failure_score
        if is_protocol_failure_type(failure_type):
            failure_key = failure_type or "unknown_protocol_failure"
            is_new_protocol_failure = failure_key not in protocol_failure_types_seen
            protocol_failure_types_seen.add(failure_key)
            protocol_rounds_used += 1
            if protocol_rounds_used <= protocol_repair_rounds and round_index < final_round:
                return True
            if (
                protocol_repair_rounds > 0
                and is_new_protocol_failure
                and adaptive_rounds_used < adaptive_round_budget
                and round_index < final_round
            ):
                adaptive_rounds_used += 1
                return True
            return False
        functional_rounds_used += 1
        if functional_rounds_used < args.max_rounds and round_index < final_round:
            root_cause_patch_pending = False
            if failure_score is not None:
                last_functional_failure_score = failure_score
            return True
        improved = (
            failure_score is not None
            and last_functional_failure_score is not None
            and failure_score < last_functional_failure_score
        )
        if improved and adaptive_rounds_used < adaptive_round_budget and round_index < final_round:
            adaptive_rounds_used += 1
            root_cause_patch_pending = False
            last_functional_failure_score = failure_score
            return True
        if (
            root_cause_patch_pending
            and root_cause_patch_rounds_used < root_cause_patch_round_budget
            and round_index < final_round
        ):
            root_cause_patch_rounds_used += 1
            root_cause_patch_pending = False
            if failure_score is not None:
                last_functional_failure_score = failure_score
            return True
        if failure_score is not None:
            last_functional_failure_score = failure_score
        return False

    def current_context_paths() -> list[str]:
        existing_writable_targets = [
            path
            for path in allowed_artifact_paths
            if (project / path).is_file()
        ]
        return list(
            dict.fromkeys(
                [
                    *args.include,
                    *existing_writable_targets,
                    *context_files,
                    *slice_context_files,
                ]
            )
        )

    for round_index in range(start_round, final_round + 1):
        guard_action(
            f"round_{round_index}_start",
            action_type="recovery" if round_index > start_round else "round_start",
        )
        completed_rounds = round_index
        write_partial_manifest("round_started", {"current_round": round_index})
        active_repair_strategy = str(latest_repair_advice.get("strategy", "")) if latest_repair_advice else ""
        if latest_repair_advice and active_repair_strategy not in TEST_HARNESS_WRITE_STRATEGIES:
            before_policy = (tuple(allowed_artifact_paths), tuple(readonly_artifact_paths))
            allowed_artifact_paths, readonly_artifact_paths = freeze_test_paths_as_readonly(
                allowed_artifact_paths,
                readonly_artifact_paths,
            )
            if before_policy != (tuple(allowed_artifact_paths), tuple(readonly_artifact_paths)):
                artifact_policy = ArtifactPathPolicy(
                    allowed_paths=tuple(allowed_artifact_paths),
                    readonly_paths=tuple(readonly_artifact_paths),
                    existing_paths=tuple(existing_project_paths),
                    allow_extra_new_files=not bool(args.no_extra_files),
                )
        semantic_writable_focus, semantic_readonly_focus = semantic_contract_focus_paths(
            semantic_contracts,
            existing_project_paths,
        )
        advice_writable_focus, advice_readonly_focus = repair_advice_policy_paths(
            RepairAdvice(
                strategy=str(latest_repair_advice.get("strategy", "")),
                focus_files=tuple(str(path) for path in latest_repair_advice.get("focus_files", []) if isinstance(path, str)),
                instructions=tuple(str(item) for item in latest_repair_advice.get("instructions", []) if isinstance(item, str)),
                evidence=tuple(str(item) for item in latest_repair_advice.get("evidence", []) if isinstance(item, str)),
            )
            if latest_repair_advice
            else None,
            existing_project_paths,
        )
        added_writable_focus = [
            path
            for path in [*semantic_writable_focus, *advice_writable_focus]
            if path not in allowed_artifact_paths
        ]
        added_readonly_focus = [
            path
            for path in [*semantic_readonly_focus, *advice_readonly_focus]
            if path not in context_files and path not in allowed_artifact_paths
        ]
        if added_writable_focus or added_readonly_focus:
            allowed_artifact_paths, readonly_artifact_paths = merge_artifact_policy_paths(
                allowed_artifact_paths,
                readonly_artifact_paths,
                added_writable_focus,
                added_readonly_focus,
            )
            context_files = unique_ordered([*context_files, *added_writable_focus, *added_readonly_focus])
            artifact_policy = ArtifactPathPolicy(
                allowed_paths=tuple(allowed_artifact_paths),
                readonly_paths=tuple(readonly_artifact_paths),
                existing_paths=tuple(existing_project_paths),
                allow_extra_new_files=not bool(args.no_extra_files),
            )
        if latest_repair_advice:
            advice_control_text = "\n".join(
                str(item)
                for item in [
                    *latest_repair_advice.get("instructions", []),
                    *latest_repair_advice.get("evidence", []),
                ]
                if isinstance(item, str)
            ).lower()
            if "cli-layer focus demoted" in advice_control_text or "keep cli files as read-only" in advice_control_text:
                cli_paths = ("minisqlite/cli.py", "minisqlite/__main__.py", "minisqlite/__init__.py")
                cli_is_current_stage_target = any(path in [*required_paths, *new_files] for path in cli_paths)
                if not cli_is_current_stage_target:
                    allowed_artifact_paths, readonly_artifact_paths = demote_writable_paths_to_readonly(
                        allowed_artifact_paths,
                        readonly_artifact_paths,
                        cli_paths,
                    )
                    context_files = unique_ordered([*context_files, *readonly_artifact_paths])
                    artifact_policy = ArtifactPathPolicy(
                        allowed_paths=tuple(allowed_artifact_paths),
                        readonly_paths=tuple(readonly_artifact_paths),
                        existing_paths=tuple(existing_project_paths),
                        allow_extra_new_files=not bool(args.no_extra_files),
                    )
        deterministic_repair_candidate: tuple[str, str] | None = None
        if pending_deterministic_repair:
            deterministic_repair_candidate = deterministic_replacement_artifact_from_failure_analysis(
                pending_deterministic_repair,
                project,
                artifact_policy,
                allow_replace_file=not bool(args.no_replace_file),
            )
            pending_deterministic_repair = None
        if not deterministic_repair_candidate and latest_repair_advice:
            deterministic_repair_candidate = deterministic_replacement_artifact_from_repair_advice(
                latest_repair_advice,
                project,
                artifact_policy,
                allow_replace_file=not bool(args.no_replace_file),
            )
        if not deterministic_repair_candidate:
            deterministic_syntax_paths = unique_ordered(
                [
                    *stage_scope_test_paths,
                    *stage_generated_test_paths,
                    *[path for path in allowed_artifact_paths if path.startswith("tests/")],
                ]
            )
            deterministic_repair_candidate = deterministic_python_syntax_repair_artifact(
                project,
                artifact_policy,
                deterministic_syntax_paths,
            )

        current_paths = current_context_paths()
        symbol_ledger = python_public_symbol_ledger(project, current_paths)
        file_context = collect_file_context(project, current_paths, args.max_context_chars, context_slices)
        if symbol_ledger:
            file_context = symbol_ledger + "\n\n" + file_context
        if recovery_failure_analysis_pending:
            run_failure_analysis(
                round_index,
                "recovery_failure_plateau",
                str(recovery_plan.get("failure_family") or ""),
                recovery_failure_analysis_evidence,
            )
            recovery_failure_analysis_pending = False
            root_cause_patch_pending = True
        new_file_instruction = "Create these new project-relative file(s): " + ", ".join(new_files) if new_files else "(none)"
        writable_targets = ", ".join(allowed_artifact_paths) if allowed_artifact_paths else "(none)"
        extra_file_policy = (
            "Additional safe project-relative new files are allowed if they did not exist before this run."
            if artifact_policy.allow_extra_new_files
            else "Additional files are disabled; use only explicit writable targets."
        )
        readonly_context_files = list(dict.fromkeys([*context_files, *[path for path in slice_context_files if path not in allowed_artifact_paths]]))
        readonly_targets = ", ".join(readonly_context_files) if readonly_context_files else "(none)"
        small_patch_instruction = ""
        if args.small_patch:
            small_patch_instruction = textwrap.dedent(
                """
                Small patch mode:
                - Prefer the smallest exact edit. For one-line or very short
                  edits, JSON search_replace is acceptable.
                - For multi-line search/replace blocks longer than about 300
                  characters, use BEGIN_SEARCH_REPLACE or a minimal unified
                  diff instead of JSON; long escaped JSON strings are easy to
                  truncate or corrupt.
                - Search text should be the shortest unique snippet that
                  identifies the edit, not an entire function body.
                - Do not rewrite an entire existing file unless a precise local
                  edit cannot satisfy the observed failure.
                - Change only the smallest number of writable files needed.
                - If the needed edit exceeds the available context, return
                  MISSING_CONTEXT with exact file paths instead of guessing.
                """
            ).strip()
        replace_file_instruction = ""
        if args.no_replace_file:
            replace_file_instruction = textwrap.dedent(
                """
                Replace-file artifacts are disabled for this run.
                Use JSON search_replace artifacts or a minimal unified diff.
                Do not return replace_file, BEGIN_FILE, or whole-file content.
                """
            ).strip()
        repair_instruction = ""
        if round_index > 1:
            repair_instruction = textwrap.dedent(
                """
                This is a repair round. Use the latest patch/test/Judge documents
                as evidence. Produce the smallest patch that addresses the
                observed failure. Do not repeat an unchanged patch.
                Inspect the included current file contents before editing.
                Preserve previously working public APIs, exported symbols,
                required HTML elements, and other already-satisfied fixed
                requirements unless the latest executable evidence proves they
                are wrong.
                If many tests or smoke checks fail with the same exception,
                same error response, or same observed value, fix the shared
                root cause in the implementation first. Do not replace or
                weaken tests when executable smoke evidence shows an
                implementation failure.
                """
            ).strip()
        if args.artifact_format == "legacy":
            artifact_output_instruction = textwrap.dedent(
                """
                Preferred output:
                - Return BEGIN_SEARCH_REPLACE/END_SEARCH_REPLACE for exact
                  local edits:
                  BEGIN_SEARCH_REPLACE: path/to/file
                  <<<<<<< SEARCH
                  exact old text
                  =======
                  exact new text
                  >>>>>>> REPLACE
                  END_SEARCH_REPLACE
                - Search text must be the shortest unique snippet that occurs
                  exactly once in the target file.
                - For a small change, a minimal unified diff that applies with
                  git apply is also acceptable.
                - Do not return JSON artifacts in legacy mode.
                - Do not include prose, test reports, or self-judgement.
                """
            ).strip()
        else:
            artifact_output_instruction = textwrap.dedent(
                f"""
                Preferred output:
                - Preferred structured format:
                  {{"artifacts":[{{"type":"search_replace","path":"path/to/file","search":"exact old text","replace":"exact new text"}}]}}
                  {"- replace_file artifacts are disabled for this run." if args.no_replace_file else 'or {\"artifacts\":[{\"type\":\"replace_file\",\"path\":\"path/to/file\",\"content\":\"complete file content\"}]}'}
                - For a tiny exact edit or any multi-line edit whose JSON escaping
                  would be long or fragile, prefer:
                  BEGIN_SEARCH_REPLACE: path/to/file
                  <<<<<<< SEARCH
                  exact old text
                  =======
                  exact new text
                  >>>>>>> REPLACE
                  END_SEARCH_REPLACE
                - For a small change, return a minimal unified diff that can be
                  applied with git apply.
                - For a large single-file HTML replacement, return exactly:
                  BEGIN_FILE: path/to/file
                  ...complete file content...
                  END_FILE
                - For a multi-file task, return one BEGIN_FILE/END_FILE block per
                  target file. You may return multiple file artifacts in one
                  response. Paths may be explicit writable targets, or additional
                  safe new files when the writable policy allows them.
                  Read-only context files are evidence only and must not be edited.
                  A BEGIN_FILE block with only a path and no
                  content is invalid; include the complete file text.
                - If the current file is truncated or only the tail is missing,
                  prefer an append-only artifact:
                  BEGIN_APPEND_FILE: path/to/file
                  ...only the missing suffix...
                  END_APPEND_FILE

                Do not include prose, test reports, or self-judgement.
                """
            ).strip()
        if args.artifact_format == "legacy":
            output_contract = (
                "Return ONLY a unified diff, BEGIN_SEARCH_REPLACE/END_SEARCH_REPLACE, "
                "one or more BEGIN_FILE/END_FILE full file artifacts, or a "
                "BEGIN_APPEND_FILE/END_APPEND_FILE suffix artifact. "
                "Do not return JSON artifacts. "
                "For BEGIN_FILE, include complete non-empty file content. "
                "For BEGIN_SEARCH_REPLACE, replacement text must differ from search text. "
                "No prose. No verdict."
            )
        else:
            output_contract = (
                "Return ONLY JSON artifacts, a unified diff, BEGIN_SEARCH_REPLACE/END_SEARCH_REPLACE, "
                "one or more BEGIN_FILE/END_FILE full file artifacts, or a "
                "BEGIN_APPEND_FILE/END_APPEND_FILE suffix artifact. "
                "JSON artifacts must be an object with an artifacts array. "
                "For BEGIN_FILE, include complete non-empty file content. "
                "For BEGIN_SEARCH_REPLACE, replacement text must differ from search text. "
                "No prose. No verdict."
            )

        artifact_failure_modes = artifact_failure_modes_from_documents(documents, args.document_window)
        strict_instruction, strict_contract = strict_artifact_output_instruction(artifact_failure_modes)
        single_artifact_stream_mode = bool(
            round_index > 1
            and artifact_failure_modes
            & {
                "artifact_invalid",
                "atomic_search_replace_required",
                "bad_search_replace",
                "corrupt_unified_diff",
                "format_repair_protocol",
                "malformed_search_replace",
                "semantic_repair_format",
                "single_artifact_required",
                "stage_scope_violation",
            }
        )
        if strict_instruction:
            artifact_output_instruction = strict_instruction
        if strict_contract:
            output_contract = strict_contract

        test_framework_instruction = ""
        if test_commands:
            joined_test_commands = "\n".join(f"- {command}" for command in test_commands)
            test_framework_instruction = textwrap.dedent(
                f"""
                Test command contract:
                The runner will execute exactly these commands:
                {joined_test_commands}
                Generated tests and support code must use only dependencies
                available to those commands. If the commands use `unittest` or
                raw `python3`, do not import pytest or any third-party test
                framework. Do not claim tests passed; executable command
                results decide that.

                Stage test scope contract:
                Generated tests are executable propositions for the current
                stage only. They must not assert behavior that belongs to a
                later stage or a broader subsystem than the current stage title
                and goal. If the current stage says single-page, leaf-only,
                parser-only, smoke-only, or another limited scope, tests must
                stay inside that limit. Do not add split, multi-page,
                integration, persistence, optimizer, network, UI, or end-to-end
                assertions unless those words are part of the current stage
                goal. Dependencies from completed earlier stages may be used
                only as read-only setup/evidence.

                Existing API import contract:
                If generated tests or current-stage product code import from
                an existing/read-only module, they must use symbol names that
                are visibly defined in the provided file context. Do not invent
                compatibility names such as `encode_record` when the existing
                module exposes `encode`. If a generated test wants a local
                descriptive name, alias the real API in the import line, e.g.
                `from module import encode as encode_record`. Do not add
                compatibility aliases to earlier-stage product modules solely
                to satisfy generated test wording.
                """
            ).strip()

        semantic_contract_instruction = ""
        if semantic_contracts:
            semantic_contract_instruction = textwrap.dedent(
                f"""
                Semantic contracts from executable evidence:
                {semantic_contracts_document(semantic_contracts)}

                These contracts are fixed for this repair round unless the
                command evidence proves the test harness itself is invalid.
                Do not emit artifacts that violate them.
                """
            ).strip()

        focused_repair_instruction = ""
        if latest_repair_advice:
            latest_strategy = str(latest_repair_advice.get("strategy", "small_patch"))
            harness_strategy = latest_strategy in TEST_HARNESS_WRITE_STRATEGIES
            mixed_product_test_strategy = latest_strategy in MIXED_PRODUCT_TEST_WRITE_STRATEGIES
            advice_product_focus = [
                str(path)
                for path in latest_repair_advice.get("focus_files", [])
                if isinstance(path, str) and not str(path).startswith("tests/")
            ]
            advice_test_focus = [
                str(path)
                for path in latest_repair_advice.get("focus_files", [])
                if isinstance(path, str) and str(path).startswith("tests/")
            ]
            if mixed_product_test_strategy:
                advice_focus = [*advice_product_focus, *advice_test_focus]
                advice_readonly = []
            else:
                advice_focus = [
                    str(path)
                    for path in (advice_test_focus if harness_strategy else advice_product_focus)
                ]
                advice_readonly = [
                    str(path)
                    for path in (advice_product_focus if harness_strategy else advice_test_focus)
                ]
            advice_instructions = [
                str(item)
                for item in latest_repair_advice.get("instructions", [])
                if isinstance(item, str)
            ]
            raw_repair_actions = latest_repair_advice.get("repair_actions", [])
            if isinstance(raw_repair_actions, list):
                for action in raw_repair_actions:
                    if not isinstance(action, dict):
                        continue
                    instruction = str(action.get("instruction") or "").strip()
                    if not instruction:
                        continue
                    action_id = str(action.get("id") or "R??")
                    action_kind = str(action.get("kind") or "repair_action")
                    advice_instructions.append(f"{action_id} {action_kind}: {instruction}")
            advice_instructions = unique_ordered(advice_instructions)
            focus_label = (
                "generated_test_and_product_focus_files"
                if mixed_product_test_strategy
                else "test_harness_focus_files"
                if harness_strategy
                else "product_focus_files"
            )
            readonly_label = (
                "readonly_external_context"
                if mixed_product_test_strategy
                else "readonly_product_api_context"
                if harness_strategy
                else "readonly_evidence_files"
            )
            if mixed_product_test_strategy:
                test_edit_rule = (
                    "- This strategy may edit only the named stage-generated tests and current-stage product files; align both to the same SPEC proposition."
                )
            elif harness_strategy:
                test_edit_rule = (
                    "- This strategy may edit only the named test harness files; use product files as API context unless later executable evidence proves product behavior is wrong."
                )
            else:
                test_edit_rule = "- Do not edit tests unless the repair advice strategy explicitly says the test harness is invalid."
            focused_artifact_rule = (
                "- For this strategy, return one BEGIN_FILE/END_FILE full replacement for the named test file. Do not use search_replace."
                if latest_strategy == "rewrite_current_stage_tests_to_scope"
                else "- Prefer one BEGIN_SEARCH_REPLACE block or one minimal diff."
            )
            focused_repair_instruction = textwrap.dedent(
                f"""
                Focused repair advice from executable evidence:
                - strategy: {latest_strategy}
                - {focus_label}: {", ".join(advice_focus) if advice_focus else "(infer one writable file from evidence)"}
                - {readonly_label}: {", ".join(advice_readonly) if advice_readonly else "(none)"}
                - required_actions:
                {chr(10).join(f"  - {item}" for item in advice_instructions) if advice_instructions else "  - Use the failed command evidence to identify one smallest product-code action."}

                Focused repair rule:
                {focused_artifact_rule}
                - Touch only one writable focus file unless two writable files are explicitly named as the same root cause.
                - Do not rewrite whole files when the evidence names a local branch, constructor, import, or operator case, except for strategy=rewrite_current_stage_tests_to_scope.
                {test_edit_rule}
                - If the exact local branch is not visible in context, return MISSING_CONTEXT for that focus file instead of guessing.
                """
            ).strip()

        current_transition = transition_for_failure(final_failure_type)
        role_label = (
            current_transition.next_role
            if round_index > 1 or force_root_cause_recovery
            else "stage_coder"
        )
        transition_instruction = ""
        if (round_index > 1 or force_root_cause_recovery) and current_transition.instructions:
            transition_instruction = textwrap.dedent(
                f"""
                Current supervisor transition rule:
                - failure_type: {current_transition.failure_type}
                - action: {current_transition.action}
                - required_actions:
                {chr(10).join(f"  - {item}" for item in current_transition.instructions)}
                """
            ).strip()
        semantic_repair_mode = bool(
            round_index > 1
            and semantic_contracts
            and role_label not in {"format_repair", "root_cause_repair"}
        )
        if semantic_repair_mode:
            role_label = "semantic_repair"
        format_repair_mode = bool(round_index > 1 and role_label == "format_repair")
        coder_call_function = "generate_artifact"
        if round_index > 1:
            coder_call_function = "format_repair" if role_label == "format_repair" else "repair_artifact"
            if role_label == "root_cause_repair":
                coder_call_function = "artifact_writer"
            if semantic_repair_mode:
                coder_call_function = "semantic_repair"
                artifact_output_instruction = textwrap.dedent(
                    """
                    Semantic repair output:
                    - Internally decide one proposition C from the semantic
                      contracts and one smallest product-code action A that
                      satisfies it. Do not print this reasoning.
                    - The first non-whitespace characters of the response must
                      be exactly `BEGIN_SEARCH_REPLACE: ` or `diff --git `.
                    - Return exactly one atomic artifact.
                    - Allowed form A must match this grammar exactly:
                      BEGIN_SEARCH_REPLACE: path/to/product_file.py
                      <<<<<<< SEARCH
                      exact old text
                      =======
                      exact new text
                      >>>>>>> REPLACE
                      END_SEARCH_REPLACE
                    - Allowed form B: one minimal unified diff touching one
                      product-code file.
                    - Do not return JSON artifacts, BEGIN_FILE,
                      BEGIN_APPEND_FILE, whole-file content, prose, test
                      reports, or self-judgement.
                    - Do not edit tests. Tests are fixed semantic evidence.
                    - Do not wrap the artifact in markdown fences.
                    - If one atomic product-code edit cannot satisfy the listed
                      semantic contracts, return MISSING_CONTEXT with exact
                      missing file paths.
                    """
                ).strip()
                output_contract = (
                    "Return ONLY one short BEGIN_SEARCH_REPLACE/END_SEARCH_REPLACE block "
                    "for product code, or one minimal unified diff touching one product file. "
                    "First non-whitespace bytes must be BEGIN_SEARCH_REPLACE: or diff --git. "
                    "No JSON. No BEGIN_FILE. No BEGIN_APPEND_FILE. No prose. No fences. Do not edit tests."
                )
                semantic_contract_instruction += textwrap.dedent(
                    """

                    Semantic repair formal rule:
                    Let S be the set of semantic contracts above and A be the
                    emitted artifact. A is acceptable only if:
                    - |A| = 1 atomic artifact.
                    - type(A) is BEGIN_SEARCH_REPLACE or unified_diff.
                    - touched_files(A) contains exactly one product-code file.
                    - touched_files(A) excludes tests/.
                    - A directly changes behavior required by at least one C in S.
                    """
                ).rstrip()

        if role_label == "root_cause_repair":
            analysis_instruction = textwrap.dedent(
                f"""
                Act as the objective root-cause analysis role for this request.

                Request:
                {args.brief}

                Trigger:
                The latest executable check has the same failure signature as a
                previous round. A patch that does not change the failure
                signature is not evidence of progress.

                Current supervisor transition rule:
                - failure_type: {current_transition.failure_type}
                - action: {current_transition.action}

                Produce a compact diagnostic document before any coder writes
                another patch. Do not emit code artifacts.

                Before selecting a new hypothesis, read the latest Failure
                Analysis document if present. Treat its rejected_hypotheses,
                forbidden_focus, and active_constraints as binding supervisor
                constraints unless command evidence directly contradicts them.

                If a Mechanical Probe document is present, treat every probe
                fact as authoritative executable evidence. Do not recompute or
                contradict deterministic facts such as byte sizes, parsed
                symbols, or resolved paths. Your chosen_root_cause must be
                consistent with those probe facts.

                Required report schema:
                ROOT_CAUSE_REPORT
                - repeated_failure_signature: one line
                - failing_observation: one line grounded in command evidence
                - rejected_hypotheses: 1-3 bullets naming edits that already failed or are not sufficient
                - chosen_root_cause: one concrete invariant or branch that likely explains the failure
                - patch_target: one writable product file and one function/region
                - patch_rule: one imperative sentence the coder must implement
                - stop_rule: one sentence saying when to stop rather than guess

                Logical constraint:
                Let F be the repeated failure signature and H be the chosen
                root-cause hypothesis. The report is valid only if H explains F
                and H is different from the previously failed local edit.

                If the visible context is insufficient, return only:
                MISSING_CONTEXT: path/to/file.py reason

                Hard bounds:
                - Each field value must be at most two short sentences.
                - Do not include self-correction narration such as "wait",
                  "actually", or "let me re-examine".
                - Do not perform arithmetic in prose when a Mechanical Probe
                  gives the exact fact.
                """
            ).strip()
            analysis_contract = (
                "Return ONLY ROOT_CAUSE_REPORT in the requested schema, or MISSING_CONTEXT. "
                "No code artifacts, no patches, no markdown fences, no long prose."
            )
            analysis_partial_path = run_dir / f"02-r{round_index:02d}-root-cause-analysis.partial.md"
            if args.stream:
                written.append(analysis_partial_path)
            update_analysis_stream_status = make_stream_callback(
                f"root cause analysis round {round_index}",
                analysis_partial_path,
                current_round=round_index,
            )
            try:
                guard_action("root_cause_analysis_api_call", action_type="api_call", risk_class="read_only")
                analysis_doc = run_skill_call(
                    client=client,
                    skill=judge_skill,
                    spec=spec,
                    instruction=analysis_instruction,
                    agent_level="judge",
                    project_manifest_text=manifest_text,
                    file_context=file_context,
                    documents=documents[-args.document_window :],
                    output_contract=analysis_contract,
                    stream_output_path=analysis_partial_path if args.stream else None,
                    stream_callback=update_analysis_stream_status if args.stream else None,
                    stream_guard=root_cause_stream_guard if args.stream else None,
                    call_function="root_cause_analysis",
                )
            except LLMStreamAbortError as exc:
                api_calls += 1
                if latest_stream_status:
                    latest_stream_status["status"] = "aborted"
                    latest_stream_status["abort_reason"] = exc.reason
                    latest_stream_status["abort_code"] = exc.code
                abort_doc = textwrap.dedent(
                    f"""
                    ## Root Cause Stream Abort

                    - status: FAIL
                    - failure_type: {exc.code}
                    - code: {exc.code}
                    - reason: {exc.reason}
                    - score: {exc.score}
                    - threshold: {exc.threshold}
                    - partial_output: {display_path(analysis_partial_path, original_project)}
                    - chunks_received: {exc.stats.chunks_received}
                    - content_chunks: {exc.stats.content_chunks}
                    - bytes_received: {exc.stats.bytes_received}

                    The diagnostic response exceeded a bounded root-cause
                    report contract. The next round must summarize the diagnosis
                    before patch generation.
                    """
                ).strip()
                path = write_run_document(run_dir, f"03-r{round_index:02d}-root-cause-stream-abort.md", abort_doc)
                written.append(path)
                documents.append((f"Root cause stream abort round {round_index}", abort_doc))
                final_verdict = "patch_failed"
                final_failure_type = exc.code
                record_transition(final_failure_type, round_index, abort_doc)
                write_partial_manifest("root_cause_stream_aborted", {"current_round": round_index})
                if not consume_failure_budget(final_failure_type, round_index):
                    break
                continue
            except RunnerError as exc:
                if not is_reasoning_only_error(exc):
                    raise
                api_calls += 1
                if latest_stream_status:
                    latest_stream_status["status"] = "aborted"
                    latest_stream_status["abort_reason"] = str(exc)
                    latest_stream_status["abort_code"] = "reasoning_only_output"
                abort_doc = textwrap.dedent(
                    f"""
                    ## Root Cause Reasoning-Only Abort

                    - status: FAIL
                    - failure_type: reasoning_only_output
                    - reason: {exc}
                    - partial_output: {display_path(analysis_partial_path, original_project)}

                    Root-cause analysis emitted hidden reasoning chunks but no
                    bounded report content. Stop this diagnostic path instead
                    of guessing a patch.
                    """
                ).strip()
                path = write_run_document(run_dir, f"03-r{round_index:02d}-root-cause-stream-abort.md", abort_doc)
                written.append(path)
                documents.append((f"Root cause stream abort round {round_index}", abort_doc))
                final_verdict = "patch_failed"
                final_failure_type = "reasoning_only_output"
                record_transition(final_failure_type, round_index, abort_doc)
                write_partial_manifest("root_cause_stream_aborted", {"current_round": round_index})
                if not consume_failure_budget(final_failure_type, round_index):
                    break
                continue
            if latest_stream_status:
                latest_stream_status["status"] = "completed"
            api_calls += 1
            analysis_path = write_run_document(run_dir, f"02-r{round_index:02d}-root-cause-analysis.md", analysis_doc)
            written.append(analysis_path)
            documents.append((f"Root cause analysis round {round_index}", analysis_doc))
            write_partial_manifest("root_cause_analysis_written", {"current_round": round_index})

            analysis_missing_requests = extract_missing_context_requests(analysis_doc)
            if analysis_missing_requests:
                requested_paths = unique_ordered(path for request in analysis_missing_requests for path in request.paths)
                safe_existing = [
                    path
                    for path in requested_paths
                    if path in existing_project_paths and path not in allowed_artifact_paths and path not in context_files
                ]
                if safe_existing:
                    context_files = unique_ordered([*context_files, *safe_existing])
                    readonly_artifact_paths = unique_ordered([*readonly_artifact_paths, *safe_existing])
                    artifact_policy = ArtifactPathPolicy(
                        allowed_paths=tuple(allowed_artifact_paths),
                        readonly_paths=tuple(readonly_artifact_paths),
                        existing_paths=tuple(existing_project_paths),
                        allow_extra_new_files=not bool(args.no_extra_files),
                    )
                failure_doc = textwrap.dedent(
                    f"""
                    ## Root Cause Missing Context

                    - failure_type: missing_context
                    - requested_paths: {", ".join(requested_paths) or "(none)"}
                    - added_context_paths: {", ".join(safe_existing) or "(none)"}

                    Runner action:
                    - Collect safe existing project files as read-only context.
                    - Retry root-cause analysis before patch generation.
                    """
                ).strip()
                path = write_run_document(run_dir, f"03-r{round_index:02d}-root-cause-missing-context.md", failure_doc)
                written.append(path)
                documents.append((f"Root cause missing context round {round_index}", failure_doc))
                final_verdict = "patch_failed"
                final_failure_type = "missing_context"
                record_transition(final_failure_type, round_index, failure_doc)
                write_partial_manifest("root_cause_missing_context", {"current_round": round_index})
                if not consume_failure_budget(final_failure_type, round_index):
                    break
                continue

            patch_plan_doc = run_patch_planner(round_index, role_label, analysis_doc)
            contradiction_doc = patch_plan_mechanical_probe_contradiction_document(patch_plan_doc, documents)
            if contradiction_doc:
                path = write_run_document(run_dir, f"03-r{round_index:02d}-patch-plan-mechanical-contradiction.md", contradiction_doc)
                written.append(path)
                documents.append((f"Patch plan mechanical contradiction round {round_index}", contradiction_doc))
                final_verdict = "patch_failed"
                final_failure_type = "mechanical_probe_contradiction"
                record_transition(final_failure_type, round_index, contradiction_doc)
                write_partial_manifest("patch_plan_mechanical_contradiction", {"current_round": round_index})
                if not consume_failure_budget(final_failure_type, round_index):
                    break
                continue
            patch_plan_paths = patch_plan_paths_from_text(patch_plan_doc, existing_project_paths)
            plan_required_paths = [
                path for path in patch_plan_paths.get("required_paths", []) if not path.startswith("tests/")
            ]
            plan_readonly_paths = patch_plan_paths.get("readonly_paths", [])
            plan_forbidden_paths = patch_plan_paths.get("forbidden_paths", [])
            patch_plan_missing_context = bool(
                re.search(r"(?im)^\s*-\s*patch_type\s*:\s*missing_context\s*$", patch_plan_doc)
            )
            if patch_plan_missing_context and not plan_required_paths:
                failure_doc = textwrap.dedent(
                    f"""
                    ## Patch Plan Missing Context

                    - failure_type: missing_context
                    - reason: patch_planner did not identify a writable product required_path

                    Runner action:
                    - Stop before artifact_writer.
                    - Do not guess an executable patch without a concrete product path.
                    """
                ).strip()
                path = write_run_document(run_dir, f"03-r{round_index:02d}-patch-plan-missing-context.md", failure_doc)
                written.append(path)
                documents.append((f"Patch plan missing context round {round_index}", failure_doc))
                final_verdict = "patch_failed"
                final_failure_type = "missing_context"
                record_transition(final_failure_type, round_index, failure_doc)
                write_partial_manifest("patch_plan_missing_context", {"current_round": round_index})
                if not consume_failure_budget(final_failure_type, round_index):
                    break
                continue
            if plan_required_paths or plan_readonly_paths or plan_forbidden_paths:
                allowed_artifact_paths, readonly_artifact_paths = merge_artifact_policy_paths(
                    allowed_artifact_paths,
                    readonly_artifact_paths,
                    plan_required_paths,
                    [*plan_readonly_paths, *plan_forbidden_paths],
                )
                artifact_policy = ArtifactPathPolicy(
                    allowed_paths=tuple(allowed_artifact_paths),
                    readonly_paths=tuple(readonly_artifact_paths),
                    existing_paths=tuple(existing_project_paths),
                    allow_extra_new_files=not bool(args.no_extra_files),
                )
                context_files = unique_ordered(
                    [
                        *context_files,
                        *plan_required_paths,
                        *plan_readonly_paths,
                        *plan_forbidden_paths,
                    ]
                )
                current_paths = current_context_paths()
                symbol_ledger = python_public_symbol_ledger(project, current_paths)
                file_context = collect_file_context(project, current_paths, args.max_context_chars, context_slices)
                if symbol_ledger:
                    file_context = symbol_ledger + "\n\n" + file_context
                writable_targets = ", ".join(allowed_artifact_paths) if allowed_artifact_paths else "(none)"
                readonly_context_files = list(
                    dict.fromkeys([*context_files, *[path for path in slice_context_files if path not in allowed_artifact_paths]])
                )
                readonly_targets = ", ".join(readonly_context_files) if readonly_context_files else "(none)"
                policy_doc = textwrap.dedent(
                    f"""
                    ## Patch Plan Path Policy

                    - required_paths: {", ".join(plan_required_paths) or "(none)"}
                    - readonly_paths: {", ".join(plan_readonly_paths) or "(none)"}
                    - forbidden_paths: {", ".join(plan_forbidden_paths) or "(none)"}

                    Runner action:
                    - Promote required product paths to writable targets.
                    - Keep readonly and forbidden paths as evidence-only.
                    - Ignore unresolved or tests/ required paths.
                    """
                ).strip()
                policy_path = write_run_document(run_dir, f"03-r{round_index:02d}-patch-plan-policy.md", policy_doc)
                written.append(policy_path)
                documents.append((f"Patch plan policy round {round_index}", policy_doc))
            artifact_output_instruction = textwrap.dedent(
                f"""
                Root-cause patch output:
                - Use the latest PATCH_PLAN as the binding patch proposition.
                - Implement only its minimal_patch_goal in its required_path.
                - Do not repeat previously failed local edits unless the PATCH_PLAN
                  explicitly says why the previous attempt was incomplete.
                - The first non-whitespace characters must be exactly
                  `BEGIN_SEARCH_REPLACE: ` or `diff --git `.
                - Return exactly one atomic product-code artifact.
                - Do not return prose, markdown fences, JSON, BEGIN_FILE,
                  BEGIN_APPEND_FILE, test edits, or alternative patches.

                Valid form A:
                BEGIN_SEARCH_REPLACE: path/to/product_file.py
                <<<<<<< SEARCH
                exact old text
                =======
                exact new text
                >>>>>>> REPLACE
                END_SEARCH_REPLACE

                Valid form B:
                one minimal unified diff touching one product-code file.

                Latest PATCH_PLAN:
                {patch_plan_doc}
                """
            ).strip()
            output_contract = (
                "Return ONLY one root-cause patch artifact. First non-whitespace bytes: "
                "BEGIN_SEARCH_REPLACE: or diff --git. No prose. No fences. No JSON. No tests."
            )

        artifact_failure_instruction = artifact_failure_instruction_from_documents(documents, args.document_window)
        coder_instruction = textwrap.dedent(
            f"""
            Act as the {role_label} coding role for this request.

            Request:
            {args.brief}

            Round:
            {round_index} of {final_round}

            Supervisor state:
            - previous_failure_type: {final_failure_type or "(none)"}
            - current_role: {role_label}
            - transition_action: {current_transition.action}

            New file targets:
            {new_file_instruction}

            Writable targets:
            {writable_targets}

            Writable policy:
            {extra_file_policy}

            Read-only context files:
            {readonly_targets}

            {small_patch_instruction}

            {replace_file_instruction}

            {repair_instruction}

            {transition_instruction}

            {test_framework_instruction}

            {semantic_contract_instruction}

            {focused_repair_instruction}

            {artifact_failure_instruction}

            Use only SPEC.md, PM/Judge documents, command results, project
            manifest, and included file contents.
            Before emitting an artifact, internally reduce the current edit to:
            P = visible premise, C = fixed constraint, G = target behavior,
            E = failing or missing evidence, A = smallest valid action. If any
            element is unknown, return MISSING_CONTEXT instead of guessing.

            {artifact_output_instruction}
            """
        ).strip()
        coder_partial_path = run_dir / f"02-r{round_index:02d}-coder-output.partial.md"
        deterministic_repair_doc = deterministic_repair_candidate[0] if deterministic_repair_candidate else None
        deterministic_repair_summary = deterministic_repair_candidate[1] if deterministic_repair_candidate else None
        if args.stream and not deterministic_repair_doc:
            written.append(coder_partial_path)
        update_stream_status = make_stream_callback(
            f"coder round {round_index}",
            coder_partial_path,
            current_round=round_index,
        )
        coder_stream_guard = None
        if args.stream:
            coder_stream_guard = (
                lambda partial_text, single_mode=single_artifact_stream_mode: artifact_stream_guard(
                    partial_text,
                    single_artifact_mode=single_mode,
                    artifact_policy=artifact_policy,
                )
            )

        stream_salvaged = False
        if deterministic_repair_doc:
            coder_doc = deterministic_repair_doc
            if deterministic_repair_summary:
                deterministic_path = write_run_document(
                    run_dir,
                    f"02-r{round_index:02d}-deterministic-repair.md",
                    deterministic_repair_summary,
                )
                written.append(deterministic_path)
                documents.append((f"Deterministic repair round {round_index}", deterministic_repair_summary))
            write_partial_manifest("deterministic_repair_selected", {"current_round": round_index})
        else:
            try:
                guard_action("coder_api_call", action_type="api_call", risk_class="read_only")
                coder_doc = run_skill_call(
                    client=client,
                    skill=coder_skill,
                    spec=spec,
                    instruction=coder_instruction,
                    agent_level="coder",
                    project_manifest_text=manifest_text,
                    file_context=file_context,
                    documents=documents[-args.document_window :],
                    output_contract=output_contract,
                    stream_output_path=coder_partial_path if args.stream else None,
                    stream_callback=update_stream_status if args.stream else None,
                    stream_guard=coder_stream_guard,
                    call_function=coder_call_function,
                )
            except LLMStreamAbortError as exc:
                api_calls += 1
                salvaged_doc = None
                if exc.code == "stream_readonly_artifact_path":
                    salvaged_doc = salvage_completed_artifact_prefix_before_readonly_path(
                        exc.partial_text,
                        artifact_policy,
                    )
                if salvaged_doc:
                    coder_doc = salvaged_doc
                    stream_salvaged = True
                    if latest_stream_status:
                        latest_stream_status["status"] = "salvaged"
                        latest_stream_status["abort_reason"] = exc.reason
                        latest_stream_status["abort_code"] = exc.code
                    salvage_doc = textwrap.dedent(
                        f"""
                        ## Stream Artifact Salvage

                        - status: PASS
                        - original_failure_type: {exc.code}
                        - reason: {exc.reason}
                        - partial_output: {display_path(coder_partial_path, original_project)}
                        - salvaged_bytes: {len(salvaged_doc.encode("utf-8"))}

                        Runner action:
                        - Truncated the stream before the first readonly artifact marker.
                        - Reused only completed writable artifacts from the safe prefix.
                        - The salvaged output still goes through normal artifact lint,
                          extraction, apply, and acceptance checks.
                        """
                    ).strip()
                    path = write_run_document(run_dir, f"03-r{round_index:02d}-stream-salvage.md", salvage_doc)
                    written.append(path)
                    documents.append((f"Stream salvage round {round_index}", salvage_doc))
                    write_partial_manifest("stream_artifact_salvaged", {"current_round": round_index})
                else:
                    if latest_stream_status:
                        latest_stream_status["status"] = "aborted"
                        latest_stream_status["abort_reason"] = exc.reason
                        latest_stream_status["abort_code"] = exc.code
                    abort_doc = textwrap.dedent(
                        f"""
                        ## Stream Artifact Abort

                        - status: FAIL
                        - failure_type: {exc.code}
                        - code: {exc.code}
                        - reason: {exc.reason}
                        - score: {exc.score}
                        - threshold: {exc.threshold}
                        - partial_output: {display_path(coder_partial_path, original_project)}
                        - chunks_received: {exc.stats.chunks_received}
                        - content_chunks: {exc.stats.content_chunks}
                        - bytes_received: {exc.stats.bytes_received}

                        The partial response crossed the artifact anomaly threshold before
                        completion. Treat this as an artifact protocol failure. The next
                        coder round must use a stricter concise artifact contract.
                        """
                    ).strip()
                    path = write_run_document(run_dir, f"03-r{round_index:02d}-stream-abort.md", abort_doc)
                    written.append(path)
                    documents.append((f"Stream abort round {round_index}", abort_doc))
                    final_verdict = "patch_failed"
                    final_failure_type = exc.code
                    record_transition(final_failure_type, round_index, abort_doc)
                    write_partial_manifest("stream_artifact_aborted", {"current_round": round_index})
                    if not consume_failure_budget(final_failure_type, round_index):
                        break
                    continue
        if latest_stream_status and not stream_salvaged and not deterministic_repair_doc:
            latest_stream_status["status"] = "completed"
        if not stream_salvaged and not deterministic_repair_doc:
            api_calls += 1
        coder_path = write_run_document(run_dir, f"02-r{round_index:02d}-coder-output.md", coder_doc)
        written.append(coder_path)
        write_partial_manifest("coder_output_written", {"current_round": round_index})

        missing_context_requests = extract_missing_context_requests(coder_doc)
        if missing_context_requests:
            requested_paths = unique_ordered(path for request in missing_context_requests for path in request.paths)
            safe_existing = [
                path
                for path in requested_paths
                if path in existing_project_paths and path not in allowed_artifact_paths and path not in context_files
            ]
            if safe_existing:
                context_files = unique_ordered([*context_files, *safe_existing])
                readonly_artifact_paths = unique_ordered([*readonly_artifact_paths, *safe_existing])
                artifact_policy = ArtifactPathPolicy(
                    allowed_paths=tuple(allowed_artifact_paths),
                    readonly_paths=tuple(readonly_artifact_paths),
                    existing_paths=tuple(existing_project_paths),
                    allow_extra_new_files=not bool(args.no_extra_files),
                )
            if semantic_repair_mode:
                missing_context_failure_type = "semantic_repair_missing_context"
            elif format_repair_mode:
                missing_context_failure_type = "format_repair_missing_context"
            else:
                missing_context_failure_type = "missing_context"
            failure_doc = textwrap.dedent(
                f"""
                ## Missing Context Request

                - failure_type: {missing_context_failure_type}
                - requested_paths: {", ".join(requested_paths) or "(none)"}
                - added_context_paths: {", ".join(safe_existing) or "(none)"}

                Runner action:
                - Collect safe existing project files as read-only context.
                - Retry the same coding role without guessing missing content.
                """
            ).strip()
            path = write_run_document(run_dir, f"03-r{round_index:02d}-missing-context.md", failure_doc)
            written.append(path)
            documents.append((f"Missing context round {round_index}", failure_doc))
            final_verdict = "patch_failed"
            final_failure_type = missing_context_failure_type
            record_transition(final_failure_type, round_index, failure_doc)
            write_partial_manifest("missing_context", {"current_round": round_index})
            if not consume_failure_budget(final_failure_type, round_index):
                break
            continue

        if is_non_artifact_output(coder_doc):
            non_artifact_failure_type = "format_repair_no_artifact" if format_repair_mode else "non_artifact_output"
            failure_doc = textwrap.dedent(
                f"""
                ## Non Artifact Output

                - failure_type: {non_artifact_failure_type}
                - budget_bytes_before_artifact: {ARTIFACT_OUTPUT_BUDGET_BYTES}
                - first_artifact_marker_offset: {first_artifact_marker_offset(coder_doc)}
                - output_bytes: {len(coder_doc.encode("utf-8"))}

                Runner action:
                - Reject this coder output as an artifact contract violation.
                - Route through PM semantic salvage / format_repair instead of applying prose.
                """
            ).strip()
            path = write_run_document(run_dir, f"03-r{round_index:02d}-non-artifact-output.md", failure_doc)
            written.append(path)
            documents.append((f"Non artifact output round {round_index}", failure_doc))
            final_verdict = "patch_failed"
            final_failure_type = non_artifact_failure_type
            record_transition(final_failure_type, round_index, coder_doc)
            write_partial_manifest("non_artifact_output", {"current_round": round_index})
            if not consume_failure_budget(final_failure_type, round_index):
                break
            continue

        lint_findings = lint_artifact_output(
            coder_doc,
            test_commands,
            semantic_contracts,
            semantic_repair_mode=semantic_repair_mode,
            format_repair_mode=format_repair_mode,
            forbidden_actions=[
                *[
                    str(item)
                    for item in latest_repair_advice.get("instructions", [])
                    if isinstance(item, str)
                ],
                *[
                    str(item)
                    for item in latest_repair_advice.get("evidence", [])
                    if isinstance(item, str)
                ],
            ] if latest_repair_advice else [],
            project=project,
            authorized_test_edit_paths=authorized_test_edit_paths_from_triages(),
        )
        lint_findings.extend(
            lint_stage_scope_output(
                coder_doc,
                args.brief,
                stage_scope_test_paths or stage_generated_test_paths,
                check_product_paths=True,
            )
        )
        blocking_lint = [finding for finding in lint_findings if finding.severity == "error"]
        if lint_findings:
            lint_doc = artifact_lint_document(lint_findings)
            path = write_run_document(run_dir, f"03-r{round_index:02d}-artifact-lint.md", lint_doc)
            written.append(path)
            documents.append((f"Artifact lint round {round_index}", lint_doc))
            write_partial_manifest("artifact_lint", {"current_round": round_index})
        if blocking_lint:
            final_verdict = "patch_failed"
            final_failure_type = artifact_lint_failure_type(blocking_lint)
            lint_doc = artifact_lint_document(blocking_lint)
            triage_record = None
            if any(finding.code in {"semantic_repair_test_edit", "test_edit_attempt"} for finding in blocking_lint):
                triage_record = run_project_policy_triage(
                    round_index,
                    "test_edit_attempt",
                    "\n\n".join([lint_doc, coder_doc]),
                    "decide whether the attempted test edit is generated test-harness repair or forbidden test mutation",
                )
                if triage_allows_test_harness_edit(triage_record):
                    editable_paths = triage_string_list(triage_record, "editable_paths")
                    readonly_paths = triage_string_list(triage_record, "readonly_paths")
                    remember_repair_advice(
                        RepairAdvice(
                            strategy="replace_test_harness",
                            focus_files=tuple(unique_ordered([*editable_paths, *readonly_paths])),
                            instructions=(
                                "Project-policy triage authorized generated test-harness repair.",
                                "Edit only the triage editable_paths; use readonly_paths as product API context.",
                            ),
                            evidence=(
                                f"project_policy_triage={triage_record.get('case_type')}:{triage_record.get('safe_next_action')}",
                            ),
                        ),
                        command_docs,
                    )
            transition_evidence = lint_doc
            if triage_record:
                transition_evidence += "\n\n## Project Policy Triage\n\n" + json.dumps(
                    triage_record,
                    ensure_ascii=False,
                    indent=2,
                )
            record_transition(final_failure_type, round_index, transition_evidence)
            if not consume_failure_budget(final_failure_type, round_index):
                break
            continue

        apply_doc = ""
        apply_ok = True
        patch = ""
        replacements: list[SearchReplaceArtifact] = []
        artifacts: list[FileArtifact] = []
        artifact_error = ""

        if args.artifact_format in {"auto", "json"}:
            try:
                replacements, artifacts = extract_json_artifacts(coder_doc, artifact_policy)
                if args.no_replace_file and any(artifact.mode == "replace" for artifact in artifacts):
                    artifact_error = "replace_file artifacts are disabled for this run; use search_replace or a unified diff"
                    replacements = []
                    artifacts = []
            except RunnerError as exc:
                artifact_error = str(exc)

        if not replacements and not artifacts and args.artifact_format != "json":
            try:
                replacements = extract_search_replace_artifacts(coder_doc, artifact_policy)
            except RunnerError:
                replacements = []

            try:
                artifacts = extract_file_artifacts(coder_doc, artifact_policy)
                if args.no_replace_file and any(artifact.mode == "replace" for artifact in artifacts):
                    artifact_error = "replace_file artifacts are disabled for this run; use search_replace or a unified diff"
                    artifacts = []
            except RunnerError as exc:
                if not artifact_error:
                    artifact_error = str(exc)
                artifacts = []

        mixed_replace_paths = mixed_replace_file_artifact_paths(replacements, artifacts)
        if mixed_replace_paths:
            failure_doc = textwrap.dedent(
                f"""
                ## Artifact Protocol Failure

                - status: FAIL
                - failure_type: stream_mixed_artifact_formats
                - reason: output contained both search_replace and replace-file artifacts for the same path(s)
                - paths: {", ".join(mixed_replace_paths)}

                The runner did not apply any artifact from this round. Return
                exactly one atomic artifact for one writable target path.
                """
            ).strip()
            path = write_run_document(run_dir, f"03-r{round_index:02d}-artifact-lint.md", failure_doc)
            written.append(path)
            documents.append((f"Artifact protocol failure round {round_index}", failure_doc))
            final_failure_type = "stream_mixed_artifact_formats"
            transition = transition_for_failure(final_failure_type)
            state_transitions.append(
                {
                    "round": round_index,
                    "failure_type": final_failure_type,
                    "owner": transition.owner,
                    "next_role": transition.next_role,
                    "action": transition.action,
                }
            )
            transition_doc = failure_transition_document(transition, round_index, failure_doc)
            transition_path = write_run_document(run_dir, f"03-r{round_index:02d}-failure-transition.md", transition_doc)
            written.append(transition_path)
            documents.append((f"Failure transition round {round_index}", transition_doc))
            write_partial_manifest("artifact_protocol_failed", {"current_round": round_index})
            if not consume_failure_budget(final_failure_type, round_index):
                break
            continue

        if replacements or artifacts:
            apply_docs: list[str] = []
            artifact_summaries: list[str] = []
            if replacements:
                replacement_summaries: list[str] = []
                for replacement_index, replacement in enumerate(replacements, start=1):
                    replacement_path = write_run_document(
                        run_dir,
                        f"03-r{round_index:02d}-search-replace-{replacement_index:02d}.md",
                        textwrap.dedent(
                            f"""
                            BEGIN_SEARCH_REPLACE: {replacement.path}
                            <<<<<<< SEARCH
                            {replacement.search}
                            =======
                            {replacement.replace}
                            >>>>>>> REPLACE
                            END_SEARCH_REPLACE
                            """
                        ).strip(),
                    )
                    written.append(replacement_path)
                    replacement_summaries.append(f"- path: {replacement.path}; search_bytes: {len(replacement.search.encode('utf-8'))}; replace_bytes: {len(replacement.replace.encode('utf-8'))}")
                documents.append((f"Search replace round {round_index}", "\n".join(replacement_summaries)))
            if artifacts:
                for artifact_index, artifact in enumerate(artifacts, start=1):
                    artifact_path = write_run_document(
                        run_dir,
                        f"03-r{round_index:02d}-{artifact_index:02d}-{Path(artifact.path).name}",
                        artifact.content,
                    )
                    written.append(artifact_path)
                    artifact_summaries.append(f"- path: {artifact.path}; mode: {artifact.mode}; bytes: {len(artifact.content)}")
                documents.append((f"File artifacts round {round_index}", "\n".join(artifact_summaries)))
            if args.apply:
                guard_action(
                    "artifact_apply",
                    action_type="artifact_apply",
                    risk_class="project_write",
                    metadata={"isolated": args.worktree_mode == "copy"},
                )
                noop_replacements: list[SearchReplaceArtifact] = []
                if replacements:
                    replacements, noop_replacements = partition_noop_replacements(replacements)
                    for replacement in noop_replacements:
                        apply_docs.append(
                            textwrap.dedent(
                                f"""
                                ## Search Replace Apply Result

                                SKIP no-op replacement in `{replacement.path}`
                                - reason: search text and replacement text are identical
                                """
                            ).strip()
                        )
                    if noop_replacements and not replacements and not artifacts:
                        apply_ok = False
                        apply_docs.append(
                            "## Search Replace Apply Result\n\nFAIL: all search_replace artifacts were no-op replacements."
                        )
                transaction_paths = unique_ordered(
                    [replacement.path for replacement in replacements]
                    + [artifact.path for artifact in artifacts]
                )
                transaction_snapshots = snapshot_artifact_targets(project, transaction_paths)
                round_changed_paths: list[str] = []
                for replacement in replacements:
                    try:
                        apply_docs.append(apply_search_replace_artifact(project, replacement, run_dir, round_index))
                        round_changed_paths.append(replacement.path)
                    except RunnerError as exc:
                        apply_ok = False
                        apply_docs.append(f"## Search Replace Apply Result\n\nFAIL applying `{replacement.path}`:\n\n```text\n{exc}\n```")
                for artifact in artifacts:
                    try:
                        apply_docs.append(apply_file_artifact(project, artifact, run_dir, round_index))
                        round_changed_paths.append(artifact.path)
                    except RunnerError as exc:
                        apply_ok = False
                        apply_docs.append(f"## File Artifact Apply Result\n\nFAIL applying `{artifact.path}`:\n\n```text\n{exc}\n```")
                if not apply_ok and transaction_snapshots:
                    restored_paths = restore_artifact_targets(project, transaction_snapshots)
                    apply_docs.append(
                        "## Artifact Transaction Rollback\n\n"
                        "PASS: restored artifact target(s) because at least one artifact in the round failed.\n"
                        + "\n".join(f"- restored: `{path}`" for path in restored_paths)
                    )
                elif apply_ok:
                    changed_paths.extend(round_changed_paths)
                apply_doc = "\n\n---\n\n".join(apply_docs)
            else:
                dry_parts = []
                if replacements:
                    dry_parts.append(f"search_replace artifacts: {len(replacements)}")
                if artifacts:
                    dry_parts.append("file artifacts:\n" + "\n".join(artifact_summaries))
                apply_doc = "## Artifact Apply Result\n\nDRY_RUN: artifacts saved but not applied:\n" + "\n".join(dry_parts)
        else:
            try:
                if args.artifact_format == "json":
                    raise RunnerError(artifact_error or "LLM output did not contain JSON artifacts")
                patch = extract_unified_diff(coder_doc)
            except RunnerError as exc:
                failure_doc = textwrap.dedent(
                    f"""
                    ## Patch Extraction Failure

                    {exc}
                    {("File artifact extraction also failed: " + artifact_error) if artifact_error else ""}

                    Next attempt guidance:
                    - For exact small edits, return BEGIN_SEARCH_REPLACE/END_SEARCH_REPLACE, or
                    - Return a valid unified diff with correct hunk headers, or
                    - for file creation/replacement, return BEGIN_FILE: path plus the complete file content before END_FILE, or
                    - for multi-file tasks, return one non-empty BEGIN_FILE/END_FILE block per target file, or
                    - for truncated files, return BEGIN_APPEND_FILE/END_APPEND_FILE.
                    """
                ).strip()
                path = write_run_document(run_dir, f"03-r{round_index:02d}-patch-failure.md", failure_doc)
                written.append(path)
                documents.append((f"Patch failure round {round_index}", failure_doc))
                final_verdict = "patch_failed"
                if format_repair_mode:
                    final_failure_type = artifact_failure_type(artifact_error, "format_repair_no_artifact") if artifact_error else "format_repair_no_artifact"
                else:
                    final_failure_type = artifact_failure_type(artifact_error, "artifact_invalid") if artifact_error else "patch_extraction_failed"
                record_transition(final_failure_type, round_index, failure_doc)
                write_partial_manifest("patch_extraction_failed", {"current_round": round_index})
                if not consume_failure_budget(final_failure_type, round_index):
                    break
                continue

            patch_path = write_run_document(run_dir, f"03-r{round_index:02d}.patch", patch)
            written.append(patch_path)
            documents.append((f"Patch round {round_index}", patch))
            patch_changed_paths = changed_paths_from_unified_diff(patch)

            if args.apply:
                guard_action(
                    "patch_apply",
                    action_type="artifact_apply",
                    risk_class="project_write",
                    metadata={"isolated": args.worktree_mode == "copy"},
                )
                try:
                    apply_patch_file(project, patch_path)
                    missing_after_apply = missing_changed_paths_after_patch(project, patch_changed_paths)
                    if missing_after_apply:
                        raise RunnerError(
                            "git apply reported success but changed path(s) are missing after apply: "
                            + ", ".join(missing_after_apply)
                        )
                    changed_paths.extend(patch_changed_paths)
                    apply_doc = f"## Patch Apply Result\n\nPASS: applied `{display_path(patch_path, project)}`"
                except RunnerError as exc:
                    apply_ok = False
                    apply_doc = textwrap.dedent(
                        f"""
                        ## Patch Apply Result

                        FAIL:

                        ```text
                        {exc}
                        ```

                        Next attempt guidance:
                        - For exact small edits, return BEGIN_SEARCH_REPLACE/END_SEARCH_REPLACE, or
                        - Return a valid unified diff with correct hunk headers, or
                        - for file creation/replacement, return BEGIN_FILE: path plus the complete file content before END_FILE, or
                        - for multi-file tasks, return one non-empty BEGIN_FILE/END_FILE block per target file, or
                        - for truncated files, return BEGIN_APPEND_FILE/END_APPEND_FILE.
                        """
                    ).strip()
            else:
                apply_doc = f"## Patch Apply Result\n\nDRY_RUN: patch saved but not applied: `{display_path(patch_path, project)}`"

        path = write_run_document(run_dir, f"04-r{round_index:02d}-apply.md", apply_doc)
        written.append(path)
        documents.append((f"Patch apply round {round_index}", apply_doc))
        write_partial_manifest("apply_result_written", {"current_round": round_index})
        if not apply_ok:
            final_verdict = "patch_failed"
            final_failure_type = artifact_failure_type(apply_doc, "patch_apply_failed")
            record_transition(final_failure_type, round_index, apply_doc)
            write_partial_manifest("patch_apply_failed", {"current_round": round_index})
            if not consume_failure_budget(final_failure_type, round_index):
                break
            continue

        command_docs: list[tuple[str, str]] = []
        command_ok = True
        if args.apply:
            guard_action("round_html_smoke", action_type="harness", risk_class="generated_code_execution")
            for index, (doc, ok) in enumerate(
                run_html_smoke_checks(project, html_targets, run_dir, args.command_timeout, tetris_checks=tetris_checks),
                start=1,
            ):
                path = write_run_document(run_dir, f"05-r{round_index:02d}-html-smoke-{index:02d}.md", doc)
                written.append(path)
                command_docs.append((f"HTML smoke round {round_index}.{index}", doc))
                evidence = evidence_from_command_document("html_smoke", f"HTML smoke round {round_index}.{index}", ok, path, original_project, doc)
                evidence["id"] = f"E{len(evidence_records) + 1:02d}"
                evidence_records.append(evidence)
                command_ok = command_ok and ok

            if redis_checks:
                guard_action("round_redis_smoke", action_type="harness", risk_class="generated_code_execution")
                doc, ok = run_redis_smoke_check(project, run_dir, args.command_timeout)
                path = write_run_document(run_dir, f"05-r{round_index:02d}-redis-smoke.md", doc)
                written.append(path)
                command_docs.append((f"Redis smoke round {round_index}", doc))
                evidence = evidence_from_command_document("redis_smoke", f"Redis smoke round {round_index}", ok, path, original_project, doc)
                evidence["id"] = f"E{len(evidence_records) + 1:02d}"
                evidence_records.append(evidence)
                command_ok = command_ok and ok

            guard_action("round_required_path_check")
            for index, (doc, ok) in enumerate(run_required_path_checks(project, required_paths), start=1):
                path = write_run_document(run_dir, f"05-r{round_index:02d}-required-path-{index:02d}.md", doc)
                written.append(path)
                command_docs.append((f"Required path round {round_index}.{index}", doc))
                evidence = evidence_from_command_document("required_path", f"Required path round {round_index}.{index}", ok, path, original_project, doc)
                evidence["id"] = f"E{len(evidence_records) + 1:02d}"
                evidence_records.append(evidence)
                command_ok = command_ok and ok

            for index, command in enumerate(test_commands, start=1):
                doc, ok = run_agent_checked_command(
                    command,
                    action=f"round_test_command_{index}",
                )
                path = write_run_document(run_dir, f"05-r{round_index:02d}-command-{index:02d}.md", doc)
                written.append(path)
                command_docs.append((f"Command result round {round_index}.{index}", doc))
                evidence = evidence_from_command_document("command", f"Command result round {round_index}.{index}", ok, path, original_project, doc)
                evidence["id"] = f"E{len(evidence_records) + 1:02d}"
                evidence_records.append(evidence)
                command_ok = command_ok and ok
                stop_for_command_safety_decision(round_index)

            acceptance_ok = record_acceptance_gate(
                f"round {round_index}",
                f"05-r{round_index:02d}-acceptance-gate.md",
                command_docs,
            )
            command_ok = command_ok and acceptance_ok

        if command_docs and not command_ok:
            summary_doc = observation_summary_document(round_index, command_docs)
            path = write_run_document(run_dir, f"05-r{round_index:02d}-observation-summary.md", summary_doc)
            written.append(path)
            command_docs.append((f"Observation summary round {round_index}", summary_doc))
            record_python_probe_evidence(
                command_docs,
                filename_prefix=f"05-r{round_index:02d}",
                round_index=round_index,
            )
            new_contracts = extract_semantic_contracts_from_command_docs(command_docs, project)
            if new_contracts:
                existing_texts = {contract.text for contract in semantic_contracts}
                for contract in new_contracts:
                    if contract.text not in existing_texts:
                        semantic_contracts.append(
                            dataclasses.replace(contract, contract_id=f"C{len(semantic_contracts) + 1:02d}")
                        )
                        existing_texts.add(contract.text)
                contracts_doc = semantic_contracts_document(semantic_contracts)
                path = write_run_document(run_dir, f"05-r{round_index:02d}-semantic-contracts.md", contracts_doc)
                written.append(path)
                command_docs.append((f"Semantic contracts round {round_index}", contracts_doc))
            advice = repair_advice_from_command_docs(
                command_docs,
                test_commands,
                project,
                stage_generated_test_paths,
            )
            if advice:
                if advice.strategy in TEST_HARNESS_WRITE_STRATEGIES:
                    triage_evidence = "\n\n".join(
                        [
                            repair_advice_document(advice),
                            *[document for _name, document in command_docs],
                        ]
                    )
                    triage_record = run_project_policy_triage(
                        round_index,
                        "test_harness_ownership",
                        triage_evidence,
                        f"decide whether `{advice.strategy}` may edit generated tests",
                    )
                    advice = apply_project_policy_triage_to_advice(advice, triage_record)
                remember_repair_advice(advice, command_docs)
                advice_doc = repair_advice_document(advice)
                path = write_run_document(run_dir, f"05-r{round_index:02d}-repair-advice.md", advice_doc)
                written.append(path)
                command_docs.append((f"Repair advice round {round_index}", advice_doc))

        documents.extend(command_docs)
        current_failure_score = command_failure_score(command_docs)
        current_failure_signature = command_failure_signature(command_docs)
        current_failure_family_signature = command_failure_family_signature(command_docs)
        same_functional_failure = bool(
            current_failure_signature
            and last_functional_failure_signature
            and current_failure_signature == last_functional_failure_signature
        ) or bool(
            current_failure_family_signature
            and last_functional_failure_family_signature
            and current_failure_family_signature == last_functional_failure_family_signature
        )
        if current_failure_signature:
            repeated_same_failure_count = repeated_same_failure_count + 1 if same_functional_failure else 0
        write_partial_manifest(
            "checks_completed",
            {
                "current_round": round_index,
                "command_ok": command_ok,
                "current_failure_score": current_failure_score,
                "current_failure_signature": current_failure_signature,
                "current_failure_family_signature": current_failure_family_signature,
                "same_functional_failure": same_functional_failure,
                "repeated_same_failure_count": repeated_same_failure_count,
            },
        )

        failure_analysis_doc = ""
        if command_docs and not command_ok and same_functional_failure and (current_failure_signature or current_failure_family_signature):
            failure_analysis_doc = run_failure_analysis(
                round_index,
                "repeated_same_failure",
                current_failure_signature or current_failure_family_signature,
                command_docs,
            )
            root_cause_patch_pending = True
            if failure_analyses:
                focus_from_analysis = focus_paths_from_failure_analysis(
                    failure_analyses[-1],
                    existing_project_paths,
                )
                if focus_from_analysis:
                    action = failure_analyses[-1].get("next_required_action", {})
                    previous_focus_files = [
                        str(path)
                        for path in latest_repair_advice.get("focus_files", [])
                        if isinstance(path, str)
                    ]
                    previous_evidence = [
                        str(item)
                        for item in latest_repair_advice.get("evidence", [])
                        if isinstance(item, str)
                    ]
                    required_focus = []
                    forbidden_focus = []
                    active_constraints = []
                    if isinstance(action, dict):
                        required_focus = [
                            str(item)
                            for item in action.get("required_focus", [])
                            if isinstance(item, str)
                        ] if isinstance(action.get("required_focus", []), list) else []
                        forbidden_focus = [
                            str(item)
                            for item in action.get("forbidden_focus", [])
                            if isinstance(item, str)
                        ] if isinstance(action.get("forbidden_focus", []), list) else []
                    if isinstance(failure_analyses[-1].get("active_constraints", []), list):
                        active_constraints = [
                            str(item)
                            for item in failure_analyses[-1].get("active_constraints", [])
                            if isinstance(item, str)
                        ]
                    remember_repair_advice(
                        RepairAdvice(
                            strategy="root_cause_patch",
                            focus_files=tuple(
                                unique_ordered(
                                    [
                                        *focus_from_analysis,
                                        *previous_focus_files,
                                    ]
                                )[:8]
                            ),
                            instructions=tuple(
                                unique_ordered(
                                    [
                                        "Use the structured failure analysis as binding root-cause evidence.",
                                        *[f"Required focus from failure analysis: {item}" for item in required_focus],
                                        *[f"Do not repeat forbidden focus: {item}" for item in forbidden_focus],
                                        *[f"Active constraint: {item}" for item in active_constraints],
                                    ]
                                )
                            ),
                            evidence=tuple(
                                unique_ordered(
                                    [
                                        f"failure_analysis_round={round_index}",
                                        *previous_evidence,
                                    ]
                                )
                            ),
                        ),
                        command_docs,
                    )
                    pending_deterministic_repair = failure_analyses[-1]

        stage_owned_test_paths = unique_ordered([*stage_generated_test_paths, *stage_scope_test_paths])
        repeated_stage_test_failures = stage_test_paths_in_command_docs(command_docs, stage_owned_test_paths)
        already_triaged_stage_tests = any(
            str(record.get("trigger", "")) == "generated_test_oracle_conflict"
            for record in project_policy_triages[-3:]
        )
        if (
            command_docs
            and not command_ok
            and same_functional_failure
            and repeated_stage_test_failures
            and not already_triaged_stage_tests
        ):
            triage_evidence = "\n\n".join(
                [
                    "## Generated Test Oracle Conflict Candidate",
                    "- repeated_failure: true",
                    "- stage_owned_test_paths: " + ", ".join(repeated_stage_test_failures),
                    "- current_failure_signature: " + (current_failure_signature or "(unknown)"),
                    "- current_failure_family_signature: " + (current_failure_family_signature or "(unknown)"),
                    repair_advice_document(
                        RepairAdvice(
                            strategy=str(latest_repair_advice.get("strategy") or "root_cause_patch"),
                            focus_files=tuple(
                                str(path)
                                for path in latest_repair_advice.get("focus_files", [])
                                if isinstance(path, str)
                            ),
                            instructions=tuple(
                                str(item)
                                for item in latest_repair_advice.get("instructions", [])
                                if isinstance(item, str)
                            ),
                            evidence=tuple(
                                str(item)
                                for item in latest_repair_advice.get("evidence", [])
                                if isinstance(item, str)
                            ),
                        )
                    ) if latest_repair_advice else "## Repair Advice\n(none)",
                    failure_analysis_doc if failure_analysis_doc else "## Structured Failure Analysis\n(none)",
                    *[document for _name, document in command_docs],
                ]
            )
            triage_record = run_project_policy_triage(
                round_index,
                "generated_test_oracle_conflict",
                triage_evidence,
                "decide whether repeated stage-owned test failure is a product bug or a generated test-oracle contradiction",
            )
            if triage_allows_test_harness_edit(triage_record):
                triage_editable = [
                    path
                    for path in triage_string_list(triage_record, "editable_paths")
                    if path in repeated_stage_test_failures
                ] or repeated_stage_test_failures
                triage_readonly = [
                    path
                    for path in triage_string_list(triage_record, "readonly_paths")
                    if path not in triage_editable
                ]
                allowed_artifact_paths, readonly_artifact_paths = merge_artifact_policy_paths(
                    allowed_artifact_paths,
                    readonly_artifact_paths,
                    triage_editable,
                    triage_readonly,
                )
                context_files = unique_ordered([*context_files, *triage_editable, *triage_readonly])
                artifact_policy = ArtifactPathPolicy(
                    allowed_paths=tuple(allowed_artifact_paths),
                    readonly_paths=tuple(readonly_artifact_paths),
                    existing_paths=tuple(existing_project_paths),
                    allow_extra_new_files=not bool(args.no_extra_files),
                )
                remember_repair_advice(
                    RepairAdvice(
                        strategy="replace_test_harness",
                        focus_files=tuple(unique_ordered([*triage_editable, *triage_readonly])),
                        instructions=(
                            "Project-policy triage authorized generated test-oracle repair after repeated identical failure.",
                            "Edit only the authorized stage-owned test harness paths.",
                            "Align the test proposition with SPEC.md and the current product API; do not weaken external acceptance requirements.",
                        ),
                        evidence=(
                            f"project_policy_triage={triage_record.get('case_type')}:{triage_record.get('safe_next_action')}",
                            "repeated stage-owned test failure referenced: " + ", ".join(repeated_stage_test_failures),
                        ),
                    ),
                    command_docs,
                )
                if failure_analyses:
                    pending_deterministic_repair = failure_analyses[-1]

        if args.judge_mode == "command-only":
            if command_ok:
                final_verdict = "approved"
                final_failure_type = None
                write_partial_manifest("approved", {"current_round": round_index})
                break
            final_verdict = "test_failed"
            observed_failure_type = failure_summary(final_verdict, evidence_records, final_failure_type or "command_failed")["failure_type"]
            transition_failure_type = "repeated_same_failure" if same_functional_failure else "stage_test_failed"
            final_failure_type = transition_failure_type if same_functional_failure else observed_failure_type
            transition_evidence = "\n\n".join(document for _name, document in command_docs)
            if same_functional_failure and (current_failure_signature or current_failure_family_signature):
                transition_evidence = textwrap.dedent(
                    f"""
                    ## Repeated Same Failure

                    - failure_signature: {current_failure_signature or current_failure_family_signature}
                    - failure_family_signature: {current_failure_family_signature or "(unknown)"}
                    - repeated_same_failure_count: {repeated_same_failure_count}

                    The latest patch did not change the executable failure signature or failure family.
                    The next round must reconsider the root cause before emitting another patch.
                    """
                ).strip() + "\n\n" + transition_evidence
                if failure_analysis_doc:
                    transition_evidence += "\n\n## Structured Failure Analysis\n\n" + failure_analysis_doc
            record_transition(transition_failure_type, round_index, transition_evidence)
            last_functional_failure_signature = current_failure_signature
            last_functional_failure_family_signature = current_failure_family_signature
            write_partial_manifest("test_failed", {"current_round": round_index})
            if not consume_failure_budget(transition_failure_type, round_index, current_failure_score):
                break
            continue

        judge_instruction = judge_review_instruction(args.brief, round_index, final_round)
        judge_partial_path = run_dir / f"06-r{round_index:02d}-judge-review.partial.md"
        if args.stream:
            written.append(judge_partial_path)
            latest_stream_status.clear()
            latest_stream_status.update(
                {
                    "label": f"judge round {round_index}",
                    "status": "starting",
                    "partial_output": display_path(judge_partial_path, original_project),
                }
            )
            write_partial_manifest("judge_streaming", {"current_round": round_index})
        guard_action("judge_api_call", action_type="api_call", risk_class="read_only")
        judge_doc = run_skill_call(
            client=client,
            skill=judge_skill,
            spec=spec,
            instruction=judge_instruction,
            agent_level="judge",
            project_manifest_text=manifest_text,
            file_context=collect_file_context(project, current_context_paths(), args.max_context_chars, context_slices),
            documents=documents[-max(8, args.document_window) :],
            output_contract=JUDGE_REVIEW_OUTPUT_CONTRACT,
            stream_output_path=judge_partial_path if args.stream else None,
            stream_callback=make_stream_callback(
                f"judge round {round_index}",
                judge_partial_path,
                current_round=round_index,
            )
            if args.stream
            else None,
            call_function="judge_review",
        )
        if args.stream and latest_stream_status:
            latest_stream_status["status"] = "completed"
            write_partial_manifest("judge_stream_completed", {"current_round": round_index})
        api_calls += 1
        path = write_run_document(run_dir, f"06-r{round_index:02d}-judge-review.md", judge_doc)
        written.append(path)
        documents.append((f"Judge review round {round_index}", judge_doc))

        if command_ok and judge_approved(judge_doc):
            final_verdict = "approved"
            final_failure_type = None
            write_partial_manifest("approved", {"current_round": round_index})
            break
        final_verdict = "test_failed" if not command_ok else "needs_changes"
        if not command_ok:
            summary = failure_summary(final_verdict, evidence_records, final_failure_type or "command_failed")
            if summary:
                final_failure_type = str(summary.get("failure_type") or "command_failed")
        elif final_verdict == "needs_changes":
            final_failure_type = "judge_requested_changes"
        transition_failure_type = "stage_test_failed" if not command_ok else final_failure_type
        record_transition(transition_failure_type, round_index, judge_doc)
        write_partial_manifest(final_verdict, {"current_round": round_index})
        if not consume_failure_budget(transition_failure_type, round_index, current_failure_score):
            break

    if final_verdict == "approved" and args.apply and args.worktree_mode == "copy":
        guard_action(
            "copy_back",
            action_type="copy_back",
            risk_class="project_write",
            metadata={"approved_paths": unique_ordered(changed_paths)},
        )
        copied_back = copy_allowed_paths_back(project, original_project, unique_ordered(changed_paths))

    final_acceptance_matrix = build_acceptance_matrix(acceptance_criteria, evidence_records)
    manifest_doc = {
        "brief": args.brief,
        "command": "agent",
        "apply": bool(args.apply),
        "requested_max_rounds": args.max_rounds,
        "max_rounds": final_round,
        "functional_round_budget": args.max_rounds,
        "protocol_repair_round_budget": protocol_repair_rounds,
        "adaptive_round_budget": adaptive_round_budget,
        "root_cause_patch_round_budget": root_cause_patch_round_budget,
        "functional_rounds_used": functional_rounds_used,
        "protocol_rounds_used": protocol_rounds_used,
        "adaptive_rounds_used": adaptive_rounds_used,
        "root_cause_patch_rounds_used": root_cause_patch_rounds_used,
        "last_functional_failure_score": last_functional_failure_score,
        "last_functional_failure_signature": last_functional_failure_signature,
        "last_functional_failure_family_signature": last_functional_failure_family_signature,
        "repeated_same_failure_count": repeated_same_failure_count,
        "resumed_from": str(args.resume.resolve()) if args.resume else None,
        "recovery_plan_id": recovery_plan.get("plan_id") if recovery_plan else None,
        "recovery_strategy": recovery_plan.get("strategy") if recovery_plan else None,
        "artifact_format": args.artifact_format,
        "small_patch": bool(args.small_patch),
        "no_replace_file": bool(args.no_replace_file),
        "allow_extra_new_files": not bool(args.no_extra_files),
        "precheck": bool(args.precheck),
        "worktree_mode": args.worktree_mode,
        "worktree_path": str(worktree_path) if worktree_path else None,
        "resumed_worktree_from": str(resume_worktree_source) if resume_worktree_source else None,
        "copied_back": copied_back,
        "changed_paths": unique_ordered(changed_paths),
        "completed_rounds": completed_rounds,
        "final_verdict": final_verdict,
        "final_failure_type": final_failure_type,
        "api_calls": api_calls,
        "model_profile": llm_model_profile_manifest(args),
        "llm_settings": llm_settings_manifest(client),
        "reasoning_records": llm_reasoning_manifest(client),
        "domain_modeling": domain_modeling_state,
        "streaming": dict(latest_stream_status) if latest_stream_status else None,
        "repair_advice": dict(latest_repair_advice) if latest_repair_advice else None,
        "test_commands": test_commands,
        "context_paths": context_files,
        "context_slices": {path: ranges for path, ranges in context_slices.items()},
        "required_paths": required_paths,
        "explicit_required_paths": explicit_required_paths,
        "auto_required_paths": auto_required_paths,
        "stage_scope_test_paths": stage_scope_test_paths,
        "stage_generated_test_paths": stage_generated_test_paths,
        "acceptance_criteria": acceptance_criteria,
        "requirements": [item.to_manifest() for item in requirement_records],
        "observables": [item.to_manifest() for item in observable_records],
        "propositions": proposition_manifest_from_documents(documents),
        "semantic_contracts": [semantic_contract_to_dict(contract) for contract in semantic_contracts],
        "evidence": evidence_records,
        "acceptance_matrix": final_acceptance_matrix,
        "verdicts": [
            verdict_to_manifest(item)
            for item in verdicts_from_acceptance_matrix(final_acceptance_matrix)
        ],
        "acceptance_blockers": acceptance_blockers(final_acceptance_matrix),
        "failure_summary": failure_summary(final_verdict, evidence_records, final_failure_type),
        "failure_analyses": failure_analyses,
        "project_policy_triages": project_policy_triages,
        "state_transitions": state_transitions,
        "cancel_requested": cancel_requested(run_dir),
        "cancel_state": load_cancel_state(run_dir) or None,
        "progress_log": display_path(progress_file_path(run_dir), original_project),
        "progress_event_count": len(read_progress_events(run_dir)),
        "safety_decisions_log": display_path(safety_decisions_file_path(run_dir), original_project),
        "safety_decision_count": len(read_safety_decisions(run_dir)),
        "safety_approvals_log": display_path(safety_approvals_file_path(run_dir), original_project),
        "safety_approval_event_count": len(read_safety_approvals(run_dir)),
        "pending_safety_decisions": pending_safety_decisions(run_dir),
        "blocked_safety_decisions": blocked_safety_decisions(run_dir),
        "action_gate_audit": action_gate_audit(run_dir),
        "budget": budget_status(run_dir),
        "progress": progress_status(run_dir, evaluate=False),
        "documents": [display_path(path, original_project) for path in written],
    }
    attach_regression_memory(manifest_doc, run_dir, original_project, written)
    manifest_path = write_run_document(run_dir, "run.json", json.dumps(manifest_doc, ensure_ascii=False, indent=2))
    written.append(manifest_path)
    if recovery_source is not None:
        complete_stalled_recovery(
            recovery_source,
            run_dir,
            outcome="completed" if final_verdict == "approved" else "failed",
        )

    print(f"run_dir: {run_dir}")
    print(f"api_calls: {api_calls}")
    print(f"final_verdict: {final_verdict}")
    if worktree_path:
        print(f"worktree_path: {worktree_path}")
    if copied_back:
        print(f"copied_back: {', '.join(copied_back)}")
    for path in written:
        print(f"wrote: {path}")
    return 0 if final_verdict == "approved" else 1
