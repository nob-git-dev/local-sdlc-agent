"""Stage planning and acceptance summarization for agentic runs."""

from __future__ import annotations

import argparse
import json
import shlex
import textwrap
from pathlib import Path
from typing import Sequence

from .artifacts import final_failure_focus_from_command_docs, repair_advice_document
from .budget import (
    DEFAULT_MAX_API_CALLS,
    DEFAULT_MAX_GOAL_ACTIONS,
    DEFAULT_MAX_RECOVERY_ACTIONS,
    DEFAULT_MAX_STAGE_ACTIONS,
    DEFAULT_MAX_WALL_SECONDS,
)
from .progress_monitor import DEFAULT_MAX_IDLE_SECONDS
from .evidence import (
    acceptance_blockers as _acceptance_blockers,
    build_acceptance_matrix as _build_acceptance_matrix,
    evidence_covers as _evidence_covers,
    evidence_matches_acceptance_text as _evidence_matches_acceptance_text,
)
from .models import RunnerError, StageRunSummary, StageWorkItem
from .requirements import (
    acceptance_required_covers as _acceptance_required_covers,
    parse_acceptance_criteria as _parse_acceptance_criteria,
)
from .stage_planning import stage_plan_from_spec
from .utils import display_path, unique_ordered
from .workspace import listed_project_files, normalize_new_files, read_text_if_exists, resolve_spec_path


def parse_acceptance_criteria(spec: str) -> list[dict[str, str]]:
    return _parse_acceptance_criteria(spec)

def synthesize_stage_queue(spec: str, project_files: Sequence[str] = ()) -> list[StageWorkItem]:
    contracted = stage_plan_from_spec(spec)
    if contracted is not None:
        return contracted
    text = spec.lower()
    stages: list[StageWorkItem] = []

    def add(title: str, goal: str, paths: Sequence[str], tests: Sequence[str], commands: Sequence[str] = ()) -> None:
        stages.append(
            StageWorkItem(
                stage_id=f"S{len(stages) + 1:02d}",
                title=title,
                goal=goal,
                suggested_paths=tuple(paths),
                test_focus=tuple(tests),
                test_commands=tuple(commands),
            )
        )

    if "sqlite" in text or "sql" in text:
        add("Core errors and result objects", "Define shared exceptions and result container before feature work.", ["minisqlite/errors.py", "minisqlite/result.py"], ["compile shared modules"])
        add("SQL lexer", "Tokenize supported SQL without third-party parsers.", ["minisqlite/sql/lexer.py"], ["lexer unit tests"])
        add("SQL parser and AST", "Parse CREATE TABLE, INSERT, SELECT, and DELETE into AST nodes.", ["minisqlite/sql/ast.py", "minisqlite/sql/parser.py"], ["parser unit tests"])
        add("Record codec", "Encode and decode supported row values.", ["minisqlite/storage/record.py"], ["record codec tests"])
        add("Pager and file header", "Implement page IO, magic header, page size, and corruption checks.", ["minisqlite/storage/pager.py"], ["pager persistence tests"])
        add("B+Tree leaf operations", "Implement single-page insert/search/scan/delete.", ["minisqlite/storage/btree.py"], ["leaf btree tests"])
        add("B+Tree split operations", "Implement multi-leaf split/internal root behavior.", ["minisqlite/storage/btree.py"], ["100+ row split tests"])
        add("Connection CRUD", "Wire parser, schema, storage, INSERT, SELECT, DELETE, and persistence.", ["minisqlite/connection.py", "minisqlite/engine/executor.py", "minisqlite/engine/schema.py"], ["end-to-end API smoke"])
        add("CLI and README", "Expose python -m minisqlite and document supported SQL.", ["minisqlite/__init__.py", "minisqlite/__main__.py", "minisqlite/cli.py", "README.md"], ["CLI smoke", "README required content"])
    elif "redis" in text or "resp" in text:
        add("RESP parser", "Parse arrays, bulk strings, integers, and malformed RESP safely.", ["resp.py"], ["RESP parser tests"])
        add("In-memory store", "Implement string/list/hash state and expiration primitives.", ["store.py"], ["store unit tests"])
        add("Command dispatch", "Implement supported Redis command semantics.", ["commands.py"], ["command unit tests"])
        add("TCP server loop", "Wire socket handling, pipelining, and error responses.", ["server.py"], ["server smoke tests"])
        add("Docs and process notes", "Document usage, protocol support, and known limits.", ["README.md", "PROCESS.md"], ["docs exist"])
    elif "html" in text or "javascript" in text or "browser" in text:
        add("Single-page app shell", "Create the static HTML/CSS/JS shell.", ["index.html"], ["html smoke"])
        add("Core interaction logic", "Implement state transitions and input handling.", ["index.html"], ["behavior smoke"])
        add("Polish and accessibility", "Add responsive layout and visible user feedback.", ["index.html"], ["visual/manual checklist"])
    else:
        add("Project skeleton", "Create the smallest runnable structure and shared errors/results.", ["README.md"], ["compile or smoke"])
        add("Core behavior", "Implement the first verifiable acceptance criterion.", list(project_files[:3]) or ["src/main.py"], ["focused unit test"])
        add("Integration and documentation", "Wire CLI/API/docs and run end-to-end checks.", ["README.md"], ["end-to-end smoke"])

    return stages


