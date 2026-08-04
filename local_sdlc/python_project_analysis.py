"""Python project analysis helpers used by artifact repair logic."""

from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path
from typing import Sequence

from .utils import unique_ordered
from .workspace import normalize_project_relative_paths, resolve_project_path

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
        "test_oracle_review",
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
    stem = Path(normalized).stem.removeprefix("test_")
    existing_product_paths = [
        path
        for path in existing_paths
        if path.endswith(".py") and not path.startswith("tests/")
    ]
    if stem and existing_product_paths:
        exact = [path for path in existing_product_paths if Path(path).stem == stem]
        if exact:
            return unique_ordered(exact)
        compact_stem = compact_identifier(stem)
        lexical = [
            path
            for path in existing_product_paths
            if compact_identifier(Path(path).stem) == compact_stem
        ]
        if lexical:
            return unique_ordered(lexical)

    candidates = list(static_map.get(normalized, ()))
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


def project_relative_trace_path(
    project: Path | None,
    raw_path: str,
    existing_paths: Sequence[str] = (),
) -> str | None:
    """Map a traceback path from an isolated worktree back to the project."""
    normalized = raw_path.replace("\\", "/")
    candidates = list(existing_paths)
    if not candidates and project is not None:
        try:
            candidates = [
                str(path.relative_to(project))
                for path in project.rglob("*.py")
                if "/.sdlc-runner/" not in str(path)
            ]
        except OSError:
            candidates = []
    matches = [
        path
        for path in candidates
        if normalized == path or normalized.endswith("/" + path)
    ]
    if matches:
        return max(matches, key=len)
    if "/project/" in normalized:
        candidate = normalized.rsplit("/project/", 1)[1]
        if candidate and not candidate.startswith("."):
            return candidate
    if "/tests/" in normalized:
        return "tests/" + normalized.split("/tests/", 1)[1]
    return None
