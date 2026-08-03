"""Shared data models and defaults for the local SDLC runner."""

from __future__ import annotations

import dataclasses
from pathlib import Path


DEFAULT_BASE_URL = "http://localhost:30000/v1"
DEFAULT_API_KEY = "dummy-local"
DEFAULT_MODEL = ""
DEFAULT_TIMEOUT = 300.0
DEFAULT_HEALTH_TIMEOUT = 5.0
DEFAULT_PM_MAX_TOKENS = 8192
DEFAULT_CODER_MAX_TOKENS = 65536
DEFAULT_JUDGE_MAX_TOKENS = 8192
DEFAULT_SKILLS_DIR = Path("sdlc-skills") / "skills"
DEFAULT_AGENTS_DIR = Path("sdlc-skills") / "agents"
GENERATED_DIR = Path(".sdlc-runner")
SUPERVISOR_STEPS = ("spec", "pm", "coder", "judge")
SDLC_PHASES = (
    "spec",
    "architect",
    "ddd",
    "tdd",
    "ui",
    "review",
    "security",
    "deploy",
    "sre",
    "observe",
    "refactor",
)


class RunnerError(RuntimeError):
    """Raised for expected CLI errors."""


class LLMTimeoutError(RunnerError):
    """Raised when a specific LLM HTTP request times out."""

    def __init__(self, path: str, timeout: float):
        self.path = path
        self.timeout = timeout
        super().__init__(f"LLM API request to {path} timed out after {timeout:g}s")


class LLMStreamAbortError(RunnerError):
    """Raised when a partial streaming response is provably malformed."""

    def __init__(
        self,
        reason: str,
        partial_text: str,
        stats: "LLMStreamStats",
        code: str = "stream_artifact_guard",
        score: int = 0,
        threshold: int = 0,
    ):
        self.reason = reason
        self.partial_text = partial_text
        self.stats = stats
        self.code = code
        self.score = score
        self.threshold = threshold
        super().__init__(f"LLM stream aborted: {reason}")


@dataclasses.dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path
    body: str
    metadata: dict[str, str]


@dataclasses.dataclass(frozen=True)
class LLMConfig:
    base_url: str
    api_key: str
    model: str
    timeout: float
    health_timeout: float
    temperature: float
    max_tokens: int
    disable_thinking: bool
    stream: bool = False
    model_profile: str = "default"
    config_file: str = ""
    role_overrides: dict[str, "LLMRoleOverride"] = dataclasses.field(default_factory=dict)
    function_overrides: dict[str, "LLMRoleOverride"] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class LLMRoleOverride:
    temperature: float | None = None
    max_tokens: int | None = None
    disable_thinking: bool | None = None
    model: str | None = None
    reasoning_effort: str | None = None


LEARNING_FUNCTION_PROFILES_REASONING: dict[str, LLMRoleOverride] = {
    "episode_review": LLMRoleOverride(temperature=0.0, max_tokens=8192, disable_thinking=False),
    "candidate_abstraction": LLMRoleOverride(temperature=0.1, max_tokens=8192, disable_thinking=False),
    "scope_classification": LLMRoleOverride(temperature=0.0, max_tokens=8192, disable_thinking=False),
    "counterexample_search": LLMRoleOverride(temperature=0.1, max_tokens=8192, disable_thinking=False),
    "candidate_serialization": LLMRoleOverride(temperature=0.0, max_tokens=4096, disable_thinking=True),
    "promotion_review": LLMRoleOverride(temperature=0.0, max_tokens=8192, disable_thinking=False),
}

LEARNING_FUNCTION_PROFILES_NO_THINKING: dict[str, LLMRoleOverride] = {
    name: dataclasses.replace(profile, disable_thinking=True)
    for name, profile in LEARNING_FUNCTION_PROFILES_REASONING.items()
}


@dataclasses.dataclass(frozen=True)
class LLMCallSettings:
    agent_level: str
    call_function: str
    model: str
    temperature: float
    max_tokens: int
    disable_thinking: bool
    reasoning_effort: str | None = None


