"""Candidate-only knowledge persistence; activation is intentionally absent."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Sequence

from sdlc_events import canonical_json, event_timestamp, stable_identifier

from .knowledge_schema import KnowledgeItem
from .privacy import sensitive_values
from .schema_validation import require_identifier, require_slug
from .storage import learning_data_dir


KNOWLEDGE_DB_FILENAME = "knowledge.sqlite3"


def _hashes(values: Sequence[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        digest = str(value).lower()
        if len(digest) != 64:
            raise ValueError("response hash must contain 64 hexadecimal characters")
        try:
            int(digest, 16)
        except ValueError as exc:
            raise ValueError("response hash is not hexadecimal") from exc
        normalized.append(digest)
    return tuple(normalized)


class CandidateStore:
    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = learning_data_dir(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / KNOWLEDGE_DB_FILENAME
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
                CREATE TABLE IF NOT EXISTS knowledge_candidates (
                    knowledge_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    candidate_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    knowledge_json TEXT NOT NULL,
                    source_episode_ids_json TEXT NOT NULL,
                    response_hashes_json TEXT NOT NULL,
                    PRIMARY KEY (knowledge_id, version)
                );
                CREATE TABLE IF NOT EXISTS candidate_mining_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    source_episode_ids_json TEXT NOT NULL,
                    response_hashes_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def put_candidate(
        self,
        item: KnowledgeItem,
        source_episode_ids: Sequence[str],
        response_hashes: Sequence[str],
    ) -> bool:
        if item.state != "candidate":
            raise ValueError("candidate store requires candidate state")
        if item.authority != "llm_hypothesis":
            raise ValueError("candidate store requires llm_hypothesis authority")
        if item.created_by != "llm-assisted":
            raise ValueError("candidate store requires llm-assisted creator")
        source_ids = tuple(sorted(require_identifier(value, "source_episode_id") for value in source_episode_ids))
        if not source_ids:
            raise ValueError("candidate store requires source episodes")
        evidence_episode_ids = tuple(
            sorted(anchor.episode_id for anchor in item.evidence_refs if anchor.episode_id)
        )
        if evidence_episode_ids != source_ids:
            raise ValueError("candidate evidence must be derived from source episodes")
        hashes = _hashes(response_hashes)
        payload = item.to_dict()
        unsafe = sensitive_values(payload)
        if unsafe:
            raise ValueError("candidate contains sensitive values: " + ", ".join(unsafe))
        serialized = canonical_json(payload)
        candidate_hash = stable_identifier("KC", serialized, length=64).removeprefix("KC-")
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO knowledge_candidates("
                "knowledge_id, version, candidate_hash, created_at, knowledge_json, "
                "source_episode_ids_json, response_hashes_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    item.knowledge_id,
                    item.version,
                    candidate_hash,
                    event_timestamp(),
                    serialized,
                    canonical_json(source_ids),
                    canonical_json(hashes),
                ),
            )
            return cursor.rowcount == 1

    def record_attempt(
        self,
        *,
        batch_id: str,
        status: str,
        reason_code: str,
        source_episode_ids: Sequence[str],
        response_hashes: Sequence[str],
    ) -> bool:
        batch = require_identifier(batch_id, "batch_id")
        normalized_status = require_slug(status, "attempt status")
        if normalized_status not in {"accepted", "duplicate", "rejected"}:
            raise ValueError("invalid candidate attempt status")
        reason = require_slug(reason_code, "reason_code")
        source_ids = tuple(sorted(require_identifier(value, "source_episode_id") for value in source_episode_ids))
        hashes = _hashes(response_hashes)
        attempt_id = stable_identifier(
            "CA",
            batch,
            normalized_status,
            reason,
            source_ids,
            hashes,
        )
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO candidate_mining_attempts("
                "attempt_id, batch_id, status, reason_code, source_episode_ids_json, "
                "response_hashes_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    attempt_id,
                    batch,
                    normalized_status,
                    reason,
                    canonical_json(source_ids),
                    canonical_json(hashes),
                    event_timestamp(),
                ),
            )
            return cursor.rowcount == 1

    def candidate_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS value FROM knowledge_candidates").fetchone()
        return int(row["value"] or 0)

    def candidates(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT knowledge_json FROM knowledge_candidates ORDER BY rowid"
            ).fetchall()
        return [json.loads(str(row["knowledge_json"])) for row in rows]

    def attempts(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT attempt_id, batch_id, status, reason_code, "
                "source_episode_ids_json, response_hashes_json "
                "FROM candidate_mining_attempts ORDER BY rowid"
            ).fetchall()
        return [
            {
                "attempt_id": str(row["attempt_id"]),
                "batch_id": str(row["batch_id"]),
                "status": str(row["status"]),
                "reason_code": str(row["reason_code"]),
                "source_episode_ids": json.loads(str(row["source_episode_ids_json"])),
                "response_hashes": json.loads(str(row["response_hashes_json"])),
            }
            for row in rows
        ]
