"""Safety decisions for runner actions."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
import datetime as _datetime
import hashlib
import json
import re
from pathlib import Path
from typing import Iterator, Mapping

import fcntl

from .models import RunnerError
from .runtime_events import record_approval_payload, record_safety_payload


SAFETY_DECISIONS_FILENAME = "safety_decisions.jsonl"
SAFETY_APPROVALS_FILENAME = "safety_approvals.jsonl"
SAFETY_LOCK_FILENAME = ".safety.lock"
APPROVAL_REQUIRED_RISK_CLASSES = {
    "database_write",
    "docker_control",
    "filesystem_delete",
    "network_exposure",
    "production_like",
    "privileged_command",
    "secret_access",
    "service_control",
}
BLOCKED_RISK_CLASSES = {"git_history_rewrite"}
ALLOW_RISK_CLASSES = {"read_only", "generated_code_execution", "project_write"}
HUMAN_APPROVAL_SOURCES = {"cli", "web", "test"}


@dataclass(frozen=True)
class SafetyDecision:
    sequence: int
    timestamp: str
    action: str
    action_type: str
    decision: str
    risk_class: str
    reason: str
    command: str = ""
    metadata: dict[str, object] | None = None
    decision_id: str = ""
    approval_key: str = ""
    approval_id: str = ""


def safety_timestamp() -> str:
    return (
        _datetime.datetime.now(_datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def safety_decisions_file_path(run_dir: Path) -> Path:
    return run_dir / SAFETY_DECISIONS_FILENAME


def safety_approvals_file_path(run_dir: Path) -> Path:
    return run_dir / SAFETY_APPROVALS_FILENAME


@contextmanager
def _safety_lock(run_dir: Path) -> Iterator[None]:
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / SAFETY_LOCK_FILENAME).open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def read_safety_decisions(run_dir: Path) -> list[dict[str, object]]:
    path = safety_decisions_file_path(run_dir)
    if not path.exists():
        return []
    decisions: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            decisions.append(payload)
    return decisions


def read_safety_approvals(run_dir: Path) -> list[dict[str, object]]:
    path = safety_approvals_file_path(run_dir)
    if not path.exists():
        return []
    approvals: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            approvals.append(payload)
    return approvals


def _approval_key(
    *,
    action: str,
    action_type: str,
    risk_class: str,
    command: str = "",
    metadata: Mapping[str, object] | None = None,
) -> str:
    approval_scope = str((metadata or {}).get("approval_scope") or "")
    basis = json.dumps(
        {
            "action": action if not command else "command",
            "action_type": action_type,
            "risk_class": risk_class,
            "command": command,
            "approval_scope": approval_scope,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _decision_approval_key(decision: SafetyDecision | Mapping[str, object]) -> str:
    if isinstance(decision, SafetyDecision):
        existing = decision.approval_key
        action = decision.action
        action_type = decision.action_type
        risk_class = decision.risk_class
        command = decision.command
        metadata = decision.metadata
    else:
        existing = str(decision.get("approval_key") or "")
        action = str(decision.get("action") or "")
        action_type = str(decision.get("action_type") or "")
        risk_class = str(decision.get("risk_class") or "")
        command = str(decision.get("command") or "")
        raw_metadata = decision.get("metadata")
        metadata = raw_metadata if isinstance(raw_metadata, Mapping) else None
    return existing or _approval_key(
        action=action,
        action_type=action_type,
        risk_class=risk_class,
        command=command,
        metadata=metadata,
    )


def _record_safety_decision_unlocked(run_dir: Path, decision: SafetyDecision) -> dict[str, object]:
    sequence = len(read_safety_decisions(run_dir)) + 1
    persisted = replace(
        decision,
        sequence=sequence,
        timestamp=safety_timestamp(),
        decision_id=decision.decision_id or f"D{sequence:06d}",
        approval_key=_decision_approval_key(decision),
    )
    payload = asdict(persisted)
    if payload.get("metadata") is None:
        payload.pop("metadata", None)
    record_safety_payload(run_dir, payload)
    with safety_decisions_file_path(run_dir).open("a", encoding="utf-8") as file:
        file.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    return payload


def record_safety_decision(run_dir: Path, decision: SafetyDecision) -> dict[str, object]:
    with _safety_lock(run_dir):
        return _record_safety_decision_unlocked(run_dir, decision)


def _append_approval_event_unlocked(run_dir: Path, payload: dict[str, object]) -> dict[str, object]:
    event = dict(payload)
    event["sequence"] = len(read_safety_approvals(run_dir)) + 1
    event["timestamp"] = safety_timestamp()
    record_approval_payload(run_dir, event)
    with safety_approvals_file_path(run_dir).open("a", encoding="utf-8") as file:
        file.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    return event


def request_safety_approval(
    run_dir: Path,
    decision_id: str,
    *,
    source: str = "cli",
    note: str = "",
) -> dict[str, object]:
    if source not in HUMAN_APPROVAL_SOURCES:
        raise RunnerError("safety approval source must be an explicit human channel")
    with _safety_lock(run_dir):
        decision = next(
            (item for item in read_safety_decisions(run_dir) if item.get("decision_id") == decision_id),
            None,
        )
        if decision is None:
            raise RunnerError(f"safety decision not found: {decision_id}")
        if decision.get("decision") != "require_approval":
            raise RunnerError("only require_approval decisions can receive human approval")
        existing = next(
            (
                item
                for item in read_safety_approvals(run_dir)
                if item.get("event") == "approved" and item.get("decision_id") == decision_id
            ),
            None,
        )
        if existing:
            return existing
        approvals = read_safety_approvals(run_dir)
        approval_id = f"A{sum(1 for item in approvals if item.get('event') == 'approved') + 1:06d}"
        return _append_approval_event_unlocked(
            run_dir,
            {
                "event": "approved",
                "approval_id": approval_id,
                "decision_id": decision_id,
                "approval_key": _decision_approval_key(decision),
                "source": source,
                "note": note,
            },
        )


def _available_approval_unlocked(run_dir: Path, approval_key: str) -> dict[str, object] | None:
    events = read_safety_approvals(run_dir)
    consumed = {
        str(item.get("approval_id"))
        for item in events
        if item.get("event") == "consumed" and item.get("approval_id")
    }
    for item in reversed(events):
        if (
            item.get("event") == "approved"
            and item.get("approval_key") == approval_key
            and str(item.get("approval_id")) not in consumed
        ):
            return item
    return None


def authorize_safety_decision(run_dir: Path, decision: SafetyDecision) -> dict[str, object]:
    with _safety_lock(run_dir):
        resolved = decision
        approval: dict[str, object] | None = None
        if decision.decision == "require_approval":
            approval = _available_approval_unlocked(run_dir, _decision_approval_key(decision))
            if approval:
                resolved = replace(
                    decision,
                    decision="allow",
                    reason="allowed by explicit one-time human approval",
                    approval_id=str(approval.get("approval_id") or ""),
                )
        persisted = _record_safety_decision_unlocked(run_dir, resolved)
        if approval:
            _append_approval_event_unlocked(
                run_dir,
                {
                    "event": "consumed",
                    "approval_id": approval.get("approval_id"),
                    "decision_id": approval.get("decision_id"),
                    "authorized_decision_id": persisted.get("decision_id"),
                    "approval_key": approval.get("approval_key"),
                },
            )
        return persisted


def pending_safety_decisions(run_dir: Path) -> list[dict[str, object]]:
    approved_ids = {
        str(item.get("decision_id"))
        for item in read_safety_approvals(run_dir)
        if item.get("event") == "approved"
    }
    return [
        item
        for item in read_safety_decisions(run_dir)
        if item.get("decision") == "require_approval"
        and str(item.get("decision_id")) not in approved_ids
    ]


def blocked_safety_decisions(run_dir: Path) -> list[dict[str, object]]:
    return [
        item
        for item in read_safety_decisions(run_dir)
        if item.get("decision") == "block"
    ]


def action_safety_decision(
    action: str,
    *,
    action_type: str,
    risk_class: str,
    command: str = "",
    metadata: Mapping[str, object] | None = None,
) -> SafetyDecision:
    if risk_class in BLOCKED_RISK_CLASSES:
        decision = "block"
        reason = f"{risk_class} is blocked by the local safety policy"
    elif risk_class in APPROVAL_REQUIRED_RISK_CLASSES:
        decision = "require_approval"
        reason = f"{risk_class} requires human approval"
    elif risk_class == "project_write" and bool((metadata or {}).get("isolated")):
        decision = "allow_in_worktree"
        reason = "project write is isolated in a temporary worktree"
    elif risk_class in ALLOW_RISK_CLASSES:
        decision = "allow"
        reason = "allowed by local action safety policy"
    else:
        decision = "require_approval"
        reason = f"unsupported risk class requires human approval: {risk_class}"
    return SafetyDecision(
        sequence=0,
        timestamp="",
        action=action,
        action_type=action_type,
        decision=decision,
        risk_class=risk_class,
        reason=reason,
        command=command,
        metadata=dict(metadata) if metadata else None,
        approval_key=_approval_key(
            action=action,
            action_type=action_type,
            risk_class=risk_class,
            command=command,
            metadata=metadata,
        ),
    )


def command_risk_class(command: str, reason: str = "") -> str:
    text = f"{command}\n{reason}"
    if re.search(r"(^|[;&|]\s*)sudo\s", command, flags=re.IGNORECASE):
        return "privileged_command"
    if re.search(r"\b(?:systemctl|service)\s+", command, flags=re.IGNORECASE):
        return "service_control"
    if re.search(r"\bdocker(?:\s+compose)?\s+", command, flags=re.IGNORECASE):
        return "docker_control"
    if re.search(r"\bgit\s+(?:reset\s+--hard|clean\s+-[fd]{1,2})\b", command, flags=re.IGNORECASE):
        return "git_history_rewrite"
    if re.search(r"\brm\s+-[rRfF]{1,2}\b", command, flags=re.IGNORECASE):
        return "filesystem_delete"
    if re.search(r"\b(?:DROP|TRUNCATE|DELETE\s+FROM|UPDATE\s+\w+\s+SET)\b", text, flags=re.IGNORECASE):
        return "database_write"
    return "generated_code_execution"


def command_safety_decision(
    command: str,
    *,
    danger_reason: str | None = None,
    shell_reason: str = "",
    action: str = "command",
    metadata: Mapping[str, object] | None = None,
) -> SafetyDecision:
    if shell_reason:
        decision = SafetyDecision(
            sequence=0,
            timestamp="",
            action=action,
            action_type="command",
            decision="block",
            risk_class="generated_code_execution",
            reason=shell_reason,
            command=command,
            metadata=dict(metadata) if metadata else None,
        )
        return replace(decision, approval_key=_decision_approval_key(decision))

    risk_class = command_risk_class(command, danger_reason or "")
    if danger_reason:
        decision = "require_approval" if risk_class in APPROVAL_REQUIRED_RISK_CLASSES else "block"
        result = SafetyDecision(
            sequence=0,
            timestamp="",
            action=action,
            action_type="command",
            decision=decision,
            risk_class=risk_class,
            reason=danger_reason,
            command=command,
            metadata=dict(metadata) if metadata else None,
        )
        return replace(result, approval_key=_decision_approval_key(result))

    if risk_class in APPROVAL_REQUIRED_RISK_CLASSES:
        decision = SafetyDecision(
            sequence=0,
            timestamp="",
            action=action,
            action_type="command",
            decision="require_approval",
            risk_class=risk_class,
            reason=f"{risk_class} requires human approval",
            command=command,
            metadata=dict(metadata) if metadata else None,
        )
        return replace(decision, approval_key=_decision_approval_key(decision))

    decision = SafetyDecision(
        sequence=0,
        timestamp="",
        action=action,
        action_type="command",
        decision="allow",
        risk_class=risk_class,
        reason="allowed by local command safety policy",
        command=command,
        metadata=dict(metadata) if metadata else None,
    )
    return replace(decision, approval_key=_decision_approval_key(decision))


def blocked_reason_from_safety_decision(decision: SafetyDecision | Mapping[str, object]) -> str:
    value = decision.decision if isinstance(decision, SafetyDecision) else str(decision.get("decision") or "")
    reason = decision.reason if isinstance(decision, SafetyDecision) else str(decision.get("reason") or "")
    if value == "require_approval":
        return f"requires human approval: {reason}"
    if value == "block":
        return reason
    return ""
