"""Shared harness plugin contracts.

Harnesses produce evidence. They do not decide whether an agent run is approved.
The application layer combines evidence with requirements and judge policy.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Protocol


@dataclasses.dataclass(frozen=True)
class HarnessEvidence:
    kind: str
    name: str
    status: str
    command: str
    exit_code: int
    duration_seconds: float
    document: str
    failure_type: str | None = None
    covers: tuple[str, ...] = ()
    observations: dict[str, object] = dataclasses.field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "pass"

    def to_legacy_result(self) -> tuple[str, bool]:
        return self.document, self.ok


class Harness(Protocol):
    name: str

    def run(self) -> list[HarnessEvidence]:
        """Return evidence records for the configured check."""


def evidence_from_command_result(kind: str, name: str, document: str, ok: bool) -> HarnessEvidence:
    """Convert a command result document into harness evidence."""
    from ..verification import classify_failure, parse_command_result_document

    parsed = parse_command_result_document(document)
    try:
        exit_code = int(parsed.get("exit_code", "0" if ok else "1"))
    except ValueError:
        exit_code = 0 if ok else 1
    try:
        duration = float(parsed.get("duration_seconds", "0") or 0.0)
    except ValueError:
        duration = 0.0
    stdout_payload: dict[str, object] = {}
    stdout = parsed.get("stdout", "").strip()
    if stdout.startswith("{"):
        try:
            loaded = json.loads(stdout)
            if isinstance(loaded, dict):
                stdout_payload = loaded
        except json.JSONDecodeError:
            stdout_payload = {}
    covers = tuple(str(item) for item in stdout_payload.get("covers", []) if isinstance(item, str))
    observations = stdout_payload.get("observations", {})
    if not isinstance(observations, dict):
        observations = {}

    return HarnessEvidence(
        kind=kind,
        name=name,
        status="pass" if ok else "fail",
        command=parsed.get("command", name),
        exit_code=exit_code,
        duration_seconds=duration,
        document=document,
        failure_type=None
        if ok
        else classify_failure(
            exit_code,
            parsed.get("stdout", ""),
            parsed.get("stderr", ""),
            parsed.get("blocked_reason"),
        ),
        covers=covers,
        observations={str(key): value for key, value in observations.items()},
    )
