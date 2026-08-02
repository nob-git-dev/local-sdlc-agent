"""Concrete project-local observations used to build a Domain Map."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Mapping

from sdlc_events import canonical_json

from .knowledge_schema import EvidenceAnchor
from .schema_validation import require_sequence, require_technology_name


SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


def normalize_domain_slug(value: str, field_name: str) -> str:
    normalized = value.strip().lower()
    if not SLUG_PATTERN.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a normalized identifier")
    return normalized


def _relative_path(value: str) -> str:
    normalized = value.strip()
    path = Path(normalized)
    if (
        not normalized
        or normalized == "."
        or path.is_absolute()
        or ".." in path.parts
    ):
        raise ValueError("component path must be a safe relative path")
    return path.as_posix()


@dataclass(frozen=True)
class TechnologyObservation:
    ecosystem: str
    name: str
    version: str = ""
    evidence_refs: tuple[EvidenceAnchor, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "ecosystem",
            normalize_domain_slug(self.ecosystem, "technology ecosystem"),
        )
        object.__setattr__(
            self,
            "name",
            require_technology_name(self.name, "technology name"),
        )
        object.__setattr__(self, "version", self.version.strip())
        refs = tuple(
            sorted(self.evidence_refs, key=lambda item: canonical_json(item.to_dict()))
        )
        if not refs:
            raise ValueError("technology observation requires evidence")
        if len({canonical_json(item.to_dict()) for item in refs}) != len(refs):
            raise ValueError("duplicate technology evidence")
        object.__setattr__(self, "evidence_refs", refs)

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.ecosystem, self.name, self.version

    def to_dict(self) -> dict[str, object]:
        return {
            "ecosystem": self.ecosystem,
            "name": self.name,
            "version": self.version,
            "evidence_refs": [item.to_dict() for item in self.evidence_refs],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "TechnologyObservation":
        unknown = set(payload) - {"ecosystem", "name", "version", "evidence_refs"}
        if unknown:
            raise ValueError(
                "unknown technology fields: " + ", ".join(sorted(unknown))
            )
        refs = require_sequence(
            payload.get("evidence_refs"),
            "technology evidence_refs",
        )
        if any(not isinstance(item, Mapping) for item in refs):
            raise ValueError("technology evidence_refs must contain objects")
        return cls(
            ecosystem=str(payload.get("ecosystem") or ""),
            name=str(payload.get("name") or ""),
            version=str(payload.get("version") or ""),
            evidence_refs=tuple(
                EvidenceAnchor.from_dict(item)
                for item in refs
                if isinstance(item, Mapping)
            ),
        )


@dataclass(frozen=True)
class ComponentObservation:
    component_id: str
    path: str
    roles: tuple[str, ...]
    symbols: tuple[str, ...] = field(default_factory=tuple)
    technologies: tuple[TechnologyObservation, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        component_id = self.component_id.strip()
        if not component_id:
            raise ValueError("component_id is required")
        object.__setattr__(self, "component_id", component_id)
        object.__setattr__(self, "path", _relative_path(self.path))
        roles = tuple(
            sorted(
                {
                    normalize_domain_slug(role, "component role")
                    for role in self.roles
                }
            )
        )
        if not roles:
            raise ValueError("component requires at least one abstract role")
        object.__setattr__(self, "roles", roles)
        symbols = tuple(
            sorted({symbol.strip() for symbol in self.symbols if symbol.strip()})
        )
        object.__setattr__(self, "symbols", symbols)
        technologies = tuple(sorted(self.technologies, key=lambda item: item.identity))
        if len({item.identity for item in technologies}) != len(technologies):
            raise ValueError("duplicate component technology observation")
        object.__setattr__(self, "technologies", technologies)

    def to_dict(self) -> dict[str, object]:
        return {
            "component_id": self.component_id,
            "path": self.path,
            "roles": list(self.roles),
            "symbols": list(self.symbols),
            "technologies": [item.to_dict() for item in self.technologies],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ComponentObservation":
        unknown = set(payload) - {
            "component_id",
            "path",
            "roles",
            "symbols",
            "technologies",
        }
        if unknown:
            raise ValueError(
                "unknown component fields: " + ", ".join(sorted(unknown))
            )
        roles = require_sequence(payload.get("roles"), "component roles")
        symbols = require_sequence(payload.get("symbols", []), "component symbols")
        technologies = require_sequence(
            payload.get("technologies", []),
            "component technologies",
        )
        if any(not isinstance(item, Mapping) for item in technologies):
            raise ValueError("component technologies must contain objects")
        return cls(
            component_id=str(payload.get("component_id") or ""),
            path=str(payload.get("path") or ""),
            roles=tuple(str(item) for item in roles),
            symbols=tuple(str(item) for item in symbols),
            technologies=tuple(
                TechnologyObservation.from_dict(item)
                for item in technologies
                if isinstance(item, Mapping)
            ),
        )


@dataclass(frozen=True)
class DomainRelation:
    source_component_id: str
    relation: str
    target_component_id: str

    def __post_init__(self) -> None:
        if not self.source_component_id.strip() or not self.target_component_id.strip():
            raise ValueError("relation endpoints are required")
        object.__setattr__(
            self,
            "source_component_id",
            self.source_component_id.strip(),
        )
        object.__setattr__(
            self,
            "target_component_id",
            self.target_component_id.strip(),
        )
        object.__setattr__(
            self,
            "relation",
            normalize_domain_slug(self.relation, "relation"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "source_component_id": self.source_component_id,
            "relation": self.relation,
            "target_component_id": self.target_component_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "DomainRelation":
        unknown = set(payload) - {
            "source_component_id",
            "relation",
            "target_component_id",
        }
        if unknown:
            raise ValueError(
                "unknown relation fields: " + ", ".join(sorted(unknown))
            )
        return cls(
            source_component_id=str(payload.get("source_component_id") or ""),
            relation=str(payload.get("relation") or ""),
            target_component_id=str(payload.get("target_component_id") or ""),
        )
