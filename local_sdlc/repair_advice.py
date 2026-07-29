"""Repair advice and deterministic repair artifact helpers."""

from __future__ import annotations

import ast
import json
import re
import textwrap
from pathlib import Path
from typing import Sequence

from .artifact_ops import *
from .models import *
from .python_project_analysis import *
from .utils import unique_ordered
from .verification import parse_command_result_document
from .workspace import resolve_project_path

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
