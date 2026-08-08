"""Supervisor autonomy policy, decisions, and completion contracts."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
import datetime as _datetime
import json
from pathlib import Path
from typing import Iterator, Mapping, Sequence

import fcntl

from .artifact_protocol import is_protocol_failure_type
from .models import RunnerError, StageRunSummary, StageWorkItem


AUTONOMY_DECISIONS_FILENAME = "autonomy_decisions.jsonl"
AUTONOMY_LOCK_FILENAME = ".autonomy.lock"

# These are the only reasons for which the runtime may transfer an unresolved
# decision to a person. Internal, reversible implementation choices are owned
# by the Supervisor and deliberately do not appear in this set.
HUMAN_DECISION_REASON_CODES = frozenset(
    {
        "spec_conflict",
        "external_value_choice",
        "irreversible_high_impact",
        "external_resource_required",
        "budget_extension_required",
    }
)


@dataclass(frozen=True)
class StageRecoveryDecision:
    action: str
    reason_code: str
    rationale: str
    human_required: bool = False
    resume_failed_worktree: bool = False
    artifact_format: str | None = None
    small_patch: bool = False
    additional_writable_paths: tuple[str, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)


def autonomy_timestamp() -> str:
    return (
        _datetime.datetime.now(_datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def autonomy_decisions_file_path(run_dir: Path) -> Path:
    return run_dir / AUTONOMY_DECISIONS_FILENAME


@contextmanager
def _autonomy_lock(run_dir: Path) -> Iterator[None]:
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / AUTONOMY_LOCK_FILENAME).open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def read_autonomy_decisions(run_dir: Path) -> list[dict[str, object]]:
    path = autonomy_decisions_file_path(run_dir)
    if not path.exists():
        return []
    records: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def requires_human_decision(reason_code: str) -> bool:
    return reason_code.strip().lower() in HUMAN_DECISION_REASON_CODES


def _append_autonomy_record(run_dir: Path, payload: Mapping[str, object]) -> dict[str, object]:
    with _autonomy_lock(run_dir):
        sequence = len(read_autonomy_decisions(run_dir)) + 1
        record = {
            "schema_version": 1,
            "sequence": sequence,
            "decision_id": f"AD{sequence:06d}",
            "timestamp": autonomy_timestamp(),
            **dict(payload),
        }
        with autonomy_decisions_file_path(run_dir).open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        return record


def record_autonomy_decision(
    run_dir: Path,
    *,
    scope: str,
    action: str,
    reason_code: str,
    rationale: str,
    evidence_paths: Sequence[str] = (),
    reversible: bool = True,
    metadata: Mapping[str, object] | None = None,
) -> dict[str, object]:
    normalized_reason = reason_code.strip().lower()
    if not normalized_reason:
        raise RunnerError("autonomy decision requires a reason_code")
    if requires_human_decision(normalized_reason):
        raise RunnerError(
            "a human-required reason cannot be recorded as an autonomous decision: "
            + normalized_reason
        )
    return _append_autonomy_record(
        run_dir,
        {
            "actor": "supervisor",
            "authority": "autonomous",
            "scope": scope,
            "action": action,
            "reason_code": normalized_reason,
            "rationale": rationale.strip(),
            "evidence_paths": [str(path) for path in evidence_paths if str(path)],
            "reversible": bool(reversible),
            "metadata": dict(metadata or {}),
        },
    )


def record_human_decision_request(
    run_dir: Path,
    *,
    scope: str,
    action: str,
    reason_code: str,
    rationale: str,
    evidence_paths: Sequence[str],
    required_human_input: str,
) -> dict[str, object]:
    normalized_reason = reason_code.strip().lower()
    if not requires_human_decision(normalized_reason):
        raise RunnerError(
            "human decision request is not authorized for internal reason: "
            + normalized_reason
        )
    if not required_human_input.strip():
        raise RunnerError("human decision request requires exact required_human_input")
    return _append_autonomy_record(
        run_dir,
        {
            "actor": "supervisor",
            "authority": "human_required",
            "scope": scope,
            "action": action,
            "reason_code": normalized_reason,
            "rationale": rationale.strip(),
            "evidence_paths": [str(path) for path in evidence_paths if str(path)],
            "required_human_input": required_human_input.strip(),
            "reversible": False,
            "metadata": {},
        },
    )


def record_external_intervention(
    run_dir: Path,
    *,
    action: str,
    reason_code: str,
    rationale: str,
    request_decision_id: str = "",
    evidence_paths: Sequence[str] = (),
) -> dict[str, object]:
    normalized_reason = reason_code.strip().lower()
    authorized = bool(request_decision_id) and requires_human_decision(normalized_reason)
    return _append_autonomy_record(
        run_dir,
        {
            "actor": "external",
            "authority": "human_required" if authorized else "unauthorized_external",
            "scope": "goal",
            "action": action,
            "reason_code": normalized_reason,
            "rationale": rationale.strip(),
            "evidence_paths": [str(path) for path in evidence_paths if str(path)],
            "request_decision_id": request_decision_id or None,
            "reversible": False,
            "metadata": {},
        },
    )


def autonomy_audit(run_dir: Path) -> dict[str, object]:
    records = read_autonomy_decisions(run_dir)
    autonomous = [item for item in records if item.get("authority") == "autonomous"]
    human_requests = [
        item
        for item in records
        if item.get("actor") == "supervisor" and item.get("authority") == "human_required"
    ]
    external = [item for item in records if item.get("actor") == "external"]
    unauthorized = [
        item for item in external if item.get("authority") != "human_required"
    ]
    return {
        "decision_log": str(autonomy_decisions_file_path(run_dir)),
        "decision_count": len(records),
        "autonomous_decision_count": len(autonomous),
        "human_decision_request_count": len(human_requests),
        "external_intervention_count": len(external),
        "unauthorized_external_intervention_count": len(unauthorized),
        "zero_unauthorized_external_interventions": not unauthorized,
        "unauthorized_external_interventions": unauthorized,
    }


def actionable_blocked_state(
    *,
    reason_code: str,
    summary: str,
    evidence_paths: Sequence[str],
    required_human_input: str,
) -> dict[str, object]:
    normalized_reason = reason_code.strip().lower()
    evidence = [str(path) for path in evidence_paths if str(path)]
    if not requires_human_decision(normalized_reason):
        raise RunnerError(
            "BLOCKED is reserved for an authorized human decision boundary: "
            + normalized_reason
        )
    if not summary.strip() or not evidence or not required_human_input.strip():
        raise RunnerError(
            "BLOCKED requires a reason summary, supporting evidence, and required human input"
        )
    return {
        "status": "BLOCKED",
        "blocked_reason": {"code": normalized_reason, "summary": summary.strip()},
        "supporting_evidence": evidence,
        "required_human_input": required_human_input.strip(),
    }


def evaluate_completion_gate(
    acceptance_matrix: Sequence[Mapping[str, object]],
    *,
    pending_safety: Sequence[Mapping[str, object]] = (),
    blocked_safety: Sequence[Mapping[str, object]] = (),
    budget_stop: Mapping[str, object] | None = None,
    stall: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Return the only goal-level status admitted by current evidence.

    Unlike the stage-local legacy gate, every declared acceptance item is a
    blocker until it is directly or externally verified as ``pass``.
    """
    blockers = [dict(item) for item in acceptance_matrix if item.get("status") != "pass"]
    if blocked_safety:
        status = "safety_blocked"
    elif pending_safety:
        status = "approval_required"
    elif budget_stop:
        status = "budget_exhausted"
    elif stall:
        status = "stalled"
    elif blockers:
        status = "acceptance_failed"
    else:
        status = "approved"
    return {
        "status": status,
        "completed": status == "approved",
        "blockers": blockers,
        "acceptance_count": len(acceptance_matrix),
        "acceptance_pass_count": len(acceptance_matrix) - len(blockers),
    }


