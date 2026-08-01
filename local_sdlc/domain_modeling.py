"""Deterministic decision for invoking the optional DDD role."""

from __future__ import annotations

import argparse
from typing import Sequence

from .models import RunnerError, Skill
from .routing import classify_task_type, detect_danger_signals, needs_domain_modeling


def domain_modeling_decision(
    args: argparse.Namespace,
    skills: dict[str, Skill],
    spec: str,
    resume_documents: Sequence[tuple[str, str]] = (),
) -> dict[str, object]:
    mode = str(getattr(args, "domain_modeling", "auto") or "auto")
    skill_name = str(getattr(args, "domain_skill", "ddd") or "ddd")
    if mode not in {"auto", "always", "never"}:
        raise RunnerError("--domain-modeling must be auto, always, or never")
    if mode == "never":
        return {"mode": mode, "skill": skill_name, "run": False, "reason": "disabled"}
    if any(title == "Domain contract document" for title, _ in resume_documents) and mode != "always":
        return {"mode": mode, "skill": skill_name, "run": False, "reason": "resume_already_has_domain_contract"}
    if skill_name not in skills:
        if mode == "always":
            raise RunnerError(f"domain skill not found: {skill_name}")
        return {"mode": mode, "skill": skill_name, "run": False, "reason": "domain_skill_missing"}

    task_type = classify_task_type(args.brief)
    danger_signals = detect_danger_signals(args.brief)
    needed = needs_domain_modeling(args.brief, spec, task_type, danger_signals)
    if mode == "always" or needed:
        return {
            "mode": mode,
            "skill": skill_name,
            "run": True,
            "reason": "forced" if mode == "always" else "domain_modeling_needed",
            "task_type": task_type,
            "danger_signals": danger_signals,
        }
    return {
        "mode": mode,
        "skill": skill_name,
        "run": False,
        "reason": "domain_modeling_not_needed",
        "task_type": task_type,
        "danger_signals": danger_signals,
    }
