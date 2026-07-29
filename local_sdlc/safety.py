"""Safety decisions for runner actions."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import datetime as _datetime
import json
import re
from pathlib import Path
from typing import Mapping


SAFETY_DECISIONS_FILENAME = "safety_decisions.jsonl"
APPROVAL_REQUIRED_RISK_CLASSES = {
    "database_write",
    "docker_control",
    "filesystem_delete",
    "privileged_command",
    "service_control",
}


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


def safety_timestamp() -> str:
    return _datetime.datetime.now(_datetime.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safety_decisions_file_path(run_dir: Path) -> Path:
    return run_dir / SAFETY_DECISIONS_FILENAME


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


def record_safety_decision(run_dir: Path, decision: SafetyDecision) -> dict[str, object]:
    run_dir.mkdir(parents=True, exist_ok=True)
    persisted = replace(
        decision,
        sequence=len(read_safety_decisions(run_dir)) + 1,
        timestamp=safety_timestamp(),
    )
    payload = asdict(persisted)
    if payload.get("metadata") is None:
        payload.pop("metadata", None)
    with safety_decisions_file_path(run_dir).open("a", encoding="utf-8") as file:
        file.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    return payload


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
        return SafetyDecision(
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

    risk_class = command_risk_class(command, danger_reason or "")
    if danger_reason:
        decision = "require_approval" if risk_class in {"privileged_command", "service_control", "docker_control"} else "block"
        return SafetyDecision(
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

    if risk_class in APPROVAL_REQUIRED_RISK_CLASSES:
        return SafetyDecision(
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

    return SafetyDecision(
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


def blocked_reason_from_safety_decision(decision: SafetyDecision) -> str:
    if decision.decision == "require_approval":
        return f"requires human approval: {decision.reason}"
    if decision.decision == "block":
        return decision.reason
    return ""
