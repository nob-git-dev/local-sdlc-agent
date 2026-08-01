"""Shared, project-independent experience persistence."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
from typing import Mapping

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

                CREATE TABLE IF NOT EXISTS recovery_episodes (
                    episode_id TEXT PRIMARY KEY,
                    source_run_id TEXT NOT NULL,
                    project_fingerprint TEXT NOT NULL,
                    eligibility TEXT NOT NULL,
                    structural_signature TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    normalized_episode_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_recovery_episodes_project
                    ON recovery_episodes(project_fingerprint, eligibility);
                CREATE INDEX IF NOT EXISTS idx_recovery_episodes_structure
                    ON recovery_episodes(structural_signature);
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

    def put_episode(self, episode: Mapping[str, object]) -> bool:
        sanitized = sanitize_shared(episode)
        unsafe = sensitive_values(sanitized)
        if unsafe:
            raise ValueError("shared episode still contains sensitive values: " + ", ".join(unsafe))
        if not isinstance(sanitized, Mapping):
            raise ValueError("shared episode must be an object")
        episode_id = str(sanitized.get("episode_id") or "")
        if not episode_id:
            raise ValueError("shared episode requires episode_id")
        serialized = canonical_json(sanitized)
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO recovery_episodes("
                "episode_id, source_run_id, project_fingerprint, eligibility, "
                "structural_signature, created_at, normalized_episode_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    episode_id,
                    str(sanitized.get("source_run_id") or ""),
                    str(sanitized.get("project_fingerprint") or ""),
                    str(sanitized.get("eligibility") or "case_only"),
                    str(sanitized.get("structural_signature") or ""),
                    event_timestamp(),
                    serialized,
                ),
            )
            return cursor.rowcount == 1

    def event_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS value FROM normalized_events").fetchone()
        return int(row["value"] or 0)

    def episode_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS value FROM recovery_episodes").fetchone()
        return int(row["value"] or 0)

    def events(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT sanitized_envelope_json FROM normalized_events ORDER BY rowid"
            ).fetchall()
        return [json.loads(str(row["sanitized_envelope_json"])) for row in rows]

    def episodes(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT normalized_episode_json FROM recovery_episodes ORDER BY rowid"
            ).fetchall()
        return [json.loads(str(row["normalized_episode_json"])) for row in rows]