DEFAULT_FUNCTION_PROFILES: dict[str, LLMRoleOverride] = {
    # Planning and routing need enough room for constraints, but should stay
    # mostly deterministic so downstream roles receive stable propositions.
    "route_task": LLMRoleOverride(temperature=0.2, max_tokens=DEFAULT_PM_MAX_TOKENS, disable_thinking=True),
    "plan_work": LLMRoleOverride(temperature=0.2, max_tokens=DEFAULT_PM_MAX_TOKENS, disable_thinking=True),
    # Exploration is evidence gathering, not ideation.
    "explore_code": LLMRoleOverride(temperature=0.0, max_tokens=DEFAULT_PM_MAX_TOKENS, disable_thinking=True),
    # Failure analysis is a bounded diagnostic classification step. It writes
    # structured history for the supervisor; it must not generate patches.
    "failure_analysis": LLMRoleOverride(temperature=0.0, max_tokens=DEFAULT_PM_MAX_TOKENS, disable_thinking=True),
    # Patch planning may benefit from hidden reasoning because it produces a
    # bounded proposition, not an executable artifact. The artifact writer below
    # remains no-thinking so reasoning cannot leak into patches.
    "patch_planner": LLMRoleOverride(temperature=0.0, max_tokens=DEFAULT_PM_MAX_TOKENS, disable_thinking=True),
    # Project-policy triage classifies context-dependent ownership decisions.
    # It never emits patches; the runner validates and enforces the decision.
    "project_policy_triage": LLMRoleOverride(temperature=0.0, max_tokens=DEFAULT_PM_MAX_TOKENS, disable_thinking=True),
    # Artifact generation can require long outputs, but should remain low
    # temperature because the output is an executable contract.
    "generate_artifact": LLMRoleOverride(temperature=0.1, max_tokens=DEFAULT_CODER_MAX_TOKENS, disable_thinking=True),
    "repair_artifact": LLMRoleOverride(temperature=0.05, max_tokens=DEFAULT_CODER_MAX_TOKENS, disable_thinking=True),
    "root_cause_analysis": LLMRoleOverride(temperature=0.0, max_tokens=DEFAULT_PM_MAX_TOKENS, disable_thinking=True),
    "root_cause_patch": LLMRoleOverride(temperature=0.0, max_tokens=DEFAULT_CODER_MAX_TOKENS, disable_thinking=True),
    "artifact_writer": LLMRoleOverride(temperature=0.0, max_tokens=DEFAULT_CODER_MAX_TOKENS, disable_thinking=True),
    "semantic_repair": LLMRoleOverride(temperature=0.0, max_tokens=DEFAULT_PM_MAX_TOKENS, disable_thinking=True),
    "format_repair": LLMRoleOverride(temperature=0.0, max_tokens=DEFAULT_PM_MAX_TOKENS, disable_thinking=True),
    # Review and verification should be evidence-only.
    "judge_review": LLMRoleOverride(temperature=0.0, max_tokens=DEFAULT_JUDGE_MAX_TOKENS, disable_thinking=True),
    "verify_acceptance": LLMRoleOverride(temperature=0.0, max_tokens=DEFAULT_JUDGE_MAX_TOKENS, disable_thinking=True),
    **LEARNING_FUNCTION_PROFILES_REASONING,
}

MODEL_PROFILE_ALIASES: dict[str, str] = {
    "default": "default",
    "none": "default",
    "deepseek": "deepseek-v4-flash-agent",
    "deepseek-agent": "deepseek-v4-flash-agent",
    "deepseek-agent-deep": "deepseek-v4-flash-agent-deep",
    "deepseek-v4": "deepseek-v4-flash-agent",
    "deepseek-v4-flash": "deepseek-v4-flash-agent",
    "deepseek-v4-flash-agent": "deepseek-v4-flash-agent",
    "deepseek-v4-flash-agent-deep": "deepseek-v4-flash-agent-deep",
    "nemotron": "nemotron3-super-agent",
    "nemotron3": "nemotron3-super-agent",
    "nemotron3-super": "nemotron3-super-agent",
    "nemotron3-super-agent": "nemotron3-super-agent",
    "nemotron-puzzle": "nemotron-labs-puzzle-75b-agent",
    "nemotron-labs-puzzle": "nemotron-labs-puzzle-75b-agent",
    "nemotron-labs-puzzle-75b": "nemotron-labs-puzzle-75b-agent",
    "nemotron-labs-puzzle-75b-agent": "nemotron-labs-puzzle-75b-agent",
    "ornith": "ornith-agent",
    "ornith-agent": "ornith-agent",
    "ornith-agent-deep": "ornith-agent-deep",
    "qwen": "qwen-agent",
    "qwen-agent": "qwen-agent",
    "qwen-agent-strict": "qwen-agent",
    "qwen-agent-deep": "qwen-agent-deep",
}

