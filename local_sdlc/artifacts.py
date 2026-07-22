"""Artifact protocol, repair advice, and stream-guard logic."""

from __future__ import annotations

import ast
from collections import Counter
import json
import os
import re
import subprocess
import struct
import textwrap
from pathlib import Path
from typing import Sequence

from .models import *
from .utils import markdown_fenced_blocks, strip_markdown_fence, truncate_text, unique_ordered
from .verification import parse_command_result_document
from .workspace import normalize_new_files, normalize_project_relative_paths, resolve_project_path


ARTIFACT_OUTPUT_BUDGET_BYTES = 6000
ROOT_CAUSE_OUTPUT_BUDGET_BYTES = 8192


JSON_SEARCH_REPLACE_PATTERN = re.compile(
    r'\{\s*"type"\s*:\s*"search_replace"\s*,\s*'
    r'"path"\s*:\s*"(?P<path>(?:\\.|[^"\\])*)"\s*,\s*'
    r'"search"\s*:\s*"(?P<search>(?:\\.|[^"\\])*)"\s*,\s*'
    r'"replace"\s*:\s*"(?P<replace>(?:\\.|[^"\\])*)"\s*\}',
    flags=re.DOTALL,
)


SEARCH_REPLACE_END_MARKER = r">{7,}\s+REPLACE"
SEARCH_REPLACE_END_MARKER_OR_SEARCH = r">{7,}\s+(?:REPLACE|SEARCH)"


VALID_SEARCH_REPLACE_PATTERN = re.compile(
    rf"^\s*BEGIN_SEARCH_REPLACE:\s*(?P<path>[^\n]+)\n\s*<<<<<<< SEARCH\n(?P<search>.*?)\n\s*=======\n(?P<replace>.*?)\n\s*{SEARCH_REPLACE_END_MARKER}(?:\n\s*END_SEARCH_REPLACE)?\s*$",
    flags=re.DOTALL | re.MULTILINE,
)


MALFORMED_SEARCH_REPLACE_WITHOUT_PATH_PATTERN = re.compile(
    r"(?m)^\s*BEGIN_SEARCH_REPLACE\s*(?:\n|$)"
)

PY_FUNCTION_REPLACE_PREFIX = "__PY_FUNCTION_REPLACE__:"

def extract_unified_diff(text: str) -> str:
    fenced_blocks = re.findall(r"```(?:diff|patch)?\n(.*?)\n```", text, flags=re.DOTALL)
    for block in fenced_blocks:
        if "diff --git " in block or ("\n--- " in "\n" + block and "\n+++ " in "\n" + block):
            return block.strip() + "\n"

    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.startswith("diff --git ") or line.startswith("--- "):
            start = index
            break
    if start is None:
        raise RunnerError("LLM output did not contain a unified diff")

    kept: list[str] = []
    for line in lines[start:]:
        if line.startswith("```"):
            break
        kept.append(line)
    patch = "\n".join(kept).strip()
    if not patch:
        raise RunnerError("LLM output contained an empty diff")
    return patch + "\n"

def changed_paths_from_unified_diff(patch: str) -> list[str]:
    paths: list[str] = []
    for line in patch.splitlines():
        if not line.startswith("+++ "):
            continue
        raw = line[4:].strip().split("\t", 1)[0]
        if raw == "/dev/null":
            continue
        if raw.startswith("b/"):
            raw = raw[2:]
        path = Path(raw)
        if path.is_absolute() or ".." in path.parts:
            continue
        paths.append(raw)
    return unique_ordered(paths)

def missing_changed_paths_after_patch(project: Path, paths: Sequence[str]) -> list[str]:
    return [
        path
        for path in unique_ordered(paths)
        if not resolve_project_path(project, path).exists()
    ]

def normalize_file_header_search_replace_artifacts(text: str) -> str:
    """Normalize `BEGIN_SEARCH_REPLACE` + `File: path` fenced variants.

    Some local models emit a mechanically recoverable form:
    BEGIN_SEARCH_REPLACE, a separate File/Path line, then a fenced conflict
    block. This function changes only the artifact envelope; the search and
    replacement payloads are preserved verbatim.
    """
    pattern = re.compile(
        r"(?ms)^\s*BEGIN_SEARCH_REPLACE\s*\n"
        r"\s*(?:File|Path)\s*:\s*(?P<path>[^\n]+)\n"
        r"\s*```(?:[A-Za-z0-9_+.-]+)?\n"
        r"(?P<body>\s*<<<<<<< SEARCH\n.*?\n\s*>>>>>>> REPLACE)\n"
        r"\s*```\s*",
    )

    def replace(match: re.Match[str]) -> str:
        path = normalize_legacy_file_artifact_path(match.group("path")).strip("`'\" ")
        body = match.group("body").strip("\n")
        return f"BEGIN_SEARCH_REPLACE: {path}\n{body}\nEND_SEARCH_REPLACE\n"

    return pattern.sub(replace, text)

def artifact_candidate_texts(text: str) -> list[str]:
    normalized_search_replace = normalize_file_header_search_replace_artifacts(text)
    candidates = [text, normalized_search_replace, strip_markdown_fence(text), strip_markdown_fence(normalized_search_replace)]
    candidates.extend(markdown_fenced_blocks(text))
    candidates.extend(markdown_fenced_blocks(normalized_search_replace))
    return unique_ordered(candidate for candidate in candidates if candidate.strip())

def json_artifact_marker_offsets(text: str) -> list[int]:
    """Return offsets for JSON that plausibly starts an artifact payload.

    Plain prose and tracebacks often contain braces, for example
    ``{type(payload).__name__}``.  Treating every ``{`` or ``[`` as an
    artifact marker disables the stream guard after ordinary explanatory text.
    A JSON artifact must begin on a line and quickly declare either an
    ``artifacts`` envelope or an item with ``type`` and ``path`` keys.
    """
    offsets: list[int] = []
    for match in re.finditer(r"(?m)^\s*[\{\[]", text):
        start = match.start()
        window = text[start : start + 512]
        if '"artifacts"' in window or (
            re.search(r'"type"\s*:', window) and re.search(r'"path"\s*:', window)
        ):
            offsets.append(start)
    return offsets

def clean_artifact_block(text: str) -> str:
    stripped = strip_markdown_fence(text).strip("\n")
    return textwrap.dedent(stripped)

def contains_conflict_markers(text: str) -> bool:
    return bool(re.search(r"(?m)^(<<<<<<<|=======|>>>>>>>)", text))

def contains_artifact_markers(text: str) -> bool:
    return bool(
        re.search(
            r"(?m)^(BEGIN_(?:APPEND_)?FILE(?::|\s*$)|BEGIN_SEARCH_REPLACE:|END_(?:APPEND_)?FILE(?:\s*:\s*[^\n]+)?\s*$|END_SEARCH_REPLACE$)",
            text,
        )
    )

def absent_api_contracts_from_texts(texts: Sequence[str]) -> list[tuple[str, str]]:
    """Extract absent API facts from deterministic probes and policy advice.

    The runner uses these as executable constraints, not as suggestions.  A
    coder may choose a different product patch, but an artifact must not call
    or add an API that the supervisor has already classified as absent unless a
    later stage explicitly changes that contract.
    """
    contracts: list[tuple[str, str]] = []
    combined_items = [text for text in texts if text]
    combined = "\n".join(combined_items)
    explicitly_forbidden: set[tuple[str, str]] = set()
    for item in combined_items:
        if not re.search(r"(?i)\bforbidden by\b", item):
            continue
        explicitly_forbidden.update(
            (class_name, attr)
            for class_name, attr in re.findall(
                r"\b([A-Z][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b",
                item,
            )
        )
    for class_name, attr in re.findall(
        r"`([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)`\s+is absent",
        combined,
    ):
        contracts.append((class_name, attr))
    for item in combined_items:
        if not re.search(r"(?i)\bforbidden by\b|\bdo not add\b|\bdo not call\b|\babsent api\b", item):
            continue
        for class_name, attr in re.findall(
            r"\b([A-Z][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b",
            item,
        ):
            contracts.append((class_name, attr))
        for attr, _class_name in re.findall(
            r"(?i)\bdo not add\s+`?([A-Za-z_][A-Za-z0-9_]*)`?\s+to\s+`?([A-Z][A-Za-z0-9_]*)`?",
            item,
        ):
            contracts.append((_class_name, attr))
    filtered: list[tuple[str, str]] = []
    for class_name, attr in unique_ordered(contracts):
        product_restore_authorized = (
            f"Treat missing `{class_name}.{attr}` as a product API/call-site inconsistency"
            in combined
        )
        if product_restore_authorized and (class_name, attr) not in explicitly_forbidden:
            continue
        filtered.append((class_name, attr))
    return filtered

def forbidden_edit_symbols_from_texts(texts: Sequence[str]) -> list[str]:
    """Extract mechanically forbidden edit targets from supervisor advice.

    The runner treats these as guard rails for the next artifact only.  This is
    intentionally shallow: the LLM may decide what to fix, but if the current
    repair advice says "do not edit X", artifacts touching X are rejected before
    apply.
    """
    symbols: list[str] = []
    stopwords = {
        "and",
        "or",
        "the",
        "for",
        "a",
        "an",
        "output",
        "tests",
        "readme",
        "cli",
        "file",
        "files",
    }
    combined = "\n".join(text for text in texts if text)
    for match in re.finditer(r"(?i)\bdo not edit\s+(?P<targets>[^\n.]+)", combined):
        targets = match.group("targets")
        targets = re.split(
            r"(?i)\b(?:for|because|unless|when|while|during|after|before)\b",
            targets,
            maxsplit=1,
        )[0]
        for token in re.findall(r"`?([A-Za-z_][A-Za-z0-9_]*)`?", targets):
            if token.lower() in stopwords:
                continue
            symbols.append(token)
    return unique_ordered(symbols)

def normalize_legacy_file_artifact_path(raw_path: str) -> str:
    path = raw_path.strip()
    if path.lower().startswith("path:"):
        path = path.split(":", 1)[1].strip()
    if path.lower().startswith("path="):
        path = path.split("=", 1)[1].strip()
    if path.lower().startswith("path/"):
        path = path.split("/", 1)[1].strip()
    if path.startswith(":"):
        path = path[1:].strip()
    return path

def normalize_legacy_file_artifact_content(raw_content: str) -> str:
    content = strip_markdown_fence(raw_content)
    lines = content.splitlines()
    if lines and lines[0].strip() == "---":
        lines = lines[1:]
    if lines and lines[-1].strip() == "---":
        lines = lines[:-1]
    if not lines:
        return ""
    return "\n".join(lines) + ("\n" if content.endswith("\n") else "")

def legacy_file_begin_count(text: str) -> int:
    return len(re.findall(r"(?m)^BEGIN_(?:APPEND_)?FILE(?::|\s*$)", text))

def legacy_file_end_count(text: str) -> int:
    return len(re.findall(r"(?m)^END_(?:APPEND_)?FILE(?:\s*:\s*[^\n]+)?\s*$", text))

def _json_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    for candidate in artifact_candidate_texts(text):
        stripped = candidate.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            candidates.append(stripped)
    return unique_ordered(candidates)

def repaired_json_candidates(candidate: str) -> list[str]:
    repairs: list[str] = []
    fixed_type = re.sub(r'"type"\s*:\s*"type"\s*:\s*"', '"type":"', candidate)
    if fixed_type != candidate:
        repairs.append(fixed_type)
    fixed_commas = remove_json_trailing_commas(candidate)
    if fixed_commas != candidate:
        repairs.append(fixed_commas)
        fixed_both = re.sub(r'"type"\s*:\s*"type"\s*:\s*"', '"type":"', fixed_commas)
        if fixed_both != fixed_commas:
            repairs.append(fixed_both)
    return repairs

def remove_json_trailing_commas(candidate: str) -> str:
    """Remove JSON trailing commas before object/array closers.

    This is intentionally a syntax-only repair.  It tracks string literals so
    commas inside artifact content remain untouched, then removes only commas
    whose next non-whitespace byte is ``}`` or ``]``.
    """
    result: list[str] = []
    in_string = False
    escaped = False
    index = 0
    while index < len(candidate):
        char = candidate[index]
        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            result.append(char)
            index += 1
            continue
        if char == ",":
            lookahead = index + 1
            while lookahead < len(candidate) and candidate[lookahead] in " \t\r\n":
                lookahead += 1
            if lookahead < len(candidate) and candidate[lookahead] in "}]":
                index += 1
                continue
        result.append(char)
        index += 1
    return "".join(result)

def artifact_path_policy(paths: Sequence[str] | ArtifactPathPolicy) -> ArtifactPathPolicy:
    if isinstance(paths, ArtifactPathPolicy):
        return paths
    return ArtifactPathPolicy(allowed_paths=tuple(path for path in paths if path))

def merge_artifact_policy_paths(
    allowed_paths: Sequence[str],
    readonly_paths: Sequence[str],
    added_writable_paths: Sequence[str] = (),
    added_readonly_paths: Sequence[str] = (),
) -> tuple[list[str], list[str]]:
    """Merge artifact policy paths while keeping writable and readonly disjoint."""
    allowed = unique_ordered([*allowed_paths, *added_writable_paths])
    writable = set(allowed)
    readonly = unique_ordered(
        path
        for path in [*readonly_paths, *added_readonly_paths]
        if path not in writable
    )
    return allowed, readonly

def freeze_test_paths_as_readonly(
    allowed_paths: Sequence[str],
    readonly_paths: Sequence[str],
) -> tuple[list[str], list[str]]:
    """Move tests from writable targets to readonly evidence targets."""
    frozen = [path for path in allowed_paths if path.startswith("tests/")]
    allowed = [path for path in allowed_paths if not path.startswith("tests/")]
    readonly = unique_ordered([*readonly_paths, *frozen])
    return allowed, readonly

def demote_writable_paths_to_readonly(
    allowed_paths: Sequence[str],
    readonly_paths: Sequence[str],
    demoted_paths: Sequence[str],
) -> tuple[list[str], list[str]]:
    """Move selected writable paths to readonly evidence paths."""
    demoted = set(normalize_project_relative_paths(demoted_paths))
    if not demoted:
        return list(allowed_paths), list(readonly_paths)
    frozen = [path for path in allowed_paths if path in demoted]
    allowed = [path for path in allowed_paths if path not in demoted]
    readonly = unique_ordered([*readonly_paths, *frozen])
    return allowed, readonly

def check_artifact_path(path: str, policy_or_paths: Sequence[str] | ArtifactPathPolicy, kind: str) -> None:
    policy = artifact_path_policy(policy_or_paths)
    normalized = normalize_project_relative_paths([path], f"{kind} path")
    if not normalized:
        raise RunnerError(f"{kind} path is empty")
    safe_path = normalized[0]
    allowed = set(policy.allowed_paths)
    readonly = set(policy.readonly_paths)
    existing = set(policy.existing_paths)
    if safe_path in readonly:
        raise RunnerError(f"{kind} path is read-only: {safe_path}")
    if safe_path in allowed:
        return
    if policy.allow_extra_new_files and safe_path not in existing:
        return
    raise RunnerError(f"{kind} path is not allowed: {safe_path}")

def reject_duplicate_json_object_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON artifact key: {key}")
        result[key] = value
    return result

def extract_json_artifacts(
    text: str,
    allowed_paths: Sequence[str] | ArtifactPathPolicy,
) -> tuple[list[SearchReplaceArtifact], list[FileArtifact]]:
    """Parse structured JSON artifacts.

    Supported shapes:
    - {"artifacts": [{"type": "replace_file", "path": "...", "content": "..."}]}
    - [{"type": "search_replace", "path": "...", "search": "...", "replace": "..."}]
    - {"type": "append_file", "path": "...", "content": "..."}
    """
    last_error = "LLM output did not contain JSON artifacts"
    for candidate in _json_candidates(text):
        payload = None
        for json_candidate in [candidate, *repaired_json_candidates(candidate)]:
            try:
                payload = json.loads(
                    json_candidate,
                    object_pairs_hook=reject_duplicate_json_object_keys,
                )
                break
            except json.JSONDecodeError as exc:
                last_error = f"invalid JSON artifact: {exc}"
                continue
            except ValueError as exc:
                last_error = f"invalid JSON artifact: {exc}"
                continue
        if payload is None:
            continue

        if isinstance(payload, dict) and "artifacts" in payload:
            raw_items = payload["artifacts"]
        else:
            raw_items = payload

        if isinstance(raw_items, dict):
            items = [raw_items]
        elif isinstance(raw_items, list):
            items = raw_items
        else:
            raise RunnerError("JSON artifacts must be an object, list, or object with an artifacts list")

        replacements: list[SearchReplaceArtifact] = []
        files: list[FileArtifact] = []
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                raise RunnerError(f"JSON artifact #{index} must be an object")
            artifact_type = str(item.get("type", "")).strip()
            path = str(item.get("path", "")).strip()
            check_artifact_path(path, allowed_paths, "JSON artifact")

            if artifact_type in {"replace_file", "file"}:
                content = item.get("content")
                if not isinstance(content, str) or not content.strip():
                    raise RunnerError(f"JSON file artifact for {path} needs non-empty string content")
                files.append(FileArtifact(path=path, content=content, mode="replace"))
            elif artifact_type == "append_file":
                content = item.get("content")
                if not isinstance(content, str) or not content.strip():
                    raise RunnerError(f"JSON append artifact for {path} needs non-empty string content")
                if content and not content.startswith("\n"):
                    content = "\n" + content
                files.append(FileArtifact(path=path, content=content, mode="append"))
            elif artifact_type == "search_replace":
                search = item.get("search")
                replace = item.get("replace")
                if not isinstance(search, str) or not search:
                    raise RunnerError(f"JSON search_replace artifact for {path} needs non-empty search")
                if not isinstance(replace, str):
                    raise RunnerError(f"JSON search_replace artifact for {path} needs string replace")
                replacements.append(SearchReplaceArtifact(path=path, search=search, replace=replace))
            else:
                raise RunnerError(f"unknown JSON artifact type: {artifact_type}")

        if replacements or files:
            return replacements, files

    raise RunnerError(last_error)

def extract_file_artifact(text: str, allowed_paths: Sequence[str] | ArtifactPathPolicy) -> FileArtifact:
    artifacts = extract_file_artifacts(text, allowed_paths)
    if artifacts:
        return artifacts[0]
    raise RunnerError("LLM output did not contain an allowed file artifact")

def fenced_path_file_artifacts(text: str, allowed_paths: Sequence[str] | ArtifactPathPolicy) -> list[FileArtifact]:
    policy = artifact_path_policy(allowed_paths)
    artifacts: list[FileArtifact] = []
    seen: set[tuple[str, str]] = set()
    path_line_pattern = re.compile(
        r"^\s*(?:#|//|<!--|/\*)\s*(?:file|path)?\s*:?\s*"
        r"(?P<path>[A-Za-z0-9_.][A-Za-z0-9_./-]*\.[A-Za-z0-9_]+)"
        r"\s*(?:-->|\*/)?\s*$"
    )
    for block in markdown_fenced_blocks(text):
        lines = block.splitlines()
        first_index = next((index for index, line in enumerate(lines) if line.strip()), None)
        if first_index is None:
            continue
        match = path_line_pattern.match(lines[first_index])
        if not match:
            continue
        path = normalize_legacy_file_artifact_path(match.group("path"))
        check_artifact_path(path, policy, "fenced file artifact")
        content = "\n".join(lines[first_index + 1:]).rstrip() + "\n"
        if not content.strip():
            continue
        key = (path, content)
        if key in seen:
            continue
        seen.add(key)
        artifacts.append(FileArtifact(path=path, content=content, mode="replace"))
    return artifacts

def unclosed_file_artifacts(text: str, allowed_paths: Sequence[str] | ArtifactPathPolicy) -> list[FileArtifact]:
    """Recover BEGIN_FILE blocks when only END_FILE markers are missing.

    This is a mechanical normalization, not intent inference. It only accepts
    output that starts directly with file artifacts, has no END_FILE markers,
    and can be split unambiguously at the next BEGIN_FILE marker or EOF.
    """
    policy = artifact_path_policy(allowed_paths)
    pattern = re.compile(
        r"(?m)^BEGIN_(?P<mode>APPEND_)?FILE(?::\s*(?P<inline_path>[^\n]+)|\s*\n(?P<line_path>[^\n]+))\n"
    )
    artifacts: list[FileArtifact] = []
    seen: set[tuple[str, str, str]] = set()
    for candidate in artifact_candidate_texts(text):
        if legacy_file_begin_count(candidate) == 0 or legacy_file_end_count(candidate) != 0:
            continue
        matches = list(pattern.finditer(candidate))
        if not matches:
            continue
        if candidate[: matches[0].start()].strip():
            continue
        for index, match in enumerate(matches):
            next_start = matches[index + 1].start() if index + 1 < len(matches) else len(candidate)
            path = normalize_legacy_file_artifact_path(
                match.group("inline_path") or match.group("line_path") or ""
            )
            check_artifact_path(path, policy, "unclosed file artifact")
            mode = "append" if match.group("mode") else "replace"
            content = normalize_legacy_file_artifact_content(candidate[match.end() : next_start])
            if not content.strip():
                raise RunnerError(f"file artifact for {path} is empty; include complete file content")
            if contains_artifact_markers(content):
                raise RunnerError(f"file artifact for {path} contains nested artifact markers")
            if mode == "append" and content and not content.startswith("\n"):
                content = "\n" + content
            key = (path, content, mode)
            if key in seen:
                continue
            seen.add(key)
            artifacts.append(FileArtifact(path=path, content=content, mode=mode))
        if artifacts and len(artifacts) == len(matches):
            return artifacts
    return []

def extract_file_artifacts(text: str, allowed_paths: Sequence[str] | ArtifactPathPolicy) -> list[FileArtifact]:
    policy = artifact_path_policy(allowed_paths)
    allowed = [path for path in policy.allowed_paths if path]
    artifacts: list[FileArtifact] = []
    seen_artifacts: set[tuple[str, str, str]] = set()
    empty_errors: list[str] = []
    begin_patterns = [
        re.compile(
            r"^BEGIN_(?P<mode>APPEND_)?FILE:\s*(?P<path>[^\n]+)\n(?P<content>.*?)\nEND_(?:APPEND_)?FILE(?:\s*:\s*[^\n]+)?\s*$",
            flags=re.DOTALL | re.MULTILINE,
        ),
        re.compile(
            r"^BEGIN_(?P<mode>APPEND_)?FILE\s*\n(?P<path>[^\n]+)\n(?P<content>.*?)\nEND_(?:APPEND_)?FILE(?:\s*:\s*[^\n]+)?\s*$",
            flags=re.DOTALL | re.MULTILINE,
        ),
    ]
    for candidate in artifact_candidate_texts(text):
        for begin_match in [match for pattern in begin_patterns for match in pattern.finditer(candidate)]:
            path = normalize_legacy_file_artifact_path(begin_match.group("path"))
            check_artifact_path(path, policy, "file artifact")
            mode = "append" if begin_match.group("mode") else "replace"
            content = normalize_legacy_file_artifact_content(begin_match.group("content"))
            if not content.strip():
                empty_errors.append(f"file artifact for {path} is empty; include complete file content")
                continue
            if mode == "append" and content and not content.startswith("\n"):
                content = "\n" + content
            key = (path, content, mode)
            if key in seen_artifacts:
                continue
            seen_artifacts.add(key)
            artifacts.append(FileArtifact(path=path, content=content, mode=mode))
    if artifacts:
        return artifacts

    recovered_unclosed = unclosed_file_artifacts(text, policy)
    if recovered_unclosed:
        return recovered_unclosed

    if search_replace_fenced_single_function_artifacts(text, policy):
        return []

    recovered_malformed_search_replace_file = malformed_search_replace_full_file_artifacts(text, policy)
    if recovered_malformed_search_replace_file:
        return recovered_malformed_search_replace_file

    for candidate in artifact_candidate_texts(text):
        loose_match = re.search(
            r"^BEGIN_(?P<mode>APPEND_)?FILE(?::\s*(?P<inline_path>[^\n]+)|\s*\n(?P<line_path>[^\n]+))\n(?P<body>.*)$",
            candidate,
            flags=re.DOTALL | re.MULTILINE,
        )
        if loose_match:
            path = normalize_legacy_file_artifact_path(
                loose_match.group("inline_path") or loose_match.group("line_path") or ""
            )
            check_artifact_path(path, policy, "file artifact")
            mode = "append" if loose_match.group("mode") else "replace"
            body = loose_match.group("body")
            # Some small local models emit END_FILE immediately after the path and
            # put the actual source after it. Salvage that single-file case.
            if re.fullmatch(r"\s*END_(?:APPEND_)?FILE(?:\s*:\s*[^\n]+)?\s*", body):
                body = ""
            elif re.match(r"^\s*END_(?:APPEND_)?FILE(?:\s*:\s*[^\n]+)?\s*\n", body):
                body = re.sub(r"^\s*END_(?:APPEND_)?FILE(?:\s*:\s*[^\n]+)?\s*\n", "", body, count=1)
            else:
                body = re.split(r"\nEND_(?:APPEND_)?FILE(?:\s*:\s*[^\n]+)?\s*$", body, maxsplit=1)[0]
            content = normalize_legacy_file_artifact_content(body)
            if not content.strip():
                raise RunnerError(
                    empty_errors[0] if empty_errors else f"file artifact for {path} is empty; include complete file content"
                )
            if mode == "append" and content and not content.startswith("\n"):
                content = "\n" + content
            return [FileArtifact(path=path, content=content, mode=mode)]

    if empty_errors:
        raise RunnerError(empty_errors[0])

    fenced_artifacts = fenced_path_file_artifacts(text, policy)
    if fenced_artifacts:
        return fenced_artifacts

    if len(allowed) == 1:
        path = allowed[0]
        fenced_blocks = markdown_fenced_blocks(text)
        for block in fenced_blocks:
            candidate = block.strip()
            if contains_artifact_markers(candidate) or re.search(r"(?m)^\s*<<<<<<< SEARCH\b", candidate):
                continue
            if "<!DOCTYPE html" in candidate or "<html" in candidate.lower():
                return [FileArtifact(path=path, content=candidate + "\n")]

        for candidate_text in artifact_candidate_texts(text):
            candidate = strip_markdown_fence(candidate_text)
            if contains_artifact_markers(candidate) or re.search(r"(?m)^\s*<<<<<<< SEARCH\b", candidate):
                continue
            if "<!DOCTYPE html" in candidate or "<html" in candidate.lower():
                return [FileArtifact(path=path, content=candidate.rstrip() + "\n")]

    return []

def malformed_search_replace_full_file_artifacts(
    text: str,
    allowed_paths: Sequence[str] | ArtifactPathPolicy,
) -> list[FileArtifact]:
    """Recover a narrow malformed header where the body is one full file.

    Local models sometimes obey the target path but use a
    BEGIN_SEARCH_REPLACE header before emitting a whole Python module. This is
    recoverable only when there are no conflict markers, the optional duplicate
    path matches the header path, and the body parses as a complete Python file.
    """
    policy = artifact_path_policy(allowed_paths)
    pattern = re.compile(
        r"^\s*BEGIN_SEARCH_REPLACE:\s*(?P<path>[^\n]+)\n"
        r"(?:\s*:\s*(?P<duplicate_path>[^\n]+)\n)?"
        r"(?P<body>.*?)\s*\Z",
        flags=re.DOTALL | re.MULTILINE,
    )
    artifacts: list[FileArtifact] = []
    seen: set[tuple[str, str]] = set()
    for candidate in artifact_candidate_texts(text):
        for match in pattern.finditer(candidate):
            body = match.group("body")
            if contains_conflict_markers(body) or "=======" in body or "Replace with:" in body:
                continue
            path = normalize_legacy_file_artifact_path(match.group("path"))
            duplicate_path = normalize_legacy_file_artifact_path(match.group("duplicate_path") or "")
            if duplicate_path and duplicate_path != path:
                continue
            check_artifact_path(path, policy, "malformed search/replace full-file recovery")
            if not path.endswith(".py"):
                continue
            content = normalize_legacy_file_artifact_content(strip_markdown_fence(body)).rstrip() + "\n"
            if looks_like_python_function_fragment(content):
                continue
            if not looks_like_complete_python_module(content):
                continue
            key = (path, content)
            if key in seen:
                continue
            seen.add(key)
            artifacts.append(FileArtifact(path=path, content=content, mode="replace"))
    return artifacts

def search_replace_fenced_single_function_artifacts(
    text: str,
    allowed_paths: Sequence[str] | ArtifactPathPolicy,
) -> list[SearchReplaceArtifact]:
    """Return loose SEARCH_REPLACE fenced blocks that are one Python function.

    A malformed ``BEGIN_SEARCH_REPLACE`` header followed by a fenced function is
    a narrower, safer interpretation than treating the same body as a whole-file
    replacement.  This predicate lets file-artifact recovery defer to the
    function-level recovery instead of producing two artifact interpretations
    for the same path.
    """
    artifacts: list[SearchReplaceArtifact] = []
    for artifact in loose_python_function_replacement_artifacts(text, allowed_paths):
        if looks_like_single_python_function_block(artifact.replace):
            artifacts.append(artifact)
    return artifacts

def looks_like_single_python_function_block(content: str) -> bool:
    try:
        module = ast.parse(textwrap.dedent(content))
    except SyntaxError:
        return False
    return len(module.body) == 1 and isinstance(module.body[0], (ast.FunctionDef, ast.AsyncFunctionDef))

def looks_like_python_function_fragment(content: str) -> bool:
    try:
        module = ast.parse(textwrap.dedent(content))
    except SyntaxError:
        return False
    return bool(module.body) and all(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) for node in module.body)

def normalized_single_python_function_block(content: str) -> str | None:
    stripped = strip_markdown_fence(content).strip("\n")
    dedented = textwrap.dedent(stripped)
    if looks_like_single_python_function_block(dedented):
        return dedented

    lines = dedented.splitlines()
    first_def_index: int | None = None
    for index, line in enumerate(lines):
        if re.match(r"^[ \t]*def\s+[A-Za-z_][A-Za-z0-9_]*\s*\(", line):
            first_def_index = index
            break
    if first_def_index is None:
        return None

    repaired: list[str] = []
    for index, line in enumerate(lines[first_def_index:], start=first_def_index):
        if index == first_def_index or not line.strip():
            repaired.append(line)
        else:
            repaired.append("    " + line)
    candidate = "\n".join(repaired).rstrip("\n") + "\n"
    if looks_like_single_python_function_block(candidate):
        return candidate
    return None

def function_replacement_fallback_name(search: str, replace: str) -> tuple[str, str] | None:
    search_name = python_function_name_from_block(search)
    replace_name = python_function_name_from_block(replace)
    if not search_name or search_name != replace_name:
        return None
    normalized_replace = normalized_single_python_function_block(replace)
    if normalized_replace is None:
        return None
    return search_name, normalized_replace

def looks_like_complete_python_module(content: str) -> bool:
    stripped = content.strip()
    if not stripped:
        return False
    if not (
        stripped.startswith(('"""', "'''"))
        or re.match(r"(?m)^(from\s+\S+\s+import\s+|import\s+\S+|def\s+\w+\s*\(|class\s+\w+)", stripped)
    ):
        return False
    if not re.search(r"(?m)^(def\s+\w+\s*\(|class\s+\w+)", stripped):
        return False
    try:
        ast.parse(content)
    except SyntaxError:
        return False
    return True

def extract_search_replace_artifact(text: str, allowed_paths: Sequence[str] | ArtifactPathPolicy) -> SearchReplaceArtifact:
    artifacts = extract_search_replace_artifacts(text, allowed_paths)
    if artifacts:
        return artifacts[0]
    raise RunnerError("LLM output did not contain a search/replace artifact")

def extract_search_replace_artifacts(text: str, allowed_paths: Sequence[str] | ArtifactPathPolicy) -> list[SearchReplaceArtifact]:
    policy = artifact_path_policy(allowed_paths)
    multi_pair_artifacts = multi_pair_search_replace_artifacts(text, policy)
    if multi_pair_artifacts:
        return multi_pair_artifacts
    pattern = re.compile(
        rf"^\s*BEGIN_SEARCH_REPLACE:\s*(?P<path>[^\n]+)\n\s*<<<<<<< SEARCH\n(?P<search>.*?)\n\s*=======\n(?P<replace>.*?)\n\s*{SEARCH_REPLACE_END_MARKER}(?:\n\s*END_SEARCH_REPLACE)?(?=\s*(?:\n\s*BEGIN_SEARCH_REPLACE:|\Z))",
        flags=re.DOTALL | re.MULTILINE,
    )
    artifacts: list[SearchReplaceArtifact] = []
    seen: set[tuple[str, str, str]] = set()
    for candidate in artifact_candidate_texts(text):
        for match in pattern.finditer(candidate):
            path = normalize_legacy_file_artifact_path(match.group("path"))
            check_artifact_path(path, policy, "search/replace")
            artifact = SearchReplaceArtifact(
                path=path,
                search=clean_artifact_block(match.group("search")),
                replace=clean_artifact_block(match.group("replace")),
            )
            key = (artifact.path, artifact.search, artifact.replace)
            if key in seen:
                continue
            seen.add(key)
            artifacts.append(artifact)
    if artifacts:
        return artifacts
    fenced_search_replace_artifacts = fenced_conflict_search_replace_artifacts(text, policy)
    if fenced_search_replace_artifacts:
        return fenced_search_replace_artifacts
    loose_artifacts = loose_python_function_replacement_artifacts(text, policy)
    if loose_artifacts:
        return loose_artifacts
    raise RunnerError("LLM output did not contain a search/replace artifact")

