"""Durable cancellation and resource budgets for learner work."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import sqlite3
import time
from typing import Callable

from sdlc_events import canonical_json, event_timestamp, stable_identifier

from .schema_validation import require_identifier, require_slug
from .work_control_store import (
    append_event,
    connect,
    database_path,
    initialize,
    request_learning_cancel,
)


ACTIVE_STATUSES = {"running", "cancel_requested"}


class LearningWorkStopped(RuntimeError):
    """Raised before new learner work when cancellation or a budget wins."""

    def __init__(self, operation_id: str, reason_code: str) -> None:
        self.operation_id = operation_id
        self.reason_code = reason_code
        super().__init__(f"learning work stopped: {reason_code} ({operation_id})")


@dataclass(frozen=True)
class LearningLimits:
    max_api_calls: int = 30
    max_cases: int = 1000
    max_tokens: int = 262_144
    max_wall_seconds: float = 3600.0

    def __post_init__(self) -> None:
        for field in ("max_api_calls", "max_cases", "max_tokens"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field} must be a non-negative integer")
        if isinstance(self.max_wall_seconds, bool) or float(self.max_wall_seconds) <= 0:
            raise ValueError("max_wall_seconds must be positive")

    def to_dict(self) -> dict[str, object]:
        return {
            "max_api_calls": self.max_api_calls,
            "max_cases": self.max_cases,
            "max_tokens": self.max_tokens,
            "max_wall_seconds": float(self.max_wall_seconds),
        }


class LearningWorkControl:
    """Serialize cancel, budget consumption, and work-start checkpoints."""

    def __init__(
        self,
        data_dir: Path | None,
        operation_kind: str,
        *,
        limits: LearningLimits | None = None,
        operation_id: str = "",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = database_path(data_dir)
        initialize(self.path)
        self.operation_kind = require_slug(operation_kind, "operation_kind")
        self.limits = limits or LearningLimits()
        self._clock = clock
        now = float(clock())
        generated = stable_identifier(
            "LW",
            self.operation_kind,
            time.time_ns(),
            os.getpid(),
        )
        self.operation_id = require_identifier(
            operation_id or generated,
            "operation_id",
        )
        with connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO learner_operations("
                    "operation_id, operation_kind, status, started_at, updated_at, "
                    "limits_json) VALUES (?, ?, 'running', ?, ?, ?)",
                    (
                        self.operation_id,
                        self.operation_kind,
                        now,
                        event_timestamp(),
                        canonical_json(self.limits.to_dict()),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(
                    f"learning operation already exists: {self.operation_id}"
                ) from exc
            row = connection.execute(
                "SELECT * FROM learner_operations WHERE operation_id = ?",
                (self.operation_id,),
            ).fetchone()
            append_event(connection, self.operation_id, "started", row)

    def _stop_locked(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        reason_code: str,
    ) -> None:
        reason = require_slug(reason_code, "reason_code")
        connection.execute(
            "UPDATE learner_operations SET status = 'stopped', reason_code = ?, "
            "ended_at = ?, updated_at = ? WHERE operation_id = ?",
            (reason, float(self._clock()), event_timestamp(), self.operation_id),
        )
        updated = connection.execute(
            "SELECT * FROM learner_operations WHERE operation_id = ?",
            (self.operation_id,),
        ).fetchone()
        append_event(
            connection,
            self.operation_id,
            "stopped",
            updated or row,
            reason_code=reason,
        )

    def checkpoint(
        self,
        checkpoint: str,
        *,
        api_calls: int = 0,
        cases: int = 0,
        tokens: int = 0,
    ) -> None:
        name = require_slug(checkpoint, "checkpoint")
        deltas = {"api_calls": api_calls, "case_count": cases, "reserved_tokens": tokens}
        for field, value in deltas.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field} delta must be a non-negative integer")
        with connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM learner_operations WHERE operation_id = ?",
                (self.operation_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"learning operation not found: {self.operation_id}")
            status = str(row["status"])
            if status != "running":
                reason = str(row["reason_code"] or status)
                if status == "cancel_requested":
                    reason = reason or "cancel_requested"
                    self._stop_locked(connection, row, reason)
                    connection.commit()
                raise LearningWorkStopped(self.operation_id, reason)

            reason = ""
            if float(self._clock()) - float(row["started_at"]) > self.limits.max_wall_seconds:
                reason = "wall_clock_budget_exhausted"
            ceilings = (
                ("api_call_budget_exhausted", "api_calls", self.limits.max_api_calls),
                ("case_budget_exhausted", "case_count", self.limits.max_cases),
                ("token_budget_exhausted", "reserved_tokens", self.limits.max_tokens),
            )
            for code, field, maximum in ceilings:
                if int(row[field]) + int(deltas[field]) > maximum:
                    reason = reason or code
            if reason:
                self._stop_locked(connection, row, reason)
                connection.commit()
                raise LearningWorkStopped(self.operation_id, reason)

            connection.execute(
                "UPDATE learner_operations SET api_calls = api_calls + ?, "
                "case_count = case_count + ?, reserved_tokens = reserved_tokens + ?, "
                "updated_at = ? WHERE operation_id = ?",
                (api_calls, cases, tokens, event_timestamp(), self.operation_id),
            )
            updated = connection.execute(
                "SELECT * FROM learner_operations WHERE operation_id = ?",
                (self.operation_id,),
            ).fetchone()
            append_event(
                connection,
                self.operation_id,
                "checkpoint",
                updated,
                checkpoint=name,
            )

    def stop(self, reason_code: str) -> None:
        with connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM learner_operations WHERE operation_id = ?",
                (self.operation_id,),
            ).fetchone()
            if row is not None and str(row["status"]) in ACTIVE_STATUSES:
                self._stop_locked(connection, row, reason_code)

    def complete(self) -> None:
        self.checkpoint("completion")
        with connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM learner_operations WHERE operation_id = ?",
                (self.operation_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"learning operation not found: {self.operation_id}")
            if str(row["status"]) != "running":
                reason = str(row["reason_code"] or row["status"])
                if str(row["status"]) == "cancel_requested":
                    self._stop_locked(connection, row, reason)
                    connection.commit()
                raise LearningWorkStopped(self.operation_id, reason)
            connection.execute(
                "UPDATE learner_operations SET status = 'completed', ended_at = ?, "
                "updated_at = ? WHERE operation_id = ?",
                (float(self._clock()), event_timestamp(), self.operation_id),
            )
            row = connection.execute(
                "SELECT * FROM learner_operations WHERE operation_id = ?",
                (self.operation_id,),
            ).fetchone()
            append_event(connection, self.operation_id, "completed", row)

    def report(self) -> dict[str, object]:
        with connect(self.path) as connection:
            row = connection.execute(
                "SELECT * FROM learner_operations WHERE operation_id = ?",
                (self.operation_id,),
            ).fetchone()
            count = connection.execute(
                "SELECT COUNT(*) AS value FROM learner_control_events "
                "WHERE operation_id = ?",
                (self.operation_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"learning operation not found: {self.operation_id}")
        ended_at = float(row["ended_at"] or 0) or float(self._clock())
        return {
            "operation_id": self.operation_id,
            "operation_kind": str(row["operation_kind"]),
            "status": str(row["status"]),
            "reason_code": str(row["reason_code"]),
            "api_calls": int(row["api_calls"]),
            "case_count": int(row["case_count"]),
            "reserved_tokens": int(row["reserved_tokens"]),
            "elapsed_seconds": max(0.0, ended_at - float(row["started_at"])),
            "limits": self.limits.to_dict(),
            "event_count": int(count["value"] or 0),
        }

__all__ = [
    "LearningLimits",
    "LearningWorkControl",
    "LearningWorkStopped",
    "request_learning_cancel",
]
