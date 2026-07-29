"""Shared harness plugin contracts.

Harnesses produce evidence. They do not decide whether an agent run is approved.
The application layer combines evidence with requirements and judge policy.
"""

from __future__ import annotations

import dataclasses
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