def multi_pair_search_replace_artifacts(
    text: str,
    allowed_paths: Sequence[str] | ArtifactPathPolicy,
) -> list[SearchReplaceArtifact]:
    """Recover repeated SEARCH/REPLACE pairs under one path header.

    Some local models correctly provide a safe path and exact conflict-marker
    pairs but omit END_SEARCH_REPLACE between edits. This normalization is
    mechanical: every pair must use the standard markers, and every edit keeps
    the explicit path from the enclosing BEGIN_SEARCH_REPLACE line.
    """
    policy = artifact_path_policy(allowed_paths)
    block_pattern = re.compile(
        r"(?ms)^\s*BEGIN_SEARCH_REPLACE:\s*(?P<path>[^\n]+)\n(?P<body>.*?)(?=^\s*BEGIN_SEARCH_REPLACE:|\Z)"
    )
    pair_pattern = re.compile(
        rf"(?ms)^\s*<<<<<<< SEARCH\n(?P<search>.*?)\n\s*=======\n(?P<replace>.*?)\n\s*{SEARCH_REPLACE_END_MARKER_OR_SEARCH}"
    )
    artifacts: list[SearchReplaceArtifact] = []
    seen: set[tuple[str, str, str]] = set()
    saw_multi_pair_block = False
    for candidate in artifact_candidate_texts(text):
        for block_match in block_pattern.finditer(candidate):
            path = normalize_legacy_file_artifact_path(block_match.group("path"))
            check_artifact_path(path, policy, "multi-pair search/replace")
            body = block_match.group("body")
            pairs = list(pair_pattern.finditer(body))
            if len(pairs) > 1:
                saw_multi_pair_block = True
            if not pairs:
                continue
            for pair in pairs:
                search = clean_artifact_block(pair.group("search"))
                replace = clean_artifact_block(pair.group("replace"))
                if contains_conflict_markers(search) or contains_conflict_markers(replace):
                    continue
                artifact = SearchReplaceArtifact(
                    path=path,
                    search=search,
                    replace=replace,
                )
                key = (artifact.path, artifact.search, artifact.replace)
                if key in seen:
                    continue
                seen.add(key)
                artifacts.append(artifact)
    if not saw_multi_pair_block:
        return []
    return artifacts

def fenced_conflict_search_replace_artifacts(
    text: str,
    allowed_paths: Sequence[str] | ArtifactPathPolicy,
) -> list[SearchReplaceArtifact]:
    """Recover fenced SEARCH/REPLACE grammar when the fence is only a wrapper.

    This is safe normalization, not intent inference: the path must be unique
    and safe, and the fenced payload must contain exactly the standard
    SEARCH/REPLACE markers.
    """
    policy = artifact_path_policy(allowed_paths)
    pattern = re.compile(
        r"^\s*BEGIN_SEARCH_REPLACE:\s*(?P<path>[^\n]+)\n"
        r"\s*```(?:python|py|text)?\s*\n"
        r"\s*<<<<<<< SEARCH\n(?P<search>.*?)\n"
        r"\s*=======\n(?P<replace>.*?)\n"
        rf"\s*{SEARCH_REPLACE_END_MARKER}\n"
        r"\s*```\s*(?:\n\s*END_SEARCH_REPLACE)?\s*$",
        flags=re.DOTALL | re.MULTILINE,
    )
    artifacts: list[SearchReplaceArtifact] = []
    seen: set[tuple[str, str, str]] = set()
    for candidate in artifact_candidate_texts(text):
        for match in pattern.finditer(candidate):
            path = normalize_legacy_file_artifact_path(match.group("path"))
            check_artifact_path(path, policy, "fenced search/replace")
            artifact = SearchReplaceArtifact(
                path=path,
                search=clean_artifact_block(match.group("search")),
                replace=clean_artifact_block(match.group("replace")),
            )
            key = (artifact.path, artifact.search, artifact.replace)
            if key in seen:
                continue
            seen.add(key)
            artifacts.append(artifact)
    return artifacts

def loose_python_function_replacement_artifacts(
    text: str,
    allowed_paths: Sequence[str] | ArtifactPathPolicy,
) -> list[SearchReplaceArtifact]:
    """Recover a common local-LLM format slip: path plus fenced Python function.

    This is intentionally narrow. The actual existing function is located only
    during apply, where the project files are available, and replacement occurs
    only when one matching function block exists.
    """
    policy = artifact_path_policy(allowed_paths)
    artifacts: list[SearchReplaceArtifact] = []
    seen: set[tuple[str, str]] = set()
    pattern = re.compile(
        r"^\s*BEGIN_SEARCH_REPLACE:\s*(?P<path>[^\n]+)\n"
        r"(?:\s*:\s*(?P<duplicate_path>[^\n]+)\n)?"
        r"\s*```(?:python|py)?\s*\n(?P<code>.*?)\n```\s*$",
        flags=re.DOTALL | re.MULTILINE,
    )
    for candidate in artifact_candidate_texts(text):
        for match in pattern.finditer(candidate):
            path = normalize_legacy_file_artifact_path(match.group("path"))
            duplicate_path = normalize_legacy_file_artifact_path(match.group("duplicate_path") or "")
            if duplicate_path and duplicate_path != path:
                continue
            check_artifact_path(path, policy, "loose Python function replacement")
            if not path.endswith(".py"):
                continue
            code = textwrap.dedent(match.group("code")).strip("\n") + "\n"
            if contains_conflict_markers(code):
                continue
            if not looks_like_single_python_function_block(code):
                continue
            function_name = python_function_name_from_block(code)
            if not function_name:
                continue
            key = (path, code)
            if key in seen:
                continue
            seen.add(key)
            artifacts.append(
                SearchReplaceArtifact(
                    path=path,
                    search=f"{PY_FUNCTION_REPLACE_PREFIX}{function_name}",
                    replace=code,
                )
            )
    return artifacts

def python_function_name_from_block(code: str) -> str | None:
    match = re.search(r"(?m)^[ \t]*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", code)
    return match.group(1) if match else None

def reindent_python_function_block(replacement: str, function_name: str, target_indent: str) -> str:
    """Align a loose fenced function body to the matched function's indentation.

    Local LLMs often emit a class method inside a fenced block. Earlier recovery
    dedented that block and accidentally moved methods to module scope. Rebase
    the emitted function to the indentation of the existing function before
    applying it.
    """
    lines = replacement.rstrip("\n").splitlines()
    if not lines:
        raise RunnerError(f"function replacement for `{function_name}` is empty")
    def_pattern = re.compile(rf"^(?P<indent>[ \t]*)def\s+{re.escape(function_name)}\s*\(")
    first_def_index: int | None = None
    source_indent = ""
    for index, line in enumerate(lines):
        match = def_pattern.match(line)
        if match:
            first_def_index = index
            source_indent = match.group("indent")
            break
    if first_def_index is None:
        raise RunnerError(f"function replacement does not define `{function_name}`")
    if lines[:first_def_index] and any(line.strip() for line in lines[:first_def_index]):
        raise RunnerError(f"function replacement for `{function_name}` contains non-empty text before def")

    function_lines = lines[first_def_index:]
    sibling_def_pattern = re.compile(rf"^{re.escape(source_indent)}def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")
    for offset, line in enumerate(function_lines[1:], start=1):
        sibling_match = sibling_def_pattern.match(line)
        if sibling_match:
            function_lines = function_lines[:offset]
            break

    rebased: list[str] = []
    for line in function_lines:
        if not line.strip():
            rebased.append("")
            continue
        if source_indent:
            if not line.startswith(source_indent):
                raise RunnerError(
                    f"function replacement for `{function_name}` has inconsistent indentation"
                )
            rebased.append(target_indent + line[len(source_indent):])
        else:
            rebased.append(target_indent + line)
    return "\n".join(rebased).rstrip("\n") + "\n"

def apply_search_replace_artifact(project: Path, artifact: SearchReplaceArtifact, run_dir: Path, round_index: int) -> str:
    target = resolve_project_path(project, artifact.path)
    if not target.exists():
        raise RunnerError(f"search/replace target does not exist: {artifact.path}")
    if artifact.search.startswith(PY_FUNCTION_REPLACE_PREFIX):
        function_name = artifact.search[len(PY_FUNCTION_REPLACE_PREFIX):]
        return apply_python_function_replacement(project, artifact.path, function_name, artifact.replace, run_dir, round_index)
    if contains_conflict_markers(artifact.replace):
        raise RunnerError(f"replacement for {artifact.path} contains conflict markers")
    if contains_artifact_markers(artifact.replace):
        raise RunnerError(f"replacement for {artifact.path} contains artifact markers")
    if artifact.search == artifact.replace:
        raise RunnerError(f"replacement for {artifact.path} is identical to the search text")
    text = target.read_text(encoding="utf-8", errors="replace")
    occurrences = text.count(artifact.search)
    if occurrences != 1:
        fallback = function_replacement_fallback_name(artifact.search, artifact.replace)
        if fallback is not None:
            function_name, normalized_replace = fallback
            return apply_python_function_replacement(
                project,
                artifact.path,
                function_name,
                normalized_replace,
                run_dir,
                round_index,
            )
        raise RunnerError(
            f"search text must occur exactly once in {artifact.path}; found {occurrences}"
        )
    backup = run_dir / f"backup-r{round_index:02d}-{Path(artifact.path).name}"
    backup.write_text(text, encoding="utf-8")
    target.write_text(text.replace(artifact.search, artifact.replace, 1), encoding="utf-8")
    return textwrap.dedent(
        f"""
        ## Search Replace Apply Result

        PASS: replaced text in `{artifact.path}`
        - search_bytes: {len(artifact.search.encode("utf-8"))}
        - replace_bytes: {len(artifact.replace.encode("utf-8"))}
        - backup: `{backup.name}`
        """
    ).strip()

def apply_python_function_replacement(
    project: Path,
    path: str,
    function_name: str,
    replacement: str,
    run_dir: Path,
    round_index: int,
) -> str:
    target = resolve_project_path(project, path)
    if not target.exists():
        raise RunnerError(f"function replacement target does not exist: {path}")
    if contains_conflict_markers(replacement):
        raise RunnerError(f"function replacement for {path} contains conflict markers")
    if contains_artifact_markers(replacement):
        raise RunnerError(f"function replacement for {path} contains artifact markers")
    text = target.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    def_pattern = re.compile(rf"^(?P<indent>[ \t]*)def\s+{re.escape(function_name)}\s*\(")
    matches: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        match = def_pattern.match(line)
        if not match:
            continue
        indent = match.group("indent")
        indent_len = len(indent.expandtabs(4))
        end = len(lines)
        for next_index in range(index + 1, len(lines)):
            next_line = lines[next_index]
            stripped = next_line.strip()
            if not stripped:
                continue
            next_indent_len = len(next_line[: len(next_line) - len(next_line.lstrip(" \t"))].expandtabs(4))
            if next_indent_len <= indent_len and not next_line.lstrip().startswith("#"):
                end = next_index
                break
        matches.append((index, end))
    if len(matches) != 1:
        raise RunnerError(
            f"function replacement target `{function_name}` must occur exactly once in {path}; found {len(matches)}"
        )
    start, end = matches[0]
    old = "".join(lines[start:end])
    target_indent_match = def_pattern.match(lines[start])
    if not target_indent_match:
        raise RunnerError(f"function replacement target `{function_name}` is no longer valid in {path}")
    new = reindent_python_function_block(replacement, function_name, target_indent_match.group("indent"))
    if old == new:
        raise RunnerError(f"function replacement for {path}:{function_name} is identical to existing text")
    backup = run_dir / f"backup-r{round_index:02d}-{Path(path).name}"
    backup.write_text(text, encoding="utf-8")
    target.write_text("".join([*lines[:start], new, *lines[end:]]), encoding="utf-8")
    return textwrap.dedent(
        f"""
        ## Python Function Replacement Result

        PASS: replaced `{function_name}` in `{path}`
        - replace_bytes: {len(new.encode("utf-8"))}
        - backup: `{backup.name}`
        """
    ).strip()

def partition_noop_replacements(
    replacements: Sequence[SearchReplaceArtifact],
) -> tuple[list[SearchReplaceArtifact], list[SearchReplaceArtifact]]:
    actionable: list[SearchReplaceArtifact] = []
    noop: list[SearchReplaceArtifact] = []
    for replacement in replacements:
        if replacement.search == replacement.replace:
            noop.append(replacement)
        else:
            actionable.append(replacement)
    return actionable, noop

def mixed_replace_file_artifact_paths(
    replacements: Sequence[SearchReplaceArtifact],
    artifacts: Sequence[FileArtifact],
) -> list[str]:
    """Return paths that received both a local edit and whole-file replacement.

    Applying both in one round is order-sensitive and can turn a recovered
    fenced snippet into a destructive full-file replacement.  Multi-file
    generation remains valid; this only blocks mixed protocols on the same
    target path.
    """
    replacement_paths = {artifact.path for artifact in replacements}
    return unique_ordered(
        artifact.path
        for artifact in artifacts
        if artifact.mode == "replace" and artifact.path in replacement_paths
    )

def apply_file_artifact(project: Path, artifact: FileArtifact, run_dir: Path, round_index: int) -> str:
    target = resolve_project_path(project, artifact.path)
    if contains_conflict_markers(artifact.content):
        raise RunnerError(f"file artifact for {artifact.path} contains conflict markers")
    if contains_artifact_markers(artifact.content):
        raise RunnerError(f"file artifact for {artifact.path} contains nested artifact markers")
    backup_note = "no previous file"
    if target.exists():
        backup = run_dir / f"backup-r{round_index:02d}-{Path(artifact.path).name}"
        backup.write_text(target.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
        backup_note = f"backup: `{backup.name}`"
    target.parent.mkdir(parents=True, exist_ok=True)
    if artifact.mode == "append":
        with target.open("a", encoding="utf-8") as handle:
            handle.write(artifact.content)
    else:
        target.write_text(artifact.content, encoding="utf-8")
    return textwrap.dedent(
        f"""
        ## File Artifact Apply Result

        PASS: {artifact.mode} `{artifact.path}`
        - bytes: {len(artifact.content.encode("utf-8"))}
        - {backup_note}
        """
    ).strip()

def apply_patch_file(project: Path, patch_file: Path) -> None:
    numstat = subprocess.run(
        ["git", "apply", "--numstat", str(patch_file)],
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
    )
    if numstat.returncode != 0:
        raise RunnerError(f"git apply --numstat failed:\n{numstat.stderr}")
    if not numstat.stdout.strip():
        raise RunnerError("git apply would not change any files; the patch was empty or skipped")
    changed_line_seen = False
    for line in numstat.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added, deleted = parts[0], parts[1]
        if added == "-" or deleted == "-":
            changed_line_seen = True
            break
        try:
            if int(added) + int(deleted) > 0:
                changed_line_seen = True
                break
        except ValueError:
            changed_line_seen = True
            break
    if not changed_line_seen:
        raise RunnerError("git apply would not change any file content; the patch was empty or skipped")

    check = subprocess.run(
        ["git", "apply", "--check", "--verbose", str(patch_file)],
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
    )
    check_output = "\n".join(part for part in (check.stdout, check.stderr) if part)
    if check.returncode != 0:
        raise RunnerError(f"git apply --check failed:\n{check_output}")
    if "Skipped patch" in check_output:
        raise RunnerError(f"git apply --check skipped the patch:\n{check_output}")

    apply = subprocess.run(
        ["git", "apply", "--verbose", str(patch_file)],
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
    )
    apply_output = "\n".join(part for part in (apply.stdout, apply.stderr) if part)
    if apply.returncode != 0:
        raise RunnerError(f"git apply failed:\n{apply_output}")
    if "Skipped patch" in apply_output:
        raise RunnerError(f"git apply skipped the patch:\n{apply_output}")

def first_artifact_marker_offset(text: str) -> int:
    markers = [
        "BEGIN_FILE:",
        "BEGIN_FILE\n",
        "BEGIN_FILE\r\n",
        "BEGIN_APPEND_FILE:",
        "BEGIN_APPEND_FILE\n",
        "BEGIN_APPEND_FILE\r\n",
        "BEGIN_SEARCH_REPLACE:",
        "BEGIN_SEARCH_REPLACE",
        "diff --git ",
        "--- ",
    ]
    offsets = [text.find(marker) for marker in markers if text.find(marker) >= 0]
    offsets.extend(json_artifact_marker_offsets(text))
    return min(offsets) if offsets else -1

def has_artifact_signature(text: str) -> bool:
    return first_artifact_marker_offset(text) >= 0

def is_non_artifact_output(text: str, budget_bytes: int = ARTIFACT_OUTPUT_BUDGET_BYTES) -> bool:
    offset = first_artifact_marker_offset(text)
    if offset < 0:
        return len(text.encode("utf-8")) > budget_bytes
    return len(text[:offset].encode("utf-8")) > budget_bytes

def extract_missing_context_requests(text: str) -> list[MissingContextRequest]:
    requests: list[MissingContextRequest] = []
    for match in re.finditer(r"(?im)^\s*MISSING_CONTEXT\s*:?\s*(?P<body>.+)$", text):
        body = match.group("body").strip()
        paths = normalize_new_files(re.findall(r"[\w./-]+\.[A-Za-z0-9_]+", body))
        if paths:
            requests.append(MissingContextRequest(paths=tuple(paths), reason=body))
    for match in re.finditer(r"(?ims)^\s*MISSING_CONTEXT\b(?P<body>.*?)(?=^\s*(?:BEGIN_|diff --git|\{|\[)|\Z)", text):
        body = match.group("body").strip()
        paths = normalize_new_files(re.findall(r"[\w./-]+\.[A-Za-z0-9_]+", body))
        if paths:
            requests.append(MissingContextRequest(paths=tuple(paths), reason=truncate_text(body, 300)))
    for candidate in _json_candidates(text):
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        items = payload.get("artifacts") if isinstance(payload, dict) else payload
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict) or str(item.get("type", "")).strip() != "missing_context":
                continue
            raw_paths = item.get("paths") or item.get("path") or []
            if isinstance(raw_paths, str):
                raw_paths = [raw_paths]
            if not isinstance(raw_paths, list):
                continue
            paths = normalize_new_files(str(path) for path in raw_paths if isinstance(path, str))
            if paths:
                requests.append(MissingContextRequest(paths=tuple(paths), reason=str(item.get("reason") or "")))
    unique: list[MissingContextRequest] = []
    seen: set[tuple[str, ...]] = set()
    for request in requests:
        if request.paths in seen:
            continue
        seen.add(request.paths)
        unique.append(request)
    return unique

def artifact_failure_type(error: str, default: str = "artifact_invalid") -> str:
    lowered = error.lower()
    if "read-only" in lowered and "tests/" in lowered:
        return "test_edit_attempt"
    if "missing_context" in lowered:
        return "missing_context"
    if "corrupt patch" in lowered or "git apply --numstat failed" in lowered or "patch does not apply" in lowered:
        return "corrupt_unified_diff"
    return default

FAILURE_TRANSITIONS: dict[str, FailureTransition] = {
    "artifact_valid": FailureTransition("artifact_valid", "runner", "apply_then_stage_test", "runner"),
    "artifact_invalid": FailureTransition(
        "artifact_invalid",
        "format_repair",
        "rewrite_previous_output_as_valid_artifact",
        "supervisor",
        ("Do not change semantic intent.", "Fix only artifact structure."),
    ),
    "corrupt_unified_diff": FailureTransition(
        "corrupt_unified_diff",
        "format_repair",
        "replace_corrupt_diff_with_atomic_search_replace",
        "runner",
        (
            "The previous unified diff could not be parsed or applied.",
            "Do not emit another unified diff.",
            "For existing-file repairs, use exactly one BEGIN_SEARCH_REPLACE block.",
            "Use BEGIN_FILE only when the target file is missing or explicitly generated.",
        ),
    ),
    "stage_scope_violation": FailureTransition(
        "stage_scope_violation",
        "format_repair",
        "rewrite_artifacts_to_current_stage_scope",
        "runner",
        (
            "The previous artifact included future-stage or broader subsystem behavior outside the current stage goal.",
            "Rewrite the offending product or test artifact so every public API, class, assertion, helper, and comment belongs to the current stage only.",
            "Remove future-stage implementation concepts instead of broadening product scope to satisfy them.",
        ),
    ),
    "non_artifact_output": FailureTransition(
        "non_artifact_output",
        "pm_planner",
        "semantic_salvage_then_format_or_repair",
        "pm",
        ("Extract usable intent from the failed output.", "Route to format_repair or repair_coder."),
    ),
    "missing_context": FailureTransition("missing_context", "runner", "collect_context_then_retry", "runner"),
    "semantic_repair_missing_context": FailureTransition(
        "semantic_repair_missing_context",
        "runner",
        "collect_semantic_focus_context_then_retry",
        "runner",
    ),
    "semantic_repair_missing_path": FailureTransition(
        "semantic_repair_missing_path",
        "format_repair",
        "rewrite_semantic_repair_as_valid_path_qualified_artifact",
        "supervisor",
        ("Preserve semantic edit intent.", "Add the missing artifact path.", "Return only one valid artifact."),
    ),
    "semantic_repair_prose_mixed": FailureTransition(
        "semantic_repair_prose_mixed",
        "format_repair",
        "strip_prose_and_emit_valid_semantic_artifact",
        "supervisor",
        ("Do not recalculate the solution.", "Remove prose/headings/fences.", "Return only one valid artifact."),
    ),
    "semantic_repair_markdown_fence": FailureTransition(
        "semantic_repair_markdown_fence",
        "format_repair",
        "remove_markdown_fence_and_emit_valid_artifact",
        "supervisor",
        ("Preserve semantic edit intent.", "Remove markdown fences.", "Return only one valid artifact."),
    ),
    "semantic_repair_malformed_search_replace": FailureTransition(
        "semantic_repair_malformed_search_replace",
        "format_repair",
        "rewrite_malformed_semantic_search_replace",
        "runner",
        (
            "Preserve semantic edit intent.",
            "After `BEGIN_SEARCH_REPLACE: path`, the next line must be `<<<<<<< SEARCH`.",
            "Do not prefix code lines with `:`.",
        ),
    ),
    "semantic_repair_multiple_artifacts": FailureTransition(
        "semantic_repair_multiple_artifacts",
        "format_repair",
        "choose_single_atomic_semantic_artifact",
        "supervisor",
        ("Preserve semantic edit intent.", "Choose the one product-code edit required by the semantic contract."),
    ),
    "semantic_repair_not_atomic": FailureTransition(
        "semantic_repair_not_atomic",
        "format_repair",
        "rewrite_as_one_atomic_semantic_artifact",
        "supervisor",
        ("Preserve semantic edit intent.", "Return one valid BEGIN_SEARCH_REPLACE with path or one one-file unified diff."),
    ),
    "semantic_repair_forbidden_artifact": FailureTransition(
        "semantic_repair_forbidden_artifact",
        "format_repair",
        "rewrite_forbidden_semantic_artifact_as_atomic_edit",
        "supervisor",
        ("Do not emit JSON, BEGIN_FILE, or BEGIN_APPEND_FILE.", "Return one valid product-code atomic artifact."),
    ),
    "semantic_repair_test_edit": FailureTransition(
        "semantic_repair_test_edit",
        "format_repair",
        "rewrite_semantic_repair_to_product_code_only",
        "runner",
        ("Tests are semantic evidence only.", "Move the repair to the product-code focus file."),
    ),
    "semantic_repair_too_large": FailureTransition(
        "semantic_repair_too_large",
        "format_repair",
        "shrink_semantic_repair_to_one_atomic_edit",
        "supervisor",
        ("Keep only the smallest product-code edit that satisfies one semantic contract.",),
    ),
    "format_repair_missing_context": FailureTransition(
        "format_repair_missing_context",
        "runner",
        "collect_context_then_retry_format_repair",
        "runner",
    ),
    "format_repair_missing_path": FailureTransition(
        "format_repair_missing_path",
        "format_repair",
        "rewrite_format_repair_as_path_qualified_artifact",
        "supervisor",
        ("Preserve the previous edit intent.", "Add the missing artifact path.", "Return only valid artifacts."),
    ),
    "format_repair_prose_mixed": FailureTransition(
        "format_repair_prose_mixed",
        "format_repair",
        "strip_prose_and_emit_valid_artifact",
        "supervisor",
        ("Do not recalculate the solution.", "Remove prose/headings/fences.", "Return only valid artifacts."),
    ),
    "format_repair_markdown_fence": FailureTransition(
        "format_repair_markdown_fence",
        "format_repair",
        "remove_markdown_fence_and_emit_valid_artifact",
        "supervisor",
        ("Preserve the previous edit intent.", "Remove markdown fences.", "Return only valid artifacts."),
    ),
    "format_repair_malformed_search_replace": FailureTransition(
        "format_repair_malformed_search_replace",
        "format_repair",
        "rewrite_malformed_search_replace_grammar",
        "runner",
        (
            "Preserve the previous edit intent.",
            "Use exact grammar: BEGIN_SEARCH_REPLACE path line, then `<<<<<<< SEARCH`, then search, `=======`, replace, `>>>>>>> REPLACE`.",
            "Do not emit `: def`, `: class`, or any colon-prefixed code body after the path line.",
        ),
    ),
    "artifact_orphan_search_replace": FailureTransition(
        "artifact_orphan_search_replace",
        "format_repair",
        "rewrite_orphan_search_replace_with_path_header",
        "runner",
        (
            "The output contained `<<<<<<< SEARCH` without `BEGIN_SEARCH_REPLACE: path`.",
            "Preserve the edit intent and add the required file path header.",
        ),
    ),
    "format_repair_unbalanced_file_artifact": FailureTransition(
        "format_repair_unbalanced_file_artifact",
        "format_repair",
        "close_or_rewrite_file_artifacts",
        "supervisor",
        ("Preserve the previous edit intent.", "Return balanced BEGIN_FILE/END_FILE blocks or a valid diff."),
    ),
    "format_repair_no_artifact": FailureTransition(
        "format_repair_no_artifact",
        "format_repair",
        "rewrite_as_valid_artifact_only",
        "supervisor",
        ("Preserve the previous edit intent.", "Start immediately with a valid artifact marker."),
    ),
    "stream_repeated_text_runaway": FailureTransition(
        "stream_repeated_text_runaway",
        "format_repair",
        "abort_repeated_text_and_rewrite_as_valid_artifact",
        "runner",
        ("The stream repeated text excessively.", "Return one concise artifact; no long enumerations."),
    ),
    "stream_repeated_json_search_replace": FailureTransition(
        "stream_repeated_json_search_replace",
        "format_repair",
        "abort_repeated_json_and_rewrite_as_non_json_artifact",
        "runner",
        ("Do not emit JSON search_replace in the next attempt.",),
    ),
    "stream_multiple_json_search_replace": FailureTransition(
        "stream_multiple_json_search_replace",
        "format_repair",
        "abort_excess_or_cross_path_json_search_replace",
        "runner",
        (
            "The stream emitted too many JSON search_replace edits or touched more than one path.",
            "A repair round may use a small same-file edit set, but must not fan out across paths.",
            "For the next attempt, emit one target file only.",
            "Prefer BEGIN_SEARCH_REPLACE for one local edit, or a bounded same-file JSON edit set when coupled hunks are required.",
        ),
    ),
    "stream_json_search_replace_excess": FailureTransition(
        "stream_json_search_replace_excess",
        "format_repair",
        "abort_excess_json_and_rewrite_as_concise_artifact",
        "runner",
        ("Do not emit large JSON search_replace lists in the next attempt.",),
    ),
    "stream_markdown_fence_before_artifact": FailureTransition(
        "stream_markdown_fence_before_artifact",
        "format_repair",
        "abort_markdown_fence_and_emit_valid_artifact",
        "runner",
        ("Do not wrap artifacts in Markdown fences.", "Start immediately with BEGIN_FILE, BEGIN_SEARCH_REPLACE, or diff --git."),
    ),
    "stream_prose_before_artifact": FailureTransition(
        "stream_prose_before_artifact",
        "format_repair",
        "abort_prose_prefix_and_emit_valid_artifact",
        "runner",
        ("Do not explain, plan, summarize, or include propositions before the artifact.", "Start immediately with a valid artifact marker."),
    ),
    "stream_non_artifact_output": FailureTransition(
        "stream_non_artifact_output",
        "format_repair",
        "abort_non_artifact_stream_and_emit_valid_artifact",
        "runner",
        (
            "The stream spent too many bytes without any valid artifact marker.",
            "Do not analyze in the response; start immediately with BEGIN_SEARCH_REPLACE, BEGIN_FILE, or diff --git.",
        ),
    ),
    "stream_json_plan_before_artifact": FailureTransition(
        "stream_json_plan_before_artifact",
        "format_repair",
        "abort_json_plan_and_emit_valid_artifact",
        "runner",
        ("Do not emit JSON plans/propositions.", "Return only the requested file artifacts."),
    ),
    "stream_mixed_artifact_formats": FailureTransition(
        "stream_mixed_artifact_formats",
        "format_repair",
        "abort_mixed_artifact_formats_and_choose_one_protocol",
        "runner",
        (
            "The stream mixed JSON file artifacts with BEGIN_FILE artifacts.",
            "Choose exactly one artifact protocol for the next attempt.",
            "Prefer balanced BEGIN_FILE/END_FILE blocks for generated multi-file output.",
            "Do not emit JSON artifacts in the next attempt.",
        ),
    ),
    "stream_multiple_file_artifacts_in_repair": FailureTransition(
        "stream_multiple_file_artifacts_in_repair",
        "format_repair",
        "abort_multi_file_repair_and_emit_single_artifact",
        "runner",
        (
            "The stream emitted multiple file artifacts during a repair round.",
            "A repair round must preserve one semantic edit intent.",
            "Return exactly one artifact for one writable target file.",
        ),
    ),
    "stream_artifact_too_large": FailureTransition(
        "stream_artifact_too_large",
        "format_repair",
        "abort_oversized_artifact_and_emit_atomic_patch",
        "runner",
        (
            "The streamed artifact exceeded the size budget.",
            "Return one small atomic edit focused on one failing assertion or exception family.",
            "For existing-file repairs, prefer exactly one BEGIN_SEARCH_REPLACE block.",
            "Do not emit JSON search_replace lists, prose, or multi-function rewrites.",
        ),
    ),
    "stream_python_file_artifact_too_large": FailureTransition(
        "stream_python_file_artifact_too_large",
        "format_repair",
        "abort_oversized_python_file_artifact_and_split_to_one_file",
        "runner",
        (
            "The streamed Python file artifact exceeded the size budget.",
            "Split the stage into one missing/generated writable file per attempt.",
            "For a missing generated file, return exactly one BEGIN_FILE/END_FILE block.",
            "If the target file already exists, return one focused BEGIN_SEARCH_REPLACE block.",
        ),
    ),
    "stream_readonly_artifact_path": FailureTransition(
        "stream_readonly_artifact_path",
        "format_repair",
        "abort_readonly_artifact_and_patch_product_code",
        "runner",
        (
            "The stream attempted to edit a read-only evidence path.",
            "Tests and readonly context are executable evidence, not repair targets.",
            "Return exactly one product-code artifact for a writable path.",
            "Do not emit artifacts for tests, README, or readonly evidence files.",
        ),
    ),
    "stream_python_diff_artifact_too_large": FailureTransition(
        "stream_python_diff_artifact_too_large",
        "format_repair",
        "abort_oversized_python_diff_and_emit_file_artifacts",
        "runner",
        (
            "The streamed unified diff for a Python file exceeded the size budget.",
            "For missing generated Python files, use balanced BEGIN_FILE/END_FILE blocks instead of a large diff.",
        ),
    ),
    "stream_artifact_process_narration": FailureTransition(
        "stream_artifact_process_narration",
        "format_repair",
        "abort_process_narration_and_emit_artifact_only",
        "runner",
        ("Do not include reasoning, self-talk, or process narration inside artifacts.", "Return only executable code/data changes."),
    ),
    "stream_artifact_malformed_search_replace": FailureTransition(
        "stream_artifact_malformed_search_replace",
        "format_repair",
        "abort_malformed_search_replace_and_rewrite_grammar",
        "runner",
        (
            "The streamed search/replace grammar was malformed.",
            "After `BEGIN_SEARCH_REPLACE: path`, emit `<<<<<<< SEARCH` immediately; do not emit colon-prefixed code.",
        ),
    ),
    "stream_orphan_search_replace": FailureTransition(
        "stream_orphan_search_replace",
        "format_repair",
        "abort_orphan_search_replace_and_rewrite_with_path_header",
        "runner",
        (
            "The stream started a search/replace body without a file path header.",
            "Start with `BEGIN_SEARCH_REPLACE: path/to/file` before `<<<<<<< SEARCH`.",
        ),
    ),
    "stream_root_cause_too_large": FailureTransition(
        "stream_root_cause_too_large",
        "root_cause_repair",
        "shrink_root_cause_report_before_patch",
        "runner",
        (
            "The diagnostic report exceeded its budget.",
            "Return a compact root-cause report with one chosen hypothesis and one patch target.",
        ),
    ),
    "mechanical_probe_contradiction": FailureTransition(
        "mechanical_probe_contradiction",
        "root_cause_repair",
        "reject_plan_contradicting_mechanical_probe",
        "runner",
        (
            "The patch plan contradicted deterministic Mechanical Probe observations.",
            "Treat the probe observations as fixed propositions.",
            "Choose a different root-cause patch or request missing context; do not repeat the rejected formula.",
        ),
    ),
    "stage_test_failed": FailureTransition("stage_test_failed", "repair_coder", "repair_stage_failure", "supervisor"),
    "final_test_failed": FailureTransition(
        "final_test_failed",
        "final_integration_repair",
        "parse_failure_focus_and_repair_product_code",
        "supervisor",
    ),
    "final_repair_artifact_invalid": FailureTransition(
        "final_repair_artifact_invalid",
        "format_repair",
        "semantic_salvage_then_format_repair",
        "pm",
    ),
    "test_edit_attempt": FailureTransition(
        "test_edit_attempt",
        "pm_planner",
        "reject_test_edit_and_reissue_product_code_repair",
        "runner",
        ("Tests are read-only in final integration repair.",),
    ),
    "repeated_same_failure": FailureTransition(
        "repeated_same_failure",
        "root_cause_repair",
        "reconsider_root_cause_before_next_patch",
        "supervisor",
        (
            "The executable failure signature did not change after the previous patch.",
            "Do not repeat the same edit or edit only formatting around the same code.",
            "Re-read the current file context and identify a different root-cause branch/data invariant before emitting one smallest patch.",
        ),
    ),
}