def stage_required_observables(stage: StageWorkItem) -> tuple[str, ...]:
    observables: list[str] = list(stage.required_observables)
    observables.extend(f"focus:{item}" for item in stage.test_focus)
    observables.extend(f"command:{command}" for command in auto_stage_test_commands(stage))
    observables.extend(f"required_path:{path}" for path in stage_required_paths(stage))
    return tuple(unique_ordered(observables))


def stage_writable_paths(stage: StageWorkItem) -> tuple[str, ...]:
    if stage.writable_paths:
        return stage.writable_paths
    return stage_required_paths(stage)


def stage_readonly_evidence_paths(stage: StageWorkItem) -> tuple[str, ...]:
    return stage.readonly_evidence_paths


def stage_work_item_manifest(stage: StageWorkItem) -> dict[str, object]:
    return {
        "stage_id": stage.stage_id,
        "title": stage.title,
        "goal": stage.goal,
        "suggested_paths": list(stage.suggested_paths),
        "test_focus": list(stage.test_focus),
        "test_paths": list(stage_test_paths(stage)),
        "test_commands": auto_stage_test_commands(stage),
        "required_observables": list(stage_required_observables(stage)),
        "writable_paths": list(stage_writable_paths(stage)),
        "readonly_evidence_paths": list(stage_readonly_evidence_paths(stage)),
        "api_profile": list(stage.api_profile),
        "max_rounds": stage.max_rounds,
    }


