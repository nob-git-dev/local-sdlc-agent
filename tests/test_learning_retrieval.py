import json
import tempfile
import unittest
from pathlib import Path

from learning_runtime.promotion import PromotionService
from local_sdlc import cli as app_cli
from local_sdlc.learning_context import (
    bind_learning_snapshot,
    inherit_learning_binding,
    knowledge_context_document,
)
from tests.learning_knowledge_fixtures import renamed_domain_map
from tests.test_learning_registry import validated_candidate
from tests.test_learning_validation import unrelated_map


class LearningRetrievalTests(unittest.TestCase):
    def test_run_binding_is_immutable_while_a_new_run_sees_a_later_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_dir = root / "learning"
            first = validated_candidate(data_dir, knowledge_id="K-binding-first")
            PromotionService(data_dir).promote(first.knowledge_id)
            domain_map = renamed_domain_map(
                "runtime",
                project_fingerprint="project-runtime",
            )

            run_a = root / "run-a"
            binding_a1 = bind_learning_snapshot(
                run_a,
                data_dir=data_dir,
                domain_map=domain_map,
            )
            second = validated_candidate(data_dir, knowledge_id="K-binding-second")
            PromotionService(data_dir).promote(second.knowledge_id)
            binding_a2 = bind_learning_snapshot(
                run_a,
                data_dir=data_dir,
                domain_map=domain_map,
            )
            binding_b = bind_learning_snapshot(
                root / "run-b",
                data_dir=data_dir,
                domain_map=domain_map,
            )

        self.assertEqual(binding_a1, binding_a2)
        self.assertEqual(len(binding_a1["selected_items"]), 1)
        self.assertEqual(len(binding_b["selected_items"]), 2)
        self.assertNotEqual(binding_a1["snapshot_id"], binding_b["snapshot_id"])

    def test_unrelated_or_missing_context_yields_an_explicit_empty_binding(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_dir = root / "learning"
            item = validated_candidate(data_dir)
            PromotionService(data_dir).promote(item.knowledge_id)
            unrelated = bind_learning_snapshot(
                root / "unrelated-run",
                data_dir=data_dir,
                domain_map=unrelated_map(),
            )
            absent_root = root / "absent-learning"
            absent = bind_learning_snapshot(
                root / "absent-run",
                data_dir=absent_root,
                domain_map=unrelated_map("project-absent"),
            )

        self.assertEqual(unrelated["status"], "empty")
        self.assertEqual(unrelated["reason_code"], "no_applicable_knowledge")
        self.assertEqual(unrelated["selected_items"], [])
        self.assertEqual(absent["reason_code"], "registry_missing")
        self.assertFalse((absent_root / "knowledge.sqlite3").exists())

    def test_handoff_is_bounded_and_contains_no_evidence_or_authority(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_dir = root / "learning"
            item = validated_candidate(data_dir)
            PromotionService(data_dir).promote(item.knowledge_id)
            binding = bind_learning_snapshot(
                root / "run",
                data_dir=data_dir,
                domain_map=renamed_domain_map(
                    "handoff",
                    project_fingerprint="project-handoff",
                ),
            )
            rendered = knowledge_context_document(binding)

        self.assertIn(item.knowledge_id, rendered)
        self.assertIn('"authorization": "none"', rendered)
        for forbidden in (
            "evidence_refs",
            "reasoning_content",
            "approval_id",
            "llm_hypothesis",
            "renamed_reader.py",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_child_run_inherits_the_exact_parent_binding(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_dir = root / "learning"
            item = validated_candidate(data_dir)
            PromotionService(data_dir).promote(item.knowledge_id)
            parent = root / "parent"
            original = bind_learning_snapshot(
                parent,
                data_dir=data_dir,
                domain_map=renamed_domain_map(
                    "parent",
                    project_fingerprint="project-parent",
                ),
            )

            inherited = inherit_learning_binding(parent, root / "child")

        self.assertEqual(original, inherited)

    def test_existing_run_binding_tampering_is_not_silently_rebound(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = root / "run"
            bind_learning_snapshot(
                run_dir,
                data_dir=root / "missing",
                domain_map=unrelated_map(),
            )
            path = run_dir / "knowledge-snapshot.json"
            altered = json.loads(path.read_text(encoding="utf-8"))
            altered["reason_code"] = "tampered"
            path.write_text(json.dumps(altered), encoding="utf-8")

            with self.assertRaises(ValueError):
                bind_learning_snapshot(
                    run_dir,
                    data_dir=root / "missing",
                    domain_map=unrelated_map(),
                )

    def test_supervise_binds_and_hands_off_knowledge_before_first_api_call(self):
        calls = []

        class FakeClient:
            def __init__(self, config):
                self.config = config

            def complete(self, messages):
                self_binding = run_dir / "knowledge-snapshot.json"
                if not self_binding.is_file():
                    raise AssertionError("knowledge binding missing before API call")
                calls.append(messages)
                return "PM control"

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "project"
            project.mkdir()
            data_dir = root / "learning"
            item = validated_candidate(data_dir)
            PromotionService(data_dir).promote(item.knowledge_id)
            domain_path = project / "DOMAIN_MAP.json"
            domain_path.write_text(
                json.dumps(
                    renamed_domain_map(
                        "integration",
                        project_fingerprint="project-integration",
                    ).to_local_dict()
                ),
                encoding="utf-8",
            )
            skills_dir = project / "skills"
            skill_dir = skills_dir / "sdlc"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: sdlc\ndescription: plan\n---\n# SDLC\n",
                encoding="utf-8",
            )
            (project / "SPEC.md").write_text("# SPEC\n", encoding="utf-8")
            run_dir = project / "run"
            original_client = app_cli.LocalLLMClient
            app_cli.LocalLLMClient = FakeClient
            try:
                args = app_cli.build_parser().parse_args(
                    [
                        "supervise",
                        "plan task",
                        "--project",
                        str(project),
                        "--skills-dir",
                        str(skills_dir),
                        "--steps",
                        "pm",
                        "--learning-data-dir",
                        str(data_dir),
                        "--domain-map",
                        str(domain_path),
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                result = app_cli.command_supervise(args)
            finally:
                app_cli.LocalLLMClient = original_client
            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn(item.knowledge_id, calls[0][1]["content"])
        self.assertEqual(manifest["knowledge_snapshot"]["selected_count"], 1)


if __name__ == "__main__":
    unittest.main()
