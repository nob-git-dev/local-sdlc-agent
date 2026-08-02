import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from learning_runtime.cli import main as learning_main
from learning_runtime.candidate_llm import LocalCandidateLLM
from local_sdlc.models import (
    MODEL_PROFILE_FUNCTION_PROFILES,
    DEFAULT_FUNCTION_PROFILES,
)


class FakeClient:
    def __init__(self):
        self.calls = []
        self.reasoning_records = [
            {
                "agent_level": "judge",
                "call_function": "candidate_abstraction",
                "model": "fixture-model",
                "chars": 42,
                "truncated": False,
                "reasoning_content": "must not cross the boundary",
            }
        ]

    def complete(self, messages, *, agent_level, call_function):
        self.calls.append(
            {
                "messages": messages,
                "agent_level": agent_level,
                "call_function": call_function,
            }
        )
        return "{}"


class CandidateLLMTests(unittest.TestCase):
    def test_each_function_call_has_a_fresh_system_and_document_pair(self):
        client = FakeClient()
        llm = LocalCandidateLLM(client)

        llm.complete("candidate_abstraction", "prompt-a", {"step": "a"})
        llm.complete("scope_classification", "prompt-b", {"step": "b"})

        self.assertEqual(len(client.calls), 2)
        self.assertEqual([len(item["messages"]) for item in client.calls], [2, 2])
        self.assertEqual(client.calls[0]["messages"][0], {"role": "system", "content": "prompt-a"})
        self.assertNotIn("prompt-a", str(client.calls[1]["messages"]))
        self.assertEqual(
            [item["call_function"] for item in client.calls],
            ["candidate_abstraction", "scope_classification"],
        )

    def test_reasoning_audit_excludes_reasoning_content(self):
        audit = LocalCandidateLLM(FakeClient()).reasoning_audit()

        self.assertEqual(audit[0]["chars"], 42)
        self.assertNotIn("reasoning_content", audit[0])

    def test_function_profiles_separate_analysis_and_serialization(self):
        self.assertFalse(DEFAULT_FUNCTION_PROFILES["candidate_abstraction"].disable_thinking)
        self.assertFalse(DEFAULT_FUNCTION_PROFILES["scope_classification"].disable_thinking)
        self.assertTrue(DEFAULT_FUNCTION_PROFILES["candidate_serialization"].disable_thinking)
        self.assertFalse(
            MODEL_PROFILE_FUNCTION_PROFILES["qwen-agent"]["candidate_abstraction"].disable_thinking
        )
        self.assertTrue(
            MODEL_PROFILE_FUNCTION_PROFILES["ornith-agent"]["candidate_abstraction"].disable_thinking
        )

    def test_candidate_cli_with_no_batches_does_not_call_the_api(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()) as output:
            result = learning_main(
                [
                    "mine-candidates",
                    "--data-dir",
                    str(Path(temp)),
                    "--model-profile",
                    "qwen-agent",
                ]
            )
            report = json.loads(output.getvalue())

        self.assertEqual(result, 0)
        self.assertEqual(report["batch_count"], 0)
        self.assertEqual(report["function_profiles"]["candidate_abstraction"]["thinking"], "on")
        self.assertEqual(report["function_profiles"]["candidate_serialization"]["thinking"], "off")


if __name__ == "__main__":
    unittest.main()
