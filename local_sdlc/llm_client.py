"""OpenAI-compatible local LLM client and per-call API settings."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import signal
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from typing import Callable

from .config import AppConfig, config_bool, config_number, config_string, config_string_list, config_value, load_app_config
from .models import *
from .utils import compact_preview, strip_markdown_fence

MAX_REASONING_RECORD_CHARS = 20000


def profiles_for_served_models(served_models: list[str]) -> list[str]:
    served = {str(model).strip() for model in served_models if str(model).strip()}
    return sorted(
        profile
        for profile, default_model in MODEL_PROFILE_DEFAULT_MODELS.items()
        if default_model in served
    )


def model_profile_compatibility(
    profile: str,
    requested_model: str,
    served_models: list[str],
) -> tuple[bool, str]:
    normalized_profile = normalize_model_profile(profile)
    if normalized_profile == "default":
        return True, "default profile does not require a fixed served model"

    served = [str(model).strip() for model in served_models if str(model).strip()]
    if requested_model in served:
        return True, f"profile model {requested_model!r} is served by /v1/models"

    available = ", ".join(served) if served else "(none)"
    matching_profiles = profiles_for_served_models(served)
    suggestion = (
        f" Matching profiles for the current runtime: {', '.join(matching_profiles)}."
        if matching_profiles
        else ""
    )
    return (
        False,
        f"model profile {normalized_profile!r} requests {requested_model!r}, but /v1/models exposes: "
        f"{available}.{suggestion} Switch the externally managed resident model or select a matching "
        "--model-profile. No generation request was sent.",
    )


def require_profile_model_compatibility(
    profile: str,
    requested_model: str,
    served_models: list[str],
) -> None:
    compatible, detail = model_profile_compatibility(profile, requested_model, served_models)
    if not compatible:
        raise RunnerError(detail)

class LocalLLMClient:
    def __init__(self, config: LLMConfig):
        self.config = config
        self.reasoning_records: list[dict[str, object]] = []
        self.completion_recovery_records: list[dict[str, object]] = []
        self.generation_request_count = 0
        self.last_completion_attempts = 0
        self.runtime_timeout_limit: float | None = None
        self.runtime_timeout_callback: Callable[[], None] | None = None
        self.runtime_progress_callback: Callable[[LLMStreamStats], None] | None = None
        self.runtime_completion_fallback_callback: Callable[[str, str, str], None] | None = None

    def set_runtime_timeout_limit(
        self,
        timeout: float | None,
        callback: Callable[[], None] | None = None,
    ) -> None:
        self.runtime_timeout_limit = None if timeout is None else max(0.001, float(timeout))
        self.runtime_timeout_callback = callback

    def _effective_timeout(self, requested: float) -> float:
        if self.runtime_timeout_limit is None:
            return requested
        return max(0.001, min(float(requested), self.runtime_timeout_limit))

    def _notify_runtime_timeout(self) -> None:
        if self.runtime_timeout_callback is not None:
            self.runtime_timeout_callback()

    def set_runtime_progress_callback(
        self,
        callback: Callable[[LLMStreamStats], None] | None,
    ) -> None:
        self.runtime_progress_callback = callback

    def set_runtime_completion_fallback_callback(
        self,
        callback: Callable[[str, str, str], None] | None,
    ) -> None:
        """Authorize and account for one physical no-thinking retry."""
        self.runtime_completion_fallback_callback = callback

    def _record_reasoning_content(
        self,
        *,
        agent_level: str,
        call_function: str,
        model: str,
        reasoning: object,
        original_chars: int | None = None,
    ) -> None:
        text = str(reasoning or "")
        if not text.strip():
            return
        chars = int(original_chars) if original_chars is not None else len(text)
        truncated = chars > len(text) or len(text) > MAX_REASONING_RECORD_CHARS
        self.reasoning_records.append(
            {
                "agent_level": normalize_agent_level(agent_level),
                "call_function": normalize_call_function(call_function),
                "model": model,
                "chars": chars,
                "truncated": truncated,
                "reasoning_content": text[:MAX_REASONING_RECORD_CHARS],
            }
        )

    def _alarm_handler(self, _signum, _frame):
        raise TimeoutError("wall-clock request timeout")

    def call_settings(
        self,
        agent_level: str = "default",
        call_function: str = "default",
        default_model: str | None = None,
    ) -> LLMCallSettings:
        normalized = normalize_agent_level(agent_level)
        normalized_function = normalize_call_function(call_function)
        role_override = self.config.role_overrides.get(normalized) or self.config.role_overrides.get("default")
        function_override = (
            self.config.function_overrides.get(normalized_function)
            or DEFAULT_FUNCTION_PROFILES.get(normalized_function)
        )
        model = default_model if default_model is not None else self.config.model
        temperature = self.config.temperature
        max_tokens = self.config.max_tokens
        disable_thinking = self.config.disable_thinking
        reasoning_effort = None
        for override in (role_override, function_override):
            if not override:
                continue
            if override.model is not None:
                model = override.model
            if override.temperature is not None:
                temperature = override.temperature
            if override.max_tokens is not None:
                max_tokens = override.max_tokens
            if override.disable_thinking is not None:
                disable_thinking = override.disable_thinking
            if override.reasoning_effort is not None:
                reasoning_effort = override.reasoning_effort
        if disable_thinking:
            reasoning_effort = None
        return LLMCallSettings(
            agent_level=normalized,
            call_function=normalized_function,
            model=model or "",
            temperature=temperature,
            max_tokens=max_tokens,
            disable_thinking=disable_thinking,
            reasoning_effort=reasoning_effort,
        )

    @staticmethod
    def _chat_template_kwargs(settings: LLMCallSettings) -> dict | None:
        if settings.disable_thinking:
            return {"enable_thinking": False}
        if settings.reasoning_effort:
            return {
                "enable_thinking": True,
                "reasoning_effort": settings.reasoning_effort,
            }
        return None

    def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        timeout: float | None = None,
    ) -> dict:
        url = self.config.base_url.rstrip("/") + path
        requested_timeout = self.config.timeout if timeout is None else timeout
        request_timeout = self._effective_timeout(requested_timeout)
        data = None
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        use_wall_clock_alarm = threading.current_thread() is threading.main_thread()
        old_handler = None
        try:
            if use_wall_clock_alarm:
                old_handler = signal.getsignal(signal.SIGALRM)
                signal.signal(signal.SIGALRM, self._alarm_handler)
                signal.setitimer(signal.ITIMER_REAL, request_timeout)
            with urllib.request.urlopen(request, timeout=request_timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (TimeoutError, socket.timeout) as exc:
            self._notify_runtime_timeout()
            raise LLMTimeoutError(path, request_timeout) from exc
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RunnerError(f"LLM API HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                self._notify_runtime_timeout()
                raise LLMTimeoutError(path, request_timeout) from exc
            raise RunnerError(f"LLM API connection failed: {exc.reason}") from exc
        finally:
            if use_wall_clock_alarm:
                signal.setitimer(signal.ITIMER_REAL, 0)
                if old_handler is not None:
                    signal.signal(signal.SIGALRM, old_handler)

    def models(self, timeout: float | None = None) -> list[str]:
        request_timeout = self.config.health_timeout if timeout is None else timeout
        result = self._request("GET", "/models", timeout=request_timeout)
        return [item.get("id", "") for item in result.get("data", []) if item.get("id")]

    def health_check(self) -> tuple[bool, str, list[str]]:
        try:
            result = self._request("GET", "/models", timeout=self.config.health_timeout)
        except RunnerError as exc:
            return False, f"unreachable (/v1/models failed: {exc})", []

        models = [item.get("id", "") for item in result.get("data", []) if item.get("id")]
        if models:
            return True, f"alive (/v1/models OK: {', '.join(models)})", models
        return True, "alive (/v1/models OK, but no models listed)", []

    def health_probe(self) -> str:
        return self.health_check()[1]

    def chat_completion_raw(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
        temperature: float,
        max_tokens: int,
        timeout: float | None = None,
        chat_template_kwargs: dict | None = None,
        extra_payload: dict | None = None,
    ) -> dict:
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if chat_template_kwargs is not None:
            payload["chat_template_kwargs"] = chat_template_kwargs
        if extra_payload:
            payload.update(extra_payload)
        return self._request("POST", "/chat/completions", payload, timeout=timeout)

    def chat_completion_stream(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
        temperature: float,
        max_tokens: int,
        timeout: float | None = None,
        chat_template_kwargs: dict | None = None,
        partial_output_path: Path | None = None,
        progress_callback: Callable[[LLMStreamStats], None] | None = None,
        stream_guard: Callable[[str], ArtifactStreamGuardResult] | None = None,
    ) -> tuple[str, LLMStreamStats, str, int]:
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if chat_template_kwargs is not None:
            payload["chat_template_kwargs"] = chat_template_kwargs
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        requested_timeout = self.config.timeout if timeout is None else timeout
        request_timeout = self._effective_timeout(requested_timeout)
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        output_parts: list[str] = []
        reasoning_parts: list[str] = []
        reasoning_chars = 0
        reasoning_saved_chars = 0
        chunks_received = 0
        content_chunks = 0
        reasoning_chunks = 0
        bytes_received = 0
        start = time.monotonic()
        first_chunk_at: float | None = None
        last_chunk_at: float | None = None
        last_progress_callback_at: float | None = None
        partial_file = None
        partial_path_text = str(partial_output_path) if partial_output_path else None
        use_wall_clock_alarm = threading.current_thread() is threading.main_thread()
        old_handler = None
        try:
            if use_wall_clock_alarm:
                old_handler = signal.getsignal(signal.SIGALRM)
                signal.signal(signal.SIGALRM, self._alarm_handler)
                signal.setitimer(signal.ITIMER_REAL, request_timeout)
            if partial_output_path is not None:
                partial_output_path.parent.mkdir(parents=True, exist_ok=True)
                partial_file = partial_output_path.open("w", encoding="utf-8")
            with urllib.request.urlopen(request, timeout=request_timeout) as response:
                content_type = response.headers.get("content-type", "")
                if "text/event-stream" not in content_type:
                    raise RunnerError(f"LLM streaming API returned unexpected content-type: {content_type}")
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data: "):
                        continue
                    data = line[len("data: ") :]
                    now = time.monotonic()
                    if data == "[DONE]":
                        break
                    chunks_received += 1
                    if first_chunk_at is None:
                        first_chunk_at = now - start
                    last_chunk_at = now - start
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choice = (event.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    content = delta.get("content")
                    reasoning = delta.get("reasoning") or delta.get("reasoning_content")
                    if content:
                        text = str(content)
                        output_parts.append(text)
                        content_chunks += 1
                        encoded_len = len(text.encode("utf-8"))
                        bytes_received += encoded_len
                        if partial_file is not None:
                            partial_file.write(text)
                            partial_file.flush()
                        if stream_guard:
                            partial_text = "".join(output_parts)
                            guard_result = stream_guard(partial_text)
                            if guard_result.should_abort:
                                stats = LLMStreamStats(
                                    chunks_received=chunks_received,
                                    content_chunks=content_chunks,
                                    reasoning_chunks=reasoning_chunks,
                                    bytes_received=bytes_received,
                                    first_chunk_at=first_chunk_at,
                                    last_chunk_at=last_chunk_at,
                                    duration_seconds=now - start,
                                    partial_output_path=partial_path_text,
                                )
                                raise LLMStreamAbortError(
                                    guard_result.reason,
                                    partial_text,
                                    stats,
                                    code=guard_result.code,
                                    score=guard_result.score,
                                    threshold=guard_result.threshold,
                                )
                    elif reasoning:
                        reasoning_text = str(reasoning)
                        reasoning_chunks += 1
                        reasoning_chars += len(reasoning_text)
                        if reasoning_saved_chars < MAX_REASONING_RECORD_CHARS:
                            remaining = MAX_REASONING_RECORD_CHARS - reasoning_saved_chars
                            saved_part = reasoning_text[:remaining]
                            reasoning_parts.append(saved_part)
                            reasoning_saved_chars += len(saved_part)
                    callback_interval = max(
                        0.05,
                        min(5.0, self._effective_timeout(requested_timeout) / 2.0),
                    )
                    callback_due = (
                        last_progress_callback_at is None
                        or content_chunks == 1
                        or chunks_received % 20 == 0
                        or now - last_progress_callback_at >= callback_interval
                    )
                    if (progress_callback or self.runtime_progress_callback) and callback_due:
                        current_stats = LLMStreamStats(
                            chunks_received=chunks_received,
                            content_chunks=content_chunks,
                            reasoning_chunks=reasoning_chunks,
                            bytes_received=bytes_received,
                            first_chunk_at=first_chunk_at,
                            last_chunk_at=last_chunk_at,
                            duration_seconds=now - start,
                            partial_output_path=partial_path_text,
                        )
                        if progress_callback:
                            progress_callback(current_stats)
                        elif self.runtime_progress_callback:
                            self.runtime_progress_callback(current_stats)
                        last_progress_callback_at = now
                        # The callback may refresh a persistent no-progress
                        # deadline. Reset the main-thread alarm without
                        # extending the independently recomputed wall budget.
                        if use_wall_clock_alarm:
                            signal.setitimer(
                                signal.ITIMER_REAL,
                                self._effective_timeout(requested_timeout),
                            )
        except (TimeoutError, socket.timeout) as exc:
            self._notify_runtime_timeout()
            raise LLMTimeoutError("/chat/completions", request_timeout) from exc
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RunnerError(f"LLM API HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                self._notify_runtime_timeout()
                raise LLMTimeoutError("/chat/completions", request_timeout) from exc
            raise RunnerError(f"LLM API connection failed: {exc.reason}") from exc
        finally:
            if use_wall_clock_alarm:
                signal.setitimer(signal.ITIMER_REAL, 0)
                if old_handler is not None:
                    signal.signal(signal.SIGALRM, old_handler)
            if partial_file is not None:
                partial_file.close()
        duration = time.monotonic() - start
        stats = LLMStreamStats(
            chunks_received=chunks_received,
            content_chunks=content_chunks,
            reasoning_chunks=reasoning_chunks,
            bytes_received=bytes_received,
            first_chunk_at=first_chunk_at,
            last_chunk_at=last_chunk_at,
            duration_seconds=duration,
            partial_output_path=partial_path_text,
        )
        if progress_callback:
            progress_callback(stats)
        elif self.runtime_progress_callback:
            self.runtime_progress_callback(stats)
        content_text = "".join(output_parts).strip()
        reasoning_text = "".join(reasoning_parts)
        if not content_text and reasoning_chunks:
            raise LLMReasoningOnlyError(
                "LLM streaming API returned reasoning-only output with empty content; "
                "keep thinking disabled for artifact calls or use a model that emits delta.content",
                reasoning=reasoning_text,
                reasoning_chars=reasoning_chars,
            )
        if not content_text:
            raise RunnerError("LLM streaming API returned an empty message with no content")
        return content_text, stats, reasoning_text, reasoning_chars

    def complete(
        self,
        messages: list[dict[str, str]],
        agent_level: str = "default",
        call_function: str = "default",
        stream_output_path: Path | None = None,
        stream_callback: Callable[[LLMStreamStats], None] | None = None,
        stream_guard: Callable[[str], ArtifactStreamGuardResult] | None = None,
    ) -> str:
        self.last_completion_attempts = 0
        alive, health_status, health_models = self.health_check()
        if not alive:
            raise RunnerError(f"LLM API preflight failed: {health_status}")

        selected_model = self.config.model
        if not selected_model:
            models = health_models
            if not models:
                raise RunnerError("LLM API returned no models; pass --model explicitly")
            selected_model = models[0]
        settings = self.call_settings(agent_level, call_function, default_model=selected_model)
        model = settings.model or selected_model
        require_profile_model_compatibility(self.config.model_profile, model, health_models)

        def complete_once(
            active_settings: LLMCallSettings,
            active_messages: list[dict[str, str]] | None = None,
        ) -> str:
            request_messages = active_messages or messages
            self.last_completion_attempts += 1
            self.generation_request_count += 1
            try:
                if self.config.stream:
                    content, _stats, reasoning, reasoning_chars = self.chat_completion_stream(
                        request_messages,
                        model=model,
                        temperature=active_settings.temperature,
                        max_tokens=active_settings.max_tokens,
                        chat_template_kwargs=self._chat_template_kwargs(active_settings),
                        partial_output_path=stream_output_path,
                        progress_callback=stream_callback,
                        stream_guard=stream_guard,
                    )
                    if reasoning:
                        self._record_reasoning_content(
                            agent_level=agent_level,
                            call_function=call_function,
                            model=model,
                            reasoning=reasoning,
                            original_chars=reasoning_chars,
                        )
                    return content
                result = self.chat_completion_raw(
                    request_messages,
                    model=model,
                    temperature=active_settings.temperature,
                    max_tokens=active_settings.max_tokens,
                    chat_template_kwargs=self._chat_template_kwargs(active_settings),
                )
            except LLMTimeoutError as exc:
                health = self.health_probe()
                raise RunnerError(
                    f"LLM generation request timed out after {exc.timeout:g}s. "
                    f"API health after timeout: {health}. "
                    "This does not prove the server is dead; it means this generation "
                    "did not return within the client timeout. For large jobs, retry "
                    "with --timeout 600, reduce --max-tokens/context, or use a smaller step."
                ) from exc
            choices = result.get("choices") or []
            if not choices:
                raise RunnerError("LLM API returned no choices")
            choice = choices[0]
            message = choice.get("message") or {}
            reasoning = message.get("reasoning") or message.get("reasoning_content")
            content = message.get("content")
            if content is None:
                content = choice.get("text")
            if content is None:
                if reasoning:
                    raise LLMReasoningOnlyError(
                        "LLM API returned reasoning-only output with empty content; "
                        "try the default no-thinking mode, a larger --max-tokens, or a model that emits message.content",
                        reasoning=str(reasoning),
                        reasoning_chars=len(str(reasoning)),
                    )
                raise RunnerError("LLM API returned an empty message with no content")
            if reasoning:
                self._record_reasoning_content(
                    agent_level=agent_level,
                    call_function=call_function,
                    model=model,
                    reasoning=reasoning,
                )
            return str(content).strip()

        try:
            return complete_once(settings)
        except LLMReasoningOnlyError as exc:
            if exc.reasoning:
                self._record_reasoning_content(
                    agent_level=agent_level,
                    call_function=call_function,
                    model=model,
                    reasoning=exc.reasoning,
                    original_chars=exc.reasoning_chars,
                )
            if settings.disable_thinking:
                raise
            if self.runtime_completion_fallback_callback is not None:
                self.runtime_completion_fallback_callback(
                    normalize_agent_level(agent_level),
                    normalize_call_function(call_function),
                    "reasoning_only_output",
                )
            fallback_settings = dataclasses.replace(
                settings,
                temperature=0.0,
                max_tokens=min(settings.max_tokens, 2048),
                disable_thinking=True,
                reasoning_effort=None,
            )
            reasoning_excerpt = exc.reasoning[-6000:].strip()
            fallback_messages = [
                *messages,
                {
                    "role": "assistant",
                    "content": (
                        "Prior private analysis excerpt for same-call condensation:\n"
                        "<private_analysis>\n"
                        f"{reasoning_excerpt}\n"
                        "</private_analysis>"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Condense the prior analysis into the exact output contract requested by the original "
                        "prompt. Return only the final answer, with no analysis, self-revision, or commentary. "
                        "Do not introduce conclusions unsupported by the prior analysis or supplied evidence."
                    ),
                },
            ]
            self.completion_recovery_records.append(
                {
                    "agent_level": normalize_agent_level(agent_level),
                    "call_function": normalize_call_function(call_function),
                    "model": model,
                    "reason": "reasoning_only_output",
                    "action": "condense_reasoning_once_with_thinking_off",
                    "fallback_max_tokens": fallback_settings.max_tokens,
                    "reasoning_excerpt_chars": len(reasoning_excerpt),
                }
            )
            return complete_once(fallback_settings, fallback_messages)

def _app_config_for_args(args: argparse.Namespace) -> AppConfig:
    project = getattr(args, "project", Path.cwd())
    if not isinstance(project, Path):
        project = Path(str(project))
    explicit = getattr(args, "config_file", None)
    if explicit is not None and not isinstance(explicit, Path):
        explicit = Path(str(explicit))
    return load_app_config(project.resolve(), explicit)


def _cli_value(args: argparse.Namespace, name: str) -> Any:
    value = getattr(args, name, None)
    if isinstance(value, str):
        return value.strip() or None
    return value


def _float_setting(args: argparse.Namespace, section: dict[str, Any], name: str, default: float) -> float:
    cli = _cli_value(args, name)
    if cli is not None:
        return float(cli)
    config = config_number(section, name, number_type=float)
    return float(config) if config is not None else float(default)


def _int_setting(args: argparse.Namespace, section: dict[str, Any], name: str, default: int) -> int:
    cli = _cli_value(args, name)
    if cli is not None:
        return int(cli)
    config = config_number(section, name, number_type=int)
    return int(config) if config is not None else int(default)


def _bool_setting(args: argparse.Namespace, section: dict[str, Any], name: str, default: bool) -> bool:
    cli = _cli_value(args, name)
    if cli is not None:
        return bool(cli)
    config = config_bool(section, name)
    return bool(config) if config is not None else bool(default)


def effective_model_profile(args: argparse.Namespace, app_config: AppConfig | None = None) -> str:
    loaded = app_config or _app_config_for_args(args)
    raw = _cli_value(args, "model_profile") or config_string(loaded.llm, "model_profile") or "default"
    return normalize_model_profile(str(raw))


def build_config(args: argparse.Namespace) -> LLMConfig:
    app_config = _app_config_for_args(args)
    llm_section = app_config.llm
    role_overrides = build_role_overrides(args, app_config)
    function_overrides = build_function_overrides(args, app_config)
    model_profile = effective_model_profile(args, app_config)
    profile_model = MODEL_PROFILE_DEFAULT_MODELS.get(model_profile, "")
    api_key_env_name = config_string(llm_section, "api_key_env")
    api_key_from_config_env = os.environ.get(api_key_env_name, "") if api_key_env_name else ""
    timeout = _float_setting(args, llm_section, "timeout", DEFAULT_TIMEOUT)
    health_timeout = _float_setting(args, llm_section, "health_timeout", DEFAULT_HEALTH_TIMEOUT)
    temperature = _float_setting(args, llm_section, "temperature", 0.2)
    max_tokens = _int_setting(args, llm_section, "max_tokens", 4096)
    if timeout <= 0:
        raise RunnerError("timeout must be positive")
    if health_timeout <= 0:
        raise RunnerError("health_timeout must be positive")
    if temperature < 0:
        raise RunnerError("temperature must be non-negative")
    if max_tokens < 1:
        raise RunnerError("max_tokens must be at least 1")
    return LLMConfig(
        base_url=(
            _cli_value(args, "base_url")
            or config_string(llm_section, "base_url")
            or os.environ.get("LOCAL_LLM_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or DEFAULT_BASE_URL
        ),
        api_key=(
            _cli_value(args, "api_key")
            or os.environ.get("LOCAL_SDLC_API_KEY")
            or api_key_from_config_env
            or config_string(llm_section, "api_key")
            or os.environ.get("LOCAL_LLM_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or DEFAULT_API_KEY
        ),
        model=(
            _cli_value(args, "model")
            or config_string(llm_section, "model")
            or os.environ.get("LOCAL_LLM_MODEL")
            or profile_model
            or DEFAULT_MODEL
        ),
        timeout=timeout,
        health_timeout=health_timeout,
        temperature=temperature,
        max_tokens=max_tokens,
        disable_thinking=not _bool_setting(args, llm_section, "enable_thinking", False),
        stream=_bool_setting(args, llm_section, "stream", False),
        model_profile=model_profile,
        config_file=str(app_config.path or ""),
        role_overrides=role_overrides,
        function_overrides=function_overrides,
    )

def normalize_model_profile(value: str | None) -> str:
    raw = (value or "default").strip().lower()
    if raw == "auto":
        return "default"
    if raw not in MODEL_PROFILE_ALIASES:
        available = ", ".join(sorted(MODEL_PROFILE_ALIASES))
        raise RunnerError(f"unknown --model-profile {value!r}; available: {available}")
    return MODEL_PROFILE_ALIASES[raw]

def profile_function_overrides(profile: str) -> dict[str, LLMRoleOverride]:
    normalized = normalize_model_profile(profile)
    return dict(MODEL_PROFILE_FUNCTION_PROFILES.get(normalized, {}))

def parse_role_thinking(value: str | None) -> bool | None:
    if not value or value == "default":
        return None
    value = value.strip().lower()
    if value in {"off", "false", "0", "no"}:
        return True
    if value in {"on", "true", "1", "yes"}:
        return False
    raise RunnerError(f"unknown thinking mode: {value}")


def _config_thinking(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return not value
    return parse_role_thinking(str(value))


def _mapping_override(mapping: dict[str, Any], source: str) -> LLMRoleOverride:
    temperature_raw = config_value(mapping, "temperature")
    max_tokens_raw = config_value(mapping, "max_tokens")
    thinking_raw = config_value(mapping, "thinking")
    model = config_string(mapping, "model")
    temperature = float(temperature_raw) if temperature_raw is not None else None
    max_tokens = int(max_tokens_raw) if max_tokens_raw is not None else None
    disable_thinking = _config_thinking(thinking_raw)
    if temperature is not None and temperature < 0:
        raise RunnerError(f"{source} temperature must be non-negative")
    if max_tokens is not None and max_tokens < 1:
        raise RunnerError(f"{source} max_tokens must be at least 1")
    return LLMRoleOverride(
        temperature=temperature,
        max_tokens=max_tokens,
        disable_thinking=disable_thinking,
        model=model,
    )


def _role_section(llm_section: dict[str, Any], role: str) -> dict[str, Any]:
    roles = config_value(llm_section, "role_profiles", "roles")
    role_config = roles.get(role) if isinstance(roles, dict) else None
    return role_config if isinstance(role_config, dict) else {}


def build_role_overrides(
    args: argparse.Namespace,
    app_config: AppConfig | None = None,
) -> dict[str, LLMRoleOverride]:
    loaded = app_config or _app_config_for_args(args)
    llm_section = loaded.llm
    overrides: dict[str, LLMRoleOverride] = {}
    defaults = {
        "pm": (DEFAULT_PM_MAX_TOKENS, 0.2, "default"),
        "coder": (DEFAULT_CODER_MAX_TOKENS, 0.1, "off"),
        "judge": (DEFAULT_JUDGE_MAX_TOKENS, 0.0, "off"),
    }
    for role in ("pm", "coder", "judge"):
        default_max_tokens, default_temperature, default_thinking = defaults[role]
        role_section = _role_section(llm_section, role)
        max_tokens = _cli_value(args, f"{role}_max_tokens")
        if max_tokens is None:
            max_tokens = config_number(role_section, "max_tokens", number_type=int)
        if max_tokens is None:
            max_tokens = config_number(llm_section, f"{role}_max_tokens", number_type=int)
        if max_tokens is None:
            max_tokens = default_max_tokens

        temperature = _cli_value(args, f"{role}_temperature")
        if temperature is None:
            temperature = config_number(role_section, "temperature", number_type=float)
        if temperature is None:
            temperature = config_number(llm_section, f"{role}_temperature", number_type=float)
        if temperature is None:
            temperature = default_temperature

        thinking_raw = _cli_value(args, f"{role}_thinking")
        if thinking_raw is None:
            thinking_raw = config_value(role_section, "thinking")
        if thinking_raw is None:
            thinking_raw = config_value(llm_section, f"{role}_thinking")
        if thinking_raw is None:
            thinking_raw = default_thinking
        thinking = _config_thinking(thinking_raw)
        if max_tokens is not None and max_tokens < 1:
            raise RunnerError(f"--{role}-max-tokens must be at least 1")
        if temperature is not None and temperature < 0:
            raise RunnerError(f"--{role}-temperature must be non-negative")
        if max_tokens is None and temperature is None and thinking is None:
            continue
        overrides[role] = LLMRoleOverride(
            temperature=temperature,
            max_tokens=max_tokens,
            disable_thinking=thinking,
        )
    return overrides

def parse_api_profile_override(value: str) -> tuple[str, LLMRoleOverride]:
    if ":" not in value:
        raise RunnerError("--api-profile must use function:key=value,...")
    name, raw_pairs = value.split(":", 1)
    function_name = normalize_call_function(name)
    if not function_name or function_name == "default":
        raise RunnerError("--api-profile requires a concrete function name")
    temperature: float | None = None
    max_tokens: int | None = None
    disable_thinking: bool | None = None
    model: str | None = None
    for raw_pair in raw_pairs.split(","):
        if not raw_pair.strip():
            continue
        if "=" not in raw_pair:
            raise RunnerError(f"invalid --api-profile item: {raw_pair!r}")
        key, raw = [part.strip() for part in raw_pair.split("=", 1)]
        if key == "temperature":
            temperature = float(raw)
            if temperature < 0:
                raise RunnerError("--api-profile temperature must be non-negative")
        elif key in {"max_tokens", "tokens"}:
            max_tokens = int(raw)
            if max_tokens < 1:
                raise RunnerError("--api-profile max_tokens must be at least 1")
        elif key == "thinking":
            disable_thinking = parse_role_thinking(raw)
            if disable_thinking is None:
                raise RunnerError("--api-profile thinking must be on or off")
        elif key == "model":
            model = raw
            if not model:
                raise RunnerError("--api-profile model must not be empty")
        else:
            raise RunnerError(f"unknown --api-profile key: {key}")
    return function_name, LLMRoleOverride(
        temperature=temperature,
        max_tokens=max_tokens,
        disable_thinking=disable_thinking,
        model=model,
    )

def build_function_overrides(
    args: argparse.Namespace,
    app_config: AppConfig | None = None,
) -> dict[str, LLMRoleOverride]:
    loaded = app_config or _app_config_for_args(args)
    llm_section = loaded.llm
    overrides: dict[str, LLMRoleOverride] = profile_function_overrides(effective_model_profile(args, loaded))
    function_profiles = config_value(llm_section, "function_profiles", "functions")
    if isinstance(function_profiles, dict):
        for raw_name, raw_override in function_profiles.items():
            if not isinstance(raw_override, dict):
                raise RunnerError(f"function_profiles.{raw_name} must be an object")
            overrides[normalize_call_function(str(raw_name))] = _mapping_override(
                raw_override,
                f"function_profiles.{raw_name}",
            )
    for value in config_string_list(llm_section, "api_profile") or []:
        name, override = parse_api_profile_override(value)
        overrides[name] = override
    for value in getattr(args, "api_profile", []) or []:
        name, override = parse_api_profile_override(value)
        overrides[name] = override
    return overrides

def first_chat_message(result: dict) -> tuple[dict, str]:
    choices = result.get("choices") or []
    if not choices:
        return {}, ""
    choice = choices[0] or {}
    message = choice.get("message") or {}
    finish_reason = str(choice.get("finish_reason") or choice.get("stop_reason") or "")
    return message, finish_reason

def parse_json_probe_content(content: str) -> object:
    candidate = strip_markdown_fence(content).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", candidate, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))

def run_llm_capability_probes(client: LocalLLMClient, model: str, timeout: float) -> list[LLMProbeResult]:
    probes: list[LLMProbeResult] = []

    def run_probe(name: str, payload: dict, checker) -> None:
        started = time.monotonic()
        try:
            result = client.chat_completion_raw(timeout=timeout, **payload)
            duration = time.monotonic() - started
            probes.append(checker(result, duration))
        except RunnerError as exc:
            probes.append(LLMProbeResult(name, "FAIL", str(exc)))

    no_thinking_payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply exactly: OK"}],
        "temperature": 0.0,
        "max_tokens": 16,
        "chat_template_kwargs": {"enable_thinking": False},
    }

    def check_no_thinking(result: dict, duration: float) -> LLMProbeResult:
        message, finish_reason = first_chat_message(result)
        content = message.get("content")
        reasoning = message.get("reasoning") or message.get("reasoning_content")
        if content is None and reasoning:
            return LLMProbeResult(
                "no_thinking_content",
                "FAIL",
                f"reasoning-only response; finish={finish_reason or '(none)'} reasoning_chars={len(str(reasoning))}",
            )
        if str(content or "").strip() == "OK":
            return LLMProbeResult("no_thinking_content", "PASS", f"content OK in {duration:.2f}s")
        return LLMProbeResult(
            "no_thinking_content",
            "WARN",
            f"unexpected content={compact_preview(content)!r} finish={finish_reason or '(none)'}",
        )

    run_probe("no_thinking_content", no_thinking_payload, check_no_thinking)

    default_payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply exactly: OK"}],
        "temperature": 0.0,
        "max_tokens": 256,
        "chat_template_kwargs": None,
    }

    def check_default(result: dict, duration: float) -> LLMProbeResult:
        message, finish_reason = first_chat_message(result)
        content = message.get("content")
        reasoning = message.get("reasoning") or message.get("reasoning_content")
        if content is None and reasoning:
            return LLMProbeResult(
                "default_thinking_behavior",
                "WARN",
                "default request returned reasoning without content; keep enable_thinking=false for artifact calls "
                f"or increase max_tokens. finish={finish_reason or '(none)'} reasoning_chars={len(str(reasoning))}",
            )
        if content:
            return LLMProbeResult(
                "default_thinking_behavior",
                "PASS",
                f"content={compact_preview(content)!r} in {duration:.2f}s finish={finish_reason or '(none)'}",
            )
        return LLMProbeResult("default_thinking_behavior", "FAIL", f"empty response finish={finish_reason or '(none)'}")

    run_probe("default_thinking_behavior", default_payload, check_default)

    json_payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": 'Return only this compact JSON object: {"ok":true,"files":["a.py"]}',
            }
        ],
        "temperature": 0.0,
        "max_tokens": 64,
        "chat_template_kwargs": {"enable_thinking": False},
    }

    def check_json(result: dict, duration: float) -> LLMProbeResult:
        message, finish_reason = first_chat_message(result)
        content = message.get("content")
        if content is None:
            reasoning = message.get("reasoning") or message.get("reasoning_content")
            return LLMProbeResult(
                "json_artifact_content",
                "FAIL",
                f"no content; finish={finish_reason or '(none)'} reasoning_chars={len(str(reasoning or ''))}",
            )
        try:
            parsed = parse_json_probe_content(str(content))
        except Exception as exc:
            return LLMProbeResult(
                "json_artifact_content",
                "FAIL",
                f"JSON parse failed: {exc}; content={compact_preview(content)!r}",
            )
        if isinstance(parsed, dict) and parsed.get("ok") is True and parsed.get("files") == ["a.py"]:
            return LLMProbeResult("json_artifact_content", "PASS", f"valid JSON in {duration:.2f}s")
        return LLMProbeResult("json_artifact_content", "WARN", f"JSON shape differed: {compact_preview(parsed)!r}")

    run_probe("json_artifact_content", json_payload, check_json)
    return probes

def llm_role_recommendations(model: str) -> list[str]:
    model_key = model.lower()
    recommendations = [
        "artifact calls: keep chat_template_kwargs.enable_thinking=false so patch/JSON/BEGIN_FILE output stays machine-parseable",
        "analysis calls: enable thinking only when the server separates reasoning_content from message.content",
        "judge calls: use temperature=0 and evidence-only prompts",
        "coder calls: prefer larger max_tokens than PM/judge when generating patches or file artifacts",
    ]
    if "deepseek" in model_key:
        recommendations.insert(
            0,
            "DeepSeek on the verified local llama.cpp endpoint: keep chat_template_kwargs.enable_thinking=false for strict artifacts; opt in to thinking only for analysis calls that return separate reasoning_content and content",
        )
        recommendations.insert(
            1,
            "size max_tokens for the server's active context window, not the model's training-context claim",
        )
    elif "nemotron" in model_key:
        recommendations.insert(
            0,
            "Nemotron reasoning models on vLLM: use --reasoning-parser nemotron_v3 and send chat_template_kwargs.enable_thinking=false for normal content",
        )
        recommendations.insert(
            1,
            "on DGX Spark / GB10, --enforce-eager may be required to avoid TorchInductor startup failures",
        )
    elif "ornith" in model_key or "qwen" in model_key:
        recommendations.insert(
            0,
            "Ornith/Qwen reasoning models on vLLM: serve with --reasoning-parser qwen3; add --enable-auto-tool-choice --tool-call-parser qwen3_xml when using tool calls",
        )
        recommendations.insert(
            1,
            "for strict file/JSON artifacts, disable thinking per request with chat_template_kwargs.enable_thinking=false",
        )
    return recommendations

def llm_reasoning_manifest(client: LocalLLMClient) -> list[dict[str, object]]:
    return list(getattr(client, "reasoning_records", []))


def llm_completion_recovery_manifest(client: LocalLLMClient) -> list[dict[str, object]]:
    return list(getattr(client, "completion_recovery_records", []))


def llm_generation_request_count(client: LocalLLMClient) -> int:
    return int(getattr(client, "generation_request_count", 0) or 0)


def llm_settings_manifest(client: LocalLLMClient) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    if not hasattr(client, "call_settings"):
        return result
    for role in ("pm", "coder", "judge"):
        settings = client.call_settings(role)
        result[role] = {
            "model": settings.model,
            "temperature": settings.temperature,
            "max_tokens": settings.max_tokens,
            "thinking": "off" if settings.disable_thinking else "on",
            "reasoning_effort": settings.reasoning_effort,
        }
    for function_name in sorted(DEFAULT_FUNCTION_PROFILES):
        settings = client.call_settings("default", function_name)
        result[f"function.{function_name}"] = {
            "role": settings.agent_level,
            "model": settings.model,
            "temperature": settings.temperature,
            "max_tokens": settings.max_tokens,
            "thinking": "off" if settings.disable_thinking else "on",
            "reasoning_effort": settings.reasoning_effort,
        }
    return result

def llm_model_profile_manifest(args: argparse.Namespace) -> dict[str, object]:
    profile = effective_model_profile(args)
    return {
        "profile": profile,
        "config_file": str(_app_config_for_args(args).path or ""),
        "default_model": MODEL_PROFILE_DEFAULT_MODELS.get(profile, ""),
        "runtime_model_requirement": (
            "listed_by_v1_models" if profile != "default" else "automatic"
        ),
        "function_overrides": {
            name: {
                "model": override.model,
                "temperature": override.temperature,
                "max_tokens": override.max_tokens,
                "thinking": (
                    "default"
                    if override.disable_thinking is None
                    else ("off" if override.disable_thinking else "on")
                ),
                "reasoning_effort": override.reasoning_effort,
            }
            for name, override in sorted(profile_function_overrides(profile).items())
        },
    }