def decide_stage_recovery(
    stage: StageWorkItem,
    summary: StageRunSummary,
    *,
    recovery_count: int,
    max_recoveries: int,
    previous_actions: Sequence[str] = (),
) -> StageRecoveryDecision:
    """Choose a reversible internal recovery without delegating it to a user."""
    if recovery_count >= max_recoveries:
        return StageRecoveryDecision(
            action="fail_closed",
            reason_code="autonomous_recovery_exhausted",
            rationale="The bounded recovery budget is exhausted; no unproven retry is admitted.",
        )

    failure = summary.failure_summary or {}
    failure_type = str(failure.get("failure_type") or "unknown")
    prior = set(previous_actions)
    writable = stage.writable_paths or stage.suggested_paths
    repair_scope = stage.repair_scope_paths or writable
    evidence_expansion = tuple(
        path
        for path in summary.repair_focus_paths
        if path in repair_scope and path not in writable
    )

    if failure_type == "runner_configuration_error":
        return StageRecoveryDecision(
            action="fail_closed",
            reason_code="runner_configuration_error",
            rationale=(
                "The child runner rejected its executable configuration. Product-code retries "
                "cannot repair the harness contract, so the parent records evidence and stops."
            ),
            metadata={"failure_type": failure_type},
        )

    if failure_type == "llm_generation_timeout":
        observed_timeout = float(failure.get("timeout_seconds") or 0.0)
        if failure.get("api_health") == "alive" and observed_timeout < 1800.0:
            next_timeout = min(max(observed_timeout * 2.0, 600.0), 1800.0)
            return StageRecoveryDecision(
                action="extend_llm_timeout",
                reason_code="live_api_generation_timeout",
                rationale=(
                    "The generation exceeded its request window while the model API remained "
                    "healthy. Resume the same bounded stage with a larger request window."
                ),
                resume_failed_worktree=True,
                metadata={
                    "failure_type": failure_type,
                    "api_health": "alive",
                    "observed_timeout_seconds": observed_timeout,
                    "timeout_seconds": next_timeout,
                },
            )
        return StageRecoveryDecision(
            action="fail_closed",
            reason_code="generation_timeout_exhausted",
            rationale=(
                "The live model API still exceeded the maximum bounded generation window; "
                "another identical wait is not admitted."
            ),
            metadata={"failure_type": failure_type, "timeout_seconds": observed_timeout},
        )

    if evidence_expansion and "expand_repair_scope" not in prior:
        return StageRecoveryDecision(
            action="expand_repair_scope",
            reason_code="evidence_dependency_closure",
            rationale=(
                "Executable failure evidence identifies a coupled path inside the parent "
                "stage authorization. The next attempt may add only that proven dependency."
            ),
            resume_failed_worktree=True,
            small_patch=True,
            additional_writable_paths=evidence_expansion,
            metadata={
                "failure_type": failure_type,
                "active_writable_paths": list(writable),
                "repair_scope_paths": list(repair_scope),
            },
        )

    if is_protocol_failure_type(failure_type) and "format_repair" not in prior:
        return StageRecoveryDecision(
            action="format_repair",
            reason_code="artifact_format_repair",
            rationale=(
                "The observed failure is in the artifact transport contract, so the next "
                "attempt is restricted to one concise legacy artifact."
            ),
            resume_failed_worktree=True,
            artifact_format="legacy",
            small_patch=True,
            metadata={"failure_type": failure_type},
        )

    if len(writable) > 1 and "split_stage" not in prior:
        return StageRecoveryDecision(
            action="split_stage",
            reason_code="stage_split",
            rationale=(
                "The failed stage has multiple writable paths; smaller path groups provide "
                "a reversible, independently verifiable next action."
            ),
            metadata={"failure_type": failure_type, "writable_path_count": len(writable)},
        )

    if "root_cause_recovery" in prior:
        return StageRecoveryDecision(
            action="fail_closed",
            reason_code="no_novel_recovery",
            rationale=(
                "The same structural recovery has already been attempted without new evidence, "
                "scope, or artifact policy; replaying it is not an admissible action."
            ),
            metadata={"failure_type": failure_type},
        )

    return StageRecoveryDecision(
        action="root_cause_recovery",
        reason_code="failure_analysis",
        rationale=(
            "The stage cannot be reduced further, so the next attempt must use the prior "
            "executable failure as root-cause evidence instead of repeating an ordinary retry."
        ),
        resume_failed_worktree=True,
        artifact_format="legacy" if is_protocol_failure_type(failure_type) else None,
        small_patch=True,
        metadata={"failure_type": failure_type},
    )


