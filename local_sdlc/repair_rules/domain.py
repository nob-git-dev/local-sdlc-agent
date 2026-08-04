"""Domain-aware repair heuristics isolated from the generic runner."""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Sequence

from ..artifact_ops import *
from ..models import RepairAdvice, RunnerError
from ..python_project_analysis import *
from ..utils import unique_ordered
from .generic import acceptance_gate_blockers_from_command_docs

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


def unittest_discovery_test_paths(test_commands: Sequence[str]) -> list[str]:
    """Return concrete test files named by unittest discovery commands."""
    paths: list[str] = []
    for command in test_commands:
        try:
            tokens = shlex.split(command)
        except ValueError:
            continue
        try:
            start = tokens[tokens.index("-s") + 1]
            pattern = tokens[tokens.index("-p") + 1]
        except (ValueError, IndexError):
            continue
        if not pattern.endswith(".py") or any(character in pattern for character in "*?[]"):
            continue
        paths.append(str(Path(start) / pattern))
    try:
        return normalize_project_relative_paths(paths)
    except RunnerError:
        return []



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
    existing_product_paths = project_python_product_paths(project)
    existing_python_paths = list(existing_product_paths)
    if project is not None:
        try:
            existing_python_paths.extend(
                str(path.relative_to(project))
                for path in project.rglob("*.py")
                if "/.sdlc-runner/" not in str(path)
            )
        except OSError:
            pass
    existing_python_paths = unique_ordered(existing_python_paths)

    for raw_path in re.findall(r'File "([^"]+)", line \d+', combined):
        relative = project_relative_trace_path(project, raw_path, existing_python_paths)
        if relative:
            focus_files.append(relative)

    test_focus_files = unique_ordered(path for path in focus_files if path.startswith("tests/"))
    inferred_stage_focus = unique_ordered(
        product_path
        for test_path in test_focus_files
        for product_path in inferred_product_focus_from_test_path(
            test_path,
            existing_product_paths,
        )
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
    acceptance_blockers = acceptance_gate_blockers_from_command_docs(command_docs)
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
        module_path = module_name_to_project_path(module_name, project)
        existing_symbols = python_defined_symbols(project, module_path)
        package_export_owners: list[str] = []
        if project is not None and module_path.endswith("/__init__.py"):
            package_dir = Path(module_path).parent
            try:
                package_export_owners = unique_ordered(
                    str(path.relative_to(project))
                    for path in (project / package_dir).glob("*.py")
                    if path.name != "__init__.py"
                    and missing_symbol in python_defined_symbols(project, str(path.relative_to(project)))
                )
            except (OSError, ValueError):
                package_export_owners = []
        importing_paths = unique_ordered([*generated_test_focus, *inferred_stage_focus, *product_trace_focus])
        projections = import_api_alias_projections(
            project,
            importing_paths,
            module_name,
            existing_symbols,
        )
        aliases = likely_symbol_aliases(missing_symbol, existing_symbols)
        same_stage_import_contract = bool(package_export_owners) or module_path in set(
            [*inferred_stage_focus, *product_trace_focus]
        )
        if same_stage_import_contract:
            strategy = "root_cause_patch"
            focus_files.extend(
                [module_path, *package_export_owners, *inferred_stage_focus, *product_trace_focus]
            )
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
        focus_files.extend(
            unique_ordered(
                [*generated_test_focus, *unittest_discovery_test_paths(test_commands)]
            )
        )
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
        focus_files.extend(
            unique_ordered(
                [*generated_test_focus, *unittest_discovery_test_paths(test_commands)]
            )
        )
        instructions.extend(
            [
                "Create only the stage-authorized unittest file paths; their parent directory will be created during artifact application.",
                "Do not invent package initializer or benchmark-specific test names that are absent from the writable stage contract.",
                "Add focused tests that encode the completed stage contracts and run with the configured unittest discovery command.",
                "Keep tests dependency-free; do not use pytest fixtures or pytest-only assertions unless the configured command uses pytest.",
            ]
        )
        evidence.append("unittest discovery start directory did not exist before the authorized test artifact was created")

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

    contract_lines = re.findall(r"(?m)^-\s+(C\d+)\s+\[([^\]]+)\]:\s*(.+)$", combined)
    authoritative_contract_lines = [
        (contract_id, contract_text)
        for contract_id, kind, contract_text in contract_lines
        if kind != "provisional_test_oracle"
    ]
    provisional_contract_lines = [
        (contract_id, contract_text)
        for contract_id, kind, contract_text in contract_lines
        if kind == "provisional_test_oracle"
    ]
    if authoritative_contract_lines:
        if strategy == "small_patch":
            strategy = "semantic_contract_patch"
        if strategy not in TEST_HARNESS_WRITE_STRATEGIES:
            for contract_id, contract_text in authoritative_contract_lines[:5]:
                instructions.append(f"Preserve semantic contract {contract_id}: {contract_text.strip()}")
            evidence.append("semantic contracts extracted from executable test evidence")
    if provisional_contract_lines and not authoritative_contract_lines and strategy == "small_patch":
        strategy = "test_oracle_review"
        instructions.extend(
            [
                "A stage-owned generated test assertion is a provisional oracle, not a fixed product contract.",
                "Classify the assertion against SPEC.md and its complete setup/action sequence before editing product code or the test.",
                "Only the project-policy action gate may authorize an edit to a machine-verified stage-owned test path.",
            ]
        )
        evidence.extend(
            f"provisional generated-test proposition {contract_id}: {contract_text.strip()}"
            for contract_id, contract_text in provisional_contract_lines[:5]
        )

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
        for product_path in inferred_product_focus_from_test_path(
            test_path,
            existing_product_paths,
        )
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
