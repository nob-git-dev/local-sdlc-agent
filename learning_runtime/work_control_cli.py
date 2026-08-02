"""Command-line adapters for learner cancellation and resource limits."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .work_control import LearningLimits, request_learning_cancel
from .work_control_store import learning_work_status


def add_work_control_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--learning-operation-id", default="")
    parser.add_argument("--learner-max-api-calls", type=int, default=30)
    parser.add_argument("--learner-max-cases", type=int, default=1000)
    parser.add_argument("--learner-max-tokens", type=int, default=262_144)
    parser.add_argument("--learner-max-wall-seconds", type=float, default=3600.0)


def limits_from_args(args: argparse.Namespace) -> LearningLimits:
    return LearningLimits(
        max_api_calls=args.learner_max_api_calls,
        max_cases=args.learner_max_cases,
        max_tokens=args.learner_max_tokens,
        max_wall_seconds=args.learner_max_wall_seconds,
    )


def command_cancel_learning(args: argparse.Namespace) -> int:
    report = request_learning_cancel(
        args.data_dir,
        operation_id=args.operation,
        reason_code=args.reason,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def command_work_status(args: argparse.Namespace) -> int:
    report = learning_work_status(args.data_dir, operation_id=args.operation)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def add_cancel_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "cancel-work",
        help="stop active learner work at its next durable checkpoint",
    )
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--operation", default="")
    parser.add_argument("--reason", default="user_requested")
    parser.set_defaults(func=command_cancel_learning)

    status = subparsers.add_parser(
        "work-status",
        help="show learner operations, budgets, and stop reasons",
    )
    status.add_argument("--data-dir", required=True, type=Path)
    status.add_argument("--operation", default="")
    status.set_defaults(func=command_work_status)


__all__ = [
    "add_cancel_parser",
    "add_work_control_arguments",
    "limits_from_args",
]
