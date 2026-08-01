#!/usr/bin/env python3
"""CLI entrypoint wiring for the local SDLC runner.

The public command remains ``local_sdlc.py``. Implementation lives in
responsibility-focused modules so the same application/domain/infrastructure
logic can be reused from future GUI or API surfaces.
"""

from __future__ import annotations

import argparse
from collections import Counter
import dataclasses
import datetime as _datetime
import inspect
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Sequence

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
from .requirements import *
from .evidence import *
from .history import *
from .stages import *
from . import agent_runner as _agent_runner
from . import phase_runner as _phase_runner
from . import stage_runner as _stage_runner
from . import supervisor_runner as _supervisor_runner
from . import web_server as _web_server


def command_doctor(args: argparse.Namespace) -> int:
    project = args.project.resolve()
    skills = load_skills(args.skills_dir)
    spec_path = project / "SPEC.md"
    config = build_config(args)
    client = LocalLLMClient(config)

    print(f"project: {project}")
    print(f"skills_dir: {args.skills_dir}")
    print(f"skills: {len(skills)} ({', '.join(sorted(skills)[:8])}{'...' if len(skills) > 8 else ''})")
    print(f"SPEC.md: {'present' if spec_path.exists() else 'missing'}")

    git = subprocess.run(
        ["git", "status", "--short"],
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
    )
    if git.returncode == 0:
        dirty = "dirty" if git.stdout.strip() else "clean"
        print(f"git: {dirty}")
    else:
        print("git: unavailable")

    print(f"llm_base_url: {config.base_url}")
    print(f"llm_config_file: {config.config_file or '(none)'}")
    print(f"llm_configured_model: {config.model or '(auto)'}")
    print(f"llm_model_profile: {config.model_profile}")
    for role in ("pm", "coder", "judge"):
        settings = client.call_settings(role)
        thinking = "off" if settings.disable_thinking else "on"
        print(
            f"llm_role.{role}: "
            f"model={settings.model or '(auto)'} "
            f"temperature={settings.temperature:g} "
            f"max_tokens={settings.max_tokens} "
            f"thinking={thinking}"
        )
    for function_name in sorted(DEFAULT_FUNCTION_PROFILES):
        settings = client.call_settings("default", function_name)
        thinking = "off" if settings.disable_thinking else "on"
        print(
            f"llm_function.{function_name}: "
            f"model={settings.model or '(auto)'} "
            f"temperature={settings.temperature:g} "
            f"max_tokens={settings.max_tokens} "
            f"thinking={thinking}"
        )
    profile_manifest = llm_model_profile_manifest(args)
    if profile_manifest["function_overrides"]:
        print("llm_model_profile_overrides:")
        for name, override in profile_manifest["function_overrides"].items():
            print(
                f"  {name}: "
                f"model={override['model'] or '(inherit)'} "
                f"temperature={override['temperature']} "
                f"max_tokens={override['max_tokens']} "
                f"thinking={override['thinking']}"
            )

    if args.skip_llm:
        print("llm: skipped")
        return 0

    models = client.models()
    print(f"llm_models: {', '.join(models) if models else '(none)'}")
    selected_model = config.model or (models[0] if models else '(none)')
    print(f"llm_selected_model: {selected_model}")
    if config.model and config.model not in models and models:
        print(f"warning: configured model '{config.model}' was not listed by /v1/models")
    for recommendation in llm_role_recommendations(selected_model):
        print(f"llm_recommendation: {recommendation}")
    if args.skip_probes:
        print("llm_probes: skipped")
        return 0
    if selected_model == "(none)":
        print("llm_probes: skipped (no selected model)")
        return 1
    probes = run_llm_capability_probes(client, selected_model, args.probe_timeout)
    failed = False
    for probe in probes:
        print(f"llm_probe.{probe.name}: {probe.status} - {probe.detail}")
        if probe.status == "FAIL":
            failed = True
    return 1 if failed else 0


def command_health(args: argparse.Namespace) -> int:
    config = build_config(args)
    client = LocalLLMClient(config)
    print(f"llm_base_url: {config.base_url}")
    print(f"health_timeout: {config.health_timeout:g}s")
    status = client.health_probe()
    print(f"llm_health: {status}")
    return 0 if status.startswith("alive") else 1