def stage_queue_document(stages: Sequence[StageWorkItem]) -> str:
    lines = ["# Stage Queue", ""]
    for stage in stages:
        test_paths = stage_test_paths(stage)
        manifest = stage_work_item_manifest(stage)
        lines.extend(
            [
                f"## {stage.stage_id}: {stage.title}",
                "",
                f"- goal: {stage.goal}",
                "- suggested_paths:",
                *[f"  - {path}" for path in stage.suggested_paths],
                "- test_focus:",
                *[f"  - {item}" for item in stage.test_focus],
                "- test_paths:",
                *[f"  - {path}" for path in test_paths],
                "- test_commands:",
                *[f"  - {command}" for command in auto_stage_test_commands(stage)],
                "- required_observables:",
                *[f"  - {item}" for item in manifest["required_observables"]],
                "- writable_paths:",
                *[f"  - {path}" for path in manifest["writable_paths"]],
                "- readonly_evidence_paths:",
                *[f"  - {path}" for path in manifest["readonly_evidence_paths"]],
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"

def selected_stage_queue(
    stages: Sequence[StageWorkItem],
    from_stage: str | None = None,
    to_stage: str | None = None,
) -> list[StageWorkItem]:
    if not stages:
        return []
    stage_ids = [stage.stage_id for stage in stages]
    start = 0
    end = len(stages)
    if from_stage:
        normalized = from_stage.upper()
        if normalized not in stage_ids:
            raise RunnerError(f"--from-stage not found in stage queue: {from_stage}")
        start = stage_ids.index(normalized)
    if to_stage:
        normalized = to_stage.upper()
        if normalized not in stage_ids:
            raise RunnerError(f"--to-stage not found in stage queue: {to_stage}")
        end = stage_ids.index(normalized) + 1
    if start >= end:
        raise RunnerError("--from-stage must be before or equal to --to-stage")
    return list(stages[start:end])

def stage_brief(base_brief: str, stage: StageWorkItem, completed: Sequence[StageRunSummary]) -> str:
    completed_lines = [
        f"- {item.stage_id} {item.title}: {item.final_verdict or item.status}"
        for item in completed
    ]
    completed_text = "\n".join(completed_lines) if completed_lines else "- none"
    paths = "\n".join(f"- {path}" for path in stage.suggested_paths) or "- none"
    test_paths = "\n".join(f"- {path}" for path in stage_test_paths(stage)) or "- none"
    focus = "\n".join(f"- {item}" for item in stage.test_focus) or "- command evidence"
    required_observables = "\n".join(f"- {item}" for item in stage_required_observables(stage)) or "- none"
    writable_paths = "\n".join(f"- {path}" for path in stage_writable_paths(stage)) or "- none"
    readonly_evidence_paths = "\n".join(f"- {path}" for path in stage_readonly_evidence_paths(stage)) or "- none"
    return textwrap.dedent(
        f"""
        {base_brief}

        ## Current Stage
        - id: {stage.stage_id}
        - title: {stage.title}
        - goal: {stage.goal}

        ## Suggested Writable Paths
        {paths}

        ## Writable Paths
        {writable_paths}

        ## Readonly Evidence Paths
        {readonly_evidence_paths}

        ## Required Stage Test Paths
        {test_paths}

        ## Stage Test Focus
        {focus}

        ## Required Observables
        {required_observables}

        ## Completed Earlier Stages
        {completed_text}

        Work only on this stage's goal. Preserve completed stage behavior.
        If this stage cannot be completed because an earlier stage is broken,
        fix the smallest shared root cause needed for this stage and report it
        through artifacts and executable evidence.
        """
    ).strip()

def stage_test_paths(stage: StageWorkItem) -> tuple[str, ...]:
    if (
        stage.writable_paths
        or stage.readonly_evidence_paths
        or stage.test_commands
        or stage.required_observables
    ):
        return tuple(
            path
            for path in stage.suggested_paths
            if path.startswith("tests/")
        )
    title = stage.title.lower()
    if "core errors" in title:
        return ("tests/test_core.py",)
    if "sql lexer" in title:
        return ("tests/test_lexer.py",)
    if "sql parser" in title or "ast" in title:
        return ("tests/test_parser.py",)
    if "record codec" in title:
        return ("tests/test_record.py",)
    if "pager" in title:
        return ("tests/test_pager.py",)
    if "b+tree" in title:
        return ("tests/test_btree.py",)
    if "connection crud" in title:
        return ("tests/test_connection.py",)
    if "cli" in title:
        return ("tests/test_cli.py",)
    if "resp parser" in title:
        return ("tests/test_resp.py",)
    if "in-memory store" in title:
        return ("tests/test_store.py",)
    if "command dispatch" in title:
        return ("tests/test_commands.py",)
    if "tcp server" in title:
        return ("tests/test_server.py",)
    return ()

def stage_required_paths(stage: StageWorkItem) -> tuple[str, ...]:
    return tuple(unique_ordered([*stage.suggested_paths, *stage_test_paths(stage)]))

def all_stage_required_paths(stages: Sequence[StageWorkItem]) -> list[str]:
    return unique_ordered(path for stage in stages for path in stage_required_paths(stage))

def stage_paths_for_agent(
    project: Path,
    stage: StageWorkItem,
    prior_changed_paths: Sequence[str],
) -> tuple[list[str], list[str], list[str], list[str]]:
    existing_paths = set(listed_project_files(project))
    safe_stage_paths = normalize_new_files(stage_writable_paths(stage))
    include_paths: list[str] = []
    new_files: list[str] = []
    for path in safe_stage_paths:
        if path in existing_paths:
            include_paths.append(path)
        else:
            new_files.append(path)
    required_paths = unique_ordered(stage_required_paths(stage))
    context_paths = [
        path
        for path in unique_ordered([*prior_changed_paths, *stage_readonly_evidence_paths(stage)])
        if path in existing_paths and path not in include_paths
    ]
    return include_paths, new_files, required_paths, context_paths

def auto_stage_test_commands(stage: StageWorkItem) -> list[str]:
    if stage.test_commands:
        return list(stage.test_commands)
    python_paths = [path for path in stage_required_paths(stage) if path.endswith(".py")]
    if not python_paths:
        return []
    commands = [f"python3 -m py_compile {' '.join(shlex.quote(path) for path in python_paths)}"]
    for test_path in stage_test_paths(stage):
        commands.append(f"python3 -m unittest discover -s tests -p {shlex.quote(Path(test_path).name)}")
    return commands

def stage_test_commands_for_agent(args: argparse.Namespace, stage: StageWorkItem) -> list[str]:
    explicit = list(getattr(args, "stage_test_command", []) or [])
    if explicit:
        return explicit
    if getattr(args, "no_auto_stage_test", False):
        return []
    return auto_stage_test_commands(stage)

def build_stage_agent_args(
    args: argparse.Namespace,
    stage: StageWorkItem,
    run_dir: Path,
    completed: Sequence[StageRunSummary],
    prior_changed_paths: Sequence[str],
) -> argparse.Namespace:
    project = args.project.resolve()
    spec_file = resolve_spec_path(project, args.spec_file)
    include_paths, new_files, required_paths, context_paths = stage_paths_for_agent(project, stage, prior_changed_paths)
    if not include_paths and not new_files and not args.allow_no_context:
        # A synthesized stage should always have paths, but fallback safely.
        new_files = ["README.md"]
        required_paths = ["README.md"]
    return argparse.Namespace(
        command="agent",
        brief=stage_brief(args.brief, stage, completed),
        project=project,
        config_file=getattr(args, "config_file", None),
        skills_dir=args.skills_dir,
        base_url=args.base_url,
        api_key=args.api_key,
        model=args.model,
        model_profile=getattr(args, "model_profile", "default"),
        timeout=args.timeout,
        health_timeout=args.health_timeout,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        enable_thinking=args.enable_thinking,
        stream=args.stream,
        api_profile=list(unique_ordered([*list(getattr(args, "api_profile", []) or []), *stage.api_profile])),
        pm_max_tokens=args.pm_max_tokens,
        coder_max_tokens=args.coder_max_tokens,
        judge_max_tokens=args.judge_max_tokens,
        pm_temperature=args.pm_temperature,
        coder_temperature=args.coder_temperature,
        judge_temperature=args.judge_temperature,
        pm_thinking=args.pm_thinking,
        coder_thinking=args.coder_thinking,
        judge_thinking=args.judge_thinking,
        pm_skill=args.pm_skill,
        coder_skill=args.coder_skill,
        judge_skill=args.judge_skill,
        domain_skill=args.domain_skill,
        domain_modeling=args.domain_modeling,
        judge_mode=args.judge_mode,
        skip_pm=args.skip_stage_pm,
        spec_file=spec_file,
        include=include_paths,
        context=context_paths,
        context_slice=[],
        new_file=new_files,
        require_path=required_paths,
        max_context_chars=args.max_context_chars,
        document_window=args.document_window,
        allow_no_context=args.allow_no_context,
        small_patch=args.small_patch,
        no_replace_file=args.no_replace_file,
        no_extra_files=args.no_extra_files,
        apply=args.apply,
        precheck=args.precheck,
        test_command=stage_test_commands_for_agent(args, stage),
        command_timeout=args.command_timeout,
        redis_smoke=args.redis_smoke,
        artifact_format=args.artifact_format,
        resume=None,
        resume_worktree=False,
        resume_worktree_path=None,
        worktree_mode=args.worktree_mode,
        max_rounds=stage.max_rounds or args.stage_max_rounds,
        protocol_repair_rounds=args.protocol_repair_rounds,
        adaptive_rounds=args.adaptive_rounds,
        root_cause_patch_rounds=args.root_cause_patch_rounds,
        max_goal_actions=getattr(args, "max_goal_actions", DEFAULT_MAX_GOAL_ACTIONS),
        max_stage_actions=getattr(args, "max_stage_actions", DEFAULT_MAX_STAGE_ACTIONS),
        max_recovery_actions=getattr(args, "max_recovery_actions", DEFAULT_MAX_RECOVERY_ACTIONS),
        max_api_calls=getattr(args, "max_api_calls", DEFAULT_MAX_API_CALLS),
        max_wall_seconds=getattr(args, "max_wall_seconds", DEFAULT_MAX_WALL_SECONDS),
        max_idle_seconds=getattr(args, "max_idle_seconds", DEFAULT_MAX_IDLE_SECONDS),
        run_dir=run_dir,
    )

def read_stage_agent_manifest(stage: StageWorkItem, run_dir: Path, exit_code: int, base: Path) -> StageRunSummary:
    manifest_path = run_dir / "run.json"
    if not manifest_path.exists():
        manifest_path = run_dir / "run.partial.json"
    manifest: dict[str, object] = {}
    if manifest_path.exists():
        try:
            parsed = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                manifest = parsed
        except json.JSONDecodeError:
            manifest = {}
    status = "approved" if exit_code == 0 else "failed"
    final_verdict = str(manifest.get("final_verdict") or status)
    changed_paths = tuple(str(path) for path in manifest.get("changed_paths", []) if isinstance(path, str))
    required_paths = tuple(str(path) for path in manifest.get("required_paths", []) if isinstance(path, str))
    api_calls_raw = manifest.get("api_calls", 0)
    try:
        api_calls = int(api_calls_raw)
    except (TypeError, ValueError):
        api_calls = 0
    failure = manifest.get("failure_summary")
    return StageRunSummary(
        stage_id=stage.stage_id,
        title=stage.title,
        status=status,
        run_dir=display_path(run_dir, base),
        exit_code=exit_code,
        api_calls=api_calls,
        final_verdict=final_verdict,
        changed_paths=changed_paths,
        required_paths=required_paths,
        failure_summary=failure if isinstance(failure, dict) else None,
    )

def integration_repair_brief(base_brief: str, completed: Sequence[StageRunSummary]) -> str:
    stage_lines = "\n".join(
        f"- {item.stage_id} {item.title}: {item.final_verdict or item.status}"
        for item in completed
    )
    return textwrap.dedent(
        f"""
        {base_brief}

        ## Final Integration Repair

        All planned implementation stages have completed, but the final
        executable acceptance gate failed. Use the resumed final command
        document as the primary evidence.

        Completed stages:
        {stage_lines or "- none"}

        Repair the smallest shared root cause that makes the final configured
        command pass. Do not weaken tests. Do not rewrite unrelated files.
        """
    ).strip()

def build_integration_repair_args(
    args: argparse.Namespace,
    stages: Sequence[StageWorkItem],
    completed: Sequence[StageRunSummary],
    run_dir: Path,
) -> argparse.Namespace:
    project = args.project.resolve()
    spec_file = resolve_spec_path(project, args.spec_file)
    required_paths = all_stage_required_paths(stages)
    existing_paths = set(listed_project_files(project))
    final_command_docs: list[tuple[str, str]] = []
    for path in sorted(run_dir.glob("99-final-command-*.md")):
        final_command_docs.append((path.name, read_text_if_exists(path)))
    advice = final_failure_focus_from_command_docs(final_command_docs, args.test_command or []) if final_command_docs else None
    product_focus = [
        path
        for path in (advice.focus_files if advice else ())
        if path in existing_paths and not path.startswith("tests/") and path.endswith((".py", ".md"))
    ]
    if product_focus:
        include_paths = product_focus
    else:
        include_paths = [
            path
            for path in required_paths
            if path in existing_paths and not path.startswith("tests/") and path.endswith((".py", ".md"))
        ]
    context_paths = unique_ordered(
        [
            path
            for path in required_paths
            if path.startswith("tests/") and path in existing_paths
        ]
        + [
            path
            for path in (advice.focus_files if advice else ())
            if path.startswith("tests/") and path in existing_paths
        ]
    )
    new_files: list[str] = []
    brief = integration_repair_brief(args.brief, completed)
    if advice:
        brief = brief + "\n\n" + repair_advice_document(advice)
    return argparse.Namespace(
        command="agent",
        brief=brief,
        project=project,
        config_file=getattr(args, "config_file", None),
        skills_dir=args.skills_dir,
        base_url=args.base_url,
        api_key=args.api_key,
        model=args.model,
        model_profile=getattr(args, "model_profile", "default"),
        timeout=args.timeout,
        health_timeout=args.health_timeout,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        enable_thinking=args.enable_thinking,
        stream=args.stream,
        api_profile=list(getattr(args, "api_profile", []) or []),
        pm_max_tokens=args.pm_max_tokens,
        coder_max_tokens=args.coder_max_tokens,
        judge_max_tokens=args.judge_max_tokens,
        pm_temperature=args.pm_temperature,
        coder_temperature=args.coder_temperature,
        judge_temperature=args.judge_temperature,
        pm_thinking=args.pm_thinking,
        coder_thinking=args.coder_thinking,
        judge_thinking=args.judge_thinking,
        pm_skill=args.pm_skill,
        coder_skill=args.coder_skill,
        judge_skill=args.judge_skill,
        domain_skill=args.domain_skill,
        domain_modeling=args.domain_modeling,
        judge_mode=args.judge_mode,
        skip_pm=args.skip_stage_pm,
        spec_file=spec_file,
        include=include_paths,
        context=context_paths,
        context_slice=[],
        new_file=new_files,
        require_path=required_paths,
        max_context_chars=args.max_context_chars,
        document_window=max(args.document_window, 24),
        allow_no_context=True,
        small_patch=True,
        no_replace_file=False,
        no_extra_files=args.no_extra_files,
        apply=args.apply,
        precheck=False,
        test_command=list(args.test_command or []),
        command_timeout=args.command_timeout,
        redis_smoke=args.redis_smoke,
        artifact_format=args.artifact_format,
        resume=run_dir,
        resume_worktree=False,
        resume_worktree_path=None,
        worktree_mode=args.worktree_mode,
        max_rounds=args.final_repair_rounds,
        protocol_repair_rounds=args.protocol_repair_rounds,
        adaptive_rounds=args.adaptive_rounds,
        root_cause_patch_rounds=args.root_cause_patch_rounds,
        max_goal_actions=getattr(args, "max_goal_actions", DEFAULT_MAX_GOAL_ACTIONS),
        max_stage_actions=getattr(args, "max_stage_actions", DEFAULT_MAX_STAGE_ACTIONS),
        max_recovery_actions=getattr(args, "max_recovery_actions", DEFAULT_MAX_RECOVERY_ACTIONS),
        max_api_calls=getattr(args, "max_api_calls", DEFAULT_MAX_API_CALLS),
        max_wall_seconds=getattr(args, "max_wall_seconds", DEFAULT_MAX_WALL_SECONDS),
        max_idle_seconds=getattr(args, "max_idle_seconds", DEFAULT_MAX_IDLE_SECONDS),
        run_dir=run_dir / "s99-final-integration-repair",
    )

def stage_failure_recovery_plan(
    failed_stage: StageWorkItem,
    failed_summary: StageRunSummary,
    stages: Sequence[StageWorkItem],
    completed: Sequence[StageRunSummary],
) -> dict[str, object]:
    stage_ids = [stage.stage_id for stage in stages]
    try:
        failed_index = stage_ids.index(failed_stage.stage_id)
    except ValueError:
        failed_index = 0
    remaining_stages = list(stages[failed_index:])
    failure_summary_data = failed_summary.failure_summary or {}
    failure_type = str(failure_summary_data.get("failure_type") or "unknown")
    return {
        "status": "stage_failed",
        "failed_stage_id": failed_stage.stage_id,
        "failed_stage_title": failed_stage.title,
        "failed_stage_run_dir": failed_summary.run_dir,
        "failure_type": failure_type,
        "failure_summary": failure_summary_data,
        "completed_ok_stage_ids": [item.stage_id for item in completed if item.exit_code == 0],
        "remaining_stage_ids": [stage.stage_id for stage in remaining_stages],
        "recommended_resume": {
            "command": "run-stages",
            "from_stage": failed_stage.stage_id,
            "to_stage": remaining_stages[-1].stage_id if remaining_stages else failed_stage.stage_id,
            "reason": "the failed stage has not proven its required observables",
        },
        "next_required_action": {
            "kind": "repair_failed_stage",
            "stage_id": failed_stage.stage_id,
            "required_observables": list(stage_required_observables(failed_stage)),
            "writable_paths": list(stage_writable_paths(failed_stage)),
            "readonly_evidence_paths": list(stage_readonly_evidence_paths(failed_stage)),
            "required_paths": list(stage_required_paths(failed_stage)),
            "test_commands": auto_stage_test_commands(failed_stage),
        },
        "retry_policy": {
            "do_not_skip_failed_stage": True,
            "retry_same_stage_after": [
                "repair or regenerate artifacts inside writable_paths",
                "produce passing evidence for every required_observable",
            ],
        },
    }

def stage_run_manifest(
    brief: str,
    stages: Sequence[StageWorkItem],
    completed: Sequence[StageRunSummary],
    final_status: str,
    test_commands: Sequence[str],
    base: Path,
    recovery_plan: dict[str, object] | None = None,
) -> dict[str, object]:
    manifest = {
        "brief": brief,
        "command": "run-stages",
        "status": final_status,
        "final_verdict": final_status,
        "stage_count": len(stages),
        "completed_stage_count": len(
            {item.stage_id for item in completed if item.exit_code == 0}
        ),
        "execution_attempt_count": len(completed),
        "api_calls": sum(item.api_calls for item in completed),
        "final_test_commands": list(test_commands),
        "stages": [stage_work_item_manifest(stage) for stage in stages],
        "completed_stages": [
            {
                "stage_id": item.stage_id,
                "title": item.title,
                "status": item.status,
                "run_dir": item.run_dir,
                "exit_code": item.exit_code,
                "api_calls": item.api_calls,
                "final_verdict": item.final_verdict,
                "changed_paths": list(item.changed_paths),
                "required_paths": list(item.required_paths),
                "failure_summary": item.failure_summary,
            }
            for item in completed
        ],
        "documents": [],
    }
    if recovery_plan is not None:
        manifest["stage_recovery_plan"] = recovery_plan
    return manifest

def build_acceptance_matrix(
    criteria: Sequence[dict[str, str]],
    evidence: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    return _build_acceptance_matrix(criteria, evidence)


def evidence_covers(evidence_item: dict[str, object]) -> list[str]:
    return _evidence_covers(evidence_item)


def evidence_matches_acceptance_text(evidence_item: dict[str, object], text: str) -> bool:
    return _evidence_matches_acceptance_text(evidence_item, text)


def acceptance_required_covers(text: str) -> list[str]:
    return _acceptance_required_covers(text)


def acceptance_blockers(matrix: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    return _acceptance_blockers(matrix)

def failure_summary(final_verdict: str, evidence: Sequence[dict[str, object]], fallback: str | None = None) -> dict[str, object] | None:
    if final_verdict == "approved":
        return None
    for item in reversed(evidence):
        if item.get("status") == "fail":
            return {
                "failure_type": item.get("failure_type") or fallback or "unknown",
                "evidence_id": item.get("id"),
                "name": item.get("name"),
                "document": item.get("document"),
            }
    if fallback:
        return {"failure_type": fallback}
    return {"failure_type": "unknown"}
