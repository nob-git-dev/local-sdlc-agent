"""Artifact lint, stream guards, and protocol repair checks."""

from __future__ import annotations

import ast as py_ast
from collections import Counter
import json
import re
import textwrap
from pathlib import Path
from typing import Any, Sequence

from .artifact_ops import *
from .artifact_protocol import *
from .models import *
from .python_project_analysis import *
from .utils import markdown_fenced_blocks, strip_markdown_fence, unique_ordered
from .workspace import normalize_project_relative_paths, read_text_if_exists, resolve_project_path


def python_declared_class_attributes(source: str) -> set[str]:
    """Return attributes declared directly in Python class bodies.

    Dataclass fields are annotations rather than ``self`` assignments, but
    generated properties may still reference them through ``self``.
    """
    try:
        tree = py_ast.parse(textwrap.dedent(source))
    except (IndentationError, SyntaxError):
        return set()

    attributes: set[str] = set()
    for node in py_ast.walk(tree):
        if not isinstance(node, py_ast.ClassDef):
            continue
        for statement in node.body:
            if isinstance(statement, py_ast.AnnAssign) and isinstance(statement.target, py_ast.Name):
                attributes.add(statement.target.id)
            elif isinstance(statement, py_ast.Assign):
                for target in statement.targets:
                    if isinstance(target, py_ast.Name):
                        attributes.add(target.id)
    return attributes


def python_class_defines_method(source: str, class_name: str, method_name: str) -> bool:
    """Return whether ``class_name`` directly defines ``method_name``."""
    try:
        tree = py_ast.parse(source)
    except (IndentationError, SyntaxError):
        return False
    for node in py_ast.walk(tree):
        if not isinstance(node, py_ast.ClassDef) or node.name != class_name:
            continue
        return any(
            isinstance(statement, (py_ast.FunctionDef, py_ast.AsyncFunctionDef))
            and statement.name == method_name
            for statement in node.body
        )
    return False


