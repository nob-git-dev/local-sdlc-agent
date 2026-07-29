"""Persistent control tokens for autonomous runner execution."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Mapping

from .models import RunnerError


CANCEL_FILENAME = "cancel.json"


def control_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def cancel_file_path(run_dir: Path) -> Path:
    return run_dir / CANCEL_FILENAME


def load_cancel_state(run_dir: Path) -> dict[str, object]:
    path = cancel_file_path(run_dir)
    if not path.exists():
        return {}
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
    run_dir.mkdir(parents=True, exist_ok=True)
    existing = load_cancel_state(run_dir)
    if existing:
        return existing
    payload: dict[str, object] = {
        "status": "cancelled",
        "source": source,
        "reason": reason,
        "requested_at": control_timestamp(),
    }
    if metadata:
        payload["metadata"] = dict(metadata)
    cancel_file_path(run_dir).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def ensure_not_cancelled(run_dir: Path, action: str) -> None:
    state = load_cancel_state(run_dir)
    if not state:
        return
    reason = str(state.get("reason") or "user_cancelled")
    source = str(state.get("source") or "unknown")
    raise RunnerError(f"cancelled before {action}: {reason} (source={source})")
