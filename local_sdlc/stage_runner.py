"""Stage-plan and staged execution commands."""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
from pathlib import Path

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
from .budget import *
from .progress_monitor import *
from .run_state import *
from .recovery_plan import validate_recovery_plan
from .recovery_runtime import begin_stalled_recovery, complete_stalled_recovery
from .stages import *
from .stage_planning import *
from .autonomy_runtime import *
from .history import *
from .action_gate import *
from .agent_runner import command_agent
from .learning_context import (
    bind_learning_snapshot_from_args,
    inherit_learning_binding,
    knowledge_binding_manifest,
    knowledge_binding_path,
)


def child_runner_error_summary(error: RunnerError) -> dict[str, object]:
    """Separate a live-API generation timeout from invalid runner configuration."""
    message = str(error).strip()
    timeout_match = re.search(
        r"LLM generation request timed out after ([0-9]+(?:\.[0-9]+)?)s",
        message,
    )
    if timeout_match and "API health after timeout: alive" in message:
        return {
            "failure_type": "llm_generation_timeout",
            "message": message,
            "timeout_seconds": float(timeout_match.group(1)),
            "api_health": "alive",
        }
    return {
        "failure_type": "runner_configuration_error",
        "message": message,
    }


def recovered_llm_timeout(current_timeout: object, recovery_timeout: object) -> float:
    """Resolve CLI defaults before monotonically extending a request window."""
    current = DEFAULT_TIMEOUT if current_timeout is None else float(current_timeout)
    return max(current, float(recovery_timeout))


def command_stage_plan(args: argparse.Namespace) -> int:
    project = args.project.resolve()
    spec_path = resolve_spec_path(project, args.spec_file)
    spec = read_text_if_exists(spec_path)
    if not spec:
        raise RunnerError("SPEC.md is required before planning stages; pass --spec-file or create SPEC.md")
    stages = synthesize_stage_queue(spec, listed_project_files(project))
    if args.format == "json":
        payload = [stage_work_item_manifest(stage) for stage in stages]
        print(json.dumps({"stages": payload}, ensure_ascii=False, indent=2))
    else:
        print(stage_queue_document(stages), end="")
    return 0

