"""Minimum shared-store redaction applied before cross-project persistence."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping

from sdlc_events.models import json_safe


SECRET_KEY_FRAGMENTS = (
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
)
EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
IPV4_PATTERN = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
HOME_PATTERN = re.compile(r"/(?:home|Users)/[^/\s]+")


def _sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(fragment in lowered for fragment in SECRET_KEY_FRAGMENTS)


def _sanitize_string(value: str) -> str:
    if Path(value).is_absolute():
        return "<redacted-absolute-path>"
    text = HOME_PATTERN.sub("/<redacted-home>", value)
    text = EMAIL_PATTERN.sub("<redacted-email>", text)
    text = IPV4_PATTERN.sub("<redacted-ip>", text)
    return text


def sanitize_shared(value: object, *, key: str = "") -> object:
    value = json_safe(value)
    if _sensitive_key(key):
        return "<redacted-secret>"
    if isinstance(value, str):
        return _sanitize_string(value)
    if isinstance(value, Mapping):
        return {
            str(item_key): sanitize_shared(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [sanitize_shared(item) for item in value]
    return value


def sensitive_values(value: object) -> list[str]:
    """Return reasons why a shared-store value is still unsafe."""
    reasons: list[str] = []

    def visit(item: object, key: str = "") -> None:
        if _sensitive_key(key) and item != "<redacted-secret>":
            reasons.append(f"secret_key:{key}")
        if isinstance(item, str):
            if Path(item).is_absolute():
                reasons.append("absolute_path")
            if EMAIL_PATTERN.search(item):
                reasons.append("email")
            if IPV4_PATTERN.search(item):
                reasons.append("ipv4")
        elif isinstance(item, Mapping):
            for child_key, child in item.items():
                visit(child, str(child_key))
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return sorted(set(reasons))
