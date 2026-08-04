import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_sdlc.cli import build_parser, command_doctor
from local_sdlc.llm_client import LocalLLMClient, build_config
from local_sdlc.models import LLMConfig, LLMReasoningOnlyError, LLMRoleOverride, RunnerError


DEEPSEEK_MODEL = "deepseek-v4-flash-0731"


class ModelProfileTests(unittest.TestCase):
    def _config(self, project: Path, profile: str):
        args = build_parser().parse_args(
            [
                "doctor",
                "--project",
                str(project),
                "--skip-llm",
                "--model-profile",
                profile,
            ]
        )
        return build_config(args)

    def test_deepseek_stable_profile_is_bounded_and_no_thinking(self):
        with tempfile.TemporaryDirectory() as temp:
            config = self._config(Path(temp), "deepseek")

        client = LocalLLMClient(config)
        self.assertEqual(config.model_profile, "deepseek-v4-flash-agent")
        self.assertEqual(config.model, DEEPSEEK_MODEL)
        self.assertEqual(client.call_settings("coder").max_tokens, 8192)
        self.assertTrue(client.call_settings("coder").disable_thinking)
        self.assertEqual(client.call_settings("coder", "generate_artifact").max_tokens, 8192)
        self.assertEqual(client.call_settings("coder", "repair_artifact").max_tokens, 4096)
        self.assertIsNone(client.call_settings("coder").reasoning_effort)
        for function_name in (
            "route_task",
            "plan_work",
            "failure_analysis",
            "generate_artifact",
            "repair_artifact",
            "judge_review",
            "verify_acceptance",
        ):
            with self.subTest(function_name=function_name):
                self.assertTrue(client.call_settings("default", function_name).disable_thinking)

    def test_deepseek_deep_profile_thinks_only_for_analysis(self):
        with tempfile.TemporaryDirectory() as temp:
            config = self._config(Path(temp), "deepseek-v4-flash-agent-deep")

        client = LocalLLMClient(config)
        high_functions = (
            "route_task",
            "explore_code",
        )
        max_functions = ("plan_work",)
        for function_name in high_functions + max_functions:
            with self.subTest(function_name=function_name):
                settings = client.call_settings("default", function_name)
                self.assertFalse(settings.disable_thinking)
                self.assertEqual(settings.max_tokens, 8192)
                self.assertEqual(settings.temperature, 1.0)
                expected_effort = "high" if function_name in high_functions else "max"
                self.assertEqual(settings.reasoning_effort, expected_effort)
        root_cause = client.call_settings("default", "root_cause_analysis")
        self.assertFalse(root_cause.disable_thinking)
        self.assertEqual(root_cause.max_tokens, 8192)
        self.assertEqual(root_cause.temperature, 1.0)
        self.assertEqual(root_cause.reasoning_effort, "high")
        structured_functions = {
            "failure_analysis": 4096,
            "patch_planner": 4096,
            "patch_conformance": 4096,
            "project_policy_triage": 4096,
            "policy_arbitration": 4096,
            "judge_review": 4096,
        }
        for function_name, max_tokens in structured_functions.items():
            with self.subTest(function_name=function_name):
                settings = client.call_settings("default", function_name)
                self.assertTrue(settings.disable_thinking)
                self.assertEqual(settings.max_tokens, max_tokens)
                self.assertEqual(settings.temperature, 0.0)
                self.assertIsNone(settings.reasoning_effort)
        for function_name in (
            "generate_artifact",
            "repair_artifact",
            "root_cause_patch",
            "artifact_writer",
            "semantic_repair",
            "format_repair",
            "verify_acceptance",
        ):
            with self.subTest(function_name=function_name):
                settings = client.call_settings("default", function_name)
                self.assertTrue(settings.disable_thinking)
                self.assertIsNone(settings.reasoning_effort)

    def test_deepseek_deep_profile_sends_effort_in_chat_template_kwargs(self):
        with tempfile.TemporaryDirectory() as temp:
            config = self._config(Path(temp), "deepseek-v4-flash-agent-deep")

        client = LocalLLMClient(config)
        payloads = []

        def fake_request(method, path, payload=None, timeout=None):
            if path == "/models":
                return {"data": [{"id": DEEPSEEK_MODEL}]}
            payloads.append(payload)
            return {
                "choices": [
                    {
                        "message": {
                            "reasoning_content": "checked",
                            "content": "done",
                        }
                    }
                ]
            }

        client._request = fake_request
        result = client.complete(
            [{"role": "user", "content": "analyze"}],
            call_function="root_cause_analysis",
        )

        self.assertEqual(result, "done")
        self.assertEqual(payloads[0]["max_tokens"], 8192)
        self.assertEqual(payloads[0]["temperature"], 1.0)
        self.assertEqual(
            payloads[0]["chat_template_kwargs"],
            {"enable_thinking": True, "reasoning_effort": "high"},
        )

    def test_reasoning_only_analysis_retries_once_with_thinking_off(self):
        with tempfile.TemporaryDirectory() as temp:
            config = self._config(Path(temp), "deepseek-v4-flash-agent-deep")

        client = LocalLLMClient(config)
        payloads = []
        fallback_events = []

        def fake_request(method, path, payload=None, timeout=None):
            if path == "/models":
                return {"data": [{"id": DEEPSEEK_MODEL}]}
            payloads.append(payload)
            if len(payloads) == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "reasoning_content": "long private analysis",
                                "content": None,
                            }
                        }
                    ]
                }
            return {"choices": [{"message": {"content": "判定: 承認"}}]}

        client._request = fake_request
        client.set_runtime_completion_fallback_callback(
            lambda agent, function, reason: fallback_events.append((agent, function, reason))
        )

        result = client.complete(
            [{"role": "user", "content": "review"}],
            agent_level="judge",
            call_function="root_cause_analysis",
        )

        self.assertEqual(result, "判定: 承認")
        self.assertEqual(len(payloads), 2)
        self.assertEqual(
            payloads[0]["chat_template_kwargs"],
            {"enable_thinking": True, "reasoning_effort": "high"},
        )
        self.assertEqual(payloads[1]["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(payloads[1]["temperature"], 0.0)
        self.assertEqual(payloads[1]["max_tokens"], 2048)
        fallback_messages = payloads[1]["messages"]
        self.assertIn("long private analysis", fallback_messages[-2]["content"])
        self.assertIn("same-call condensation", fallback_messages[-2]["content"])
        self.assertIn("Return only the final answer", fallback_messages[-1]["content"])
        self.assertEqual(client.generation_request_count, 2)
        self.assertEqual(client.last_completion_attempts, 2)
        self.assertEqual(
            fallback_events,
            [("judge", "root_cause_analysis", "reasoning_only_output")],
        )
        self.assertEqual(client.reasoning_records[0]["reasoning_content"], "long private analysis")
        self.assertEqual(
            client.completion_recovery_records[0]["action"],
            "condense_reasoning_once_with_thinking_off",
        )
        self.assertEqual(client.completion_recovery_records[0]["reasoning_excerpt_chars"], 21)

    def test_reasoning_only_no_thinking_call_fails_without_retry_loop(self):
        with tempfile.TemporaryDirectory() as temp:
            config = self._config(Path(temp), "deepseek-v4-flash-agent")

        client = LocalLLMClient(config)
        payloads = []

        def fake_request(method, path, payload=None, timeout=None):
            if path == "/models":
                return {"data": [{"id": DEEPSEEK_MODEL}]}
            payloads.append(payload)
            return {
                "choices": [
                    {"message": {"reasoning_content": "unexpected", "content": None}}
                ]
            }

        client._request = fake_request

        with self.assertRaises(LLMReasoningOnlyError):
            client.complete(
                [{"role": "user", "content": "review"}],
                agent_level="judge",
                call_function="judge_review",
            )

        self.assertEqual(len(payloads), 1)
        self.assertEqual(client.generation_request_count, 1)
        self.assertEqual(client.completion_recovery_records, [])

    def test_qwen_profile_remains_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            config = self._config(Path(temp), "qwen-agent")

        client = LocalLLMClient(config)
        generated = client.call_settings("coder", "generate_artifact")
        self.assertEqual(config.model, "qwen3.5-122b")
        self.assertEqual(generated.max_tokens, 49152)
        self.assertTrue(generated.disable_thinking)
        self.assertFalse(client.call_settings("judge", "judge_review").disable_thinking)
        self.assertIsNone(client.call_settings("judge", "judge_review").reasoning_effort)

    def test_named_profile_rejects_unserved_model_before_generation(self):
        config = LLMConfig(
            base_url="http://localhost:30000/v1",
            api_key="dummy-local",
            model=DEEPSEEK_MODEL,
            timeout=10.0,
            health_timeout=0.2,
            temperature=0.0,
            max_tokens=16,
            disable_thinking=True,
            model_profile="deepseek-v4-flash-agent",
        )
        client = LocalLLMClient(config)
        calls = []

        def fake_request(method, path, payload=None, timeout=None):
            calls.append((method, path))
            if path == "/models":
                return {"data": [{"id": "qwen3.5-122b"}]}
            raise AssertionError("generation request must not be sent")

        client._request = fake_request
        with self.assertRaises(RunnerError) as caught:
            client.complete([{"role": "user", "content": "hello"}])

        self.assertEqual(calls, [("GET", "/models")])
        self.assertIn("deepseek-v4-flash-agent", str(caught.exception))
        self.assertIn("qwen-agent", str(caught.exception))
        self.assertIn("No generation request was sent", str(caught.exception))

    def test_function_model_override_must_be_served(self):
        config = LLMConfig(
            base_url="http://localhost:30000/v1",
            api_key="dummy-local",
            model="qwen3.5-122b",
            timeout=10.0,
            health_timeout=0.2,
            temperature=0.0,
            max_tokens=16,
            disable_thinking=True,
            model_profile="qwen-agent",
            function_overrides={
                "failure_analysis": LLMRoleOverride(model=DEEPSEEK_MODEL),
            },
        )
        client = LocalLLMClient(config)
        client._request = lambda method, path, payload=None, timeout=None: {
            "data": [{"id": "qwen3.5-122b"}]
        }

        with self.assertRaises(RunnerError) as caught:
            client.complete(
                [{"role": "user", "content": "analyze"}],
                call_function="failure_analysis",
            )

        self.assertIn(DEEPSEEK_MODEL, str(caught.exception))
        self.assertIn("qwen3.5-122b", str(caught.exception))

    def test_doctor_rejects_profile_runtime_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "SPEC.md").write_text("# Test spec\n", encoding="utf-8")
            args = build_parser().parse_args(
                [
                    "doctor",
                    "--project",
                    str(project),
                    "--model-profile",
                    "deepseek",
                    "--skip-probes",
                ]
            )
            output = io.StringIO()
            with mock.patch.object(LocalLLMClient, "models", return_value=["qwen3.5-122b"]):
                with contextlib.redirect_stdout(output):
                    exit_code = command_doctor(args)

        self.assertEqual(exit_code, 1)
        self.assertIn("llm_profile_compatibility: FAIL", output.getvalue())
        self.assertIn("qwen-agent", output.getvalue())


if __name__ == "__main__":
    unittest.main()
