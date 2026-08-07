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


_NEXT_LINE_SEARCH_REPLACE_HEADER = re.compile(
    r"(?m)^(?P<indent>[ \t]*)BEGIN_SEARCH_REPLACE[ \t]*\n"
    r"[ \t]*(?P<path>[A-Za-z0-9_.][A-Za-z0-9_./-]*)[ \t]*\n"
    r"(?=[ \t]*<<<<<<< SEARCH[ \t]*$)"
)


def normalize_next_line_search_replace_headers(text: str) -> str:
    """Join an unambiguous next-line path to its search/replace marker.

    Recovery is limited to one conservative path token immediately followed by
    the canonical SEARCH marker.  The ordinary artifact path policy still
    decides whether that path is writable; this function changes no payload.
    """

    def replace(match: re.Match[str]) -> str:
        raw_path = match.group("path")
        try:
            paths = normalize_project_relative_paths(
                [raw_path],
                "search/replace envelope path",
            )
        except RunnerError:
            return match.group(0)
        if len(paths) != 1:
            return match.group(0)
        return (
            f"{match.group('indent')}BEGIN_SEARCH_REPLACE: {paths[0]}\n"
        )

    return _NEXT_LINE_SEARCH_REPLACE_HEADER.sub(replace, text)


def normalize_terminal_end_search_replace_artifact(text: str) -> str:
    """Recover an unambiguous block that omits ``>>>>>>> REPLACE``.

    Some models use ``END_SEARCH_REPLACE`` as both the conflict-pair terminator
    and the outer block terminator.  Recovery is safe only when the response
    contains exactly one path header, one SEARCH marker, one separator, no
    standard REPLACE marker, and a single terminal END marker.  The payload is
    preserved verbatim; only the missing envelope marker is inserted.
    """
    if re.search(rf"(?m)^\s*{SEARCH_REPLACE_END_MARKER}\s*$", text):
        return text
    if len(re.findall(r"(?m)^\s*BEGIN_SEARCH_REPLACE:\s*[^\n]+$", text)) != 1:
        return text
    if len(re.findall(r"(?m)^\s*<<<<<<< SEARCH\s*$", text)) != 1:
        return text
    if len(re.findall(r"(?m)^\s*=======\s*$", text)) != 1:
        return text
    if len(re.findall(r"(?m)^\s*END_SEARCH_REPLACE\s*$", text)) != 1:
        return text
    pattern = re.compile(
        r"(?ms)^(?P<body>\s*BEGIN_SEARCH_REPLACE:\s*[^\n]+\n"
        r"\s*<<<<<<< SEARCH\n.*?\n\s*=======\n.*?)\n"
        r"\s*END_SEARCH_REPLACE\s*$"
    )
    match = pattern.match(text)
    if not match:
        return text
    return match.group("body") + "\n>>>>>>> REPLACE\nEND_SEARCH_REPLACE"


def normalize_short_search_replace_start_markers(text: str) -> str:
    """Normalize a five-chevron SEARCH marker in an otherwise exact envelope.

    This changes syntax only. Each path-qualified block must contain exactly one
    short SEARCH marker, one separator, one canonical REPLACE marker, and no
    competing conflict-marker line. Ambiguous or incomplete blocks are left for
    the normal artifact rejection path.
    """
    block_pattern = re.compile(
        r"(?ms)^(?P<header>\s*BEGIN_SEARCH_REPLACE:\s*[^\n]+\n)"
        r"(?P<body>.*?)"
        r"(?P<footer>^\s*END_SEARCH_REPLACE\s*$)"
    )

    def replace_block(match: re.Match[str]) -> str:
        body = match.group("body")
        short_markers = list(re.finditer(r"(?m)^(?P<indent>[ \t]*)<<<<< SEARCH[ \t]*$", body))
        conflict_lines = re.findall(
            r"(?m)^[ \t]*(?:<{4,}[ \t]+SEARCH|={4,}|>{4,}[ \t]+REPLACE)[ \t]*$",
            body,
        )
        if (
            len(short_markers) != 1
            or len(re.findall(r"(?m)^[ \t]*=======[ \t]*$", body)) != 1
            or len(re.findall(r"(?m)^[ \t]*>>>>>>> REPLACE[ \t]*$", body)) != 1
            or len(conflict_lines) != 3
        ):
            return match.group(0)
        marker = short_markers[0]
        normalized_body = (
            body[: marker.start()]
            + marker.group("indent")
            + "<<<<<<< SEARCH"
            + body[marker.end() :]
        )
        return match.group("header") + normalized_body + match.group("footer")

    return block_pattern.sub(replace_block, text)


