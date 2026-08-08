from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dagrunner import (
    CycleError,
    Engine,
    Graph,
    GraphValidationError,
    StateMismatchError,
    Task,
)


class DagRunnerAcceptanceTests(unittest.TestCase):
    def test_graph_validation_and_lexical_topological_order(self):
        graph = Graph(
            [
                Task("publish", ["test", "build"]),
                Task("test", ["fetch"]),
                Task("build", ["fetch"]),
                Task("fetch"),
            ]
        )
        self.assertEqual(
            graph.topological_order(),
            ["fetch", "build", "test", "publish"],
        )

        invalid_graphs = [
            [Task("bad id")],
            [Task("same"), Task("same")],
            [Task("a", ["missing"])],
            [Task("a", ["a"])],
            [Task("a", ["b"]), Task("b", ["a"])],
        ]
        for tasks in invalid_graphs[:3]:
            with self.subTest(tasks=tasks):
                with self.assertRaises(GraphValidationError):
                    Graph(tasks)
        for tasks in invalid_graphs[3:]:
            with self.subTest(tasks=tasks):
                with self.assertRaises(CycleError):
                    Graph(tasks)

    def test_retry_and_dependency_value_handoff(self):
        calls = {"source": 0, "derived": 0}

        def source(_dependencies):
            calls["source"] += 1
            if calls["source"] < 3:
                raise ValueError("temporary")
            return 20

        def derived(dependencies):
            calls["derived"] += 1
            self.assertEqual(dict(dependencies), {"source": 20})
            return dependencies["source"] + 22

        report = Engine(
            [Task("derived", ["source"]), Task("source", max_attempts=3)],
            {"source": source, "derived": derived},
        ).run()

        self.assertTrue(report.completed)
        self.assertEqual(report.statuses, {"derived": "succeeded", "source": "succeeded"})
        self.assertEqual(report.attempts, {"derived": 1, "source": 3})
        self.assertEqual(report.values, {"derived": 42, "source": 20})
        self.assertEqual(report.errors, {})

    def test_failure_blocks_descendants_but_not_independent_branch(self):
        invoked = []

        def fail(_dependencies):
            invoked.append("fail")
            raise RuntimeError("broken root")

        def must_not_run(_dependencies):
            invoked.append("child")
            return "wrong"

        def independent(_dependencies):
            invoked.append("independent")
            return "ok"

        report = Engine(
            [
                Task("fail", max_attempts=2),
                Task("child", ["fail"]),
                Task("grandchild", ["child"]),
                Task("independent"),
            ],
            {
                "fail": fail,
                "child": must_not_run,
                "grandchild": must_not_run,
                "independent": independent,
            },
        ).run()

        self.assertEqual(invoked.count("fail"), 2)
        self.assertNotIn("child", invoked)
        self.assertEqual(invoked.count("independent"), 1)
        self.assertEqual(report.statuses["fail"], "failed")
        self.assertEqual(report.statuses["child"], "blocked")
        self.assertEqual(report.statuses["grandchild"], "blocked")
        self.assertEqual(report.statuses["independent"], "succeeded")
        self.assertEqual(report.attempts["child"], 0)
        self.assertIn("RuntimeError", report.errors["fail"])
        self.assertIn("broken root", report.errors["fail"])
        self.assertIn("fail", report.errors["child"])

    def test_partial_checkpoint_resumes_without_reexecuting_success(self):
        with tempfile.TemporaryDirectory() as temp:
            state_path = Path(temp) / "nested" / "state.json"
            calls = {"alpha": 0, "beta": 0, "join": 0}

            def alpha(_dependencies):
                calls["alpha"] += 1
                return "A"

            def beta(_dependencies):
                calls["beta"] += 1
                return "B"

            def join(dependencies):
                calls["join"] += 1
                return dependencies["alpha"] + dependencies["beta"]

            tasks = [Task("join", ["beta", "alpha"]), Task("beta"), Task("alpha")]
            handlers = {"alpha": alpha, "beta": beta, "join": join}
            first = Engine(tasks, handlers, state_path=state_path).run(max_tasks=1)

            self.assertFalse(first.completed)
            self.assertEqual(first.statuses["alpha"], "succeeded")
            self.assertEqual(first.statuses["beta"], "pending")
            self.assertEqual(calls, {"alpha": 1, "beta": 0, "join": 0})
            self.assertTrue(state_path.is_file())

            second = Engine(tasks, handlers, state_path=state_path).run()
            self.assertTrue(second.completed)
            self.assertEqual(second.values["join"], "AB")
            self.assertEqual(calls, {"alpha": 1, "beta": 1, "join": 1})

            third = Engine(tasks, handlers, state_path=state_path).run()
            self.assertTrue(third.completed)
            self.assertEqual(calls, {"alpha": 1, "beta": 1, "join": 1})

            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["schema_version"], 1)
            self.assertRegex(persisted["graph_fingerprint"], r"^[0-9a-f]{64}$")
            self.assertFalse(list(state_path.parent.glob("*.tmp")))

    def test_checkpoint_graph_mismatch_fails_before_handlers(self):
        with tempfile.TemporaryDirectory() as temp:
            state_path = Path(temp) / "state.json"
            calls = []

            def handler(_dependencies):
                calls.append("called")
                return 1

            Engine([Task("a")], {"a": handler}, state_path=state_path).run()
            self.assertEqual(calls, ["called"])

            with self.assertRaises(StateMismatchError):
                Engine(
                    [Task("a"), Task("b")],
                    {"a": handler, "b": handler},
                    state_path=state_path,
                ).run()
            self.assertEqual(calls, ["called"])

    def test_malformed_checkpoint_and_missing_handler_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            state_path = Path(temp) / "state.json"
            state_path.write_text("{not valid json", encoding="utf-8")
            called = []

            def handler(_dependencies):
                called.append(True)
                return 1

            with self.assertRaises(StateMismatchError):
                Engine([Task("a")], {"a": handler}, state_path=state_path).run()
            self.assertEqual(called, [])

        with self.assertRaises(GraphValidationError):
            Engine([Task("a"), Task("b", ["a"])], {"a": lambda _deps: 1})

    def test_report_is_a_snapshot(self):
        engine = Engine([Task("a")], {"a": lambda _dependencies: {"value": 1}})
        first = engine.run()
        first.statuses["a"] = "failed"
        first.attempts["a"] = 99
        first.values.clear()

        second = engine.run()
        self.assertEqual(second.statuses, {"a": "succeeded"})
        self.assertEqual(second.attempts, {"a": 1})
        self.assertEqual(second.values, {"a": {"value": 1}})


if __name__ == "__main__":
    unittest.main()