def command_run_stages(args: argparse.Namespace) -> int:
    project = args.project.resolve()
    spec_path = resolve_spec_path(project, args.spec_file)
    spec = read_text_if_exists(spec_path)
    if not spec:
        raise RunnerError("SPEC.md is required before running staged agent work")
    if not list(getattr(args, "test_command", []) or []) and bool(
        getattr(args, "spec_verification", True)
    ):
        args.test_command = verification_commands_from_spec(spec)
    if args.stage_max_rounds < 1:
        raise RunnerError("--stage-max-rounds must be at least 1")
    if args.protocol_repair_rounds < 0:
        raise RunnerError("--protocol-repair-rounds must be zero or greater")
    if args.artifact_plan_repair_rounds < 0:
        raise RunnerError("--artifact-plan-repair-rounds must be zero or greater")
    if args.max_stage_recoveries < 0:
        raise RunnerError("--max-stage-recoveries must be zero or greater")
    if args.max_stalled_recoveries < 0:
        raise RunnerError("--max-stalled-recoveries must be zero or greater")
    if args.max_stage_writable_paths < 1:
        raise RunnerError("--max-stage-writable-paths must be at least 1")

    stored_regression_memories = load_regression_memories(project)
    planned = apply_regression_memories_to_stages(
        synthesize_stage_queue(spec, listed_project_files(project)),
        stored_regression_memories,
    )
    selected = selected_stage_queue(planned, args.from_stage, args.to_stage)
    stages = split_oversized_stage_queue(
        selected,
        max_writable_paths=args.max_stage_writable_paths,
    )
    selected_ids = {stage.stage_id for stage in selected}
    prior_context_paths: list[str] = []
    for stage in planned:
        if stage.stage_id in selected_ids:
            break
        prior_context_paths.extend(stage_required_paths(stage))
    recovery_source: Path | None = None
    recovery_document: dict[str, object] = {}
    if getattr(args, "resume_run", None) is not None:
        recovery_source = Path(args.resume_run).resolve()
        raw_plan = getattr(args, "recovery_plan", None)
        if raw_plan is None:
            raise RunnerError("--resume-run requires --recovery-plan")
        recovery_document = validate_recovery_plan(recovery_source, Path(raw_plan))
        planned_target = Path(str(recovery_document["target_run_dir"])).resolve()
        if args.run_dir is not None and resolve_run_dir(project, args.run_dir).resolve() != planned_target:
            raise RunnerError("--run-dir does not match the parent recovery plan target")
        run_dir = make_run_dir(project, planned_target)
    else:
        if getattr(args, "recovery_plan", None) is not None:
            raise RunnerError("--recovery-plan requires --resume-run")
        run_dir = make_run_dir(project, args.run_dir)
    args.completed_run_dir = run_dir
    initialize_budget(run_dir, budget_limits_from_args(args), scope_kind="goal")
    initialize_progress_monitor(
        run_dir,
        progress_policy_from_args(args),
        scope_kind="goal",
    )
    if recovery_source is not None:
        begin_stalled_recovery(
            recovery_source,
            run_dir,
            recovery_document,
            cancel_dirs=(recovery_source,),
            budget_dirs=(recovery_source,),
            progress_dirs=(),
        )
    else:
        begin_action(
            run_dir,
            "run_stages_setup",
            action_type="orchestration",
            risk_class="read_only",
        )
    knowledge_binding = bind_learning_snapshot_from_args(args, project, run_dir)
    written: list[Path] = [knowledge_binding_path(run_dir)]
    completed: list[StageRunSummary] = []
    final_checks: list[dict[str, object]] = []
    recovery_plan: dict[str, object] | None = None
    child_pending_safety_decisions: list[dict[str, object]] = []
    child_blocked_safety_decisions: list[dict[str, object]] = []
    child_budget_stops: list[dict[str, object]] = []
    child_stalls: list[dict[str, object]] = []
    runtime_stages: list[StageWorkItem] = list(stages)
    stage_recovery_counts: dict[str, int] = {}
    stage_recovery_actions: dict[str, list[str]] = {}

    for original in selected:
        slices = [
            stage
            for stage in stages
            if stage.stage_id == original.stage_id
            or stage.stage_id.startswith(original.stage_id + ".")
        ]
        if len(slices) <= 1:
            continue
        record_autonomy_decision(
            run_dir,
            scope=f"stage:{original.stage_id}",
            action="split_stage",
            reason_code="stage_split",
            rationale=(
                f"The planned stage owns more than {args.max_stage_writable_paths} writable paths; "
                "the runtime split it before code generation."
            ),
            evidence_paths=("00-stage-queue.md",),
            metadata={"child_stage_ids": [stage.stage_id for stage in slices]},
        )

    def run_child_agent(
        child_args: argparse.Namespace,
        child_run_dir: Path,
    ) -> tuple[int, list[dict[str, object]], list[dict[str, object]], dict[str, object], dict[str, object]]:
        try:
            exit_code = command_agent(child_args)
        except BudgetExceeded as exc:
            return 1, pending_safety_decisions(child_run_dir), blocked_safety_decisions(child_run_dir), dict(exc.stop), read_stall_state(child_run_dir)
        except ProgressStalled as exc:
            return 1, pending_safety_decisions(child_run_dir), blocked_safety_decisions(child_run_dir), read_budget_stop(child_run_dir), dict(exc.stall)
        except RunnerError as exc:
            child_pending = pending_safety_decisions(child_run_dir)
            child_blocked = blocked_safety_decisions(child_run_dir)
            child_stall = read_stall_state(child_run_dir)
            if child_pending or child_blocked or child_stall:
                return 1, child_pending, child_blocked, read_budget_stop(child_run_dir), child_stall
            failure_summary = child_runner_error_summary(exc)
            failure_type = str(failure_summary["failure_type"])
            error_document = write_run_document(
                child_run_dir,
                "00-runner-error.md",
                "# Child Runner Error\n\n"
                f"- failure_type: `{failure_type}`\n"
                f"- message: {str(exc).strip()}\n",
            )
            partial_manifest: dict[str, object] = {}
            partial_path = child_run_dir / "run.partial.json"
            if partial_path.exists():
                try:
                    parsed_partial = json.loads(partial_path.read_text(encoding="utf-8"))
                    if isinstance(parsed_partial, dict):
                        partial_manifest = parsed_partial
                except json.JSONDecodeError:
                    partial_manifest = {}
            partial_documents = [
                str(path)
                for path in partial_manifest.get("documents", [])
                if isinstance(path, str)
            ]
            write_run_document(
                child_run_dir,
                "run.json",
                json.dumps(
                    {
                        **partial_manifest,
                        "status": "failed",
                        "final_verdict": failure_type,
                        "api_calls": int(partial_manifest.get("api_calls", 0) or 0),
                        "changed_paths": list(partial_manifest.get("changed_paths", []) or []),
                        "required_paths": list(partial_manifest.get("required_paths", []) or []),
                        "documents": unique_ordered([*partial_documents, error_document.name]),
                        "failure_summary": {
                            **failure_summary,
                            "evidence_document": error_document.name,
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            return 1, [], [], {}, {}
        return (
            exit_code,
            pending_safety_decisions(child_run_dir),
            blocked_safety_decisions(child_run_dir),
            read_budget_stop(child_run_dir),
            read_stall_state(child_run_dir),
        )

    def configure_recovery_args(
        child_args: argparse.Namespace,
        source_run_dir: Path,
        decision: StageRecoveryDecision,
    ) -> None:
        child_args.resume = source_run_dir
        source_manifest_path = source_run_dir / "run.json"
        if not source_manifest_path.exists():
            source_manifest_path = source_run_dir / "run.partial.json"
        source_manifest: dict[str, object] = {}
        if source_manifest_path.exists():
            try:
                parsed_source = json.loads(source_manifest_path.read_text(encoding="utf-8"))
                if isinstance(parsed_source, dict):
                    source_manifest = parsed_source
            except json.JSONDecodeError:
                source_manifest = {}
        source_worktree = str(source_manifest.get("worktree_path") or "")
        child_args.resume_worktree = bool(
            args.worktree_mode == "copy"
            and decision.resume_failed_worktree
            and source_worktree
            and Path(source_worktree).is_dir()
        )
        # Resumed child counters are cumulative, so add the fresh bounded
        # allowance to what the failed attempt already consumed.
        child_args.max_rounds = int(source_manifest.get("functional_rounds_used", 0) or 0) + int(
            child_args.max_rounds
        )
        child_args.protocol_repair_rounds = int(
            source_manifest.get("protocol_rounds_used", 0) or 0
        ) + int(child_args.protocol_repair_rounds)
        child_args.adaptive_rounds = int(
            source_manifest.get("adaptive_rounds_used", 0) or 0
        ) + int(child_args.adaptive_rounds)
        child_args.root_cause_patch_rounds = int(
            source_manifest.get("root_cause_patch_rounds_used", 0) or 0
        ) + int(child_args.root_cause_patch_rounds)
        child_args.artifact_plan_repair_rounds = int(
            source_manifest.get("artifact_plan_repair_rounds_used", 0) or 0
        ) + int(child_args.artifact_plan_repair_rounds)
        child_args.skip_pm = True
        recovery_timeout = decision.metadata.get("timeout_seconds")
        if isinstance(recovery_timeout, (int, float)):
            child_args.timeout = recovered_llm_timeout(child_args.timeout, recovery_timeout)
        child_args.small_patch = bool(child_args.small_patch or decision.small_patch)
        if decision.artifact_format:
            child_args.artifact_format = decision.artifact_format
        child_args.brief += (
            "\n\n## Supervisor Recovery Contract\n"
            f"- action: {decision.action}\n"
            f"- reason: {decision.rationale}\n"
            "- ordinary unchanged retry is forbidden\n"
            "- use the resumed executable evidence and produce one smaller falsifiable change"
        )

    queue_doc = stage_queue_document(stages)
    path = write_run_document(run_dir, "00-stage-queue.md", queue_doc)
    written.append(path)

    if args.dry_run:
        manifest = stage_run_manifest(args.brief, stages, completed, "dry_run", args.test_command or [], project)
        manifest["model_profile"] = llm_model_profile_manifest(args)
        manifest["documents"] = [display_path(path, project) for path in written]
        manifest["budget"] = budget_status(run_dir)
        manifest["progress"] = progress_status(run_dir, evaluate=False)
        manifest["knowledge_snapshot"] = knowledge_binding_manifest(knowledge_binding)
        manifest_path = write_run_document(run_dir, "run.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        written.append(manifest_path)
        print(f"run_dir: {run_dir}")
        print("final_status: dry_run")
        for item in written:
            print(f"wrote: {item}")
        return 0

    final_status = "approved"
    prior_changed_paths: list[str] = unique_ordered(
        path
        for path in prior_context_paths
        if (project / path).is_file()
    )
    execution_queue: list[tuple[StageWorkItem, Path | None, StageRecoveryDecision | None]] = [
        (stage, None, None) for stage in stages
    ]
    stage_attempts: dict[str, int] = {}
    while execution_queue:
        stage, resume_stage_dir, recovery_decision = execution_queue.pop(0)
        root_stage_id = stage.stage_id.split(".", 1)[0]
        stage_attempts[stage.stage_id] = stage_attempts.get(stage.stage_id, 0) + 1
        attempt_number = stage_attempts[stage.stage_id]
        try:
            begin_action(
                run_dir,
                f"stage_{stage.stage_id}_attempt_{attempt_number}_start",
                action_type="recovery" if recovery_decision else "stage_start",
                risk_class="read_only",
                metadata=(
                    {
                        "recovery_action": recovery_decision.action,
                        "recovery_reason_code": recovery_decision.reason_code,
                    }
                    if recovery_decision
                    else None
                ),
            )
        except BudgetExceeded:
            final_status = "budget_exhausted"
            break
        except ProgressStalled:
            final_status = "stalled"
            break
        stage_dir_name = f"{stage.stage_id.lower()}-{slugify(stage.title)}"
        if attempt_number > 1:
            stage_dir_name += f"-recovery-{attempt_number - 1:02d}"
        stage_dir = run_dir / stage_dir_name
        inherit_learning_binding(run_dir, stage_dir)
        stage_args = build_stage_agent_args(args, stage, stage_dir, completed, prior_changed_paths)
        stage_args.control_dir = [run_dir]
        if resume_stage_dir is not None and recovery_decision is not None:
            configure_recovery_args(stage_args, resume_stage_dir, recovery_decision)
        print(f"stage: {stage.stage_id} {stage.title} (attempt {attempt_number})")
        exit_code, stage_pending, stage_blocked, stage_budget_stop, stage_stall = run_child_agent(stage_args, stage_dir)
        summary = read_stage_agent_manifest(stage, stage_dir, exit_code, project)
        completed.append(summary)
        if exit_code == 0:
            prior_changed_paths = unique_ordered(
                [*prior_changed_paths, *summary.changed_paths, *summary.required_paths]
            )
        if stage_pending:
            child_pending_safety_decisions.extend(
                {**item, "run_dir": str(stage_dir.resolve()), "stage_id": stage.stage_id}
                for item in stage_pending
            )
        if stage_blocked:
            child_blocked_safety_decisions.extend(
                {**item, "run_dir": str(stage_dir.resolve()), "stage_id": stage.stage_id}
                for item in stage_blocked
            )
        if stage_budget_stop:
            child_budget_stops.append(
                {**stage_budget_stop, "run_dir": str(stage_dir.resolve()), "stage_id": stage.stage_id}
            )
        if stage_stall:
            child_stalls.append(
                {**stage_stall, "run_dir": str(stage_dir.resolve()), "stage_id": stage.stage_id}
            )

        manifest = stage_run_manifest(args.brief, runtime_stages, completed, "running", args.test_command or [], project)
        manifest["model_profile"] = llm_model_profile_manifest(args)
        manifest["documents"] = [display_path(path, project) for path in written]
        manifest["pending_safety_decisions"] = list(child_pending_safety_decisions)
        manifest["blocked_safety_decisions"] = list(child_blocked_safety_decisions)
        manifest["budget"] = budget_status(run_dir)
        manifest["child_budget_stops"] = list(child_budget_stops)
        manifest["progress"] = progress_status(run_dir, evaluate=False)
        manifest["knowledge_snapshot"] = knowledge_binding_manifest(knowledge_binding)
        manifest["child_stalls"] = list(child_stalls)
        write_run_document(run_dir, "run.partial.json", json.dumps(manifest, ensure_ascii=False, indent=2))

        if stage_stall:
            final_status = "stalled"
            break
        if stage_budget_stop:
            final_status = "budget_exhausted"
            break
        if stage_blocked:
            final_status = "safety_blocked"
            break
        if stage_pending:
            final_status = "approval_required"
            break
        if (
            exit_code == 0
            and args.autonomous_recovery
            and stage_recovery_counts.get(root_stage_id, 0) > 0
        ):
            final_status = "approved"
        if exit_code != 0:
            final_status = "stage_failed"
            if recovery_plan is None:
                recovery_plan = stage_failure_recovery_plan(stage, summary, runtime_stages, completed)
                recovery_path = write_run_document(
                    run_dir,
                    "01-stage-recovery-plan.json",
                    json.dumps(recovery_plan, ensure_ascii=False, indent=2),
                )
                written.append(recovery_path)
            if args.autonomous_recovery:
                previous_actions = tuple(stage_recovery_actions.get(root_stage_id, []))
                decision = decide_stage_recovery(
                    stage,
                    summary,
                    recovery_count=stage_recovery_counts.get(root_stage_id, 0),
                    max_recoveries=args.max_stage_recoveries,
                    previous_actions=previous_actions,
                )
                record_autonomy_decision(
                    run_dir,
                    scope=f"stage:{stage.stage_id}",
                    action=decision.action,
                    reason_code=decision.reason_code,
                    rationale=decision.rationale,
                    evidence_paths=(
                        display_path(stage_dir / "run.json", project),
                        display_path(stage_dir / "run.partial.json", project),
                    ),
                    metadata=stage_recovery_decision_manifest(decision),
                )
                if decision.action == "fail_closed":
                    break
                stage_recovery_counts[root_stage_id] = stage_recovery_counts.get(root_stage_id, 0) + 1
                stage_recovery_actions.setdefault(root_stage_id, []).append(decision.action)
                if decision.action == "split_stage":
                    children = split_stage_work_item(stage, parts=2)
                    if len(children) == 1:
                        final_status = "stage_failed"
                        break
                    runtime_stages.extend(children)
                    execution_queue[0:0] = [(child, None, None) for child in children]
                    continue
                if decision.action == "expand_repair_scope":
                    expanded_paths = tuple(
                        unique_ordered(
                            [
                                *(stage.writable_paths or stage.suggested_paths),
                                *decision.additional_writable_paths,
                            ]
                        )
                    )
                    stage = dataclasses.replace(
                        stage,
                        suggested_paths=expanded_paths,
                        writable_paths=expanded_paths,
                    )
                execution_queue.insert(0, (stage, stage_dir, decision))
                continue
            if args.stop_on_failure:
                break

    def run_final_acceptance_checks(*, cycle: int, filename_prefix: str, action_prefix: str) -> bool:
        nonlocal final_status
        final_ok = True
        for index, command in enumerate(args.test_command or [], start=1):
            try:
                doc, ok = run_checked_command(
                    project,
                    command,
                    args.command_timeout,
                    run_dir,
                    action=f"{action_prefix}_command_{index}",
                )
            except BudgetExceeded:
                final_status = "budget_exhausted"
                final_ok = False
                break
            except ProgressStalled:
                final_status = "stalled"
                final_ok = False
                break
            path = write_run_document(run_dir, f"{filename_prefix}-command-{index:02d}.md", doc)
            written.append(path)
            final_checks.append(
                {
                    "kind": "command",
                    "name": f"Final command {index}",
                    "command": command,
                    "cycle": cycle,
                    "status": "pass" if ok else "fail",
                    "document": display_path(path, project),
                }
            )
            final_ok = final_ok and ok
            if blocked_safety_decisions(run_dir):
                final_status = "safety_blocked"
                final_ok = False
                break
            if pending_safety_decisions(run_dir):
                final_status = "approval_required"
                final_ok = False
                break
        if read_budget_stop(run_dir):
            final_status = "budget_exhausted"
            final_ok = False
        if final_status not in {"approval_required", "safety_blocked", "budget_exhausted", "stalled"}:
            final_required_paths = all_stage_required_paths(stages)
            try:
                begin_action(
                    run_dir,
                    f"{action_prefix}_required_path_checks",
                    action_type="harness",
                    risk_class="read_only",
                )
            except BudgetExceeded:
                final_status = "budget_exhausted"
                final_ok = False
            except ProgressStalled:
                final_status = "stalled"
                final_ok = False
        if final_status not in {"approval_required", "safety_blocked", "budget_exhausted", "stalled"}:
            for index, ((doc, ok), required_path) in enumerate(
                zip(run_required_path_checks(project, final_required_paths), final_required_paths),
                start=1,
            ):
                path = write_run_document(run_dir, f"{filename_prefix}-required-path-{index:02d}.md", doc)
                written.append(path)
                final_checks.append(
                    {
                        "kind": "required_path",
                        "name": f"Final required path {index}",
                        "path": required_path,
                        "cycle": cycle,
                        "status": "pass" if ok else "fail",
                        "document": display_path(path, project),
                    }
                )
                final_ok = final_ok and ok
        if not final_ok and final_status not in {"approval_required", "safety_blocked", "budget_exhausted", "stalled"}:
            final_status = "final_check_failed"
        return final_ok

    if final_status == "approved" and args.apply:
        run_final_acceptance_checks(
            cycle=1,
            filename_prefix="99-final",
            action_prefix="final",
        )

    integration_repair: StageRunSummary | None = None
    integration_repair_attempts: list[StageRunSummary] = []
    if final_status == "final_check_failed" and args.apply and args.final_repair_rounds > 0:
        try:
            begin_action(
                run_dir,
                "final_integration_repair",
                action_type="recovery",
                risk_class="read_only",
            )
        except BudgetExceeded:
            final_status = "budget_exhausted"
        except ProgressStalled:
            final_status = "stalled"
        if final_status not in {"budget_exhausted", "stalled"}:
            repair_args = build_integration_repair_args(args, stages, completed, run_dir)
            repair_paths = tuple(unique_ordered(repair_args.include or repair_args.new_file))
            repair_stage = StageWorkItem(
                stage_id="S99",
                title="Final integration repair",
                goal="Repair the smallest root cause behind the final acceptance failure.",
                suggested_paths=repair_paths,
                test_focus=tuple(args.test_command or ["final acceptance command"]),
                writable_paths=repair_paths,
                repair_scope_paths=repair_paths,
            )
            repair_recovery_count = 0
            repair_recovery_actions: list[str] = []
            repair_attempt = 1
            while True:
                inherit_learning_binding(run_dir, repair_args.run_dir)
                repair_args.control_dir = [run_dir]
                print(f"stage: S99 Final integration repair (attempt {repair_attempt})")
                exit_code, repair_pending, repair_blocked, repair_budget_stop, repair_stall = run_child_agent(
                    repair_args,
                    repair_args.run_dir,
                )
                integration_repair = read_stage_agent_manifest(
                    repair_stage, repair_args.run_dir, exit_code, project
                )
                integration_repair_attempts.append(integration_repair)
                if repair_stall:
                    child_stalls.append(
                        {**repair_stall, "run_dir": str(repair_args.run_dir.resolve()), "stage_id": "S99"}
                    )
                    final_status = "stalled"
                    break
                if repair_budget_stop:
                    child_budget_stops.append(
                        {**repair_budget_stop, "run_dir": str(repair_args.run_dir.resolve()), "stage_id": "S99"}
                    )
                    final_status = "budget_exhausted"
                    break
                if repair_blocked:
                    child_blocked_safety_decisions.extend(
                        {**item, "run_dir": str(repair_args.run_dir.resolve()), "stage_id": repair_stage.stage_id}
                        for item in repair_blocked
                    )
                    final_status = "safety_blocked"
                    break
                if repair_pending:
                    child_pending_safety_decisions.extend(
                        {**item, "run_dir": str(repair_args.run_dir.resolve()), "stage_id": repair_stage.stage_id}
                        for item in repair_pending
                    )
                    final_status = "approval_required"
                    break
                if exit_code == 0:
                    final_status = "approved"
                    run_final_acceptance_checks(
                        cycle=2,
                        filename_prefix="99-post-repair",
                        action_prefix="post_repair",
                    )
                    break
                final_status = "final_check_failed"
                if not args.autonomous_recovery:
                    break
                decision = decide_final_integration_recovery(
                    integration_repair,
                    recovery_count=repair_recovery_count,
                    max_recoveries=args.max_stage_recoveries,
                    previous_actions=tuple(repair_recovery_actions),
                )
                record_autonomy_decision(
                    run_dir,
                    scope="stage:S99",
                    action=decision.action,
                    reason_code=decision.reason_code,
                    rationale=decision.rationale,
                    evidence_paths=(
                        display_path(repair_args.run_dir / "run.json", project),
                        display_path(repair_args.run_dir / "run.partial.json", project),
                    ),
                    metadata=stage_recovery_decision_manifest(decision),
                )
                if decision.action == "fail_closed":
                    break
                repair_recovery_count += 1
                repair_recovery_actions.append(decision.action)
                source_repair_dir = repair_args.run_dir
                repair_attempt += 1
                repair_args = build_integration_repair_args(args, stages, completed, run_dir)
                repair_args.run_dir = run_dir / f"s99-final-integration-repair-recovery-{repair_attempt - 1:02d}"
                configure_recovery_args(repair_args, source_repair_dir, decision)

    final_acceptance_criteria = parse_acceptance_criteria(spec)
    latest_check_cycle = max(
        (int(item.get("cycle", 0) or 0) for item in final_checks),
        default=0,
    )
    active_final_checks = [
        item
        for item in final_checks
        if int(item.get("cycle", 0) or 0) == latest_check_cycle
    ]
    final_evidence: list[dict[str, object]] = []
    for index, check in enumerate(active_final_checks, start=1):
        evidence_item: dict[str, object] = {
            "id": f"FE{index:03d}",
            "kind": check.get("kind"),
            "name": check.get("name"),
            "status": check.get("status"),
            "command": check.get("command") or check.get("name"),
            "document": check.get("document"),
        }
        if check.get("kind") == "command":
            evidence_item["covers"] = ["external_test_suite"]
        elif check.get("kind") == "required_path" and check.get("path"):
            evidence_item["covers"] = [f"required_path:{check['path']}"]
        final_evidence.append(evidence_item)
    final_acceptance_matrix = build_acceptance_matrix(
        final_acceptance_criteria,
        final_evidence,
    )
    completion_gate = evaluate_completion_gate(
        final_acceptance_matrix,
        pending_safety=[
            *pending_safety_decisions(run_dir),
            *child_pending_safety_decisions,
        ],
        blocked_safety=[
            *blocked_safety_decisions(run_dir),
            *child_blocked_safety_decisions,
        ],
        budget_stop=read_budget_stop(run_dir) or (child_budget_stops[-1] if child_budget_stops else None),
        stall=read_stall_state(run_dir) or (child_stalls[-1] if child_stalls else None),
    )
    if final_status in {"approved", "final_check_failed"}:
        final_status = str(completion_gate["status"])
        record_autonomy_decision(
            run_dir,
            scope="goal",
            action="complete" if final_status == "approved" else "withhold_completion",
            reason_code=(
                "acceptance_evidence_complete"
                if final_status == "approved"
                else "acceptance_evidence_incomplete"
            ),
            rationale=(
                "Every declared acceptance item has passing executable evidence."
                if final_status == "approved"
                else "At least one declared acceptance item lacks passing executable evidence."
            ),
            evidence_paths=tuple(
                str(item.get("document"))
                for item in active_final_checks
                if item.get("document")
            ),
            metadata={"completion_gate": completion_gate},
        )
    else:
        completion_gate = {
            **completion_gate,
            "status": final_status,
            "completed": False,
            "not_evaluated_reason": "an earlier runtime gate prevented goal completion",
        }

    blocked_state: dict[str, object] | None = None
    if final_status in {"approval_required", "safety_blocked", "budget_exhausted"}:
        if final_status == "budget_exhausted":
            blocked_reason_code = "budget_extension_required"
            blocked_summary = "The immutable autonomous action budget has been exhausted."
            required_input = (
                "Review the budget evidence and explicitly authorize a new run with a larger budget, "
                "or reduce the requested scope."
            )
            blocked_evidence = (display_path(run_dir / "budget_stop.json", project),)
        else:
            blocked_reason_code = "irreversible_high_impact"
            blocked_summary = (
                "The next action is outside the autonomous safety authority."
                if final_status == "approval_required"
                else "The requested action is blocked by the fixed safety policy."
            )
            if final_status == "approval_required":
                pending = [
                    *pending_safety_decisions(run_dir),
                    *child_pending_safety_decisions,
                ]
                decision_id = str(pending[-1].get("decision_id") or "") if pending else ""
                required_input = (
                    f"Approve safety decision {decision_id} through the CLI or Web approval control."
                    if decision_id
                    else "Review and explicitly approve the pending safety decision."
                )
            else:
                required_input = (
                    "Change the requested operation so it no longer requires a blocked action; "
                    "blocked decisions cannot be approved."
                )
            blocked_evidence = (display_path(safety_decisions_file_path(run_dir), project),)
        blocked_state = actionable_blocked_state(
            reason_code=blocked_reason_code,
            summary=blocked_summary,
            evidence_paths=blocked_evidence,
            required_human_input=required_input,
        )
        record_human_decision_request(
            run_dir,
            scope="goal",
            action="resume_after_human_decision",
            reason_code=blocked_reason_code,
            rationale=blocked_summary,
            evidence_paths=blocked_evidence,
            required_human_input=required_input,
        )

    manifest = stage_run_manifest(args.brief, runtime_stages, completed, final_status, args.test_command or [], project, recovery_plan)
    manifest["model_profile"] = llm_model_profile_manifest(args)
    manifest["final_checks"] = final_checks
    manifest["final_evidence"] = final_evidence
    manifest["acceptance_matrix"] = final_acceptance_matrix
    manifest["completion_gate"] = completion_gate
    if blocked_state:
        manifest["blocked_state"] = blocked_state
        manifest["blocked_reason"] = blocked_state["blocked_reason"]
        manifest["supporting_evidence"] = blocked_state["supporting_evidence"]
        manifest["required_human_input"] = blocked_state["required_human_input"]
    if integration_repair:
        manifest["integration_repair"] = {
            "stage_id": integration_repair.stage_id,
            "title": integration_repair.title,
            "status": integration_repair.status,
            "run_dir": integration_repair.run_dir,
            "exit_code": integration_repair.exit_code,
            "api_calls": integration_repair.api_calls,
            "final_verdict": integration_repair.final_verdict,
            "changed_paths": list(integration_repair.changed_paths),
            "required_paths": list(integration_repair.required_paths),
            "failure_summary": integration_repair.failure_summary,
        }
        manifest["integration_repair_attempts"] = [
            {
                "run_dir": item.run_dir,
                "exit_code": item.exit_code,
                "api_calls": item.api_calls,
                "final_verdict": item.final_verdict,
                "failure_summary": item.failure_summary,
            }
            for item in integration_repair_attempts
        ]
        manifest["api_calls"] = int(manifest.get("api_calls", 0)) + sum(
            item.api_calls for item in integration_repair_attempts
        )
    new_regression_memories = regression_memories_from_manifest(manifest)
    if new_regression_memories:
        memory_path = write_run_document(
            run_dir,
            "02-regression-memory.json",
            json.dumps(regression_memory_document(new_regression_memories), ensure_ascii=False, indent=2),
        )
        written.append(memory_path)
        stored_regression_memories = merge_regression_memories(
            stored_regression_memories,
            new_regression_memories,
        )
        store_path = save_regression_memories(project, stored_regression_memories)
        manifest["regression_memory"] = {
            "document": display_path(memory_path, project),
            "record_count": len(new_regression_memories),
            "store": display_path(store_path, project),
            "store_record_count": len(stored_regression_memories),
        }
    manifest["documents"] = [display_path(path, project) for path in written]
    manifest["safety_decisions_log"] = display_path(safety_decisions_file_path(run_dir), project)
    manifest["safety_decision_count"] = len(read_safety_decisions(run_dir))
    manifest["safety_approvals_log"] = display_path(safety_approvals_file_path(run_dir), project)
    manifest["safety_approval_event_count"] = len(read_safety_approvals(run_dir))
    manifest["pending_safety_decisions"] = [
        *pending_safety_decisions(run_dir),
        *child_pending_safety_decisions,
    ]
    manifest["blocked_safety_decisions"] = [
        *blocked_safety_decisions(run_dir),
        *child_blocked_safety_decisions,
    ]
    manifest["budget"] = budget_status(run_dir)
    manifest["child_budget_stops"] = child_budget_stops
    manifest["progress"] = progress_status(run_dir, evaluate=False)
    manifest["child_stalls"] = child_stalls
    manifest["action_gate_audit"] = action_gate_audit(run_dir)
    manifest["autonomy"] = autonomy_audit(run_dir)
    manifest["knowledge_snapshot"] = knowledge_binding_manifest(knowledge_binding)
    manifest_path = write_run_document(run_dir, "run.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    written.append(manifest_path)
    if recovery_source is not None:
        complete_stalled_recovery(
            recovery_source,
            run_dir,
            outcome="completed" if final_status == "approved" else "failed",
            changed_paths=unique_ordered(
                path
                for item in completed
                if item.exit_code == 0
                for path in item.changed_paths
            ),
            change_isolation="isolated" if args.worktree_mode == "copy" else "unisolated",
        )

    print(f"run_dir: {run_dir}")
    print(f"final_status: {final_status}")
    approved_stage_ids = {item.stage_id for item in completed if item.exit_code == 0}
    print(f"approved_stage_runs: {len(approved_stage_ids)}")
    print(f"execution_attempts: {len(completed)}")
    print(f"planned_stages: {len(stages)}")
    print(f"api_calls: {sum(item.api_calls for item in completed)}")
    for path in written:
        print(f"wrote: {path}")
    return 0 if final_status == "approved" else 1
