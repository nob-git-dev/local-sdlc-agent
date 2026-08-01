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

from .repair_rules.domain import (
    native_struct_format_lines,
    repair_advice_from_command_docs,
    stage_test_paths_in_command_docs,
)
from .repair_rules.generic import (
    acceptance_gate_blockers_from_command_docs,
    repair_action_from_acceptance_blocker,
    repair_action_to_dict,
    repair_actions_from_advice,
    repair_advice_to_manifest,
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