MODEL_PROFILE_DEFAULT_MODELS: dict[str, str] = {
    "deepseek-v4-flash-agent": "deepseek-v4-flash-0731",
    "deepseek-v4-flash-agent-deep": "deepseek-v4-flash-0731",
    "nemotron3-super-agent": "nemotron-3-super",
    "nemotron-labs-puzzle-75b-agent": "nemotron-labs-3-puzzle-75b-a9b",
    "qwen-agent": "qwen3.5-122b",
    "qwen-agent-deep": "qwen3.5-122b",
    "ornith-agent": "Ornith-1.0-35B",
    "ornith-agent-deep": "Ornith-1.0-35B",
}

MODEL_PROFILE_FUNCTION_PROFILES: dict[str, dict[str, LLMRoleOverride]] = {
    "default": {},
    # The local llama.cpp DeepSeek-V4-Flash deployment is intentionally a
    # bounded profile. Its active context is commonly smaller than the model's
    # training context, so staged work is safer than copying Qwen's long
    # artifact budgets. This stable profile keeps every call no-thinking.
    "deepseek-v4-flash-agent": {
        "default": LLMRoleOverride(temperature=0.0, max_tokens=8192, disable_thinking=True),
        "episode_review": LLMRoleOverride(temperature=0.0, max_tokens=4096, disable_thinking=True),
        "candidate_abstraction": LLMRoleOverride(temperature=0.0, max_tokens=6144, disable_thinking=True),
        "scope_classification": LLMRoleOverride(temperature=0.0, max_tokens=4096, disable_thinking=True),
        "counterexample_search": LLMRoleOverride(temperature=0.0, max_tokens=6144, disable_thinking=True),
        "candidate_serialization": LLMRoleOverride(temperature=0.0, max_tokens=4096, disable_thinking=True),
        "promotion_review": LLMRoleOverride(temperature=0.0, max_tokens=4096, disable_thinking=True),
        "route_task": LLMRoleOverride(temperature=0.0, max_tokens=4096, disable_thinking=True),
        "plan_work": LLMRoleOverride(temperature=0.0, max_tokens=6144, disable_thinking=True),
        "explore_code": LLMRoleOverride(temperature=0.0, max_tokens=4096, disable_thinking=True),
        "failure_analysis": LLMRoleOverride(temperature=0.0, max_tokens=6144, disable_thinking=True),
        "patch_planner": LLMRoleOverride(temperature=0.0, max_tokens=6144, disable_thinking=True),
        "project_policy_triage": LLMRoleOverride(temperature=0.0, max_tokens=4096, disable_thinking=True),
        "generate_artifact": LLMRoleOverride(temperature=0.05, max_tokens=8192, disable_thinking=True),
        "repair_artifact": LLMRoleOverride(temperature=0.0, max_tokens=4096, disable_thinking=True),
        "root_cause_analysis": LLMRoleOverride(temperature=0.0, max_tokens=6144, disable_thinking=True),
        "root_cause_patch": LLMRoleOverride(temperature=0.0, max_tokens=4096, disable_thinking=True),
        "artifact_writer": LLMRoleOverride(temperature=0.0, max_tokens=8192, disable_thinking=True),
        "semantic_repair": LLMRoleOverride(temperature=0.0, max_tokens=4096, disable_thinking=True),
        "format_repair": LLMRoleOverride(temperature=0.0, max_tokens=4096, disable_thinking=True),
        "judge_review": LLMRoleOverride(temperature=0.0, max_tokens=4096, disable_thinking=True),
        "verify_acceptance": LLMRoleOverride(temperature=0.0, max_tokens=4096, disable_thinking=True),
    },
    # DeepSeek can return reasoning_content separately from content on the
    # verified local llama.cpp endpoint. Opt in only for non-artifact analysis;
    # every machine-readable artifact path stays identical to the stable preset.
    # The active 16K server context caps output at 8192, so harder analysis uses
    # the model's explicit high/max effort rather than unsafe Qwen-sized limits.
    "deepseek-v4-flash-agent-deep": {
        "default": LLMRoleOverride(temperature=0.0, max_tokens=8192, disable_thinking=True),
        "episode_review": LLMRoleOverride(temperature=1.0, max_tokens=8192, disable_thinking=False, reasoning_effort="high"),
        "candidate_abstraction": LLMRoleOverride(temperature=1.0, max_tokens=8192, disable_thinking=False, reasoning_effort="high"),
        "scope_classification": LLMRoleOverride(temperature=1.0, max_tokens=8192, disable_thinking=False, reasoning_effort="high"),
        "counterexample_search": LLMRoleOverride(temperature=1.0, max_tokens=8192, disable_thinking=False, reasoning_effort="max"),
        "candidate_serialization": LLMRoleOverride(temperature=0.0, max_tokens=4096, disable_thinking=True),
        "promotion_review": LLMRoleOverride(temperature=1.0, max_tokens=8192, disable_thinking=False, reasoning_effort="max"),
        "route_task": LLMRoleOverride(temperature=1.0, max_tokens=8192, disable_thinking=False, reasoning_effort="high"),
        "plan_work": LLMRoleOverride(temperature=1.0, max_tokens=8192, disable_thinking=False, reasoning_effort="max"),
        "explore_code": LLMRoleOverride(temperature=1.0, max_tokens=8192, disable_thinking=False, reasoning_effort="high"),
        "failure_analysis": LLMRoleOverride(temperature=1.0, max_tokens=8192, disable_thinking=False, reasoning_effort="max"),
        "patch_planner": LLMRoleOverride(temperature=1.0, max_tokens=8192, disable_thinking=False, reasoning_effort="max"),
        "project_policy_triage": LLMRoleOverride(temperature=1.0, max_tokens=8192, disable_thinking=False, reasoning_effort="high"),
        "generate_artifact": LLMRoleOverride(temperature=0.05, max_tokens=8192, disable_thinking=True),
        "repair_artifact": LLMRoleOverride(temperature=0.0, max_tokens=4096, disable_thinking=True),
        "root_cause_analysis": LLMRoleOverride(temperature=1.0, max_tokens=8192, disable_thinking=False, reasoning_effort="max"),
        "root_cause_patch": LLMRoleOverride(temperature=0.0, max_tokens=4096, disable_thinking=True),
        "artifact_writer": LLMRoleOverride(temperature=0.0, max_tokens=8192, disable_thinking=True),
        "semantic_repair": LLMRoleOverride(temperature=0.0, max_tokens=4096, disable_thinking=True),
        "format_repair": LLMRoleOverride(temperature=0.0, max_tokens=4096, disable_thinking=True),
        "judge_review": LLMRoleOverride(temperature=1.0, max_tokens=8192, disable_thinking=False, reasoning_effort="max"),
        "verify_acceptance": LLMRoleOverride(temperature=0.0, max_tokens=4096, disable_thinking=True),
    },
    # Nemotron-3-Super on vLLM exposes reasoning separately. Keep thinking
    # disabled for every call until the runner intentionally consumes
    # reasoning fields; otherwise message.content can be null.
    "nemotron3-super-agent": {
        **LEARNING_FUNCTION_PROFILES_NO_THINKING,
        "route_task": LLMRoleOverride(temperature=0.1, max_tokens=8192, disable_thinking=True),
        "plan_work": LLMRoleOverride(temperature=0.1, max_tokens=12288, disable_thinking=True),
        "explore_code": LLMRoleOverride(temperature=0.0, max_tokens=8192, disable_thinking=True),
        "failure_analysis": LLMRoleOverride(temperature=0.0, max_tokens=12288, disable_thinking=True),
        "patch_planner": LLMRoleOverride(temperature=0.0, max_tokens=12288, disable_thinking=True),
        "project_policy_triage": LLMRoleOverride(temperature=0.0, max_tokens=12288, disable_thinking=True),
        "generate_artifact": LLMRoleOverride(temperature=0.05, max_tokens=32768, disable_thinking=True),
        "repair_artifact": LLMRoleOverride(temperature=0.0, max_tokens=16384, disable_thinking=True),
        "root_cause_analysis": LLMRoleOverride(temperature=0.0, max_tokens=12288, disable_thinking=True),
        "root_cause_patch": LLMRoleOverride(temperature=0.0, max_tokens=16384, disable_thinking=True),
        "artifact_writer": LLMRoleOverride(temperature=0.0, max_tokens=16384, disable_thinking=True),
        "semantic_repair": LLMRoleOverride(temperature=0.0, max_tokens=8192, disable_thinking=True),
        "format_repair": LLMRoleOverride(temperature=0.0, max_tokens=8192, disable_thinking=True),
        "judge_review": LLMRoleOverride(temperature=0.0, max_tokens=8192, disable_thinking=True),
        "verify_acceptance": LLMRoleOverride(temperature=0.0, max_tokens=8192, disable_thinking=True),
    },
    # Nemotron Labs 3 Puzzle 75B-A9B on vLLM has the same operational
    # requirement as Super: default thinking can return reasoning without
    # message.content, so every agent call must explicitly request no-thinking.
    # Empirically it also tends to over-generate repair artifacts, so keep
    # artifact budgets below the generic Nemotron profile and rely on staged
    # repair rounds instead of one very long response.
    "nemotron-labs-puzzle-75b-agent": {
        **LEARNING_FUNCTION_PROFILES_NO_THINKING,
        "route_task": LLMRoleOverride(temperature=0.1, max_tokens=8192, disable_thinking=True),
        "plan_work": LLMRoleOverride(temperature=0.1, max_tokens=12288, disable_thinking=True),
        "explore_code": LLMRoleOverride(temperature=0.0, max_tokens=8192, disable_thinking=True),
        "failure_analysis": LLMRoleOverride(temperature=0.0, max_tokens=12288, disable_thinking=True),
        "patch_planner": LLMRoleOverride(temperature=0.0, max_tokens=12288, disable_thinking=True),
        "project_policy_triage": LLMRoleOverride(temperature=0.0, max_tokens=12288, disable_thinking=True),
        "generate_artifact": LLMRoleOverride(temperature=0.05, max_tokens=6144, disable_thinking=True),
        "repair_artifact": LLMRoleOverride(temperature=0.0, max_tokens=4096, disable_thinking=True),
        "root_cause_analysis": LLMRoleOverride(temperature=0.0, max_tokens=12288, disable_thinking=True),
        "root_cause_patch": LLMRoleOverride(temperature=0.0, max_tokens=4096, disable_thinking=True),
        "artifact_writer": LLMRoleOverride(temperature=0.0, max_tokens=4096, disable_thinking=True),
        "semantic_repair": LLMRoleOverride(temperature=0.0, max_tokens=8192, disable_thinking=True),
        "format_repair": LLMRoleOverride(temperature=0.0, max_tokens=8192, disable_thinking=True),
        "judge_review": LLMRoleOverride(temperature=0.0, max_tokens=8192, disable_thinking=True),
        "verify_acceptance": LLMRoleOverride(temperature=0.0, max_tokens=8192, disable_thinking=True),
    },
    # Qwen3 reasoning models are capable, but for this runner the highest-risk
    # failure mode is leaking long analysis into executable artifacts. Analysis
    # and review calls may think when the serving stack separates
    # reasoning_content from content; artifact-producing calls stay no-thinking
    # so patch protocols remain machine-parseable.
    "qwen-agent": {
        **LEARNING_FUNCTION_PROFILES_REASONING,
        "route_task": LLMRoleOverride(temperature=0.1, max_tokens=8192, disable_thinking=False),
        "plan_work": LLMRoleOverride(temperature=0.1, max_tokens=12288, disable_thinking=False),
        "explore_code": LLMRoleOverride(temperature=0.0, max_tokens=8192, disable_thinking=False),
        "failure_analysis": LLMRoleOverride(temperature=0.0, max_tokens=16384, disable_thinking=False),
        "patch_planner": LLMRoleOverride(temperature=0.0, max_tokens=12288, disable_thinking=False),
        "project_policy_triage": LLMRoleOverride(temperature=0.0, max_tokens=12288, disable_thinking=False),
        "generate_artifact": LLMRoleOverride(temperature=0.05, max_tokens=49152, disable_thinking=True),
        "repair_artifact": LLMRoleOverride(temperature=0.0, max_tokens=24576, disable_thinking=True),
        "root_cause_analysis": LLMRoleOverride(temperature=0.0, max_tokens=12288, disable_thinking=False),
        "root_cause_patch": LLMRoleOverride(temperature=0.0, max_tokens=16384, disable_thinking=True),
        "artifact_writer": LLMRoleOverride(temperature=0.0, max_tokens=16384, disable_thinking=True),
        "semantic_repair": LLMRoleOverride(temperature=0.0, max_tokens=8192, disable_thinking=True),
        "format_repair": LLMRoleOverride(temperature=0.0, max_tokens=8192, disable_thinking=True),
        "judge_review": LLMRoleOverride(temperature=0.0, max_tokens=8192, disable_thinking=False),
        "verify_acceptance": LLMRoleOverride(temperature=0.0, max_tokens=8192, disable_thinking=False),
    },
    # Deep profile is intentionally separate from qwen-agent. It allows hidden
    # reasoning only for non-artifact diagnostic calls. Artifact-producing calls
    # remain no-thinking so patch protocols stay machine-parseable.
    "qwen-agent-deep": {
        **LEARNING_FUNCTION_PROFILES_REASONING,
        "route_task": LLMRoleOverride(temperature=0.1, max_tokens=12288, disable_thinking=False),
        "plan_work": LLMRoleOverride(temperature=0.1, max_tokens=16384, disable_thinking=False),
        "failure_analysis": LLMRoleOverride(temperature=0.0, max_tokens=24576, disable_thinking=False),
        "patch_planner": LLMRoleOverride(temperature=0.0, max_tokens=16384, disable_thinking=False),
        "project_policy_triage": LLMRoleOverride(temperature=0.0, max_tokens=16384, disable_thinking=False),
        "generate_artifact": LLMRoleOverride(temperature=0.05, max_tokens=49152, disable_thinking=True),
        "repair_artifact": LLMRoleOverride(temperature=0.0, max_tokens=24576, disable_thinking=True),
        "root_cause_analysis": LLMRoleOverride(temperature=0.0, max_tokens=16384, disable_thinking=False),
        "root_cause_patch": LLMRoleOverride(temperature=0.0, max_tokens=16384, disable_thinking=True),
        "artifact_writer": LLMRoleOverride(temperature=0.0, max_tokens=16384, disable_thinking=True),
        "semantic_repair": LLMRoleOverride(temperature=0.0, max_tokens=8192, disable_thinking=True),
        "format_repair": LLMRoleOverride(temperature=0.0, max_tokens=8192, disable_thinking=True),
        "judge_review": LLMRoleOverride(temperature=0.0, max_tokens=12288, disable_thinking=False),
        "verify_acceptance": LLMRoleOverride(temperature=0.0, max_tokens=8192, disable_thinking=True),
    },
    # Ornith was the earlier baseline for this runner. Keep a named preset so
    # Qwen/Ornith comparisons change only one CLI switch and remain visible in
    # run manifests.
    "ornith-agent": {
        **LEARNING_FUNCTION_PROFILES_NO_THINKING,
        "route_task": LLMRoleOverride(temperature=0.2, max_tokens=8192, disable_thinking=True),
        "plan_work": LLMRoleOverride(temperature=0.2, max_tokens=8192, disable_thinking=True),
        "explore_code": LLMRoleOverride(temperature=0.0, max_tokens=8192, disable_thinking=True),
        "failure_analysis": LLMRoleOverride(temperature=0.0, max_tokens=8192, disable_thinking=True),
        "patch_planner": LLMRoleOverride(temperature=0.0, max_tokens=8192, disable_thinking=True),
        "project_policy_triage": LLMRoleOverride(temperature=0.0, max_tokens=8192, disable_thinking=True),
        "generate_artifact": LLMRoleOverride(temperature=0.1, max_tokens=65536, disable_thinking=True),
        "repair_artifact": LLMRoleOverride(temperature=0.05, max_tokens=32768, disable_thinking=True),
        "root_cause_analysis": LLMRoleOverride(temperature=0.0, max_tokens=8192, disable_thinking=True),
        "root_cause_patch": LLMRoleOverride(temperature=0.0, max_tokens=32768, disable_thinking=True),
        "artifact_writer": LLMRoleOverride(temperature=0.0, max_tokens=32768, disable_thinking=True),
        "semantic_repair": LLMRoleOverride(temperature=0.0, max_tokens=8192, disable_thinking=True),
        "format_repair": LLMRoleOverride(temperature=0.0, max_tokens=8192, disable_thinking=True),
        "judge_review": LLMRoleOverride(temperature=0.0, max_tokens=8192, disable_thinking=True),
        "verify_acceptance": LLMRoleOverride(temperature=0.0, max_tokens=8192, disable_thinking=True),
    },
    "ornith-agent-deep": {
        **LEARNING_FUNCTION_PROFILES_REASONING,
        "route_task": LLMRoleOverride(temperature=0.2, max_tokens=12288, disable_thinking=False),
        "plan_work": LLMRoleOverride(temperature=0.2, max_tokens=12288, disable_thinking=False),
        "failure_analysis": LLMRoleOverride(temperature=0.0, max_tokens=16384, disable_thinking=False),
        "patch_planner": LLMRoleOverride(temperature=0.0, max_tokens=12288, disable_thinking=False),
        "project_policy_triage": LLMRoleOverride(temperature=0.0, max_tokens=12288, disable_thinking=False),
        "generate_artifact": LLMRoleOverride(temperature=0.1, max_tokens=65536, disable_thinking=True),
        "repair_artifact": LLMRoleOverride(temperature=0.05, max_tokens=32768, disable_thinking=True),
        "root_cause_analysis": LLMRoleOverride(temperature=0.0, max_tokens=16384, disable_thinking=False),
        "root_cause_patch": LLMRoleOverride(temperature=0.0, max_tokens=32768, disable_thinking=True),
        "artifact_writer": LLMRoleOverride(temperature=0.0, max_tokens=32768, disable_thinking=True),
        "semantic_repair": LLMRoleOverride(temperature=0.0, max_tokens=8192, disable_thinking=True),
        "format_repair": LLMRoleOverride(temperature=0.0, max_tokens=8192, disable_thinking=True),
        "judge_review": LLMRoleOverride(temperature=0.0, max_tokens=12288, disable_thinking=False),
        "verify_acceptance": LLMRoleOverride(temperature=0.0, max_tokens=8192, disable_thinking=True),
    },
}


