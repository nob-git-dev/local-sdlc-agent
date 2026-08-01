"""Run-local SQLite event ledger and transactional outbox."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Iterable, Mapping
import uuid

from .contracts import EventType, TransitionKind, contract_for, validate_contract_registry
from .models import (
    EVENT_SCHEMA_VERSION,
    EventEnvelope,
    TransitionRequest,
    canonical_json,
    event_timestamp,
    json_safe,
    stable_identifier,
)


EVENT_LEDGER_FILENAME = "runtime-events.sqlite3"


class EventLedgerError(RuntimeError):
    """Base error for an event contract or ledger failure."""


class EventConflictError(EventLedgerError):
    """An idempotency key was reused for a different transition."""


class InjectedLedgerFault(EventLedgerError):
    """Fault used to verify transaction behavior."""


def event_ledger_path(run_dir: Path) -> Path:
    return run_dir / EVENT_LEDGER_FILENAME


def _project_root(run_dir: Path) -> Path:
    resolved = run_dir.resolve()
    for parent in (resolved, *resolved.parents):
        if parent.name == ".sdlc-runner":
            return parent.parent
    return resolved.parent


def _default_project_fingerprint(run_dir: Path) -> str:
    return stable_identifier("PROJECT", _project_root(run_dir).resolve())


def _default_run_id(run_dir: Path, project_fingerprint: str) -> str:
    resolved = run_dir.resolve()
    root = _project_root(run_dir).resolve()
    try:
        locator = resolved.relative_to(root)
    except ValueError:
        locator = resolved
    return stable_identifier("RUN", project_fingerprint, locator)


class RuntimeEventLedger:
    """One run's canonical transitions, immutable events, and outbox."""

    def __init__(
        self,
        run_dir: Path,
        *,
        run_id: str = "",
        project_fingerprint: str = "",
    ) -> None:
        self.run_dir = run_dir.resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.project_fingerprint = project_fingerprint or _default_project_fingerprint(self.run_dir)
        self.run_id = run_id or _default_run_id(self.run_dir, self.project_fingerprint)
        self.path = event_ledger_path(self.run_dir)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS transitions (
                    transition_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    transition_kind TEXT NOT NULL,
                    aggregate_type TEXT NOT NULL,
                    aggregate_id TEXT NOT NULL,
                    source_component TEXT NOT NULL,
                    source_key TEXT NOT NULL DEFAULT '',
                    committed_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_transitions_source_key
                    ON transitions(run_id, source_key) WHERE source_key <> '';

                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    transition_id TEXT NOT NULL UNIQUE,
                    run_id TEXT NOT NULL,
                    aggregate_type TEXT NOT NULL,
                    aggregate_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    previous_hash TEXT,
                    envelope_json TEXT NOT NULL,
                    FOREIGN KEY(transition_id) REFERENCES transitions(transition_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_events_aggregate_sequence
                    ON events(run_id, aggregate_type, aggregate_id, sequence);

                CREATE TABLE IF NOT EXISTS outbox (
                    event_id TEXT PRIMARY KEY,
                    envelope_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    delivered_at TEXT,
                    last_error TEXT,
                    FOREIGN KEY(event_id) REFERENCES events(event_id)
                );

                CREATE TABLE IF NOT EXISTS projection_status (
                    projection_name TEXT PRIMARY KEY,
                    source_event_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS contract_audits (
                    audit_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    finding_count INTEGER NOT NULL,
                    report_json TEXT NOT NULL
                );
                """
            )

    def _existing_event(self, connection: sqlite3.Connection, transition_id: str) -> EventEnvelope | None:
        row = connection.execute(
            "SELECT envelope_json FROM events WHERE transition_id = ?",
            (transition_id,),
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row["envelope_json"]))
        return EventEnvelope.from_dict(payload)

    def commit_transition(
        self,
        request: TransitionRequest,
        *,
        fault_at: str = "",
    ) -> EventEnvelope:
        contract = contract_for(request.transition_kind)
        if request.aggregate_type != contract.aggregate_type:
            raise EventLedgerError(
                f"aggregate type mismatch for {contract.transition_kind.value}: "
                f"expected={contract.aggregate_type} actual={request.aggregate_type}"
            )
        transition_id = request.transition_id.strip()
        if not transition_id and request.source_key:
            transition_id = stable_identifier("TR", self.run_id, request.source_key)
        if not transition_id:
            transition_id = "TR-" + uuid.uuid4().hex
        event_id = stable_identifier("EV", transition_id)
        occurred_at = request.occurred_at or event_timestamp()
        correlation_id = request.correlation_id or self.run_id

        connection = self._connect()
        committed = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._existing_event(connection, transition_id)
            if existing is not None:
                row = connection.execute(
                    "SELECT transition_kind, aggregate_type, aggregate_id FROM transitions "
                    "WHERE transition_id = ?",
                    (transition_id,),
                ).fetchone()
                expected = (
                    contract.transition_kind.value,
                    request.aggregate_type,
                    request.aggregate_id,
                )
                actual = (
                    str(row["transition_kind"]),
                    str(row["aggregate_type"]),
                    str(row["aggregate_id"]),
                ) if row is not None else ("", "", "")
                if actual != expected:
                    raise EventConflictError(
                        f"transition id reused with different identity: {transition_id}"
                    )
                connection.rollback()
                return existing

            sequence_row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS value FROM events "
                "WHERE run_id = ? AND aggregate_type = ? AND aggregate_id = ?",
                (self.run_id, request.aggregate_type, request.aggregate_id),
            ).fetchone()
            sequence = int(sequence_row["value"] or 0) + 1
            prior = connection.execute(
                "SELECT event_hash FROM events WHERE run_id = ? AND aggregate_type = ? "
                "AND aggregate_id = ? ORDER BY sequence DESC LIMIT 1",
                (self.run_id, request.aggregate_type, request.aggregate_id),
            ).fetchone()
            previous_hash = str(prior["event_hash"]) if prior is not None else None

            envelope = EventEnvelope(
                schema_version=EVENT_SCHEMA_VERSION,
                event_id=event_id,
                transition_id=transition_id,
                run_id=self.run_id,
                project_fingerprint=self.project_fingerprint,
                aggregate_type=request.aggregate_type,
                aggregate_id=request.aggregate_id,
                sequence=sequence,
                event_type=contract.event_type.value,
                occurred_at=occurred_at,
                source_component=request.source_component,
                correlation_id=correlation_id,
                causation_id=request.causation_id,
                knowledge_eligibility=request.knowledge_eligibility,
                propositions=tuple(str(item) for item in request.propositions),
                evidence_refs=tuple(request.evidence_refs),
                payload=json_safe(request.payload) if isinstance(request.payload, Mapping) else {},
                sensitivity=request.sensitivity,
                previous_hash=previous_hash,
                event_hash="",
            )
            event_hash = hashlib.sha256(
                canonical_json(envelope.unsigned_dict()).encode("utf-8")
            ).hexdigest()
            envelope = replace(envelope, event_hash=event_hash)
            serialized = canonical_json(envelope.to_dict())

            connection.execute(
                "INSERT INTO transitions(transition_id, run_id, transition_kind, aggregate_type, "
                "aggregate_id, source_component, source_key, committed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    transition_id,
                    self.run_id,
                    contract.transition_kind.value,
                    request.aggregate_type,
                    request.aggregate_id,
                    request.source_component,
                    request.source_key,
                    occurred_at,
                ),
            )
            if fault_at == "after_transition":
                raise InjectedLedgerFault("injected fault after transition insert")
            connection.execute(
                "INSERT INTO events(event_id, transition_id, run_id, aggregate_type, aggregate_id, "
                "sequence, event_type, event_hash, previous_hash, envelope_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    transition_id,
                    self.run_id,
                    request.aggregate_type,
                    request.aggregate_id,
                    sequence,
                    contract.event_type.value,
                    event_hash,
                    previous_hash,
                    serialized,
                ),
            )
            connection.execute(
                "INSERT INTO outbox(event_id, envelope_json, status, attempts, created_at) "
                "VALUES (?, ?, 'pending', 0, ?)",
                (event_id, serialized, occurred_at),
            )
            if fault_at == "before_commit":
                raise InjectedLedgerFault("injected fault before transaction commit")
            connection.commit()
            committed = True
            if fault_at == "after_commit":
                raise InjectedLedgerFault("injected fault after transaction commit")
            return envelope
        except Exception:
            if not committed:
                connection.rollback()
            raise
        finally:
            connection.close()

    def list_events(self) -> list[EventEnvelope]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT envelope_json FROM events ORDER BY rowid"
            ).fetchall()
        return [EventEnvelope.from_dict(json.loads(str(row["envelope_json"]))) for row in rows]

    def transition_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS value FROM transitions").fetchone()
        return int(row["value"] or 0)

    def event_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS value FROM events").fetchone()
        return int(row["value"] or 0)

    def pending_outbox(self, limit: int = 1000) -> list[EventEnvelope]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT envelope_json FROM outbox WHERE status = 'pending' ORDER BY rowid LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [EventEnvelope.from_dict(json.loads(str(row["envelope_json"]))) for row in rows]

    def mark_delivered(self, event_ids: Iterable[str]) -> None:
        delivered_at = event_timestamp()
        with self._connect() as connection:
            for event_id in event_ids:
                connection.execute(
                    "UPDATE outbox SET status = 'delivered', attempts = attempts + 1, "
                    "delivered_at = ?, last_error = NULL WHERE event_id = ?",
                    (delivered_at, str(event_id)),
                )

    def mark_delivery_error(self, event_ids: Iterable[str], error: str) -> None:
        with self._connect() as connection:
            for event_id in event_ids:
                connection.execute(
                    "UPDATE outbox SET attempts = attempts + 1, last_error = ? WHERE event_id = ?",
                    (str(error), str(event_id)),
                )

    def outbox_status(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS value FROM outbox GROUP BY status"
            ).fetchall()
        result = {"pending": 0, "delivered": 0}
        for row in rows:
            result[str(row["status"])] = int(row["value"] or 0)
        return result

    def integrity_findings(self) -> list[dict[str, object]]:
        findings: list[dict[str, object]] = [
            {"code": item, "detail": "transition contract registry is incomplete"}
            for item in validate_contract_registry()
        ]
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT t.transition_id, t.transition_kind, t.aggregate_type, t.aggregate_id, "
                "e.event_id AS stored_event_id, e.run_id AS stored_run_id, "
                "e.aggregate_type AS stored_aggregate_type, e.aggregate_id AS stored_aggregate_id, "
                "e.sequence AS stored_sequence, e.event_type AS stored_event_type, "
                "e.event_hash AS stored_event_hash, e.previous_hash AS stored_previous_hash, "
                "e.envelope_json FROM transitions t LEFT JOIN events e "
                "ON e.transition_id = t.transition_id ORDER BY t.rowid"
            ).fetchall()
            outbox_ids = {
                str(row["event_id"])
                for row in connection.execute("SELECT event_id FROM outbox").fetchall()
            }

        events_by_aggregate: dict[tuple[str, str], list[tuple[int, EventEnvelope]]] = {}
        valid_events: list[EventEnvelope] = []
        event_types: set[str] = set()
        for row in rows:
            transition_id = str(row["transition_id"])
            if row["envelope_json"] is None:
                findings.append({"code": "transition_without_event", "transition_id": transition_id})
                continue
            try:
                raw_envelope = json.loads(str(row["envelope_json"]))
                if not isinstance(raw_envelope, Mapping):
                    raise ValueError("event envelope is not an object")
                envelope = EventEnvelope.from_dict(raw_envelope)
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                findings.append(
                    {
                        "code": "invalid_event_envelope",
                        "transition_id": transition_id,
                        "detail": str(exc),
                    }
                )
                continue
            valid_events.append(envelope)
            event_types.add(envelope.event_type)
            try:
                contract = contract_for(str(row["transition_kind"]))
            except ValueError:
                findings.append({"code": "unknown_transition", "transition_id": transition_id})
                continue
            if envelope.event_type != contract.event_type.value:
                findings.append(
                    {
                        "code": "event_type_mismatch",
                        "transition_id": transition_id,
                        "expected": contract.event_type.value,
                        "actual": envelope.event_type,
                    }
                )
            stored_fields = {
                "event_id": str(row["stored_event_id"]),
                "run_id": str(row["stored_run_id"]),
                "aggregate_type": str(row["stored_aggregate_type"]),
                "aggregate_id": str(row["stored_aggregate_id"]),
                "sequence": int(row["stored_sequence"]),
                "event_type": str(row["stored_event_type"]),
                "event_hash": str(row["stored_event_hash"]),
                "previous_hash": (
                    str(row["stored_previous_hash"])
                    if row["stored_previous_hash"] is not None
                    else None
                ),
            }
            envelope_fields = {
                "event_id": envelope.event_id,
                "run_id": envelope.run_id,
                "aggregate_type": envelope.aggregate_type,
                "aggregate_id": envelope.aggregate_id,
                "sequence": envelope.sequence,
                "event_type": envelope.event_type,
                "event_hash": envelope.event_hash,
                "previous_hash": envelope.previous_hash,
            }
            for field, stored_value in stored_fields.items():
                if stored_value != envelope_fields[field]:
                    findings.append(
                        {
                            "code": "event_storage_mismatch",
                            "event_id": envelope.event_id,
                            "field": field,
                            "stored": stored_value,
                            "envelope": envelope_fields[field],
                        }
                    )
            if envelope.schema_version != EVENT_SCHEMA_VERSION:
                findings.append({"code": "unsupported_schema", "event_id": envelope.event_id})
            if not envelope.verify_hash():
                findings.append({"code": "event_hash_mismatch", "event_id": envelope.event_id})
            if envelope.event_id not in outbox_ids:
                findings.append({"code": "event_missing_outbox", "event_id": envelope.event_id})
            events_by_aggregate.setdefault(
                (str(row["stored_aggregate_type"]), str(row["stored_aggregate_id"])), []
            ).append((int(row["stored_sequence"]), envelope))

        for (aggregate_type, aggregate_id), events in events_by_aggregate.items():
            prior_hash: str | None = None
            for expected_sequence, (stored_sequence, envelope) in enumerate(
                sorted(events, key=lambda item: item[0]), 1
            ):
                if stored_sequence != expected_sequence:
                    findings.append(
                        {
                            "code": "aggregate_sequence_gap",
                            "aggregate_type": aggregate_type,
                            "aggregate_id": aggregate_id,
                            "expected": expected_sequence,
                            "actual": stored_sequence,
                        }
                    )
                if envelope.previous_hash != prior_hash:
                    findings.append(
                        {
                            "code": "event_hash_chain_broken",
                            "event_id": envelope.event_id,
                        }
                    )
                prior_hash = envelope.event_hash

        if EventType.RUN_TERMINATED.value in event_types and EventType.RUN_STARTED.value not in event_types:
            findings.append({"code": "run_closed_without_start"})
        for envelope in valid_events:
            if envelope.event_type == EventType.RUN_TERMINATED.value:
                expected = bool(envelope.payload.get("verification_expected"))
                if expected and EventType.VERIFICATION_COMPLETED.value not in event_types:
                    findings.append({"code": "run_closed_without_verification"})
            if envelope.event_type == EventType.STAGE_CLOSED.value:
                matching_start = any(
                    item.event_type == EventType.STAGE_STARTED.value
                    and item.aggregate_id == envelope.aggregate_id
                    for item in valid_events
                )
                if not matching_start:
                    findings.append(
                        {"code": "stage_closed_without_start", "aggregate_id": envelope.aggregate_id}
                    )
        return findings

    def record_audit(self, report: Mapping[str, object]) -> str:
        created_at = event_timestamp()
        report_json = canonical_json(report)
        audit_id = stable_identifier("AUDIT", self.run_id, created_at, report_json)
        findings = report.get("findings")
        count = len(findings) if isinstance(findings, list) else 0
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO contract_audits(audit_id, created_at, status, finding_count, report_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (audit_id, created_at, str(report.get("status") or "unknown"), count, report_json),
            )
        return audit_id

    def record_contract_violation(self, findings: list[dict[str, object]]) -> EventEnvelope:
        fingerprint = hashlib.sha256(canonical_json(findings).encode("utf-8")).hexdigest()
        return self.commit_transition(
            TransitionRequest(
                transition_kind=TransitionKind.EVENT_CONTRACT_VIOLATION.value,
                aggregate_type="goal",
                aggregate_id=self.run_id,
                source_component="event_audit",
                source_key=f"contract_violation:{fingerprint}",
                payload={"finding_count": len(findings), "findings": findings},
                knowledge_eligibility="ineligible",
                sensitivity="project",
            )
        )