def python_receivers_bound_to_class(source: str, class_name: str) -> set[str]:
    """Infer simple local and ``self`` attribute bindings to a class.

    This deliberately handles only mechanically visible assignments and type
    annotations.  Unknown receiver types are left to the LLM and tests instead
    of being guessed by the lint layer.
    """
    identifier = r"[A-Za-z_][A-Za-z0-9_]*"
    qualified_class = rf"(?:{identifier}\.)*{re.escape(class_name)}"
    receivers = {
        match.group("receiver")
        for match in re.finditer(
            rf"(?<![A-Za-z0-9_.])(?P<receiver>(?:self\.)?{identifier})\s*"
            rf"(?:\:\s*{qualified_class}\s*)?=\s*{qualified_class}\s*\(",
            source,
        )
    }
    receivers.update(
        match.group("receiver")
        for match in re.finditer(
            rf"(?<![A-Za-z0-9_.])(?P<receiver>(?:self\.)?{identifier})\s*:\s*{qualified_class}\b",
            source,
        )
    )
    return receivers


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

        # Detect periodic multi-line loops such as a delimiter followed by the
        # same replacement body.  Consecutive-line checks miss these because
        # no individual line is adjacent to itself.
        max_block_width = min(16, len(lines) // 2)
        for width in range(2, max_block_width + 1):
            for start in range(0, len(lines) - (2 * width) + 1):
                block = lines[start : start + width]
                repeats = 1
                cursor = start + width
                while cursor + width <= len(lines) and lines[cursor : cursor + width] == block:
                    repeats += 1
                    cursor += width
                score = repeats * width
                if repeats >= 3 and score > max_score:
                    max_score = score
                    label = f"block_run:{truncate_text(' | '.join(block), 80)}"

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
    text = normalize_inline_file_artifact_headers(text)
    text = normalize_next_line_search_replace_headers(text)
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

    text = normalize_inline_file_artifact_headers(text)

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
    text = normalize_inline_file_artifact_headers(text)
    text = normalize_next_line_search_replace_headers(text)
    encoded_len = len(text.encode("utf-8"))
    artifact_offset = first_artifact_marker_offset(text)
    stripped = text.lstrip()
    if stripped.startswith("{"):
        first_key_match = re.match(r'^\{\s*"(?P<key>(?:\\.|[^"\\])*)"\s*:', stripped)
        if first_key_match:
            try:
                first_key = json.loads(f'"{first_key_match.group("key")}"')
            except json.JSONDecodeError:
                first_key = ""
            if first_key not in {"artifacts", "type"}:
                return ArtifactStreamGuardResult(
                    should_abort=True,
                    reason=(
                        f"unsupported top-level JSON key {first_key!r}; "
                        "artifact JSON must begin with `artifacts` or `type`"
                    ),
                    code="stream_json_schema_mismatch",
                    score=1,
                    threshold=0,
                )
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
    repeated_score, repeated_label = repeated_text_run_score(text)
    if (
        has_streamed_search_replace
        and repeated_label.startswith("block_run:")
        and repeated_score >= repeated_text_threshold
    ):
        return ArtifactStreamGuardResult(
            should_abort=True,
            reason=f"repeated text runaway detected while streaming: {repeated_label} repeated score={repeated_score}",
            code="stream_repeated_text_runaway",
            score=repeated_score,
            threshold=repeated_text_threshold,
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
    if repeated_score >= repeated_text_threshold:
        return ArtifactStreamGuardResult(
            should_abort=True,
            reason=f"repeated text runaway detected while streaming: {repeated_label} repeated score={repeated_score}",
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
    if trigger == "test_harness_ownership" and project is not None:
        owned_tests = [
            path
            for path in unique_ordered(stage_generated_test_paths)
            if path.startswith("tests/")
        ]
        missing_tests = [
            path
            for path in owned_tests
            if not resolve_project_path(project, path).is_file()
            and (
                f"required path missing: {path}" in evidence_doc
                or path in evidence_doc
            )
        ]
        zero_test_evidence = (
            "verification infrastructure: unittest command discovered zero tests"
            in evidence_doc.lower()
            or "no tests ran" in evidence_doc.lower()
        )
        if missing_tests and zero_test_evidence:
            return {
                "trigger": trigger,
                "case_type": "test_harness",
                "confidence": "high",
                "project_policy_basis": [
                    "The test path is declared as stage-owned generated output.",
                    "The path is absent on disk and configured discovery executed zero tests.",
                ],
                "safe_next_action": "edit_test_harness",
                "editable_paths": missing_tests,
                "readonly_paths": [],
                "forbidden_actions": [
                    "Do not edit fixed acceptance tests.",
                    "Do not grant write access to undeclared test paths.",
                ],
                "rationale": (
                    "Creating an absent, declared stage-owned test harness satisfies a machine-owned "
                    "stage obligation; it does not alter an existing test oracle."
                ),
            }
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
    valid_search_replace = any(
        VALID_SEARCH_REPLACE_PATTERN.match(candidate.strip())
        for candidate in artifact_candidate_texts(text)
    )
    valid_fenced_search_replace = bool(
        fenced_conflict_search_replace_artifacts(text, ArtifactPathPolicy(allowed_paths=(), allow_extra_new_files=True))
    )
    valid_unified_diff = stripped.startswith("diff --git ") and len(re.findall(r"(?m)^diff --git\s+a/[^\s]+\s+b/[^\s]+", stripped)) == 1

    if MALFORMED_SEARCH_REPLACE_WITHOUT_PATH_PATTERN.search(text) and not valid_search_replace:
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
    if malformed_search_replace_full_file_artifacts(
        text,
        ArtifactPathPolicy(allowed_paths=(), allow_extra_new_files=True),
    ):
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
    recoverable_malformed_full_file = bool(
        malformed_search_replace_full_file_artifacts(
            text,
            ArtifactPathPolicy(allowed_paths=(), allow_extra_new_files=True),
        )
    )
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
        and not recoverable_malformed_full_file
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
            if not isinstance(item, dict):
                continue
            artifact_type = str(item.get("type", "")).strip()
            path = str(item.get("path", "")).strip() or None
            if artifact_type in {"replace_file", "file"}:
                content = item.get("content")
                if project is not None and path and isinstance(content, str):
                    target = resolve_project_path(project, normalize_legacy_file_artifact_path(path))
                    if target.is_file():
                        current = read_text_if_exists(target)
                        normalized_current = current.replace("\r\n", "\n").rstrip("\n")
                        normalized_content = content.replace("\r\n", "\n").rstrip("\n")
                        if normalized_current == normalized_content:
                            findings.append(
                                ArtifactLintFinding(
                                    severity="error",
                                    code="unchanged_replace_file",
                                    message=(
                                        "whole-file replacement is unchanged from the current file; "
                                        "emit a real behavioral change or MISSING_CONTEXT"
                                    ),
                                    path=path,
                                )
                            )
                continue
            if artifact_type != "search_replace":
                continue
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
        block_declared_attrs_by_path: dict[str, set[str]] = {}
        block_defined_methods_by_path: dict[str, set[str]] = {}
        for path, block in generated_blocks:
            if not path:
                continue
            normalized_path = normalize_legacy_file_artifact_path(path)
            block_assignments_by_path.setdefault(normalized_path, set()).update(
                re.findall(r"\bself\.([A-Za-z_][A-Za-z0-9_]*)\s*=", block)
            )
            block_declared_attrs_by_path.setdefault(normalized_path, set()).update(
                python_declared_class_attributes(block)
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
            known_attrs.update(python_declared_class_attributes(source))
            known_methods = set(re.findall(r"(?m)^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", source))
            assigned_attrs = block_assignments_by_path.get(normalized_path, set())
            assigned_attrs.update(block_declared_attrs_by_path.get(normalized_path, set()))
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
    mechanical_absent_facts = mechanically_absent_api_facts_from_texts(forbidden_actions)
    if project is not None and mechanical_absent_facts and generated_blocks:
        generated_by_path: dict[str, list[str]] = {}
        for path, block in generated_blocks:
            if path:
                generated_by_path.setdefault(
                    normalize_legacy_file_artifact_path(path), []
                ).append(block)
        reported_unresolved_calls: set[tuple[str, str, str]] = set()
        for class_name, attr, owner_path in mechanical_absent_facts:
            normalized_owner = (
                normalize_legacy_file_artifact_path(owner_path)
                if owner_path
                else None
            )
            owner_source = ""
            if normalized_owner:
                owner_target = resolve_project_path(project, normalized_owner)
                if owner_target.exists() and owner_target.is_file():
                    owner_source = owner_target.read_text(encoding="utf-8", errors="replace")
            owner_defines_method = python_class_defines_method(
                owner_source,
                class_name,
                attr,
            )
            candidate_defines_method = bool(
                normalized_owner
                and any(
                    re.search(rf"(?m)^\s*def\s+{re.escape(attr)}\s*\(", block)
                    for block in generated_by_path.get(normalized_owner, [])
                )
            )
            if owner_defines_method or candidate_defines_method:
                continue
            for path, block in generated_blocks:
                if not path or not path.endswith(".py"):
                    continue
                normalized_path = normalize_legacy_file_artifact_path(path)
                target = resolve_project_path(project, normalized_path)
                source = (
                    target.read_text(encoding="utf-8", errors="replace")
                    if target.exists() and target.is_file()
                    else ""
                )
                receivers = python_receivers_bound_to_class(
                    source + "\n" + block,
                    class_name,
                )
                if normalized_owner and normalized_path == normalized_owner:
                    receivers.add("self")
                called_receivers = {
                    match.group("receiver")
                    for match in re.finditer(
                        rf"(?<![A-Za-z0-9_.])(?P<receiver>(?:self\.)?[A-Za-z_][A-Za-z0-9_]*)\."
                        rf"{re.escape(attr)}\s*\(",
                        block,
                    )
                }
                direct_constructor_call = bool(
                    re.search(
                        rf"\b(?:[A-Za-z_][A-Za-z0-9_]*\.)*{re.escape(class_name)}\s*\([^\n]*\)\."
                        rf"{re.escape(attr)}\s*\(",
                        block,
                    )
                )
                if not direct_constructor_call and not (called_receivers & receivers):
                    continue
                key = (normalized_path, class_name, attr)
                if key in reported_unresolved_calls:
                    continue
                reported_unresolved_calls.add(key)
                findings.append(
                    ArtifactLintFinding(
                        severity="error",
                        code="unresolved_absent_api_call",
                        message=(
                            f"artifact calls mechanically absent API `{class_name}.{attr}` through a "
                            f"receiver visibly bound to `{class_name}`, but neither the current owner "
                            "nor this artifact transaction defines that method"
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
        fixture_match = next(
            (
                (path, block)
                for path, block in generated_blocks
                if re.search(r"\btmp_path\b", block)
                and (
                    str(path or "").startswith("tests/")
                    or block_looks_like_test_harness(block)
                )
            ),
            None,
        )
        if fixture_match is not None:
            bad_path, _block = fixture_match
            findings.append(
                ArtifactLintFinding(
                    severity="error",
                    code="pytest_fixture",
                    message=(
                        "unittest command is configured, but the generated test artifact appears "
                        "to depend on a pytest fixture"
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
            if search.rstrip() == replace.rstrip():
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
    codes = [finding.code for finding in findings if finding.severity == "error"]
    if "identical_search_replace" in codes:
        return "candidate_no_effect"
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
        "unresolved_absent_api_call",
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
        "search_replace_conflict_markers",
        "unbalanced_file_artifact",
    ]
    for code in priority:
        if code in codes:
            if code == "search_replace_conflict_markers":
                return "artifact_invalid"
            if code == "unbalanced_file_artifact":
                return "artifact_invalid"
            return code
    return default
