"""Persistent multi-dimensional budgets for autonomous runtime actions."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
import json
import time
from pathlib import Path
from typing import Iterator, Mapping, Sequence

import fcntl

from .models import RunnerError
from .runtime_events import record_budget_payload


BUDGET_POLICY_FILENAME = "budget.json"
BUDGET_EVENTS_FILENAME = "budget_events.jsonl"
BUDGET_STOP_FILENAME = "budget_stop.json"
BUDGET_LOCK_FILENAME = ".budget.lock"
BUDGET_SCHEMA_VERSION = 1

DEFAULT_MAX_GOAL_ACTIONS = 1000
DEFAULT_MAX_STAGE_ACTIONS = 200
DEFAULT_MAX_RECOVERY_ACTIONS = 100
DEFAULT_MAX_API_CALLS = 250
DEFAULT_MAX_WALL_SECONDS = 86400.0

RECOVERY_ACTION_TYPES = {"recovery", "resume", "retry", "stage_split"}
VALID_SCOPE_KINDS = {"goal", "stage", "goal_stage"}


@dataclass(frozen=True)
class BudgetLimits:
    max_goal_actions: int = DEFAULT_MAX_GOAL_ACTIONS
    max_stage_actions: int = DEFAULT_MAX_STAGE_ACTIONS
    max_recovery_actions: int = DEFAULT_MAX_RECOVERY_ACTIONS
    max_api_calls: int = DEFAULT_MAX_API_CALLS
    max_wall_seconds: float = DEFAULT_MAX_WALL_SECONDS

    def validate(self) -> None:
        count_values = {
            "max_goal_actions": self.max_goal_actions,
            "max_stage_actions": self.max_stage_actions,
            "max_recovery_actions": self.max_recovery_actions,
            "max_api_calls": self.max_api_calls,
        }
        invalid = [name for name, value in count_values.items() if int(value) < 0]
        if invalid:
            raise RunnerError("count budget limits must be zero or greater: " + ", ".join(invalid))
        if float(self.max_wall_seconds) <= 0:
            raise RunnerError("max_wall_seconds must be greater than zero")


class BudgetExceeded(RunnerError):
    """Raised when an absorbing runtime budget stop prevents an action."""

    def __init__(self, stop: Mapping[str, object]):
        self.stop = dict(stop)
        dimension = str(stop.get("dimension") or "unknown")
        used = stop.get("used")
        limit = stop.get("limit")
        action = str(stop.get("action") or "unknown")
        scope = str(stop.get("scope_run_dir") or "unknown")
        super().__init__(
            f"budget exhausted before {action}: {dimension} used={used} limit={limit} "
            f"(run_dir={scope})"
        )


def budget_timestamp(epoch: float | None = None) -> str:
    value = time.time() if epoch is None else epoch
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))


def budget_policy_file_path(run_dir: Path) -> Path:
    return run_dir / BUDGET_POLICY_FILENAME


def budget_events_file_path(run_dir: Path) -> Path:
    return run_dir / BUDGET_EVENTS_FILENAME


def budget_stop_file_path(run_dir: Path) -> Path:
    return run_dir / BUDGET_STOP_FILENAME


def _budget_lock_file_path(run_dir: Path) -> Path:
    return run_dir / BUDGET_LOCK_FILENAME


def budget_limits_from_args(args: object) -> BudgetLimits:
    limits = BudgetLimits(
        max_goal_actions=int(getattr(args, "max_goal_actions", DEFAULT_MAX_GOAL_ACTIONS)),
        max_stage_actions=int(getattr(args, "max_stage_actions", DEFAULT_MAX_STAGE_ACTIONS)),
        max_recovery_actions=int(getattr(args, "max_recovery_actions", DEFAULT_MAX_RECOVERY_ACTIONS)),
        max_api_calls=int(getattr(args, "max_api_calls", DEFAULT_MAX_API_CALLS)),
        max_wall_seconds=float(getattr(args, "max_wall_seconds", DEFAULT_MAX_WALL_SECONDS)),
    )
    limits.validate()
    return limits


def _read_json_object(path: Path, label: str) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunnerError(f"invalid {label}: {path}") from exc
    if not isinstance(payload, dict):
        raise RunnerError(f"invalid {label}: {path}")
    return payload


def read_budget_policy(run_dir: Path) -> dict[str, object]:
    return _read_json_object(budget_policy_file_path(run_dir), "budget policy")


def read_budget_stop(run_dir: Path) -> dict[str, object]:
    return _read_json_object(budget_stop_file_path(run_dir), "budget stop")


def read_budget_events(run_dir: Path) -> list[dict[str, object]]:
    path = budget_events_file_path(run_dir)
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


@contextmanager
def _locked_budget_scope(paths: Sequence[Path]) -> Iterator[list[Path]]:
    unique = {str(path.resolve()): path.resolve() for path in paths}
    scoped = [unique[key] for key in sorted(unique)]
    with ExitStack() as stack:
        for path in scoped:
            path.mkdir(parents=True, exist_ok=True)
            lock_file = stack.enter_context(_budget_lock_file_path(path).open("a+", encoding="utf-8"))
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            stack.callback(fcntl.flock, lock_file.fileno(), fcntl.LOCK_UN)
        yield scoped


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def initialize_budget(
    run_dir: Path,
    limits: BudgetLimits,
    *,
    scope_kind: str,
    now: float | None = None,
) -> dict[str, object]:
    limits.validate()
    if scope_kind not in VALID_SCOPE_KINDS:
        raise RunnerError(f"invalid budget scope kind: {scope_kind}")
    with _locked_budget_scope((run_dir,)):
        existing = read_budget_policy(run_dir)
        if existing:
            if existing.get("scope_kind") != scope_kind:
                raise RunnerError(
                    f"budget scope mismatch: existing={existing.get('scope_kind')} requested={scope_kind}"
                )
            return existing
        started_at_epoch = time.time() if now is None else float(now)
        policy: dict[str, object] = {
            "schema_version": BUDGET_SCHEMA_VERSION,
            "scope_kind": scope_kind,
            "started_at": budget_timestamp(started_at_epoch),
            "started_at_epoch": started_at_epoch,
            "limits": asdict(limits),
        }
        _atomic_write_json(budget_policy_file_path(run_dir), policy)
        return policy


def _policy_limits(policy: Mapping[str, object]) -> dict[str, float]:
    raw = policy.get("limits")
    if not isinstance(raw, Mapping):
        raise RunnerError("budget policy has no limits object")
    names = (
        "max_goal_actions",
        "max_stage_actions",
        "max_recovery_actions",
        "max_api_calls",
        "max_wall_seconds",
    )
    try:
        limits = {name: float(raw[name]) for name in names}
    except (KeyError, TypeError, ValueError) as exc:
        raise RunnerError("budget policy contains invalid limits") from exc
    if any(limits[name] < 0 for name in names if name != "max_wall_seconds"):
        raise RunnerError("budget policy contains a negative count limit")
    if limits["max_wall_seconds"] <= 0:
        raise RunnerError("budget policy contains a non-positive wall limit")
    return limits


def _usage(run_dir: Path) -> dict[str, int]:
    usage = {
        "goal_actions": 0,
        "stage_actions": 0,
        "recovery_actions": 0,
        "api_calls": 0,
    }
    for event in read_budget_events(run_dir):
        outcome = event.get("outcome")
        if outcome not in {"consumed", "refunded"}:
            continue
        charges = event.get("charges")
        if not isinstance(charges, Mapping):
            continue
        direction = -1 if outcome == "refunded" else 1
        for name in usage:
            try:
                usage[name] += direction * int(charges.get(name, 0) or 0)
            except (TypeError, ValueError):
                continue
    return usage


def _charges_for_scope(scope_kind: str, action_type: str) -> dict[str, int]:
    charges: dict[str, int] = {}
    if scope_kind in {"goal", "goal_stage"}:
        charges["goal_actions"] = 1
        if action_type == "api_call":
            charges["api_calls"] = 1
        if action_type in RECOVERY_ACTION_TYPES:
            charges["recovery_actions"] = 1
    if scope_kind in {"stage", "goal_stage"}:
        charges["stage_actions"] = 1
    return charges


def _append_budget_event_unlocked(run_dir: Path, payload: Mapping[str, object]) -> dict[str, object]:
    sequence = len(read_budget_events(run_dir)) + 1
    event = {
        "sequence": sequence,
        "event_id": f"B{sequence:06d}",
        "timestamp": budget_timestamp(),
        **dict(payload),
    }
    record_budget_payload(run_dir, event)
    with budget_events_file_path(run_dir).open("a", encoding="utf-8") as file:
        file.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    return event


def _budget_paths(run_dir: Path, budget_dirs: Sequence[Path]) -> list[Path]:
    candidates = [run_dir.resolve(), *(path.resolve() for path in budget_dirs)]
    return [path for path in candidates if budget_policy_file_path(path).exists()]


def _existing_stop(scoped: Sequence[Path]) -> dict[str, object]:
    for path in scoped:
        stop = read_budget_stop(path)
        if stop:
            return stop
    return {}


def _persist_stop_unlocked(
    evaluations: Sequence[Mapping[str, object]],
    stop: Mapping[str, object],
    *,
    outcome: str,
) -> None:
    for evaluation in evaluations:
        scope = evaluation["scope"]
        if not isinstance(scope, Path):
            continue
        _append_budget_event_unlocked(
            scope,
            {
                "outcome": outcome,
                "action": stop.get("action"),
                "action_type": stop.get("action_type"),
                "action_id": stop.get("action_id"),
                "charges": evaluation.get("charges", {}),
                "usage_before": evaluation.get("usage", {}),
                "elapsed_seconds": round(float(evaluation.get("elapsed", 0.0)), 6),
                "reason": stop.get("reason"),
            },
        )
        if not budget_stop_file_path(scope).exists():
            _atomic_write_json(budget_stop_file_path(scope), stop)


def consume_action_budget(
    run_dir: Path,
    action: str,
    *,
    action_type: str,
    action_id: str,
    budget_dirs: Sequence[Path] = (),
    now: float | None = None,
) -> dict[str, object] | None:
    paths = _budget_paths(run_dir, budget_dirs)
    if not paths:
        return None
    current_time = time.time() if now is None else float(now)
    with _locked_budget_scope(paths) as scoped:
        stopped = _existing_stop(scoped)
        if stopped:
            evaluations = []
            for scope in scoped:
                policy = read_budget_policy(scope)
                started = float(policy.get("started_at_epoch", current_time))
                evaluations.append(
                    {
                        "scope": scope,
                        "charges": _charges_for_scope(
                            str(policy.get("scope_kind") or ""),
                            action_type,
                        ),
                        "usage": _usage(scope),
                        "elapsed": max(0.0, current_time - started),
                    }
                )
            missing_stop_scopes = [
                item
                for item in evaluations
                if isinstance(item.get("scope"), Path)
                and not budget_stop_file_path(item["scope"]).exists()
            ]
            if missing_stop_scopes:
                _persist_stop_unlocked(
                    missing_stop_scopes,
                    stopped,
                    outcome="propagated_stop",
                )
            raise BudgetExceeded(stopped)

        evaluations: list[dict[str, object]] = []
        violations: list[dict[str, object]] = []
        dimension_limits = {
            "goal_actions": "max_goal_actions",
            "stage_actions": "max_stage_actions",
            "recovery_actions": "max_recovery_actions",
            "api_calls": "max_api_calls",
        }
        for scope in scoped:
            policy = read_budget_policy(scope)
            limits = _policy_limits(policy)
            usage = _usage(scope)
            scope_kind = str(policy.get("scope_kind") or "")
            charges = _charges_for_scope(scope_kind, action_type)
            started = float(policy.get("started_at_epoch", current_time))
            elapsed = max(0.0, current_time - started)
            evaluation = {
                "scope": scope,
                "scope_kind": scope_kind,
                "policy": policy,
                "limits": limits,
                "usage": usage,
                "charges": charges,
                "elapsed": elapsed,
            }
            evaluations.append(evaluation)
            if elapsed >= limits["max_wall_seconds"]:
                violations.append(
                    {
                        "scope": scope,
                        "dimension": "wall_seconds",
                        "used": round(elapsed, 6),
                        "limit": limits["max_wall_seconds"],
                    }
                )
            for dimension, charge in charges.items():
                limit_name = dimension_limits[dimension]
                proposed = usage[dimension] + charge
                if proposed > limits[limit_name]:
                    violations.append(
                        {
                            "scope": scope,
                            "dimension": dimension,
                            "used": usage[dimension],
                            "limit": limits[limit_name],
                        }
                    )

        if violations:
            violation = violations[0]
            stop = {
                "schema_version": BUDGET_SCHEMA_VERSION,
                "status": "budget_exhausted",
                "stopped_at": budget_timestamp(current_time),
                "action": action,
                "action_type": action_type,
                "action_id": action_id,
                "dimension": violation["dimension"],
                "used": violation["used"],
                "limit": violation["limit"],
                "scope_run_dir": str(violation["scope"]),
                "reason": (
                    f"{violation['dimension']} budget exhausted before action {action}: "
                    f"used={violation['used']} limit={violation['limit']}"
                ),
            }
            _persist_stop_unlocked(evaluations, stop, outcome="denied")
            raise BudgetExceeded(stop)

        local_event: dict[str, object] | None = None
        for evaluation in evaluations:
            scope = evaluation["scope"]
            usage_before = evaluation["usage"]
            charges = evaluation["charges"]
            usage_after = {
                name: int(usage_before[name]) + int(charges.get(name, 0) or 0)
                for name in usage_before
            }
            event = _append_budget_event_unlocked(
                scope,
                {
                    "outcome": "consumed",
                    "action": action,
                    "action_type": action_type,
                    "action_id": action_id,
                    "charges": charges,
                    "usage_before": usage_before,
                    "usage_after": usage_after,
                    "elapsed_seconds": round(float(evaluation["elapsed"]), 6),
                },
            )
            if scope == run_dir.resolve():
                local_event = event
        return local_event or None


def refund_action_budget(
    run_dir: Path,
    action: str,
    *,
    action_type: str,
    action_id: str,
    budget_dirs: Sequence[Path] = (),
    reason: str,
) -> None:
    """Append a compensating event when admitted work never reaches work_start."""
    paths = _budget_paths(run_dir, budget_dirs)
    if not paths:
        return
    with _locked_budget_scope(paths) as scoped:
        for scope in scoped:
            events = read_budget_events(scope)
            consumed = next(
                (
                    event
                    for event in reversed(events)
                    if event.get("action_id") == action_id
                    and event.get("outcome") == "consumed"
                ),
                None,
            )
            already_refunded = any(
                event.get("action_id") == action_id
                and event.get("outcome") == "refunded"
                for event in events
            )
            if consumed is None or already_refunded:
                continue
            charges = consumed.get("charges")
            if not isinstance(charges, Mapping):
                charges = {}
            usage_before = _usage(scope)
            usage_after = {
                name: max(0, int(value) - int(charges.get(name, 0) or 0))
                for name, value in usage_before.items()
            }
            _append_budget_event_unlocked(
                scope,
                {
                    "outcome": "refunded",
                    "action": action,
                    "action_type": action_type,
                    "action_id": action_id,
                    "charges": dict(charges),
                    "usage_before": usage_before,
                    "usage_after": usage_after,
                    "reason": reason,
                    "consumed_event_id": consumed.get("event_id"),
                },
            )


def enforce_wall_budget(
    run_dir: Path,
    action: str,
    *,
    action_type: str,
    budget_dirs: Sequence[Path] = (),
    action_id: str = "",
    now: float | None = None,
) -> None:
    """Persist and raise an absorbing stop when wall time expires in-flight."""
    paths = _budget_paths(run_dir, budget_dirs)
    if not paths:
        return
    current_time = time.time() if now is None else float(now)
    with _locked_budget_scope(paths) as scoped:
        stopped = _existing_stop(scoped)
        if stopped:
            raise BudgetExceeded(stopped)
        evaluations: list[dict[str, object]] = []
        violation: dict[str, object] | None = None
        for scope in scoped:
            policy = read_budget_policy(scope)
            limits = _policy_limits(policy)
            usage = _usage(scope)
            started = float(policy.get("started_at_epoch", current_time))
            elapsed = max(0.0, current_time - started)
            evaluation = {
                "scope": scope,
                "charges": {},
                "usage": usage,
                "elapsed": elapsed,
            }
            evaluations.append(evaluation)
            if violation is None and elapsed >= limits["max_wall_seconds"]:
                violation = {
                    "scope": scope,
                    "used": round(elapsed, 6),
                    "limit": limits["max_wall_seconds"],
                }
        if violation is None:
            return
        stop = {
            "schema_version": BUDGET_SCHEMA_VERSION,
            "status": "budget_exhausted",
            "stopped_at": budget_timestamp(current_time),
            "action": action,
            "action_type": action_type,
            "action_id": action_id,
            "dimension": "wall_seconds",
            "used": violation["used"],
            "limit": violation["limit"],
            "scope_run_dir": str(violation["scope"]),
            "reason": (
                f"wall_seconds budget exhausted during action {action}: "
                f"used={violation['used']} limit={violation['limit']}"
            ),
        }
        _persist_stop_unlocked(evaluations, stop, outcome="exhausted_during_action")
        raise BudgetExceeded(stop)


def budget_status(run_dir: Path, *, now: float | None = None) -> dict[str, object]:
    policy = read_budget_policy(run_dir)
    if not policy:
        return {"state": "not_configured", "run_dir": str(run_dir.resolve())}
    current_time = time.time() if now is None else float(now)
    limits = _policy_limits(policy)
    usage = _usage(run_dir)
    started = float(policy.get("started_at_epoch", current_time))
    elapsed = max(0.0, current_time - started)
    stop = read_budget_stop(run_dir)
    remaining = {
        "goal_actions": max(0, int(limits["max_goal_actions"]) - usage["goal_actions"]),
        "stage_actions": max(0, int(limits["max_stage_actions"]) - usage["stage_actions"]),
        "recovery_actions": max(0, int(limits["max_recovery_actions"]) - usage["recovery_actions"]),
        "api_calls": max(0, int(limits["max_api_calls"]) - usage["api_calls"]),
        "wall_seconds": max(0.0, limits["max_wall_seconds"] - elapsed),
    }
    return {
        "state": "exhausted" if stop else "active",
        "run_dir": str(run_dir.resolve()),
        "scope_kind": policy.get("scope_kind"),
        "started_at": policy.get("started_at"),
        "limits": policy.get("limits"),
        "usage": usage,
        "elapsed_seconds": round(elapsed, 6),
        "remaining": remaining,
        "stop": stop or None,
        "event_count": len(read_budget_events(run_dir)),
    }


def remaining_wall_seconds(run_dir: Path, budget_dirs: Sequence[Path] = ()) -> float | None:
    values: list[float] = []
    for path in _budget_paths(run_dir, budget_dirs):
        status = budget_status(path)
        remaining = status.get("remaining")
        if isinstance(remaining, Mapping):
            try:
                values.append(float(remaining.get("wall_seconds")))
            except (TypeError, ValueError):
                continue
    return min(values) if values else None


def bounded_action_timeout(
    requested_timeout: float,
    run_dir: Path,
    budget_dirs: Sequence[Path] = (),
) -> float:
    remaining = remaining_wall_seconds(run_dir, budget_dirs)
    if remaining is None:
        return requested_timeout
    return max(0.001, min(float(requested_timeout), remaining))