def command_web(args: argparse.Namespace) -> int:
    return _web_server.command_web(args)


def command_list_skills(args: argparse.Namespace) -> int:
    skills = load_skills(args.skills_dir)
    for name in sorted(skills):
        description = skills[name].description
        print(f"{name}\t{description}")
    return 0


def _sync_runner_dependencies() -> None:
    _agent_runner.LocalLLMClient = LocalLLMClient
    _phase_runner.LocalLLMClient = LocalLLMClient
    _stage_runner.LocalLLMClient = LocalLLMClient
    _stage_runner.command_agent = command_agent
    _supervisor_runner.LocalLLMClient = LocalLLMClient


def command_stage_plan(args: argparse.Namespace) -> int:
    _sync_runner_dependencies()
    return _stage_runner.command_stage_plan(args)


def command_run_stages(args: argparse.Namespace) -> int:
    _sync_runner_dependencies()
    return _stage_runner.command_run_stages(args)


def command_spec(args: argparse.Namespace) -> int:
    _sync_runner_dependencies()
    return _phase_runner.command_spec(args)


def command_phase(args: argparse.Namespace) -> int:
    _sync_runner_dependencies()
    return _phase_runner.command_phase(args)


def append_to_spec(spec_path: Path, skill_name: str, output: str) -> None:
    return _phase_runner.append_to_spec(spec_path, skill_name, output)


def command_implement(args: argparse.Namespace) -> int:
    _sync_runner_dependencies()
    return _phase_runner.command_implement(args)


def command_supervisor(args: argparse.Namespace) -> int:
    _sync_runner_dependencies()
    return _supervisor_runner.command_supervisor(args)


def command_supervise(args: argparse.Namespace) -> int:
    _sync_runner_dependencies()
    return _supervisor_runner.command_supervise(args)


def command_agent(args: argparse.Namespace) -> int:
    # Preserve the existing CLI-module monkeypatch surface used by tests and
    # external harnesses while keeping the execution loop outside this file.
    _sync_runner_dependencies()
    return _agent_runner.command_agent(args)



def command_check_command(args: argparse.Namespace) -> int:
    reason = dangerous_command_reason(args.command)
    if reason:
        print(f"BLOCKED: {reason}")
        return 2
    print("allowed")
    return 0

def _run_json_from_path(path: Path) -> Path:
    if path.is_dir():
        return path / "run.json"
    return path

