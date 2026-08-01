"""CLI for the independent Experience Learning Runtime foundation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from typing import Sequence

from sdlc_events import RuntimeEventLedger, event_ledger_path, validate_contract_registry

from .audit import audit_run
from .collector import collect_run
from .legacy import import_legacy_run
from .inventory import validate_mutation_inventory
from .storage import ExperienceStore, learning_data_dir


def _print(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def command_collect(args: argparse.Namespace) -> int:
    report = collect_run(
        args.run_dir,
        data_dir=args.data_dir,
        import_legacy=not args.no_legacy,
    )
    _print(report)
    return 0 if report.get("status") == "pass" else 1


def command_audit(args: argparse.Namespace) -> int:
    report = audit_run(
        args.run_dir,
        import_legacy=not args.no_legacy,
        persist_violation=not args.no_persist_violation,
    )
    _print(report)
    return 0 if report.get("status") == "pass" else 1


def command_import_legacy(args: argparse.Namespace) -> int:
    report = import_legacy_run(args.run_dir)
    _print(report)
    return 0 if report.get("status") == "pass" else 1


def command_status(args: argparse.Namespace) -> int:
    path = event_ledger_path(args.run_dir)
    if not path.is_file():
        _print({"status": "not_initialized", "run_dir": str(args.run_dir)})
        return 1
    ledger = RuntimeEventLedger(args.run_dir)
    findings = ledger.integrity_findings()
    _print(
        {
            "status": "pass" if not findings else "fail",
            "run_id": ledger.run_id,
            "transition_count": ledger.transition_count(),
            "event_count": ledger.event_count(),
            "outbox": ledger.outbox_status(),
            "findings": findings,
        }
    )
    return 0 if not findings else 1


def command_doctor(args: argparse.Namespace) -> int:
    data_dir = learning_data_dir(args.data_dir)
    findings = validate_contract_registry()
    findings.extend(validate_mutation_inventory(Path(__file__).resolve().parents[1]))
    sqlite_version = sqlite3.sqlite_version
    try:
        store = ExperienceStore(data_dir)
        writable = store.path.is_file()
    except OSError as exc:
        findings.append(f"shared_store_unavailable:{exc}")
        writable = False
    report = {
        "status": "pass" if not findings and writable else "fail",
        "event_schema_contracts": "pass" if not validate_contract_registry() else "fail",
        "sqlite_version": sqlite_version,
        "data_dir": str(data_dir),
        "shared_store_writable": writable,
        "findings": findings,
    }
    _print(report)
    return 0 if report["status"] == "pass" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="local_sdlc_learning.py",
        description="Collect and audit Local SDLC Agent runtime events.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    collect = sub.add_parser("collect", help="collect one run's pending outbox")
    collect.add_argument("--run-dir", type=Path, required=True)
    collect.add_argument("--data-dir", type=Path, default=None)
    collect.add_argument("--no-legacy", action="store_true")
    collect.set_defaults(func=command_collect)

    audit = sub.add_parser("audit", help="audit event and closure completeness")
    audit.add_argument("--run-dir", type=Path, required=True)
    audit.add_argument("--no-legacy", action="store_true")
    audit.add_argument("--no-persist-violation", action="store_true")
    audit.set_defaults(func=command_audit)

    legacy = sub.add_parser("import-legacy", help="import existing JSON/JSONL evidence")
    legacy.add_argument("--run-dir", type=Path, required=True)
    legacy.set_defaults(func=command_import_legacy)

    status = sub.add_parser("status", help="show one run's event ledger status")
    status.add_argument("--run-dir", type=Path, required=True)
    status.set_defaults(func=command_status)

    doctor = sub.add_parser("doctor", help="check contracts and shared persistence")
    doctor.add_argument("--data-dir", type=Path, default=None)
    doctor.set_defaults(func=command_doctor)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (OSError, ValueError, sqlite3.Error, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