@dataclasses.dataclass(frozen=True)
class LLMProbeResult:
    name: str
    status: str
    detail: str


@dataclasses.dataclass(frozen=True)
class LLMStreamStats:
    chunks_received: int
    content_chunks: int
    reasoning_chunks: int
    bytes_received: int
    first_chunk_at: float | None
    last_chunk_at: float | None
    duration_seconds: float
    partial_output_path: str | None = None


@dataclasses.dataclass(frozen=True)
class ArtifactStreamGuardResult:
    should_abort: bool
    reason: str = ""
    code: str = ""
    score: int = 0
    threshold: int = 0


@dataclasses.dataclass(frozen=True)
class FileArtifact:
    path: str
    content: str
    mode: str = "replace"


@dataclasses.dataclass(frozen=True)
class SearchReplaceArtifact:
    path: str
    search: str
    replace: str


@dataclasses.dataclass(frozen=True)
class ArtifactPathPolicy:
    allowed_paths: tuple[str, ...]
    readonly_paths: tuple[str, ...] = ()
    existing_paths: tuple[str, ...] = ()
    allow_extra_new_files: bool = False


def normalize_agent_level(agent_level: str = "default") -> str:
    normalized = (agent_level or "default").strip().lower()
    if normalized in {"supervisor", "spec"}:
        return "pm"
    if normalized in {"review", "security", "deploy", "sre", "observe"}:
        return "judge"
    return normalized or "default"