def decide_final_integration_recovery(
    summary: StageRunSummary,
    *,
    recovery_count: int,
    max_recoveries: int,
    previous_actions: Sequence[str] = (),
) -> StageRecoveryDecision:
    """Choose a bounded S99 recovery without splitting integration ownership."""
    if recovery_count >= max_recoveries:
        return StageRecoveryDecision(
            action="fail_closed",
            reason_code="autonomous_recovery_exhausted",
            rationale="The bounded final-integration recovery budget is exhausted.",
        )

    failure_type = str((summary.failure_summary or {}).get("failure_type") or "unknown")
    prior = set(previous_actions)
    if failure_type == "runner_configuration_error":
        return StageRecoveryDecision(
            action="fail_closed",
            reason_code="runner_configuration_error",
            rationale="Product repair cannot correct a rejected runner configuration.",
            metadata={"failure_type": failure_type},
        )
    if failure_type == "llm_generation_timeout":
        observed_timeout = float((summary.failure_summary or {}).get("timeout_seconds") or 0.0)
        if observed_timeout < 1800.0:
            return StageRecoveryDecision(
                action="extend_llm_timeout",
                reason_code="live_api_generation_timeout",
                rationale="Resume final integration with a larger bounded LLM request window.",
                resume_failed_worktree=True,
                metadata={
                    "failure_type": failure_type,
                    "timeout_seconds": min(max(observed_timeout * 2.0, 600.0), 1800.0),
                },
            )
        return StageRecoveryDecision(
            action="fail_closed",
            reason_code="generation_timeout_exhausted",
            rationale="Final integration exhausted the bounded LLM generation window.",
            metadata={"failure_type": failure_type, "timeout_seconds": observed_timeout},
        )
    if is_protocol_failure_type(failure_type) and "format_repair" not in prior:
        return StageRecoveryDecision(
            action="format_repair",
            reason_code="artifact_format_repair",
            rationale="Resume final integration with prior evidence and a restricted artifact format.",
            resume_failed_worktree=True,
            artifact_format="legacy",
            small_patch=True,
            metadata={"failure_type": failure_type},
        )
    if "root_cause_recovery" in prior:
        return StageRecoveryDecision(
            action="fail_closed",
            reason_code="no_novel_recovery",
            rationale="Final integration already attempted root-cause recovery without new evidence.",
            metadata={"failure_type": failure_type},
        )
    return StageRecoveryDecision(
        action="root_cause_recovery",
        reason_code="final_integration_failure_analysis",
        rationale=(
            "Resume the failed final-integration worktree and use its executable evidence "
            "for one independently bounded root-cause attempt."
        ),
        resume_failed_worktree=True,
        small_patch=True,
        metadata={"failure_type": failure_type},
    )


def stage_recovery_decision_manifest(decision: StageRecoveryDecision) -> dict[str, object]:
    return asdict(decision)
