import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from learning_runtime.candidate_miner import mine_candidates
from learning_runtime.candidate_store import CandidateStore
from learning_runtime.cli import main as learning_main
from learning_runtime.evaluation_store import EvaluationStore
from learning_runtime.storage import ExperienceStore
from learning_runtime.validation import validate_and_store
from learning_runtime.work_control import (
    LearningLimits,
    LearningWorkControl,
    LearningWorkStopped,
    request_learning_cancel,
)
from tests.learning_candidate_fixtures import (
    ScriptedCandidateLLM,
    eligible_episode,
    valid_case_responses,
)
from tests.test_learning_registry import passing_cases
from tests.test_learning_validation import structural_candidate


class LearningWorkControlTests(unittest.TestCase):
    def test_cancel_is_durable_and_blocks_the_next_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = LearningWorkControl(
                root,
                "candidate_mining",
                operation_id="LW-cancel-check",
            )
            control.checkpoint("before_first_call", api_calls=1, tokens=10)

            cancellation = request_learning_cancel(
                root,
                operation_id=control.operation_id,
                reason_code="operator_stop",
            )
            with self.assertRaises(LearningWorkStopped) as raised:
                control.checkpoint("before_second_call", api_calls=1, tokens=10)
            report = control.report()

        self.assertEqual(cancellation["status"], "cancel_requested")
        self.assertEqual(raised.exception.reason_code, "operator_stop")
        self.assertEqual(report["status"], "stopped")
        self.assertEqual(report["api_calls"], 1)
        self.assertEqual(report["reserved_tokens"], 10)

    def test_each_budget_stops_before_over_consumption(self):
        dimensions = (
            (
                "api",
                LearningLimits(max_api_calls=1, max_cases=9, max_tokens=99),
                {"api_calls": 1},
                {"api_calls": 1},
                "api_call_budget_exhausted",
                "api_calls",
                1,
            ),
            (
                "case",
                LearningLimits(max_api_calls=9, max_cases=1, max_tokens=99),
                {"cases": 1},
                {"cases": 1},
                "case_budget_exhausted",
                "case_count",
                1,
            ),
            (
                "token",
                LearningLimits(max_api_calls=9, max_cases=9, max_tokens=10),
                {"tokens": 10},
                {"tokens": 1},
                "token_budget_exhausted",
                "reserved_tokens",
                10,
            ),
        )
        for name, limits, first, second, reason, field, expected in dimensions:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                control = LearningWorkControl(Path(temp), "budget_check", limits=limits)
                control.checkpoint("first", **first)
                with self.assertRaises(LearningWorkStopped) as raised:
                    control.checkpoint("second", **second)
                report = control.report()
                self.assertEqual(raised.exception.reason_code, reason)
                self.assertEqual(report[field], expected)

    def test_wall_clock_budget_is_checked_at_work_boundaries(self):
        now = [100.0]
        with tempfile.TemporaryDirectory() as temp:
            control = LearningWorkControl(
                Path(temp),
                "wall_check",
                limits=LearningLimits(max_wall_seconds=5),
                clock=lambda: now[0],
            )
            now[0] = 106.0
            with self.assertRaises(LearningWorkStopped) as raised:
                control.checkpoint("next_work")
            report = control.report()

        self.assertEqual(raised.exception.reason_code, "wall_clock_budget_exhausted")
        self.assertEqual(report["status"], "stopped")
        self.assertEqual(report["elapsed_seconds"], 6.0)

    def test_candidate_mining_stops_before_an_unbudgeted_api_call(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            experience = ExperienceStore(root)
            candidates = CandidateStore(root)
            experience.put_episode(eligible_episode())
            llm = ScriptedCandidateLLM(valid_case_responses())
            control = LearningWorkControl(
                root,
                "candidate_mining",
                limits=LearningLimits(
                    max_api_calls=2,
                    max_cases=10,
                    max_tokens=100_000,
                ),
            )

            report = mine_candidates(
                experience,
                candidates,
                llm,
                max_batches=1,
                control=control,
            )
            attempts = candidates.attempts()
            candidate_count = candidates.candidate_count()

        self.assertEqual(report["status"], "stopped")
        self.assertEqual(report["reason_code"], "api_call_budget_exhausted")
        self.assertEqual(len(llm.calls), 2)
        self.assertEqual(candidate_count, 0)
        self.assertEqual(attempts[0]["status"], "stopped")

    def test_validation_stops_before_storage_when_case_budget_is_exhausted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            experience = ExperienceStore(root)
            for episode_id, project in (
                ("EP-source-a", "project-a"),
                ("EP-source-b", "project-b"),
            ):
                experience.put_episode(
                    eligible_episode(episode_id, project_fingerprint=project)
                )
            evaluations = EvaluationStore(root)
            control = LearningWorkControl(
                root,
                "candidate_validation",
                limits=LearningLimits(max_cases=1),
            )

            with self.assertRaises(LearningWorkStopped) as raised:
                validate_and_store(
                    structural_candidate(),
                    experience,
                    evaluations,
                    passing_cases(),
                    control=control,
                )
            report = control.report()
            report_count = evaluations.report_count()

        self.assertEqual(raised.exception.reason_code, "case_budget_exhausted")
        self.assertEqual(report["case_count"], 1)
        self.assertEqual(report_count, 0)

    def test_cancel_work_cli_targets_a_running_operation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = LearningWorkControl(
                root,
                "candidate_validation",
                operation_id="LW-cli-cancel",
            )
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = learning_main(
                    [
                        "cancel-work",
                        "--data-dir",
                        str(root),
                        "--operation",
                        control.operation_id,
                    ]
                )
            payload = json.loads(output.getvalue())
            with self.assertRaises(LearningWorkStopped):
                control.checkpoint("next_work")
            with contextlib.redirect_stdout(io.StringIO()) as status_output:
                status_code = learning_main(
                    [
                        "work-status",
                        "--data-dir",
                        str(root),
                        "--operation",
                        control.operation_id,
                    ]
                )
            status = json.loads(status_output.getvalue())

        self.assertEqual(code, 0)
        self.assertEqual(payload["operation_ids"], [control.operation_id])
        self.assertEqual(status_code, 0)
        self.assertEqual(status["operations"][0]["status"], "stopped")
        self.assertEqual(status["operations"][0]["reason_code"], "user_requested")


if __name__ == "__main__":
    unittest.main()
