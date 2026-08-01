import json
import tempfile
from pathlib import Path
from unittest import mock

from sdlc_events import EventType, RuntimeEventLedger

from tests.helpers import LocalSDLCTestCase


class RecoveryRuntimeTests(LocalSDLCTestCase):
    def make_stalled_run(
        self,
        root: Path,
        name: str = "run",
        *,
        manifest: dict[str, object] | None = None,
        max_recovery_actions: int = 4,
    ) -> Path:
        run_dir = root / name
        self.local_sdlc.initialize_budget(
            run_dir,
            self.local_sdlc.BudgetLimits(max_recovery_actions=max_recovery_actions),
            scope_kind="goal_stage",
        )
        self.local_sdlc.initialize_progress_monitor(
            run_dir,
            self.local_sdlc.ProgressPolicy(max_idle_seconds=10.0),
            scope_kind="goal_stage",
            now=100.0,
        )
        payload = {
            "brief": "repair the project",
            "command": "agent",
            "status": "stalled",
            "final_verdict": "stalled",
            "completed_rounds": 1,
            **(manifest or {}),
        }
        self.local_sdlc.write_run_document(
            run_dir,
            "run.partial.json",
            json.dumps(payload),
        )
        with self.assertRaises(self.local_sdlc.ProgressStalled):
            self.local_sdlc.enforce_progress_deadline(
                run_dir,
                "watchdog",
                now=110.0,
            )
        return run_dir

    def test_valid_stall_plan_is_persisted_and_emits_canonical_event(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.make_stalled_run(root)
            target = root / "run-recovery-01"

            plan = self.local_sdlc.plan_stalled_recovery(
                source,
                target_run_dir=target,
            )
            loaded = self.local_sdlc.read_recovery_plan(source)
            events = RuntimeEventLedger(source).list_events()

        self.assertEqual(plan, loaded)
        self.assertEqual(plan["status"], "RECOVERY_PLANNED")
        self.assertEqual(plan["strategy"], "resume")
        self.assertEqual(plan["source_run_dir"], str(source.resolve()))
        self.assertEqual(plan["target_run_dir"], str(target.resolve()))
        self.assertEqual(len(plan["source_stall_sha256"]), 64)
        self.assertIn(EventType.RECOVERY_PLANNED.value, [event.event_type for event in events])

    def test_stalled_source_requires_a_plan_and_tampered_evidence_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.make_stalled_run(root)
            target = root / "recovery"

            with self.assertRaises(self.local_sdlc.RecoveryPlanRequired):
                self.local_sdlc.require_recovery_plan_for_resume(source, None)

            plan = self.local_sdlc.plan_stalled_recovery(
                source,
                target_run_dir=target,
            )
            stall = self.local_sdlc.read_stall_state(source)
            stall["reason"] = "tampered"
            self.local_sdlc.stall_file_path(source).write_text(
                json.dumps(stall),
                encoding="utf-8",
            )
            with self.assertRaises(self.local_sdlc.InvalidRecoveryPlan):
                self.local_sdlc.validate_recovery_plan(source, plan)

    def test_valid_recovery_start_obeys_cancel_and_recovery_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cancelled = self.make_stalled_run(root, "cancelled")
            cancel_target = root / "cancel-target"
            cancel_plan = self.local_sdlc.plan_stalled_recovery(
                cancelled,
                target_run_dir=cancel_target,
            )
            self.local_sdlc.request_cancel(cancelled)
            with self.assertRaises(self.local_sdlc.RunnerError):
                self.local_sdlc.begin_stalled_recovery(
                    cancelled,
                    cancel_target,
                    cancel_plan,
                )

            exhausted = self.make_stalled_run(
                root,
                "exhausted",
                max_recovery_actions=0,
            )
            exhausted_target = root / "exhausted-target"
            exhausted_plan = self.local_sdlc.plan_stalled_recovery(
                exhausted,
                target_run_dir=exhausted_target,
            )
            with self.assertRaises(self.local_sdlc.BudgetExceeded):
                self.local_sdlc.begin_stalled_recovery(
                    exhausted,
                    exhausted_target,
                    exhausted_plan,
                )

            cancel_work = [
                event
                for event in self.local_sdlc.read_progress_events(cancelled)
                if event.get("starts_work")
            ]
            exhausted_work = [
                event
                for event in self.local_sdlc.read_progress_events(exhausted)
                if event.get("starts_work")
            ]

        self.assertEqual(cancel_work, [])
        self.assertEqual(exhausted_work, [])

    def test_recovery_start_records_authorization_and_keeps_ordinary_stall_absorbing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.make_stalled_run(root)
            target = root / "recovery"
            self.local_sdlc.initialize_budget(
                target,
                self.local_sdlc.BudgetLimits(),
                scope_kind="goal_stage",
            )
            self.local_sdlc.initialize_progress_monitor(
                target,
                self.local_sdlc.ProgressPolicy(),
                scope_kind="goal_stage",
            )
            plan = self.local_sdlc.plan_stalled_recovery(
                source,
                target_run_dir=target,
            )

            started = self.local_sdlc.begin_stalled_recovery(source, target, plan)
            with self.assertRaises(self.local_sdlc.ProgressStalled):
                self.local_sdlc.begin_action(
                    source,
                    "ordinary_retry",
                    action_type="retry",
                    risk_class="read_only",
                )
            audit = self.local_sdlc.action_gate_audit(source)
            source_work = [
                event
                for event in self.local_sdlc.read_progress_events(source)
                if event.get("starts_work")
            ]
            recovery_events = RuntimeEventLedger(source).list_events()

        self.assertEqual(started["status"], "RESUMED")
        self.assertEqual(len(source_work), 1)
        authorization = source_work[0]["metadata"]["recovery_authorization"]
        self.assertEqual(authorization["plan_id"], plan["plan_id"])
        self.assertEqual(audit["status"], "pass")
        self.assertTrue(audit["stall_absorbing"])
        self.assertIn(EventType.RECOVERY_STARTED.value, [event.event_type for event in recovery_events])

    def test_forged_recovery_metadata_does_not_bypass_stall_audit(self):
        with tempfile.TemporaryDirectory() as temp:
            source = self.make_stalled_run(Path(temp))
            self.local_sdlc.record_work_start(
                source,
                "forged_recovery",
                metadata={
                    "recovery_authorization": {
                        "plan_id": "forged",
                        "plan_path": str(source / "recovery_plan.json"),
                    }
                },
            )
            audit = self.local_sdlc.action_gate_audit(source)

        self.assertEqual(audit["status"], "fail")
        self.assertFalse(audit["stall_absorbing"])
        self.assertEqual(len(audit["stall_violations"]), 1)

    def test_recovery_rechecks_stall_evidence_at_work_start_and_refunds_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.make_stalled_run(root)
            target = root / "recovery"
            self.local_sdlc.initialize_budget(
                target,
                self.local_sdlc.BudgetLimits(),
                scope_kind="goal_stage",
            )
            self.local_sdlc.initialize_progress_monitor(
                target,
                self.local_sdlc.ProgressPolicy(),
                scope_kind="goal_stage",
            )
            plan = self.local_sdlc.plan_stalled_recovery(
                source,
                target_run_dir=target,
            )
            from local_sdlc import action_gate

            original = action_gate.start_progress_action

            def tamper_then_start(*args, **kwargs):
                stall = self.local_sdlc.read_stall_state(source)
                stall["reason"] = "changed before work start"
                self.local_sdlc.stall_file_path(source).write_text(
                    json.dumps(stall),
                    encoding="utf-8",
                )
                return original(*args, **kwargs)

            with mock.patch(
                "local_sdlc.action_gate.start_progress_action",
                side_effect=tamper_then_start,
            ):
                with self.assertRaises(self.local_sdlc.InvalidRecoveryPlan):
                    self.local_sdlc.begin_stalled_recovery(source, target, plan)

            source_budget = self.local_sdlc.budget_status(source)
            target_budget = self.local_sdlc.budget_status(target)
            target_work = [
                event
                for event in self.local_sdlc.read_progress_events(target)
                if event.get("starts_work")
            ]

        self.assertEqual(source_budget["usage"]["recovery_actions"], 0)
        self.assertEqual(target_budget["usage"]["recovery_actions"], 0)
        self.assertEqual(target_work, [])

    def test_failure_plateau_forces_analysis_then_root_cause(self):
        family = "python3 -m unittest|test_assertion_failed|tests.test_core.test_insert"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.make_stalled_run(
                root,
                manifest={
                    "last_functional_failure_family_signature": family,
                    "repeated_same_failure_count": 1,
                },
            )
            analysis_plan = self.local_sdlc.plan_stalled_recovery(
                source,
                requested_strategy="retry",
                failure_family_threshold=2,
                target_run_dir=root / "analysis",
            )

        self.assertEqual(analysis_plan["requested_strategy"], "retry")
        self.assertEqual(analysis_plan["strategy"], "failure_analysis")
        self.assertGreaterEqual(analysis_plan["failure_family_count"], 2)
        self.assertTrue(analysis_plan["plateau_detected"])

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            previous = root / "previous-analysis"
            previous.mkdir()
            (previous / "run.json").write_text(
                json.dumps(
                    {
                        "command": "agent",
                        "last_functional_failure_family_signature": family,
                        "failure_analyses": [
                            {
                                "analysis_status": "completed",
                                "failure_signature": family,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            source = self.make_stalled_run(
                root,
                manifest={
                    "resumed_from": str(previous),
                    "last_functional_failure_family_signature": family,
                    "repeated_same_failure_count": 1,
                },
            )
            root_cause_plan = self.local_sdlc.plan_stalled_recovery(
                source,
                requested_strategy="retry",
                failure_family_threshold=2,
                target_run_dir=root / "root-cause",
            )

        self.assertEqual(root_cause_plan["strategy"], "root_cause_recovery")
        self.assertTrue(root_cause_plan["analysis_available"])

    def test_different_failure_families_do_not_trigger_plateau(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            previous = root / "previous"
            previous.mkdir()
            (previous / "run.json").write_text(
                json.dumps(
                    {
                        "command": "agent",
                        "last_functional_failure_family_signature": "family-a",
                    }
                ),
                encoding="utf-8",
            )
            source = self.make_stalled_run(
                root,
                manifest={
                    "resumed_from": str(previous),
                    "last_functional_failure_family_signature": "family-b",
                    "repeated_same_failure_count": 0,
                },
            )
            plan = self.local_sdlc.plan_stalled_recovery(
                source,
                requested_strategy="retry",
                failure_family_threshold=2,
                target_run_dir=root / "retry",
            )

        self.assertFalse(plan["plateau_detected"])
        self.assertEqual(plan["strategy"], "retry")
        self.assertEqual(plan["failure_family_count"], 1)

    def test_agent_resume_from_stall_requires_plan_and_records_recovery_origin(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root)
            (project / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
            source = self.make_stalled_run(project, "source")
            target = project / "recovered"
            parser = self.local_sdlc.build_parser()

            plain_args = parser.parse_args(
                [
                    "agent",
                    "verify existing code",
                    "--project",
                    str(project),
                    "--skills-dir",
                    str(skills_dir),
                    "--include",
                    "app.py",
                    "--resume",
                    str(source),
                    "--run-dir",
                    str(target),
                ]
            )
            with self.assertRaises(self.local_sdlc.RecoveryPlanRequired):
                self.local_sdlc.command_agent(plain_args)

            plan = self.local_sdlc.plan_stalled_recovery(
                source,
                target_run_dir=target,
            )
            mismatched_target = project / "mismatched-target"
            mismatched_args = parser.parse_args(
                [
                    "agent",
                    "verify existing code",
                    "--project",
                    str(project),
                    "--skills-dir",
                    str(skills_dir),
                    "--include",
                    "app.py",
                    "--resume",
                    str(source),
                    "--recovery-plan",
                    str(self.local_sdlc.recovery_plan_file_path(source)),
                    "--run-dir",
                    str(mismatched_target),
                ]
            )
            with self.assertRaises(self.local_sdlc.InvalidRecoveryPlan):
                self.local_sdlc.command_agent(mismatched_args)
            self.assertFalse(mismatched_target.exists())

            recovery_args = parser.parse_args(
                [
                    "agent",
                    "verify existing code",
                    "--project",
                    str(project),
                    "--skills-dir",
                    str(skills_dir),
                    "--include",
                    "app.py",
                    "--resume",
                    str(source),
                    "--recovery-plan",
                    str(self.local_sdlc.recovery_plan_file_path(source)),
                    "--run-dir",
                    str(target),
                    "--precheck",
                    "--test-command",
                    "python3 -c pass",
                    "--judge-mode",
                    "command-only",
                ]
            )
            with mock.patch(
                "local_sdlc.agent_runner.LocalLLMClient.complete",
                return_value=json.dumps(
                    {
                        "artifacts": [
                            {
                                "type": "replace_file",
                                "path": "app.py",
                                "content": "VALUE = 1\n",
                            }
                        ]
                    }
                ),
            ):
                result = self.local_sdlc.command_agent(recovery_args)

            manifest = json.loads((target / "run.json").read_text(encoding="utf-8"))
            origin = json.loads((target / "recovery_origin.json").read_text(encoding="utf-8"))
            state = json.loads((source / "recovery_state.json").read_text(encoding="utf-8"))
            completion_evidence = json.loads(
                (source / "recovery_completion_evidence.json").read_text(encoding="utf-8")
            )
            recovery_events = RuntimeEventLedger(source).list_events()

        self.assertEqual(result, 0)
        self.assertEqual(manifest["resumed_from"], str(source.resolve()))
        self.assertEqual(manifest["recovery_plan_id"], plan["plan_id"])
        self.assertEqual(origin["strategy"], "resume")
        self.assertEqual(state["status"], "RECOVERY_COMPLETED")
        self.assertEqual(state["outcome"], "completed")
        self.assertTrue(completion_evidence["verification_passed"])
        self.assertEqual(completion_evidence["target_final_verdict"], "approved")
        completed_event = next(
            event
            for event in recovery_events
            if event.event_type == EventType.RECOVERY_COMPLETED.value
        )
        self.assertEqual(
            [reference.path for reference in completed_event.evidence_refs],
            ["recovery_completion_evidence.json"],
        )

    def test_profile_switch_plan_rejects_a_conflicting_model_profile_before_work(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root)
            (project / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
            source = self.make_stalled_run(project, "source")
            target = project / "profile-recovery"
            self.local_sdlc.plan_stalled_recovery(
                source,
                requested_strategy="profile_switch",
                target_profile="qwen-agent",
                target_run_dir=target,
            )
            args = self.local_sdlc.build_parser().parse_args(
                [
                    "agent",
                    "verify existing code",
                    "--project",
                    str(project),
                    "--skills-dir",
                    str(skills_dir),
                    "--include",
                    "app.py",
                    "--resume",
                    str(source),
                    "--recovery-plan",
                    str(self.local_sdlc.recovery_plan_file_path(source)),
                    "--run-dir",
                    str(target),
                    "--model-profile",
                    "ornith-agent",
                ]
            )

            with self.assertRaisesRegex(
                self.local_sdlc.InvalidRecoveryPlan,
                "target_profile",
            ):
                self.local_sdlc.command_agent(args)

        self.assertFalse(target.exists())

    def test_plateau_recovery_runs_failure_analysis_before_any_patch_call(self):
        family = "python3 -m unittest|test_assertion_failed|tests.test_core.test_insert"
        calls: list[str] = []

        def fake_skill_call(**kwargs):
            call_function = str(kwargs.get("call_function") or "")
            calls.append(call_function)
            if call_function == "failure_analysis":
                return json.dumps(
                    {
                        "analysis_status": "completed",
                        "failure_signature": family,
                        "failure_type": "repeated_same_failure",
                        "rejected_hypotheses": [],
                        "next_required_action": {
                            "kind": "root_cause_recovery",
                            "required_paths": ["app.py"],
                        },
                    }
                )
            raise self.local_sdlc.RunnerError("stop after observing recovery routing")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root)
            (project / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
            source = self.make_stalled_run(
                project,
                "source",
                manifest={
                    "last_functional_failure_family_signature": family,
                    "repeated_same_failure_count": 1,
                },
            )
            (source / "05-r01-command-01.md").write_text(
                self.local_sdlc.command_result_document(
                    "python3 -m unittest",
                    1,
                    "FAIL: test_insert (tests.test_core.TestCore.test_insert)\n",
                    "AssertionError: expected row\n",
                    0.1,
                ),
                encoding="utf-8",
            )
            target = project / "analysis-recovery"
            plan = self.local_sdlc.plan_stalled_recovery(
                source,
                requested_strategy="retry",
                failure_family_threshold=2,
                target_run_dir=target,
            )
            args = self.local_sdlc.build_parser().parse_args(
                [
                    "agent",
                    "repair repeated failure",
                    "--project",
                    str(project),
                    "--skills-dir",
                    str(skills_dir),
                    "--include",
                    "app.py",
                    "--resume",
                    str(source),
                    "--recovery-plan",
                    str(self.local_sdlc.recovery_plan_file_path(source)),
                    "--run-dir",
                    str(target),
                ]
            )
            with mock.patch(
                "local_sdlc.agent_runner.run_skill_call",
                side_effect=fake_skill_call,
            ):
                with self.assertRaisesRegex(
                    self.local_sdlc.RunnerError,
                    "stop after observing recovery routing",
                ):
                    self.local_sdlc.command_agent(args)

            state = json.loads((source / "recovery_state.json").read_text(encoding="utf-8"))

        self.assertEqual(plan["strategy"], "failure_analysis")
        self.assertEqual(calls[:2], ["failure_analysis", "root_cause_analysis"])
        self.assertNotIn("generate_artifact", calls)
        self.assertNotIn("repair_artifact", calls)
        self.assertEqual(state["status"], "RESUMED")

    def test_auto_recover_stalls_restarts_in_a_new_run_and_finishes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, skills_dir = self.make_agent_project(root)
            (project / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
            args = self.local_sdlc.build_parser().parse_args(
                [
                    "agent",
                    "verify existing code",
                    "--project",
                    str(project),
                    "--skills-dir",
                    str(skills_dir),
                    "--include",
                    "app.py",
                    "--auto-recover-stalls",
                    "--precheck",
                    "--test-command",
                    "python3 -c pass",
                    "--judge-mode",
                    "command-only",
                ]
            )
            original = self.local_sdlc._agent_runner.command_agent
            attempts: list[Path] = []

            def stall_once(current_args):
                run_dir = Path(current_args.run_dir).resolve()
                attempts.append(run_dir)
                if len(attempts) == 1:
                    self.local_sdlc.initialize_budget(
                        run_dir,
                        self.local_sdlc.budget_limits_from_args(current_args),
                        scope_kind="goal_stage",
                    )
                    self.local_sdlc.initialize_progress_monitor(
                        run_dir,
                        self.local_sdlc.ProgressPolicy(max_idle_seconds=1.0),
                        scope_kind="goal_stage",
                        now=100.0,
                    )
                    self.local_sdlc.write_run_document(
                        run_dir,
                        "run.partial.json",
                        json.dumps(
                            {
                                "brief": current_args.brief,
                                "command": "agent",
                                "status": "stalled",
                                "final_verdict": "stalled",
                                "completed_rounds": 1,
                            }
                        ),
                    )
                    self.local_sdlc.enforce_progress_deadline(
                        run_dir,
                        "injected_watchdog",
                        now=101.0,
                    )
                return original(current_args)

            with mock.patch.object(
                self.local_sdlc._agent_runner,
                "command_agent",
                side_effect=stall_once,
            ), mock.patch(
                "local_sdlc.agent_runner.LocalLLMClient.complete",
                return_value=json.dumps(
                    {
                        "artifacts": [
                            {
                                "type": "replace_file",
                                "path": "app.py",
                                "content": "VALUE = 1\n",
                            }
                        ]
                    }
                ),
            ):
                result = self.local_sdlc.command_agent(args)

            final_run = Path(args.completed_run_dir)
            manifest = json.loads((final_run / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertEqual(len(attempts), 2)
        self.assertNotEqual(attempts[0], attempts[1])
        self.assertEqual(manifest["resumed_from"], str(attempts[0]))
        self.assertEqual(manifest["final_verdict"], "approved")
