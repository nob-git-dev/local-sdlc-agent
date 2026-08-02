"""Shared strict-input helpers for learning-runtime schemas."""

from __future__ import annotations

import json
import re
from typing import Mapping, Sequence

from sdlc_events import canonical_json


IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
TECHNOLOGY_NAME_PATTERN = re.compile(
    r"^@?[a-z0-9][a-z0-9+._:@/-]{0,127}$"
)


class KnowledgeValidationError(ValueError):
    """Raised when learning data cannot be mechanically interpreted."""


def require_mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise KnowledgeValidationError(f"{field} must be an object")
    return value


def require_sequence(value: object, field: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise KnowledgeValidationError(f"{field} must be an array")
    return value


def require_string(
    value: object,
    field: str,
    *,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise KnowledgeValidationError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized and not allow_empty:
        raise KnowledgeValidationError(f"{field} is required")
    return normalized


def require_choice(value: object, field: str, choices: set[str]) -> str:
    normalized = require_string(value, field)
    if normalized not in choices:
        raise KnowledgeValidationError(f"invalid {field}: {normalized}")
    return normalized


def require_slug(value: object, field: str) -> str:
    normalized = require_string(value, field).lower()
    if not SLUG_PATTERN.fullmatch(normalized):
        raise KnowledgeValidationError(f"{field} must be a normalized identifier")
    return normalized


def require_technology_name(value: object, field: str) -> str:
    normalized = require_string(value, field).lower()
    if (
        not TECHNOLOGY_NAME_PATTERN.fullmatch(normalized)
        or ".." in normalized
        or normalized.endswith("/")
    ):
        raise KnowledgeValidationError(f"{field} must be a package or runtime name")
    return normalized


def require_identifier(value: object, field: str) -> str:
    normalized = require_string(value, field)
    if not IDENTIFIER_PATTERN.fullmatch(normalized):
        raise KnowledgeValidationError(f"{field} must be an opaque identifier")
    return normalized


def json_object(value: object, field: str) -> dict[str, object]:
    payload = require_mapping(value, field)
    normalized = json.loads(canonical_json(payload))
    if not isinstance(normalized, dict):
        raise KnowledgeValidationError(f"{field} must be an object")
    return normalized


def json_objects(value: object, field: str) -> tuple[dict[str, object], ...]:
    result: list[dict[str, object]] = []
    for index, item in enumerate(require_sequence(value, field)):
        result.append(json_object(item, f"{field}[{index}]"))
    return tuple(result)


def json_values(value: object, field: str) -> tuple[object, ...]:
    values = tuple(require_sequence(value, field))
    for index, item in enumerate(values):
        if not isinstance(item, (str, Mapping)):
            raise KnowledgeValidationError(
                f"{field}[{index}] must be a string or object"
            )
    normalized = tuple(json.loads(canonical_json(item)) for item in values)
    return tuple(sorted(normalized, key=canonical_json))


def string_values(value: object, field: str) -> tuple[str, ...]:
    result = [
        require_string(item, f"{field}[]")
        for item in require_sequence(value, field)
    ]
    return tuple(sorted(set(result)))


def identifier_values(value: object, field: str) -> tuple[str, ...]:
    result = [
        require_identifier(item, f"{field}[]")
        for item in require_sequence(value, field)
    ]
    return tuple(sorted(set(result)))