def artifact_candidate_texts(text: str) -> list[str]:
    normalized_file_headers = normalize_inline_file_artifact_headers(text)
    normalized_next_line_headers = normalize_next_line_search_replace_headers(
        normalized_file_headers
    )
    normalized_short_search_markers = normalize_short_search_replace_start_markers(
        normalized_next_line_headers
    )
    normalized_search_replace = normalize_file_header_search_replace_artifacts(
        normalized_short_search_markers
    )
    normalized_terminal = normalize_terminal_end_search_replace_artifact(normalized_search_replace)
    candidates = [
        text,
        normalized_file_headers,
        normalized_next_line_headers,
        normalized_short_search_markers,
        normalized_search_replace,
        normalized_terminal,
        strip_markdown_fence(text),
        strip_markdown_fence(normalized_search_replace),
        strip_markdown_fence(normalized_terminal),
    ]
    candidates.extend(markdown_fenced_blocks(text))
    candidates.extend(markdown_fenced_blocks(normalized_search_replace))
    candidates.extend(markdown_fenced_blocks(normalized_terminal))
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
            r"(?m)^(BEGIN_(?:APPEND_)?FILE(?:\s*:|[ \t]+[A-Za-z0-9_.][A-Za-z0-9_./-]*[ \t]*$|\s*$)|BEGIN_SEARCH_REPLACE:|END_(?:APPEND_)?FILE(?:\s*:\s*[^\n]+)?\s*$|END_SEARCH_REPLACE$)",
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


def mechanically_absent_api_facts_from_texts(
    texts: Sequence[str],
) -> list[tuple[str, str, str | None]]:
    """Extract mechanically observed absent APIs and their owner paths.

    Unlike :func:`absent_api_contracts_from_texts`, this function does not
    decide whether restoring an API is allowed.  It records only the probe
    fact needed to validate a candidate transaction: a call to an absent API
    is unresolved unless the current project or the same transaction defines
    that method on its owner class.
    """
    facts: list[tuple[str, str, str | None]] = []
    pending: list[tuple[str, str]] = []
    owner_paths: list[str] = []
    for text in texts:
        if not text:
            continue
        for class_name, attr, owner_path in re.findall(
            r"`([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)`\s+is absent\s+from\s+`([^`]+)`",
            text,
        ):
            facts.append((class_name, attr, normalize_legacy_file_artifact_path(owner_path)))
        if re.search(r"(?i)\babsent api(?:\s+from\s+mechanical\s+probe)?\b", text):
            pending.extend(
                (class_name, attr)
                for class_name, attr in re.findall(
                    r"\b([A-Z][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b",
                    text,
                )
            )
        owner_paths.extend(
            normalize_legacy_file_artifact_path(path)
            for path in re.findall(
                r"(?im)^\s*-?\s*(?:api surface probed from|source file|owner path):\s*`?([^`\n]+?)`?\s*$",
                text,
            )
        )

    known_pairs = {(class_name, attr) for class_name, attr, _path in facts}
    default_owner = unique_ordered(path for path in owner_paths if path)
    for class_name, attr in dict.fromkeys(pending):
        if (class_name, attr) in known_pairs:
            continue
        owner_path = default_owner[0] if len(default_owner) == 1 else None
        facts.append((class_name, attr, owner_path))
    return list(dict.fromkeys(facts))

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
        # A forbidden path constrains the artifact target, not every identifier
        # appearing inside a valid edit.  Remove path-shaped targets before
        # extracting symbols so ``pkg/checkpoint.py`` does not accidentally
        # forbid imports containing ``pkg`` or ``checkpoint``.
        targets = re.sub(
            r"`?(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]*`?",
            " ",
            targets,
        )
        for token in re.findall(r"`?([A-Za-z_][A-Za-z0-9_]*)`?", targets):
            if token.lower() in stopwords:
                continue
            symbols.append(token)
    return unique_ordered(symbols)


def forbidden_edit_paths_from_texts(texts: Sequence[str]) -> list[str]:
    """Extract project-relative file or directory targets from ``do not edit`` advice."""
    paths: list[str] = []
    combined = "\n".join(text for text in texts if text)
    for match in re.finditer(r"(?i)\bdo not edit\s+(?P<targets>[^\n.]+(?:\.[A-Za-z0-9_-]+)?)", combined):
        targets = match.group("targets")
        for raw_path in re.findall(
            r"`?((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]*)`?",
            targets,
        ):
            normalized = normalize_legacy_file_artifact_path(raw_path)
            if normalized and normalized not in {".", ".."} and not normalized.startswith("../"):
                paths.append(normalized)
    return unique_ordered(paths)

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