def transition_for_failure(failure_type: str | None) -> FailureTransition:
    if not failure_type:
        return FailureTransition("unknown", "repair_coder", "continue_with_latest_evidence", "supervisor")
    if failure_type in FAILURE_TRANSITIONS:
        return FAILURE_TRANSITIONS[failure_type]
    if failure_type in {"test_assertion_failed", "test_error", "syntax_error", "command_failed"}:
        return FAILURE_TRANSITIONS["stage_test_failed"]
    return FailureTransition(failure_type, "repair_coder", "continue_with_latest_evidence", "supervisor")

def is_protocol_failure_type(failure_type: str | None) -> bool:
    if not failure_type:
        return False
    if failure_type.startswith(("format_repair_", "semantic_repair_", "stream_")):
        return True
    return failure_type in {
        "artifact_invalid",
        "artifact_lint_failed",
        "patch_extraction_failed",
        "patch_apply_failed",
        "non_artifact_output",
        "missing_context",
        "mechanical_probe_contradiction",
        "test_edit_attempt",
        "unbalanced_file_artifact",
    }

def failure_transition_document(transition: FailureTransition, round_index: int, evidence: str = "") -> str:
    lines = [
        "## Failure Transition",
        "",
        f"- round: {round_index}",
        f"- failure_type: {transition.failure_type}",
        f"- owner: {transition.owner}",
        f"- next_role: {transition.next_role}",
        f"- action: {transition.action}",
    ]
    if transition.instructions:
        lines.append("- instructions:")
        lines.extend(f"  - {item}" for item in transition.instructions)
    if evidence:
        lines.extend(["", "### evidence", "```text", truncate_text(evidence, 2000), "```"])
    return "\n".join(lines)

def semantic_contract_to_dict(contract: SemanticContract) -> dict[str, object]:
    return {
        "id": contract.contract_id,
        "kind": contract.kind,
        "text": contract.text,
        "source": contract.source,
        "focus_files": list(contract.focus_files),
        "evidence": list(contract.evidence),
    }

def semantic_contracts_document(contracts: Sequence[SemanticContract]) -> str:
    lines = ["## Semantic Contracts", ""]
    if not contracts:
        lines.append("No semantic contracts extracted.")
        return "\n".join(lines)
    for contract in contracts:
        lines.append(f"- {contract.contract_id} [{contract.kind}]: {contract.text}")
        lines.append(f"  - source: {contract.source}")
        if contract.focus_files:
            lines.append("  - focus_files: " + ", ".join(contract.focus_files))
        if contract.evidence:
            lines.append("  - evidence: " + "; ".join(contract.evidence))
    return "\n".join(lines)

def semantic_contract_focus_paths(
    contracts: Sequence[SemanticContract],
    existing_paths: Sequence[str],
) -> tuple[list[str], list[str]]:
    existing = set(existing_paths)
    writable_product: list[str] = []
    readonly_context: list[str] = []
    for contract in contracts:
        for path in contract.focus_files:
            if path not in existing:
                continue
            if path.startswith("tests/"):
                readonly_context.append(path)
            else:
                writable_product.append(path)
    return unique_ordered(writable_product), unique_ordered(readonly_context)

def extract_proposition_ledger(text: str, source: str = "document") -> list[dict[str, str]]:
    propositions: list[dict[str, str]] = []
    for raw_line in text.splitlines():
        match = re.match(r"^\s*(?:[-*]\s*)?(?P<id>[PCGEAV]\d+)\s*:\s*(?P<text>.+?)\s*$", raw_line)
        if not match:
            continue
        proposition_id = match.group("id")
        propositions.append(
            {
                "id": proposition_id,
                "kind": proposition_id[0],
                "text": match.group("text").strip(),
                "source": source,
            }
        )
    return propositions

