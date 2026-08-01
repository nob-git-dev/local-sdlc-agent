"""Versioned immutable models for runtime transitions and learning events."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
import datetime as _datetime
import hashlib
import json
from pathlib import Path
from typing import Mapping, Sequence


EVENT_SCHEMA_VERSION = 1
VALID_AGGREGATE_TYPES = {"goal", "stage", "action", "verification", "knowledge"}
VALID_ELIGIBILITY = {"unknown", "eligible", "ineligible"}
VALID_SENSITIVITY = {"public", "project", "restricted"}


def event_timestamp() -> str:
    return _datetime.datetime.now(tz=_datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def stable_identifier(prefix: str, *parts: object, length: int = 24) -> str:
    basis = canonical_json([str(part) for part in parts])
    return f"{prefix}-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:length]


def json_safe(value: object) -> object:
    """Convert event data to a deterministic JSON-compatible value."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return json_safe(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(item) for item in value]
    return str(value)


def canonical_json(value: object) -> str:
    return json.dumps(
        json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


@dataclass(frozen=True)
class EvidenceReference:
    path: str
    sha256: str
    media_type: str = "application/octet-stream"
    line: int | None = None
    redaction_status: str = "not_required"

    def __post_init__(self) -> None:
        if not self.path.strip() or self.path == ".":
            raise ValueError("evidence path is required")
        path = Path(self.path)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("evidence path must be a safe relative path")
        if not self.sha256 or len(self.sha256) != 64:
            raise ValueError("evidence sha256 must contain 64 hexadecimal characters")
        try:
            int(self.sha256, 16)
        except ValueError as exc:
            raise ValueError("evidence sha256 is not hexadecimal") from exc
        if self.line is not None and self.line <= 0:
            raise ValueError("evidence line must be positive")

    def to_dict(self) -> dict[str, object]:
        return {
            key: value
            for key, value in asdict(self).items()
            if value is not None
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "EvidenceReference":
        raw_line = payload.get("line")
        return cls(
            path=str(payload.get("path") or ""),
            sha256=str(payload.get("sha256") or ""),
            media_type=str(payload.get("media_type") or "application/octet-stream"),
            line=int(raw_line) if raw_line is not None else None,
            redaction_status=str(payload.get("redaction_status") or "not_required"),
        )


@dataclass(frozen=True)
class TransitionRequest:
    transition_kind: str
    aggregate_type: str
    aggregate_id: str
    source_component: str
    payload: Mapping[str, object] = field(default_factory=dict)
    propositions: Sequence[str] = field(default_factory=tuple)
    evidence_refs: Sequence[EvidenceReference] = field(default_factory=tuple)
    correlation_id: str = ""
    causation_id: str | None = None
    knowledge_eligibility: str = "unknown"
    sensitivity: str = "project"
    transition_id: str = ""
    source_key: str = ""
    occurred_at: str = ""

    def __post_init__(self) -> None:
        if self.aggregate_type not in VALID_AGGREGATE_TYPES:
            raise ValueError(f"invalid aggregate type: {self.aggregate_type}")
        if not self.aggregate_id.strip():
            raise ValueError("aggregate_id is required")
        if not self.source_component.strip():
            raise ValueError("source_component is required")
        if self.knowledge_eligibility not in VALID_ELIGIBILITY:
            raise ValueError(f"invalid knowledge eligibility: {self.knowledge_eligibility}")
        if self.sensitivity not in VALID_SENSITIVITY:
            raise ValueError(f"invalid sensitivity: {self.sensitivity}")


@dataclass(frozen=True)
class EventEnvelope:
    schema_version: int
    event_id: str
    transition_id: str
    run_id: str
    project_fingerprint: str
    aggregate_type: str
    aggregate_id: str
    sequence: int
    event_type: str
    occurred_at: str
    source_component: str
    correlation_id: str
    causation_id: str | None
    knowledge_eligibility: str
    propositions: tuple[str, ...]
    evidence_refs: tuple[EvidenceReference, ...]
    payload: Mapping[str, object]
    sensitivity: str
    previous_hash: str | None
    event_hash: str

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "transition_id": self.transition_id,
            "run_id": self.run_id,
            "project_fingerprint": self.project_fingerprint,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at,
            "source_component": self.source_component,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "knowledge_eligibility": self.knowledge_eligibility,
            "propositions": list(self.propositions),
            "evidence_refs": [item.to_dict() for item in self.evidence_refs],
            "payload": json_safe(self.payload),
            "sensitivity": self.sensitivity,
            "previous_hash": self.previous_hash,
        }

    def to_dict(self) -> dict[str, object]:
        return {**self.unsigned_dict(), "event_hash": self.event_hash}

    def verify_hash(self) -> bool:
        expected = hashlib.sha256(canonical_json(self.unsigned_dict()).encode("utf-8")).hexdigest()
        return expected == self.event_hash

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "EventEnvelope":
        refs = payload.get("evidence_refs")
        evidence_refs = tuple(
            EvidenceReference.from_dict(item)
            for item in refs
            if isinstance(item, Mapping)
        ) if isinstance(refs, list) else ()
        propositions = payload.get("propositions")
        return cls(
            schema_version=int(payload.get("schema_version") or 0),
            event_id=str(payload.get("event_id") or ""),
            transition_id=str(payload.get("transition_id") or ""),
            run_id=str(payload.get("run_id") or ""),
            project_fingerprint=str(payload.get("project_fingerprint") or ""),
            aggregate_type=str(payload.get("aggregate_type") or ""),
            aggregate_id=str(payload.get("aggregate_id") or ""),
            sequence=int(payload.get("sequence") or 0),
            event_type=str(payload.get("event_type") or ""),
            occurred_at=str(payload.get("occurred_at") or ""),
            source_component=str(payload.get("source_component") or ""),
            correlation_id=str(payload.get("correlation_id") or ""),
            causation_id=str(payload["causation_id"]) if payload.get("causation_id") else None,
            knowledge_eligibility=str(payload.get("knowledge_eligibility") or "unknown"),
            propositions=tuple(str(item) for item in propositions) if isinstance(propositions, list) else (),
            evidence_refs=evidence_refs,
            payload=payload.get("payload") if isinstance(payload.get("payload"), Mapping) else {},
            sensitivity=str(payload.get("sensitivity") or "project"),
            previous_hash=str(payload["previous_hash"]) if payload.get("previous_hash") else None,
            event_hash=str(payload.get("event_hash") or ""),
        )
