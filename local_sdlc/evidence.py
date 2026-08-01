"""Evidence records and requirement verdict projection."""

from __future__ import annotations

import dataclasses
import re
import shlex
from typing import Sequence

from .requirements import acceptance_required_covers


EVIDENCE_STATUSES = frozenset({"pass", "fail", "blocked", "invalid_evidence"})
VERDICT_STATUSES = frozenset({"pass", "fail", "unverified", "blocked", "invalid_evidence"})


@dataclasses.dataclass(frozen=True)
class Evidence:
    evidence_id: str
    observable_id: str
    kind: str
    status: str
    command: str = ""
    exit_code: int | None = None
    document: str = ""
    covers: tuple[str, ...] = ()
    observations: dict[str, object] = dataclasses.field(default_factory=dict)
    failure_type: str | None = None

    def __post_init__(self) -> None:
        if self.status not in EVIDENCE_STATUSES:
            raise ValueError(f"invalid evidence status: {self.status}")

    def to_manifest(self) -> dict[str, object]:
        return {
            "id": self.evidence_id,
            "observable_id": self.observable_id,
            "kind": self.kind,
            "status": self.status,
            "command": self.command,
            "exit_code": self.exit_code,
            "document": self.document,
            "covers": list(self.covers),
            "observations": dict(self.observations),
            "failure_type": self.failure_type,
        }


@dataclasses.dataclass(frozen=True)
class Verdict:
    verdict_id: str
    requirement_id: str
    status: str
    evidence_ids: tuple[str, ...] = ()
    required_covers: tuple[str, ...] = ()
    evidence_scope: str = "direct"

    def __post_init__(self) -> None:
        if self.status not in VERDICT_STATUSES:
            raise ValueError(f"invalid verdict status: {self.status}")


def evidence_covers(evidence_item: dict[str, object]) -> list[str]:
    raw = evidence_item.get("covers")
    if isinstance(raw, list):
        return [str(item) for item in raw if isinstance(item, str)]
    return []


def evidence_matches_acceptance_text(evidence_item: dict[str, object], text: str) -> bool:
    command = str(evidence_item.get("command") or evidence_item.get("name") or "").lower()
    if not command:
        return False
    fragments = re.findall(r"`([^`]+)`", text)
    for fragment in fragments:
        try:
            tokens = shlex.split(fragment.lower())
        except ValueError:
            tokens = fragment.lower().split()
        if tokens and all(token in command for token in tokens):
            return True
    return False


def _verdict_status(relevant: Sequence[dict[str, object]]) -> str:
    statuses = {str(record.get("status") or "") for record in relevant}
    if "fail" in statuses:
        return "fail"
    if "invalid_evidence" in statuses:
        return "invalid_evidence"
    if "blocked" in statuses:
        return "blocked"
    if "pass" in statuses:
        return "pass"
    return "unverified"


def build_acceptance_matrix(
    criteria: Sequence[dict[str, str]],
    evidence: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    if not criteria:
        return [
            {
                "id": f"A{index:02d}",
                "text": f"Executable check: {item.get('name')}",
                "status": item.get("status"),
                "evidence_ids": [item.get("id")],
                "evidence_scope": "direct",
            }
            for index, item in enumerate(evidence, start=1)
        ]

    matrix: list[dict[str, object]] = []
    for item in criteria:
        required_covers = acceptance_required_covers(item["text"])
        relevant: list[dict[str, object]] = []
        direct_command_match = False
        for evidence_item in evidence:
            covers = evidence_covers(evidence_item)
            if evidence_matches_acceptance_text(evidence_item, item["text"]):
                relevant.append(evidence_item)
                direct_command_match = True
                continue
            if "external_test_suite" in covers:
                relevant.append(evidence_item)
                continue
            if required_covers and any(cover in covers for cover in required_covers):
                relevant.append(evidence_item)
        matrix.append(
            {
                "id": item["id"],
                "text": item["text"],
                "status": _verdict_status(relevant),
                "required_covers": required_covers,
                "evidence_ids": [record.get("id") for record in relevant],
                "evidence_scope": "direct" if required_covers or direct_command_match else "external_or_unmapped",
            }
        )
    return matrix


def acceptance_blockers(matrix: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    return [
        item
        for item in matrix
        if item.get("required_covers") and item.get("status") != "pass"
    ]


def verdicts_from_acceptance_matrix(matrix: Sequence[dict[str, object]]) -> list[Verdict]:
    verdicts: list[Verdict] = []
    for index, item in enumerate(matrix, start=1):
        status = str(item.get("status") or "unverified")
        if status not in VERDICT_STATUSES:
            status = "invalid_evidence"
        verdicts.append(
            Verdict(
                verdict_id=f"V{index:03d}",
                requirement_id=str(item.get("id") or f"A{index:02d}"),
                status=status,
                evidence_ids=tuple(
                    str(value)
                    for value in item.get("evidence_ids", [])
                    if isinstance(value, str)
                ),
                required_covers=tuple(
                    str(value)
                    for value in item.get("required_covers", [])
                    if isinstance(value, str)
                ),
                evidence_scope=str(item.get("evidence_scope") or "direct"),
            )
        )
    return verdicts


def verdict_to_manifest(verdict: Verdict) -> dict[str, object]:
    return {
        "id": verdict.verdict_id,
        "requirement_id": verdict.requirement_id,
        "status": verdict.status,
        "evidence_ids": list(verdict.evidence_ids),
        "required_covers": list(verdict.required_covers),
        "evidence_scope": verdict.evidence_scope,
    }
