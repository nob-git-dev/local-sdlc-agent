#!/usr/bin/env python3
"""Capture two verified P04/P05 recovery episodes through production controls."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from learning_runtime.audit import audit_run
from learning_runtime.collector import collect_run
from local_sdlc.action_gate import begin_action
from local_sdlc.budget import BudgetLimits, initialize_budget
from local_sdlc.progress_monitor import (
    ProgressPolicy,
    ProgressStalled,
    enforce_progress_deadline,
    initialize_progress_monitor,
    read_progress_state,
)
from local_sdlc.recovery import (
    begin_stalled_recovery,
    complete_stalled_recovery,
    plan_stalled_recovery,
    recovery_authorization,
)
from local_sdlc.utils import write_run_document
from local_sdlc.verification import (
    command_failure_family_signature,
    parse_command_result_document,
    run_checked_command,
)
from sdlc_events import EventType, RuntimeEventLedger


TEST_COMMAND = "python3 -B -m unittest discover -s tests"
RECOVERY_EVENT_TYPES = (
    EventType.RECOVERY_PLANNED.value,
    EventType.RECOVERY_STARTED.value,
    EventType.RECOVERY_COMPLETED.value,
)


@dataclass(frozen=True)
class CaptureScenario:
    name: str
    module_name: str
    test_name: str
    broken_source: str
    fixed_source: str
    test_source: str
    analysis_available: bool


SCENARIOS = (
    CaptureScenario(
        "calculator-assertion",
        "calculator.py",
        "test_add",
        "def add(left, right):\n    return left - right\n",
        "def add(left, right):\n    return left + right\n",
        (
            "import unittest\nfrom calculator import add\n\n"
            "class BehaviorTests(unittest.TestCase):\n"
            "    def test_add(self):\n"
            "        self.assertEqual(add(7, 5), 12)\n"
        ),
        False,
    ),
    CaptureScenario(
        "normalizer-assertion",
        "normalizer.py",
        "test_trim",
        "def normalize(value):\n    return value\n",
        "def normalize(value):\n    return value.strip()\n",
        (
            "import unittest\nfrom normalizer import normalize\n\n"
            "class BehaviorTests(unittest.TestCase):\n"
            "    def test_trim(self):\n"
            "        self.assertEqual(normalize('  ready  '), 'ready')\n"
        ),
        True,
    ),
)


def _json_write(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _initialize_run(run_dir: Path, now: float) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    initialize_budget(
        run_dir,
        BudgetLimits(
            max_goal_actions=32,
            max_stage_actions=16,
            max_recovery_actions=16,
            max_api_calls=4,
            max_wall_seconds=120.0,
        ),
        scope_kind="goal",
        now=now,
    )
    initialize_progress_monitor(
        run_dir,
        ProgressPolicy(max_idle_seconds=1.0),
        scope_kind="goal",
        now=now,
    )


def _causal_chain_is_valid(events: list[object]) -> bool:
    if [event.event_type for event in events] != list(RECOVERY_EVENT_TYPES):
        return False
    planned, started, completed = events
    return (
        bool(planned.causation_id)
        and started.causation_id == planned.event_id
        and completed.causation_id == started.event_id
        and len({event.aggregate_id for event in events}) == 1
    )


def _capture_one(
    root: Path,
    store_dir: Path,
    scenario: CaptureScenario,
    index: int,
) -> dict[str, object]:
    scenario_root = root / f"{index:02d}-{scenario.name}"
    project = scenario_root / "project"
    source_run = scenario_root / "runs" / "source"
    target_run = scenario_root / "runs" / "recovery"
    tests_dir = project / "tests"
    tests_dir.mkdir(parents=True)
    (project / scenario.module_name).write_text(scenario.broken_source, encoding="utf-8")
    (tests_dir / "test_behavior.py").write_text(scenario.test_source, encoding="utf-8")

    base_time = 1_800_000_000.0 + index * 100.0
    _initialize_run(source_run, base_time)
    failed_document, failed_ok = run_checked_command(
        project,
        TEST_COMMAND,
        30.0,
        run_dir=source_run,
        action=f"capture_{scenario.name}_failing_test",
        progress_dirs=(),
        metadata={"capture_scenario": scenario.name},
    )
    if failed_ok:
        raise RuntimeError(f"capture precondition unexpectedly passed: {scenario.name}")
    write_run_document(source_run, "01-failing-test.md", failed_document)
    family = command_failure_family_signature([("acceptance", failed_document)])
    if not family:
        raise RuntimeError(f"failure family was not derived: {scenario.name}")

    analyses: list[dict[str, object]] = []
    if scenario.analysis_available:
        analyses.append(
            {
                "analysis_status": "completed",
                "failure_family_signature": family,
                "root_cause": f"{scenario.module_name} violates its tested behavior",
            }
        )
    _json_write(
        source_run / "run.json",
        {
            "schema_version": 1,
            "final_verdict": "test_failed",
            "last_functional_failure_family_signature": family,
            "repeated_same_failure_count": 1,
            "failure_analyses": analyses,
            "changed_paths": [],
        },
    )

    progress = read_progress_state(source_run)
    try:
        enforce_progress_deadline(
            source_run,
            f"capture_{scenario.name}_idle_deadline",
            now=float(progress["last_progress_at_epoch"]) + 2.0,
        )
    except ProgressStalled:
        pass
    else:
        raise RuntimeError(f"production progress monitor did not stall: {scenario.name}")

    plan = plan_stalled_recovery(
        source_run,
        requested_strategy="retry",
        failure_family_threshold=2,
        target_run_dir=target_run,
    )
    expected = "root_cause_recovery" if scenario.analysis_available else "failure_analysis"
    if plan["strategy"] != expected:
        raise RuntimeError(f"unexpected recovery strategy: {plan['strategy']}")

    _initialize_run(target_run, base_time + 3.0)
    begin_stalled_recovery(
        source_run,
        target_run,
        plan,
        cancel_dirs=(source_run,),
        budget_dirs=(source_run,),
        progress_dirs=(),
    )
    authorization = recovery_authorization(plan)
    begin_action(
        target_run,
        f"capture_{scenario.name}_atomic_fix",
        action_type="artifact_apply",
        risk_class="project_write",
        metadata={
            "isolated": True,
            "child_run_dir": str(target_run.resolve()),
            "recovery_authorization": authorization,
        },
        cancel_dirs=(source_run,),
        budget_dirs=(source_run,),
        progress_dirs=(),
    )
    (project / scenario.module_name).write_text(scenario.fixed_source, encoding="utf-8")

    passed_document, passed_ok = run_checked_command(
        project,
        TEST_COMMAND,
        30.0,
        run_dir=target_run,
        action=f"capture_{scenario.name}_recovery_test",
        cancel_dirs=(source_run,),
        budget_dirs=(source_run,),
        progress_dirs=(),
        metadata={"recovery_authorization": authorization},
    )
    if not passed_ok:
        raise RuntimeError(f"recovery verification failed: {scenario.name}\n{passed_document}")
    write_run_document(target_run, "01-recovery-test.md", passed_document)
    _json_write(
        target_run / "run.json",
        {
            "schema_version": 1,
            "final_verdict": "approved",
            "resumed_from": str(source_run.resolve()),
            "recovery_plan_id": plan["plan_id"],
            "changed_paths": [scenario.module_name],
            "acceptance_matrix": [
                {
                    "criterion_id": f"AC-{scenario.test_name}",
                    "status": "pass",
                    "evidence_ids": ["recovery-command"],
                }
            ],
        },
    )
    complete_stalled_recovery(
        source_run,
        target_run,
        outcome="completed",
        changed_paths=(scenario.module_name,),
        change_isolation="isolated",
    )

    ledger = RuntimeEventLedger(source_run)
    recovery_events = [
        event for event in ledger.list_events() if event.event_type in RECOVERY_EVENT_TYPES
    ]
    causal_chain_valid = _causal_chain_is_valid(recovery_events)
    completion = json.loads(
        (source_run / "recovery_completion_evidence.json").read_text(encoding="utf-8")
    )
    audit = audit_run(source_run, import_legacy=False)
    collection = collect_run(source_run, data_dir=store_dir, import_legacy=False)
    if audit["status"] != "pass" or collection["status"] != "pass":
        raise RuntimeError(f"event capture integrity failed: {scenario.name}")
    if not causal_chain_valid or not completion.get("verification_passed"):
        raise RuntimeError(f"recovery evidence is incomplete: {scenario.name}")

    return {
        "scenario": scenario.name,
        "failure_family": family,
        "strategy": plan["strategy"],
        "source_test_status": parse_command_result_document(failed_document).get("status"),
        "recovery_test_status": parse_command_result_document(passed_document).get("status"),
        "event_types": [event.event_type for event in recovery_events],
        "causal_chain_valid": causal_chain_valid,
        "completion_verified": completion["verification_passed"],
        "atomic_change": completion["atomic_change"],
        "change_isolation": completion["change_isolation"],
        "changed_paths": completion["changed_paths"],
        "audit_status": audit["status"],
        "outbox": collection["outbox"],
        "source_run_dir": str(source_run.resolve()),
        "target_run_dir": str(target_run.resolve()),
    }


def capture_recovery_episodes(output_dir: Path) -> dict[str, object]:
    output = output_dir.expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"capture output is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    store_dir = output / "learning-store"
    episodes = [
        _capture_one(output, store_dir, scenario, index)
        for index, scenario in enumerate(SCENARIOS, start=1)
    ]
    if len({str(item["failure_family"]) for item in episodes}) != len(episodes):
        raise RuntimeError("capture scenarios did not produce distinct failure families")
    report: dict[str, object] = {
        "schema_version": 1,
        "status": "pass",
        "captured_at": dt.datetime.now(tz=dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "episode_count": len(episodes),
        "episodes": episodes,
    }
    _json_write(output / "capture-report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".sdlc-runner") / "learning-evidence" / (
            "p04-p05-" + dt.datetime.now(tz=dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
        ),
    )
    args = parser.parse_args()
    print(json.dumps(capture_recovery_episodes(args.output), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
