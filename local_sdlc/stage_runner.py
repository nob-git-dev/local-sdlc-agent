"""Stage-plan and staged execution commands."""

from __future__ import annotations

import argparse
import json
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
from .run_state import *
from .stages import *
from .history import *
from .action_gate import *
from .agent_runner import command_agent


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
    if args.stage_max_rounds < 1:
        raise RunnerError("--stage-max-rounds must be at least 1")
    if args.protocol_repair_rounds < 0:
        raise RunnerError("--protocol-repair-rounds must be zero or greater")

    stored_regression_memories = load_regression_memories(project)
    planned = apply_regression_memories_to_stages(
        synthesize_stage_queue(spec, listed_project_files(project)),
        stored_regression_memories,
    )
    stages = selected_stage_queue(planned, args.from_stage, args.to_stage)
    selected_ids = {stage.stage_id for stage in stages}
    prior_context_paths: list[str] = []
    for stage in planned:
        if stage.stage_id in selected_ids:
            break
        prior_context_paths.extend(stage_required_paths(stage))
    run_dir = make_run_dir(project, args.run_dir)
    begin_action(
        run_dir,
        "run_stages_setup",
        action_type="orchestration",
        risk_class="read_only",
    )
    written: list[Path] = []
    completed: list[StageRunSummary] = []
    final_checks: list[dict[str, object]] = []
    recovery_plan: dict[str, object] | None = None
    child_pending_safety_decisions: list[dict[str, object]] = []
    child_blocked_safety_decisions: list[dict[str, object]] = []

    def run_child_agent(
        child_args: argparse.Namespace,
        child_run_dir: Path,
    ) -> tuple[int, list[dict[str, object]], list[dict[str, object]]]:
        try:
            exit_code = command_agent(child_args)
        except RunnerError:
            child_pending = pending_safety_decisions(child_run_dir)
            child_blocked = blocked_safety_decisions(child_run_dir)
            if not child_pending and not child_blocked:
                raise
            return 1, child_pending, child_blocked
        return exit_code, pending_safety_decisions(child_run_dir), blocked_safety_decisions(child_run_dir)

    queue_doc = stage_queue_document(stages)
    path = write_run_document(run_dir, "00-stage-queue.md", queue_doc)
    written.append(path)

    if args.dry_run:
        manifest = stage_run_manifest(args.brief, stages, completed, "dry_run", args.test_command or [], project)
        manifest["model_profile"] = llm_model_profile_manifest(args)
        manifest["documents"] = [display_path(path, project) for path in written]
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
    for stage in stages:
        begin_action(
            run_dir,
            f"stage_{stage.stage_id}_start",
            action_type="stage_start",
            risk_class="read_only",
        )
        stage_dir = run_dir / f"{stage.stage_id.lower()}-{slugify(stage.title)}"
        stage_args = build_stage_agent_args(args, stage, stage_dir, completed, prior_changed_paths)
        stage_args.control_dir = [run_dir]
        print(f"stage: {stage.stage_id} {stage.title}")
        exit_code, stage_pending, stage_blocked = run_child_agent(stage_args, stage_dir)
        summary = read_stage_agent_manifest(stage, stage_dir, exit_code, project)
        completed.append(summary)
        prior_changed_paths = unique_ordered([*prior_changed_paths, *summary.changed_paths, *summary.required_paths])
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

        manifest = stage_run_manifest(args.brief, stages, completed, "running", args.test_command or [], project)
        manifest["model_profile"] = llm_model_profile_manifest(args)
        manifest["documents"] = [display_path(path, project) for path in written]
        manifest["pending_safety_decisions"] = list(child_pending_safety_decisions)
        manifest["blocked_safety_decisions"] = list(child_blocked_safety_decisions)
        write_run_document(run_dir, "run.partial.json", json.dumps(manifest, ensure_ascii=False, indent=2))

        if stage_blocked:
            final_status = "safety_blocked"
            break
        if stage_pending:
            final_status = "approval_required"
            break
        if exit_code != 0:
            final_status = "stage_failed"
            if recovery_plan is None:
                recovery_plan = stage_failure_recovery_plan(stage, summary, stages, completed)
                recovery_path = write_run_document(
                    run_dir,
                    "01-stage-recovery-plan.json",
                    json.dumps(recovery_plan, ensure_ascii=False, indent=2),
                )
                written.append(recovery_path)
            if args.stop_on_failure:
                break

    if final_status == "approved" and args.apply:
        final_ok = True
        for index, command in enumerate(args.test_command or [], start=1):
            doc, ok = run_checked_command(
                project,
                command,
                args.command_timeout,
                run_dir,
                action=f"final_command_{index}",
            )
            path = write_run_document(run_dir, f"99-final-command-{index:02d}.md", doc)
            written.append(path)
            final_checks.append(
                {
                    "kind": "command",
                    "name": f"Final command {index}",
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
        if final_status not in {"approval_required", "safety_blocked"}:
            final_required_paths = all_stage_required_paths(stages)
            begin_action(
                run_dir,
                "final_required_path_checks",
                action_type="harness",
                risk_class="read_only",
            )
            for index, (doc, ok) in enumerate(run_required_path_checks(project, final_required_paths), start=1):
                path = write_run_document(run_dir, f"99-final-required-path-{index:02d}.md", doc)
                written.append(path)
                final_checks.append(
                    {
                        "kind": "required_path",
                        "name": f"Final required path {index}",
                        "status": "pass" if ok else "fail",
                        "document": display_path(path, project),
                    }
                )
                final_ok = final_ok and ok
        if not final_ok and final_status not in {"approval_required", "safety_blocked"}:
            final_status = "final_check_failed"

    integration_repair: StageRunSummary | None = None
    if final_status == "final_check_failed" and args.apply and args.final_repair_rounds > 0:
        begin_action(
            run_dir,
            "final_integration_repair",
            action_type="stage_start",
            risk_class="read_only",
        )
        repair_stage = StageWorkItem(
            stage_id="S99",
            title="Final integration repair",
            goal="Repair the smallest root cause behind the final acceptance failure.",
            suggested_paths=tuple(all_stage_required_paths(stages)),
            test_focus=tuple(args.test_command or ["final acceptance command"]),
        )
        print("stage: S99 Final integration repair")
        repair_args = build_integration_repair_args(args, stages, completed, run_dir)
        repair_args.control_dir = [run_dir]
        exit_code, repair_pending, repair_blocked = run_child_agent(repair_args, repair_args.run_dir)
        integration_repair = read_stage_agent_manifest(repair_stage, repair_args.run_dir, exit_code, project)
        if repair_blocked:
            child_blocked_safety_decisions.extend(
                {**item, "run_dir": str(repair_args.run_dir.resolve()), "stage_id": repair_stage.stage_id}
                for item in repair_blocked
            )
            final_status = "safety_blocked"
        elif repair_pending:
            child_pending_safety_decisions.extend(
                {**item, "run_dir": str(repair_args.run_dir.resolve()), "stage_id": repair_stage.stage_id}
                for item in repair_pending
            )
            final_status = "approval_required"
        elif exit_code == 0:
            final_status = "approved"

    manifest = stage_run_manifest(args.brief, stages, completed, final_status, args.test_command or [], project, recovery_plan)
    manifest["model_profile"] = llm_model_profile_manifest(args)
    manifest["final_checks"] = final_checks
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
        manifest["api_calls"] = int(manifest.get("api_calls", 0)) + integration_repair.api_calls
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
    manifest["action_gate_audit"] = action_gate_audit(run_dir)
    manifest_path = write_run_document(run_dir, "run.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    written.append(manifest_path)

    print(f"run_dir: {run_dir}")
    print(f"final_status: {final_status}")
    print(f"completed_stages: {len(completed)}/{len(stages)}")
    print(f"api_calls: {sum(item.api_calls for item in completed)}")
    for path in written:
        print(f"wrote: {path}")
    return 0 if final_status == "approved" else 1
