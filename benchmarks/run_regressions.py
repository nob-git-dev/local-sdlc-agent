#!/usr/bin/env python3
"""Run deterministic cross-domain regressions for the coding-agent harness."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _suite_result(name: str, cwd: Path, timeout: float) -> dict[str, object]:
    started = time.monotonic()
    completed = subprocess.run(
        ["python3", "-m", "unittest", "discover", "-s", "tests"],
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    output = completed.stdout + completed.stderr
    match = re.search(r"Ran\s+(\d+)\s+tests?", output)
    stable_lines = [
        line
        for line in output.strip().splitlines()
        if "[INFO]" not in line and "[WARNING]" not in line
    ]
    return {
        "name": name,
        "status": "pass" if completed.returncode == 0 else "fail",
        "returncode": completed.returncode,
        "test_count": int(match.group(1)) if match else None,
        "duration_seconds": round(time.monotonic() - started, 3),
        "tail": "\n".join(stable_lines[-8:]),
    }


def _tetris_false_positive_result(timeout: float) -> dict[str, object]:
    from local_sdlc.verification import run_html_smoke_checks

    project = ROOT / "benchmarks" / "runs" / "tetris" / "puzzle75b-tetris-20260716-0815"
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as temp:
        results = run_html_smoke_checks(
            project,
            ["tetris.html"],
            Path(temp),
            timeout,
            tetris_checks=True,
        )
    combined = "\n".join(document for document, _ok in results)
    rejected = bool(results) and not all(ok for _document, ok in results)
    expected_evidence = (
        "active piece did not visibly move after ArrowLeft" in combined
        and "board does not have 200 cells" in combined
    )
    return {
        "name": "tetris_false_positive_guard",
        "status": "pass" if rejected and expected_evidence else "fail",
        "expected_outcome": "known nonfunctional artifact is rejected",
        "harness_results": ["pass" if ok else "fail" for _document, ok in results],
        "observed_active_piece_motion_failure": "active piece did not visibly move after ArrowLeft" in combined,
        "observed_board_count_failure": "board does not have 200 cells" in combined,
        "duration_seconds": round(time.monotonic() - started, 3),
    }


def _unknown_scope_result() -> dict[str, object]:
    from local_sdlc.history import apply_regression_memories_to_stages, regression_memory_from_dict
    from local_sdlc.models import StageWorkItem

    fixture = ROOT / "tests" / "fixtures" / "regression_memory" / "tetris_active_piece.json"
    memory = regression_memory_from_dict(json.loads(fixture.read_text(encoding="utf-8")))
    stage = StageWorkItem(
        stage_id="S02",
        title="Core parser behavior",
        goal="Parse an unknown line-oriented format.",
        suggested_paths=("src/parser.py",),
        test_focus=("parser unit tests",),
    )
    updated = apply_regression_memories_to_stages([stage], [memory])
    leaked = any("active_piece" in item for item in updated[0].required_observables)
    return {
        "name": "unknown_domain_scope_guard",
        "status": "fail" if leaked else "pass",
        "domain_rule_leaked": leaked,
        "required_observables": list(updated[0].required_observables),
    }


def run_regressions(timeout: float = 120.0) -> dict[str, object]:
    checks = [
        _suite_result("mini_sqlite", ROOT / "benchmarks" / "mini-sqlite-engine", timeout),
        _suite_result("redis_kvs", ROOT / "benchmarks" / "redis-kvs", timeout),
        _tetris_false_positive_result(min(timeout, 30.0)),
        _unknown_scope_result(),
    ]
    return {
        "schema_version": 1,
        "status": "pass" if all(item["status"] == "pass" for item in checks) else "fail",
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run_regressions(args.timeout)
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output = args.output if args.output.is_absolute() else ROOT / args.output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