def normalize_call_function(call_function: str = "default") -> str:
    normalized = (call_function or "default").strip().lower().replace("-", "_")
    aliases = {
        "supervisor": "route_task",
        "route": "route_task",
        "spec": "plan_work",
        "architect": "plan_work",
        "ddd": "plan_work",
        "tdd": "plan_work",
        "pm": "plan_work",
        "coder": "generate_artifact",
        "code": "generate_artifact",
        "judge": "judge_review",
        "review": "judge_review",
        "security": "judge_review",
        "deploy": "judge_review",
        "acceptance": "verify_acceptance",
    }
    return aliases.get(normalized, normalized or "default")


def default_call_function_for(agent_level: str, skill_name: str = "") -> str:
    skill_key = normalize_call_function(skill_name)
    if skill_key in DEFAULT_FUNCTION_PROFILES:
        return skill_key
    role = normalize_agent_level(agent_level)
    if role == "pm":
        return "plan_work"
    if role == "coder":
        return "generate_artifact"
    if role == "judge":
        return "judge_review"
    return "default"


@dataclasses.dataclass(frozen=True)
class ArtifactLintFinding:
    severity: str
    code: str
    message: str
    path: str | None = None


@dataclasses.dataclass(frozen=True)
class RepairAdvice:
    strategy: str
    focus_files: tuple[str, ...]
    instructions: tuple[str, ...]
    evidence: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class RepairAction:
    action_id: str
    kind: str
    source: str
    target_paths: tuple[str, ...]
    required_covers: tuple[str, ...]
    instruction: str
    evidence: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class ProjectPolicyTriage:
    trigger: str
    case_type: str
    confidence: str
    safe_next_action: str
    editable_paths: tuple[str, ...] = ()
    readonly_paths: tuple[str, ...] = ()
    forbidden_actions: tuple[str, ...] = ()
    project_policy_basis: tuple[str, ...] = ()
    rationale: str = ""


