from __future__ import annotations

import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest

from learning_runtime.audit import audit_run
from learning_runtime.collector import collect_run
from learning_runtime.inventory import MUTATION_CONTRACTS, validate_mutation_inventory
from learning_runtime.legacy import import_legacy_run
from learning_runtime.privacy import sensitive_values
from learning_runtime.storage import ExperienceStore
from sdlc_events import (
    EVENT_CONTRACTS,
    EventType,
    InjectedLedgerFault,
    RuntimeEventLedger,
    TransitionKind,
    TransitionRequest,
    validate_contract_registry,
)

from local_sdlc.budget import BudgetLimits, consume_action_budget, initialize_budget
from local_sdlc.control import cancel_requested, request_cancel
from local_sdlc.history import (
    persist_regression_memories_for_manifest,
    regression_memories_from_manifest,
)
from local_sdlc.progress_monitor import ProgressPolicy, initialize_progress_monitor
from local_sdlc.safety import action_safety_decision, record_safety_decision
from local_sdlc.utils import write_run_document


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "learning_runtime"


class LearningRuntimeTests(unittest.TestCase):
    def request(
        self,
        ledger: RuntimeEventLedger,
        transition: TransitionKind = TransitionKind.RUN_STARTED,
        *,
        source_key: str = "test:run_started",
        payload: dict[str, object] | None = None,
    ) -> TransitionRequest:
        aggregate_type = EVENT_CONTRACTS[transition].aggregate_type
        aggregate_id = ledger.run_id if aggregate_type == "goal" else f"{aggregate_type}-1"
        return TransitionRequest(
            transition_kind=transition.value,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            source_component="test",
            source_key=source_key,
            payload=payload or {},
        )

    def copy_fixture(self, name: str, target: Path) -> Path:
        run_dir = target / "run"
        shutil.copytree(FIXTURES / name, run_dir)
        return run_dir

    def test_el01_transition_contract_registry_is_exhaustive(self):
        self.assertEqual(validate_contract_registry(), [])
        self.assertEqual(set(EVENT_CONTRACTS), set(TransitionKind))

    def test_el01_mutation_inventory_has_hooks_and_registered_transitions(self):
        self.assertEqual(validate_mutation_inventory(ROOT), [])
        self.assertGreaterEqual(len(MUTATION_CONTRACTS), 11)

    def test_el02_transition_event_and_outbox_commit_atomically(self):
        with tempfile.TemporaryDirectory() as temp:
            ledger = RuntimeEventLedger(Path(temp) / "run")
            event = ledger.commit_transition(self.request(ledger))

            self.assertEqual(ledger.transition_count(), 1)
            self.assertEqual(len(ledger.list_events()), 1)
            self.assertEqual(ledger.outbox_status(), {"pending": 1, "delivered": 0})
            self.assertTrue(event.verify_hash())

    def test_el02_fault_before_commit_leaves_no_transition_or_outbox(self):
        with tempfile.TemporaryDirectory() as temp:
            ledger = RuntimeEventLedger(Path(temp) / "run")

            with self.assertRaises(InjectedLedgerFault):
                ledger.commit_transition(self.request(ledger), fault_at="before_commit")

            self.assertEqual(ledger.transition_count(), 0)
            self.assertEqual(ledger.list_events(), [])
            self.assertEqual(ledger.outbox_status(), {"pending": 0, "delivered": 0})

    def test_el02_fault_after_commit_leaves_replayable_outbox(self):
        with tempfile.TemporaryDirectory() as temp:
            ledger = RuntimeEventLedger(Path(temp) / "run")

            with self.assertRaises(InjectedLedgerFault):
                ledger.commit_transition(self.request(ledger), fault_at="after_commit")

            self.assertEqual(ledger.transition_count(), 1)
            self.assertEqual(len(ledger.pending_outbox()), 1)

    def test_el02_source_key_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            ledger = RuntimeEventLedger(Path(temp) / "run")
            first = ledger.commit_transition(self.request(ledger))
            second = ledger.commit_transition(self.request(ledger))

            self.assertEqual(first.event_id, second.event_id)
            self.assertEqual(ledger.transition_count(), 1)

    def test_el03_collector_can_be_absent_until_after_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = root / "run"
            ledger = RuntimeEventLedger(run_dir)
            event = ledger.commit_transition(self.request(ledger))

            self.assertEqual(ledger.pending_outbox()[0].event_id, event.event_id)
            report = collect_run(run_dir, data_dir=root / "learning", import_legacy=False)

            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["inserted_count"], 1)
            self.assertEqual(ledger.outbox_status(), {"pending": 0, "delivered": 1})

    def test_el04_collection_is_idempotent_after_store_before_ack(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = root / "run"
            ledger = RuntimeEventLedger(run_dir)
            event = ledger.commit_transition(
                self.request(
                    ledger,
                    payload={
                        "workspace": "/home/example/private-project",
                        "email": "person@example.com",
                        "api_key": "secret-value",
                    },
                )
            )
            store = ExperienceStore(root / "learning")
            self.assertTrue(store.put_event(event))

            report = collect_run(run_dir, data_dir=root / "learning", import_legacy=False)

            self.assertEqual(report["inserted_count"], 0)
            self.assertEqual(report["duplicate_count"], 1)
            self.assertEqual(store.event_count(), 1)
            self.assertEqual(sensitive_values(store.events()), [])

    def test_el04_integrity_audit_detects_sequence_tampering(self):
        with tempfile.TemporaryDirectory() as temp:
            ledger = RuntimeEventLedger(Path(temp) / "run")
            ledger.commit_transition(self.request(ledger, source_key="test:first"))
            ledger.commit_transition(
                self.request(
                    ledger,
                    TransitionKind.RUN_PLANNED,
                    source_key="test:second",
                )
            )
            with sqlite3.connect(ledger.path) as connection:
                connection.execute("UPDATE events SET sequence = 9 WHERE sequence = 2")

            codes = {item["code"] for item in ledger.integrity_findings()}
            self.assertIn("aggregate_sequence_gap", codes)

    def test_el04_integrity_audit_reports_malformed_envelope_without_crashing(self):
        with tempfile.TemporaryDirectory() as temp:
            ledger = RuntimeEventLedger(Path(temp) / "run")
            ledger.commit_transition(self.request(ledger))
            with sqlite3.connect(ledger.path) as connection:
                connection.execute("UPDATE events SET envelope_json = '{broken'")

            codes = {item["code"] for item in ledger.integrity_findings()}
            report = audit_run(ledger.run_dir, import_legacy=False, persist_violation=False)

            self.assertIn("invalid_event_envelope", codes)
            self.assertEqual(report["status"], "fail")
            self.assertEqual(report["event_count"], 1)

    def test_el05_closure_audit_persists_explicit_violation(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            ledger = RuntimeEventLedger(run_dir)
            ledger.commit_transition(
                self.request(
                    ledger,
                    TransitionKind.RUN_TERMINATED,
                    source_key="test:terminated",
                    payload={"verification_expected": False},
                )
            )

            report = audit_run(run_dir, import_legacy=False)

            self.assertEqual(report["status"], "fail")
            self.assertIn("run_closed_without_start", {item["code"] for item in report["findings"]})
            self.assertTrue(report.get("violation_event_id"))
            self.assertIn(
                EventType.EVENT_CONTRACT_VIOLATION.value,
                {event.event_type for event in ledger.list_events()},
            )

    def test_el05_manifest_writer_records_start_verification_and_termination(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            payload = {"final_verdict": "approved", "api_calls": 1, "completed_rounds": 1}

            write_run_document(run_dir, "run.json", json.dumps(payload))
            ledger = RuntimeEventLedger(run_dir)
            event_types = {event.event_type for event in ledger.list_events()}

            self.assertIn(EventType.RUN_STARTED.value, event_types)
            self.assertIn(EventType.VERIFICATION_COMPLETED.value, event_types)
            self.assertIn(EventType.RUN_TERMINATED.value, event_types)
            self.assertEqual(ledger.integrity_findings(), [])

    def test_el05_stage_manifest_closes_both_stage_and_run_contracts(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            initialize_progress_monitor(
                run_dir,
                ProgressPolicy(max_idle_seconds=60),
                scope_kind="stage",
                now=1,
            )

            write_run_document(run_dir, "run.json", json.dumps({"final_verdict": "approved"}))
            ledger = RuntimeEventLedger(run_dir)
            event_types = {event.event_type for event in ledger.list_events()}

            self.assertIn(EventType.RUN_STARTED.value, event_types)
            self.assertIn(EventType.STAGE_STARTED.value, event_types)
            self.assertIn(EventType.STAGE_CLOSED.value, event_types)
            self.assertIn(EventType.RUN_TERMINATED.value, event_types)
            self.assertEqual(ledger.integrity_findings(), [])

    def test_el05_cancel_is_durable_even_if_projection_is_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            request_cancel(run_dir, source="test", reason="stop-now")
            (run_dir / "cancel.json").unlink()

            self.assertTrue(cancel_requested(run_dir))
            event_types = {event.event_type for event in RuntimeEventLedger(run_dir).list_events()}
            self.assertIn(EventType.CANCELLATION_REQUESTED.value, event_types)

    def test_el05_safety_and_budget_writers_emit_through_gateway(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            decision = action_safety_decision(
                "inspect",
                action_type="command",
                risk_class="read_only",
                metadata={"action_id": "action-safe"},
            )
            record_safety_decision(run_dir, decision)
            initialize_budget(run_dir, BudgetLimits(max_goal_actions=2), scope_kind="goal", now=0)
            consume_action_budget(
                run_dir,
                "inspect",
                action_type="command",
                action_id="action-safe",
                now=1,
            )

            event_types = {event.event_type for event in RuntimeEventLedger(run_dir).list_events()}
            self.assertIn(EventType.SAFETY_DECISION_RECORDED.value, event_types)
            self.assertIn(EventType.BUDGET_CONSUMED.value, event_types)

    def test_el06_new_regression_memory_is_recorded_before_projection(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            run_dir = project / "run"
            manifest = json.loads(
                (FIXTURES / "legacy_run" / "run.json").read_text(encoding="utf-8")
            )

            result = persist_regression_memories_for_manifest(project, run_dir, manifest)
            events = RuntimeEventLedger(run_dir).list_events()

            self.assertIsNotNone(result)
            self.assertIn(
                EventType.REGRESSION_MEMORY_RECORDED.value,
                {event.event_type for event in events},
            )

    def test_el06_new_failure_analysis_is_recorded_before_projection(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            analysis = {
                "failure_type": "persistence_loss",
                "rejected_hypotheses": [
                    {"hypothesis": "presentation_only", "reason": "state is absent"}
                ],
            }

            write_run_document(
                run_dir,
                "05-r01-failure-analysis.json",
                json.dumps(analysis),
            )
            event_types = {
                event.event_type for event in RuntimeEventLedger(run_dir).list_events()
            }

            self.assertIn(EventType.FAILURE_CLASSIFIED.value, event_types)
            self.assertIn(EventType.HYPOTHESIS_REJECTED.value, event_types)

    def test_el06_legacy_fixture_imports_all_evidence_families_with_provenance(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = self.copy_fixture("legacy_run", Path(temp))

            report = import_legacy_run(run_dir)
            events = RuntimeEventLedger(run_dir).list_events()
            event_types = {event.event_type for event in events}

            self.assertEqual(report["status"], "pass")
            self.assertIn(EventType.ACTION_ADMITTED.value, event_types)
            self.assertIn(EventType.APPROVAL_REQUIRED.value, event_types)
            self.assertIn(EventType.APPROVAL_GRANTED.value, event_types)
            self.assertIn(EventType.BUDGET_EXHAUSTED.value, event_types)
            self.assertIn(EventType.GOAL_STALLED.value, event_types)
            self.assertIn(EventType.FAILURE_CLASSIFIED.value, event_types)
            self.assertIn(EventType.HYPOTHESIS_REJECTED.value, event_types)
            self.assertIn(EventType.REGRESSION_MEMORY_RECORDED.value, event_types)
            self.assertIn(EventType.RUN_TERMINATED.value, event_types)
            self.assertTrue(all(ref.sha256 for event in events for ref in event.evidence_refs))
            self.assertEqual(RuntimeEventLedger(run_dir).integrity_findings(), [])

    def test_el06_malformed_legacy_line_is_reported_not_silently_skipped(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = self.copy_fixture("legacy_malformed", Path(temp))

            report = import_legacy_run(run_dir)

            self.assertEqual(report["status"], "integrity_failed")
            malformed = [item for item in report["findings"] if item["code"] == "legacy_jsonl_malformed"]
            self.assertEqual(malformed[0]["source"], "progress.jsonl")
            self.assertEqual(malformed[0]["line"], 2)

    def test_el06_malformed_failure_analysis_is_an_explicit_finding(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = self.copy_fixture("legacy_malformed", Path(temp))
            (run_dir / "05-r01-failure-analysis.json").write_text(
                "{broken\n", encoding="utf-8"
            )

            report = import_legacy_run(run_dir)

            malformed = [
                item
                for item in report["findings"]
                if item["code"] == "legacy_json_malformed"
            ]
            self.assertEqual(malformed[0]["source"], "05-r01-failure-analysis.json")

    def test_el06_failure_analysis_regression_memory_is_no_longer_dropped(self):
        manifest = json.loads((FIXTURES / "legacy_run" / "run.json").read_text(encoding="utf-8"))

        memories = regression_memories_from_manifest(manifest)

        analysis_memory = next(item for item in memories if item.scope.get("kind") == "failure_family")
        self.assertEqual(analysis_memory.failure_family, "family:persistence-loss-after-reopen")
        self.assertIn("presentation-only defect", analysis_memory.false_positive_pattern)
        self.assertIn("required_path:src/storage.py", analysis_memory.required_future_observables)

    def test_learning_cli_doctor_and_audit_are_independent_entrypoints(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.copy_fixture("legacy_run", root)
            doctor = subprocess.run(
                [
                    "python3",
                    str(ROOT / "local_sdlc_learning.py"),
                    "doctor",
                    "--data-dir",
                    str(root / "learning"),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            audit = subprocess.run(
                [
                    "python3",
                    str(ROOT / "local_sdlc_learning.py"),
                    "audit",
                    "--run-dir",
                    str(run_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(doctor.returncode, 0, doctor.stderr)
            self.assertEqual(audit.returncode, 0, audit.stderr)


if __name__ == "__main__":
    unittest.main()
