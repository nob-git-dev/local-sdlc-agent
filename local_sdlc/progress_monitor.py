"""Persistent, auditable no-progress detection for autonomous runs."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import math
from pathlib import Path
import time
from typing import Callable, Iterator, Mapping, Sequence

from .control import append_progress_event, read_progress_events
from .models import RunnerError


PROGRESS_POLICY_FILENAME = "progress_policy.json"
PROGRESS_STATE_FILENAME = "progress_state.json"
STALL_FILENAME = "stall.json"
PROGRESS_LOCK_FILENAME = ".progress.lock"
PROGRESS_SCHEMA_VERSION = 1
DEFAULT_MAX_IDLE_SECONDS = 900.0
VALID_SCOPE_KINDS = {"goal", "stage", "goal_stage"}

# Only observable workflow changes may refresh the no-progress clock. Volatile
# timestamps and status-file mtimes are intentionally excluded: recording the
# monitor itself must never count as progress.
MEANINGFUL_PROGRESS_FIELDS = {
    "lifecycle",
    "stage_id",
    "round",
    "current_function",
    "stream_bytes",
    "stream_chunks",
    "reasoning_chunks",
    "command_output_bytes",
    "documents_count",
    "evidence_count",
    "acceptance_pass_count",
    "failure_count",
    "changed_paths_hash",
    "evidence_hash",
    "required_paths_hash",
    "completed_actions",
}


@dataclass(frozen=True)
class ProgressPolicy:
    max_idle_seconds: float = DEFAULT_MAX_IDLE_SECONDS

    def validate(self) -> None:
        value = float(self.max_idle_seconds)
        if not math.isfinite(value) or value <= 0:
            raise RunnerError("max_idle_seconds must be greater than zero")


class ProgressStalled(RunnerError):
    """Raised when a persistent no-progress threshold has been reached."""

    def __init__(self, stall: Mapping[str, object]):
        self.stall = dict(stall)
        reason = str(stall.get("reason") or "run made no observable progress")
        run_dir = str(stall.get("scope_run_dir") or "unknown")
        super().__init__(f"STALLED: {reason} (run_dir={run_dir})")


def progress_timestamp(epoch: float | None = None) -> str:
    value = time.time() if epoch is None else float(epoch)
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))


def progress_policy_file_path(run_dir: Path) -> Path:
    return run_dir / PROGRESS_POLICY_FILENAME


def progress_state_file_path(run_dir: Path) -> Path:
    return run_dir / PROGRESS_STATE_FILENAME


def stall_file_path(run_dir: Path) -> Path:
    return run_dir / STALL_FILENAME


def _progress_lock_file_path(run_dir: Path) -> Path:
    return run_dir / PROGRESS_LOCK_FILENAME


def progress_policy_from_args(args: object) -> ProgressPolicy:
    return ProgressPolicy(
        max_idle_seconds=float(
            getattr(args, "max_idle_seconds", DEFAULT_MAX_IDLE_SECONDS)
        )
    )


def _read_json_object(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def read_progress_policy(run_dir: Path) -> dict[str, object]:
    return _read_json_object(progress_policy_file_path(run_dir))


def read_progress_state(run_dir: Path) -> dict[str, object]:
    return _read_json_object(progress_state_file_path(run_dir))


def read_stall_state(run_dir: Path) -> dict[str, object]:
    path = stall_file_path(run_dir)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "schema_version": PROGRESS_SCHEMA_VERSION,
            "status": "STALLED",
            "scope_run_dir": str(run_dir.resolve()),
            "reason": "invalid_stall_file",
            "path": str(path),
        }
    if not isinstance(payload, dict):
        return {
            "schema_version": PROGRESS_SCHEMA_VERSION,
            "status": "STALLED",
            "scope_run_dir": str(run_dir.resolve()),
            "reason": "invalid_stall_file",
            "path": str(path),
        }
    return payload


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


@contextmanager
def _locked_progress_scope(paths: Sequence[Path]) -> Iterator[list[Path]]:
    unique = {str(path.resolve()): path.resolve() for path in paths}
    scoped = [unique[key] for key in sorted(unique)]
    with ExitStack() as stack:
        for path in scoped:
            path.mkdir(parents=True, exist_ok=True)
            lock_file = stack.enter_context(
                _progress_lock_file_path(path).open("a+", encoding="utf-8")
            )
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            stack.callback(fcntl.flock, lock_file.fileno(), fcntl.LOCK_UN)
        yield scoped


def initialize_progress_monitor(
    run_dir: Path,
    policy: ProgressPolicy,
    *,
    scope_kind: str,
    now: float | None = None,
) -> dict[str, object]:
    policy.validate()
    if scope_kind not in VALID_SCOPE_KINDS:
        raise RunnerError(f"invalid progress scope kind: {scope_kind}")
    current_time = time.time() if now is None else float(now)
    with _locked_progress_scope((run_dir,)):
        existing = read_progress_policy(run_dir)
        if existing:
            if existing.get("scope_kind") != scope_kind:
                raise RunnerError(
                    "progress scope mismatch: "
                    f"existing={existing.get('scope_kind')} requested={scope_kind}"
                )
            limits = existing.get("policy")
            if not isinstance(limits, Mapping):
                raise RunnerError("existing progress policy is invalid")
            existing_idle = float(limits.get("max_idle_seconds", 0.0) or 0.0)
            if existing_idle != float(policy.max_idle_seconds):
                raise RunnerError(
                    "progress policy is immutable for an existing run: "
                    f"existing={existing_idle:g} requested={policy.max_idle_seconds:g}"
                )
            return existing
        document: dict[str, object] = {
            "schema_version": PROGRESS_SCHEMA_VERSION,
            "scope_kind": scope_kind,
            "initialized_at": progress_timestamp(current_time),
            "initialized_at_epoch": current_time,
            "policy": asdict(policy),
        }
        initial_vector = normalize_progress_vector({"lifecycle": "initialized"})
        state = {
            "schema_version": PROGRESS_SCHEMA_VERSION,
            "runtime_state": "RUNNING",
            "last_progress_at": progress_timestamp(current_time),
            "last_progress_at_epoch": current_time,
            "vector": initial_vector,
            "vector_hash": progress_vector_hash(initial_vector),
            "source": "initialize",
        }
        _atomic_write_json(progress_policy_file_path(run_dir), document)
        _atomic_write_json(progress_state_file_path(run_dir), state)
        return document


def normalize_progress_vector(vector: Mapping[str, object]) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for key in sorted(MEANINGFUL_PROGRESS_FIELDS):
        if key not in vector:
            continue
        value = vector[key]
        if value is None:
            continue
        if isinstance(value, bool):
            normalized[key] = value
        elif isinstance(value, (int, float, str)):
            normalized[key] = value
        else:
            normalized[key] = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if not normalized:
        raise RunnerError(
            "progress vector has no meaningful observable fields; accepted fields: "
            + ", ".join(sorted(MEANINGFUL_PROGRESS_FIELDS))
        )
    return normalized


def progress_vector_hash(vector: Mapping[str, object]) -> str:
    payload = json.dumps(dict(vector), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _progress_paths(run_dir: Path, control_dirs: Sequence[Path]) -> list[Path]:
    candidates = [run_dir.resolve(), *(path.resolve() for path in control_dirs)]
    return [path for path in candidates if progress_policy_file_path(path).exists()]


def _max_idle_seconds(run_dir: Path) -> float:
    policy = read_progress_policy(run_dir)
    raw = policy.get("policy")
    if not isinstance(raw, Mapping):
        raise RunnerError(f"progress policy has no policy object: {run_dir}")
    try:
        value = float(raw["max_idle_seconds"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RunnerError(f"progress policy has invalid max_idle_seconds: {run_dir}") from exc
    if not math.isfinite(value) or value <= 0:
        raise RunnerError(f"progress policy has non-positive max_idle_seconds: {run_dir}")
    return value


def _last_progress_epoch(run_dir: Path) -> float:
    state = read_progress_state(run_dir)
    policy = read_progress_policy(run_dir)
    fallback = policy.get("initialized_at_epoch", time.time())
    try:
        return float(state.get("last_progress_at_epoch", fallback))
    except (TypeError, ValueError):
        return float(fallback)


def _existing_stall(scoped: Sequence[Path]) -> dict[str, object]:
    for path in scoped:
        stall = read_stall_state(path)
        if stall:
            return stall
    return {}


def _persist_stall_unlocked(
    scoped: Sequence[Path],
    stall: Mapping[str, object],
) -> dict[str, object]:
    canonical = dict(stall)
    for path in scoped:
        if stall_file_path(path).exists():
            continue
        event = append_progress_event(
            path,
            "stalled",
            action=str(stall.get("action") or "progress_monitor"),
            starts_work=False,
            metadata={
                "runtime_state": "STALLED",
                "reason": stall.get("reason"),
                "idle_seconds": stall.get("idle_seconds"),
                "max_idle_seconds": stall.get("max_idle_seconds"),
                "scope_run_dir": stall.get("scope_run_dir"),
            },
        )
        local = {
            **canonical,
            "progress_sequence": event.get("sequence"),
        }
        _atomic_write_json(stall_file_path(path), local)
        state = read_progress_state(path)
        _atomic_write_json(
            progress_state_file_path(path),
            {
                **state,
                "runtime_state": "STALLED",
                "stalled_at": stall.get("stalled_at"),
                "stall_reason": stall.get("reason"),
            },
        )
        if path == Path(str(stall.get("scope_run_dir") or "")).resolve():
            canonical = local
    return canonical


def _evaluate_stall_unlocked(
    scoped: Sequence[Path],
    *,
    action: str,
    current_time: float,
) -> dict[str, object]:
    existing = _existing_stall(scoped)
    if existing:
        missing = [path for path in scoped if not stall_file_path(path).exists()]
        if missing:
            _persist_stall_unlocked(missing, existing)
        return existing
    violation: dict[str, object] | None = None
    for path in scoped:
        idle = max(0.0, current_time - _last_progress_epoch(path))
        threshold = _max_idle_seconds(path)
        if idle >= threshold:
            violation = {
                "scope": path,
                "idle": idle,
                "threshold": threshold,
            }
            break
    if violation is None:
        return {}
    scope = violation["scope"]
    state = read_progress_state(scope) if isinstance(scope, Path) else {}
    stall = {
        "schema_version": PROGRESS_SCHEMA_VERSION,
        "status": "STALLED",
        "stalled_at": progress_timestamp(current_time),
        "action": action,
        "idle_seconds": round(float(violation["idle"]), 6),
        "max_idle_seconds": float(violation["threshold"]),
        "scope_run_dir": str(scope),
        "last_progress_at": state.get("last_progress_at"),
        "last_progress_vector": state.get("vector", {}),
        "last_progress_vector_hash": state.get("vector_hash", ""),
        "reason": (
            f"progress vector was unchanged for {float(violation['idle']):.3f}s "
            f"(limit={float(violation['threshold']):g}s) before {action}"
        ),
    }
    return _persist_stall_unlocked(scoped, stall)


def ensure_not_stalled(
    run_dir: Path,
    action: str,
    control_dirs: Sequence[Path] = (),
    *,
    now: float | None = None,
) -> None:
    paths = _progress_paths(run_dir, control_dirs)
    if not paths:
        return
    current_time = time.time() if now is None else float(now)
    with _locked_progress_scope(paths) as scoped:
        stall = _evaluate_stall_unlocked(
            scoped,
            action=action,
            current_time=current_time,
        )
        if stall:
            raise ProgressStalled(stall)


def _write_observation_unlocked(
    scope: Path,
    vector: Mapping[str, object],
    *,
    source: str,
    source_run_dir: Path,
    current_time: float,
) -> tuple[dict[str, object], bool]:
    normalized = normalize_progress_vector(vector)
    vector_hash = progress_vector_hash(normalized)
    prior = read_progress_state(scope)
    changed = vector_hash != str(prior.get("vector_hash") or "")
    last_epoch = current_time if changed else _last_progress_epoch(scope)
    state = {
        "schema_version": PROGRESS_SCHEMA_VERSION,
        "runtime_state": "PROGRESSING" if changed else "RUNNING",
        "last_progress_at": progress_timestamp(last_epoch),
        "last_progress_at_epoch": last_epoch,
        "observed_at": progress_timestamp(current_time),
        "observed_at_epoch": current_time,
        "vector": normalized,
        "vector_hash": vector_hash,
        "source": source,
        "source_run_dir": str(source_run_dir.resolve()),
    }
    _atomic_write_json(progress_state_file_path(scope), state)
    if changed:
        append_progress_event(
            scope,
            "progress_observed",
            action=source,
            starts_work=False,
            metadata={
                "vector": normalized,
                "vector_hash": vector_hash,
                "source_run_dir": str(source_run_dir.resolve()),
            },
        )
    return state, changed


def observe_progress(
    run_dir: Path,
    vector: Mapping[str, object],
    *,
    source: str,
    control_dirs: Sequence[Path] = (),
    now: float | None = None,
) -> dict[str, object]:
    paths = _progress_paths(run_dir, control_dirs)
    if not paths:
        return {"state": "not_configured", "changed": False}
    current_time = time.time() if now is None else float(now)
    with _locked_progress_scope(paths) as scoped:
        stall = _evaluate_stall_unlocked(
            scoped,
            action=source,
            current_time=current_time,
        )
        if stall:
            raise ProgressStalled(stall)
        local_state: dict[str, object] = {}
        local_changed = False
        for scope in scoped:
            state, changed = _write_observation_unlocked(
                scope,
                vector,
                source=source,
                source_run_dir=run_dir,
                current_time=current_time,
            )
            if scope == run_dir.resolve():
                local_state = state
                local_changed = changed
        return {**local_state, "changed": local_changed}


def start_progress_action(
    run_dir: Path,
    action: str,
    *,
    action_type: str,
    metadata: Mapping[str, object],
    control_dirs: Sequence[Path],
    start_work: Callable[[], dict[str, object]],
    now: float | None = None,
) -> dict[str, object]:
    """Atomically reject STALLED or start work and refresh the action vector."""
    paths = _progress_paths(run_dir, control_dirs)
    if not paths:
        return start_work()
    current_time = time.time() if now is None else float(now)
    with _locked_progress_scope(paths) as scoped:
        stall = _evaluate_stall_unlocked(
            scoped,
            action=action,
            current_time=current_time,
        )
        if stall:
            raise ProgressStalled(stall)
        work_event = start_work()
        vector: dict[str, object] = {
            "current_function": action,
            "lifecycle": "action_started",
        }
        for key in ("stage_id", "round"):
            if key in metadata:
                vector[key] = metadata[key]
        for scope in scoped:
            _write_observation_unlocked(
                scope,
                vector,
                source=f"action:{action_type}",
                source_run_dir=run_dir,
                current_time=current_time,
            )
        return work_event


def enforce_progress_deadline(
    run_dir: Path,
    action: str,
    *,
    control_dirs: Sequence[Path] = (),
    now: float | None = None,
) -> None:
    paths = _progress_paths(run_dir, control_dirs)
    if not paths:
        return
    current_time = time.time() if now is None else float(now)
    with _locked_progress_scope(paths) as scoped:
        stall = _evaluate_stall_unlocked(
            scoped,
            action=action,
            current_time=current_time,
        )
        if stall:
            raise ProgressStalled(stall)


def progress_status(
    run_dir: Path,
    *,
    now: float | None = None,
    evaluate: bool = True,
) -> dict[str, object]:
    policy = read_progress_policy(run_dir)
    if not policy:
        return {"state": "not_configured", "run_dir": str(run_dir.resolve())}
    current_time = time.time() if now is None else float(now)
    if evaluate:
        try:
            enforce_progress_deadline(run_dir, "progress_status", now=current_time)
        except ProgressStalled:
            pass
    state = read_progress_state(run_dir)
    stall = read_stall_state(run_dir)
    idle = max(0.0, current_time - _last_progress_epoch(run_dir))
    threshold = _max_idle_seconds(run_dir)
    return {
        "state": "stalled" if stall else "active",
        "runtime_state": "STALLED" if stall else state.get("runtime_state", "RUNNING"),
        "run_dir": str(run_dir.resolve()),
        "scope_kind": policy.get("scope_kind"),
        "policy": policy.get("policy"),
        "last_progress_at": state.get("last_progress_at"),
        "idle_seconds": round(idle, 6),
        "remaining_idle_seconds": max(0.0, threshold - idle),
        "vector": state.get("vector", {}),
        "vector_hash": state.get("vector_hash", ""),
        "source": state.get("source", ""),
        "stall": stall or None,
    }


def remaining_progress_seconds(
    run_dir: Path,
    control_dirs: Sequence[Path] = (),
) -> float | None:
    values: list[float] = []
    for path in _progress_paths(run_dir, control_dirs):
        status = progress_status(path, evaluate=False)
        if status.get("state") == "stalled":
            values.append(0.0)
            continue
        try:
            values.append(float(status.get("remaining_idle_seconds")))
        except (TypeError, ValueError):
            continue
    return min(values) if values else None


def bounded_progress_timeout(
    requested_timeout: float,
    run_dir: Path,
    control_dirs: Sequence[Path] = (),
) -> float:
    remaining = remaining_progress_seconds(run_dir, control_dirs)
    if remaining is None:
        return float(requested_timeout)
    return max(0.001, min(float(requested_timeout), remaining))


def work_starts_after_stall(run_dir: Path) -> list[dict[str, object]]:
    stall = read_stall_state(run_dir)
    if not stall:
        return []
    try:
        sequence = int(stall.get("progress_sequence", -1))
    except (TypeError, ValueError):
        sequence = -1
    return [
        event
        for event in read_progress_events(run_dir)
        if bool(event.get("starts_work"))
        and int(event.get("sequence", 0) or 0) > sequence
    ]
