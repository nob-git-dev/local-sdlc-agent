"""CLI for the independent Experience Learning Runtime foundation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from typing import Sequence

from local_sdlc.llm_client import LocalLLMClient, build_config
from local_sdlc.models import (
    DEFAULT_BASE_URL,
    DEFAULT_HEALTH_TIMEOUT,
    DEFAULT_TIMEOUT,
    MODEL_PROFILE_ALIASES,
)
from sdlc_events import RuntimeEventLedger, event_ledger_path, validate_contract_registry

from .audit import audit_run
from .candidate_llm import LocalCandidateLLM
from .candidate_miner import mine_candidates
from .candidate_store import CandidateStore
from .collector import collect_run
from .domain_map import DomainMap
from .episodes import build_and_store_recovery_episodes
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


def command_build_episodes(args: argparse.Namespace) -> int:
    store = ExperienceStore(args.data_dir)
    report = build_and_store_recovery_episodes(store)
    _print(report)
    return 0 if report.get("status") == "pass" else 1


def _load_domain_maps(paths: Sequence[Path]) -> dict[str, DomainMap]:
    result: dict[str, DomainMap] = {}
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Domain Map must be a JSON object: {path}")
        domain_map = DomainMap.from_dict(payload)
        if domain_map.project_fingerprint in result:
            raise ValueError(
                "duplicate Domain Map project_fingerprint: "
                + domain_map.project_fingerprint
            )
        result[domain_map.project_fingerprint] = domain_map
    return result


def command_mine_candidates(args: argparse.Namespace) -> int:
    config = build_config(args)
    client = LocalLLMClient(config)
    llm = LocalCandidateLLM(client)
    experience = ExperienceStore(args.data_dir)
    candidates = CandidateStore(args.data_dir)
    report = mine_candidates(
        experience,
        candidates,
        llm,
        domain_maps=_load_domain_maps(args.domain_map),
        max_batches=args.max_batches,
    )
    report["model_profile"] = config.model_profile
    report["function_profiles"] = {
        name: {
            "model": settings.model or "(auto)",
            "temperature": settings.temperature,
            "max_tokens": settings.max_tokens,
            "thinking": "off" if settings.disable_thinking else "on",
        }
        for name in (
            "candidate_abstraction",
            "scope_classification",
            "candidate_serialization",
        )
        for settings in [client.call_settings("judge", name)]
    }
    report["reasoning_audit"] = llm.reasoning_audit()
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
        candidate_store = CandidateStore(data_dir)
        writable = store.path.is_file() and candidate_store.path.is_file()
    except (OSError, sqlite3.Error) as exc:
        findings.append(f"shared_store_unavailable:{exc}")
        writable = False
    report = {
        "status": "pass" if not findings and writable else "fail",
        "event_schema_contracts": "pass" if not validate_contract_registry() else "fail",
        "sqlite_version": sqlite_version,
        "data_dir": str(data_dir),
        "shared_store_writable": writable,
        "candidate_store_writable": candidate_store.path.is_file() if writable else False,
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

    episodes = sub.add_parser("build-episodes", help="build normalized causal recovery episodes")
    episodes.add_argument("--data-dir", type=Path, required=True)
    episodes.set_defaults(func=command_build_episodes)

    candidates = sub.add_parser(
        "mine-candidates",
        help="propose candidate-only knowledge from eligible episodes",
    )
    candidates.add_argument("--data-dir", type=Path, required=True)
    candidates.add_argument("--domain-map", type=Path, action="append", default=[])
    candidates.add_argument("--max-batches", type=int, default=10)
    _add_llm_arguments(candidates)
    candidates.set_defaults(func=command_mine_candidates)

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


def _add_llm_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--config-file", type=Path, default=None)
    parser.add_argument("--base-url", default=None, help=f"OpenAI-compatible URL (default {DEFAULT_BASE_URL})")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument(
        "--model-profile",
        choices=sorted(MODEL_PROFILE_ALIASES),
        default=None,
    )
    parser.add_argument("--timeout", type=float, default=None, help=f"request timeout (default {DEFAULT_TIMEOUT:g}s)")
    parser.add_argument(
        "--health-timeout",
        type=float,
        default=None,
        help=f"health timeout (default {DEFAULT_HEALTH_TIMEOUT:g}s)",
    )
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--max-tokens", type=int, default=None)
    parser.set_defaults(enable_thinking=None, stream=None)
    parser.add_argument("--enable-thinking", dest="enable_thinking", action="store_true")
    parser.add_argument("--disable-thinking", dest="enable_thinking", action="store_false")
    parser.add_argument("--stream", dest="stream", action="store_true")
    parser.add_argument("--no-stream", dest="stream", action="store_false")
    parser.add_argument("--api-profile", action="append", default=None)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (OSError, ValueError, sqlite3.Error, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
