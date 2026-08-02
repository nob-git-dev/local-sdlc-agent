"""Immutable, content-addressed active-knowledge snapshots."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Sequence

from sdlc_events import canonical_json, event_timestamp, stable_identifier

from .candidate_store import KNOWLEDGE_DB_FILENAME
from .knowledge_schema import KnowledgeItem
from .privacy import sensitive_values
from .schema_validation import require_identifier
from .storage import learning_data_dir


def _active_document(item: KnowledgeItem) -> dict[str, object]:
    payload = item.to_dict()
    payload["state"] = "active"
    return KnowledgeItem.from_dict(payload).to_dict()


class SnapshotStore:
    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = learning_data_dir(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / KNOWLEDGE_DB_FILENAME
        self.snapshots_dir = self.data_dir / "snapshots"
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
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
                CREATE TABLE IF NOT EXISTS knowledge_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    snapshot_hash TEXT NOT NULL,
                    active_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL
                );
                """
            )

    def snapshot_path(self, snapshot_id: str) -> Path:
        identifier = require_identifier(snapshot_id, "snapshot_id")
        return self.snapshots_dir / f"{identifier}.json"

    def put_snapshot(self, items: Sequence[KnowledgeItem]) -> dict[str, object]:
        active_items = sorted(
            (_active_document(item) for item in items),
            key=lambda item: (str(item["knowledge_id"]), int(item["version"])),
        )
        core = {"schema_version": 1, "active_items": active_items}
        unsafe = sensitive_values(core)
        if unsafe:
            raise ValueError("snapshot contains sensitive values: " + ", ".join(unsafe))
        snapshot_hash = hashlib.sha256(
            canonical_json(core).encode("utf-8")
        ).hexdigest()
        snapshot_id = stable_identifier("KS", snapshot_hash)
        existing = self.get_snapshot(snapshot_id, required=False)
        if existing is not None:
            return existing
        record = {
            **core,
            "snapshot_id": snapshot_id,
            "snapshot_hash": snapshot_hash,
            "created_at": event_timestamp(),
        }
        serialized = canonical_json(record)
        target = self.snapshot_path(snapshot_id)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        temporary.write_text(serialized + "\n", encoding="utf-8")
        os.replace(temporary, target)
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO knowledge_snapshots("
                "snapshot_id, snapshot_hash, active_count, created_at, snapshot_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    snapshot_id,
                    snapshot_hash,
                    len(active_items),
                    record["created_at"],
                    serialized,
                ),
            )
        return self.get_snapshot(snapshot_id)

    def get_snapshot(
        self,
        snapshot_id: str,
        *,
        required: bool = True,
    ) -> dict[str, object] | None:
        identifier = require_identifier(snapshot_id, "snapshot_id")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT snapshot_json FROM knowledge_snapshots WHERE snapshot_id = ?",
                (identifier,),
            ).fetchone()
        if row is None:
            if required:
                raise KeyError(f"snapshot not found: {identifier}")
            return None
        record = json.loads(str(row["snapshot_json"]))
        core = {
            "schema_version": record.get("schema_version"),
            "active_items": record.get("active_items"),
        }
        calculated = hashlib.sha256(
            canonical_json(core).encode("utf-8")
        ).hexdigest()
        if calculated != record.get("snapshot_hash"):
            raise ValueError(f"snapshot hash mismatch: {identifier}")
        if stable_identifier("KS", calculated) != identifier:
            raise ValueError(f"snapshot identity mismatch: {identifier}")
        file_path = self.snapshot_path(identifier)
        if (
            not file_path.is_file()
            or file_path.read_text(encoding="utf-8").strip()
            != str(row["snapshot_json"])
        ):
            raise ValueError(f"snapshot file mismatch: {identifier}")
        return record

    def snapshots(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT snapshot_json FROM knowledge_snapshots ORDER BY rowid"
            ).fetchall()
        return [self.get_snapshot(json.loads(str(row["snapshot_json"]))["snapshot_id"]) for row in rows]


__all__ = ["SnapshotStore"]
