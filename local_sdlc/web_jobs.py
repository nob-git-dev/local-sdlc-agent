"""Local subprocess job management for the browser UI."""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from urllib.parse import quote

from .control import request_cancel
from .action_gate import begin_action
from .safety import pending_safety_decisions, read_safety_decisions, request_safety_approval
from .models import DEFAULT_BASE_URL, DEFAULT_MODEL, GENERATED_DIR, RunnerError


MAX_JOB_LOG_LINES = 4000
PREVIEWABLE_SUFFIXES = {".html", ".htm", ".css", ".js", ".json", ".md", ".txt", ".py"}
RUN_DIR_PATTERN = re.compile(r"^run_dir:\s*(?P<path>.+?)\s*$")


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _as_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        raw_items = [str(item) for item in value]
    else:
        raw_items = str(value).replace(",", "\n").splitlines()
    return [item.strip() for item in raw_items if item.strip()]


def _optional_text(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    return str(value).strip() if value is not None else ""


def _repo_entrypoint() -> Path:
    return Path(__file__).resolve().parents[1] / "local_sdlc.py"


def _tail_file(path: Path, max_lines: int) -> list[str]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as file:
            return list(deque(file, maxlen=max_lines))
    except OSError:
        return []


def _find_run_dir(log_path: Path, output_lines: list[str]) -> Path | None:
    candidates = output_lines
    if log_path.exists():
        try:
            candidates = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            candidates = output_lines
    for line in candidates:
        match = RUN_DIR_PATTERN.match(line.strip())
        if match:
            return Path(match.group("path")).expanduser().resolve()
    return None


def _read_json_file(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _values_after_flag(command_display: str, flag: str) -> list[str]:
    try:
        argv = shlex.split(command_display)
    except ValueError:
        return []
    values: list[str] = []
    for index, value in enumerate(argv[:-1]):
        if value == flag:
            values.append(argv[index + 1])
    return values


def looks_like_creation_request(brief: str) -> bool:
    text = brief.strip().lower()
    if not text:
        return False
    creation_terms = ("作って", "作成", "作る", "実装", "生成", "build", "create", "make")
    consult_terms = ("分析", "原因", "相談", "教えて", "なぜ", "どう", "確認")
    return any(term in text for term in creation_terms) and not any(term in text for term in consult_terms)


def suggest_new_files_from_brief(brief: str) -> list[str]:
    text = brief.strip().lower()
    if any(term in text for term in ("テトリス", "tetris")):
        return ["tetris.html"]
    if any(term in text for term in ("html", "ブラウザ", "web", "ウェブ", "ゲーム", "game")):
        return ["index.html"]
    return []


def resolve_project_path(project: Path, raw_path: str) -> tuple[Path, str]:
    if not raw_path:
        raise RunnerError("file path is required")
    path = Path(raw_path).expanduser()
    candidate = path.resolve() if path.is_absolute() else (project / path).resolve()
    try:
        relative = candidate.relative_to(project.resolve())
    except ValueError as exc:
        raise RunnerError("file path must stay inside the project") from exc
    return candidate, relative.as_posix()


def ensure_project_directory(project: Path, allowed_new_root: Path) -> bool:
    """Ensure a web-selected project directory exists.

    Existing directories are accepted anywhere the local process can read.
    New directories are auto-created only inside the web workspace root to
    avoid turning a typo into an arbitrary filesystem mkdir.
    """
    resolved_project = project.expanduser().resolve()
    if resolved_project.exists():
        if not resolved_project.is_dir():
            raise RunnerError(f"project path exists but is not a directory: {resolved_project}")
        return False
    root = allowed_new_root.expanduser().resolve()
    try:
        resolved_project.relative_to(root)
    except ValueError as exc:
        raise RunnerError(f"new project directory must be inside web workspace root: {root}") from exc
    resolved_project.mkdir(parents=True, exist_ok=True)
    return True


def _spec_line(text: object, fallback: str = "(not specified)") -> str:
    line = str(text or "").strip().replace("\r", " ").replace("\n", " ")
    return line or fallback


def _spec_bullets(values: list[str], fallback: str) -> str:
    items = [_spec_line(value) for value in values if _spec_line(value)]
    if not items:
        return f"- {fallback}"
    return "\n".join(f"- `{item}`" for item in items)


def build_web_bootstrap_spec(payload: dict[str, object]) -> str:
    """Create a minimal SPEC.md for first-run browser jobs.

    The CLI keeps enforcing SPEC-first execution. The browser adapter uses this
    deterministic bootstrap only when a user starts a coding job in a project
    that has no SPEC.md yet.
    """
    brief = _spec_line(payload.get("brief"), "Web UI request")
    new_files = _string_list(payload.get("new_file"))
    includes = _string_list(payload.get("include"))
    required_paths = _string_list(payload.get("require_path")) or new_files
    test_commands = _string_list(payload.get("test_command"))
    return (
        "# SPEC.md\n\n"
        "## 目的\n"
        f"{brief}\n\n"
        "## 固定要件\n"
        "- This file was generated by the local Web UI as a minimal SPEC bootstrap.\n"
        "- Preserve the user's request as the primary product requirement.\n"
        "- Do not use network services or heavy build tooling unless the user explicitly asks for them.\n\n"
        "## 対象ファイル\n"
        "### 作成または更新するファイル\n"
        f"{_spec_bullets(new_files, 'Infer the smallest necessary writable file set from the request.')}\n\n"
        "### 参照する既存ファイル\n"
        f"{_spec_bullets(includes, 'No existing context file was provided.')}\n\n"
        "## 受け入れ条件\n"
        "- The implementation satisfies the request in `## 目的`.\n"
        f"{_spec_bullets([f'{path} exists and is non-empty' for path in required_paths], 'The requested artifact exists and is non-empty.')}\n\n"
        "## 検証方法\n"
        f"{_spec_bullets(test_commands, 'Use built-in artifact and smoke checks when applicable.')}\n"
    )


def ensure_web_bootstrap_spec(project: Path, payload: dict[str, object], mode: str) -> bool:
    if mode != "agent":
        return False
    if _optional_text(payload, "spec_file"):
        return False
    spec_path = project / "SPEC.md"
    if spec_path.exists():
        return False
    spec_path.write_text(build_web_bootstrap_spec(payload), encoding="utf-8")
    return True


def normalize_web_job_payload(payload: dict[str, object], project: Path, job_id: str) -> dict[str, object]:
    """Normalize browser input before mapping it to the strict CLI contract."""
    normalized = dict(payload)
    mode = _optional_text(normalized, "mode") or "agent"
    brief = _optional_text(normalized, "brief")
    if mode in {"agent", "run-stages", "supervisor"} and not _optional_text(normalized, "run_dir"):
        normalized["run_dir"] = str(project / GENERATED_DIR / "runs" / job_id)
    if mode == "agent":
        includes = _string_list(normalized.get("include"))
        new_files = _string_list(normalized.get("new_file"))
        allow_no_context = _as_bool(normalized.get("allow_no_context"), False)
        if not includes and not new_files and not allow_no_context and looks_like_creation_request(brief):
            suggested = suggest_new_files_from_brief(brief)
            if suggested:
                normalized["new_file"] = suggested
                if not _string_list(normalized.get("require_path")):
                    normalized["require_path"] = suggested
    return normalized


def _artifact_entry(project: Path, raw_path: str, source: str) -> dict[str, object] | None:
    try:
        path, relative = resolve_project_path(project, raw_path)
    except RunnerError:
        return None
    suffix = path.suffix.lower()
    exists = path.exists() and path.is_file()
    url_path = quote(relative, safe="/")
    entry: dict[str, object] = {
        "path": relative,
        "source": source,
        "exists": exists,
        "suffix": suffix,
        "previewable": exists and suffix in PREVIEWABLE_SUFFIXES,
        "preview_url": f"/files?path={url_path}",
    }
    if exists:
        try:
            entry["size"] = path.stat().st_size
        except OSError:
            entry["size"] = None
    return entry


def _append_artifact(
    artifacts: list[dict[str, object]],
    seen: set[str],
    project: Path,
    raw_path: str,
    source: str,
) -> None:
    entry = _artifact_entry(project, raw_path, source)
    if entry is None:
        return
    key = str(entry["path"])
    if key in seen:
        return
    artifacts.append(entry)
    seen.add(key)


def _infer_run_dir_from_job_id(project: Path, log_dir: Path) -> Path | None:
    job_id = log_dir.name
    if len(job_id) < 15:
        return None
    exact = project / GENERATED_DIR / "runs" / job_id
    if exact.exists() and exact.is_dir():
        return exact.resolve()
    candidate = project / GENERATED_DIR / "runs" / job_id[:15]
    return candidate.resolve() if candidate.exists() and candidate.is_dir() else None


def _manifest_for_run(run_dir: Path | None) -> tuple[dict[str, object], Path | None, bool]:
    if run_dir is None:
        return {}, None, False
    final_path = run_dir / "run.json"
    if final_path.exists():
        return _read_json_file(final_path), final_path, False
    partial_path = run_dir / "run.partial.json"
    if partial_path.exists():
        return _read_json_file(partial_path), partial_path, True
    return {}, None, False


def _progress_text(payload: dict[str, object], run_dir: Path | None, manifest: dict[str, object], partial: bool) -> str:
    status = str(payload.get("status") or "")
    streaming = manifest.get("streaming")
    if isinstance(streaming, dict):
        label = str(streaming.get("label") or "").strip()
        stream_status = str(streaming.get("status") or "").strip()
        if label or stream_status:
            suffix = f" ({stream_status})" if stream_status else ""
            if status == "running":
                return f"実行中: {label or 'LLM処理'}{suffix}"
            return f"最終処理: {label or 'LLM処理'}{suffix}"
    if status == "running" and partial:
        return "実行中: partial manifest を更新中"
    if status == "running" and run_dir is not None:
        return "実行中: run_dir 作成済み"
    return ""


def summarize_job_result(payload: dict[str, object], project: Path, log_dir: Path, output_lines: list[str]) -> dict[str, object]:
    """Summarize a subprocess job into UI actions and previewable artifacts."""
    output_log = log_dir / "output.log"
    run_dir = _find_run_dir(output_log, output_lines) or _infer_run_dir_from_job_id(project, log_dir)
    manifest, manifest_path, partial_manifest = _manifest_for_run(run_dir)
    artifacts: list[dict[str, object]] = []
    seen: set[str] = set()

    changed_paths = manifest.get("changed_paths")
    if isinstance(changed_paths, list):
        for raw_path in changed_paths:
            _append_artifact(artifacts, seen, project, str(raw_path), "changed")
    copied_back = manifest.get("copied_back")
    if isinstance(copied_back, list):
        for raw_path in copied_back:
            _append_artifact(artifacts, seen, project, str(raw_path), "copied_back")
    for raw_path in _values_after_flag(str(payload.get("command", "")), "--new-file"):
        _append_artifact(artifacts, seen, project, raw_path, "requested_new_file")
    for raw_path in _values_after_flag(str(payload.get("command", "")), "--require-path"):
        _append_artifact(artifacts, seen, project, raw_path, "required_path")

    final_verdict = str(manifest.get("final_verdict") or "")
    failure_type = str(manifest.get("final_failure_type") or "")
    next_actions: list[dict[str, object]] = []
    existing_paths = [str(item["path"]) for item in artifacts if item.get("exists")]
    if existing_paths:
        next_actions.append(
            {
                "type": "continue_with_files",
                "label": "このファイルを対象に続ける",
                "paths": existing_paths,
                "brief": "Webで確認した結果をもとに、このファイルを修正して",
            }
        )
    brief = str(payload.get("brief") or "")
    if str(payload.get("mode") or "") == "supervisor" and final_verdict == "planned":
        next_actions.append(
            {
                "type": "execute_supervisor_plan",
                "label": "この計画を実行する",
                "brief": brief,
            }
        )
        if looks_like_creation_request(brief):
            next_actions.append(
                {
                    "type": "create_from_prompt",
                    "label": "新規作成として直接作る",
                    "brief": brief,
                    "new_file": suggest_new_files_from_brief(brief),
                }
            )
    if failure_type == "missing_context":
        next_actions.append(
            {
                "type": "create_spec",
                "label": "仕様書を作って続ける",
                "brief": f"{payload.get('brief') or 'この依頼'}について、目的、固定要件、受け入れ条件、検証方法をSPEC.mdに整理して",
            }
        )
    if payload.get("status") == "failed":
        next_actions.append(
            {
                "type": "analyze_failure",
                "label": "失敗理由を分析する",
                "brief": "このジョブの失敗理由を分析し、次に入力すべき内容を具体的に示して",
                "include": [str(output_log)] if output_log.exists() else [],
            }
        )

    streaming = manifest.get("streaming") if isinstance(manifest.get("streaming"), dict) else {}
    pending_approvals: list[dict[str, object]] = []
    if run_dir is not None:
        pending_approvals.extend(
            {**item, "run_dir": str(run_dir)}
            for item in pending_safety_decisions(run_dir)
        )
    manifest_pending = manifest.get("pending_safety_decisions")
    if isinstance(manifest_pending, list):
        for item in manifest_pending:
            if not isinstance(item, dict):
                continue
            candidate = dict(item)
            candidate.setdefault("run_dir", str(run_dir) if run_dir is not None else "")
            key = (str(candidate.get("run_dir") or ""), str(candidate.get("decision_id") or ""))
            existing_keys = {
                (str(existing.get("run_dir") or ""), str(existing.get("decision_id") or ""))
                for existing in pending_approvals
            }
            if key not in existing_keys:
                pending_approvals.append(candidate)
    safety_decisions = read_safety_decisions(run_dir) if run_dir is not None else []
    blocked_decisions = [item for item in safety_decisions if item.get("decision") == "block"]
    manifest_blocked = manifest.get("blocked_safety_decisions")
    if isinstance(manifest_blocked, list):
        for item in manifest_blocked:
            if not isinstance(item, dict):
                continue
            candidate = dict(item)
            candidate.setdefault("run_dir", str(run_dir) if run_dir is not None else "")
            key = (str(candidate.get("run_dir") or ""), str(candidate.get("decision_id") or ""))
            existing_keys = {
                (str(existing.get("run_dir") or ""), str(existing.get("decision_id") or ""))
                for existing in blocked_decisions
            }
            if key not in existing_keys:
                blocked_decisions.append(candidate)
    safety_state = ""
    if blocked_decisions:
        safety_state = "SAFETY_BLOCKED"
    elif pending_approvals:
        safety_state = "APPROVAL_REQUIRED"
    return {
        "run_dir": str(run_dir) if run_dir is not None else "",
        "run_manifest": str(manifest_path) if manifest_path is not None else "",
        "partial_manifest": partial_manifest,
        "final_verdict": final_verdict,
        "failure_type": failure_type,
        "progress": _progress_text(payload, run_dir, manifest, partial_manifest),
        "current_round": manifest.get("current_round"),
        "completed_rounds": manifest.get("completed_rounds"),
        "api_calls": manifest.get("api_calls"),
        "streaming_label": streaming.get("label", "") if isinstance(streaming, dict) else "",
        "safety_state": safety_state,
        "pending_safety_decisions": pending_approvals,
        "blocked_safety_decisions": blocked_decisions[-5:],
        "artifacts": artifacts,
        "next_actions": next_actions,
        "output_log": str(output_log),
    }


@dataclasses.dataclass(frozen=True)
class WebConfig:
    host: str
    port: int
    project: Path
    entrypoint: Path
    config_file: Path | None = None
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    model_profile: str = "default"
    api_key_configured: bool = False
    open_browser: bool = False


@dataclasses.dataclass(frozen=True)
class BuiltCommand:
    argv: tuple[str, ...]
    cwd: Path
    display: str
    env_overrides: dict[str, str] = dataclasses.field(default_factory=dict)


def _append_common_args(command: list[str], payload: dict[str, object], config: WebConfig, project: Path) -> None:
    command.extend(["--project", str(project)])
    skills_dir = config.entrypoint.parent / "sdlc-skills" / "skills"
    if skills_dir.exists():
        command.extend(["--skills-dir", str(skills_dir)])
    if config.config_file is not None:
        command.extend(["--config-file", str(config.config_file)])
    base_url = _optional_text(payload, "base_url") or config.base_url
    if base_url:
        command.extend(["--base-url", base_url])
    model = _optional_text(payload, "model") or config.model
    if model:
        command.extend(["--model", model])
    model_profile = _optional_text(payload, "model_profile") or config.model_profile
    if model_profile:
        command.extend(["--model-profile", model_profile])
    if _as_bool(payload.get("stream"), False):
        command.append("--stream")


def _append_run_dir_arg(command: list[str], payload: dict[str, object], project: Path) -> None:
    run_dir_text = _optional_text(payload, "run_dir")
    if not run_dir_text:
        return
    run_dir, _relative = resolve_project_path(project, run_dir_text)
    command.extend(["--run-dir", str(run_dir)])


def build_cli_command(payload: dict[str, object], config: WebConfig) -> BuiltCommand:
    """Build a safe argv list for a local agent subprocess."""
    mode = _optional_text(payload, "mode") or "agent"
    if mode not in {"agent", "run-stages", "spec", "supervisor", "doctor", "health"}:
        raise RunnerError(f"unsupported web mode: {mode}")

    project_text = _optional_text(payload, "project")
    project = Path(project_text).expanduser().resolve() if project_text else config.project
    command = [sys.executable, str(config.entrypoint), mode]
    env_overrides: dict[str, str] = {}
    api_key = _optional_text(payload, "api_key")
    if api_key:
        env_overrides["LOCAL_SDLC_API_KEY"] = api_key
    _append_common_args(command, payload, config, project)

    brief = _optional_text(payload, "brief")
    if mode in {"agent", "run-stages", "spec"} and not brief:
        raise RunnerError("brief is required")

    if mode == "doctor":
        if _as_bool(payload.get("skip_llm"), False):
            command.append("--skip-llm")
        if _as_bool(payload.get("skip_probes"), True):
            command.append("--skip-probes")
    elif mode == "health":
        pass
    elif mode == "spec":
        command.append(brief)
        if _as_bool(payload.get("apply"), False):
            command.append("--apply")
    elif mode == "supervisor":
        command.append(brief)
        _append_run_dir_arg(command, payload, project)
        for include in _string_list(payload.get("include")):
            command.extend(["--include", include])
        if _as_bool(payload.get("execute"), False):
            command.append("--execute")
        if _as_bool(payload.get("allow_ambiguous"), False):
            command.append("--allow-ambiguous")
    elif mode == "run-stages":
        command.append(brief)
        _append_run_dir_arg(command, payload, project)
        spec_file = _optional_text(payload, "spec_file")
        if spec_file:
            command.extend(["--spec-file", spec_file])
        if _as_bool(payload.get("apply"), True):
            command.append("--apply")
        if _as_bool(payload.get("allow_no_context"), False):
            command.append("--allow-no-context")
        if _as_bool(payload.get("worktree_copy"), False):
            command.extend(["--worktree-mode", "copy"])
        for test_command in _string_list(payload.get("test_command")):
            command.extend(["--test-command", test_command])
    else:
        command.append(brief)
        _append_run_dir_arg(command, payload, project)
        spec_file = _optional_text(payload, "spec_file")
        if spec_file:
            command.extend(["--spec-file", spec_file])
        includes = _string_list(payload.get("include"))
        new_files = _string_list(payload.get("new_file"))
        allow_no_context = _as_bool(payload.get("allow_no_context"), False)
        if not includes and not new_files and not allow_no_context:
            raise RunnerError(
                "agent mode needs at least one target: add an existing file to Include, "
                "add a file to Create, or enable No-context execution. "
                "For questions or error analysis, choose Consult / Analyze instead."
            )
        for include in includes:
            command.extend(["--include", include])
        for new_file in new_files:
            command.extend(["--new-file", new_file])
        for require_path in _string_list(payload.get("require_path")):
            command.extend(["--require-path", require_path])
        for test_command in _string_list(payload.get("test_command")):
            command.extend(["--test-command", test_command])
        if _as_bool(payload.get("apply"), True):
            command.append("--apply")
        if allow_no_context:
            command.append("--allow-no-context")
        if _as_bool(payload.get("worktree_copy"), False):
            command.extend(["--worktree-mode", "copy"])

    return BuiltCommand(tuple(command), project, shlex.join(command), env_overrides)


class AgentJob:
    def __init__(self, job_id: str, command: BuiltCommand, brief: str, mode: str, log_dir: Path):
        self.id = job_id
        self.command = command
        self.brief = brief
        self.mode = mode
        self.log_dir = log_dir
        self.status = "queued"
        self.returncode: int | None = None
        self.started_at = _utc_timestamp()
        self.ended_at: str | None = None
        self.output_lines: list[str] = []
        self.process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()

    def append_output(self, line: str) -> None:
        with self._lock:
            self.output_lines.append(line)
            if len(self.output_lines) > MAX_JOB_LOG_LINES:
                self.output_lines = self.output_lines[-MAX_JOB_LOG_LINES:]
        with (self.log_dir / "output.log").open("a", encoding="utf-8") as file:
            file.write(line)

    def mark(self, status: str, returncode: int | None = None) -> None:
        with self._lock:
            self.status = status
            self.returncode = returncode
            if status in {"completed", "failed", "stopped"}:
                self.ended_at = _utc_timestamp()
        self.write_metadata()

    def write_metadata(self) -> None:
        payload = self.to_dict(tail=None, include_output=False)
        (self.log_dir / "job.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def to_dict(self, tail: int | None = 300, include_output: bool = True) -> dict[str, object]:
        with self._lock:
            lines = list(self.output_lines)
            if tail is not None:
                lines = lines[-tail:]
            payload: dict[str, object] = {
                "id": self.id,
                "mode": self.mode,
                "brief": self.brief,
                "status": self.status,
                "returncode": self.returncode,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "cwd": str(self.command.cwd),
                "command": self.command.display,
                "log_dir": str(self.log_dir),
            }
            if include_output:
                payload["output"] = "".join(lines)
            payload["result"] = summarize_job_result(payload, self.command.cwd, self.log_dir, lines)
            return payload


class PersistedJob:
    def __init__(self, metadata: dict[str, object], log_dir: Path):
        self.metadata = dict(metadata)
        self.log_dir = log_dir

    def to_dict(self, tail: int | None = 300, include_output: bool = True) -> dict[str, object]:
        payload = dict(self.metadata)
        payload.setdefault("log_dir", str(self.log_dir))
        lines = _tail_file(self.log_dir / "output.log", tail or MAX_JOB_LOG_LINES)
        if include_output:
            payload["output"] = "".join(lines)
        cwd = Path(str(payload.get("cwd") or self.log_dir.parent)).expanduser().resolve()
        payload["result"] = summarize_job_result(payload, cwd, self.log_dir, lines)
        return payload


class JobRegistry:
    def __init__(self, config: WebConfig):
        self.config = config
        self.jobs: dict[str, AgentJob] = {}
        self._lock = threading.Lock()
        self.jobs_root = config.project / ".sdlc-runner" / "web" / "jobs"
        self.jobs_root.mkdir(parents=True, exist_ok=True)

    def start(self, payload: dict[str, object]) -> AgentJob:
        mode = _optional_text(payload, "mode") or "agent"
        brief = _optional_text(payload, "brief")
        job_id = time.strftime("%Y%m%d-%H%M%S", time.localtime()) + "-" + uuid.uuid4().hex[:8]
        project_text = _optional_text(payload, "project")
        project = Path(project_text).expanduser().resolve() if project_text else self.config.project
        command_payload = normalize_web_job_payload(payload, project, job_id)
        built = build_cli_command(command_payload, self.config)
        log_dir = self.jobs_root / job_id
        log_dir.mkdir(parents=True, exist_ok=True)
        job = AgentJob(job_id, built, brief, mode, log_dir)
        if not built.cwd.exists():
            begin_action(
                log_dir,
                "web_project_directory_create",
                action_type="project_create",
                risk_class="project_write",
            )
        created_project = ensure_project_directory(built.cwd, self.config.project.parent)
        spec_path = built.cwd / "SPEC.md"
        if not spec_path.exists() and mode in {"agent", "run-stages"}:
            begin_action(
                log_dir,
                "web_bootstrap_spec_write",
                action_type="document_write",
                risk_class="project_write",
            )
        created_spec = ensure_web_bootstrap_spec(built.cwd, command_payload, mode)
        if created_project:
            job.append_output(f"created_project_dir: {built.cwd}\n")
        if created_spec:
            job.append_output(f"created_spec_file: {built.cwd / 'SPEC.md'}\n")
        job.write_metadata()
        with self._lock:
            self.jobs[job_id] = job
        thread = threading.Thread(target=self._run_job, args=(job,), daemon=True)
        thread.start()
        return job

    def _run_job(self, job: AgentJob) -> None:
        try:
            run_dir = _infer_run_dir_from_job_id(job.command.cwd, job.log_dir)
            begin_action(
                job.log_dir,
                "web_job_process_start",
                action_type="process_start",
                risk_class="generated_code_execution",
                control_dirs=(run_dir,) if run_dir is not None else (),
            )
            job.mark("running")
            process = subprocess.Popen(
                list(job.command.argv),
                cwd=job.command.cwd,
                env={**os.environ, "PYTHONUNBUFFERED": "1", **job.command.env_overrides},
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=(os.name == "posix"),
            )
            job.process = process
            if process.stdout is not None:
                for line in process.stdout:
                    job.append_output(line)
            returncode = process.wait()
            if job.status == "stopped":
                job.mark("stopped", returncode)
            elif returncode == 0:
                job.mark("completed", returncode)
            else:
                job.mark("failed", returncode)
        except Exception as exc:  # pragma: no cover - defensive process boundary
            if job.status == "stopped":
                job.mark("stopped", job.returncode)
            else:
                job.append_output(f"web runner error: {exc}\n")
                job.mark("failed", 1)

    def list_jobs(self) -> list[dict[str, object]]:
        with self._lock:
            jobs = list(self.jobs.values())
            live_ids = set(self.jobs)
        payloads = [job.to_dict(tail=20) for job in jobs]
        for job_json in sorted(self.jobs_root.glob("*/job.json"), reverse=True):
            job_id = job_json.parent.name
            if job_id in live_ids:
                continue
            metadata = _read_json_file(job_json)
            if metadata:
                metadata.setdefault("id", job_id)
                payloads.append(PersistedJob(metadata, job_json.parent).to_dict(tail=20))
        return sorted(payloads, key=lambda item: str(item.get("started_at") or ""), reverse=True)

    def get(self, job_id: str) -> AgentJob | PersistedJob | None:
        with self._lock:
            live = self.jobs.get(job_id)
        if live is not None:
            return live
        metadata_path = self.jobs_root / job_id / "job.json"
        metadata = _read_json_file(metadata_path)
        if not metadata:
            return None
        metadata.setdefault("id", job_id)
        return PersistedJob(metadata, metadata_path.parent)

    def stop(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not isinstance(job, AgentJob) or job.status in {"completed", "failed", "stopped"}:
            return False
        metadata = {"job_id": job.id, "mode": job.mode}
        request_cancel(job.log_dir, source="web", reason="user_stop", metadata=metadata)
        run_dir = _find_run_dir(job.log_dir / "output.log", job.output_lines) or _infer_run_dir_from_job_id(
            job.command.cwd,
            job.log_dir,
        )
        if run_dir is not None:
            request_cancel(run_dir, source="web", reason="user_stop", metadata=metadata)
        job.mark("stopped")
        if job.process is not None and job.process.poll() is None:
            if os.name == "posix":
                os.killpg(os.getpgid(job.process.pid), signal.SIGTERM)
            else:  # pragma: no cover - Windows fallback
                job.process.terminate()
        return True

    def approve(
        self,
        job_id: str,
        decision_id: str,
        note: str = "",
        decision_run_dir: str = "",
    ) -> dict[str, object]:
        job = self.get(job_id)
        if job is None:
            raise RunnerError("job not found")
        result = job.to_dict().get("result")
        pending = result.get("pending_safety_decisions") if isinstance(result, dict) else None
        candidates = [
            item
            for item in (pending if isinstance(pending, list) else [])
            if isinstance(item, dict)
            and item.get("decision_id") == decision_id
            and (not decision_run_dir or item.get("run_dir") == decision_run_dir)
        ]
        if len(candidates) != 1:
            raise RunnerError("approval target must match exactly one pending safety decision")
        run_dir = Path(str(candidates[0].get("run_dir") or "")).expanduser().resolve()
        root_text = str(result.get("run_dir") or "") if isinstance(result, dict) else ""
        if not root_text:
            raise RunnerError("job run directory is unavailable for approval")
        job_run_dir = Path(root_text).expanduser().resolve()
        if run_dir != job_run_dir and job_run_dir not in run_dir.parents:
            raise RunnerError("approval target is outside the job run directory")
        return request_safety_approval(
            run_dir,
            decision_id,
            source="web",
            note=note,
        )