_INLINE_FILE_HEADER_WITHOUT_COLON = re.compile(
    r"(?m)^(?P<header>BEGIN_(?:APPEND_)?FILE)[ \t]+"
    r"(?P<path>[A-Za-z0-9_.][A-Za-z0-9_./-]*)[ \t]*$"
)


def normalize_inline_file_artifact_headers(text: str) -> str:
    """Insert a missing colon in an otherwise unambiguous file header.

    Only the reserved marker plus one conservative project-relative path is
    normalized. Path authorization remains the responsibility of the normal
    artifact policy, and content is never changed by this repair.
    """

    return _INLINE_FILE_HEADER_WITHOUT_COLON.sub(
        lambda match: f"{match.group('header')}: {match.group('path')}",
        text,
    )

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
    return len(
        re.findall(
            r"(?m)^BEGIN_(?:APPEND_)?FILE(?:\s*:|[ \t]+[A-Za-z0-9_.][A-Za-z0-9_./-]*[ \t]*$|\s*$)",
            text,
        )
    )

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
    for base in [candidate, *list(repairs)]:
        completed = complete_json_terminal_closers(base)
        if completed is None:
            continue
        repairs.append(completed)
        completed_without_trailing_commas = remove_json_trailing_commas(completed)
        if completed_without_trailing_commas != completed:
            repairs.append(completed_without_trailing_commas)
    return unique_ordered(repairs)


def complete_json_terminal_closers(candidate: str, max_insertions: int = 4) -> str | None:
    """Repair only unambiguous missing JSON container closers.

    The function never invents values, keys, quotes, commas, or string bytes.
    It may insert ``]``/``}`` before an already present ancestor closer or at
    end-of-input when the lexer is outside a string.  Any extra/mismatched
    closer or truncated string remains invalid and is rejected by the caller.
    """
    stack: list[str] = []
    result: list[str] = []
    in_string = False
    escaped = False
    insertions = 0
    closing_for = {"{": "}", "[": "]"}

    for char in candidate:
        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            result.append(char)
            continue
        if char in closing_for:
            stack.append(char)
            result.append(char)
            continue
        if char in "}]":
            matching_index = next(
                (
                    index
                    for index in range(len(stack) - 1, -1, -1)
                    if closing_for[stack[index]] == char
                ),
                None,
            )
            if matching_index is None:
                return None
            while len(stack) - 1 > matching_index:
                result.append(closing_for[stack.pop()])
                insertions += 1
            stack.pop()
            result.append(char)
            continue
        result.append(char)

    if in_string or escaped:
        return None
    while stack:
        result.append(closing_for[stack.pop()])
        insertions += 1
    if insertions == 0 or insertions > max_insertions:
        return None
    return "".join(result)

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
    existing_paths: Sequence[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Move existing tests from writable targets to readonly evidence targets.

    A declared test path that does not exist is still an implementation
    obligation. Freezing it before the harness has been created makes the
    obligation impossible to satisfy after a rolled-back candidate. Callers
    that provide ``existing_paths`` therefore retain missing tests as writable
    until one has actually been materialized.
    """
    existing = set(existing_paths) if existing_paths is not None else None
    frozen = [
        path
        for path in allowed_paths
        if path.startswith("tests/") and (existing is None or path in existing)
    ]
    frozen_set = set(frozen)
    allowed = [path for path in allowed_paths if path not in frozen_set]
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
            path = normalize_legacy_file_artifact_path(match.group("path"))
            duplicate_path = normalize_legacy_file_artifact_path(match.group("duplicate_path") or "")
            if not path:
                continue
            if duplicate_path and duplicate_path != path:
                continue
            terminal = re.search(
                r"\n\s*END_SEARCH_REPLACE(?:\s*:\s*(?P<path>[^\n]*))?\s*\Z",
                body,
                flags=re.MULTILINE,
            )
            if terminal:
                terminal_path = normalize_legacy_file_artifact_path(terminal.group("path") or "")
                if terminal_path and terminal_path != path:
                    continue
                body = body[: terminal.start()]
            if contains_conflict_markers(body) or "=======" in body or "Replace with:" in body:
                continue
            check_artifact_path(path, policy, "malformed search/replace full-file recovery")
            if not path.endswith(".py"):
                continue
            content = normalize_legacy_file_artifact_content(strip_markdown_fence(body)).rstrip() + "\n"
            if contains_artifact_markers(content):
                continue
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
            if not path:
                continue
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
            if not path:
                continue
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
            if not path:
                continue
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



__all__ = [name for name in globals() if not name.startswith("__")]
