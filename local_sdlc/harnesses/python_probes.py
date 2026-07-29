"""Deterministic Python mechanical probes for agent repair evidence."""

from __future__ import annotations

import ast
import json
import os
import re
import struct
import subprocess
from pathlib import Path
from typing import Sequence

from ..python_project_analysis import class_owner_paths_from_project, project_python_product_paths
from ..utils import unique_ordered
from .base import HarnessEvidence

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


PYTHON_PROBE_SEQUENCE = (
    ("struct", "python_struct", "Mechanical struct probe", python_struct_probe_document),
    ("api", "python_api", "Mechanical API probe", python_api_probe_document),
    ("precondition", "python_precondition", "Mechanical precondition probe", expected_exception_precondition_probe_document),
    ("cli", "python_cli", "Mechanical CLI probe", python_cli_probe_document),
    ("cli-state", "python_cli_state", "Mechanical CLI state probe", python_cli_state_probe_document),
    ("storage-state", "python_storage_state", "Mechanical storage state probe", python_storage_state_probe_document),
)


def mechanical_probe_status(document: str) -> tuple[str, int, str | None]:
    if re.search(r"(?m)^\s*-\s*status:\s*ERROR\s*$", document):
        return "fail", 1, "mechanical_probe_error"
    return "pass", 0, None


def mechanical_probe_evidence(slug: str, kind_slug: str, title: str, document: str) -> HarnessEvidence:
    status, exit_code, failure_type = mechanical_probe_status(document)
    return HarnessEvidence(
        kind="mechanical_probe",
        name=title,
        status=status,
        command=f"mechanical-probe {slug}",
        exit_code=exit_code,
        duration_seconds=0.0,
        document=document,
        failure_type=failure_type,
        covers=("mechanical_probe", kind_slug),
        observations={"slug": slug, "title": title},
    )


class PythonProbeHarness:
    """Run deterministic Python probes and return evidence records."""

    name = "python_probes"

    def run(
        self,
        project: Path | None,
        command_docs: Sequence[tuple[str, str]],
        timeout: float = 5.0,
    ) -> list[HarnessEvidence]:
        evidence: list[HarnessEvidence] = []
        working_docs = list(command_docs)
        for slug, kind_slug, title, probe in PYTHON_PROBE_SEQUENCE:
            if slug in {"cli-state", "storage-state"}:
                document = probe(project, working_docs, timeout)  # type: ignore[misc]
            else:
                document = probe(project, working_docs)  # type: ignore[misc]
            if not document:
                continue
            evidence_item = mechanical_probe_evidence(slug, kind_slug, title, document)
            evidence.append(evidence_item)
            working_docs.append((title, document))
        return evidence


def run_python_probe_evidence(
    project: Path | None,
    command_docs: Sequence[tuple[str, str]],
    timeout: float = 5.0,
) -> list[HarnessEvidence]:
    return PythonProbeHarness().run(project, command_docs, timeout)
