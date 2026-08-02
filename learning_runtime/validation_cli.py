"""CLI adapter for deterministic candidate validation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .candidate_store import CandidateStore
from .evaluation_store import EvaluationStore
from .schema_validation import require_mapping
from .storage import ExperienceStore
from .validation import validate_and_store
from .validation_models import ValidationCase
from .work_control import LearningWorkControl, LearningWorkStopped
from .work_control_cli import add_work_control_arguments, limits_from_args


def _load_cases(paths: Sequence[Path]) -> tuple[ValidationCase, ...]:
    cases: list[ValidationCase] = []
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        cases.append(
            ValidationCase.from_dict(
                require_mapping(payload, f"validation case {path.name}")
            )
        )
    return tuple(cases)


def command_validate(args: argparse.Namespace) -> int:
    item = CandidateStore(args.data_dir).get_candidate(
        args.candidate,
        args.version,
    )
    cases = _load_cases(args.case)
    control = LearningWorkControl(
        args.data_dir,
        "candidate_validation",
        limits=limits_from_args(args),
        operation_id=args.learning_operation_id,
    )
    try:
        report = validate_and_store(
            item,
            ExperienceStore(args.data_dir),
            EvaluationStore(args.data_dir),
            cases,
            control=control,
        )
    except LearningWorkStopped as exc:
        report = {
            "status": "stopped",
            "reason_code": exc.reason_code,
            "learning_control": control.report(),
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2
    report = {**report, "learning_control": control.report()}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["verdict"] == "shadow_pass" else 1


def add_validation_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "validate",
        help="validate a candidate against explicit replay and holdout cases",
    )
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--version", type=int, default=None)
    parser.add_argument("--case", type=Path, action="append", required=True)
    add_work_control_arguments(parser)
    parser.set_defaults(func=command_validate)


__all__ = ["add_validation_parser", "command_validate"]