@dataclasses.dataclass(frozen=True)
class SemanticContract:
    contract_id: str
    kind: str
    text: str
    source: str
    focus_files: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class SemanticRepairFormatIssue:
    code: str
    message: str
    path: str | None = None


@dataclasses.dataclass(frozen=True)
class StageWorkItem:
    stage_id: str
    title: str
    goal: str
    suggested_paths: tuple[str, ...]
    test_focus: tuple[str, ...]
    test_commands: tuple[str, ...] = ()
    required_observables: tuple[str, ...] = ()
    writable_paths: tuple[str, ...] = ()
    readonly_evidence_paths: tuple[str, ...] = ()
    api_profile: tuple[str, ...] = ()
    max_rounds: int | None = None


@dataclasses.dataclass(frozen=True)
class FailureTransition:
    failure_type: str
    next_role: str
    action: str
    owner: str
    instructions: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class MissingContextRequest:
    paths: tuple[str, ...]
    reason: str = ""


@dataclasses.dataclass(frozen=True)
class StageRunSummary:
    stage_id: str
    title: str
    status: str
    run_dir: str
    exit_code: int
    api_calls: int = 0
    final_verdict: str = ""
    changed_paths: tuple[str, ...] = ()
    required_paths: tuple[str, ...] = ()
    failure_summary: dict[str, object] | None = None


@dataclasses.dataclass(frozen=True)
class SupervisorRoute:
    task_type: str
    danger_signals: tuple[str, ...]
    phases: tuple[str, ...]
    reason: str
