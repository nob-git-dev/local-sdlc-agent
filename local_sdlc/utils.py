"""Small shared helpers for the local SDLC runner."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Sequence

def compact_preview(value: object, limit: int = 80) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."

def strip_markdown_fence(text: str) -> str:
    stripped = text.strip()
    match = re.fullmatch(r"```[a-zA-Z0-9_-]*\n(.*)\n```", stripped, flags=re.DOTALL)
    if match:
        return match.group(1)
    return stripped

def markdown_fenced_blocks(text: str) -> list[str]:
    return re.findall(r"```[a-zA-Z0-9_-]*\n(.*?)\n```", text, flags=re.DOTALL)

def truncate_text(text: str, limit: int = 20000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... truncated after {limit} chars ..."

def unique_ordered(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result

def write_run_document(run_dir: Path, filename: str, content: str) -> Path:
    path = run_dir / filename
    path.write_text(content.rstrip() + "\n", encoding="utf-8")
    return path

def display_path(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)
