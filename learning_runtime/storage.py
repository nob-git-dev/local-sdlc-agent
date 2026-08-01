"""Shared, project-independent experience persistence."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3

from sdlc_events import EventEnvelope, canonical_json, event_timestamp

from .privacy import sanitize_shared, sensitive_values


EXPERIENCE_DB_FILENAME = "experience.sqlite3"


def learning_data_dir(explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    configured = os.environ.get("LOCAL_SDLC_LEARNING_HOME", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    root = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return (root / "local-sdlc" / "learning").resolve()


class ExperienceStore:
    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = learning_data_dir(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / EXPERIENCE_DB_FILENAME
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
                CREATE TABLE IF NOT EXISTS normalized_events (
                    event_id TEXT PRIMARY KEY,
                    source_event_hash TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    project_fingerprint TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    collected_at TEXT NOT NULL,
                    sanitized_envelope_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_normalized_events_run
                    ON normalized_events(run_id, occurred_at);

                CREATE TABLE IF NOT EXISTS collection_runs (
                    collection_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL,
                    inserted_count INTEGER NOT NULL DEFAULT 0,
                    duplicate_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT ''
                );
                """
            )

    def put_event(self, event: EventEnvelope) -> bool:
        sanitized = sanitize_shared(event.to_dict())
        unsafe = sensitive_values(sanitized)
        if unsafe:
            raise ValueError("shared event still contains sensitive values: " + ", ".join(unsafe))
        serialized = canonical_json(sanitized)
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO normalized_events("
                "event_id, source_event_hash, run_id, project_fingerprint, event_type, "
                "occurred_at, collected_at, sanitized_envelope_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    event.event_hash,
                    event.run_id,
                    event.project_fingerprint,
                    event.event_type,
                    event.occurred_at,
                    event_timestamp(),
                    serialized,
                ),
            )
            return cursor.rowcount == 1

    def event_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS value FROM normalized_events").fetchone()
        return int(row["value"] or 0)

    def events(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT sanitized_envelope_json FROM normalized_events ORDER BY rowid"
            ).fetchall()
        return [json.loads(str(row["sanitized_envelope_json"])) for row in rows]
