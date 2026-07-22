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
from .run_state import *
from .stages import *
from .agent_runner import command_agent


def command_stage_plan(args: argparse.Namespace) -> int:
    project = args.project.resolve()
    spec_path = resolve_spec_path(project, args.spec_file)
    spec = read_text_if_exists(spec_path)
    if not spec:
        raise RunnerError("SPEC.md is required before planning stages; pass --spec-file or create SPEC.md")
    stages = synthesize_stage_queue(spec, listed_project_files(project))
    if args.format == "json":
        payload = [
            {
                "stage_id": stage.stage_id,
                "title": stage.title,
                "goal": stage.goal,
                "suggested_paths": list(stage.suggested_paths),
                "test_focus": list(stage.test_focus),
                "test_commands": auto_stage_test_commands(stage),
            }
            for stage in stages
        ]
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

    planned = synthesize_stage_queue(spec, listed_project_files(project))
    stages = selected_stage_queue(planned, args.from_stage, args.to_stage)
    selected_ids = {stage.stage_id for stage in stages}
    prior_context_paths: list[str] = []
    for stage in planned:
        if stage.stage_id in selected_ids:
            break
        prior_context_paths.extend(stage_required_paths(stage))
    run_dir = make_run_dir(project, args.run_dir)
    written: list[Path] = []
    completed: list[StageRunSummary] = []
    final_checks: list[dict[str, object]] = []

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
        stage_dir = run_dir / f"{stage.stage_id.lower()}-{slugify(stage.title)}"
        stage_args = build_stage_agent_args(args, stage, stage_dir, completed, prior_changed_paths)
        print(f"stage: {stage.stage_id} {stage.title}")
        exit_code = command_agent(stage_args)
        summary = read_stage_agent_manifest(stage, stage_dir, exit_code, project)
        completed.append(summary)
        prior_changed_paths = unique_ordered([*prior_changed_paths, *summary.changed_paths, *summary.required_paths])

        manifest = stage_run_manifest(args.brief, stages, completed, "running", args.test_command or [], project)
        manifest["model_profile"] = llm_model_profile_manifest(args)
        manifest["documents"] = [display_path(path, project) for path in written]
        write_run_document(run_dir, "run.partial.json", json.dumps(manifest, ensure_ascii=False, indent=2))

        if exit_code != 0:
            final_status = "stage_failed"
            if args.stop_on_failure:
                break

    if final_status == "approved" and args.apply:
        final_ok = True
        for index, command in enumerate(args.test_command or [], start=1):
            doc, ok = run_checked_command(project, command, args.command_timeout)
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
        final_required_paths = all_stage_required_paths(stages)
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
        if not final_ok:
            final_status = "final_check_failed"

    integration_repair: StageRunSummary | None = None
    if final_status == "final_check_failed" and args.apply and args.final_repair_rounds > 0:
        repair_stage = StageWorkItem(
            stage_id="S99",
            title="Final integration repair",
            goal="Repair the smallest root cause behind the final acceptance failure.",
            suggested_paths=tuple(all_stage_required_paths(stages)),
            test_focus=tuple(args.test_command or ["final acceptance command"]),
        )
        print("stage: S99 Final integration repair")
        repair_args = build_integration_repair_args(args, stages, completed, run_dir)
        exit_code = command_agent(repair_args)
        integration_repair = read_stage_agent_manifest(repair_stage, repair_args.run_dir, exit_code, project)
        if exit_code == 0:
            final_status = "approved"

    manifest = stage_run_manifest(args.brief, stages, completed, final_status, args.test_command or [], project)
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
    manifest["documents"] = [display_path(path, project) for path in written]
    manifest_path = write_run_document(run_dir, "run.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    written.append(manifest_path)

    print(f"run_dir: {run_dir}")
    print(f"final_status: {final_status}")
    print(f"completed_stages: {len(completed)}/{len(stages)}")
    print(f"api_calls: {sum(item.api_calls for item in completed)}")
    for path in written:
        print(f"wrote: {path}")
    return 0 if final_status == "approved" else 1
