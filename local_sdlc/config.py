"""Project configuration loading for Local SDLC Agent.

The runner intentionally stays stdlib-only. JSON is the preferred machine
format; YAML support is deliberately small and limited to the shapes used by
``local_sdlc.yaml``.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

from .models import RunnerError


CONFIG_FILENAMES = ("local_sdlc.json", "local_sdlc.yaml", "local_sdlc.yml")


@dataclasses.dataclass(frozen=True)
class AppConfig:
    path: Path | None
    root: dict[str, Any]
    llm: dict[str, Any]


def discover_config_file(project: Path, explicit: Path | None = None) -> Path | None:
    """Return the explicit or first project-local config path."""
    if explicit is not None:
        candidate = explicit.expanduser()
        if not candidate.is_absolute():
            candidate = project / candidate
        candidate = candidate.resolve()
        if not candidate.exists():
            raise RunnerError(f"config file not found: {candidate}")
        if not candidate.is_file():
            raise RunnerError(f"config path is not a file: {candidate}")
        return candidate

    for name in CONFIG_FILENAMES:
        candidate = project / name
        if candidate.is_file():
            return candidate.resolve()
    return None


def load_app_config(project: Path, explicit: Path | None = None) -> AppConfig:
    path = discover_config_file(project, explicit)
    if path is None:
        return AppConfig(path=None, root={}, llm={})
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RunnerError(f"could not read config file: {path}: {exc}") from exc

    suffix = path.suffix.lower()
    try:
        if suffix == ".json":
            parsed = json.loads(text)
        elif suffix in {".yaml", ".yml"}:
            parsed = parse_simple_yaml(text)
        else:
            raise RunnerError("config file must be .json, .yaml, or .yml")
    except json.JSONDecodeError as exc:
        raise RunnerError(f"invalid JSON config: {path}: {exc}") from exc

    if not isinstance(parsed, dict):
        raise RunnerError(f"config file must contain an object: {path}")
    llm = parsed.get("llm", parsed)
    if llm is None:
        llm = {}
    if not isinstance(llm, dict):
        raise RunnerError("config key 'llm' must be an object")
    return AppConfig(path=path, root=dict(parsed), llm=dict(llm))


def parse_simple_yaml(text: str) -> dict[str, Any]:
    """Parse a small YAML subset: mappings, nested mappings, and scalar lists."""
    logical_lines: list[tuple[int, str, int]] = []
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        if "\t" in raw_line[: len(raw_line) - len(raw_line.lstrip())]:
            raise RunnerError(f"YAML indentation must use spaces, line {lineno}")
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        logical_lines.append((indent, stripped, lineno))

    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any] | list[Any]]] = [(-1, root)]

    def next_is_list(index: int, indent: int) -> bool:
        if index + 1 >= len(logical_lines):
            return False
        next_indent, next_text, _next_lineno = logical_lines[index + 1]
        return next_indent > indent and next_text.startswith("- ")

    for index, (indent, line, lineno) in enumerate(logical_lines):
        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            raise RunnerError(f"invalid YAML indentation at line {lineno}")
        parent = stack[-1][1]

        if line.startswith("- "):
            if not isinstance(parent, list):
                raise RunnerError(f"YAML list item without list key at line {lineno}")
            parent.append(parse_yaml_scalar(line[2:].strip()))
            continue

        if ":" not in line:
            raise RunnerError(f"YAML line must use key: value syntax at line {lineno}")
        key, raw_value = line.split(":", 1)
        key = key.strip()
        if not key:
            raise RunnerError(f"empty YAML key at line {lineno}")
        if not isinstance(parent, dict):
            raise RunnerError(f"YAML mapping entry under list is not supported at line {lineno}")
        value = raw_value.strip()
        if value:
            parent[key] = parse_yaml_scalar(value)
            continue
        child: dict[str, Any] | list[Any] = [] if next_is_list(index, indent) else {}
        parent[key] = child
        stack.append((indent, child))
    return root


def parse_yaml_scalar(value: str) -> Any:
    if not value:
        return ""
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    lowered = value.lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"null", "none", "~"}:
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def config_value(section: dict[str, Any], *names: str) -> Any:
    for name in names:
        variants = {name, name.replace("-", "_"), name.replace("_", "-")}
        for variant in variants:
            if variant in section:
                return section[variant]
    return None


def config_string(section: dict[str, Any], *names: str) -> str | None:
    value = config_value(section, *names)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def config_number(section: dict[str, Any], *names: str, number_type: type = float) -> int | float | None:
    value = config_value(section, *names)
    if value is None:
        return None
    try:
        return number_type(value)
    except (TypeError, ValueError) as exc:
        joined = "/".join(names)
        raise RunnerError(f"config value {joined} must be {number_type.__name__}") from exc


def config_bool(section: dict[str, Any], *names: str) -> bool | None:
    value = config_value(section, *names)
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    joined = "/".join(names)
    raise RunnerError(f"config value {joined} must be boolean")


def config_string_list(section: dict[str, Any], *names: str) -> list[str] | None:
    value = config_value(section, *names)
    if value is None:
        return None
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value).replace(",", "\n").splitlines() if item.strip()]
