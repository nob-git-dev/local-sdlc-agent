"""Local subprocess job management for the browser UI."""

from __future__ import annotations

import dataclasses
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .models import DEFAULT_BASE_URL, DEFAULT_MODEL, RunnerError


MAX_JOB_LOG_LINES = 4000


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


@dataclasses.dataclass(frozen=True)
class WebConfig:
    host: str
    port: int
    project: Path
    entrypoint: Path
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    model_profile: str = "default"
    open_browser: bool = False


@dataclasses.dataclass(frozen=True)
class BuiltCommand:
    argv: tuple[str, ...]
    cwd: Path
    display: str


def _append_common_args(command: list[str], payload: dict[str, object], config: WebConfig, project: Path) -> None:
    command.extend(["--project", str(project)])
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


def build_cli_command(payload: dict[str, object], config: WebConfig) -> BuiltCommand:
    """Build a safe argv list for a local agent subprocess."""
    mode = _optional_text(payload, "mode") or "agent"
    if mode not in {"agent", "run-stages", "spec", "doctor", "health"}:
        raise RunnerError(f"unsupported web mode: {mode}")

    project_text = _optional_text(payload, "project")
    project = Path(project_text).expanduser().resolve() if project_text else config.project
    command = [sys.executable, str(config.entrypoint), mode]
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
    elif mode == "run-stages":
        command.append(brief)
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
        spec_file = _optional_text(payload, "spec_file")
        if spec_file:
            command.extend(["--spec-file", spec_file])
        for include in _string_list(payload.get("include")):
            command.extend(["--include", include])
        for new_file in _string_list(payload.get("new_file")):
            command.extend(["--new-file", new_file])
        for require_path in _string_list(payload.get("require_path")):
            command.extend(["--require-path", require_path])
        for test_command in _string_list(payload.get("test_command")):
            command.extend(["--test-command", test_command])
        if _as_bool(payload.get("apply"), True):
            command.append("--apply")
        if _as_bool(payload.get("allow_no_context"), False):
            command.append("--allow-no-context")
        if _as_bool(payload.get("worktree_copy"), False):
            command.extend(["--worktree-mode", "copy"])

    return BuiltCommand(tuple(command), project, shlex.join(command))


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
            return payload


class JobRegistry:
    def __init__(self, config: WebConfig):
        self.config = config
        self.jobs: dict[str, AgentJob] = {}
        self._lock = threading.Lock()
        self.jobs_root = config.project / ".sdlc-runner" / "web" / "jobs"
        self.jobs_root.mkdir(parents=True, exist_ok=True)

    def start(self, payload: dict[str, object]) -> AgentJob:
        built = build_cli_command(payload, self.config)
        mode = _optional_text(payload, "mode") or "agent"
        brief = _optional_text(payload, "brief")
        job_id = time.strftime("%Y%m%d-%H%M%S", time.localtime()) + "-" + uuid.uuid4().hex[:8]
        log_dir = self.jobs_root / job_id
        log_dir.mkdir(parents=True, exist_ok=True)
        job = AgentJob(job_id, built, brief, mode, log_dir)
        job.write_metadata()
        with self._lock:
            self.jobs[job_id] = job
        thread = threading.Thread(target=self._run_job, args=(job,), daemon=True)
        thread.start()
        return job

    def _run_job(self, job: AgentJob) -> None:
        job.mark("running")
        try:
            process = subprocess.Popen(
                list(job.command.argv),
                cwd=job.command.cwd,
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
            job.append_output(f"web runner error: {exc}\n")
            job.mark("failed", 1)

    def list_jobs(self) -> list[dict[str, object]]:
        with self._lock:
            jobs = list(self.jobs.values())
        return [job.to_dict(tail=20) for job in sorted(jobs, key=lambda item: item.started_at, reverse=True)]

    def get(self, job_id: str) -> AgentJob | None:
        with self._lock:
            return self.jobs.get(job_id)

    def stop(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None or job.process is None or job.process.poll() is not None:
            return False
        job.mark("stopped")
        if os.name == "posix":
            os.killpg(os.getpgid(job.process.pid), signal.SIGTERM)
        else:  # pragma: no cover - Windows fallback
            job.process.terminate()
        return True