def _load_run_json(path: Path) -> dict[str, object]:
    run_json = _run_json_from_path(path)
    if not run_json.exists():
        raise RunnerError(f"run.json not found: {run_json}")
    try:
        payload = json.loads(run_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RunnerError(f"invalid run.json: {run_json}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RunnerError(f"run.json must contain an object: {run_json}")
    payload["_run_json_path"] = str(run_json)
    return payload

def _run_profile_from_payload_or_children(run_json: Path, payload: dict[str, object]) -> str:
    if isinstance(payload.get("model_profile"), dict):
        return str(payload["model_profile"].get("profile") or "")
    if not run_json.name == "run.json":
        return ""
    for child in sorted(run_json.parent.glob("*/run.json")):
        try:
            child_payload = json.loads(child.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(child_payload, dict) and isinstance(child_payload.get("model_profile"), dict):
            return str(child_payload["model_profile"].get("profile") or "")
    return ""

def _run_compare_row(path: Path, payload: dict[str, object]) -> dict[str, object]:
    run_json = _run_json_from_path(path)
    completed = payload.get("completed_stages")
    stage_count = payload.get("stage_count")
    if isinstance(completed, list):
        approved = sum(
            1
            for item in completed
            if isinstance(item, dict) and item.get("status") == "approved"
        )
        failed = [
            item
            for item in completed
            if isinstance(item, dict) and item.get("status") == "failed"
        ]
        failed_stage = str(failed[0].get("stage_id") or "") if failed else ""
        failure_type = ""
        if failed:
            summary = failed[0].get("failure_summary")
            if isinstance(summary, dict):
                failure_type = str(summary.get("failure_type") or "")
        total = int(stage_count) if isinstance(stage_count, int) else len(completed)
    else:
        approved = 1 if payload.get("final_verdict") == "approved" else 0
        total = 1
        failed_stage = ""
        failure_type = str(payload.get("final_failure_type") or "")

    api_calls = payload.get("api_calls", 0)
    try:
        api_calls_int = int(api_calls)
    except (TypeError, ValueError):
        api_calls_int = 0
    llm_settings = payload.get("llm_settings")
    profile = _run_profile_from_payload_or_children(run_json, payload)
    return {
        "path": str(run_json),
        "status": str(payload.get("status") or payload.get("final_status") or payload.get("final_verdict") or ""),
        "approved_stages": approved,
        "total_stages": total,
        "failed_stage": failed_stage,
        "failure_type": failure_type,
        "api_calls": api_calls_int,
        "profile": profile,
        "llm_settings": llm_settings if isinstance(llm_settings, dict) else {},
    }

def command_compare_runs(args: argparse.Namespace) -> int:
    rows = [_run_compare_row(path, _load_run_json(path)) for path in args.runs]
    if args.format == "json":
        print(json.dumps({"runs": rows}, ensure_ascii=False, indent=2))
        return 0
    headers = ["run", "status", "stages", "failed", "failure", "api", "profile"]
    print("| " + " | ".join(headers) + " |")
    print("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        stages = f"{row['approved_stages']}/{row['total_stages']}"
        print(
            "| "
            + " | ".join(
                [
                    str(row["path"]),
                    str(row["status"]),
                    stages,
                    str(row["failed_stage"] or "-"),
                    str(row["failure_type"] or "-"),
                    str(row["api_calls"]),
                    str(row["profile"] or "-"),
                ]
            )
            + " |"
        )
    return 0



def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", type=Path, default=Path.cwd(), help="project root")
    parser.add_argument(
        "--config-file",
        type=Path,
        default=None,
        help="project config file; defaults to local_sdlc.json/yaml/yml when present",
    )
    parser.add_argument("--skills-dir", type=Path, default=DEFAULT_SKILLS_DIR, help="directory containing */SKILL.md")
    parser.add_argument("--base-url", default=None, help="OpenAI-compatible base URL")
    parser.add_argument("--api-key", default=None, help="API key for the configured endpoint")
    parser.add_argument("--model", default=None, help="model name")
    parser.add_argument(
        "--model-profile",
        choices=sorted(MODEL_PROFILE_ALIASES),
        default=None,
        help="named model optimization profile for function-level API settings",
    )
    parser.add_argument("--timeout", type=float, default=None, help=f"HTTP timeout seconds (default {DEFAULT_TIMEOUT:g})")
    parser.add_argument("--health-timeout", type=float, default=None, help=f"short timeout for /v1/models health probes (default {DEFAULT_HEALTH_TIMEOUT:g})")
    parser.add_argument("--temperature", type=float, default=None, help="base LLM temperature (default 0.2)")
    parser.add_argument("--max-tokens", type=int, default=None, help="base max completion tokens (default 4096)")
    parser.set_defaults(enable_thinking=None, stream=None)
    parser.add_argument("--enable-thinking", dest="enable_thinking", action="store_true", help="do not send chat_template_kwargs.enable_thinking=false")
    parser.add_argument("--disable-thinking", dest="enable_thinking", action="store_false", help="force chat_template_kwargs.enable_thinking=false")
    parser.add_argument("--stream", dest="stream", action="store_true", help="stream chat completions and checkpoint partial output when supported")
    parser.add_argument("--no-stream", dest="stream", action="store_false", help="disable streaming even when config enables it")
    parser.add_argument("--pm-max-tokens", type=int, default=None, help=f"completion tokens for PM/supervisor/spec calls (default {DEFAULT_PM_MAX_TOKENS})")
    parser.add_argument("--coder-max-tokens", type=int, default=None, help=f"completion tokens for coder calls (default {DEFAULT_CODER_MAX_TOKENS})")
    parser.add_argument("--judge-max-tokens", type=int, default=None, help=f"completion tokens for judge calls (default {DEFAULT_JUDGE_MAX_TOKENS})")
    parser.add_argument("--pm-temperature", type=float, default=None, help="temperature for PM/supervisor/spec calls (default 0.2)")
    parser.add_argument("--coder-temperature", type=float, default=None, help="temperature for coder calls (default 0.1)")
    parser.add_argument("--judge-temperature", type=float, default=None, help="temperature for judge calls (default 0.0)")
    parser.add_argument("--pm-thinking", choices=["default", "on", "off"], default=None, help="thinking mode override for PM/supervisor/spec calls")
    parser.add_argument("--coder-thinking", choices=["default", "on", "off"], default=None, help="thinking mode override for coder artifact calls")
    parser.add_argument("--judge-thinking", choices=["default", "on", "off"], default=None, help="thinking mode override for judge calls")
    parser.add_argument(
        "--api-profile",
        action="append",
        default=None,
        metavar="FUNCTION:KEY=VALUE,...",
        help=(
            "override a function-level API profile, e.g. "
            "generate_artifact:max_tokens=32768,temperature=0.05,thinking=off"
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run SDLC skills with a configurable OpenAI-compatible LLM API."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="check local configuration")
    add_common_arguments(doctor)
    doctor.add_argument("--skip-llm", action="store_true", help="do not call /v1/models")
    doctor.add_argument("--skip-probes", action="store_true", help="skip short /chat/completions capability probes")
    doctor.add_argument("--probe-timeout", type=float, default=30.0, help="timeout seconds for each doctor probe")
    doctor.set_defaults(func=command_doctor)

    health = sub.add_parser("health", help="quickly check whether the LLM API is reachable")
    add_common_arguments(health)
    health.set_defaults(func=command_health)

    web = sub.add_parser("web", help="serve a tiny local browser UI for chat-style agent jobs")
    add_common_arguments(web)
    web.add_argument("--host", default="127.0.0.1", help="bind host; default keeps the UI local-only")
    web.add_argument("--port", type=int, default=8765, help="bind port; use 0 for an ephemeral port")
    web.add_argument("--entrypoint", type=Path, default=_web_server._repo_entrypoint(), help="local_sdlc.py entrypoint")
    web.add_argument("--open-browser", action="store_true", help="open the UI in the default browser")
    web.set_defaults(func=command_web)

    list_skills = sub.add_parser("list-skills", help="list available skills")
    add_common_arguments(list_skills)
    list_skills.set_defaults(func=command_list_skills)

    stage_plan = sub.add_parser("stage-plan", help="derive a deterministic small-stage queue from SPEC.md")
    add_common_arguments(stage_plan)
    stage_plan.add_argument("--spec-file", type=Path, default=None, help="SPEC.md-compatible file to plan from")
    stage_plan.add_argument("--format", choices=("markdown", "json"), default="markdown", help="output format")
    stage_plan.set_defaults(func=command_stage_plan)

    run_stages = sub.add_parser("run-stages", help="execute the synthesized stage queue with isolated agent runs")
    add_common_arguments(run_stages)
    run_stages.add_argument("brief", help="development request")
    run_stages.add_argument("--pm-skill", default="sdlc", help="skill used for PM-level planning")
    run_stages.add_argument("--coder-skill", default="tdd", help="skill used for coder-level implementation")
    run_stages.add_argument("--judge-skill", default="review", help="skill used for objective review")
    run_stages.add_argument("--domain-skill", default="ddd", help="skill used for DDD/domain-contract modeling")
    run_stages.add_argument(
        "--domain-modeling",
        choices=("auto", "always", "never"),
        default="auto",
        help="run the DDD/domain-contract phase automatically, always, or never",
    )
    run_stages.add_argument(
        "--judge-mode",
        choices=("llm", "command-only"),
        default="llm",
        help="use an LLM judge or accept solely from passing command/smoke evidence",
    )
    run_stages.add_argument("--skip-stage-pm", action="store_true", help="skip each stage PM API call and use deterministic PM control")
    run_stages.add_argument("--spec-file", type=Path, default=None, help="SPEC.md-compatible file to use instead of project/SPEC.md")
    run_stages.add_argument("--from-stage", default=None, help="first stage id to execute, e.g. S03")
    run_stages.add_argument("--to-stage", default=None, help="last stage id to execute, e.g. S05")
    run_stages.add_argument("--stage-max-rounds", type=int, default=3, help="maximum agent repair rounds per stage")
    run_stages.add_argument("--protocol-repair-rounds", type=int, default=2, help="extra artifact-protocol repair rounds per stage")
    run_stages.add_argument("--adaptive-rounds", type=int, default=2, help="extra functional rounds allowed when executable failure counts shrink")
    run_stages.add_argument("--root-cause-patch-rounds", type=int, default=2, help="extra stage-local root-cause patch rounds after repeated same functional failures")
    run_stages.add_argument("--final-repair-rounds", type=int, default=2, help="maximum final integration repair rounds after all stages pass but final checks fail")
    run_stages.add_argument("--max-context-chars", type=int, default=40000, help="max included file chars per stage")
    run_stages.add_argument("--document-window", type=int, default=6, help="recent run documents to pass to each stage coder")
    run_stages.add_argument("--allow-no-context", action="store_true", help="allow a stage without inferred file targets")
    run_stages.add_argument("--small-patch", action="store_true", help="tell stage coders to prefer minimal local edits")
    run_stages.add_argument("--no-replace-file", action="store_true", help="reject whole-file replacement artifacts")
    run_stages.add_argument("--no-extra-files", action="store_true", help="reject files outside inferred stage targets")
    run_stages.add_argument("--apply", action="store_true", help="apply stage artifacts")
    run_stages.add_argument("--precheck", action="store_true", help="run configured checks before each stage coder call")
    run_stages.add_argument("--stage-test-command", action="append", default=[], help="command to run inside each stage instead of the auto py_compile stage check")
    run_stages.add_argument("--no-auto-stage-test", action="store_true", help="disable automatic per-stage py_compile checks")
    run_stages.add_argument("--test-command", action="append", default=[], help="final gate command to run after all stages pass")
    run_stages.add_argument("--command-timeout", type=float, default=60.0, help="timeout seconds for each test command")
    run_stages.add_argument(
        "--redis-smoke",
        choices=("auto", "always", "never"),
        default="auto",
        help="run built-in Redis protocol smoke checks automatically, always, or never",
    )
    run_stages.add_argument(
        "--artifact-format",
        choices=("auto", "json", "legacy"),
        default="auto",
        help="coder artifact format for each stage",
    )
    run_stages.add_argument(
        "--worktree-mode",
        choices=("off", "copy"),
        default="off",
        help="run each stage in an isolated temporary copy and copy allowed files back only after approval",
    )
    run_stages.add_argument("--stop-on-failure", action=argparse.BooleanOptionalAction, default=True, help="stop the queue when a stage fails")
    run_stages.add_argument("--dry-run", action="store_true", help="write the stage queue without executing stages")
    run_stages.add_argument("--run-dir", type=Path, default=None, help="directory for staged run documents")
    run_stages.set_defaults(func=command_run_stages)

    spec = sub.add_parser("spec", help="generate or update SPEC.md")
    add_common_arguments(spec)
    spec.add_argument("brief", help="development request")
    spec.add_argument("--apply", action="store_true", help="write generated content to SPEC.md")
    spec.set_defaults(func=command_spec)

    phase = sub.add_parser("phase", help="run a single SDLC skill phase")
    add_common_arguments(phase)
    phase.add_argument("skill", help="skill name, e.g. architect, tdd, review")
    phase.add_argument("--agent-level", choices=["pm", "coder", "judge"], default="", help="role contract for this call")
    phase.add_argument("--instruction", default="", help="extra instruction for the phase")
    phase.add_argument("--apply", action="store_true", help="append output to SPEC.md")
    phase.set_defaults(func=command_phase)

    implement = sub.add_parser("implement", help="generate an implementation patch proposal")
    add_common_arguments(implement)
    implement.add_argument("--skill", default="tdd", help="skill to guide implementation")
    implement.add_argument("--instruction", default="", help="extra patch instruction")
    implement.add_argument("--include", action="append", default=[], help="file to include in prompt")
    implement.add_argument("--new-file", action="append", default=[], help="project-relative new file target")
    implement.add_argument("--max-context-chars", type=int, default=40000, help="max included file chars")
    implement.add_argument("--patch-file", type=Path, default=GENERATED_DIR / "proposal.patch")
    implement.add_argument("--allow-no-context", action="store_true", help="allow coder call without included file contents")
    implement.add_argument("--apply", action="store_true", help="git apply the patch after validation")
    implement.set_defaults(func=command_implement)

    supervisor = sub.add_parser("supervisor", help="route work using the bundled SDLC Supervisor")
    add_common_arguments(supervisor)
    supervisor.add_argument("brief", help="user request to classify and route")
    supervisor.add_argument("--agents-dir", type=Path, default=DEFAULT_AGENTS_DIR, help="directory containing supervisor.md")
    supervisor.add_argument("--phases", default="auto", help="auto or comma-separated SDLC phases")
    supervisor.add_argument("--execute", action="store_true", help="execute the routed SDLC phase calls")
    supervisor.add_argument("--apply-spec", action="store_true", help="write /spec output to SPEC.md when executed")
    supervisor.add_argument("--append-phase-output", action="store_true", help="append executed phase outputs to SPEC.md")
    supervisor.add_argument("--allow-ambiguous", action="store_true", help="execute even when vague wording is detected")
    supervisor.add_argument("--include", action="append", default=[], help="file to include for phase context")
    supervisor.add_argument("--max-context-chars", type=int, default=40000, help="max included file chars")
    supervisor.add_argument("--run-dir", type=Path, default=None, help="directory for run documents")
    supervisor.set_defaults(func=command_supervisor)

    supervise = sub.add_parser("supervise", help="run PM/Coder/Judge as independent documented calls")
    add_common_arguments(supervise)
    supervise.add_argument("brief", help="development request")
    supervise.add_argument("--steps", default="pm,coder,judge", help="comma-separated: spec,pm,coder,judge or all")
    supervise.add_argument("--pm-skill", default="sdlc", help="skill used for PM-level planning")
    supervise.add_argument("--coder-skill", default="tdd", help="skill used for coder-level implementation")
    supervise.add_argument("--judge-skill", default="review", help="skill used for objective review")
    supervise.add_argument("--include", action="append", default=[], help="file to include for coder context")
    supervise.add_argument("--new-file", action="append", default=[], help="project-relative new file target for coder")
    supervise.add_argument("--max-context-chars", type=int, default=40000, help="max included file chars")
    supervise.add_argument("--allow-no-context", action="store_true", help="allow coder step without included file contents")
    supervise.add_argument("--auto-fix", action="store_true", help="loop coder/judge rounds until approved or --max-rounds is reached")
    supervise.add_argument("--max-rounds", type=int, default=1, help="maximum coder/judge rounds when --auto-fix is enabled")
    supervise.add_argument("--run-dir", type=Path, default=None, help="directory for run documents")
    supervise.add_argument("--apply-spec", action="store_true", help="write spec draft to SPEC.md when the spec step runs")
    supervise.set_defaults(func=command_supervise)

    agent = sub.add_parser("agent", help="run a minimal coding agent loop: code, apply, test, judge")
    add_common_arguments(agent)
    agent.add_argument("brief", help="development request")
    agent.add_argument("--pm-skill", default="sdlc", help="skill used for PM-level planning")
    agent.add_argument("--coder-skill", default="tdd", help="skill used for coder-level implementation")
    agent.add_argument("--judge-skill", default="review", help="skill used for objective review")
    agent.add_argument("--domain-skill", default="ddd", help="skill used for DDD/domain-contract modeling")
    agent.add_argument(
        "--domain-modeling",
        choices=("auto", "always", "never"),
        default="auto",
        help="run the DDD/domain-contract phase automatically, always, or never",
    )
    agent.add_argument(
        "--project-policy-triage",
        choices=("auto", "always", "never"),
        default="auto",
        help="run judge-level project policy triage for context-dependent ownership decisions",
    )
    agent.add_argument(
        "--judge-mode",
        choices=("llm", "command-only"),
        default="llm",
        help="use an LLM judge or accept solely from passing command/smoke evidence",
    )
    agent.add_argument("--skip-pm", action="store_true", help="skip PM API call and use deterministic control doc")
    agent.add_argument("--spec-file", type=Path, default=None, help="SPEC.md-compatible file to use instead of project/SPEC.md")
    agent.add_argument("--include", action="append", default=[], help="file to include for coder context")
    agent.add_argument("--context", action="append", default=[], help="read-only project-relative file to include as coder context")
    agent.add_argument("--context-slice", action="append", default=[], help="include only path:start:end line ranges as coder context")
    agent.add_argument("--new-file", action="append", default=[], help="project-relative new file target for coder")
    agent.add_argument("--require-path", action="append", default=[], help="project-relative artifact that must exist and be non-empty before approval")
    agent.add_argument("--max-context-chars", type=int, default=40000, help="max included file chars")
    agent.add_argument("--document-window", type=int, default=6, help="recent run documents to pass to coder/judge calls")
    agent.add_argument("--allow-no-context", action="store_true", help="allow coder step without included file contents")
    agent.add_argument("--small-patch", action="store_true", help="tell the coder to prefer minimal local edits over full rewrites")
    agent.add_argument("--no-replace-file", action="store_true", help="reject whole-file replacement artifacts and require local edits")
    agent.add_argument(
        "--no-extra-files",
        action="store_true",
        help="reject file artifacts outside --include/--new-file targets; by default safe newly created files are allowed",
    )
    agent.add_argument("--apply", action="store_true", help="apply generated patches with git apply")
    agent.add_argument("--precheck", action="store_true", help="run configured command/smoke checks before the first coder call")
    agent.add_argument("--test-command", action="append", default=[], help="command to run after applying each patch")
    agent.add_argument("--command-timeout", type=float, default=60.0, help="timeout seconds for each test command")
    agent.add_argument(
        "--redis-smoke",
        choices=("auto", "always", "never"),
        default="auto",
        help="run built-in Redis protocol smoke checks automatically, always, or never",
    )
    agent.add_argument(
        "--artifact-format",
        choices=("auto", "json", "legacy"),
        default="auto",
        help="coder artifact format: JSON first, JSON only, or legacy diff/marker artifacts",
    )
    agent.add_argument("--resume", type=Path, default=None, help="resume/repair from a previous agent run directory")
    agent.add_argument("--resume-worktree", action="store_true", help="when resuming, start from the previous run's temporary worktree state")
    agent.add_argument("--resume-worktree-path", type=Path, default=None, help="start from an explicit temporary worktree path, even without a completed run.json")
    agent.add_argument(
        "--worktree-mode",
        choices=("off", "copy"),
        default="off",
        help="run edits/tests in an isolated temporary copy and copy allowed files back only after approval",
    )
    agent.add_argument("--max-rounds", type=int, default=3, help="maximum functional code/apply/test/judge repair rounds")
    agent.add_argument("--protocol-repair-rounds", type=int, default=1, help="extra artifact-protocol repair rounds that do not consume --max-rounds")
    agent.add_argument("--adaptive-rounds", type=int, default=2, help="extra functional rounds allowed when executable failure counts shrink")
    agent.add_argument("--root-cause-patch-rounds", type=int, default=1, help="extra root-cause patch rounds after repeated same functional failures")
    agent.add_argument("--run-dir", type=Path, default=None, help="directory for run documents")
    agent.set_defaults(func=command_agent)

    check = sub.add_parser("check-command", help="evaluate a shell command against local safety rules")
    check.add_argument("command")
    check.set_defaults(func=command_check_command)

    compare = sub.add_parser("compare-runs", help="compare one or more local_sdlc run.json files")
    compare.add_argument("runs", nargs="+", type=Path, help="run directory or run.json path")
    compare.add_argument("--format", choices=("markdown", "json"), default="markdown", help="output format")
    compare.set_defaults(func=command_compare_runs)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("interrupted by user", file=sys.stderr)
        return 130
    except RunnerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
