"""Persistent control tokens for autonomous runner execution."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
import json
import time
from pathlib import Path
from typing import Iterator, Mapping, Sequence

import fcntl

from .models import RunnerError
from .runtime_events import cancellation_state_from_ledger, record_progress_payload


CANCEL_FILENAME = "cancel.json"
PROGRESS_FILENAME = "progress.jsonl"
CONTROL_LOCK_FILENAME = ".control.lock"


def control_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def cancel_file_path(run_dir: Path) -> Path:
    return run_dir / CANCEL_FILENAME


def progress_file_path(run_dir: Path) -> Path:
    return run_dir / PROGRESS_FILENAME


def control_lock_file_path(run_dir: Path) -> Path:
    return run_dir / CONTROL_LOCK_FILENAME


def _control_scope(run_dir: Path, control_dirs: Sequence[Path] = ()) -> list[Path]:
    scoped: dict[str, Path] = {}
    for path in (run_dir, *control_dirs):
        resolved = path.resolve()
        scoped[str(resolved)] = resolved
    return [scoped[key] for key in sorted(scoped)]


@contextmanager
def _locked_control_scope(run_dir: Path, control_dirs: Sequence[Path] = ()) -> Iterator[list[Path]]:
    scoped = _control_scope(run_dir, control_dirs)
    with ExitStack() as stack:
        for path in scoped:
            path.mkdir(parents=True, exist_ok=True)
            lock_file = stack.enter_context(control_lock_file_path(path).open("a+", encoding="utf-8"))
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            stack.callback(fcntl.flock, lock_file.fileno(), fcntl.LOCK_UN)
        yield scoped


def read_progress_events(run_dir: Path) -> list[dict[str, object]]:
    path = progress_file_path(run_dir)
    if not path.exists():
        return []
    events: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def _next_progress_sequence(run_dir: Path) -> int:
    return len(read_progress_events(run_dir)) + 1


def _append_progress_event_unlocked(
    run_dir: Path,
    event: str,
    *,
    action: str = "",
    starts_work: bool = False,
    metadata: Mapping[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "sequence": _next_progress_sequence(run_dir),
        "timestamp": control_timestamp(),
        "event": event,
        "action": action,
        "starts_work": starts_work,
    }
    if metadata:
        payload["metadata"] = dict(metadata)
    record_progress_payload(run_dir, payload)
    with progress_file_path(run_dir).open("a", encoding="utf-8") as file:
        file.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    return payload


def append_progress_event(
    run_dir: Path,
    event: str,
    *,
    action: str = "",
    starts_work: bool = False,
    metadata: Mapping[str, object] | None = None,
) -> dict[str, object]:
    with _locked_control_scope(run_dir):
        return _append_progress_event_unlocked(
            run_dir,
            event,
            action=action,
            starts_work=starts_work,
            metadata=metadata,
        )


def load_cancel_state(run_dir: Path) -> dict[str, object]:
    path = cancel_file_path(run_dir)
    if not path.exists():
        return cancellation_state_from_ledger(run_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "status": "cancelled",
            "reason": "invalid_cancel_file",
            "source": "control",
            "path": str(path),
        }
    return payload if isinstance(payload, dict) else {
        "status": "cancelled",
        "reason": "invalid_cancel_file",
        "source": "control",
        "path": str(path),
    }


def cancel_requested(run_dir: Path) -> bool:
    state = load_cancel_state(run_dir)
    return bool(state) and str(state.get("status") or "cancelled") == "cancelled"


def request_cancel(
    run_dir: Path,
    *,
    source: str = "user",
    reason: str = "user_cancelled",
    metadata: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Persist an idempotent cancellation token in a run directory."""
    with _locked_control_scope(run_dir):
        existing = load_cancel_state(run_dir)
        if existing:
            return existing
        payload: dict[str, object] = {
            "status": "cancelled",
            "source": source,
            "reason": reason,
            "requested_at": control_timestamp(),
            "progress_sequence": len(read_progress_events(run_dir)),
        }
        if metadata:
            payload["metadata"] = dict(metadata)
        _append_progress_event_unlocked(
            run_dir,
            "cancel_requested",
            action="cancel",
            starts_work=False,
            metadata={"source": source, "reason": reason},
        )
        cancel_file_path(run_dir).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload


def _ensure_scope_not_cancelled(scoped: Sequence[Path], action: str) -> None:
    for path in scoped:
        state = load_cancel_state(path)
        if not state:
            continue
        reason = str(state.get("reason") or "user_cancelled")
        source = str(state.get("source") or "unknown")
        raise RunnerError(f"cancelled before {action}: {reason} (source={source}, run_dir={path})")


def ensure_not_cancelled(run_dir: Path, action: str, control_dirs: Sequence[Path] = ()) -> None:
    with _locked_control_scope(run_dir, control_dirs) as scoped:
        _ensure_scope_not_cancelled(scoped, action)


def record_work_start(
    run_dir: Path,
    action: str,
    *,
    metadata: Mapping[str, object] | None = None,
    control_dirs: Sequence[Path] = (),
) -> dict[str, object]:
    with _locked_control_scope(run_dir, control_dirs) as scoped:
        _ensure_scope_not_cancelled(scoped, action)
        event = _append_progress_event_unlocked(
            run_dir,
            "work_start",
            action=action,
            starts_work=True,
            metadata=metadata,
        )
        for controller in scoped:
            if controller == run_dir.resolve():
                continue
            mirrored_metadata = dict(metadata or {})
            mirrored_metadata.update({"mirrored": True, "child_run_dir": str(run_dir.resolve())})
            _append_progress_event_unlocked(
                controller,
                "work_start",
                action=action,
                starts_work=True,
                metadata=mirrored_metadata,
            )
        return event


def work_starts_after_cancel(run_dir: Path) -> list[dict[str, object]]:
    cancel_state = load_cancel_state(run_dir)
    if not cancel_state:
        return []
    try:
        cancel_sequence = int(cancel_state.get("progress_sequence", -1))
    except (TypeError, ValueError):
        cancel_sequence = -1
    return [
        event
        for event in read_progress_events(run_dir)
        if bool(event.get("starts_work")) and int(event.get("sequence", 0) or 0) > cancel_sequence
    ]
