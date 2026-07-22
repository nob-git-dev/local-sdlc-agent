"""OpenAI-compatible local LLM client and per-call API settings."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from .models import *
from .utils import compact_preview, strip_markdown_fence

class LocalLLMClient:
    def __init__(self, config: LLMConfig):
        self.config = config

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
        return LLMCallSettings(
            agent_level=normalized,
            call_function=normalized_function,
            model=model or "",
            temperature=temperature,
            max_tokens=max_tokens,
            disable_thinking=disable_thinking,
        )

    def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        timeout: float | None = None,
    ) -> dict:
        url = self.config.base_url.rstrip("/") + path
        request_timeout = self.config.timeout if timeout is None else timeout
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
            raise LLMTimeoutError(path, request_timeout) from exc
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RunnerError(f"LLM API HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
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
    ) -> tuple[str, LLMStreamStats]:
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
        request_timeout = self.config.timeout if timeout is None else timeout
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
        chunks_received = 0
        content_chunks = 0
        reasoning_chunks = 0
        bytes_received = 0
        start = time.monotonic()
        first_chunk_at: float | None = None
        last_chunk_at: float | None = None
        partial_file = None
        partial_path_text = str(partial_output_path) if partial_output_path else None
        try:
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
                        reasoning_chunks += 1
                    if progress_callback and (content_chunks == 1 or chunks_received % 20 == 0):
                        progress_callback(
                            LLMStreamStats(
                                chunks_received=chunks_received,
                                content_chunks=content_chunks,
                                reasoning_chunks=reasoning_chunks,
                                bytes_received=bytes_received,
                                first_chunk_at=first_chunk_at,
                                last_chunk_at=last_chunk_at,
                                duration_seconds=now - start,
                                partial_output_path=partial_path_text,
                            )
                        )
        except (TimeoutError, socket.timeout) as exc:
            raise LLMTimeoutError("/chat/completions", request_timeout) from exc
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RunnerError(f"LLM API HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise LLMTimeoutError("/chat/completions", request_timeout) from exc
            raise RunnerError(f"LLM API connection failed: {exc.reason}") from exc
        finally:
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
        content_text = "".join(output_parts).strip()
        if not content_text and reasoning_chunks:
            raise RunnerError(
                "LLM streaming API returned reasoning-only output with empty content; "
                "keep thinking disabled for artifact calls or use a model that emits delta.content"
            )
        if not content_text:
            raise RunnerError("LLM streaming API returned an empty message with no content")
        return content_text, stats

    def complete(
        self,
        messages: list[dict[str, str]],
        agent_level: str = "default",
        call_function: str = "default",
        stream_output_path: Path | None = None,
        stream_callback: Callable[[LLMStreamStats], None] | None = None,
        stream_guard: Callable[[str], ArtifactStreamGuardResult] | None = None,
    ) -> str:
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

        try:
            if self.config.stream:
                content, _stats = self.chat_completion_stream(
                    messages,
                    model=model,
                    temperature=settings.temperature,
                    max_tokens=settings.max_tokens,
                    chat_template_kwargs={"enable_thinking": False} if settings.disable_thinking else None,
                    partial_output_path=stream_output_path,
                    progress_callback=stream_callback,
                    stream_guard=stream_guard,
                )
                return content
            result = self.chat_completion_raw(
                messages,
                model=model,
                temperature=settings.temperature,
                max_tokens=settings.max_tokens,
                chat_template_kwargs={"enable_thinking": False} if settings.disable_thinking else None,
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
        content = (choice.get("message") or {}).get("content")
        if content is None:
            content = choice.get("text")
        if content is None:
            message = choice.get("message") or {}
            if message.get("reasoning"):
                raise RunnerError(
                    "LLM API returned reasoning-only output with empty content; "
                    "try the default no-thinking mode, a larger --max-tokens, or a model that emits message.content"
                )
            raise RunnerError("LLM API returned an empty message with no content")
        return str(content).strip()

def build_config(args: argparse.Namespace) -> LLMConfig:
    role_overrides = build_role_overrides(args)
    function_overrides = build_function_overrides(args)
    model_profile = normalize_model_profile(getattr(args, "model_profile", "default"))
    profile_model = MODEL_PROFILE_DEFAULT_MODELS.get(model_profile, "")
    return LLMConfig(
        base_url=args.base_url
        or os.environ.get("LOCAL_LLM_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or DEFAULT_BASE_URL,
        api_key=args.api_key
        or os.environ.get("LOCAL_LLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or DEFAULT_API_KEY,
        model=args.model or os.environ.get("LOCAL_LLM_MODEL") or profile_model or DEFAULT_MODEL,
        timeout=args.timeout,
        health_timeout=args.health_timeout,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        disable_thinking=not args.enable_thinking,
        stream=bool(args.stream),
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
    if value == "off":
        return True
    if value == "on":
        return False
    raise RunnerError(f"unknown thinking mode: {value}")

def build_role_overrides(args: argparse.Namespace) -> dict[str, LLMRoleOverride]:
    overrides: dict[str, LLMRoleOverride] = {}
    for role in ("pm", "coder", "judge"):
        max_tokens = getattr(args, f"{role}_max_tokens", None)
        temperature = getattr(args, f"{role}_temperature", None)
        thinking = parse_role_thinking(getattr(args, f"{role}_thinking", "default"))
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

def build_function_overrides(args: argparse.Namespace) -> dict[str, LLMRoleOverride]:
    overrides: dict[str, LLMRoleOverride] = profile_function_overrides(getattr(args, "model_profile", "default"))
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
        "max_tokens": 16,
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
        "agent artifact calls: keep chat_template_kwargs.enable_thinking=false unless the runner consumes reasoning_content",
        "judge calls: use temperature=0 and evidence-only prompts",
        "coder calls: prefer larger max_tokens than PM/judge when generating patches or file artifacts",
    ]
    if "nemotron" in model_key:
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
        }
    for function_name in sorted(DEFAULT_FUNCTION_PROFILES):
        settings = client.call_settings("default", function_name)
        result[f"function.{function_name}"] = {
            "role": settings.agent_level,
            "model": settings.model,
            "temperature": settings.temperature,
            "max_tokens": settings.max_tokens,
            "thinking": "off" if settings.disable_thinking else "on",
        }
    return result

def llm_model_profile_manifest(args: argparse.Namespace) -> dict[str, object]:
    profile = normalize_model_profile(getattr(args, "model_profile", "default"))
    return {
        "profile": profile,
        "default_model": MODEL_PROFILE_DEFAULT_MODELS.get(profile, ""),
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
            }
            for name, override in sorted(profile_function_overrides(profile).items())
        },
    }
