"""Single SDLC phase and implementation commands."""

from __future__ import annotations

import argparse
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


def command_spec(args: argparse.Namespace) -> int:
    skills = load_skills(args.skills_dir)
    skill = skills.get("spec")
    if skill is None:
        raise RunnerError("spec skill not found")

    project = args.project.resolve()
    current_spec = read_text_if_exists(project / "SPEC.md")
    instruction = textwrap.dedent(
        f"""
        Create or update SPEC.md for this request.

        Request:
        {args.brief}

        Project manifest:
        {project_manifest(project)}

        Return the complete SPEC.md content in Markdown.
        """
    ).strip()
    client = LocalLLMClient(build_config(args))
    output = run_skill_call(
        client=client,
        skill=skill,
        spec=current_spec,
        instruction=instruction,
        agent_level="pm",
        project_manifest_text=project_manifest(project),
        output_contract="Return the complete SPEC.md content in Markdown.",
        call_function="plan_work",
    )
    if args.apply:
        (project / "SPEC.md").write_text(output + "\n", encoding="utf-8")
        print(f"wrote: {project / 'SPEC.md'}")
    else:
        print(output)
    return 0

def command_phase(args: argparse.Namespace) -> int:
    skills = load_skills(args.skills_dir)
    skill = skills.get(args.skill)
    if skill is None:
        raise RunnerError(f"skill not found: {args.skill}")

    project = args.project.resolve()
    spec = read_text_if_exists(project / "SPEC.md")
    if not spec:
        raise RunnerError("SPEC.md is required before running a phase")

    agent_level = args.agent_level or default_agent_level(args.skill)
    instruction = args.instruction or (
        f"Run the {args.skill} phase. Return the SPEC.md section that should be reviewed by a human."
    )
    client = LocalLLMClient(build_config(args))
    output = run_skill_call(
        client=client,
        skill=skill,
        spec=spec,
        instruction=instruction,
        agent_level=agent_level,
        project_manifest_text=project_manifest(project),
        call_function=default_call_function_for(agent_level, args.skill),
    )
    if args.apply:
        append_to_spec(project / "SPEC.md", args.skill, output)
        print(f"appended {args.skill} output to: {project / 'SPEC.md'}")
    else:
        print(output)
    return 0

def append_to_spec(spec_path: Path, skill_name: str, output: str) -> None:
    existing = read_text_if_exists(spec_path).rstrip()
    block = textwrap.dedent(
        f"""

        ## ローカル SDLC 実行ログ: {skill_name}

        {output.strip()}
        """
    )
    spec_path.write_text(existing + block + "\n", encoding="utf-8")

def command_implement(args: argparse.Namespace) -> int:
    skills = load_skills(args.skills_dir)
    skill = skills.get(args.skill)
    if skill is None:
        raise RunnerError(f"skill not found: {args.skill}")

    project = args.project.resolve()
    spec = read_text_if_exists(project / "SPEC.md")
    if not spec:
        raise RunnerError("SPEC.md is required before generating an implementation patch")
    new_files = normalize_new_files(args.new_file)
    if not args.include and not args.allow_no_context and not new_files:
        raise RunnerError("implement requires --include, --new-file, or explicit --allow-no-context")

    file_context = collect_file_context(project, args.include, args.max_context_chars)
    new_file_instruction = ""
    if new_files:
        new_file_instruction = "Create these new project-relative file(s): " + ", ".join(new_files)
    instruction = textwrap.dedent(
        f"""
        Generate a minimal implementation patch as unified diff only.
        Do not include prose outside the diff.

        Additional instruction:
        {args.instruction or "(none)"}

        New file targets:
        {new_file_instruction or "(none)"}

        Project manifest:
        {project_manifest(project)}

        Included file contents:
        {file_context or "(no file contents included; ask for --include files if needed)"}
        """
    ).strip()

    client = LocalLLMClient(build_config(args))
    patch = run_skill_call(
        client=client,
        skill=skill,
        spec=spec,
        instruction=instruction,
        agent_level="coder",
        project_manifest_text=project_manifest(project),
        file_context=file_context,
        output_contract="Return unified diff only. If required context is missing, return a short MISSING_CONTEXT list.",
        call_function="generate_artifact",
    )

    patch_file = project / args.patch_file
    patch_file.parent.mkdir(parents=True, exist_ok=True)
    patch_file.write_text(patch + "\n", encoding="utf-8")
    print(f"wrote patch proposal: {patch_file}")

    if args.apply:
        apply_patch_file(project, patch_file)
        print("applied patch")
    return 0
