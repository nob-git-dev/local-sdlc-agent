"""CLI commands for promotion, lifecycle control, and explanation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .operations import explain_knowledge, inspect_knowledge, snapshot_view
from .promotion import PromotionService


def _print(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def command_promote(args: argparse.Namespace) -> int:
    result = PromotionService(args.data_dir).promote(args.candidate, args.version)
    _print(result)
    return 2 if result["status"] == "approval_required" else 0


def command_approve_promotion(args: argparse.Namespace) -> int:
    result = PromotionService(args.data_dir).approve(
        args.operation,
        args.decision,
        source="cli",
        note=args.note,
    )
    _print(result)
    return 0


def command_challenge(args: argparse.Namespace) -> int:
    result = PromotionService(args.data_dir).challenge(
        args.knowledge,
        reason_code=args.reason,
    )
    _print(result)
    return 0


def command_retire(args: argparse.Namespace) -> int:
    result = PromotionService(args.data_dir).retire(
        args.knowledge,
        reason_code=args.reason,
    )
    _print(result)
    return 0


def command_supersede(args: argparse.Namespace) -> int:
    result = PromotionService(args.data_dir).supersede(
        args.knowledge,
        by_knowledge_id=args.by,
        reason_code=args.reason,
    )
    _print(result)
    return 0


def command_rollback(args: argparse.Namespace) -> int:
    result = PromotionService(args.data_dir).rollback(args.snapshot)
    _print(result)
    return 0


def command_inspect(args: argparse.Namespace) -> int:
    _print(inspect_knowledge(args.data_dir, args.knowledge, args.version))
    return 0


def command_explain(args: argparse.Namespace) -> int:
    _print(explain_knowledge(args.data_dir, args.knowledge, args.version))
    return 0


def command_snapshots(args: argparse.Namespace) -> int:
    _print(snapshot_view(args.data_dir))
    return 0


def _knowledge_parser(subparsers, name: str, help_text: str):
    parser = subparsers.add_parser(name, help=help_text)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("knowledge")
    parser.add_argument("--version", type=int, default=None)
    return parser


def add_operations_parsers(subparsers: argparse._SubParsersAction) -> None:
    promote = subparsers.add_parser("promote", help="promote one validated candidate")
    promote.add_argument("--data-dir", type=Path, default=None)
    promote.add_argument("--candidate", required=True)
    promote.add_argument("--version", type=int, default=None)
    promote.set_defaults(func=command_promote)

    approve = subparsers.add_parser(
        "approve-promotion",
        help="grant one explicit human approval to a pending promotion",
    )
    approve.add_argument("--data-dir", type=Path, default=None)
    approve.add_argument("--operation", required=True)
    approve.add_argument("--decision", required=True)
    approve.add_argument("--note", default="")
    approve.set_defaults(func=command_approve_promotion)

    challenge = subparsers.add_parser("challenge", help="disable active knowledge")
    challenge.add_argument("--data-dir", type=Path, default=None)
    challenge.add_argument("--knowledge", required=True)
    challenge.add_argument("--reason", required=True)
    challenge.set_defaults(func=command_challenge)

    retire = subparsers.add_parser("retire", help="retire active or challenged knowledge")
    retire.add_argument("--data-dir", type=Path, default=None)
    retire.add_argument("--knowledge", required=True)
    retire.add_argument("--reason", required=True)
    retire.set_defaults(func=command_retire)

    supersede = subparsers.add_parser("supersede", help="replace active knowledge")
    supersede.add_argument("--data-dir", type=Path, default=None)
    supersede.add_argument("--knowledge", required=True)
    supersede.add_argument("--by", required=True)
    supersede.add_argument("--reason", required=True)
    supersede.set_defaults(func=command_supersede)

    rollback = subparsers.add_parser("rollback", help="select an earlier verified snapshot")
    rollback.add_argument("--data-dir", type=Path, default=None)
    rollback.add_argument("--snapshot", required=True)
    rollback.set_defaults(func=command_rollback)

    inspect = _knowledge_parser(subparsers, "inspect", "inspect one knowledge item")
    inspect.set_defaults(func=command_inspect)
    explain = _knowledge_parser(subparsers, "explain", "explain authority and lifecycle")
    explain.set_defaults(func=command_explain)

    snapshots = subparsers.add_parser("snapshots", help="list immutable snapshots")
    snapshots.add_argument("--data-dir", type=Path, default=None)
    snapshots.set_defaults(func=command_snapshots)


__all__ = ["add_operations_parsers"]
