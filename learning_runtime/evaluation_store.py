"""Immutable, hash-addressed validation report persistence."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Mapping

from sdlc_events import canonical_json, event_timestamp, stable_identifier

from .candidate_store import KNOWLEDGE_DB_FILENAME
from .privacy import sensitive_values
from .schema_validation import require_identifier
from .storage import learning_data_dir


class EvaluationStore:
    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = learning_data_dir(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / KNOWLEDGE_DB_FILENAME
        self.evaluations_dir = self.data_dir / "evaluations"
        self.evaluations_dir.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS validation_reports (
                    evaluation_id TEXT PRIMARY KEY,
                    knowledge_id TEXT NOT NULL,
                    knowledge_version INTEGER NOT NULL,
                    verdict TEXT NOT NULL,
                    report_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    report_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_validation_candidate
                    ON validation_reports(knowledge_id, knowledge_version);
                """
            )

    def report_path(self, evaluation_id: str) -> Path:
        return self.evaluations_dir / f"{require_identifier(evaluation_id, 'evaluation_id')}.json"

    def put_report(self, core: Mapping[str, object]) -> dict[str, object]:
        unsafe = sensitive_values(core)
        if unsafe:
            raise ValueError("evaluation contains sensitive values: " + ", ".join(unsafe))
        identity = canonical_json(core)
        evaluation_id = stable_identifier("EVAL", identity)
        existing = self.get_report(evaluation_id)
        if existing is not None:
            return existing
        report = {
            "schema_version": 1,
            "evaluation_id": evaluation_id,
            "created_at": event_timestamp(),
            **dict(core),
        }
        hash_payload = canonical_json(report)
        report["report_hash"] = hashlib.sha256(hash_payload.encode("utf-8")).hexdigest()
        serialized = canonical_json(report)
        target = self.report_path(evaluation_id)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        temporary.write_text(serialized + "\n", encoding="utf-8")
        os.replace(temporary, target)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO validation_reports("
                "evaluation_id, knowledge_id, knowledge_version, verdict, report_hash, "
                "created_at, report_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    evaluation_id,
                    str(report["knowledge_id"]),
                    int(report["knowledge_version"]),
                    str(report["verdict"]),
                    str(report["report_hash"]),
                    str(report["created_at"]),
                    serialized,
                ),
            )
        return report

    def get_report(self, evaluation_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT report_json FROM validation_reports WHERE evaluation_id = ?",
                (require_identifier(evaluation_id, "evaluation_id"),),
            ).fetchone()
        return json.loads(str(row["report_json"])) if row else None

    def reports(self, knowledge_id: str = "") -> list[dict[str, object]]:
        query = "SELECT report_json FROM validation_reports"
        parameters: tuple[object, ...] = ()
        if knowledge_id:
            query += " WHERE knowledge_id = ?"
            parameters = (require_identifier(knowledge_id, "knowledge_id"),)
        query += " ORDER BY rowid"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [json.loads(str(row["report_json"])) for row in rows]

    def latest_pass(
        self,
        knowledge_id: str,
        version: int,
    ) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT report_json FROM validation_reports "
                "WHERE knowledge_id = ? AND knowledge_version = ? "
                "AND verdict = 'shadow_pass' ORDER BY rowid DESC LIMIT 1",
                (require_identifier(knowledge_id, "knowledge_id"), int(version)),
            ).fetchone()
        return json.loads(str(row["report_json"])) if row else None

    def report_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS value FROM validation_reports"
            ).fetchone()
        return int(row["value"] or 0)