def proposition_manifest_from_documents(documents: Sequence[tuple[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str, str]] = set()
    result: list[dict[str, str]] = []
    for title, content in documents:
        for item in extract_proposition_ledger(content, title):
            key = (item["kind"], item["text"], item["source"])
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
    return result

def normalize_contract_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()

def python_function_source(block: str, function_name: str) -> str:
    try:
        module = ast.parse(textwrap.dedent(block))
    except SyntaxError:
        return ""
    lines = textwrap.dedent(block).splitlines()
    for node in ast.walk(module):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != function_name:
            continue
        end_lineno = getattr(node, "end_lineno", None)
        if not end_lineno:
            return ""
        return "\n".join(lines[node.lineno - 1 : end_lineno])
    return ""

def test_source_asserts_raw_page_round_trip(source: str) -> bool:
    lowered = source.lower()
    return (
        "write_page(" in lowered
        and "read_page(" in lowered
        and "assertequal(" in lowered.replace(" ", "")
        and ("page_size" in lowered or "page_data" in lowered)
    )

def test_source_asserts_zero_allocated_page(source: str) -> bool:
    normalized = re.sub(r"\s+", "", source.lower())
    return (
        "allocate_page(" in normalized
        and "read_page(" in normalized
        and "assertequal(" in normalized
        and ('b"\\x00"*page_size' in normalized or "b'\\x00'*page_size" in normalized)
    )

def project_tests_assert_pager_raw_page_contract(project: Path | None, test_paths: Sequence[str]) -> bool:
    if project is None:
        return False
    for test_path in normalize_project_relative_paths(test_paths):
        if test_path != "tests/test_pager.py":
            continue
        path = resolve_project_path(project, test_path)
        if not path.exists():
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if test_source_asserts_raw_page_round_trip(source) or test_source_asserts_zero_allocated_page(source):
            return True
    return False

TEST_HARNESS_WRITE_STRATEGIES = frozenset(
    {
        "replace_test_harness",
        "create_test_harness",
        "test_harness_api_mismatch",
        "generated_binary_contract_alignment",
        "generated_test_import_api_mismatch",
        "rewrite_current_stage_tests_to_scope",
    }
)

MIXED_PRODUCT_TEST_WRITE_STRATEGIES = frozenset(
    {
        "generated_binary_contract_alignment",
        "generated_test_import_api_mismatch",
    }
)

KNOWN_CLASS_OWNER_PATHS = {
    "Lexer": ("minisqlite/sql/lexer.py",),
    "Token": ("minisqlite/sql/lexer.py",),
    "Parser": ("minisqlite/sql/parser.py",),
    "Pager": ("minisqlite/storage/pager.py",),
    "BTree": ("minisqlite/storage/btree.py",),
    "Connection": ("minisqlite/connection.py",),
    "Result": ("minisqlite/result.py",),
}

def compact_identifier(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())

def camel_to_snake(value: str) -> str:
    step1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", value)
    step2 = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", step1)
    return step2.lower()

def project_python_product_paths(project: Path | None) -> list[str]:
    if project is None or not project.exists():
        return []
    paths: list[str] = []
    try:
        candidates = project.rglob("*.py")
    except OSError:
        return []
    for path in candidates:
        try:
            rel = str(path.relative_to(project))
        except ValueError:
            continue
        if rel.startswith("tests/") or rel.startswith("."):
            continue
        if "/.sdlc-runner/" in rel:
            continue
        paths.append(rel)
    return unique_ordered(paths)

def module_name_to_project_path(module_name: str) -> str:
    return module_name.replace(".", "/") + ".py"

def python_defined_symbols(project: Path | None, rel_path: str) -> list[str]:
    if project is None:
        return []
    path = project / rel_path
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return unique_ordered(
        [
            *re.findall(r"(?m)^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", text),
            *re.findall(r"(?m)^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\s*[\(:]", text),
        ]
    )

def likely_symbol_aliases(missing_symbol: str, existing_symbols: Sequence[str]) -> list[str]:
    missing_parts = [part for part in missing_symbol.split("_") if part]
    aliases: list[str] = []
    for symbol in existing_symbols:
        if not symbol or symbol.startswith("_"):
            continue
        if symbol == missing_symbol:
            aliases.append(symbol)
        elif missing_parts and symbol == missing_parts[0]:
            aliases.append(symbol)
        elif missing_symbol.startswith(symbol + "_") or missing_symbol.endswith("_" + symbol):
            aliases.append(symbol)
        elif compact_identifier(symbol) and compact_identifier(symbol) in compact_identifier(missing_symbol):
            aliases.append(symbol)
    return unique_ordered(aliases)

def import_api_alias_projections(
    project: Path | None,
    importing_paths: Sequence[str],
    module_name: str,
    existing_symbols: Sequence[str],
) -> list[tuple[str, str, str]]:
    """Find generated import names that should project onto existing API names.

    Returns tuples of (path, missing_name, existing_name). This is deliberately
    syntactic and narrow: it only handles explicit ``from x import y`` forms in
    files already selected by the failure evidence.
    """
    if project is None:
        return []
    module_pattern = re.escape(module_name)
    pattern = re.compile(
        rf"(?ms)^from\s+{module_pattern}\s+import\s+(?P<names>(?:\([^\)]*\)|[^\n]+))"
    )
    projections: list[tuple[str, str, str]] = []
    for rel_path in unique_ordered(importing_paths):
        path = project / rel_path
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for match in pattern.finditer(text):
            raw_names = match.group("names").strip()
            if raw_names.startswith("(") and raw_names.endswith(")"):
                raw_names = raw_names[1:-1]
            for raw_name in raw_names.split(","):
                imported = raw_name.strip()
                if not imported:
                    continue
                imported_name = imported.split(" as ", 1)[0].strip()
                if imported_name in existing_symbols:
                    continue
                aliases = likely_symbol_aliases(imported_name, existing_symbols)
                if aliases:
                    projections.append((rel_path, imported_name, aliases[0]))
    return unique_ordered(projections)

def inferred_product_focus_from_class_name(
    class_name: str,
    existing_paths: Sequence[str] = (),
) -> list[str]:
    """Infer the likely product file that owns a class name."""
    candidates = list(KNOWN_CLASS_OWNER_PATHS.get(class_name, ()))
    snake = camel_to_snake(class_name)
    compact = snake.replace("_", "")
    for stem in unique_ordered([snake, compact]):
        candidates.extend(
            [
                f"minisqlite/{stem}.py",
                f"minisqlite/storage/{stem}.py",
                f"minisqlite/sql/{stem}.py",
                f"minisqlite/engine/{stem}.py",
                f"{stem}.py",
            ]
        )
    existing = set(existing_paths)
    if existing:
        existing_candidates = [path for path in candidates if path in existing]
        if existing_candidates:
            return unique_ordered(existing_candidates)
    return unique_ordered(candidates)

def class_owner_paths_from_project(
    project: Path | None,
    class_name: str,
    existing_paths: Sequence[str] = (),
) -> list[str]:
    """Find product files that actually define class_name, falling back to naming."""
    existing = list(existing_paths) or project_python_product_paths(project)
    owners: list[str] = []
    if project is not None:
        class_pattern = re.compile(rf"(?m)^\s*class\s+{re.escape(class_name)}\b")
        for rel_path in existing:
            path = project / rel_path
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if class_pattern.search(text):
                owners.append(rel_path)
    if owners:
        return unique_ordered(owners)
    return inferred_product_focus_from_class_name(class_name, existing)

def product_paths_referencing_attribute(project: Path | None, attr: str) -> list[str]:
    """Find product files that call or reference a missing attribute name."""
    if project is None or not attr:
        return []
    pattern = re.compile(rf"\.\s*{re.escape(attr)}\b")
    matches: list[str] = []
    for rel_path in project_python_product_paths(project):
        path = project / rel_path
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if pattern.search(text):
            matches.append(rel_path)
    return unique_ordered(matches)

def focus_paths_overlap(left: Sequence[str], right: Sequence[str]) -> bool:
    left_tokens = {compact_identifier(Path(path).with_suffix("").as_posix()) for path in left}
    right_tokens = {compact_identifier(Path(path).with_suffix("").as_posix()) for path in right}
    return bool(left_tokens & right_tokens)

def attribute_error_is_cross_stage_test_harness_mismatch(
    class_name: str,
    test_paths: Sequence[str],
    project: Path | None = None,
    existing_paths: Sequence[str] = (),
) -> bool:
    """Return True when a generated stage test calls another stage's API.

    Example: tests/test_btree.py failing on Pager.init_db should repair the
    generated BTree test harness, not mutate the already approved Pager API.
    """
    inferred_stage_focus = unique_ordered(
        product_path
        for test_path in test_paths
        if test_path.startswith("tests/")
        for product_path in inferred_product_focus_from_test_path(test_path, existing_paths)
    )
    if not inferred_stage_focus:
        return False
    owner_focus = class_owner_paths_from_project(project, class_name, existing_paths)
    if not owner_focus:
        return False
    return not focus_paths_overlap(owner_focus, inferred_stage_focus)

def extract_semantic_contracts_from_command_docs(
    command_docs: Sequence[tuple[str, str]],
    project: Path | None = None,
) -> list[SemanticContract]:
    combined = "\n".join(document for _name, document in command_docs)
    lowered = combined.lower()
    contracts: list[SemanticContract] = []
    existing_product_paths = project_python_product_paths(project)

    def add(kind: str, text: str, source: str, focus_files: Sequence[str] = (), evidence: Sequence[str] = ()) -> None:
        normalized = normalize_contract_text(text)
        if any(existing.text == normalized for existing in contracts):
            return
        contracts.append(
            SemanticContract(
                contract_id=f"C{len(contracts) + 1:02d}",
                kind=kind,
                text=normalized,
                source=source,
                focus_files=tuple(unique_ordered(focus_files)),
                evidence=tuple(unique_ordered(evidence)),
            )
        )

    focus_files: list[str] = []
    for raw_path in re.findall(r'File "([^"]+)", line \d+', combined):
        if "/tests/" in raw_path:
            focus_files.append("tests/" + raw_path.split("/tests/", 1)[1])
        elif "/minisqlite/" in raw_path:
            focus_files.append("minisqlite/" + raw_path.split("/minisqlite/", 1)[1])
    inferred_product_focus = unique_ordered(
        product_path
        for test_path in focus_files
        if test_path.startswith("tests/")
        for product_path in inferred_product_focus_from_test_path(test_path)
    )

    if "cannot import name 'tokenize'" in lowered:
        add(
            "api_contract",
            "The lexer module must export a callable tokenize function.",
            "ImportError",
            ["minisqlite/sql/lexer.py", "minisqlite/sql/__init__.py"],
            ["cannot import name 'tokenize'"],
        )

    if "unexpected character '*'" in lowered or "unexpected character: '*'" in lowered or 'unexpected character "*" ' in lowered:
        add(
            "api_contract",
            "The SQL lexer must tokenize '*' as a STAR token instead of raising SQLSyntaxError.",
            "unittest traceback",
            ["minisqlite/sql/lexer.py"],
            ["Unexpected character '*'"],
        )

    for attr in re.findall(r"AttributeError: 'Token' object has no attribute '([^']+)'", combined):
        add(
            "api_contract",
            f"Token objects must expose a `{attr}` attribute used by the tests.",
            "AttributeError",
            ["minisqlite/sql/lexer.py"],
            [f"missing Token.{attr}"],
        )

    for class_name, attr in re.findall(r"AttributeError: '([^']+)' object has no attribute '([^']+)'", combined):
        if class_name == "Token":
            continue
        if not inferred_product_focus:
            continue
        test_focus = [path for path in focus_files if path.startswith("tests/")]
        if attribute_error_is_cross_stage_test_harness_mismatch(
            class_name,
            test_focus,
            project,
            existing_product_paths,
        ):
            continue
        add(
            "api_contract",
            f"{class_name} objects must expose public `{attr}` attribute used by the tests.",
            "AttributeError",
            [*inferred_product_focus, *[path for path in focus_files if path.startswith("tests/")]],
            [f"missing {class_name}.{attr}"],
        )

    if "token.type" in combined or "TokenType." in combined:
        if re.search(r"AssertionError:\s*'?[A-Z_]+'?\s+!=\s+<TokenType\.", combined) or re.search(
            r"AssertionError:\s*'?[A-Z_]+'?\s+not found in \[<TokenType\.",
            combined,
        ):
            add(
                "api_contract",
                "Token.type must be string-compatible with the tested token names, not a raw Enum value.",
                "unittest assertion",
                ["minisqlite/sql/lexer.py"],
                ["TokenType enum compared against string token names"],
            )
        if re.search(r"AssertionError:\s*<TokenType\.IDENTIFIER:[^>]*>\s+!=\s+<TokenType\.[A-Z_]+:", combined) or re.search(
            r"AssertionError:\s*<TokenType\.[A-Z_]+:[^>]*>\s+not found in \[<TokenType\.IDENTIFIER:",
            combined,
        ):
            add(
                "api_contract",
                "SQL keywords must map to their keyword TokenType values instead of IDENTIFIER.",
                "unittest assertion",
                ["minisqlite/sql/lexer.py"],
                ["keyword token assertions produced IDENTIFIER"],
            )

    if re.search(r"AssertionError:\s*'?\d+'?\s+!=\s+'?-\d+'?", combined):
        add(
            "api_contract",
            "Negative integer literals must preserve the leading '-' in Token.value.",
            "unittest assertion",
            ["minisqlite/sql/lexer.py"],
            ["negative integer token lost sign"],
        )

    if "AssertionError: 2 != 1" in combined or "AssertionError: 1 != 0" in combined:
        add(
            "api_contract",
            "tokenize must not append an extra EOF token when tests expect only source tokens.",
            "unittest assertion",
            ["minisqlite/sql/lexer.py"],
            ["token count assertions show one extra token"],
        )

    # Read failing assert lines when the file still exists. This turns a raw
    # traceback into a stable repair contract that the next round can obey.
    if project is not None:
        for raw_path, raw_line in re.findall(r'File "([^"]+)", line (\d+)', combined):
            try:
                line_no = int(raw_line)
            except ValueError:
                continue
            path = Path(raw_path)
            if not path.exists():
                continue
            try:
                source_text = path.read_text(encoding="utf-8", errors="replace")
                source_line = source_text.splitlines()[line_no - 1].strip()
            except (OSError, IndexError):
                continue
            rel_path = str(path)
            if "/tests/" in rel_path:
                rel_path = "tests/" + rel_path.split("/tests/", 1)[1]
            product_focus_for_rel_path = inferred_product_focus_from_test_path(rel_path)
            if rel_path == "tests/test_pager.py" and product_focus_for_rel_path:
                if test_source_asserts_raw_page_round_trip(source_text):
                    add(
                        "api_contract",
                        "Pager.write_page(page_id, data) and Pager.read_page(page_id) must round-trip exact PAGE_SIZE bytes; pager.py must not interpret B+Tree page_type bytes in raw page IO.",
                        f"{rel_path}:{line_no}",
                        [*product_focus_for_rel_path, rel_path],
                        ["write_page/read_page raw page round-trip asserted by tests"],
                    )
                if test_source_asserts_zero_allocated_page(source_text):
                    add(
                        "state_contract",
                        "Pager.allocate_page() must create a zero-filled PAGE_SIZE page readable through read_page().",
                        f"{rel_path}:{line_no}",
                        [*product_focus_for_rel_path, rel_path],
                        ["allocated page zero-fill asserted by tests"],
                    )
            if "assertEqual" in source_line and ".type" in source_line and re.search(r'"[A-Z_]+"', source_line):
                add(
                    "api_contract",
                    "Existing tests assert token.type against string literals; preserve that public lexer contract.",
                    f"{rel_path}:{line_no}",
                    ["minisqlite/sql/lexer.py", rel_path],
                    [source_line],
                )
            elif "assertEqual" in source_line and "len(" in source_line:
                product_focus = inferred_product_focus_from_test_path(rel_path)
                if "token" in source_line.lower() or "minisqlite/sql/lexer.py" in product_focus:
                    add(
                        "api_contract",
                        "Existing lexer tests assert exact token counts; do not add hidden/sentinel tokens unless tests expect them.",
                        f"{rel_path}:{line_no}",
                        ["minisqlite/sql/lexer.py", rel_path],
                        [source_line],
                    )
                elif product_focus:
                    add(
                        "state_contract",
                        "Existing tests assert exact collection cardinality; preserve the tested count by repairing product behavior first.",
                        f"{rel_path}:{line_no}",
                        [*product_focus, rel_path],
                        [source_line],
                    )
            elif "assertEqual" in source_line and rel_path.startswith("tests/"):
                product_focus = inferred_product_focus_from_test_path(rel_path)
                if not product_focus:
                    continue
                if "next_page_id" in source_line:
                    add(
                        "state_contract",
                        "Pager.next_page_id must persist allocated page IDs across close/reopen when tests assert it after reopening.",
                        f"{rel_path}:{line_no}",
                        [*product_focus, rel_path],
                        [source_line],
                    )
                elif "format_version" in source_line:
                    add(
                        "api_contract",
                        "Pager must expose public format_version when tests assert header round-trip metadata.",
                        f"{rel_path}:{line_no}",
                        [*product_focus, rel_path],
                        [source_line],
                    )
                else:
                    add(
                        "api_contract",
                        "Existing tests assert a public product-code value; preserve the tested behavior by repairing product code first.",
                        f"{rel_path}:{line_no}",
                        [*product_focus, rel_path],
                        [source_line],
                    )

    return contracts

def observation_summary_document(round_index: int, command_docs: Sequence[tuple[str, str]]) -> str:
    failed: list[tuple[str, dict[str, str]]] = []
    patterns: Counter[str] = Counter()
    for name, document in command_docs:
        parsed = parse_command_result_document(document)
        if parsed.get("status") == "PASS":
            continue
        failed.append((name, parsed))
        text = f"{parsed.get('stdout', '')}\n{parsed.get('stderr', '')}"
        for pattern in re.findall(r"got (b[\"'][^\"']{1,160}[\"'])", text):
            patterns[f"observed response: {pattern}"] += 1
        for pattern in re.findall(r"RESP parse error:\s*([^\n]{1,160})", text):
            patterns[f"parse error: {pattern.strip()}"] += 1
        for pattern in re.findall(r"([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception):[^\n]{1,160})", text):
            patterns[f"exception: {pattern.strip()}"] += 1
        for pattern in re.findall(r"(-ERR [^\r\n]{1,160})", text):
            patterns[f"error response: {pattern.strip()}"] += 1

    lines = [
        "## Observation Summary",
        "",
        f"- round: {round_index}",
        f"- failed_checks: {len(failed)}",
    ]
    if failed:
        lines.append("- failed_names:")
        lines.extend(f"  - {name}" for name, _parsed in failed)
    common = [(item, count) for item, count in patterns.most_common(8) if count >= 2]
    if common:
        lines.append("- repeated_failure_patterns:")
        lines.extend(f"  - count={count}: {item}" for item, count in common)
        lines.extend(
            [
                "",
                "Repair guidance:",
                "- Multiple checks share the same failure pattern. Fix the shared implementation root cause first.",
                "- Do not weaken or replace tests while executable smoke evidence shows the implementation returns the wrong value.",
                "- Prefer the file that owns the repeated error path (parser, command dispatch, server loop, or storage) over unrelated docs/tests.",
            ]
        )
    elif failed:
        lines.extend(
            [
                "",
                "Repair guidance:",
                "- Use the failed command documents as executable evidence.",
                "- Change tests only when the test harness itself is the clearly demonstrated root cause.",
            ]
        )
    return "\n".join(lines).strip()

def test_commands_use_unittest(test_commands: Sequence[str]) -> bool:
    return any("unittest" in command or "discover -s tests" in command for command in test_commands)

def inferred_product_focus_from_test_path(test_path: str, existing_paths: Sequence[str] = ()) -> list[str]:
    """Infer product files from conventional stage test names.

    Tracebacks for assertion failures often point only at tests. The repair
    loop still needs a product-code focus so it does not weaken the test that
    revealed the contract.
    """
    normalized = test_path.strip()
    if not normalized.startswith("tests/"):
        return []
    static_map = {
        "tests/test_core.py": ("minisqlite/errors.py", "minisqlite/result.py"),
        "tests/test_lexer.py": ("minisqlite/sql/lexer.py",),
        "tests/test_parser.py": ("minisqlite/sql/parser.py", "minisqlite/sql/ast.py"),
        "tests/test_record.py": ("minisqlite/storage/record.py",),
        "tests/test_pager.py": ("minisqlite/storage/pager.py",),
        "tests/test_btree.py": ("minisqlite/storage/btree.py",),
        "tests/test_connection.py": (
            "minisqlite/connection.py",
            "minisqlite/result.py",
            "minisqlite/engine/executor.py",
            "minisqlite/engine/schema.py",
        ),
        "tests/test_cli.py": (
            "minisqlite/cli.py",
            "minisqlite/__main__.py",
            "minisqlite/__init__.py",
        ),
    }
    candidates = list(static_map.get(normalized, ()))
    stem = Path(normalized).stem.removeprefix("test_")
    if stem and not candidates:
        candidates.extend(
            [
                f"minisqlite/{stem}.py",
                f"minisqlite/storage/{stem}.py",
                f"minisqlite/sql/{stem}.py",
                f"minisqlite/engine/{stem}.py",
                f"{stem}.py",
            ]
        )
    existing = set(existing_paths)
    if existing:
        existing_candidates = [path for path in candidates if path in existing]
        return unique_ordered(existing_candidates or candidates)
    return unique_ordered(candidates)

def json_generated_blocks(text: str) -> list[tuple[str | None, str]]:
    blocks: list[tuple[str | None, str]] = []
    for candidate in _json_candidates(text):
        payload = None
        for json_candidate in [candidate, *repaired_json_candidates(candidate)]:
            try:
                payload = json.loads(json_candidate)
                break
            except json.JSONDecodeError:
                continue
        if payload is None:
            continue
        raw_items: Any
        if isinstance(payload, dict) and "artifacts" in payload:
            raw_items = payload["artifacts"]
        else:
            raw_items = payload
        if isinstance(raw_items, dict):
            items = [raw_items]
        elif isinstance(raw_items, list):
            items = raw_items
        else:
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            artifact_type = str(item.get("type", "")).strip()
            path = str(item.get("path", "")).strip() or None
            if artifact_type in {"replace_file", "file", "append_file"} and isinstance(item.get("content"), str):
                blocks.append((path, str(item["content"])))
            elif artifact_type == "search_replace" and isinstance(item.get("replace"), str):
                blocks.append((path, str(item["replace"])))
    return blocks

def block_looks_like_test_harness(block: str) -> bool:
    return bool(
        re.search(
            r"(?m)^\s*(?:@patch\(|def\s+test_[A-Za-z0-9_]*\s*\(|class\s+Test[A-Za-z0-9_]*\b|"
            r"import\s+unittest\b|from\s+unittest\b|self\.assert[A-Za-z0-9_]*\s*\()",
            block,
        )
    )

def artifact_path_content_mismatch(path: str | None, block: str) -> str | None:
    if not path:
        return None
    normalized = normalize_legacy_file_artifact_path(path)
    if normalized.startswith("tests/"):
        return None
    if block_looks_like_test_harness(block):
        return (
            "artifact path names product code, but artifact content looks like a test harness; "
            "the path and search/replace content must belong to the same file owner"
        )
    return None

def _current_stage_scope_text(brief: str) -> str:
    match = re.search(r"(?ims)^\s*##\s+Current Stage\s*\n(?P<body>.*?)(?=^\s*##\s+|\Z)", brief)
    if not match:
        return brief
    return match.group("body")

def _normalized_scope_text(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()

def _scope_text_without_negated_terms(text: str) -> str:
    lowered = text.lower()
    future_terms = r"(?:split|splits|splitting|page[-_\s]*splits?|page[-_\s]*splitting|multi[-_\s]*pages?|internal[-_\s]*pages?|internal[-_\s]*roots?)"
    lowered = re.sub(
        r"\bno\s+split\s*/\s*multi[-_\s]*pages?\b",
        " ",
        lowered,
    )
    lowered = re.sub(
        rf"\b(?:no|not|without|non)\s+{future_terms}(?:[-_\s]+support)?(?:\s+(?:or|and)\s+{future_terms}(?:[-_\s]+support)?)*\b",
        " ",
        lowered,
    )
    lowered = re.sub(
        rf"\b{future_terms}\s+(?:is|are)\s+out\s+of\s+(?:[a-z0-9_\s-]+\s+)?scope\b",
        " ",
        lowered,
    )
    lowered = re.sub(
        rf"\b(?:unused|not\s+used)\s+{future_terms}\b",
        " ",
        lowered,
    )
    return lowered

def stage_scope_forbidden_terms(brief: str) -> tuple[str, ...]:
    """Infer stage-local predicates that generated tests must not assert.

    This is intentionally conservative and proposition-oriented: when the
    current stage declares a limited domain such as single-page, leaf-only,
    parser-only, or smoke-only, generated tests may not introduce predicates
    from broader stages unless those predicates are explicitly part of the
    current stage text.
    """
    scope = _current_stage_scope_text(brief)
    normalized = _normalized_scope_text(scope)
    forbidden: list[str] = []

    def has(*terms: str) -> bool:
        return any(_normalized_scope_text(term) in normalized for term in terms)

    def add(*terms: str) -> None:
        forbidden.extend(terms)

    if has("single-page", "single page") or (has("leaf") and not has("split", "internal", "multi leaf", "multi-page")):
        add(
            "split",
            "page split",
            "root split",
            "multi-page",
            "multi page",
            "multi-leaf",
            "multi leaf",
            "internal root",
            "internal page",
            "InternalPage",
            "rightmost_child",
            "large_inserts",
            "large inserts",
        )
    if has("parser-only", "parser only"):
        add("execute", "execution", "storage", "btree", "planner", "optimizer", "transaction")
    if has("smoke-only", "smoke only"):
        add("integration", "end-to-end", "end to end", "persistence", "stress", "performance")
    return tuple(unique_ordered(forbidden))

def _generated_blocks_for_stage_scope_lint(text: str) -> list[tuple[str | None, str]]:
    blocks: list[tuple[str | None, str]] = list(json_generated_blocks(text))
    for candidate in artifact_candidate_texts(text):
        for match in re.finditer(
            r"^BEGIN_(?:APPEND_)?FILE:\s*(?P<path>[^\n]+)\n(?P<content>.*?)\nEND_(?:APPEND_)?FILE(?:\s*:\s*[^\n]+)?\s*$",
            candidate,
            flags=re.DOTALL | re.MULTILINE,
        ):
            blocks.append(
                (normalize_legacy_file_artifact_path(match.group("path")), strip_markdown_fence(match.group("content")))
            )
        for match in re.finditer(
            r"^BEGIN_(?:APPEND_)?FILE\s*\n(?P<path>[^\n]+)\n(?P<content>.*?)\nEND_(?:APPEND_)?FILE(?:\s*:\s*[^\n]+)?\s*$",
            candidate,
            flags=re.DOTALL | re.MULTILINE,
        ):
            blocks.append(
                (normalize_legacy_file_artifact_path(match.group("path")), strip_markdown_fence(match.group("content")))
            )
        for match in re.finditer(
            r"^\s*BEGIN_SEARCH_REPLACE:\s*(?P<path>[^\n]+)\n\s*<<<<<<< SEARCH\n(?P<search>.*?)\n\s*=======\n(?P<replace>.*?)\n\s*>>>>>>> REPLACE",
            candidate,
            flags=re.DOTALL | re.MULTILINE,
        ):
            blocks.append((match.group("path").strip(), clean_artifact_block(match.group("replace"))))
    try:
        for artifact in unclosed_file_artifacts(text, ArtifactPathPolicy(allowed_paths=(), allow_extra_new_files=True)):
            blocks.append((artifact.path, artifact.content))
    except RunnerError:
        pass
    for artifact in loose_python_function_replacement_artifacts(
        text,
        ArtifactPathPolicy(allowed_paths=(), allow_extra_new_files=True),
    ):
        blocks.append((artifact.path, artifact.replace))
    return blocks

def lint_stage_scope_output(
    text: str,
    brief: str,
    stage_generated_test_paths: Sequence[str] = (),
    check_product_paths: bool = False,
) -> list[ArtifactLintFinding]:
    forbidden_terms = stage_scope_forbidden_terms(brief)
    if not forbidden_terms:
        return []
    generated_test_path_set = set(normalize_project_relative_paths(stage_generated_test_paths))
    findings: list[ArtifactLintFinding] = []
    for path, block in _generated_blocks_for_stage_scope_lint(text):
        normalized_path = normalize_project_relative_paths([path])[0] if path else ""
        if generated_test_path_set:
            is_generated_test = normalized_path in generated_test_path_set
        else:
            is_generated_test = normalized_path.startswith("tests/")
        is_product_artifact = bool(normalized_path) and not normalized_path.startswith("tests/")
        if not is_generated_test and not (check_product_paths and is_product_artifact):
            continue
        comparable_block = _scope_text_without_negated_terms(block)
        block_lower = comparable_block.lower()
        block_normalized = _normalized_scope_text(comparable_block)
        matched_terms = [
            term
            for term in forbidden_terms
            if term.lower() in block_lower or _normalized_scope_text(term) in block_normalized
        ]
        if matched_terms:
            findings.append(
                ArtifactLintFinding(
                    severity="error",
                    code="stage_scope_violation",
                    message=(
                        "generated stage artifact asserts predicates outside the current stage scope: "
                        + ", ".join(unique_ordered(matched_terms)[:8])
                    ),
                    path=normalized_path or "tests",
                )
            )
    return findings

def lint_semantic_contracts(
    generated_blocks: Sequence[tuple[str | None, str]],
    semantic_contracts: Sequence[SemanticContract],
) -> list[ArtifactLintFinding]:
    findings: list[ArtifactLintFinding] = []
    contract_text = "\n".join(contract.text.lower() for contract in semantic_contracts)
    if not contract_text:
        return findings

    for path, block in generated_blocks:
        lowered = block.lower()
        relevant_path = path or ""
        if "token.type must be string-compatible" in contract_text or "token.type against string literals" in contract_text:
            string_enum = (
                "class tokentype(str, enum" in lowered
                or "class tokentype(str, enum" in lowered
                or "class tokentype(str," in lowered
            )
            if relevant_path.endswith("lexer.py") and not string_enum and ("type: tokentype" in lowered or "token(tokentype." in lowered):
                findings.append(
                    ArtifactLintFinding(
                        severity="error",
                        code="semantic_contract_token_type_string",
                        message="semantic contract says Token.type must remain string-compatible, but artifact uses raw TokenType enum values",
                        path=path,
                    )
                )
        if "keywords must map to their keyword tokentype" in contract_text:
            if relevant_path.endswith("lexer.py") and (
                "upper()" not in block
                or "_KEYWORDS" not in block
                or "TokenType.IDENTIFIER" not in block
            ):
                findings.append(
                    ArtifactLintFinding(
                        severity="error",
                        code="semantic_contract_keyword_mapping",
                        message="semantic contract requires keyword lookup before IDENTIFIER fallback",
                        path=path,
                    )
                )
        if "negative integer literals must preserve" in contract_text:
            if relevant_path.endswith("lexer.py") and "-?" not in block and "append(self._advance())" not in block:
                findings.append(
                    ArtifactLintFinding(
                        severity="error",
                        code="semantic_contract_negative_integer_sign",
                        message="semantic contract requires negative integer token values to keep the '-' sign",
                        path=path,
                    )
                )
        if "must not append an extra eof token" in contract_text or "do not add hidden/sentinel tokens" in contract_text:
            if relevant_path.endswith("lexer.py") and ("tokentype.eof" in lowered or 'token("eof"' in lowered or "append(token" in lowered and "eof" in lowered):
                findings.append(
                    ArtifactLintFinding(
                        severity="error",
                        code="semantic_contract_no_extra_eof",
                        message="semantic contract says tokenize must not append hidden EOF tokens for the current tests",
                        path=path,
                    )
                )
        if "must tokenize '*' as a star token" in contract_text:
            if relevant_path.endswith("lexer.py") and "*" not in block:
                findings.append(
                    ArtifactLintFinding(
                        severity="error",
                        code="semantic_contract_star_missing",
                        message="semantic contract requires '*' STAR handling, but lexer artifact does not mention '*'",
                        path=path,
                    )
                )
        if "pager.write_page(page_id, data) and pager.read_page(page_id) must round-trip exact page_size bytes" in contract_text:
            if relevant_path.endswith("pager.py"):
                violating_functions = []
                for function_name in ("read_page", "write_page"):
                    function_source = python_function_source(block, function_name)
                    function_lowered = function_source.lower()
                    if (
                        function_source
                        and "page_type" in function_lowered
                        and ("page_type_leaf" in function_lowered or "page_type_internal" in function_lowered)
                        and ("raise corruptdatabaseerror" in function_lowered or "raise storageerror" in function_lowered)
                    ):
                        violating_functions.append(function_name)
                if violating_functions:
                    findings.append(
                        ArtifactLintFinding(
                            severity="error",
                            code="semantic_contract_pager_raw_page_io",
                            message=(
                                "semantic contract requires pager raw page IO round-trip, but artifact validates "
                                "B+Tree page_type in " + ", ".join(violating_functions)
                            ),
                            path=path,
                        )
                    )
        if "pager.allocate_page() must create a zero-filled page_size page" in contract_text:
            if relevant_path.endswith("pager.py"):
                function_source = python_function_source(block, "allocate_page")
                function_lowered = function_source.lower()
                if function_source and ("page_type_leaf" in function_lowered or "page_type_internal" in function_lowered):
                    findings.append(
                        ArtifactLintFinding(
                            severity="error",
                            code="semantic_contract_pager_zero_allocation",
                            message="semantic contract requires zero-filled allocated pages, but allocate_page initializes a typed B+Tree page",
                            path=path,
                        )
                    )
    return findings

def repeated_json_search_replace_score(text: str) -> tuple[int, int]:
    """Return (max_duplicate_count, total_json_search_replace_count)."""
    keys = [
        (match.group("path"), match.group("search"), match.group("replace"))
        for match in JSON_SEARCH_REPLACE_PATTERN.finditer(text)
    ]
    if not keys:
        return 0, 0
    counts = Counter(keys)
    return max(counts.values()), len(keys)

def json_search_replace_paths(text: str) -> list[str]:
    return unique_ordered(match.group("path") for match in JSON_SEARCH_REPLACE_PATTERN.finditer(text))

def repeated_text_run_score(text: str) -> tuple[int, str]:
    """Return a conservative consecutive repeated-token/line runaway score."""
    max_score = 0
    label = ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines:
        current_line = ""
        current_line_run = 0
        max_line_run = 0
        max_line = ""
        for line in lines:
            if line == current_line:
                current_line_run += 1
            else:
                current_line = line
                current_line_run = 1
            if current_line_run > max_line_run:
                max_line_run = current_line_run
                max_line = line
        if max_line_run > max_score and len(max_line) >= 12:
            max_score = max_line_run
            label = f"line_run:{truncate_text(max_line, 80)}"

    tokens = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b", text)
    if tokens:
        current_token = ""
        current_run = 0
        max_run = 0
        max_run_token = ""
        for token in tokens:
            if token == current_token:
                current_run += 1
            else:
                current_token = token
                current_run = 1
            if current_run > max_run:
                max_run = current_run
                max_run_token = token
        if max_run > max_score:
            max_score = max_run
            label = f"token_run:{max_run_token}"
    return max_score, label

def streamed_artifact_paths(text: str) -> list[str]:
    """Extract target paths from incomplete streamed artifact output."""
    paths: list[str] = []
    for match in re.finditer(r"(?m)^BEGIN_(?:APPEND_)?FILE:\s*(?P<path>[^\n]+)\s*$", text):
        paths.append(match.group("path"))
    for match in re.finditer(r"(?m)^BEGIN_SEARCH_REPLACE:\s*(?P<path>[^\n]+)\s*$", text):
        paths.append(match.group("path"))
    for match in re.finditer(r'"path"\s*:\s*"(?P<path>(?:\\.|[^"\\])*)"', text):
        raw = match.group("path")
        try:
            paths.append(json.loads(f'"{raw}"'))
        except json.JSONDecodeError:
            paths.append(raw)

    normalized: list[str] = []
    for raw_path in paths:
        cleaned = normalize_legacy_file_artifact_path(str(raw_path).strip())
        try:
            normalized.extend(normalize_project_relative_paths([cleaned], "stream artifact path"))
        except RunnerError:
            continue
    return unique_ordered(normalized)

def salvage_completed_artifact_prefix_before_readonly_path(
    text: str,
    artifact_policy: ArtifactPathPolicy | None,
) -> str | None:
    """Return complete writable artifacts before the first readonly artifact marker.

    This is a conservative stream recovery path. It never authorizes editing a
    readonly path; it only keeps the already-complete prefix if the normal
    artifact parsers can validate that prefix against the active path policy.
    """
    if artifact_policy is None or not artifact_policy.readonly_paths:
        return None

    readonly = set(artifact_policy.readonly_paths)
    readonly_offsets: list[int] = []
    marker_patterns = (
        r"(?m)^BEGIN_(?:APPEND_)?FILE:\s*(?P<path>[^\n]+)\s*$",
        r"(?m)^BEGIN_SEARCH_REPLACE:\s*(?P<path>[^\n]+)\s*$",
    )
    for pattern in marker_patterns:
        for match in re.finditer(pattern, text):
            raw_path = normalize_legacy_file_artifact_path(match.group("path").strip())
            try:
                normalized_paths = normalize_project_relative_paths([raw_path], "stream artifact path")
            except RunnerError:
                continue
            if normalized_paths and normalized_paths[0] in readonly:
                readonly_offsets.append(match.start())

    if not readonly_offsets:
        return None

    prefix = text[: min(readonly_offsets)].rstrip()
    if not prefix or not contains_artifact_markers(prefix):
        return None
    if legacy_file_end_count(prefix) < 1 and "END_SEARCH_REPLACE" not in prefix:
        return None

    try:
        replacements, files = extract_json_artifacts(prefix, artifact_policy)
        if replacements or files:
            return prefix + "\n"
    except RunnerError:
        pass
    try:
        if extract_search_replace_artifacts(prefix, artifact_policy):
            return prefix + "\n"
    except RunnerError:
        pass
    try:
        if extract_file_artifacts(prefix, artifact_policy):
            return prefix + "\n"
    except RunnerError:
        pass
    return None

def artifact_stream_guard(
    text: str,
    duplicate_threshold: int = 8,
    json_search_replace_atomic_threshold: int = 6,
    total_threshold: int = 40,
    repeated_text_threshold: int = 80,
    non_artifact_prefix_threshold: int = 2048,
    narrative_prefix_threshold: int = 256,
    search_replace_artifact_threshold: int = ARTIFACT_OUTPUT_BUDGET_BYTES * 2,
    python_file_artifact_threshold: int = ARTIFACT_OUTPUT_BUDGET_BYTES * 3,
    single_artifact_mode: bool = False,
    artifact_policy: ArtifactPathPolicy | None = None,
) -> ArtifactStreamGuardResult:
    encoded_len = len(text.encode("utf-8"))
    artifact_offset = first_artifact_marker_offset(text)
    stripped = text.lstrip()
    budget_artifact_offset = artifact_offset
    if budget_artifact_offset < 0 and stripped.startswith("{") and '"artifacts"' in stripped[:non_artifact_prefix_threshold]:
        budget_artifact_offset = len(text) - len(stripped)
    has_streamed_search_replace = (
        "BEGIN_SEARCH_REPLACE:" in text
        or bool(re.search(r'"type"\s*:\s*"search_replace"', text))
    )
    has_streamed_python_file_artifact = bool(
        re.search(r"(?m)^BEGIN_(?:APPEND_)?FILE:\s*[^\n]+\.py\s*$", text)
        or re.search(
            r'"type"\s*:\s*"(?:replace_file|file|append_file)"\s*,\s*"path"\s*:\s*"[^"]+\.py"',
            text,
        )
    )
    has_streamed_json_file_artifact = bool(
        re.search(
            r'"type"\s*:\s*"(?:replace_file|file|append_file)"\s*,\s*"path"\s*:\s*"[^"]+"',
            text,
        )
    )
    has_streamed_legacy_file_artifact = bool(
        re.search(r"(?m)^BEGIN_(?:APPEND_)?FILE:\s*[^\n]+\s*$", text)
    )
    legacy_file_artifact_count = len(
        re.findall(r"(?m)^BEGIN_(?:APPEND_)?FILE:\s*[^\n]+\s*$", text)
    )
    json_file_artifact_count = len(
        re.findall(
            r'"type"\s*:\s*"(?:replace_file|file|append_file)"\s*,\s*"path"\s*:\s*"[^"]+"',
            text,
        )
    )
    unified_diff_artifact_count = len(re.findall(r"(?m)^diff --git a/[^\n]+ b/[^\n]+\s*$", text))
    has_streamed_python_diff_artifact = bool(
        re.search(r"(?m)^diff --git a/[^\n]+\.py b/[^\n]+\.py\s*$", text)
        and re.search(r"(?m)^\+\+\+ b/[^\n]+\.py\s*$", text)
    )
    if artifact_policy is not None and artifact_policy.readonly_paths:
        readonly = set(artifact_policy.readonly_paths)
        for streamed_path in streamed_artifact_paths(text):
            if streamed_path in readonly:
                return ArtifactStreamGuardResult(
                    should_abort=True,
                    reason=(
                        f"artifact attempted to edit read-only evidence path {streamed_path}; "
                        "emit one product-code artifact instead"
                    ),
                    code="stream_readonly_artifact_path",
                    score=1,
                    threshold=0,
                )
    if has_streamed_json_file_artifact and has_streamed_legacy_file_artifact:
        return ArtifactStreamGuardResult(
            should_abort=True,
            reason=(
                "stream mixed JSON file artifacts with BEGIN_FILE artifacts; "
                "choose exactly one artifact protocol"
            ),
            code="stream_mixed_artifact_formats",
            score=1,
            threshold=0,
        )
    if single_artifact_mode:
        if legacy_file_artifact_count > 1:
            return ArtifactStreamGuardResult(
                should_abort=True,
                reason=(
                    "multiple BEGIN_FILE artifacts appeared in one repair stream; "
                    "emit exactly one artifact for one target file"
                ),
                code="stream_multiple_file_artifacts_in_repair",
                score=legacy_file_artifact_count,
                threshold=1,
            )
        if json_file_artifact_count > 1:
            return ArtifactStreamGuardResult(
                should_abort=True,
                reason=(
                    "multiple JSON file artifacts appeared in one repair stream; "
                    "emit exactly one artifact for one target file"
                ),
                code="stream_multiple_file_artifacts_in_repair",
                score=json_file_artifact_count,
                threshold=1,
            )
        if unified_diff_artifact_count > 1:
            return ArtifactStreamGuardResult(
                should_abort=True,
                reason=(
                    "multi-file unified diff appeared in one repair stream; "
                    "emit exactly one artifact for one target file"
                ),
                code="stream_multiple_file_artifacts_in_repair",
                score=unified_diff_artifact_count,
                threshold=1,
            )
    if stripped.startswith("```") and artifact_offset >= 0:
        return ArtifactStreamGuardResult(
            should_abort=True,
            reason=(
                "markdown fenced output wrapped an artifact; remove the fence and "
                "start with the artifact marker directly"
            ),
            code="stream_markdown_fence_before_artifact",
            score=artifact_offset,
            threshold=0,
        )
    narrative_prefix_re = re.compile(
        r"(?is)^\s*(?:"
        r"looking at|let me|i need to|i should|i will|we need to|"
        r"previous failures|current failure|supervisor state|required output|"
        r"the evidence|from the evidence|analysis\b|reasoning\b"
        r")"
    )
    if (
        encoded_len >= narrative_prefix_threshold
        and artifact_offset > 0
        and narrative_prefix_re.search(text)
    ):
        return ArtifactStreamGuardResult(
            should_abort=True,
            reason=(
                "process analysis appeared before the first artifact marker; "
                "artifact-only repair must start with the artifact marker"
            ),
            code="stream_prose_before_artifact",
            score=artifact_offset,
            threshold=0,
        )
    if (
        encoded_len >= narrative_prefix_threshold
        and artifact_offset < 0
        and narrative_prefix_re.search(text)
    ):
        return ArtifactStreamGuardResult(
            should_abort=True,
            reason=(
                "process analysis exceeded the repair prefix budget without a valid artifact marker"
            ),
            code="stream_non_artifact_output",
            score=encoded_len,
            threshold=narrative_prefix_threshold,
        )
    max_duplicate_count, total_json_search_replace_count = repeated_json_search_replace_score(text)
    if max_duplicate_count >= duplicate_threshold:
        return ArtifactStreamGuardResult(
            should_abort=True,
            reason=(
                "same JSON search_replace artifact repeated "
                f"{max_duplicate_count} times while streaming"
            ),
            code="stream_repeated_json_search_replace",
            score=max_duplicate_count,
            threshold=duplicate_threshold,
        )
    json_search_replace_path_count = len(json_search_replace_paths(text))
    if total_json_search_replace_count > json_search_replace_atomic_threshold:
        return ArtifactStreamGuardResult(
            should_abort=True,
            reason=(
                "too many JSON search_replace artifacts appeared in one repair stream; "
                "emit a smaller same-file edit set or one atomic BEGIN_SEARCH_REPLACE edit"
            ),
            code="stream_multiple_json_search_replace",
            score=total_json_search_replace_count,
            threshold=json_search_replace_atomic_threshold,
        )
    if single_artifact_mode and json_search_replace_path_count > 1:
        return ArtifactStreamGuardResult(
            should_abort=True,
            reason=(
                "JSON search_replace artifacts touched multiple paths in single-artifact mode; "
                "emit one target file per repair round"
            ),
            code="stream_multiple_json_search_replace",
            score=json_search_replace_path_count,
            threshold=1,
        )
    if budget_artifact_offset < 0 and re.search(r"(?m)^\s*<<<<<<< SEARCH\b", text):
        return ArtifactStreamGuardResult(
            should_abort=True,
            reason=(
                "search/replace body appeared without `BEGIN_SEARCH_REPLACE: path`; "
                "emit the artifact header before `<<<<<<< SEARCH`"
            ),
            code="stream_orphan_search_replace",
            score=1,
            threshold=0,
        )
    if budget_artifact_offset >= 0 and has_streamed_search_replace:
        malformed_header_issues = malformed_search_replace_header_issues(text, "stream_artifact")
        if malformed_header_issues:
            issue = malformed_header_issues[0]
            return ArtifactStreamGuardResult(
                should_abort=True,
                reason=issue.message,
                code=issue.code,
                score=1,
                threshold=0,
            )
        try:
            recoverable_multi_pair_artifacts = multi_pair_search_replace_artifacts(
                text,
                ArtifactPathPolicy(allowed_paths=(), allow_extra_new_files=True),
            )
        except RunnerError:
            recoverable_multi_pair_artifacts = []
        if not recoverable_multi_pair_artifacts:
            for match in re.finditer(
                r"^\s*BEGIN_SEARCH_REPLACE:\s*(?P<path>[^\n]+)\n\s*<<<<<<< SEARCH\n(?P<search>.*?)\n\s*=======\n(?P<replace>.*?)\n\s*>>>>>>> REPLACE",
                text,
                flags=re.DOTALL | re.MULTILINE,
            ):
                search = clean_artifact_block(match.group("search"))
                replace = clean_artifact_block(match.group("replace"))
                if search == replace:
                    return ArtifactStreamGuardResult(
                        should_abort=True,
                        reason=(
                            "search_replace artifact has identical search and replacement text; "
                            "emit a real behavioral change or return MISSING_CONTEXT"
                        ),
                        code="stream_identical_search_replace",
                        score=1,
                        threshold=0,
                    )
                if contains_conflict_markers(search) or contains_conflict_markers(replace):
                    return ArtifactStreamGuardResult(
                        should_abort=True,
                        reason=(
                            "search_replace content contains nested conflict/artifact markers; "
                            "emit one valid atomic artifact"
                        ),
                        code="stream_search_replace_conflict_markers",
                        score=1,
                        threshold=0,
                    )
        artifact_bytes = len(text[budget_artifact_offset:].encode("utf-8"))
        if artifact_bytes >= search_replace_artifact_threshold:
            return ArtifactStreamGuardResult(
                should_abort=True,
                reason=(
                    "search/replace artifact exceeded the stream size budget; "
                    "emit one smaller atomic edit"
                ),
                code="stream_artifact_too_large",
                score=artifact_bytes,
                threshold=search_replace_artifact_threshold,
            )
        if re.search(
            r"(?i)\b(?:let me|i need to|i should|i am confused|i'm confused|"
            r"i see that|i already|i just|reconsider|thinking through)\b",
            text[budget_artifact_offset:],
        ):
            return ArtifactStreamGuardResult(
                should_abort=True,
                reason="process narration appeared inside an artifact stream",
                code="stream_artifact_process_narration",
                score=1,
                threshold=0,
            )
    if budget_artifact_offset >= 0 and has_streamed_python_file_artifact:
        python_begin_offsets = [
            match.start()
            for match in re.finditer(r"(?m)^BEGIN_(?:APPEND_)?FILE:\s*[^\n]+\.py\s*$", text)
        ]
        if python_begin_offsets:
            # Enforce the budget per Python file block. Multi-file stages should
            # be able to emit two small generated files without the second file
            # being charged for the first one's bytes.
            artifact_bytes = len(text[python_begin_offsets[-1]:].encode("utf-8"))
        else:
            artifact_bytes = len(text[budget_artifact_offset:].encode("utf-8"))
        effective_python_file_threshold = python_file_artifact_threshold
        if artifact_bytes >= effective_python_file_threshold:
            return ArtifactStreamGuardResult(
                should_abort=True,
                reason=(
                    "Python file artifact exceeded the stream size budget; "
                    "emit one smaller module, a minimal diff, or a focused search_replace"
                ),
                code="stream_python_file_artifact_too_large",
                score=artifact_bytes,
                threshold=effective_python_file_threshold,
            )
    if budget_artifact_offset >= 0 and has_streamed_python_diff_artifact:
        artifact_bytes = len(text[budget_artifact_offset:].encode("utf-8"))
        if artifact_bytes >= python_file_artifact_threshold:
            return ArtifactStreamGuardResult(
                should_abort=True,
                reason=(
                    "Python unified diff artifact exceeded the stream size budget; "
                    "emit balanced BEGIN_FILE blocks for missing generated files"
                ),
                code="stream_python_diff_artifact_too_large",
                score=artifact_bytes,
                threshold=python_file_artifact_threshold,
            )
    if (
        encoded_len >= non_artifact_prefix_threshold
        and artifact_offset < 0
        and stripped.startswith("```")
    ):
        return ArtifactStreamGuardResult(
            should_abort=True,
            reason=(
                "markdown fenced output exceeded the artifact prefix budget "
                "without a valid artifact marker"
            ),
            code="stream_markdown_fence_before_artifact",
            score=encoded_len,
            threshold=non_artifact_prefix_threshold,
        )
    if (
        encoded_len >= non_artifact_prefix_threshold
        and stripped.startswith("{")
        and '"artifacts"' not in stripped[:non_artifact_prefix_threshold]
        and re.search(r"(?m)^BEGIN_(?:APPEND_)?FILE(?::|\s*$)|^BEGIN_SEARCH_REPLACE:", text)
    ):
        return ArtifactStreamGuardResult(
            should_abort=True,
            reason=(
                "non-artifact JSON plan was mixed with file artifacts while streaming"
            ),
            code="stream_json_plan_before_artifact",
            score=encoded_len,
            threshold=non_artifact_prefix_threshold,
        )
    if artifact_offset > non_artifact_prefix_threshold:
        return ArtifactStreamGuardResult(
            should_abort=True,
            reason=(
                "prose before the first artifact marker exceeded the stream prefix budget"
            ),
            code="stream_prose_before_artifact",
            score=artifact_offset,
            threshold=non_artifact_prefix_threshold,
        )
    if (
        encoded_len >= non_artifact_prefix_threshold
        and artifact_offset < 0
        and not stripped.startswith("{")
        and not stripped.lower().startswith(("<!doctype", "<html"))
        and not re.match(r"(?m)^\s*(?:def\s+|async\s+def\s+|class\s+|import\s+|from\s+|@|[A-Za-z_][A-Za-z0-9_]*\s*=)", stripped)
    ):
        return ArtifactStreamGuardResult(
            should_abort=True,
            reason="stream exceeded the non-artifact output budget without any valid artifact marker",
            code="stream_non_artifact_output",
            score=encoded_len,
            threshold=non_artifact_prefix_threshold,
        )

    if total_json_search_replace_count >= total_threshold:
        return ArtifactStreamGuardResult(
            should_abort=True,
            reason=f"too many JSON search_replace artifacts while streaming: {total_json_search_replace_count}",
            code="stream_json_search_replace_excess",
            score=total_json_search_replace_count,
            threshold=total_threshold,
        )
    repeated_score, repeated_label = repeated_text_run_score(text)
    if repeated_score >= repeated_text_threshold:
        return ArtifactStreamGuardResult(
            should_abort=True,
            reason=f"repeated text runaway detected while streaming: {repeated_label} repeated {repeated_score} times",
            code="stream_repeated_text_runaway",
            score=repeated_score,
            threshold=repeated_text_threshold,
        )
    return ArtifactStreamGuardResult(False)

def root_cause_stream_guard(
    text: str,
    max_bytes: int = ROOT_CAUSE_OUTPUT_BUDGET_BYTES,
    max_line_bytes: int = 3000,
) -> ArtifactStreamGuardResult:
    encoded_len = len(text.encode("utf-8"))
    longest_line = max((len(line.encode("utf-8")) for line in text.splitlines()), default=0)
    if longest_line >= max_line_bytes:
        return ArtifactStreamGuardResult(
            should_abort=True,
            reason=(
                "root-cause analysis emitted an unbounded diagnostic bullet; "
                "return only the bounded schema fields"
            ),
            code="stream_root_cause_too_large",
            score=longest_line,
            threshold=max_line_bytes,
        )
    self_revision_count = len(
        re.findall(
            r"(?i)\b(?:wait|actually|unless|let me re-?examine|the only explanation|this should work)\b",
            text,
        )
    )
    if encoded_len >= 4096 and self_revision_count >= 4:
        return ArtifactStreamGuardResult(
            should_abort=True,
            reason=(
                "root-cause analysis is revising itself repeatedly instead of "
                "returning a bounded diagnosis"
            ),
            code="stream_root_cause_too_large",
            score=self_revision_count,
            threshold=4,
        )
    if encoded_len >= max_bytes:
        return ArtifactStreamGuardResult(
            should_abort=True,
            reason="root-cause analysis exceeded the diagnostic output budget; return a shorter report",
            code="stream_root_cause_too_large",
            score=encoded_len,
            threshold=max_bytes,
        )
    return ArtifactStreamGuardResult(False)

def python_struct_probe_document(
    project: Path | None,
    command_docs: Sequence[tuple[str, str]],
    max_files: int = 8,
) -> str | None:
    """Return deterministic struct facts for Python failures when useful.

    The runner should not ask an LLM to mentally calculate byte layouts when
    Python's ``struct.calcsize`` can settle the proposition exactly.  This probe
    is intentionally narrow: it reads traceback-mentioned Python files and
    evaluates only literal struct format strings via the standard library.
    """
    if project is None:
        return None
    combined = "\n".join(document for _name, document in command_docs)
    lowered = combined.lower()
    struct_trigger = (
        "struct.error" in lowered
        or "struct.unpack" in lowered
        or "struct.pack" in lowered
        or "header size mismatch" in lowered
        or "size mismatch" in lowered
    )
    if not struct_trigger:
        return None

    trace_paths: list[str] = []
    for raw_path in re.findall(r'File "([^"]+\.py)", line \d+', combined):
        try:
            path = Path(raw_path)
        except ValueError:
            continue
        rel = ""
        if path.is_absolute():
            try:
                rel = path.resolve().relative_to(project.resolve()).as_posix()
            except ValueError:
                if "/tests/" in raw_path:
                    rel = "tests/" + raw_path.split("/tests/", 1)[1]
                else:
                    continue
        else:
            rel = path.as_posix()
        if rel.startswith("tests/"):
            continue
        if ".." in Path(rel).parts:
            continue
        trace_paths.append(rel)
    trace_paths = unique_ordered(trace_paths)[:max_files]
    if not trace_paths:
        return None

    facts: list[str] = []
    constants: list[str] = []
    format_pattern = re.compile(
        r"struct\.(?P<func>pack|unpack|pack_into|unpack_from)\(\s*(?P<quote>['\"])(?P<fmt>[^'\"]+)(?P=quote)"
    )
    struct_ctor_pattern = re.compile(
        r"struct\.Struct\(\s*(?P<quote>['\"])(?P<fmt>[^'\"]+)(?P=quote)\s*\)"
    )
    constant_pattern = re.compile(r"^\s*([A-Z][A-Z0-9_]*(?:SIZE|LEN|LENGTH|BYTES))\s*=\s*(\d+)\s*(?:#.*)?$")
    for rel_path in trace_paths:
        path = project / rel_path
        if not path.exists() or not path.is_file():
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        seen_format_sites: set[tuple[int, str, str]] = set()
        try:
            tree = ast.parse(source)
        except SyntaxError:
            tree = None
        if tree is not None:
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "struct"
                    and func.attr in {"pack", "unpack", "pack_into", "unpack_from", "Struct"}
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    fmt = node.args[0].value
                    line_no = getattr(node, "lineno", 0) or 0
                    site = (line_no, func.attr, fmt)
                    seen_format_sites.add(site)
                    try:
                        size = struct.calcsize(fmt)
                    except struct.error as exc:
                        facts.append(f"- {rel_path}:{line_no} struct.{func.attr} format `{fmt}` is invalid: {exc}")
                        continue
                    facts.append(f"- {rel_path}:{line_no} struct.{func.attr} format `{fmt}` calcsize={size}")
        for line_no, line in enumerate(source.splitlines(), start=1):
            for match in format_pattern.finditer(line):
                fmt = match.group("fmt")
                site = (line_no, match.group("func"), fmt)
                if site in seen_format_sites:
                    continue
                try:
                    size = struct.calcsize(fmt)
                except struct.error as exc:
                    facts.append(f"- {rel_path}:{line_no} struct.{match.group('func')} format `{fmt}` is invalid: {exc}")
                    continue
                facts.append(f"- {rel_path}:{line_no} struct.{match.group('func')} format `{fmt}` calcsize={size}")
            for match in struct_ctor_pattern.finditer(line):
                fmt = match.group("fmt")
                try:
                    size = struct.calcsize(fmt)
                except struct.error as exc:
                    facts.append(f"- {rel_path}:{line_no} struct.Struct format `{fmt}` is invalid: {exc}")
                    continue
                facts.append(f"- {rel_path}:{line_no} struct.Struct format `{fmt}` calcsize={size}")
            constant_match = constant_pattern.match(line)
            if constant_match:
                constants.append(f"- {rel_path}:{line_no} {constant_match.group(1)}={constant_match.group(2)}")

    if not facts and not constants:
        return None
    lines = [
        "## Mechanical Probe: Python struct formats",
        "",
        "- status: PASS",
        "- rule: `struct.calcsize(format)` is authoritative for Python struct byte sizes.",
        "- trigger: executable evidence mentioned `struct` packing/unpacking.",
        "- source_files:",
        *[f"  - {path}" for path in trace_paths],
    ]
    if facts:
        lines.extend(["- calcsize_facts:", *facts])
    if constants:
        lines.extend(["- size_constants:", *constants])
    lines.extend(
        [
            "- invariant:",
            "  - Pack and unpack formats that describe the same binary record must have compatible field widths.",
            "  - Header-size constants and slices must match the actual serialized header layout.",
            "  - Do not override these facts with natural-language arithmetic.",
        ]
    )
    return "\n".join(lines)

def _python_function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args: list[str] = []
    defaults = list(node.args.defaults)
    default_offset = len(node.args.args) - len(defaults)
    for index, arg in enumerate(node.args.args):
        text = arg.arg
        if index >= default_offset:
            default_node = defaults[index - default_offset]
            try:
                text += "=" + ast.unparse(default_node)
            except Exception:
                text += "=..."
        args.append(text)
    if node.args.vararg:
        args.append("*" + node.args.vararg.arg)
    elif node.args.kwonlyargs:
        args.append("*")
    for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
        text = arg.arg
        if default is not None:
            try:
                text += "=" + ast.unparse(default)
            except Exception:
                text += "=..."
        args.append(text)
    if node.args.kwarg:
        args.append("**" + node.args.kwarg.arg)
    return f"{node.name}({', '.join(args)})"

def python_api_probe_document(
    project: Path | None,
    command_docs: Sequence[tuple[str, str]],
    max_classes: int = 6,
) -> str | None:
    """Return deterministic class API facts for AttributeError/TypeError loops."""
    if project is None:
        return None
    combined = "\n".join(document for _name, document in command_docs)
    class_names: list[str] = []
    missing_attrs: list[tuple[str, str]] = []
    attr_error_pattern = r"(?:AttributeError:\s*)?'([^']+)'\s+object has no attribute '([^']+)'"
    for class_name, attr in re.findall(attr_error_pattern, combined):
        class_names.append(class_name)
        missing_attrs.append((class_name, attr))
    for class_name in re.findall(r"TypeError:\s*([A-Za-z_][A-Za-z0-9_]*)\.__init__\(\)\s+takes\s+\d+\s+positional argument", combined):
        class_names.append(class_name)
    for class_name in re.findall(r"TypeError:\s*([A-Za-z_][A-Za-z0-9_]*)\(\)\s+takes no arguments", combined):
        class_names.append(class_name)
    class_names = unique_ordered(class_names)[:max_classes]
    if not class_names:
        return None

    facts: list[str] = []
    absent_facts: list[str] = []
    source_files: list[str] = []
    for class_name in class_names:
        owners = class_owner_paths_from_project(project, class_name)
        for rel_path in owners[:3]:
            path = project / rel_path
            if not path.exists() or not path.is_file():
                continue
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(source)
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef) or node.name != class_name:
                    continue
                source_files.append(rel_path)
                methods: list[ast.FunctionDef | ast.AsyncFunctionDef] = [
                    item
                    for item in node.body
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                ]
                signatures = {method.name: _python_function_signature(method) for method in methods}
                init_signature = signatures.get("__init__", "__init__(self)")
                public_methods = [
                    signature
                    for name, signature in signatures.items()
                    if not name.startswith("_")
                ]
                self_attrs = sorted(
                    {
                        target.attr
                        for method in methods
                        for target in ast.walk(method)
                        if isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"
                        and isinstance(target.ctx, ast.Store)
                    }
                )
                public_attrs = [attr for attr in self_attrs if not attr.startswith("_")]
                facts.append(f"- {rel_path}:{getattr(node, 'lineno', 0)} class `{class_name}`")
                facts.append(f"  - constructor: `{class_name}.{init_signature}`")
                if public_methods:
                    facts.append("  - public_methods:")
                    facts.extend(f"    - `{signature}`" for signature in public_methods)
                else:
                    facts.append("  - public_methods: []")
                if public_attrs:
                    facts.append("  - public_attrs:")
                    facts.extend(f"    - `{attr}`" for attr in public_attrs)
                method_names = set(signatures)
                known_attrs = method_names | set(self_attrs)
                for missing_class, attr in missing_attrs:
                    if missing_class == class_name and attr not in known_attrs:
                        absent_facts.append(f"- `{class_name}.{attr}` is absent from `{rel_path}`")
                break

    if not facts:
        return None
    lines = [
        "## Mechanical Probe: Python API surface",
        "",
        "- status: PASS",
        "- rule: AST class signatures and method names are authoritative for existing product APIs.",
        "- trigger: executable evidence mentioned Python AttributeError or constructor TypeError.",
        "- source_files:",
        *[f"  - {path}" for path in unique_ordered(source_files)],
        "- class_facts:",
        *facts,
    ]
    if absent_facts:
        lines.extend(["- absent_api_facts:", *unique_ordered(absent_facts)])
    lines.extend(
        [
            "- invariant:",
            "  - A product patch must not call a method that is absent from the probed class API.",
            "  - Constructor call sites must match the probed constructor signature unless the current stage explicitly changes that class.",
            "  - Do not invent compatibility methods when an existing public method can satisfy the current-stage behavior.",
        ]
    )
    return "\n".join(lines)

def expected_exception_precondition_probe_document(
    project: Path | None,
    command_docs: Sequence[tuple[str, str]],
    max_cases: int = 4,
) -> str | None:
    """Return deterministic facts for `ExpectedError not raised` loops.

    The probe is intentionally framed as a precondition-validation rule.  Some
    domains expose the concrete facts more clearly (for example SQL identifier
    validation), but the invariant is generic: expected exceptions must be
    checked before data-dependent iteration can skip the validation path.
    """
    if project is None:
        return None
    combined = "\n".join(document for _name, document in command_docs)
    expected_errors = unique_ordered(
        re.findall(r"AssertionError:\s*([A-Za-z_][A-Za-z0-9_]*)\s+not raised", combined)
    )
    if not expected_errors:
        return None
    trace_test_paths = unique_ordered(
        "tests/" + raw_path.split("/tests/", 1)[1]
        for raw_path in re.findall(r'File "([^"]+/tests/[^"]+\.py)"', combined)
    )
    if not trace_test_paths:
        trace_test_paths = sorted(
            str(path.relative_to(project))
            for path in project.glob("tests/test*.py")
            if path.is_file()
        )

    facts: list[str] = []
    source_files: list[str] = []
    case_count = 0
    for rel_path in trace_test_paths:
        path = project / rel_path
        if not path.exists() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        source_files.append(rel_path)
        method_matches = list(re.finditer(r"(?m)^    def (test_[A-Za-z0-9_]+)\(self\).*?:\n", text))
        for index, match in enumerate(method_matches):
            method_name = match.group(1)
            start = match.end()
            end = method_matches[index + 1].start() if index + 1 < len(method_matches) else len(text)
            block = text[start:end]
            matched_errors = [name for name in expected_errors if f"assertRaises({name})" in block]
            if not matched_errors:
                continue
            sql_strings = re.findall(r"\.execute\(\s*(?:f)?([\"'])(.*?)\1\s*\)", block, flags=re.DOTALL)
            sql_texts = [sql for _quote, sql in sql_strings]
            create_columns_by_table: dict[str, set[str]] = {}
            for sql in sql_texts:
                create_match = re.search(
                    r"(?is)\bCREATE\s+TABLE\s+([A-Za-z_][A-Za-z0-9_]*)\s*\((.*?)\)",
                    sql,
                )
                if not create_match:
                    continue
                table_name = create_match.group(1)
                columns: set[str] = set()
                for raw_col in create_match.group(2).split(","):
                    col_match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\b", raw_col)
                    if col_match:
                        columns.add(col_match.group(1))
                if columns:
                    create_columns_by_table[table_name] = columns
            for sql in sql_texts:
                select_match = re.search(
                    r"(?is)\bSELECT\b.*?\bFROM\s+([A-Za-z_][A-Za-z0-9_]*)\b.*?\bWHERE\s+([A-Za-z_][A-Za-z0-9_]*)\b",
                    sql,
                )
                if not select_match:
                    continue
                table_name, where_column = select_match.group(1), select_match.group(2)
                known_columns = create_columns_by_table.get(table_name, set())
                if known_columns and where_column not in known_columns:
                    facts.append(
                        f"- {rel_path}:{method_name}: expects {', '.join(matched_errors)} "
                        f"for SQL WHERE column `{where_column}` absent from `{table_name}` columns "
                        f"{sorted(known_columns)}"
                    )
                    case_count += 1
                    break

            if "TypeMismatchError" in matched_errors and "RecordCodec.validate_types" in block:
                values_match = re.search(r"(?m)^\s*values\s*=\s*(\[[^\n]+\])", block)
                types_match = re.search(r"(?m)^\s*expected_types\s*=\s*(\[[^\n]+\])", block)
                if values_match and types_match:
                    try:
                        values = ast.literal_eval(values_match.group(1))
                        expected_types = ast.literal_eval(types_match.group(1))
                    except (ValueError, SyntaxError):
                        values = expected_types = None
                    if isinstance(values, list) and isinstance(expected_types, list) and len(values) == len(expected_types):
                        mismatches: list[str] = []
                        matches: list[str] = []
                        for item_index, (value, expected_type) in enumerate(zip(values, expected_types)):
                            if value is None:
                                matches.append(f"{item_index}:NULL-compatible")
                                continue
                            if expected_type == "INTEGER":
                                ok = isinstance(value, int) and not isinstance(value, bool)
                            elif expected_type == "TEXT":
                                ok = isinstance(value, str)
                            else:
                                ok = False
                            description = f"{item_index}:{value!r}->{expected_type}"
                            if ok:
                                matches.append(description)
                            else:
                                mismatches.append(description)
                        if mismatches:
                            facts.append(
                                f"- {rel_path}:{method_name}: expects TypeMismatchError for literal type mismatch "
                                f"at {mismatches}; matching pairs: {matches}"
                            )
                        else:
                            facts.append(
                                f"- {rel_path}:{method_name}: TEST_ORACLE_CONFLICT candidate: "
                                f"assertRaises(TypeMismatchError) wraps RecordCodec.validate_types(values={values!r}, "
                                f"expected_types={expected_types!r}), but all literal pairs satisfy declared type predicates: {matches}"
                            )
                        case_count += 1
            if case_count >= max_cases:
                break
        if case_count >= max_cases:
            break

    lines = [
        "## Mechanical Probe: Precondition validation",
        "",
        "- status: PASS",
        "- rule: expected exception predicates are authoritative preconditions, not optional data-dependent outcomes.",
        f"- trigger: executable evidence reported `{', '.join(expected_errors[:4])} not raised`.",
    ]
    if source_files:
        lines.extend(["- source_files:", *[f"  - {path}" for path in unique_ordered(source_files)]])
    if facts:
        lines.extend(["- precondition_facts:", *facts])
    lines.extend(
        [
            "- invariant:",
            "  - If a public operation is expected to raise for invalid input, validate that input before returning success or an empty result.",
            "  - Do not rely on row iteration, loop bodies, callbacks, or data-dependent filters to perform identifier/type validation.",
            "  - For SQL-like WHERE predicates, validate referenced columns against schema before scanning rows; an empty table must still reject an invalid column.",
            "  - Reject root-cause hypotheses that depend on Python list comprehensions swallowing exceptions; Python comprehensions propagate exceptions.",
        ]
    )
    return "\n".join(lines)

def python_cli_probe_document(
    project: Path | None,
    command_docs: Sequence[tuple[str, str]],
) -> str | None:
    """Return deterministic facts for CLI string/dispatch contract failures."""
    if project is None:
        return None
    combined = "\n".join(document for _name, document in command_docs)
    lowered = combined.lower()
    facts: list[str] = []
    source_files: list[str] = []

    if "unknown dot command:" in lowered:
        for rel_path in project_python_product_paths(project):
            path = project / rel_path
            try:
                source = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if "line[1:].split()" not in source:
                continue
            dot_literals = unique_ordered(
                re.findall(r'cmd\s*==\s*"(\.[A-Za-z_][A-Za-z0-9_]*)"', source)
            )
            if not dot_literals:
                continue
            source_files.append(rel_path)
            unknown = unique_ordered(
                re.findall(r"Unknown dot command:\s*([A-Za-z_][A-Za-z0-9_]*)", combined)
            )
            facts.append(
                f"{rel_path} strips the leading dot with `line[1:].split()` before dispatch, "
                f"but handler comparisons use dot-prefixed literals {dot_literals[:6]}"
            )
            if unknown:
                facts.append(
                    f"observed unrecognized dot commands after stripping: {', '.join(unknown[:6])}"
                )

    if "test_empty_sql" in lowered and "minisqlite engine - connected" in lowered:
        for rel_path in project_python_product_paths(project):
            path = project / rel_path
            try:
                source = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if '" ".join(argv[1:]) if len(argv) > 1 else ""' not in source:
                continue
            if re.search(r"(?m)^\s*if\s+sql\s*:", source) and "Interactive mode" in source:
                source_files.append(rel_path)
                facts.append(
                    f"{rel_path} collapses no SQL argument and explicit empty SQL into `sql == \"\"`, "
                    "then routes both cases through the interactive `else` branch"
                )

    if "table 'users' does not exist" in lowered or "table 't' does not exist" in lowered:
        test_cli = project / "tests" / "test_cli.py"
        if test_cli.exists():
            try:
                test_source = test_cli.read_text(encoding="utf-8")
            except OSError:
                test_source = ""
            if "test_insert_and_select" in lowered and "CREATE TABLE users" in test_source:
                for rel_path in ("minisqlite/cli.py", "minisqlite/connection.py"):
                    if (project / rel_path).exists():
                        source_files.append(rel_path)
                facts.append(
                    "tests/test_cli.py:test_insert_and_select invokes `main([same_db_file, CREATE])`, "
                    "`main([same_db_file, INSERT])`, then `main([same_db_file, SELECT])`; expected SELECT "
                    "output means same database path CLI calls must observe prior committed schema and rows"
                )
            if "test_schema_command" in lowered and "Connection(self.db_file)" in test_source:
                for rel_path in ("minisqlite/cli.py", "minisqlite/connection.py"):
                    if (project / rel_path).exists():
                        source_files.append(rel_path)
                facts.append(
                    "tests/test_cli.py:test_schema_command creates schema through `Connection(self.db_file)`, "
                    "closes it, then invokes interactive `.schema t`; expected schema output means dot commands "
                    "must observe schema loaded from the same database path"
                )

    if not facts:
        return None

    lines = [
        "## Mechanical Probe: CLI command contracts",
        "",
        "- status: PASS",
        "- rule: CLI dispatch string transformations are authoritative.",
        "- trigger: executable evidence reported CLI command dispatch or empty-command failures.",
        "- source_files:",
        *[f"  - {path}" for path in unique_ordered(source_files)],
        "- facts:",
        *[f"  - {fact}" for fact in unique_ordered(facts)],
        "- invariant:",
        "  - If a caller strips a command prefix before dispatch, the handler must compare against stripped command names or the caller must pass the prefix through.",
        "  - No-argument interactive mode and explicit empty command mode are distinct states when tests or CLI contract distinguish them.",
        "  - Repeated CLI invocations with the same database path form one persistence contract when executable evidence issues CREATE/INSERT/SELECT across calls.",
        "  - Metadata dot commands must read the same schema state as normal SQL execution on the same connection/database path.",
        "  - Patch the CLI command normalization branch before changing tests or unrelated storage code.",
    ]
    return "\n".join(lines)

def python_cli_state_probe_document(
    project: Path | None,
    command_docs: Sequence[tuple[str, str]],
    timeout: float = 5.0,
) -> str | None:
    """Run a minimal local state probe for CLI/database persistence failures."""
    if project is None:
        return None
    combined = "\n".join(document for _name, document in command_docs)
    lowered = combined.lower()
    if not (
        "table 'users' does not exist" in lowered
        or "table 't' does not exist" in lowered
        or "1|alice" in lowered
        or "row" in lowered and "not found" in lowered
    ):
        return None
    if not (project / "minisqlite" / "connection.py").exists():
        return None
    probe_code = r'''
import json
import os
import tempfile

result = {"status": "unknown"}
path = tempfile.mktemp(suffix=".db")
direct_path = tempfile.mktemp(suffix=".db")
try:
    from minisqlite.connection import Connection
    from minisqlite.storage.pager import Pager
    c = Connection(path)
    c.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);")
    c.execute("INSERT INTO users (id, name) VALUES (1, 'Alice');")
    before_select = c.execute("SELECT * FROM users;")
    before_rows = getattr(before_select, "rows", None)
    before_tables = sorted(getattr(getattr(c, "_schema", None), "tables", {}).keys())
    before_payload_len = None
    if hasattr(getattr(c, "pager", None), "read_schema_metadata"):
        payload = c.pager.read_schema_metadata()
        before_payload_len = len(payload) if payload else 0
    c.close()
    after_payload_len = None
    try:
        p = Pager(path)
        p.open()
        if hasattr(p, "read_schema_metadata"):
            payload = p.read_schema_metadata()
            after_payload_len = len(payload) if payload else 0
        p.close()
    except Exception as exc:
        after_payload_len = f"probe_error:{type(exc).__name__}:{exc}"
    direct_after_write_len = None
    direct_after_flush_len = None
    direct_after_reopen_len = None
    try:
        p2 = Pager(direct_path)
        p2.open()
        if hasattr(p2, "write_schema_metadata") and hasattr(p2, "read_schema_metadata"):
            p2.write_schema_metadata(b"probe-schema")
            payload = p2.read_schema_metadata()
            direct_after_write_len = len(payload) if payload else 0
            p2.flush()
            payload = p2.read_schema_metadata()
            direct_after_flush_len = len(payload) if payload else 0
        p2.close()
        p3 = Pager(direct_path)
        p3.open()
        if hasattr(p3, "read_schema_metadata"):
            payload = p3.read_schema_metadata()
            direct_after_reopen_len = len(payload) if payload else 0
        p3.close()
    except Exception as exc:
        direct_after_reopen_len = f"probe_error:{type(exc).__name__}:{exc}"
    c2 = Connection(path)
    after_tables = sorted(getattr(getattr(c2, "_schema", None), "tables", {}).keys())
    after_rows = None
    try:
        after_select = c2.execute("SELECT * FROM users;")
        after_rows = getattr(after_select, "rows", None)
    except Exception as exc:
        after_rows = f"probe_error:{type(exc).__name__}:{exc}"
    c2.close()
    result = {
        "status": "ok",
        "before_close_tables": before_tables,
        "before_close_rows": before_rows,
        "before_close_schema_payload_len": before_payload_len,
        "after_reopen_schema_payload_len": after_payload_len,
        "after_reopen_tables": after_tables,
        "after_reopen_rows": after_rows,
        "direct_after_write_schema_payload_len": direct_after_write_len,
        "direct_after_flush_schema_payload_len": direct_after_flush_len,
        "direct_after_reopen_schema_payload_len": direct_after_reopen_len,
    }
except Exception as exc:
    result = {"status": "error", "error_type": type(exc).__name__, "error": str(exc)}
finally:
    if os.path.exists(path):
        os.unlink(path)
    if os.path.exists(direct_path):
        os.unlink(direct_path)
print(json.dumps(result, sort_keys=True))
'''
    env = {"PYTHONPATH": str(project)}
    try:
        completed = subprocess.run(
            ["python3", "-c", probe_code],
            cwd=project,
            env={**os.environ, **env},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "\n".join(
            [
                "## Mechanical Probe: CLI state persistence",
                "",
                "- status: ERROR",
                f"- probe_error: {type(exc).__name__}: {exc}",
                "- invariant:",
                "  - If stateful CLI tests fail, isolate direct API persistence before editing tests.",
            ]
        )
    try:
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        payload = {
            "status": "error",
            "stdout": completed.stdout[-500:],
            "stderr": completed.stderr[-500:],
            "exit_code": completed.returncode,
        }
    lines = [
        "## Mechanical Probe: CLI state persistence",
        "",
        "- status: PASS" if payload.get("status") == "ok" else "- status: ERROR",
        "- rule: local state transition probes are authoritative for persistence boundary failures.",
        "- trigger: executable evidence reported missing table after same database path command sequence.",
        "- observations:",
    ]
    for key in (
        "before_close_tables",
        "before_close_rows",
        "before_close_schema_payload_len",
        "after_reopen_schema_payload_len",
        "after_reopen_tables",
        "after_reopen_rows",
        "direct_after_write_schema_payload_len",
        "direct_after_flush_schema_payload_len",
        "direct_after_reopen_schema_payload_len",
        "error_type",
        "error",
        "exit_code",
    ):
        if key in payload:
            lines.append(f"  - {key}: {payload[key]}")
    lines.extend(
        [
            "- invariant:",
            "  - If direct API state exists before close but is absent after reopen, patch the persistence boundary, not CLI dispatch or tests.",
            "  - If schema payload is present before close and absent after reopen, patch the low-level schema metadata write/flush/read path.",
            "  - If direct Pager metadata exists after write but is absent after flush or reopen, patch Pager header/page-0 rewrite so flush/close preserves schema metadata.",
            "  - If schema payload survives but tables are absent after reopen, patch schema deserialization/loading.",
            "  - If rows exist before close but disappear after reopen while schema survives, patch row/page persistence below CLI before changing CLI output.",
        ]
    )
    return "\n".join(lines)


def python_storage_state_probe_document(
    project: Path | None,
    command_docs: Sequence[tuple[str, str]],
    timeout: float = 5.0,
) -> str | None:
    """Run a minimal storage state probe for page/persistence failures.

    This probe keeps LLM root-cause analysis grounded in observed runtime
    facts. It is intentionally activated only for storage persistence failures
    in Python projects that expose Pager, BPlusTree, and RecordCodec.
    """
    if project is None:
        return None
    combined = "\n".join(document for _name, document in command_docs)
    lowered = combined.lower()
    if not (
        "test_persistence_after_close_reopen" in lowered
        or "persists after closing and reopening" in lowered
        or "unexpectedly none" in lowered and "btree" in lowered
    ):
        return None
    required = [
        project / "minisqlite" / "storage" / "pager.py",
        project / "minisqlite" / "storage" / "btree.py",
        project / "minisqlite" / "storage" / "record.py",
    ]
    if not all(path.exists() for path in required):
        return None

    probe_code = r"""
import json
import os
import tempfile

result = {"status": "unknown"}
path = tempfile.mktemp(suffix=".db")
try:
    from minisqlite.storage.btree import BPlusTree
    from minisqlite.storage.file_format import PAGE_SIZE
    from minisqlite.storage.pager import Pager
    from minisqlite.storage.record import RecordCodec

    codec = RecordCodec()
    pager1 = Pager(path)
    first_page_id = pager1.allocate_page()
    root_page_id = pager1.allocate_page()
    before_insert_next_page_id = getattr(pager1, "next_page_id", None)
    btree1 = BPlusTree(pager1, root_page_id)
    payload = codec.encode([1, "Alice", 30])
    btree1.insert(1, payload)
    before_close_search = btree1.search(1)
    root_header_before_close = list(pager1.read_page(root_page_id)[:16])
    page1_header_before_close = list(pager1.read_page(1)[:16])
    pager1.close()

    pager2 = Pager(path)
    reopen_next_page_id = getattr(pager2, "next_page_id", None)
    page1_header_after_reopen = list(pager2.read_page(1)[:16])
    root_header_after_reopen = list(pager2.read_page(root_page_id)[:16])
    btree2_page1 = BPlusTree(pager2, 1)
    search_page1 = btree2_page1.search(1)
    btree2_root = BPlusTree(pager2, root_page_id)
    search_root = btree2_root.search(1)
    pager2.close()

    result = {
        "status": "ok",
        "first_allocate_page_id": first_page_id,
        "second_allocate_root_page_id": root_page_id,
        "before_insert_next_page_id": before_insert_next_page_id,
        "reopen_next_page_id": reopen_next_page_id,
        "before_close_search_is_none": before_close_search is None,
        "search_page1_is_none": search_page1 is None,
        "search_root_is_none": search_root is None,
        "payload_len": len(payload),
        "page_size": PAGE_SIZE,
        "page1_header_before_close": page1_header_before_close,
        "root_header_before_close": root_header_before_close,
        "page1_header_after_reopen": page1_header_after_reopen,
        "root_header_after_reopen": root_header_after_reopen,
    }
except Exception as exc:
    result = {"status": "error", "error_type": type(exc).__name__, "error": str(exc)}
finally:
    if os.path.exists(path):
        os.unlink(path)
print(json.dumps(result, sort_keys=True))
"""
    env = {"PYTHONPATH": str(project)}
    try:
        completed = subprocess.run(
            ["python3", "-c", probe_code],
            cwd=project,
            env={**os.environ, **env},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "\n".join(
            [
                "## Mechanical Probe: Python storage state",
                "",
                "- status: ERROR",
                f"- probe_error: {type(exc).__name__}: {exc}",
                "- invariant:",
                "  - If a storage persistence test fails, measure allocation and page headers before selecting a root cause.",
            ]
        )
    try:
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        payload = {
            "status": "error",
            "stdout": completed.stdout[-500:],
            "stderr": completed.stderr[-500:],
            "exit_code": completed.returncode,
        }
    lines = [
        "## Mechanical Probe: Python storage state",
        "",
        "- status: PASS" if payload.get("status") == "ok" else "- status: ERROR",
        "- rule: local storage state probes are authoritative for page allocation and persistence facts.",
        "- trigger: executable evidence reported a B-tree close/reopen persistence failure.",
        "- observations:",
    ]
    for key in (
        "first_allocate_page_id",
        "second_allocate_root_page_id",
        "before_insert_next_page_id",
        "reopen_next_page_id",
        "before_close_search_is_none",
        "search_page1_is_none",
        "search_root_is_none",
        "payload_len",
        "page_size",
        "page1_header_before_close",
        "root_header_before_close",
        "page1_header_after_reopen",
        "root_header_after_reopen",
        "error_type",
        "error",
        "exit_code",
    ):
        if key in payload:
            lines.append(f"  - {key}: {payload[key]}")
    lines.append("- invariant:")
    root_page_id = payload.get("second_allocate_root_page_id")
    reopen_next_page_id = payload.get("reopen_next_page_id")
    if isinstance(root_page_id, int) and isinstance(reopen_next_page_id, int):
        lines.append(
            f"  - `reopen_next_page_id - 1` equals {reopen_next_page_id - 1}, "
            f"which is not the observed root page {root_page_id}; do not use that formula."
        )
    lines.extend(
        [
            "  - Root-cause reports must use the probed page IDs instead of assuming allocation order.",
            "  - If search succeeds at the probed root page but fails at page 1, patch the page-id contract or allocation/header metadata, not B-tree cell parsing.",
            "  - If search fails at both page 1 and the probed root page, patch page write/read or B-tree serialization.",
            "  - Do not repeat a hypothesis contradicted by these observations.",
        ]
    )
    return "\n".join(lines)


def _last_int_observation(text: str, key: str) -> int | None:
    matches = re.findall(rf"(?m)^\s*-\s*{re.escape(key)}:\s*(-?\d+)\s*$", text)
    if not matches:
        return None
    try:
        return int(matches[-1])
    except ValueError:
        return None




def deterministic_project_policy_triage_from_evidence(
    trigger: str,
    evidence_doc: str,
    project: Path | None,
    stage_generated_test_paths: Sequence[str] = (),
) -> dict[str, object] | None:
    """Classify mechanically provable generated-test oracle conflicts."""
    if trigger != "generated_test_oracle_conflict":
        return None
    if "Mechanical Probe: Python storage state" not in evidence_doc:
        return None
    root_page_id = _last_int_observation(evidence_doc, "second_allocate_root_page_id")
    reopen_next_page_id = _last_int_observation(evidence_doc, "reopen_next_page_id")
    if root_page_id is None or reopen_next_page_id is None:
        return None
    search_page1_is_none = re.search(r"(?m)^\s*-\s*search_page1_is_none:\s*True\s*$", evidence_doc) is not None
    search_root_is_none = re.search(r"(?m)^\s*-\s*search_root_is_none:\s*False\s*$", evidence_doc) is not None
    if not (search_page1_is_none and search_root_is_none and root_page_id != 1):
        return None
    generated_tests = set(stage_generated_test_paths)
    btree_test = "tests/test_btree.py"
    if generated_tests and btree_test not in generated_tests:
        return None
    if project is None:
        return None
    btree_test_path = project / btree_test
    pager_test_path = project / "tests" / "test_pager.py"
    if not btree_test_path.exists() or not pager_test_path.exists():
        return None
    try:
        btree_test_text = btree_test_path.read_text(encoding="utf-8", errors="replace")
        pager_test_text = pager_test_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    btree_hardcodes_page1 = "root_page_id_2 = 1" in btree_test_text
    btree_discards_first_allocation = re.search(
        r"pager1\.allocate_page\(\)\s*#\s*header",
        btree_test_text,
        flags=re.IGNORECASE,
    ) is not None
    pager_allocates_from_one = "self.assertEqual(page_ids, [1, 2, 3, 4, 5])" in pager_test_text
    if not (btree_hardcodes_page1 and btree_discards_first_allocation and pager_allocates_from_one):
        return None
    return {
        "trigger": trigger,
        "case_type": "test_harness",
        "confidence": "high",
        "project_policy_basis": [
            "Mechanical Probe: page 1 is empty while the probed root page contains the persisted row.",
            "tests/test_pager.py asserts Pager.allocate_page() returns [1, 2, 3, 4, 5].",
            "tests/test_btree.py is a stage-owned generated test that discards the first allocation as '# header' and then hardcodes root_page_id_2 = 1.",
        ],
        "safe_next_action": "edit_test_harness",
        "editable_paths": [btree_test],
        "readonly_paths": [
            "tests/test_pager.py",
            "minisqlite/storage/pager.py",
            "minisqlite/storage/btree.py",
        ],
        "forbidden_actions": [
            "Do not change Pager.allocate_page() to satisfy a generated test that contradicts tests/test_pager.py.",
            "Do not use reopen_next_page_id - 1 when the probe shows it does not equal the observed root page.",
            "Do not patch BPlusTree cell parsing for this oracle conflict; search succeeds at the probed root page.",
        ],
        "rationale": "The repeated failure is caused by a stage-generated test setup that contradicts the earlier Pager allocation contract, not by row persistence loss.",
    }


def patch_plan_mechanical_probe_contradiction_document(
    patch_plan_doc: str,
    evidence_docs: Sequence[tuple[str, str]],
) -> str | None:
    """Reject patch plans that contradict deterministic probe arithmetic."""
    plan_lower = patch_plan_doc.lower()
    if not re.search(r"\bnext_page_id\s*-\s*1\b", plan_lower):
        return None
    evidence_text = "\n".join(document for _name, document in evidence_docs)
    if "Mechanical Probe: Python storage state" not in evidence_text:
        return None
    root_page_id = _last_int_observation(evidence_text, "second_allocate_root_page_id")
    reopen_next_page_id = _last_int_observation(evidence_text, "reopen_next_page_id")
    if root_page_id is None or reopen_next_page_id is None:
        return None
    proposed_page_id = reopen_next_page_id - 1
    if proposed_page_id == root_page_id:
        return None
    return "\n".join(
        [
            "## Patch Plan Mechanical Probe Contradiction",
            "",
            "- status: FAIL",
            "- failure_type: mechanical_probe_contradiction",
            "- rule: Patch plans must not use formulas contradicted by Mechanical Probe observations.",
            f"- contradicted_formula: `reopen_next_page_id - 1` = {proposed_page_id}",
            f"- observed_root_page_id: {root_page_id}",
            f"- observed_reopen_next_page_id: {reopen_next_page_id}",
            "- rejected_plan_fragment: next_page_id - 1",
            "",
            "Runner action:",
            "- Reject this patch plan before artifact generation.",
            "- Re-run root-cause planning using the observed page IDs as fixed propositions.",
            "- Choose a different page-id contract fix or request missing context instead of repeating the rejected formula.",
        ]
    )

def orphan_search_replace_marker_issues(text: str, code_prefix: str) -> list[SemanticRepairFormatIssue]:
    if "BEGIN_SEARCH_REPLACE" in text:
        return []
    if not re.search(r"(?m)^\s*<<<<<<< SEARCH\b", text):
        return []
    return [
        SemanticRepairFormatIssue(
            code=f"{code_prefix}_orphan_search_replace",
            message=(
                "search/replace markers were emitted without `BEGIN_SEARCH_REPLACE: path`; "
                "the file path header is required before `<<<<<<< SEARCH`"
            ),
        )
    ]

def malformed_search_replace_header_issues(text: str, code_prefix: str) -> list[SemanticRepairFormatIssue]:
    """Detect BEGIN_SEARCH_REPLACE blocks whose body starts with code, not grammar.

    Valid search/replace artifacts must place ``<<<<<<< SEARCH`` immediately
    after the path line. A narrow loose-Python recovery also permits a duplicate
    ``: path`` line followed by a fenced function. Local LLMs sometimes emit
    ``: def ...`` after the header; treating that as "no artifact" creates a
    wasteful format-repair loop, so classify it precisely.
    """
    issues: list[SemanticRepairFormatIssue] = []
    code_line_pattern = re.compile(
        r"^(?:@|async\s+def\b|def\b|class\b|from\b|import\b|if\b|for\b|while\b|try\b|with\b|return\b|"
        r"[A-Za-z_][A-Za-z0-9_]*\s*=)"
    )
    for candidate in artifact_candidate_texts(text):
        for match in re.finditer(
            r"(?m)^\s*BEGIN_SEARCH_REPLACE:\s*(?P<path>[^\n]+)\n(?P<line>[^\n]+)\n",
            candidate,
        ):
            path = normalize_legacy_file_artifact_path(match.group("path"))
            line = match.group("line").strip()
            if line.startswith("<<<<<<< SEARCH") or line.startswith("```"):
                continue
            if line.startswith(":"):
                after_colon = line[1:].strip()
                duplicate_path = normalize_legacy_file_artifact_path(after_colon)
                if duplicate_path and duplicate_path == path:
                    continue
                issues.append(
                    SemanticRepairFormatIssue(
                        code=f"{code_prefix}_malformed_search_replace",
                        message=(
                            "BEGIN_SEARCH_REPLACE body is malformed: after the path line, "
                            "emit `<<<<<<< SEARCH`; do not prefix code with ':'"
                        ),
                        path=path or None,
                    )
                )
                continue
            if code_line_pattern.match(line):
                issues.append(
                    SemanticRepairFormatIssue(
                        code=f"{code_prefix}_malformed_search_replace",
                        message=(
                            "BEGIN_SEARCH_REPLACE body starts with code instead of "
                            "the required `<<<<<<< SEARCH` marker"
                        ),
                        path=path or None,
                    )
                )
    return issues

def semantic_repair_format_issues(text: str) -> list[SemanticRepairFormatIssue]:
    issues: list[SemanticRepairFormatIssue] = []
    if extract_missing_context_requests(text):
        issues.append(
            SemanticRepairFormatIssue(
                code="semantic_repair_missing_context",
                message="semantic repair requested missing context and should be routed through context collection before artifact lint",
            )
        )

    stripped = text.strip()
    valid_search_replace = bool(VALID_SEARCH_REPLACE_PATTERN.match(stripped))
    valid_fenced_search_replace = bool(
        fenced_conflict_search_replace_artifacts(text, ArtifactPathPolicy(allowed_paths=(), allow_extra_new_files=True))
    )
    valid_unified_diff = stripped.startswith("diff --git ") and len(re.findall(r"(?m)^diff --git\s+a/[^\s]+\s+b/[^\s]+", stripped)) == 1

    if MALFORMED_SEARCH_REPLACE_WITHOUT_PATH_PATTERN.search(text):
        issues.append(
            SemanticRepairFormatIssue(
                code="semantic_repair_missing_path",
                message="BEGIN_SEARCH_REPLACE is missing the required ': path/to/file' line",
            )
        )
    issues.extend(orphan_search_replace_marker_issues(text, "semantic_repair"))
    issues.extend(malformed_search_replace_header_issues(text, "semantic_repair"))

    artifact_marker_offset = first_artifact_marker_offset(text)
    if artifact_marker_offset > 0 and text[:artifact_marker_offset].strip():
        issues.append(
            SemanticRepairFormatIssue(
                code="semantic_repair_prose_mixed",
                message="semantic repair output contains prose before the artifact marker",
            )
        )

    if "```" in text and not valid_fenced_search_replace:
        issues.append(
            SemanticRepairFormatIssue(
                code="semantic_repair_markdown_fence",
                message="semantic repair output must not wrap artifacts in Markdown fences",
            )
        )

    search_replace_markers = len(re.findall(r"(?m)^\s*BEGIN_SEARCH_REPLACE\b", text))
    diff_markers = len(re.findall(r"(?m)^diff --git\s+a/[^\s]+\s+b/[^\s]+", text))
    file_markers = legacy_file_begin_count(text)
    json_blocks = len(json_generated_blocks(text))
    if search_replace_markers + diff_markers + file_markers + json_blocks > 1:
        issues.append(
            SemanticRepairFormatIssue(
                code="semantic_repair_multiple_artifacts",
                message="semantic repair output contains more than one artifact candidate",
            )
        )
    if file_markers or json_blocks:
        issues.append(
            SemanticRepairFormatIssue(
                code="semantic_repair_forbidden_artifact",
                message="semantic repair must not use JSON, BEGIN_FILE, or BEGIN_APPEND_FILE artifacts",
            )
        )
    if not valid_search_replace and not valid_fenced_search_replace and not valid_unified_diff and not issues:
        issues.append(
            SemanticRepairFormatIssue(
                code="semantic_repair_not_atomic",
                message="semantic repair must be exactly one valid BEGIN_SEARCH_REPLACE block or one unified-diff file patch",
            )
        )
    return issues

def output_has_valid_artifact_candidate(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if recoverable_fenced_unified_diff(text):
        return True
    if json_generated_blocks(text):
        return True
    if VALID_SEARCH_REPLACE_PATTERN.match(stripped):
        return True
    if any(VALID_SEARCH_REPLACE_PATTERN.match(candidate.strip()) for candidate in artifact_candidate_texts(text)):
        return True
    if stripped.startswith("diff --git ") and re.search(r"(?m)^diff --git\s+a/[^\s]+\s+b/[^\s]+", stripped):
        return True
    begin_file_count = legacy_file_begin_count(text)
    end_file_count = legacy_file_end_count(text)
    if begin_file_count and begin_file_count == end_file_count:
        return True
    try:
        if unclosed_file_artifacts(text, ArtifactPathPolicy(allowed_paths=(), allow_extra_new_files=True)):
            return True
    except RunnerError:
        pass
    if loose_python_function_replacement_artifacts(text, ArtifactPathPolicy(allowed_paths=(), allow_extra_new_files=True)):
        return True
    if fenced_conflict_search_replace_artifacts(text, ArtifactPathPolicy(allowed_paths=(), allow_extra_new_files=True)):
        return True
    if fenced_path_file_artifacts(text, ArtifactPathPolicy(allowed_paths=(), allow_extra_new_files=True)):
        return True
    return False

def recoverable_fenced_unified_diff(text: str) -> bool:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return False
    blocks = markdown_fenced_blocks(stripped)
    if len(blocks) != 1:
        return False
    outside = re.sub(r"(?s)^\s*```[A-Za-z0-9_-]*\n.*?\n```\s*$", "", stripped).strip()
    if outside:
        return False
    block = blocks[0].strip()
    return block.startswith("diff --git ") and bool(re.search(r"(?m)^diff --git\s+a/[^\s]+\s+b/[^\s]+", block))

def format_repair_format_issues(text: str) -> list[SemanticRepairFormatIssue]:
    issues: list[SemanticRepairFormatIssue] = []
    recoverable_fenced_diff = recoverable_fenced_unified_diff(text)
    recoverable_search_replace = any(
        VALID_SEARCH_REPLACE_PATTERN.match(candidate.strip())
        for candidate in artifact_candidate_texts(text)
    )
    if extract_missing_context_requests(text):
        issues.append(
            SemanticRepairFormatIssue(
                code="format_repair_missing_context",
                message="format repair requested missing context before producing an artifact",
            )
        )
    if MALFORMED_SEARCH_REPLACE_WITHOUT_PATH_PATTERN.search(text) and not recoverable_search_replace:
        issues.append(
            SemanticRepairFormatIssue(
                code="format_repair_missing_path",
                message="BEGIN_SEARCH_REPLACE is missing the required ': path/to/file' line",
            )
        )
    issues.extend(orphan_search_replace_marker_issues(text, "format_repair"))
    issues.extend(malformed_search_replace_header_issues(text, "format_repair"))
    artifact_marker_offset = first_artifact_marker_offset(text)
    if not recoverable_fenced_diff and artifact_marker_offset > 0 and text[:artifact_marker_offset].strip():
        issues.append(
            SemanticRepairFormatIssue(
                code="format_repair_prose_mixed",
                message="format repair output contains prose before the artifact marker",
            )
        )
    loose_function_replacements = loose_python_function_replacement_artifacts(
        text,
        ArtifactPathPolicy(allowed_paths=(), allow_extra_new_files=True),
    )
    fenced_search_replace_artifacts = fenced_conflict_search_replace_artifacts(
        text,
        ArtifactPathPolicy(allowed_paths=(), allow_extra_new_files=True),
    )
    fenced_file_artifacts = fenced_path_file_artifacts(text, ArtifactPathPolicy(allowed_paths=(), allow_extra_new_files=True))
    recoverable_legacy_file_artifacts = False
    if "```" in text and legacy_file_begin_count(text):
        try:
            recoverable_legacy_file_artifacts = bool(
                extract_file_artifacts(text, ArtifactPathPolicy(allowed_paths=(), allow_extra_new_files=True))
            )
        except RunnerError:
            recoverable_legacy_file_artifacts = False
    if (
        "```" in text
        and not recoverable_fenced_diff
        and not recoverable_legacy_file_artifacts
        and not recoverable_search_replace
        and not fenced_file_artifacts
        and not loose_function_replacements
        and not fenced_search_replace_artifacts
    ):
        issues.append(
            SemanticRepairFormatIssue(
                code="format_repair_markdown_fence",
                message="format repair output must not wrap artifacts in Markdown fences",
            )
        )
    begin_file_count = legacy_file_begin_count(text)
    end_file_count = legacy_file_end_count(text)
    recoverable_unclosed_files = False
    if begin_file_count and begin_file_count != end_file_count:
        try:
            recoverable_unclosed_files = bool(
                unclosed_file_artifacts(text, ArtifactPathPolicy(allowed_paths=(), allow_extra_new_files=True))
            )
        except RunnerError:
            recoverable_unclosed_files = False
    if begin_file_count != end_file_count and not recoverable_unclosed_files:
        issues.append(
            SemanticRepairFormatIssue(
                code="format_repair_unbalanced_file_artifact",
                message=f"file artifact markers are unbalanced: begin={begin_file_count}, end={end_file_count}",
            )
        )
    if not output_has_valid_artifact_candidate(text):
        issues.append(
            SemanticRepairFormatIssue(
                code="format_repair_no_artifact",
                message="format repair output did not contain a valid JSON, BEGIN_FILE, BEGIN_SEARCH_REPLACE, or unified diff artifact",
            )
        )
    return issues

def lint_artifact_output(
    text: str,
    test_commands: Sequence[str],
    semantic_contracts: Sequence[SemanticContract] = (),
    semantic_repair_mode: bool = False,
    format_repair_mode: bool = False,
    forbidden_actions: Sequence[str] = (),
    project: Path | None = None,
    authorized_test_edit_paths: Sequence[str] = (),
) -> list[ArtifactLintFinding]:
    findings: list[ArtifactLintFinding] = []
    edited_blocks: list[tuple[str | None, str]] = []
    authorized_test_edits = set(normalize_project_relative_paths(authorized_test_edit_paths))
    max_json_search_replace_duplicate, json_search_replace_count = repeated_json_search_replace_score(text)
    if json_search_replace_count > 12:
        findings.append(
            ArtifactLintFinding(
                severity="error",
                code="repeated_json_search_replace",
                message=f"too many repeated JSON search_replace artifacts: {json_search_replace_count}",
            )
        )
    if max_json_search_replace_duplicate >= 4:
        findings.append(
            ArtifactLintFinding(
                severity="error",
                code="duplicated_json_search_replace",
                message=f"same JSON search_replace artifact repeated {max_json_search_replace_duplicate} times",
            )
        )
    for candidate in _json_candidates(text):
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        raw_items: Any
        if isinstance(payload, dict) and "artifacts" in payload:
            raw_items = payload["artifacts"]
        else:
            raw_items = payload
        items = [raw_items] if isinstance(raw_items, dict) else raw_items if isinstance(raw_items, list) else []
        for item in items:
            if not isinstance(item, dict) or str(item.get("type", "")).strip() != "search_replace":
                continue
            path = str(item.get("path", "")).strip() or None
            search = item.get("search")
            replace = item.get("replace")
            edit_text_parts = [value for value in (search, replace) if isinstance(value, str)]
            if edit_text_parts:
                edited_blocks.append((path, "\n".join(edit_text_parts)))
            if isinstance(search, str) and isinstance(replace, str) and search == replace:
                findings.append(
                    ArtifactLintFinding(
                        severity="error",
                        code="identical_search_replace",
                        message="JSON search_replace artifact has identical search and replacement text",
                        path=path,
                    )
                )
            for block in [value for value in (search, replace) if isinstance(value, str)]:
                mismatch = artifact_path_content_mismatch(path, block)
                if mismatch:
                    findings.append(
                        ArtifactLintFinding(
                            severity="error",
                            code="artifact_path_content_mismatch",
                            message=mismatch,
                            path=path,
                        )
                    )
                    break
    generated_blocks: list[tuple[str | None, str]] = json_generated_blocks(text)
    for issue in orphan_search_replace_marker_issues(text, "artifact"):
        findings.append(
            ArtifactLintFinding(
                severity="error",
                code=issue.code,
                message=issue.message,
                path=issue.path,
            )
        )
    for candidate in artifact_candidate_texts(text):
        for match in re.finditer(
            r"^BEGIN_(?:APPEND_)?FILE:\s*(?P<path>[^\n]+)\n(?P<content>.*?)\nEND_(?:APPEND_)?FILE(?:\s*:\s*[^\n]+)?\s*$",
            candidate,
            flags=re.DOTALL | re.MULTILINE,
        ):
            generated_blocks.append(
                (normalize_legacy_file_artifact_path(match.group("path")), strip_markdown_fence(match.group("content")))
            )
        for match in re.finditer(
            r"^BEGIN_(?:APPEND_)?FILE\s*\n(?P<path>[^\n]+)\n(?P<content>.*?)\nEND_(?:APPEND_)?FILE(?:\s*:\s*[^\n]+)?\s*$",
            candidate,
            flags=re.DOTALL | re.MULTILINE,
        ):
            generated_blocks.append(
                (normalize_legacy_file_artifact_path(match.group("path")), strip_markdown_fence(match.group("content")))
            )
        for match in re.finditer(
            r"^\s*BEGIN_SEARCH_REPLACE:\s*(?P<path>[^\n]+)\n\s*<<<<<<< SEARCH\n(?P<search>.*?)\n\s*=======\n(?P<replace>.*?)\n\s*>>>>>>> REPLACE",
            candidate,
            flags=re.DOTALL | re.MULTILINE,
        ):
            generated_blocks.append((match.group("path").strip(), clean_artifact_block(match.group("replace"))))
    for artifact in fenced_path_file_artifacts(text, ArtifactPathPolicy(allowed_paths=(), allow_extra_new_files=True)):
        generated_blocks.append((artifact.path, artifact.content))
    for artifact in loose_python_function_replacement_artifacts(text, ArtifactPathPolicy(allowed_paths=(), allow_extra_new_files=True)):
        generated_blocks.append((artifact.path, artifact.replace))
    for path, block in generated_blocks:
        edited_blocks.append((path, block))
        mismatch = artifact_path_content_mismatch(path, block)
        if mismatch:
            findings.append(
                ArtifactLintFinding(
                    severity="error",
                    code="artifact_path_content_mismatch",
                    message=mismatch,
                    path=path,
                )
            )
    if project is not None:
        block_assignments_by_path: dict[str, set[str]] = {}
        block_defined_methods_by_path: dict[str, set[str]] = {}
        for path, block in generated_blocks:
            if not path:
                continue
            normalized_path = normalize_legacy_file_artifact_path(path)
            block_assignments_by_path.setdefault(normalized_path, set()).update(
                re.findall(r"\bself\.([A-Za-z_][A-Za-z0-9_]*)\s*=", block)
            )
            block_defined_methods_by_path.setdefault(normalized_path, set()).update(
                re.findall(r"(?m)^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", block)
            )
        for path, block in generated_blocks:
            if not path or not path.endswith(".py"):
                continue
            normalized_path = normalize_legacy_file_artifact_path(path)
            target = resolve_project_path(project, normalized_path)
            if not target.exists() or not target.is_file():
                continue
            source = target.read_text(encoding="utf-8", errors="replace")
            known_attrs = set(re.findall(r"\bself\.([A-Za-z_][A-Za-z0-9_]*)\b", source))
            known_methods = set(re.findall(r"(?m)^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", source))
            assigned_attrs = block_assignments_by_path.get(normalized_path, set())
            defined_methods = block_defined_methods_by_path.get(normalized_path, set())
            for attr in sorted(set(re.findall(r"\bself\.([A-Za-z_][A-Za-z0-9_]*)\b", block))):
                if attr.startswith("__") and attr.endswith("__"):
                    continue
                if attr in known_attrs or attr in known_methods or attr in assigned_attrs or attr in defined_methods:
                    continue
                findings.append(
                    ArtifactLintFinding(
                        severity="error",
                        code="unknown_self_attribute_reference",
                        message=(
                            f"artifact introduces `self.{attr}` but `{normalized_path}` does not define that "
                            "attribute/method and the artifact does not assign it before use"
                        ),
                        path=normalized_path,
                    )
                )
    absent_api_contracts = absent_api_contracts_from_texts(forbidden_actions)
    forbidden_edit_symbols = forbidden_edit_symbols_from_texts(forbidden_actions)
    if forbidden_edit_symbols and edited_blocks:
        for path, block in edited_blocks:
            for symbol in forbidden_edit_symbols:
                if re.search(rf"(?m)(?:\bdef\s+{re.escape(symbol)}\s*\(|\b{re.escape(symbol)}\b)", block):
                    findings.append(
                        ArtifactLintFinding(
                            severity="error",
                            code="forbidden_repair_target_edit",
                            message=(
                                f"artifact edits `{symbol}`, but current supervisor repair advice marks "
                                "that target as forbidden for this failure family"
                            ),
                            path=path,
                        )
                    )
    if absent_api_contracts and generated_blocks:
        for path, block in generated_blocks:
            for class_name, attr in absent_api_contracts:
                if re.search(rf"(?m)^\s*def\s+{re.escape(attr)}\s*\(", block):
                    findings.append(
                        ArtifactLintFinding(
                            severity="error",
                            code="forbidden_absent_api_addition",
                            message=(
                                f"artifact adds `{class_name}.{attr}` even though supervisor evidence "
                                "classified that API as absent/forbidden; patch the call site or current-stage adapter instead"
                            ),
                            path=path,
                        )
                    )
                if re.search(rf"\.{re.escape(attr)}\s*\(", block):
                    findings.append(
                        ArtifactLintFinding(
                            severity="error",
                            code="forbidden_absent_api_call",
                            message=(
                                f"artifact still calls absent API `{class_name}.{attr}`; use the existing probed API "
                                "or request missing context instead"
                            ),
                            path=path,
                        )
                    )
    if generated_blocks and test_commands_use_unittest(test_commands):
        pytest_patterns = {
            "pytest_import": r"(?m)^\s*import pytest\s*$",
            "pytest_raises": r"pytest\.raises\s*\(",
            "pytest_fixture": r"\btmp_path\b",
        }
        for code, pattern in pytest_patterns.items():
            bad_match = next(((path, block) for path, block in generated_blocks if re.search(pattern, block)), None)
            if bad_match is not None:
                bad_path, _block = bad_match
                findings.append(
                    ArtifactLintFinding(
                        severity="error",
                        code=code,
                        message=(
                            "unittest command is configured, but the generated artifact appears to depend "
                            "on pytest or pytest fixtures"
                        ),
                        path=bad_path or "tests",
                    )
                )

    for candidate in artifact_candidate_texts(text):
        for match in re.finditer(
            r"^\s*BEGIN_SEARCH_REPLACE:\s*(?P<path>[^\n]+)\n\s*<<<<<<< SEARCH\n(?P<search>.*?)\n\s*=======\n(?P<replace>.*?)\n\s*>>>>>>> REPLACE",
            candidate,
            flags=re.DOTALL | re.MULTILINE,
        ):
            search = clean_artifact_block(match.group("search"))
            replace = clean_artifact_block(match.group("replace"))
            path = match.group("path").strip()
            if search == replace:
                findings.append(
                    ArtifactLintFinding(
                        severity="error",
                        code="identical_search_replace",
                        message="search_replace artifact has identical search and replacement text",
                        path=path,
                    )
                )
            if contains_conflict_markers(search) or contains_conflict_markers(replace):
                findings.append(
                    ArtifactLintFinding(
                        severity="error",
                        code="search_replace_conflict_markers",
                        message=(
                            "search_replace content contains nested conflict/artifact markers; "
                            "emit one atomic edit, a whole-file artifact, or a valid unified diff instead"
                        ),
                        path=path,
                    )
                )
            if len(search.encode("utf-8")) > 4000:
                findings.append(
                    ArtifactLintFinding(
                        severity="warn",
                        code="large_search_replace",
                        message="large search_replace block is fragile; prefer whole-file artifact for generated files",
                        path=path,
                    )
                )

    begin_file_count = legacy_file_begin_count(text)
    end_file_count = legacy_file_end_count(text)
    recoverable_unclosed_files = False
    if begin_file_count and begin_file_count != end_file_count:
        try:
            recovered_files = unclosed_file_artifacts(text, ArtifactPathPolicy(allowed_paths=(), allow_extra_new_files=True))
            recoverable_unclosed_files = bool(recovered_files)
            for artifact in recovered_files:
                generated_blocks.append((artifact.path, artifact.content))
        except RunnerError:
            recoverable_unclosed_files = False
    if begin_file_count != end_file_count and not recoverable_unclosed_files:
        findings.append(
            ArtifactLintFinding(
                severity="error",
                code="unbalanced_file_artifact",
                message=f"file artifact markers are unbalanced: begin={begin_file_count}, end={end_file_count}",
            )
        )
    if format_repair_mode:
        for issue in format_repair_format_issues(text):
            findings.append(
                ArtifactLintFinding(
                    severity="error",
                    code=issue.code,
                    message=issue.message,
                    path=issue.path,
                )
            )
    if semantic_repair_mode:
        for issue in semantic_repair_format_issues(text):
            findings.append(
                ArtifactLintFinding(
                    severity="error",
                    code=issue.code,
                    message=issue.message,
                    path=issue.path,
                )
            )
        search_replace_matches = [
            match
            for candidate in artifact_candidate_texts(text)
            for match in re.finditer(
                r"^\s*BEGIN_SEARCH_REPLACE:\s*(?P<path>[^\n]+)\n\s*<<<<<<< SEARCH\n(?P<search>.*?)\n\s*=======\n(?P<replace>.*?)\n\s*>>>>>>> REPLACE",
                candidate,
                flags=re.DOTALL | re.MULTILINE,
            )
        ]
        diff_file_count = len(re.findall(r"(?m)^diff --git\s+a/[^\s]+\s+b/[^\s]+", text))
        diff_file_count = diff_file_count or len(re.findall(r"(?m)^\+\+\+\s+b/[^\n]+", text))
        json_artifact_count = len(json_generated_blocks(text))
        forbidden_file_count = legacy_file_begin_count(text)
        if json_artifact_count or forbidden_file_count:
            findings.append(
                ArtifactLintFinding(
                    severity="error",
                    code="semantic_repair_forbidden_artifact",
                    message="semantic repair must use one short BEGIN_SEARCH_REPLACE or one minimal unified diff, not JSON or whole-file artifacts",
                )
            )
        artifact_kinds = int(bool(search_replace_matches)) + int(bool(diff_file_count))
        artifact_units = len(search_replace_matches) + diff_file_count
        if artifact_kinds != 1 or artifact_units != 1:
            findings.append(
                ArtifactLintFinding(
                    severity="error",
                    code="semantic_repair_not_atomic",
                    message=(
                        "semantic repair must emit exactly one atomic artifact: "
                        "one BEGIN_SEARCH_REPLACE block or one unified-diff file patch"
                    ),
                )
            )
        for match in search_replace_matches:
            path = match.group("path").strip()
            search = clean_artifact_block(match.group("search"))
            replace = clean_artifact_block(match.group("replace"))
            if path.startswith("tests/") and path not in authorized_test_edits:
                findings.append(
                    ArtifactLintFinding(
                        severity="error",
                        code="semantic_repair_test_edit",
                        message="semantic repair must satisfy fixed contracts by editing product code, not tests",
                        path=path,
                    )
                )
            if len(search.encode("utf-8")) > 2000 or len(replace.encode("utf-8")) > 4000:
                findings.append(
                    ArtifactLintFinding(
                        severity="error",
                        code="semantic_repair_too_large",
                        message="semantic repair search/replace is too large; reduce to the shortest unique contract-focused edit",
                        path=path,
                    )
                )
    findings.extend(lint_semantic_contracts(generated_blocks, semantic_contracts))
    return findings

def artifact_lint_document(findings: Sequence[ArtifactLintFinding]) -> str:
    lines = ["## Artifact Lint Result", ""]
    if not findings:
        lines.append("PASS: no blocking artifact issues detected.")
        return "\n".join(lines)
    for finding in findings:
        path = f" path={finding.path}" if finding.path else ""
        lines.append(f"- {finding.severity.upper()} `{finding.code}`{path}: {finding.message}")
    return "\n".join(lines)

def artifact_lint_failure_type(findings: Sequence[ArtifactLintFinding], default: str = "artifact_lint_failed") -> str:
    priority = [
        "semantic_repair_missing_context",
        "semantic_repair_missing_path",
        "semantic_repair_prose_mixed",
        "semantic_repair_markdown_fence",
        "semantic_repair_malformed_search_replace",
        "semantic_repair_multiple_artifacts",
        "semantic_repair_not_atomic",
        "semantic_repair_forbidden_artifact",
        "semantic_repair_test_edit",
        "semantic_repair_too_large",
        "artifact_path_content_mismatch",
        "forbidden_absent_api_addition",
        "forbidden_absent_api_call",
        "format_repair_missing_context",
        "format_repair_missing_path",
        "format_repair_prose_mixed",
        "format_repair_markdown_fence",
        "format_repair_malformed_search_replace",
        "artifact_orphan_search_replace",
        "format_repair_unbalanced_file_artifact",
        "format_repair_no_artifact",
        "stage_scope_violation",
        "test_edit_attempt",
        "identical_search_replace",
        "search_replace_conflict_markers",
        "unbalanced_file_artifact",
    ]
    codes = [finding.code for finding in findings if finding.severity == "error"]
    for code in priority:
        if code in codes:
            if code == "identical_search_replace":
                return "artifact_invalid"
            if code == "search_replace_conflict_markers":
                return "artifact_invalid"
            if code == "unbalanced_file_artifact":
                return "artifact_invalid"
            return code
    return default

def native_struct_format_lines(project: Path | None, paths: Sequence[str]) -> list[str]:
    """Return source snippets that use native struct layouts in binary tests.

    Python's default struct mode (`@`) can add native alignment padding. For
    fixed on-disk/network formats, generated tests should use an explicit
    prefix (`>`, `<`, `!`, or `=`) so the test proposition is platform-stable.
    """
    if project is None:
        return []
    findings: list[str] = []
    pattern = re.compile(r"struct\.(?:pack|unpack_from|unpack)\(\s*[rubf]?([\"'])(?P<fmt>[^\"']+)\1")
    for raw_path in paths:
        path = project / raw_path
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line_no, line in enumerate(lines, start=1):
            match = pattern.search(line)
            if not match:
                continue
            fmt = match.group("fmt")
            if not fmt or fmt[0] in "@=<>!":
                continue
            if any(code in fmt for code in "bBhHiIlLqQnNfdspP"):
                findings.append(f"{raw_path}:{line_no}: {line.strip()}")
    return findings

def stage_test_paths_in_command_docs(
    command_docs: Sequence[tuple[str, str]],
    stage_test_paths: Sequence[str],
) -> list[str]:
    """Return stage-owned test paths referenced by command failure evidence."""
    normalized = normalize_project_relative_paths(stage_test_paths)
    if not normalized:
        return []
    combined = "\n".join(document for _name, document in command_docs)
    return unique_ordered(path for path in normalized if path in combined)


def repair_advice_from_command_docs(
    command_docs: Sequence[tuple[str, str]],
    test_commands: Sequence[str],
    project: Path | None = None,
    generated_test_paths: Sequence[str] = (),
) -> RepairAdvice | None:
    combined = "\n".join(document for _name, document in command_docs)
    lowered = combined.lower()
    focus_files: list[str] = []
    instructions: list[str] = []
    evidence: list[str] = []
    strategy = "small_patch"
    state_probe_points_below_cli = False
    direct_pager_flush_clears_metadata = False
    row_persistence_loss = False

    for raw_path in re.findall(r'File "([^"]+)", line \d+', combined):
        if "/tests/" in raw_path:
            focus_files.append("tests/" + raw_path.split("/tests/", 1)[1])
        elif "/minisqlite/" in raw_path:
            focus_files.append("minisqlite/" + raw_path.split("/minisqlite/", 1)[1])
        elif "/benchmarks/" in raw_path:
            focus_files.append(Path(raw_path).name)

    test_focus_files = unique_ordered(path for path in focus_files if path.startswith("tests/"))
    inferred_stage_focus = unique_ordered(
        product_path
        for test_path in test_focus_files
        for product_path in inferred_product_focus_from_test_path(test_path)
    )
    product_trace_focus = unique_ordered(path for path in focus_files if not path.startswith("tests/"))
    generated_test_focus = unique_ordered(
        path for path in [*generated_test_paths, *test_focus_files] if path.startswith("tests/")
    )
    generated_test_set = set(generated_test_focus)
    if (
        "invalid page type" in lowered
        and "test_pager.py" in combined
        and project_tests_assert_pager_raw_page_contract(
            project,
            unique_ordered([*generated_test_focus, *test_focus_files, "tests/test_pager.py"]),
        )
    ):
        strategy = "semantic_contract_patch"
        focus_files.extend(["minisqlite/storage/pager.py", "tests/test_pager.py"])
        instructions.extend(
            [
                "Tests assert a raw page IO contract: write_page(page_id, data) followed by read_page(page_id) must return the exact PAGE_SIZE bytes.",
                "Patch minisqlite/storage/pager.py so Pager validates file/header boundaries and byte lengths, but does not enforce B+Tree page_type invariants in read_page() or write_page().",
                "Keep B+Tree page structure validation in storage/btree.py or higher layers; do not move that invariant into the pager raw IO layer.",
                "Preserve allocate_page() as a zero-filled PAGE_SIZE allocation readable via read_page().",
            ]
        )
        evidence.append("tests/test_pager.py asserts raw page round-trip and zero-allocation behavior")
    acceptance_blockers: list[dict[str, object]] = []
    for _name, document in command_docs:
        parsed = parse_command_result_document(document)
        if parsed.get("command") != "acceptance-evidence-gate":
            continue
        try:
            payload = json.loads(parsed.get("stdout", ""))
        except json.JSONDecodeError:
            continue
        raw_blockers = payload.get("blockers")
        if isinstance(raw_blockers, list):
            acceptance_blockers.extend(item for item in raw_blockers if isinstance(item, dict))
    if acceptance_blockers:
        strategy = "acceptance_gap_patch"
        html_targets = re.findall(r"\b([A-Za-z0-9_./-]+\.html)\b", combined)
        focus_files.extend(html_targets)
        blocker_lines: list[str] = []
        blocker_covers: list[str] = []
        for blocker in acceptance_blockers[:8]:
            blocker_id = str(blocker.get("id") or "acceptance")
            blocker_status = str(blocker.get("status") or "unverified")
            blocker_text = str(blocker.get("text") or "")
            blocker_lines.append(f"{blocker_id} {blocker_status}: {blocker_text}")
            raw_covers = blocker.get("required_covers")
            if isinstance(raw_covers, list):
                blocker_covers.extend(str(item) for item in raw_covers if isinstance(item, str))
        blocker_covers = unique_ordered(blocker_covers)
        if any(
            cover in blocker_covers
            for cover in (
                "html_visible",
                "required_window_functions",
                "board_200_cells",
                "start_button",
                "active_piece_visible",
                "keyboard_interaction",
                "score_update",
                "line_clear",
                "game_over",
                "restart_after_game_over",
            )
        ):
            focus_files.append("tetris.html")
        instructions.append(
            "Acceptance evidence gate failed; do not claim completion until each blocker has passing executable evidence."
        )
        if blocker_lines:
            instructions.append("Resolve these acceptance propositions: " + " | ".join(blocker_lines))
        if "active_piece_visible" in blocker_covers:
            instructions.append("Ensure the current active game piece is rendered visibly immediately after game start.")
        if "keyboard_interaction" in blocker_covers:
            instructions.append("Ensure keyboard input causes an observable state/render change covered by browser smoke or an explicit command.")
        if "score_update" in blocker_covers:
            instructions.append("Ensure score, level, and line counters update from actual gameplay events, not static placeholders.")
        if "line_clear" in blocker_covers:
            instructions.append("Ensure filled rows are removed and board state is compacted by the line-clear implementation.")
        if "restart_after_game_over" in blocker_covers:
            instructions.append("Ensure Start after game over resets state and starts a fresh playable session.")
        if "required_window_functions" in blocker_covers:
            instructions.append("Expose the SPEC-required public functions on window and keep them callable after initialization.")
        evidence.append("acceptance gate blockers: " + " | ".join(blocker_lines[:4]))
    if "mechanical probe: python api surface" in lowered:
        probe_source_files = unique_ordered(
            re.findall(r"(?m)^\s*-\s+(?!status:|rule:|trigger:|source_files:|class_facts:|public_methods:|invariant:)([A-Za-z0-9_./-]+\.py)\b", combined)
        )
        probed_absent = unique_ordered(re.findall(r"`([A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*)`\s+is absent", combined))
        strategy = "root_cause_patch"
        focus_files.extend([*product_trace_focus, *probe_source_files, *inferred_stage_focus])
        instructions.extend(
            [
                "Mechanical Probe: Python API surface is authoritative for existing class constructors and public methods.",
                "Do not call methods listed as absent by the probe; reject any hypothesis that depends on an absent API.",
                "If the probe lists public_attrs, prefer those visible attributes over inventing private attributes or compatibility methods.",
                "For constructor TypeError, patch the call site to match the probed constructor signature unless the current stage explicitly owns that class API.",
                "For AttributeError, prefer an existing public method or a smaller current-stage adapter at the call site; add a new product method only when the SPEC/current stage explicitly requires that API.",
                "If a root-cause or patch plan contradicts the mechanical API probe, follow the probe and choose a different patch target.",
            ]
        )
        if probed_absent:
            evidence.append("absent API from mechanical probe: " + ", ".join(probed_absent[:8]))
        if probe_source_files:
            evidence.append("API surface probed from: " + ", ".join(probe_source_files[:8]))
    if "mechanical probe: precondition validation" in lowered:
        precondition_facts = unique_ordered(
            re.findall(r"(?m)^\s*-\s+(tests/[^\n]+)$", combined)
        )
        oracle_conflict_facts = [fact for fact in precondition_facts if "TEST_ORACLE_CONFLICT" in fact]
        if oracle_conflict_facts:
            strategy = "replace_test_harness"
            focus_files.extend([*test_focus_files, *generated_test_focus, *inferred_stage_focus])
            instructions.extend(
                [
                    "Mechanical Probe found a generated test-oracle conflict: the expected exception predicate is contradicted by literal test inputs.",
                    "Do not patch product code to reject inputs that mechanically satisfy the declared type/schema predicate.",
                    "Rewrite the generated test harness so the failing assertRaises block contains an actually invalid input or asserts success for valid input.",
                    "Keep external acceptance criteria intact; only repair stage-owned generated tests after project-policy triage authorizes it.",
                ]
            )
            evidence.append("test oracle conflict facts: " + " | ".join(oracle_conflict_facts[:4]))
        else:
            strategy = "root_cause_patch"
            focus_files.extend([*product_trace_focus, *inferred_stage_focus])
            instructions.extend(
                [
                    "Mechanical Probe: Precondition validation is authoritative for expected exception failures.",
                    "If an operation is expected to raise for invalid input, validate that input before row iteration, callbacks, or data-dependent filters can skip the check.",
                    "For SQL-like WHERE predicates, validate referenced columns against schema before scanning rows; an empty table must still reject an invalid column.",
                    "Reject root-cause hypotheses claiming Python list comprehensions swallow exceptions; Python comprehensions propagate exceptions.",
                    "Patch the product operation that owns precondition validation, not tests and not downstream data storage.",
                ]
            )
            if precondition_facts:
                evidence.append("precondition facts: " + " | ".join(precondition_facts[:4]))
    if "mechanical probe: cli command contracts" in lowered:
        strategy = "root_cause_patch"
        cli_source_files = unique_ordered(
            re.findall(r"(?m)^\s*-\s+([A-Za-z0-9_./-]+\.py)\s*$", combined)
        )
        focus_files.extend([*product_trace_focus, *cli_source_files, *inferred_stage_focus])
        instructions.extend(
            [
                "Mechanical Probe: CLI command contracts are authoritative for dispatch string normalization.",
                "If the caller strips a command prefix before dispatch, patch the handler comparisons to use stripped command names or pass the prefix through consistently.",
                "If explicit empty command input and no command argument are distinct, branch on argument count as well as command string truthiness.",
                "If the probe states that repeated CLI calls share the same database path, preserve committed schema/data across those calls before changing assertions.",
                "If dot commands inspect metadata, make them read the same schema state as normal SQL execution for the same connection/database path.",
                "Do not edit tests for CLI dispatch facts unless project-policy triage explicitly authorizes generated test-harness repair.",
            ]
        )
        cli_facts = unique_ordered(
            re.findall(r"(?m)^\s*-\s+([^\n]*(?:strips the leading dot|explicit empty SQL|observed unrecognized dot commands|same database path|dot commands)[^\n]*)$", combined)
        )
        if cli_facts:
            evidence.append("CLI probe facts: " + " | ".join(cli_facts[:4]))
    if "mechanical probe: cli state persistence" in lowered:
        strategy = "root_cause_patch"
        state_probe_points_below_cli = bool(
            re.search(r"after_reopen_schema_payload_len:\s*(?:0|\[\]|None)", combined)
            or re.search(r"after_reopen_tables:\s*\[\]", combined)
        )
        direct_pager_flush_clears_metadata = bool(
            re.search(r"direct_after_write_schema_payload_len:\s*[1-9]\d*", combined)
            and (
                re.search(r"direct_after_flush_schema_payload_len:\s*(?:0|\[\]|None)", combined)
                or re.search(r"direct_after_reopen_schema_payload_len:\s*(?:0|\[\]|None)", combined)
            )
        )
        row_persistence_loss = bool(
            re.search(r"before_close_rows:\s*\[\[", combined)
            and re.search(r"after_reopen_rows:\s*\[\]", combined)
        )
        state_focus_candidates = (
            ("minisqlite/storage/pager.py", "minisqlite/connection.py")
            if direct_pager_flush_clears_metadata
            else ("minisqlite/storage/btree.py", "minisqlite/storage/pager.py", "minisqlite/connection.py", "minisqlite/engine/executor.py")
            if row_persistence_loss
            else ("minisqlite/connection.py", "minisqlite/storage/pager.py")
            if state_probe_points_below_cli
            else ("minisqlite/connection.py", "minisqlite/storage/pager.py", "minisqlite/cli.py")
        )
        focus_files.extend(path for path in state_focus_candidates if project is None or (project / path).exists())
        state_instructions = [
            "Mechanical Probe: CLI state persistence is authoritative for close/reopen state boundaries.",
            "If direct API state exists before close but is absent after reopen, patch the persistence boundary before changing CLI dispatch.",
            "If schema payload is present before close and absent after reopen, patch the schema metadata write/flush/read path.",
        ]
        if direct_pager_flush_clears_metadata:
            state_instructions.extend(
                [
                    "If direct Pager metadata exists after write but disappears after flush or close/reopen, patch Pager.flush/_write_header/page-0 preservation before changing Connection.",
                    "Required predicate for direct Pager loss: after write_schema_metadata(payload), flush() must not zero or replace bytes page0[_HEADER_SIZE:].",
                    "Required edit shape for direct Pager loss: change the header rewrite path (_write_header or flush) so it preserves existing page-0 metadata bytes while updating only the fixed header.",
                    "Forbidden non-fixes for direct Pager loss: adding helper predicates such as has_schema_metadata, changing only write_schema_metadata, adding blank lines, changing CLI, changing README, or changing tests.",
                ]
            )
        if row_persistence_loss:
            state_instructions.extend(
                [
                    "If rows exist before close but disappear after reopen while schema survives, patch row/page persistence in btree/pager/connection/executor before changing CLI output.",
                    "For row persistence loss with surviving schema metadata, do not edit _write_header, write_schema_metadata, read_schema_metadata, CLI output, README, or tests.",
                    "Required predicate for row persistence loss: after INSERT and close/reopen, SELECT must return the inserted row, not only the header columns.",
                    "Forbidden non-fixes for row persistence loss: changing print formatting, changing tests, changing README, or adding CLI buffering without making after_reopen_rows non-empty.",
                ]
            )
        else:
            state_instructions.append("If schema payload survives but tables are absent after reopen, patch schema deserialization/loading.")
        state_instructions.extend(
            [
                "When the state probe points below CLI, keep CLI files as read-only evidence until the direct API close/reopen probe passes.",
                "Do not edit tests for state-persistence failures unless project-policy triage explicitly authorizes generated test-harness repair.",
            ]
        )
        instructions.extend(state_instructions)
        state_facts = unique_ordered(
            re.findall(r"(?m)^\s*-\s+((?:before_close|after_reopen)_[^:]+:\s*[^\n]+)$", combined)
        )
        if state_facts:
            evidence.append("CLI state probe facts: " + " | ".join(state_facts[:8]))
        if direct_pager_flush_clears_metadata:
            evidence.append("direct Pager probe: metadata write succeeds before flush but is lost by flush/close")
        if row_persistence_loss:
            evidence.append("row persistence probe: rows exist before close but disappear after reopen")
    for missing_symbol, module_name in re.findall(
        r"ImportError:\s*cannot import name '([^']+)' from '([^']+)'",
        combined,
    ):
        module_path = module_name_to_project_path(module_name)
        existing_symbols = python_defined_symbols(project, module_path)
        importing_paths = unique_ordered([*generated_test_focus, *inferred_stage_focus, *product_trace_focus])
        projections = import_api_alias_projections(
            project,
            importing_paths,
            module_name,
            existing_symbols,
        )
        aliases = likely_symbol_aliases(missing_symbol, existing_symbols)
        same_stage_import_contract = module_path in set([*inferred_stage_focus, *product_trace_focus])
        if same_stage_import_contract:
            strategy = "root_cause_patch"
            focus_files.extend([module_path, *inferred_stage_focus, *product_trace_focus])
            instructions.extend(
                [
                    (
                        f"Treat missing import `{missing_symbol}` from same-stage product module "
                        f"`{module_name}` as a product public-API regression, not as a test-harness mismatch."
                    ),
                    (
                        f"Restore or preserve the public symbol `{missing_symbol}` in `{module_path}` "
                        "with the smallest product-code patch that keeps existing behavior intact."
                    ),
                    (
                        "Do not edit the importing test for same-stage product imports; tests are executable "
                        "evidence that the product module lost or failed to expose its public contract."
                    ),
                ]
            )
            evidence.append(
                f"same-stage test imports missing product symbol `{missing_symbol}` from `{module_name}`"
            )
            break
        if not projections and aliases:
            projections = [("", missing_symbol, aliases[0])]
        generated_test_import_seen = any(
            test_path in generated_test_set
            and re.search(rf"File \"[^\"]*/{re.escape(test_path)}\"", combined)
            for test_path in test_focus_files
        ) or bool(generated_test_set and any(path in combined for path in generated_test_set))
        if not generated_test_import_seen or not module_path.startswith("minisqlite/"):
            continue
        strategy = "generated_test_import_api_mismatch"
        focus_files.extend([*generated_test_focus, *inferred_stage_focus, *product_trace_focus])
        alias_hint = ""
        if projections:
            alias_hint = (
                " Required import projections: "
                + ", ".join(f"`{existing} as {missing}`" for _path, missing, existing in projections[:8])
                + "."
            )
        instructions.extend(
            [
                (
                    f"Treat missing import `{missing_symbol}` from `{module_name}` as a current-stage "
                    "generated test-harness API mismatch, not an external product contract."
                ),
                (
                    f"Use `{module_path}` as read-only API context and update the generated test import/calls "
                    f"to existing symbols.{alias_hint}"
                ),
                (
                    "Apply all missing-name projections from the same import statement in one patch; "
                    "do not repair only the first missing symbol and leave sibling missing imports behind."
                ),
                (
                    "Do not add product compatibility aliases solely to satisfy a generated test's invented name; "
                    "first project the generated test onto the existing product API."
                ),
                (
                    "If a current-stage product file also imports the same invented names, patch that current-stage "
                    "file to use existing API names or remove the unused import; keep the imported existing module read-only."
                ),
                "If product behavior still fails after the test harness uses existing API names, patch only the current-stage product file(s).",
            ]
        )
        evidence.append(
            f"stage-generated test imported missing symbol `{missing_symbol}` from `{module_name}`"
        )
        break
    didyoumean_attribute_errors: set[tuple[str, str]] = set()
    for class_name, missing_attr, suggested_attr in re.findall(
        r"AttributeError:\s*'([^']+)'\s+object has no attribute '([^']+)'\.\s+Did you mean: '([^']+)'\?",
        combined,
    ):
        if not product_trace_focus and attribute_error_is_cross_stage_test_harness_mismatch(class_name, test_focus_files):
            continue
        didyoumean_attribute_errors.add((class_name, missing_attr))
        strategy = "attribute_didyoumean_patch"
        focus_files.extend([*product_trace_focus, *inferred_stage_focus])
        instructions.extend(
            [
                (
                    f"Patch the product-code AttributeError root cause: `{class_name}` calls or exposes "
                    f"`{missing_attr}` but the existing nearby API is `{suggested_attr}`."
                ),
                (
                    f"Prefer replacing the internal call site from `{missing_attr}` to `{suggested_attr}` "
                    "when the observed signatures are compatible; otherwise add the smallest wrapper method "
                    f"`{missing_attr}` that delegates to `{suggested_attr}`."
                ),
                "Do not edit generated or external tests for this case; executable evidence points to a missing product attribute.",
                "Do not emit an identical search_replace; the replacement must change the missing attribute call or add the missing wrapper.",
            ]
        )
        evidence.append(
            f"python AttributeError suggested `{suggested_attr}` for missing `{class_name}.{missing_attr}`"
        )
        break

    for class_name, attr in re.findall(r"(?:AttributeError:\s*)?'([^']+)'\s+object has no attribute '([^']+)'", combined):
        if (class_name, attr) in didyoumean_attribute_errors:
            continue
        if class_name == "Token":
            continue
        owner_focus = class_owner_paths_from_project(project, class_name)
        product_attr_refs = product_paths_referencing_attribute(project, attr)
        if product_trace_focus or product_attr_refs:
            strategy = "root_cause_patch"
            focus_files.extend([*product_trace_focus, *product_attr_refs, *owner_focus, *inferred_stage_focus])
            instructions.extend(
                [
                    (
                        f"Treat missing `{class_name}.{attr}` as a product API/call-site inconsistency, "
                        "not as a generated test-harness mismatch, because product code references it."
                    ),
                    (
                        "Patch the smallest product-code boundary: either restore the missing method on the owner class "
                        "or change the product call site to an existing API with equivalent behavior."
                    ),
                    "Do not edit tests for a missing attribute that is also referenced by product code.",
                ]
            )
            evidence.append(
                f"product code references missing attribute `{class_name}.{attr}` in "
                + ", ".join(unique_ordered([*product_trace_focus, *product_attr_refs])[:6])
            )
            break
        if not attribute_error_is_cross_stage_test_harness_mismatch(class_name, test_focus_files):
            continue
        strategy = "test_harness_api_mismatch"
        focus_files.extend([*test_focus_files, *owner_focus, *inferred_stage_focus])
        instructions.extend(
            [
                (
                    f"Treat `{class_name}.{attr}` as a generated test-harness API mismatch, "
                    "not as a new product API contract."
                ),
                (
                    "Update the affected generated tests to use the existing API of "
                    + ", ".join(owner_focus[:3])
                    + "; do not add compatibility methods solely to satisfy the mistaken test setup."
                ),
                (
                    "Use product files as read-only API context first. After the test harness reaches "
                    "the intended product behavior, repair the stage product file(s) if executable evidence still fails."
                ),
            ]
        )
        evidence.append(
            f"test file(s) {', '.join(test_focus_files)} failed on cross-stage missing API {class_name}.{attr}"
        )
        break

    for class_name in re.findall(
        r"TypeError:\s*([A-Za-z_][A-Za-z0-9_]*)\.__init__\(\) missing \d+ required positional argument",
        combined,
    ):
        if not attribute_error_is_cross_stage_test_harness_mismatch(class_name, test_focus_files):
            continue
        owner_focus = class_owner_paths_from_project(None, class_name)
        strategy = "test_harness_api_mismatch"
        focus_files.extend([*test_focus_files, *owner_focus, *inferred_stage_focus])
        instructions.extend(
            [
                (
                    f"Treat `{class_name}.__init__` as an existing cross-stage API contract. "
                    "Do not change the constructor solely to satisfy the generated stage test."
                ),
                (
                    "Update the affected generated tests or stage-local adapter usage to call the existing API from "
                    + ", ".join(owner_focus[:3])
                    + "."
                ),
                (
                    "Use the owner product file as read-only contract context first; patch the current stage product files only after the test harness matches that contract."
                ),
            ]
        )
        evidence.append(
            f"test file(s) {', '.join(test_focus_files)} called existing cross-stage constructor `{class_name}.__init__` with the wrong shape"
        )
        break

    if test_commands_use_unittest(test_commands) and (
        "modulenotfounderror: no module named 'pytest'" in lowered
        or "import pytest" in lowered
        or "pytest.raises" in lowered
        or "tmp_path" in lowered
    ):
        strategy = "replace_test_harness"
        focus_files.append("tests/test_minisqlite.py")
        instructions.extend(
            [
                "Rewrite the affected test file as pure unittest when pytest dependency or fixtures are observed.",
                "Do not use import pytest, tmp_path fixtures, pytest.raises, or bare pytest-style test classes.",
                "Prefer BEGIN_FILE for a generated test-harness conversion instead of many brittle search_replace blocks.",
            ]
        )
        evidence.append("unittest runner observed pytest-specific test harness usage")

    if "start directory is not importable: 'tests'" in lowered or 'start directory is not importable: "tests"' in lowered:
        strategy = "create_test_harness"
        focus_files.extend(["tests/__init__.py", "tests/test_minisqlite.py"])
        instructions.extend(
            [
                "Create a unittest-compatible tests directory instead of patching unrelated implementation files.",
                "Add focused tests that encode the completed stage contracts and can run with `python3 -m unittest discover -s tests`.",
                "Keep tests dependency-free; do not use pytest fixtures or pytest-only assertions unless the configured command uses pytest.",
            ]
        )
        evidence.append("final unittest discovery failed because tests directory was missing or not importable")

    if "search text must occur exactly once" in lowered or "found 0" in lowered:
        strategy = "whole_file_or_shorter_search"
        instructions.extend(
            [
                "The previous search_replace did not match the current file exactly.",
                "Use a shorter exact snippet from the included file, or replace the whole generated file with BEGIN_FILE.",
            ]
        )
        evidence.append("search_replace failed exact-match validation")

    if "browser-tetris-smoke" in lowered and "missing function " in lowered:
        strategy = "browser_public_api_patch"
        html_targets = re.findall(r"html-smoke\s+([^\s`]+\.html)", combined, flags=re.IGNORECASE)
        focus_files.extend(html_targets or ["tetris.html"])
        missing_names = unique_ordered(re.findall(r"missing function\s+([A-Za-z_$][\w$]*)", combined))
        if missing_names:
            instructions.append(
                "Restore the browser-visible public API before the closing script/IIFE: "
                + ", ".join(f"window.{name} = {name}" for name in missing_names[:8])
            )
        instructions.extend(
            [
                "Do not rename required public functions; if internal names differ, add exact-name wrapper functions.",
                "Preserve the required DOM elements and keep gameOver() setting `.overlay-title` text to `GAME OVER`.",
                "Prefer a local export/wrapper patch over rewriting the entire HTML file when the current file is otherwise functional.",
            ]
        )
        evidence.append("browser smoke reported missing window-visible public functions")

    if "active piece is not visible after start" in lowered or "active piece did not visibly move after arrowleft" in lowered:
        strategy = "browser_behavior_patch"
        html_targets = re.findall(r"html-smoke\s+([^\s`]+\.html)", combined, flags=re.IGNORECASE)
        focus_files.extend(html_targets or ["tetris.html"])
        instructions.extend(
            [
                "Render the active falling piece into `#game-board .cell` on every board refresh, not only locked cells.",
                "After `startGame()`, the active piece must produce visible non-background cells before the first timer tick.",
                "After an ArrowLeft key event from the spawn position, the visible active-piece cell indexes must change.",
                "Keep locked board state and active-piece overlay separate so movement does not permanently write blocks until lock/drop.",
            ]
        )
        evidence.append("browser smoke reported invisible or non-moving active piece")

    if "initial board render is missing" in lowered:
        strategy = "browser_startup_patch" if strategy == "small_patch" else strategy
        html_targets = re.findall(r"html-smoke\s+([^\s`]+\.html)", combined, flags=re.IGNORECASE)
        focus_files.extend(html_targets or ["tetris.html"])
        instructions.append(
            "Ensure startup initializes the DOM board and renders the initial visible board state before the user interacts."
        )
        evidence.append("HTML smoke reported missing startup board initialization/render sequence")

    generated_test_focus = unique_ordered(
        path for path in [*generated_test_paths, *test_focus_files] if path.startswith("tests/")
    )
    native_struct_test_lines = native_struct_format_lines(project, generated_test_focus)
    binary_layout_symptoms = (
        ("unicodedecodeerror" in lowered and "utf-8" in lowered)
        or "\\x00\\x00\\x00" in lowered
        or "\x00\x00\x00" in combined
        or "3026418949592973312" in combined
        or "83886080" in combined
        or "150994944" in combined
        or "246 != -10" in combined
    ) and (
        "roundtrip_integer" in lowered
        or "decode_integer" in lowered
        or "large integer" in lowered
        or "roundtrip_text" in lowered
        or "decode_text" in lowered
        or "test_encode_positive_integer" in combined
        or "test_encode_ascii_text" in combined
        or "test_decode_ascii_text" in combined
        or "test_decode_negative_integer" in combined
    )
    if binary_layout_symptoms and native_struct_test_lines and generated_test_paths:
        strategy = "generated_binary_contract_alignment"
        focus_files.extend([*generated_test_focus, "minisqlite/storage/record.py"])
        instructions.extend(
            [
                "Treat the affected stage-generated binary tests as mutable specification proxies, not external immutable evidence.",
                "Align both generated tests and current-stage product codec with the same fixed binary proposition from SPEC.md.",
                "Replace native struct formats in generated tests and product code with explicit byte order/no-padding formats such as `>H`, `>Bq`, `>BI`, and `>qI`.",
                "Do not use native struct formats like `H`, `q`, `I`, `Bq`, `BI`, or `qI` for fixed record/cell layout assertions.",
                "Keep external or previous-stage tests read-only; edit only the generated stage test file(s) and current-stage product codec.",
            ]
        )
        evidence.extend(
            [
                "binary assertion values match native struct padding/endianness drift",
                *native_struct_test_lines[:6],
            ]
        )
    elif binary_layout_symptoms:
        strategy = "binary_struct_layout_patch"
        focus_files.append("minisqlite/storage/record.py")
        instructions.extend(
            [
                "Patch the binary codec layout at the product-code root cause; do not edit tests.",
                "Use explicit byte order and no native padding for every struct format; avoid native formats like `Bq` or `BI`.",
                "Keep the encoded field order identical to the decoder contract: type tag first, then INTEGER 8-byte signed big-endian or TEXT 4-byte length followed by UTF-8 bytes.",
                "For this record codec shape, INTEGER should be encoded like `struct.pack(\">Bq\", TYPE_INTEGER, value)` and TEXT like `struct.pack(\">BI\", TYPE_TEXT, len(utf8_bytes)) + utf8_bytes`; do not reverse the field order.",
                "After decoding the declared values, reject trailing bytes as corruption if the stage contract requires exact payload consumption.",
            ]
        )
        evidence.append("binary round-trip failures indicate struct byte-order/alignment or field-order mismatch")

    contract_lines = re.findall(r"(?m)^-\s+(C\d+)\s+\[[^\]]+\]:\s*(.+)$", combined)
    if contract_lines:
        if strategy == "small_patch":
            strategy = "semantic_contract_patch"
        if strategy not in TEST_HARNESS_WRITE_STRATEGIES:
            for contract_id, contract_text in contract_lines[:5]:
                instructions.append(f"Preserve semantic contract {contract_id}: {contract_text.strip()}")
            evidence.append("semantic contracts extracted from executable test evidence")

    repeated = re.findall(r"count=(\d+):\s*exception:\s*([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception):[^\n]+)", combined)
    if repeated:
        top_count, top_pattern = sorted(((int(count), pattern) for count, pattern in repeated), reverse=True)[0]
        if strategy not in TEST_HARNESS_WRITE_STRATEGIES:
            if strategy != "semantic_contract_patch":
                strategy = "root_cause_patch"
            instructions.append(f"Multiple checks share one exception pattern; patch the shared root cause first: {top_pattern}")
        else:
            instructions.append(
                f"Multiple checks share one exception pattern, but the classified owner is the test harness: {top_pattern}"
            )
        evidence.append(f"repeated exception count={top_count}: {top_pattern}")

    keyword_arg_error = re.search(
        r"TypeError:\s*([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception))\(\) takes no keyword arguments",
        combined,
    )
    if keyword_arg_error:
        strategy = "root_cause_patch"
        exception_name = keyword_arg_error.group(1)
        focus_files.append("minisqlite/errors.py")
        instructions.append(
            f"Patch the shared exception definition `{exception_name}` in minisqlite/errors.py so observed constructor calls are accepted."
        )
        evidence.append(f"`{exception_name}` constructor rejected keyword arguments")

    if "typeerror: argument must be read-write bytes-like object, not bytes" in lowered:
        strategy = "root_cause_patch"
        focus_files.extend(["minisqlite/storage/btree.py", "minisqlite/storage/pager.py"])
        instructions.append("Fix bytes/bytearray mutability at the storage root cause; do not weaken tests.")
        evidence.append("storage mutation attempted on immutable bytes")

    if (
        "test_comparison_op_not_equals" in combined
        or "unexpected character '!' at position" in lowered
        or "unexpected character '!'" in lowered
    ):
        strategy = "root_cause_patch"
        focus_files.append("minisqlite/sql/lexer.py")
        instructions.append(
            "For `!=`, do not tokenize `!` as a standalone symbol. Route `!` into the two-character comparison operator path before raising SQLSyntaxError."
        )
        evidence.append("lexer failed to tokenize `!=` as one comparison operator")

    if (
        "test_multiple_statements" in combined
        or "unexpected character '*' at position" in lowered
        or "unexpected character '*'" in lowered
    ):
        strategy = "root_cause_patch"
        focus_files.append("minisqlite/sql/lexer.py")
        instructions.append(
            "For `SELECT *`, tokenize `*` as one source token without adding hidden EOF/sentinel tokens."
        )
        evidence.append("lexer failed to tokenize `*` in SELECT statements")

    residual_operator_token = (
        ("got equals" in lowered or "got tokentype.equals" in lowered or "got equals (value='=')" in lowered)
        and (
            "greater_equals" in lowered
            or "less_equals" in lowered
            or "not_equals" in lowered
            or ">=" in combined
            or "<=" in combined
            or "!=" in combined
        )
    )
    if residual_operator_token:
        strategy = "root_cause_patch"
        focus_files.append("minisqlite/sql/lexer.py")
        instructions.extend(
            [
                "Treat a residual `=` token after `>=`, `<=`, or `!=` as an upstream lexer/operator consumption bug before changing parser value handling.",
                "Patch the lexer so multi-character operators consume both source characters and do not leave a second `=` token in the stream.",
            ]
        )
        evidence.append("parser received residual EQUALS after a multi-character comparison operator")

    name_error_match = re.search(r"NameError: name '([^']+)' is not defined", combined)
    if name_error_match:
        strategy = "small_patch"
        missing_name = name_error_match.group(1)
        instructions.append(f"Fix missing symbol `{missing_name}` at the import/definition site with the smallest patch.")
        evidence.append(f"NameError for `{missing_name}`")

    if "mechanical probe: python struct formats" in lowered and (
        "struct.error" in lowered or "struct.unpack" in lowered or "struct.pack" in lowered
    ):
        strategy = "root_cause_patch"
        product_focus = unique_ordered(
            path for path in [*product_trace_focus, *inferred_stage_focus] if not path.startswith("tests/")
        )
        focus_files.extend(product_focus)
        instructions.extend(
            [
                "Treat Mechanical Probe calcsize facts as authoritative; do not recalculate struct sizes in prose.",
                "Patch the product-code binary layout so pack/unpack formats, field widths, header constants, and slices agree.",
                "When a previous patch changed only one side of a pack/unpack pair and the same failure remained, inspect the paired format before emitting the next patch.",
                "Do not edit tests; they are read-only executable evidence for this binary layout failure.",
            ]
        )
        evidence.append("mechanical struct probe fixed deterministic byte-size facts")

    inferred_product_focus = unique_ordered(
        product_path
        for test_path in focus_files
        if test_path.startswith("tests/")
        for product_path in inferred_product_focus_from_test_path(test_path)
    )
    if inferred_product_focus and strategy not in TEST_HARNESS_WRITE_STRATEGIES:
        focus_files.extend(inferred_product_focus)
        instructions.append(
            "Traceback/assertion locations may point at tests; treat tests as read-only executable evidence and patch the inferred product file(s) first: "
            + ", ".join(inferred_product_focus[:4])
        )
        evidence.append("product focus inferred from conventional test file name")

    if not instructions:
        return None
    final_focus_files = unique_ordered(focus_files)
    if direct_pager_flush_clears_metadata:
        final_focus_files = unique_ordered(
            [
                "minisqlite/storage/pager.py",
                *[path for path in final_focus_files if path != "minisqlite/storage/pager.py"],
            ]
        )
    if (state_probe_points_below_cli or row_persistence_loss) and strategy not in TEST_HARNESS_WRITE_STRATEGIES:
        cli_layer_paths = {"minisqlite/cli.py", "minisqlite/__main__.py", "minisqlite/__init__.py"}
        narrowed = [path for path in final_focus_files if path not in cli_layer_paths]
        if narrowed:
            final_focus_files = narrowed
            if row_persistence_loss:
                evidence.append("CLI-layer focus demoted because row persistence failed below CLI close/reopen boundary")
            else:
                evidence.append("CLI-layer focus demoted because state probe failed below CLI close/reopen boundary")
    if strategy not in TEST_HARNESS_WRITE_STRATEGIES and strategy not in MIXED_PRODUCT_TEST_WRITE_STRATEGIES:
        product_only_focus = [path for path in final_focus_files if not path.startswith("tests/")]
        readonly_test_focus = [path for path in final_focus_files if path.startswith("tests/")]
        if product_only_focus:
            final_focus_files = product_only_focus
            if readonly_test_focus:
                instructions.append(
                    "Treat test traceback files as readonly evidence for this product-first strategy; do not emit artifacts for tests/ unless a later project-policy triage explicitly authorizes test-harness repair."
                )
                evidence.append("test focus demoted to readonly evidence: " + ", ".join(readonly_test_focus[:4]))
    return RepairAdvice(
        strategy=strategy,
        focus_files=tuple(final_focus_files)[:8],
        instructions=tuple(unique_ordered(instructions)),
        evidence=tuple(unique_ordered(evidence)),
    )

def repair_advice_policy_paths(
    advice: RepairAdvice | None,
    existing_paths: Sequence[str],
) -> tuple[list[str], list[str]]:
    """Return writable product paths and readonly evidence paths from repair advice."""
    if advice is None:
        return [], []
    existing = set(existing_paths)
    writable: list[str] = []
    readonly: list[str] = []
    test_harness_strategy = advice.strategy in TEST_HARNESS_WRITE_STRATEGIES
    mixed_product_test_strategy = advice.strategy in MIXED_PRODUCT_TEST_WRITE_STRATEGIES
    for path in advice.focus_files:
        if mixed_product_test_strategy and path in existing:
            writable.append(path)
        elif path.startswith("tests/") and test_harness_strategy:
            if path in existing:
                writable.append(path)
            else:
                writable.append(path)
        elif path.startswith("tests/"):
            readonly.append(path)
        elif path in existing and test_harness_strategy:
            readonly.append(path)
        elif path in existing:
            writable.append(path)
    return unique_ordered(writable), unique_ordered(readonly)

def focus_paths_from_failure_analysis(
    analysis: dict[str, object],
    existing_paths: Sequence[str],
) -> list[str]:
    """Infer concrete project files named by a structured failure analysis.

    The failure-analysis role often names a function or basename rather than a
    full path, for example `lexer.py _emit()`. Convert only unambiguous file
    references into project-relative paths. Ambiguous basenames are ignored
    instead of guessing.
    """
    action = analysis.get("next_required_action", {})
    if not isinstance(action, dict):
        action = {}
    existing = tuple(str(path) for path in existing_paths)
    by_basename: dict[str, list[str]] = {}
    for path in existing:
        by_basename.setdefault(Path(path).name, []).append(path)
    focus_texts: list[str] = []
    raw_focus = action.get("required_focus", [])
    if isinstance(raw_focus, list):
        focus_texts.extend(str(item) for item in raw_focus if isinstance(item, str))
    for key in ("goal", "summary", "rationale"):
        value = action.get(key)
        if isinstance(value, str):
            focus_texts.append(value)
    raw_constraints = analysis.get("active_constraints", [])
    if isinstance(raw_constraints, list):
        focus_texts.extend(str(item) for item in raw_constraints if isinstance(item, str))
    raw_facts = analysis.get("observed_facts", [])
    if isinstance(raw_facts, list):
        focus_texts.extend(str(item) for item in raw_facts if isinstance(item, str))

    focus_paths: list[str] = []
    raw_required_paths = action.get("required_paths", [])
    if isinstance(raw_required_paths, list):
        for item in raw_required_paths:
            if not isinstance(item, str):
                continue
            normalized = normalize_legacy_file_artifact_path(item)
            if normalized in existing:
                focus_paths.append(normalized)
                continue
            basename_matches = by_basename.get(Path(normalized).name, [])
            if len(basename_matches) == 1:
                focus_paths.append(basename_matches[0])
    for item in focus_texts:
        if not isinstance(item, str):
            continue
        candidates = re.findall(r"(?<![\w./-])([\w./-]+\.py)(?![\w./-])", item)
        for candidate in candidates:
            normalized = normalize_legacy_file_artifact_path(candidate)
            if normalized in existing:
                focus_paths.append(normalized)
                continue
            basename_matches = by_basename.get(Path(normalized).name, [])
            if len(basename_matches) == 1:
                focus_paths.append(basename_matches[0])
    return unique_ordered(focus_paths)

def deterministic_replacement_artifact_from_failure_analysis(
    analysis: dict[str, object],
    project: Path,
    artifact_policy: ArtifactPathPolicy,
    *,
    allow_replace_file: bool = True,
) -> tuple[str, str] | None:
    """Build a safe artifact for simple exact replacements from analysis.

    This is deliberately narrow. The failure-analysis role may identify an
    edit such as "replace all occurrences of X with Y in path". The runner may
    synthesize that artifact only when the target path is already writable, the
    file exists, and the old/new terms are short single-line tokens. Anything
    more ambiguous falls back to the normal LLM repair loop.
    """
    action = analysis.get("next_required_action", {})
    if not isinstance(action, dict):
        return None
    if str(action.get("next_patch_type", "")).strip().lower() != "search_replace":
        return None

    existing = tuple(str(path) for path in artifact_policy.existing_paths)
    required_paths: list[str] = []
    raw_required_paths = action.get("required_paths", [])
    if isinstance(raw_required_paths, list):
        for item in raw_required_paths:
            if not isinstance(item, str):
                continue
            normalized = normalize_legacy_file_artifact_path(item)
            if normalized in existing:
                required_paths.append(normalized)
    required_paths = unique_ordered(required_paths)
    if len(required_paths) != 1:
        return None
    path = required_paths[0]
    try:
        check_artifact_path(path, artifact_policy, "deterministic repair")
    except RunnerError:
        return None

    term = r"(?:`[^`\n]+`|'[^'\n]+'|\"[^\"\n]+\"|[A-Za-z_][A-Za-z0-9_.]*(?:\([^()\n]*\))?)"
    text_candidates = [
        str(action.get(key, ""))
        for key in ("minimal_patch_goal", "goal", "rationale")
        if isinstance(action.get(key, ""), str)
    ]
    raw_required_focus = action.get("required_focus", [])
    if isinstance(raw_required_focus, list):
        text_candidates.extend(str(item) for item in raw_required_focus if isinstance(item, str))

    parsed: tuple[str, str, str | None, bool] | None = None
    for text_item in text_candidates:
        replace_match = re.search(
            rf"\breplace\s+(?P<all>all\s+occurrences\s+of\s+)?(?P<old>{term})\s+with\s+(?P<new>{term})(?:\s+in\s+(?P<path>[\w./-]+\.py))?",
            text_item,
            flags=re.IGNORECASE,
        )
        if replace_match:
            parsed = (
                replace_match.group("old"),
                replace_match.group("new"),
                replace_match.group("path"),
                bool(replace_match.group("all")),
            )
            break
        instead_match = re.search(
            rf"\buse\s+(?P<new>{term})\s+instead\s+of\s+(?P<old>{term})(?:\s+in\s+(?P<path>[\w./-]+\.py))?",
            text_item,
            flags=re.IGNORECASE,
        )
        if instead_match:
            parsed = (
                instead_match.group("old"),
                instead_match.group("new"),
                instead_match.group("path"),
                False,
            )
            break
    if not parsed:
        return None

    old_raw, new_raw, path_raw, all_occurrences = parsed

    def clean_term(value: str) -> str | None:
        cleaned = value.strip().strip(".,;:")
        if (
            (cleaned.startswith("`") and cleaned.endswith("`"))
            or (cleaned.startswith("'") and cleaned.endswith("'"))
            or (cleaned.startswith('"') and cleaned.endswith('"'))
        ):
            cleaned = cleaned[1:-1]
        if not cleaned:
            return None
        if "\n" in cleaned or contains_artifact_markers(cleaned) or contains_conflict_markers(cleaned):
            return None
        if len(cleaned.encode("utf-8")) > 200:
            return None
        return cleaned

    search = clean_term(old_raw)
    replace = clean_term(new_raw)
    if not search or replace is None or search == replace:
        return None
    if path_raw:
        normalized_path = normalize_legacy_file_artifact_path(path_raw)
        if normalized_path != path and Path(normalized_path).name != Path(path).name:
            return None

    target = resolve_project_path(project, path)
    if not target.is_file():
        return None
    content = target.read_text(encoding="utf-8")
    occurrences = content.count(search)
    if occurrences < 1:
        return None
    if occurrences > 1 and not all_occurrences:
        return None

    if occurrences == 1:
        artifact = textwrap.dedent(
            f"""
            BEGIN_SEARCH_REPLACE: {path}
            <<<<<<< SEARCH
            {search}
            =======
            {replace}
            >>>>>>> REPLACE
            END_SEARCH_REPLACE
            """
        ).strip()
        artifact_type = "BEGIN_SEARCH_REPLACE"
    else:
        if not allow_replace_file:
            return None
        updated = content.replace(search, replace)
        if updated == content or contains_artifact_markers(updated):
            return None
        if not updated.endswith("\n"):
            updated += "\n"
        artifact = f"BEGIN_FILE: {path}\n{updated}END_FILE"
        artifact_type = "BEGIN_FILE"

    summary = textwrap.dedent(
        f"""
        ## Deterministic Replacement Repair

        - status: PASS
        - source: structured failure_analysis.next_required_action
        - path: {path}
        - artifact_type: {artifact_type}
        - search: {search}
        - replace: {replace}
        - occurrences: {occurrences}

        Runner action:
        - Built a deterministic artifact instead of asking the LLM to re-emit
          a fragile repair artifact.
        - The artifact still goes through normal lint, extraction, apply, and
          executable checks.
        """
    ).strip()
    return artifact, summary

def deterministic_replacement_artifact_from_repair_advice(
    advice: dict[str, object],
    project: Path,
    artifact_policy: ArtifactPathPolicy,
    *,
    allow_replace_file: bool = True,
) -> tuple[str, str] | None:
    """Build a safe test-harness API replacement from repair advice.

    This handles the common deterministic case where executable evidence says a
    generated test calls an absent attribute and Python reports a concrete
    did-you-mean public API.  The helper does not infer broad semantics; it only
    converts `.missing_api` references in an authorized test harness to the
    observed public name.
    """
    strategy = str(advice.get("strategy", "")).strip()
    if strategy not in TEST_HARNESS_WRITE_STRATEGIES:
        return None
    focus_files = [
        normalize_legacy_file_artifact_path(str(item))
        for item in advice.get("focus_files", [])
        if isinstance(item, str)
    ] if isinstance(advice.get("focus_files", []), list) else []
    test_paths = [path for path in unique_ordered(focus_files) if path.startswith("tests/")]
    if len(test_paths) != 1:
        return None

    text_parts = [strategy]
    for key in ("instructions", "evidence"):
        values = advice.get(key, [])
        if isinstance(values, list):
            text_parts.extend(str(item) for item in values if isinstance(item, str))
    combined = "\n".join(text_parts)
    match = re.search(
        r"AttributeError:[^\n]*no attribute ['\"](?P<old>[A-Za-z_][A-Za-z0-9_]{0,120})['\"]\.\s*Did you mean: ['\"](?P<new>[A-Za-z_][A-Za-z0-9_]{0,120})['\"]",
        combined,
    )
    if not match:
        return None
    old_name = match.group("old")
    new_name = match.group("new")
    if old_name == new_name:
        return None

    analysis = {
        "next_required_action": {
            "required_paths": [test_paths[0]],
            "next_patch_type": "search_replace",
            "minimal_patch_goal": f"Replace all occurrences of {old_name} with {new_name} in {test_paths[0]}",
        }
    }
    result = deterministic_replacement_artifact_from_failure_analysis(
        analysis,
        project,
        artifact_policy,
        allow_replace_file=allow_replace_file,
    )
    if not result:
        return None
    artifact, summary = result
    summary += "\n- repair_advice_strategy: " + strategy
    return artifact, summary

def repair_unclosed_paren_line(line: str, message: str) -> str | None:
    """Return a one-line syntax repair for a narrow parse-time defect."""
    if "was never closed" not in message or "(" not in message:
        return None
    if "(" not in line:
        return None
    if "#" in line:
        code, comment = line.split("#", 1)
        if code.count("(") <= code.count(")"):
            return None
        code_without_trailing = code.rstrip()
        spacing = code[len(code_without_trailing):]
        return code_without_trailing + ")" + spacing + "#" + comment
    if line.count("(") <= line.count(")"):
        return None
    stripped = line.rstrip()
    trailing = line[len(stripped):]
    return stripped + ")" + trailing

def deterministic_python_syntax_repair_artifact(
    project: Path,
    artifact_policy: ArtifactPathPolicy,
    candidate_paths: Sequence[str],
) -> tuple[str, str] | None:
    """Build a minimal artifact for authorized generated-test syntax defects.

    This is intentionally parser-backed and narrow: only runner-writable test
    files are considered, and only one-line parse defects with an exact source
    line are repaired. Product behavior and assertions are not weakened; the
    goal is to make the generated test harness executable.
    """
    for raw_path in unique_ordered(candidate_paths):
        path = normalize_legacy_file_artifact_path(str(raw_path))
        if not path.startswith("tests/"):
            continue
        try:
            check_artifact_path(path, artifact_policy, "deterministic syntax repair")
        except RunnerError:
            continue
        target = resolve_project_path(project, path)
        if not target.is_file():
            continue
        try:
            content = target.read_text(encoding="utf-8")
            ast.parse(content, filename=path)
        except SyntaxError as exc:
            if not exc.lineno or exc.lineno < 1:
                continue
            lines = content.splitlines()
            if exc.lineno > len(lines):
                continue
            original = lines[exc.lineno - 1]
            repaired = repair_unclosed_paren_line(original, exc.msg or "")
            if not repaired or repaired == original:
                continue
            artifact = textwrap.dedent(
                f"""
                BEGIN_SEARCH_REPLACE: {path}
                <<<<<<< SEARCH
                {original}
                =======
                {repaired}
                >>>>>>> REPLACE
                END_SEARCH_REPLACE
                """
            ).strip()
            summary = textwrap.dedent(
                f"""
                ## Deterministic Python Syntax Repair

                - status: PASS
                - source: ast.parse SyntaxError
                - path: {path}
                - line: {exc.lineno}
                - message: {exc.msg}
                - artifact_type: BEGIN_SEARCH_REPLACE

                Runner action:
                - Built an exact-line syntax repair for an authorized test harness file.
                - The artifact still goes through normal lint, extraction, apply, and executable checks.
                """
            ).strip()
            return artifact, summary
        except (OSError, UnicodeDecodeError):
            continue
    return None

def patch_plan_paths_from_text(
    plan_doc: str,
    existing_paths: Sequence[str],
) -> dict[str, list[str]]:
    """Parse PATCH_PLAN path fields without trusting the planner blindly.

    The planner may abbreviate a unique basename, but it does not get direct
    write authority. The runner uses this parsed result only after resolving a
    path to a known project-relative file.
    """
    existing = tuple(str(path) for path in existing_paths)
    by_basename: dict[str, list[str]] = {}
    for path in existing:
        by_basename.setdefault(Path(path).name, []).append(path)

    def resolve_path(raw_path: str) -> str | None:
        normalized = normalize_legacy_file_artifact_path(raw_path.strip())
        if not normalized or normalized.lower() in {"(none)", "none", "n/a"}:
            return None
        normalized = normalized.strip("`'\" ")
        if normalized in existing:
            return normalized
        basename_matches = by_basename.get(Path(normalized).name, [])
        if len(basename_matches) == 1:
            return basename_matches[0]
        return None

    fields: dict[str, list[str]] = {
        "required_paths": [],
        "readonly_paths": [],
        "forbidden_paths": [],
    }
    line_values: dict[str, str] = {}
    for match in re.finditer(r"(?im)^\s*-\s*(required_path|readonly_paths|forbidden_paths)\s*:\s*(.*?)\s*$", plan_doc):
        line_values[match.group(1).lower()] = match.group(2).strip()

    required = line_values.get("required_path", "")
    resolved_required = resolve_path(required)
    if resolved_required:
        fields["required_paths"].append(resolved_required)

    for field_name in ("readonly_paths", "forbidden_paths"):
        raw_value = line_values.get(field_name, "")
        for item in re.split(r"[,;\n]", raw_value):
            resolved = resolve_path(item)
            if resolved:
                fields[field_name].append(resolved)

    return {key: unique_ordered(value) for key, value in fields.items()}

def final_failure_focus_from_command_docs(
    command_docs: Sequence[tuple[str, str]],
    test_commands: Sequence[str],
) -> RepairAdvice | None:
    combined = "\n".join(document for _name, document in command_docs)
    focus_files: list[str] = []
    instructions: list[str] = []
    evidence: list[str] = []

    for raw_path in re.findall(r'File "([^"]+)", line \d+', combined):
        if "/tests/" in raw_path:
            focus_files.append("tests/" + raw_path.split("/tests/", 1)[1])
        elif "/minisqlite/" in raw_path:
            focus_files.append("minisqlite/" + raw_path.split("/minisqlite/", 1)[1])
        else:
            path = Path(raw_path)
            if not path.is_absolute() and ".." not in path.parts and path.suffix:
                focus_files.append(str(path))

    for match in re.findall(r"(?m)^(?:ERROR|FAIL):\s+([A-Za-z_][\w.]+)", combined):
        evidence.append(f"failed test: {match}")
    for match in re.findall(r"([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception)):[^\n]{0,160}", combined):
        evidence.append(f"exception: {match}")
    for match in re.findall(r"cannot import name '([^']+)'|No module named '([^']+)'", combined):
        symbol = next((item for item in match if item), "")
        if symbol:
            evidence.append(f"missing symbol/module: {symbol}")

    base_advice = repair_advice_from_command_docs(command_docs, test_commands)
    if base_advice:
        focus_files.extend(base_advice.focus_files)
        instructions.extend(base_advice.instructions)
        evidence.extend(base_advice.evidence)

    product_focus = [path for path in unique_ordered(focus_files) if not path.startswith("tests/")]
    test_focus = [path for path in unique_ordered(focus_files) if path.startswith("tests/")]
    if product_focus:
        instructions.append("Focus final integration repair on product code first; tests are read-only evidence.")
    elif test_focus:
        instructions.append("Traceback points at tests; infer the product API contract from the assertion/import and repair product code only.")
    if not product_focus and not test_focus and not instructions:
        return None

    return RepairAdvice(
        strategy=base_advice.strategy if base_advice else "final_failure_focus",
        focus_files=tuple(unique_ordered([*product_focus, *test_focus]))[:8],
        instructions=tuple(unique_ordered(instructions)),
        evidence=tuple(unique_ordered(evidence)),
    )

def repair_advice_document(advice: RepairAdvice) -> str:
    lines = [
        "## Repair Strategy Advice",
        "",
        f"- strategy: {advice.strategy}",
    ]
    if advice.focus_files:
        lines.append("- focus_files:")
        lines.extend(f"  - {path}" for path in advice.focus_files)
    if advice.instructions:
        lines.append("- instructions:")
        lines.extend(f"  - {item}" for item in advice.instructions)
    if advice.evidence:
        lines.append("- evidence:")
        lines.extend(f"  - {item}" for item in advice.evidence)
    return "\n".join(lines)

def artifact_failure_modes_from_documents(documents: Sequence[tuple[str, str]], window: int) -> set[str]:
    recent_text = "\n\n".join(document for _name, document in documents[-max(1, window) :]).lower()
    modes: set[str] = set()
    if "patch extraction failure" in recent_text or "artifact_invalid" in recent_text or "file artifact extraction also failed" in recent_text:
        modes.add("artifact_invalid")
    if "non_artifact_output" in recent_text:
        modes.add("non_artifact_output")
    if "missing_context" in recent_text:
        modes.add("missing_context")
    semantic_format_codes = {
        "semantic_repair_missing_path",
        "semantic_repair_prose_mixed",
        "semantic_repair_markdown_fence",
        "semantic_repair_malformed_search_replace",
        "semantic_repair_multiple_artifacts",
        "semantic_repair_not_atomic",
        "semantic_repair_forbidden_artifact",
        "semantic_repair_test_edit",
        "semantic_repair_too_large",
    }
    if any(code in recent_text for code in semantic_format_codes):
        modes.add("semantic_repair_format")
    format_repair_codes = {
        "format_repair_missing_path",
        "format_repair_prose_mixed",
        "format_repair_markdown_fence",
        "format_repair_malformed_search_replace",
        "artifact_orphan_search_replace",
        "format_repair_unbalanced_file_artifact",
        "format_repair_no_artifact",
        "stream_repeated_text_runaway",
        "stream_repeated_json_search_replace",
        "stream_multiple_json_search_replace",
        "stream_json_search_replace_excess",
        "stream_markdown_fence_before_artifact",
        "stream_prose_before_artifact",
        "stream_non_artifact_output",
        "stream_json_plan_before_artifact",
        "stream_mixed_artifact_formats",
        "stream_multiple_file_artifacts_in_repair",
        "stream_artifact_too_large",
        "stream_python_file_artifact_too_large",
        "stream_python_diff_artifact_too_large",
        "stream_artifact_process_narration",
        "stream_artifact_malformed_search_replace",
        "stream_orphan_search_replace",
        "stream_identical_search_replace",
        "stream_search_replace_conflict_markers",
        "stream_root_cause_too_large",
    }
    if any(code in recent_text for code in format_repair_codes):
        modes.add("format_repair_protocol")
    if "stream_json_plan_before_artifact" in recent_text:
        modes.add("json_plan_before_artifact")
    if "stream_mixed_artifact_formats" in recent_text or "mixed json file artifacts" in recent_text:
        modes.add("mixed_artifact_formats")
    if (
        "stream_multiple_file_artifacts_in_repair" in recent_text
        or "multiple begin_file artifacts appeared in one repair stream" in recent_text
        or "multiple json file artifacts appeared in one repair stream" in recent_text
        or "multi-file unified diff appeared in one repair stream" in recent_text
    ):
        modes.add("single_artifact_required")
    if (
        "stream_multiple_json_search_replace" in recent_text
        or "multiple json search_replace" in recent_text
        or "search/replace artifact exceeded the stream size budget" in recent_text
        or "stream_prose_before_artifact" in recent_text
        or "stream_non_artifact_output" in recent_text
        or "stream_artifact_process_narration" in recent_text
    ):
        modes.add("atomic_search_replace_required")
    if "function replacement target" in recent_text and "must occur exactly once" in recent_text:
        modes.add("ambiguous_function_replacement")
    if "stream_python_file_artifact_too_large" in recent_text:
        modes.add("oversized_python_file_artifact")
    if "stream_python_diff_artifact_too_large" in recent_text:
        modes.add("oversized_python_diff_artifact")
    if "malformed_search_replace" in recent_text or "orphan_search_replace" in recent_text:
        modes.add("malformed_search_replace")
    if (
        "test_edit_attempt" in recent_text
        or "path is read-only: tests/" in recent_text
        or "stream_readonly_artifact_path" in recent_text
        or "read-only evidence path" in recent_text
    ):
        modes.add("test_edit_attempt")
    if (
        "search text must occur exactly once" in recent_text
        or ("replacement for" in recent_text and "is identical to the search text" in recent_text)
        or ("replacement for" in recent_text and "contains conflict markers" in recent_text)
        or "search_replace_conflict_markers" in recent_text
        or "stream_search_replace_conflict_markers" in recent_text
        or "identical_search_replace" in recent_text
        or "stream_identical_search_replace" in recent_text
    ):
        modes.add("bad_search_replace")
    if "artifact_path_content_mismatch" in recent_text:
        modes.add("path_content_mismatch")
    if "forbidden_absent_api_addition" in recent_text or "forbidden_absent_api_call" in recent_text:
        modes.add("forbidden_api_violation")
    if "forbidden_repair_target_edit" in recent_text:
        modes.add("forbidden_repair_target_edit")
    if "would not change any files" in recent_text or "would not change any file content" in recent_text or "skipped the patch" in recent_text:
        modes.add("empty_or_skipped_patch")
    if "corrupt patch" in recent_text or "corrupt_unified_diff" in recent_text or "git apply --numstat failed" in recent_text:
        modes.add("corrupt_unified_diff")
    if "stage_scope_violation" in recent_text or "outside the current stage scope" in recent_text:
        modes.add("stage_scope_violation")
    if "invalid json artifact" in recent_text or "jsondecodeerror" in recent_text:
        modes.add("bad_json")
    if (
        "repeated_json_search_replace" in recent_text
        or "too many repeated json search_replace" in recent_text
        or "stream_multiple_json_search_replace" in recent_text
    ):
        modes.add("repeated_json")
    return modes

def artifact_failure_instruction_from_documents(documents: Sequence[tuple[str, str]], window: int) -> str:
    modes = artifact_failure_modes_from_documents(documents, window)
    instructions: list[str] = []
    if "bad_search_replace" in modes:
        instructions.extend(
            [
                "Previous artifact failure: search_replace was invalid.",
                "Do not emit search_replace in this round.",
                "For the affected writable file, emit BEGIN_FILE with complete file content, or a valid minimal unified diff.",
            ]
        )
    if "ambiguous_function_replacement" in modes:
        instructions.extend(
            [
                "Previous loose Python function replacement was ambiguous because the function name occurs multiple times.",
                "Do not emit a fenced function body after BEGIN_SEARCH_REPLACE.",
                "Use explicit SEARCH/REPLACE markers with a short unique contiguous snippet that includes class or surrounding context.",
                "If coupled edits are required in the same file, use a bounded same-file JSON search_replace edit set.",
            ]
        )
    if "path_content_mismatch" in modes:
        instructions.extend(
            [
                "Previous artifact declared one file path but used search/replace content from a different owner.",
                "Before emitting an artifact, ensure path(A) and body(A) come from the same file.",
                "If the desired edit is in product code, search only product-code text from that file; do not paste test methods into a product path.",
            ]
        )
    if "forbidden_api_violation" in modes:
        instructions.extend(
            [
                "Previous artifact violated a supervisor-forbidden API proposition.",
                "Do not add or call an API that Mechanical Probe or project-policy triage classified as absent/forbidden.",
                "Patch the observed call site to use an existing public API, or return MISSING_CONTEXT for the required file.",
            ]
        )
    if "forbidden_repair_target_edit" in modes:
        instructions.extend(
            [
                "Previous artifact edited a target explicitly forbidden by current repair advice.",
                "Do not touch forbidden functions, methods, CLI output, README, or tests for this failure family.",
                "Choose a different product-code root cause consistent with the mechanical probe.",
            ]
        )
    if "bad_json" in modes:
        instructions.extend(
            [
                "Previous artifact failure: JSON was invalid.",
                "Do not emit JSON artifacts in this round.",
                "Use BEGIN_FILE/END_FILE for full-file replacement or BEGIN_SEARCH_REPLACE for a short exact edit.",
            ]
        )
    if "repeated_json" in modes:
        instructions.extend(
            [
                "Previous artifact failure: repeated JSON search_replace loop.",
                "Do not emit JSON artifacts or repeated search_replace objects.",
            ]
        )
    if "atomic_search_replace_required" in modes:
        instructions.extend(
            [
                "Previous repair violated atomicity by emitting an oversized or multi-edit search_replace artifact.",
                "Use exactly one BEGIN_SEARCH_REPLACE block for one existing writable product file.",
                "Edit one contiguous code region that addresses one failing predicate or one repeated exception family.",
                "Do not emit JSON, multiple search_replace objects, BEGIN_FILE, unified diff, prose, or a multi-function rewrite.",
            ]
        )
    if "single_artifact_required" in modes:
        instructions.extend(
            [
                "Previous repair violated the one-artifact repair contract by emitting multiple file artifacts.",
                "Return exactly one artifact for exactly one writable target file.",
                "Do not regenerate the stage, README plus code, tests plus code, or multiple files in one response.",
            ]
        )
    if "malformed_search_replace" in modes:
        instructions.extend(
            [
                "Previous artifact failure: malformed BEGIN_SEARCH_REPLACE grammar.",
                "After `BEGIN_SEARCH_REPLACE: path/to/file`, the next line must be exactly `<<<<<<< SEARCH`.",
                "Do not emit colon-prefixed code such as `: def ...`; use a complete search/replace block or a unified diff.",
            ]
        )
    if "artifact_invalid" in modes:
        instructions.extend(
            [
                "Previous output had useful intent but invalid artifact structure.",
                "Use format_repair behavior: preserve the previous semantic edit intent and convert it to valid artifacts only.",
            ]
        )
    if "stage_scope_violation" in modes:
        instructions.extend(
            [
                "Previous generated tests violated the current-stage scope contract.",
                "Rewrite generated test artifacts so they assert only the current stage goal.",
                "Remove future-stage predicates such as split/internal/multi-page/e2e behavior unless the current stage explicitly requires them.",
                "Do not broaden product code to satisfy an out-of-scope generated test.",
            ]
        )
    if "semantic_repair_format" in modes:
        instructions.extend(
            [
                "Previous semantic repair output violated the artifact grammar.",
                "Preserve the intended product-code edit, but rewrite only the artifact envelope.",
                "The first non-whitespace characters must be `BEGIN_SEARCH_REPLACE: ` or `diff --git `.",
                "Return exactly one product-code artifact; no prose, markdown fences, JSON, BEGIN_FILE, or test edits.",
            ]
        )
    if "format_repair_protocol" in modes:
        instructions.extend(
            [
                "Previous format repair output violated the artifact protocol.",
                "Preserve the intended edit, but rewrite only the artifact envelope.",
                "Start with a valid artifact marker: BEGIN_SEARCH_REPLACE:, diff --git, or BEGIN_FILE:.",
                "No prose, headings, markdown fences, long keyword lists, repeated tokens, or alternative patches.",
            ]
        )
    if "json_plan_before_artifact" in modes:
        instructions.extend(
            [
                "Previous output mixed a JSON plan/proposition block before the artifact.",
                "Do not emit propositions, plans, graphs, JSON notes, or analysis in the answer.",
                "The first non-whitespace bytes must be an artifact marker, not `{` or `[`.",
            ]
        )
    if "mixed_artifact_formats" in modes:
        instructions.extend(
            [
                "Previous output had more than one recoverable artifact interpretation.",
                "Choose exactly one artifact protocol in this round.",
                "For existing-file repairs, use exactly one BEGIN_SEARCH_REPLACE block.",
                "Use BEGIN_FILE only when the target file is missing or explicitly generated.",
                "Do not emit JSON artifacts, JSON plans, whole-file rewrites for existing repairs, or a second artifact protocol.",
            ]
        )
    if "oversized_python_file_artifact" in modes:
        instructions.extend(
            [
                "Previous output attempted a monolithic or multi-file Python artifact that exceeded the stream budget.",
                "Split the work: emit exactly one artifact for one missing/generated writable file in this round.",
                "For a missing generated file, use one balanced BEGIN_FILE/END_FILE block with complete content.",
                "For an existing file repair, use one focused BEGIN_SEARCH_REPLACE block.",
                "Do not emit JSON, unified diff, README plus code, tests plus code, or multiple file artifacts in the same round.",
            ]
        )
    if "oversized_python_diff_artifact" in modes:
        instructions.extend(
            [
                "Previous output attempted a large unified diff for Python generated files.",
                "Do not emit another unified diff in this repair round.",
                "For missing generated Python files, use balanced BEGIN_FILE/END_FILE blocks with complete file content.",
            ]
        )
    if "empty_or_skipped_patch" in modes:
        instructions.extend(
            [
                "Previous unified diff was empty or skipped by git apply.",
                "Do not emit another unified diff for missing new files.",
                "For each missing generated file, use a balanced BEGIN_FILE/END_FILE block with complete content.",
            ]
        )
    if "corrupt_unified_diff" in modes:
        instructions.extend(
            [
                "Previous unified diff was corrupt or could not be parsed by git apply.",
                "Do not emit another unified diff in this repair round.",
                "For existing-file repairs, use exactly one BEGIN_SEARCH_REPLACE block with a short unique search snippet.",
                "Use BEGIN_FILE only when the target file is missing or explicitly generated.",
            ]
        )
    if "non_artifact_output" in modes:
        instructions.extend(
            [
                "Previous coder output spent too many bytes on prose before an artifact.",
                "Start immediately with the artifact marker; no explanation before or after it.",
            ]
        )
    if "test_edit_attempt" in modes:
        instructions.extend(
            [
                "Previous output attempted to edit a read-only test file.",
                "Repair product code only. Tests are executable evidence, not writable targets.",
            ]
        )
    if not instructions:
        return ""
    return "\n".join(["Artifact failure constraint:", *[f"- {item}" for item in unique_ordered(instructions)]])

def strict_artifact_output_instruction(modes: set[str]) -> tuple[str | None, str | None]:
    if not modes:
        return None, None
    if "single_artifact_required" in modes:
        instruction = textwrap.dedent(
            """
            Required output for this repair round:
            - SINGLE ARTIFACT REPAIR MODE.
            - The previous repair emitted multiple file artifacts for one repair intent.
            - Model this round as one proposition: one failing predicate or one repeated exception family maps to one smallest action.
            - The first non-whitespace characters must be exactly one of `BEGIN_SEARCH_REPLACE: `, `BEGIN_FILE: `, or `diff --git `.
            - Return exactly one artifact for exactly one writable target file.
            - Use BEGIN_SEARCH_REPLACE for an existing-file local repair.
            - Use BEGIN_FILE only when the one target file must be created or completely regenerated.
            - Use a unified diff only if it touches one file and one local hunk.
            - Do not return prose, headings, markdown fences, JSON, JSON artifacts, README plus code, tests plus code, multiple BEGIN_FILE blocks, multiple diffs, proposition lists, graph objects, or alternatives.
            """
        ).strip()
        contract = (
            "Return ONLY one artifact for one target file. First non-whitespace bytes: "
            "BEGIN_SEARCH_REPLACE:, BEGIN_FILE:, or diff --git. No JSON. No prose. "
            "No fences. No multiple files. No multiple artifacts."
        )
        return instruction, contract
    if "atomic_search_replace_required" in modes:
        instruction = textwrap.dedent(
            """
            Required output for this repair round:
            - ATOMIC SEARCH_REPLACE REPAIR MODE.
            - The previous repair emitted too much output, multiple search_replace edits, or analysis instead of one patch.
            - Model this round as one proposition: fix exactly one failing predicate or one repeated exception family.
            - The first non-whitespace characters must be exactly `BEGIN_SEARCH_REPLACE: `.
            - Return exactly one BEGIN_SEARCH_REPLACE/END_SEARCH_REPLACE block for one existing writable product file.
            - The SEARCH text must be short, exact, contiguous, and occur exactly once in the current file.
            - The REPLACE text may change only that contiguous region; do not rewrite unrelated functions.
            - Do not return prose, headings, markdown fences, JSON, JSON search_replace, unified diff, BEGIN_FILE, proposition lists, graph objects, or multiple alternatives.
            """
        ).strip()
        contract = (
            "Return ONLY one atomic BEGIN_SEARCH_REPLACE artifact for an existing product file. "
            "First non-whitespace bytes: BEGIN_SEARCH_REPLACE:. One file. One contiguous edit. "
            "No JSON. No diff. No BEGIN_FILE. No prose."
        )
        return instruction, contract
    if "mixed_artifact_formats" in modes:
        instruction = textwrap.dedent(
            """
            Required output for this repair round:
            - MIXED ARTIFACT PROTOCOL REPAIR MODE.
            - The previous response produced more than one artifact interpretation for the same repair.
            - Choose exactly one artifact protocol for one writable target file.
            - For an existing-file repair, the first non-whitespace characters must be exactly `BEGIN_SEARCH_REPLACE: `.
            - Use BEGIN_FILE only if the one target file is missing or explicitly generated.
            - Do not return JSON, JSON artifacts, JSON plans, proposition lists, graph objects, markdown fences, prose, whole-file rewrites for existing repairs, or multiple alternatives.
            """
        ).strip()
        contract = (
            "Return ONLY one artifact for one target file. For existing-file repairs, "
            "first non-whitespace bytes: BEGIN_SEARCH_REPLACE:. No JSON. No prose. No mixed protocols."
        )
        return instruction, contract
    if "corrupt_unified_diff" in modes and "empty_or_skipped_patch" not in modes and "oversized_python_diff_artifact" not in modes:
        instruction = textwrap.dedent(
            """
            Required output for this repair round:
            - CORRUPT DIFF EXISTING-FILE REPAIR MODE.
            - The previous unified diff was corrupt or could not be parsed by git apply.
            - Do not return another unified diff in this round.
            - The first non-whitespace characters must be exactly `BEGIN_SEARCH_REPLACE: `.
            - Return exactly one BEGIN_SEARCH_REPLACE/END_SEARCH_REPLACE block for one existing writable product file.
            - The SEARCH text must be short, exact, and occur exactly once in the current file.
            - Do not return prose, headings, markdown fences, JSON, proposition lists, graph objects, BEGIN_FILE, or multiple alternatives.
            """
        ).strip()
        contract = (
            "Return ONLY one BEGIN_SEARCH_REPLACE artifact for an existing file. "
            "First non-whitespace bytes: BEGIN_SEARCH_REPLACE:. No diff. No BEGIN_FILE. No JSON. No prose."
        )
        return instruction, contract
    if "empty_or_skipped_patch" in modes or "oversized_python_diff_artifact" in modes:
        instruction = textwrap.dedent(
            """
            Required output for this repair round:
            - SKIPPED/OVERSIZED DIFF REPAIR MODE.
            - The previous unified diff was empty, skipped, too large, or did not create the required file.
            - Do not return a unified diff in this round.
            - The first non-whitespace characters must be exactly `BEGIN_FILE: `.
            - Return one balanced BEGIN_FILE/END_FILE block per missing generated file.
            - Each BEGIN_FILE block must contain complete non-empty file content.
            - Do not return prose, headings, markdown fences, JSON, proposition lists, graph objects, BEGIN_SEARCH_REPLACE, or multiple alternatives.
            """
        ).strip()
        contract = (
            "Return ONLY balanced BEGIN_FILE/END_FILE artifacts for missing files. "
            "First non-whitespace bytes: BEGIN_FILE:. No diff. No JSON. No prose."
        )
        return instruction, contract
    if "malformed_search_replace" in modes or "bad_search_replace" in modes:
        instruction = textwrap.dedent(
            """
            Required output for this repair round:
            - SEARCH_REPLACE FAILURE MODE.
            - The previous search_replace artifact was malformed, non-unique, or unsafe.
            - The first non-whitespace characters must be exactly `BEGIN_SEARCH_REPLACE: `.
            - Return exactly one BEGIN_SEARCH_REPLACE/END_SEARCH_REPLACE block for one existing writable product file.
            - After the path line, the next line must be exactly `<<<<<<< SEARCH`.
            - The SEARCH text must be short, exact, contiguous, and occur exactly once in the current file.
            - Do not return prose, test reports, markdown fences, JSON, JSON search_replace, colon-prefixed snippets, unified diff, BEGIN_FILE, or multiple alternative patches.
            """
        ).strip()
        contract = (
            "Return ONLY one well-formed BEGIN_SEARCH_REPLACE artifact. "
            "First non-whitespace bytes: BEGIN_SEARCH_REPLACE:. No JSON. No diff. No BEGIN_FILE. No prose."
        )
        return instruction, contract
    if "stage_scope_violation" in modes:
        instruction = textwrap.dedent(
            """
            Required output for this repair round:
            - STAGE SCOPE REPAIR MODE.
            - The previous artifact asserted predicates outside the current stage goal.
            - Rewrite the offending artifact so every test assertion and product edit belongs to the current stage only.
            - Do not include future-stage concepts such as split/internal/multi-page/e2e behavior unless the current stage explicitly says so.
            - The first non-whitespace characters must be exactly `BEGIN_FILE: `, `BEGIN_SEARCH_REPLACE: `, or `diff --git `.
            - Return one valid artifact unless multiple current-stage generated files are explicitly required.
            - Do not return prose, JSON plans, markdown fences, proposition lists, graph objects, or self-judgement.
            """
        ).strip()
        contract = (
            "Return ONLY current-stage artifacts. First non-whitespace bytes: BEGIN_FILE:, "
            "BEGIN_SEARCH_REPLACE:, or diff --git. No future-stage predicates. No prose. No JSON."
        )
        return instruction, contract
    if "oversized_python_file_artifact" in modes:
        instruction = textwrap.dedent(
            """
            Required output for this repair round:
            - ONE-FILE PYTHON ARTIFACT BUDGET MODE.
            - The previous response tried to emit a monolithic or multi-file Python artifact and exceeded the stream budget.
            - Split the stage into one writable file per round.
            - If the next required file is missing or generated, the first non-whitespace characters must be exactly `BEGIN_FILE: `.
            - Return exactly one balanced BEGIN_FILE/END_FILE block containing complete content for that one missing/generated file.
            - If the target file already exists and only needs repair, return exactly one BEGIN_SEARCH_REPLACE/END_SEARCH_REPLACE block instead.
            - Do not return prose, headings, markdown fences, JSON, JSON artifacts, proposition lists, graph objects, unified diffs, README plus code, tests plus code, or multiple alternatives.
            """
        ).strip()
        contract = (
            "Return ONLY one file artifact for one target file. "
            "First non-whitespace bytes: BEGIN_FILE: for missing/generated files, or BEGIN_SEARCH_REPLACE: for existing-file repair. "
            "No JSON. No diff. No prose. No fences. No multiple files."
        )
        return instruction, contract
    if "json_plan_before_artifact" in modes:
        instruction = textwrap.dedent(
            """
            Required output for this repair round:
            - ARTIFACT-ONLY MODE.
            - The previous response emitted a JSON plan/proposition block before the artifact.
            - Do not externalize reasoning. Keep propositions and graph reasoning internal.
            - The first non-whitespace characters must be exactly `BEGIN_FILE: `, `BEGIN_SEARCH_REPLACE: `, or `diff --git `.
            - Return only valid artifacts that can be applied by the runner.
            - Do not return JSON, prose, headings, markdown fences, proposition lists, graph objects, test reports, or multiple alternative patches.
            """
        ).strip()
        contract = (
            "Return ONLY artifacts. First non-whitespace bytes: BEGIN_FILE:, "
            "BEGIN_SEARCH_REPLACE:, or diff --git. No JSON plans. No prose. No fences."
        )
        return instruction, contract
    if "semantic_repair_format" in modes:
        instruction = textwrap.dedent(
            """
            Required output for this repair round:
            - SEMANTIC FORMAT REPAIR MODE.
            - Preserve the previous semantic edit intent; do not redesign the solution.
            - The first non-whitespace characters must be exactly `BEGIN_SEARCH_REPLACE: ` or `diff --git `.
            - Return exactly one valid product-code artifact.
            - Valid form A:
              BEGIN_SEARCH_REPLACE: path/to/product_file.py
              <<<<<<< SEARCH
              exact old text
              =======
              exact new text
              >>>>>>> REPLACE
              END_SEARCH_REPLACE
            - Valid form B: one minimal unified diff touching one product-code file.
            - Do not return prose, headings, markdown fences, JSON, BEGIN_FILE, BEGIN_APPEND_FILE, test edits, or multiple alternatives.
            """
        ).strip()
        contract = (
            "Return ONLY one valid semantic repair artifact. First non-whitespace bytes: "
            "BEGIN_SEARCH_REPLACE: or diff --git. No prose. No fences. No JSON. No tests."
        )
        return instruction, contract
    if "format_repair_protocol" in modes:
        instruction = textwrap.dedent(
            """
            Required output for this repair round:
            - STRICT FORMAT REPAIR MODE.
            - Preserve the previous semantic edit intent; do not redesign the solution.
            - The first non-whitespace characters must be one of: `BEGIN_FILE:`, `BEGIN_SEARCH_REPLACE:`, or `diff --git `.
            - Return only valid artifacts that can be applied by the runner.
            - Use balanced BEGIN_FILE/END_FILE blocks for generated files.
            - Use BEGIN_SEARCH_REPLACE with a required `: path/to/file` suffix for exact local edits.
            - Do not return JSON plans, prose, headings, markdown fences, test reports, long keyword lists, repeated text, or multiple alternative patches.
            """
        ).strip()
        contract = (
            "Return ONLY valid artifacts. First non-whitespace bytes must be one of: "
            "BEGIN_FILE:, BEGIN_SEARCH_REPLACE:, or diff --git. "
            "No JSON plans. No prose. No markdown fences. No repeated text."
        )
        return instruction, contract
    if "artifact_invalid" in modes or "non_artifact_output" in modes or "test_edit_attempt" in modes:
        instruction = textwrap.dedent(
            """
            Required output for this repair round:
            - FORMAT REPAIR MODE when previous output had invalid artifact form.
            - Preserve the previous semantic edit intent; do not redesign the solution.
            - Start with the artifact marker immediately. No prose, headings, test reports, or markdown fences.
            - Return exactly one valid artifact block unless multiple explicit writable files are required.
            - Do not edit read-only context files, especially tests supplied only as evidence.
            """
        ).strip()
        contract = (
            "Return ONLY valid artifacts. Preserve prior semantic intent. "
            "No prose. No markdown fences. Do not edit read-only tests."
        )
        return instruction, contract
    if "bad_json" in modes or "repeated_json" in modes:
        instruction = textwrap.dedent(
            """
            Required output for this repair round:
            - Do not return JSON artifacts.
            - Return exactly one BEGIN_FILE/END_FILE block for the affected writable file, or one valid minimal unified diff.
            - Use BEGIN_FILE for generated Python modules or tests when the edit is more than a few lines.
            - Do not return prose, test reports, markdown fences, or multiple alternative patches.
            """
        ).strip()
        contract = (
            "Return ONLY one BEGIN_FILE/END_FILE full file artifact, or one valid minimal unified diff. "
            "Do not return JSON artifacts. Do not return BEGIN_SEARCH_REPLACE. No prose. No verdict."
        )
        return instruction, contract
    return None, None
