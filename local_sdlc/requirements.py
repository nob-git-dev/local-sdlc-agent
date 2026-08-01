"""Requirement and observable domain models.

This module owns generic requirement parsing. Domain-specific evidence labels
are supplied by harness coverage rules so the application runner does not need
to know about a particular benchmark.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Sequence

from .harnesses.coverage_rules import required_covers_for_text


REQUIREMENT_STATUSES = frozenset({"pending", "covered", "blocked"})


@dataclasses.dataclass(frozen=True)
class Observable:
    observable_id: str
    kind: str
    target: str
    expected: str
    harness: str
    timeout: float

    def to_manifest(self) -> dict[str, object]:
        return {
            "id": self.observable_id,
            "kind": self.kind,
            "target": self.target,
            "expected": self.expected,
            "harness": self.harness,
            "timeout": self.timeout,
        }


@dataclasses.dataclass(frozen=True)
class Requirement:
    requirement_id: str
    text: str
    source_path: str
    source_section: str
    required_observables: tuple[str, ...] = ()
    status: str = "pending"

    def __post_init__(self) -> None:
        if self.status not in REQUIREMENT_STATUSES:
            raise ValueError(f"invalid requirement status: {self.status}")

    def to_manifest(self) -> dict[str, object]:
        return {
            "id": self.requirement_id,
            "text": self.text,
            "source_path": self.source_path,
            "source_section": self.source_section,
            "required_observables": list(self.required_observables),
            "status": self.status,
        }


def requirements_from_spec(spec: str, source_path: str = "SPEC.md") -> list[Requirement]:
    requirements: list[Requirement] = []
    seen: set[str] = set()
    section = ""
    in_acceptance_section = False
    for line in spec.splitlines():
        heading = re.match(r"^(#{1,3})\s+(.+?)\s*$", line)
        if heading:
            section = heading.group(2).strip()
            lowered = section.lower()
            in_acceptance_section = any(
                token in lowered
                for token in ("受け入れ", "受入", "acceptance", "検証", "完了条件")
            )
        match = re.match(r"^\s*[-*]\s+(?:\[[ xX]\]\s+)?(.+?)\s*$", line)
        if not match:
            continue
        text = match.group(1).strip()
        if not in_acceptance_section and not re.search(
            r"pass|ok|確認|検証|テスト|smoke|command|agent|runner",
            text,
            re.IGNORECASE,
        ):
            continue
        if text in seen:
            continue
        seen.add(text)
        requirement_id = f"A{len(requirements) + 1:02d}"
        required = tuple(acceptance_required_covers(text))
        requirements.append(
            Requirement(
                requirement_id=requirement_id,
                text=text,
                source_path=source_path,
                source_section=section,
                required_observables=required,
            )
        )
    return requirements


def parse_acceptance_criteria(spec: str) -> list[dict[str, str]]:
    """Return the legacy acceptance-criteria projection."""
    return [
        {"id": item.requirement_id, "text": item.text}
        for item in requirements_from_spec(spec)
    ]


def acceptance_required_covers(text: str) -> list[str]:
    return required_covers_for_text(text)


def observables_for_requirements(
    requirements: Sequence[Requirement],
    timeout: float = 60.0,
) -> list[Observable]:
    observables: list[Observable] = []
    for requirement in requirements:
        for cover in requirement.required_observables:
            observables.append(
                Observable(
                    observable_id=f"O{len(observables) + 1:03d}",
                    kind="coverage",
                    target=cover,
                    expected="pass",
                    harness="auto",
                    timeout=timeout,
                )
            )
    return observables
