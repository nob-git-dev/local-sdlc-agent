"""SQLite persistence helpers for learner cancellation and budgets."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from sdlc_events import event_timestamp

from .schema_validation import require_identifier, require_slug
from .storage import learning_data_dir


CONTROL_DB_FILENAME = "learning-control.sqlite3"


def database_path(data_dir: Path | None) -> Path:
    root = learning_data_dir(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root / CONTROL_DB_FILENAME


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = DELETE")
    connection.execute("PRAGMA synchronous = FULL")
    return connection


def initialize(path: Path) -> None:
    with connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS learner_operations (
                operation_id TEXT PRIMARY KEY,
                operation_kind TEXT NOT NULL,
                status TEXT NOT NULL,
                reason_code TEXT NOT NULL DEFAULT '',
                started_at REAL NOT NULL,
                ended_at REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                limits_json TEXT NOT NULL,
                api_calls INTEGER NOT NULL DEFAULT 0,
                case_count INTEGER NOT NULL DEFAULT 0,
                reserved_tokens INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS learner_control_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                reason_code TEXT NOT NULL DEFAULT '',
                checkpoint TEXT NOT NULL DEFAULT '',
                api_calls INTEGER NOT NULL,
                case_count INTEGER NOT NULL,
                reserved_tokens INTEGER NOT NULL,
                occurred_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_learner_control_operation
                ON learner_control_events(operation_id, sequence);
            """
        )


def append_event(
    connection: sqlite3.Connection,
    operation_id: str,
    event_type: str,
    row: sqlite3.Row,
    *,
    reason_code: str = "",
    checkpoint: str = "",
) -> None:
    connection.execute(
        "INSERT INTO learner_control_events("
        "operation_id, event_type, reason_code, checkpoint, api_calls, "
        "case_count, reserved_tokens, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            operation_id,
            event_type,
            reason_code,
            checkpoint,
            int(row["api_calls"]),
            int(row["case_count"]),
            int(row["reserved_tokens"]),
            event_timestamp(),
        ),
    )


def request_learning_cancel(
    data_dir: Path | None,
    *,
    operation_id: str = "",
    reason_code: str = "user_requested",
) -> dict[str, object]:
    path = database_path(data_dir)
    initialize(path)
    reason = require_slug(reason_code, "reason_code")
    identifier = require_identifier(operation_id, "operation_id") if operation_id else ""
    with connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        query = "SELECT * FROM learner_operations WHERE status = 'running'"
        parameters: tuple[object, ...] = ()
        if identifier:
            query += " AND operation_id = ?"
            parameters = (identifier,)
        rows = connection.execute(query, parameters).fetchall()
        for row in rows:
            target = str(row["operation_id"])
            connection.execute(
                "UPDATE learner_operations SET status = 'cancel_requested', "
                "reason_code = ?, updated_at = ? WHERE operation_id = ?",
                (reason, event_timestamp(), target),
            )
            updated = connection.execute(
                "SELECT * FROM learner_operations WHERE operation_id = ?",
                (target,),
            ).fetchone()
            append_event(
                connection,
                target,
                "cancel_requested",
                updated,
                reason_code=reason,
            )
    return {
        "status": "cancel_requested" if rows else "no_running_operation",
        "reason_code": reason,
        "operation_ids": [str(row["operation_id"]) for row in rows],
    }


def learning_work_status(
    data_dir: Path | None,
    *,
    operation_id: str = "",
) -> dict[str, object]:
    path = database_path(data_dir)
    initialize(path)
    identifier = require_identifier(operation_id, "operation_id") if operation_id else ""
    query = "SELECT * FROM learner_operations"
    parameters: tuple[object, ...] = ()
    if identifier:
        query += " WHERE operation_id = ?"
        parameters = (identifier,)
    query += " ORDER BY started_at DESC LIMIT 100"
    with connect(path) as connection:
        rows = connection.execute(query, parameters).fetchall()
        event_counts = {
            str(row["operation_id"]): int(row["value"])
            for row in connection.execute(
                "SELECT operation_id, COUNT(*) AS value "
                "FROM learner_control_events GROUP BY operation_id"
            ).fetchall()
        }
        total = int(
            connection.execute(
                "SELECT COUNT(*) AS value FROM learner_operations"
            ).fetchone()["value"]
        )
        summary_rows = connection.execute(
            "SELECT status, COUNT(*) AS value FROM learner_operations GROUP BY status"
        ).fetchall()
    if identifier and not rows:
        raise KeyError(f"learning operation not found: {identifier}")
    status_counts = (
        {str(row["status"]): 1 for row in rows}
        if identifier
        else {str(row["status"]): int(row["value"]) for row in summary_rows}
    )
    return {
        "status": "pass",
        "operation_count": total if not identifier else len(rows),
        "returned_count": len(rows),
        "active_count": sum(
            count
            for status, count in status_counts.items()
            if status in {"running", "cancel_requested"}
        ),
        "status_counts": dict(sorted(status_counts.items())),
        "operations": [
            {
                "operation_id": str(row["operation_id"]),
                "operation_kind": str(row["operation_kind"]),
                "status": str(row["status"]),
                "reason_code": str(row["reason_code"]),
                "updated_at": str(row["updated_at"]),
                "api_calls": int(row["api_calls"]),
                "case_count": int(row["case_count"]),
                "reserved_tokens": int(row["reserved_tokens"]),
                "limits": json.loads(str(row["limits_json"])),
                "event_count": event_counts.get(str(row["operation_id"]), 0),
            }
            for row in rows
        ],
    }


__all__ = [
    "append_event",
    "connect",
    "database_path",
    "initialize",
    "learning_work_status",
    "request_learning_cancel",
]
