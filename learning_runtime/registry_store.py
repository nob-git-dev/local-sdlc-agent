"""Append-only, hash-chained lifecycle registry for learned knowledge."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Mapping

from sdlc_events import canonical_json, event_timestamp, stable_identifier

from .candidate_store import KNOWLEDGE_DB_FILENAME
from .knowledge_schema import KnowledgeItem
from .privacy import sensitive_values
from .schema_validation import require_identifier, require_slug
from .storage import learning_data_dir


REGISTRY_EVENT_TYPES = {
    "shadow_validated",
    "knowledge_promoted",
    "knowledge_challenged",
    "knowledge_retired",
    "knowledge_superseded",
    "snapshot_published",
    "snapshot_rolled_back",
}


class RegistryStore:
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
                CREATE TABLE IF NOT EXISTS registry_events (
                    sequence INTEGER PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    knowledge_id TEXT NOT NULL DEFAULT '',
                    knowledge_version INTEGER NOT NULL DEFAULT 0,
                    snapshot_id TEXT NOT NULL DEFAULT '',
                    previous_hash TEXT NOT NULL DEFAULT '',
                    event_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_registry_knowledge
                    ON registry_events(knowledge_id, knowledge_version);
                """
            )

    def append_event(
        self,
        event_type: str,
        *,
        knowledge_id: str = "",
        version: int = 0,
        snapshot_id: str = "",
        payload: Mapping[str, object] | None = None,
        idempotency_key: str = "",
    ) -> dict[str, object]:
        kind = require_slug(event_type, "registry event_type")
        if kind not in REGISTRY_EVENT_TYPES:
            raise ValueError(f"unsupported registry event_type: {kind}")
        identifier = (
            require_identifier(knowledge_id, "knowledge_id") if knowledge_id else ""
        )
        snapshot = (
            require_identifier(snapshot_id, "snapshot_id") if snapshot_id else ""
        )
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            raise ValueError("registry knowledge version must be non-negative")
        body = dict(payload or {})
        unsafe = sensitive_values(body)
        if unsafe:
            raise ValueError("registry event contains sensitive values: " + ", ".join(unsafe))
        identity = idempotency_key or canonical_json(body)
        event_id = stable_identifier(
            "RE",
            kind,
            identifier,
            version,
            snapshot,
            identity,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT record_json FROM registry_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if existing is not None:
                record = json.loads(str(existing["record_json"]))
                expected = (kind, identifier, version, snapshot, body)
                actual = (
                    record.get("event_type"),
                    record.get("knowledge_id"),
                    record.get("knowledge_version"),
                    record.get("snapshot_id"),
                    record.get("payload"),
                )
                if actual != expected:
                    connection.rollback()
                    raise ValueError(
                        f"registry idempotency conflict: {event_id}"
                    )
                connection.rollback()
                return record
            prior = connection.execute(
                "SELECT sequence, event_hash FROM registry_events "
                "ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            sequence = int(prior["sequence"] if prior else 0) + 1
            previous_hash = str(prior["event_hash"] if prior else "")
            core = {
                "schema_version": 1,
                "sequence": sequence,
                "event_id": event_id,
                "event_type": kind,
                "knowledge_id": identifier,
                "knowledge_version": version,
                "snapshot_id": snapshot,
                "previous_hash": previous_hash,
                "occurred_at": event_timestamp(),
                "payload": body,
            }
            event_hash = hashlib.sha256(
                canonical_json(core).encode("utf-8")
            ).hexdigest()
            record = {**core, "event_hash": event_hash}
            connection.execute(
                "INSERT INTO registry_events("
                "sequence, event_id, event_type, knowledge_id, knowledge_version, "
                "snapshot_id, previous_hash, event_hash, payload_json, record_json, "
                "occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sequence,
                    event_id,
                    kind,
                    identifier,
                    version,
                    snapshot,
                    previous_hash,
                    event_hash,
                    canonical_json(body),
                    canonical_json(record),
                    core["occurred_at"],
                ),
            )
        return record

    def events(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT record_json FROM registry_events ORDER BY sequence"
            ).fetchall()
        return [json.loads(str(row["record_json"])) for row in rows]

    def integrity_findings(self) -> list[str]:
        findings: list[str] = []
        previous_hash = ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT sequence, previous_hash, event_hash, payload_json, record_json "
                "FROM registry_events ORDER BY sequence"
            ).fetchall()
        for expected, row in enumerate(rows, start=1):
            sequence = int(row["sequence"])
            if sequence != expected:
                findings.append(f"sequence_gap:{expected}:{sequence}")
            if str(row["previous_hash"]) != previous_hash:
                findings.append(f"previous_hash_mismatch:{sequence}")
            try:
                record = json.loads(str(row["record_json"]))
                payload = json.loads(str(row["payload_json"]))
            except json.JSONDecodeError:
                findings.append(f"invalid_json:{sequence}")
                previous_hash = str(row["event_hash"])
                continue
            if record.get("payload") != payload:
                findings.append(f"payload_projection_mismatch:{sequence}")
            core = {key: value for key, value in record.items() if key != "event_hash"}
            calculated = hashlib.sha256(
                canonical_json(core).encode("utf-8")
            ).hexdigest()
            if calculated != str(row["event_hash"]):
                findings.append(f"event_hash_mismatch:{sequence}")
            previous_hash = str(row["event_hash"])
        return findings

    def current_snapshot_id(self) -> str:
        for event in reversed(self.events()):
            if event["event_type"] in {"snapshot_published", "snapshot_rolled_back"}:
                return str(event["snapshot_id"])
        return ""

    def projected_items(self) -> dict[tuple[str, int], dict[str, object]]:
        states: dict[tuple[str, int], dict[str, object]] = {}
        for event in self.events():
            identifier = str(event["knowledge_id"])
            version = int(event["knowledge_version"])
            if not identifier or not version:
                continue
            key = (identifier, version)
            kind = str(event["event_type"])
            if kind == "shadow_validated":
                states.setdefault(key, {"state": "shadow"})
            elif kind == "knowledge_promoted":
                knowledge = dict(event["payload"].get("knowledge") or {})
                states[key] = {"state": "active", "knowledge": knowledge}
            elif kind == "knowledge_challenged" and key in states:
                states[key]["state"] = "challenged"
            elif kind in {"knowledge_retired", "knowledge_superseded"} and key in states:
                states[key]["state"] = "retired"
        return states

    def state_for(self, knowledge_id: str, version: int) -> str:
        key = (require_identifier(knowledge_id, "knowledge_id"), int(version))
        return str(self.projected_items().get(key, {}).get("state") or "candidate")

    def active_items(self) -> tuple[KnowledgeItem, ...]:
        active: list[KnowledgeItem] = []
        for projection in self.projected_items().values():
            if projection.get("state") != "active":
                continue
            active.append(KnowledgeItem.from_dict(projection["knowledge"]))
        return tuple(sorted(active, key=lambda item: (item.knowledge_id, item.version)))


__all__ = ["REGISTRY_EVENT_TYPES", "RegistryStore"]
