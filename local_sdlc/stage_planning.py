"""Generic, machine-validated stage-plan contracts and stage splitting."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Mapping, Sequence

from .llm_client import parse_api_profile_override
from .models import RunnerError, StageWorkItem
from .utils import markdown_fenced_blocks, unique_ordered
from .workspace import normalize_project_relative_paths


STAGE_PLAN_SCHEMA_VERSION = 1
STAGE_PLAN_HEADING_RE = re.compile(
    r"(?im)^#{2,3}\s+(?:implementation\s+stages?|stage\s+plan|実装ステージ|実装段階)\s*$"
)
VERIFICATION_HEADING_TOKENS = (
    "verification commands",
    "verification method",
    "test commands",
    "executable verification",
    "検証コマンド",
    "検証方法",
    "テストコマンド",
)
COMMAND_PREFIXES = (
    "python ",
    "python3 ",
    "pytest ",
    "uv ",
    "node ",
    "npm ",
    "npx ",
    "deno ",
    "cargo ",
    "go ",
    "make",
    "bash ",
    "sh ",
    "curl ",
)


def _string_list(value: object, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RunnerError(f"stage-plan {field_name} must be a list of strings")
    return unique_ordered(item.strip() for item in value if item.strip())


def _api_profile_list(value: object, field_name: str) -> list[str]:
    overrides = _string_list(value, field_name)
    for override in overrides:
        try:
            parse_api_profile_override(override)
        except (RunnerError, TypeError, ValueError) as exc:
            raise RunnerError(
                f"stage-plan {field_name} contains an invalid function override: {override!r}: {exc}"
            ) from exc
    return overrides


def _marked_stage_plan_payload(spec: str) -> Mapping[str, object] | None:
    has_heading = bool(STAGE_PLAN_HEADING_RE.search(spec))
    marked_text_seen = "stage_plan_schema" in spec
    parse_errors: list[str] = []
    for block in markdown_fenced_blocks(spec):
        if "stage_plan_schema" not in block:
            continue
        try:
            payload = json.loads(block)
        except json.JSONDecodeError as exc:
            parse_errors.append(str(exc))
            continue
        if not isinstance(payload, Mapping):
            raise RunnerError("stage-plan contract must be a JSON object")
        return payload
    if marked_text_seen or has_heading:
        detail = f": {parse_errors[-1]}" if parse_errors else ""
        raise RunnerError("Implementation Stages requires one valid stage-plan JSON contract" + detail)
    return None


def stage_plan_from_spec(spec: str) -> list[StageWorkItem] | None:
    payload = _marked_stage_plan_payload(spec)
    if payload is None:
        return None
    try:
        schema_version = int(payload.get("stage_plan_schema", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise RunnerError("stage_plan_schema must be an integer") from exc
    if schema_version != STAGE_PLAN_SCHEMA_VERSION:
        raise RunnerError(f"unsupported stage_plan_schema: {schema_version}")
    raw_stages = payload.get("stages")
    if not isinstance(raw_stages, list) or not raw_stages:
        raise RunnerError("stage-plan stages must be a non-empty list")
    if len(raw_stages) > 32:
        raise RunnerError("stage-plan contains more than 32 stages")

    stages: list[StageWorkItem] = []
    for index, raw in enumerate(raw_stages, start=1):
        if not isinstance(raw, Mapping):
            raise RunnerError(f"stage-plan stage {index} must be an object")
        expected_id = f"S{index:02d}"
        stage_id = str(raw.get("stage_id") or "").strip().upper()
        if stage_id != expected_id:
            raise RunnerError(
                f"stage-plan stage ids must be ordered and contiguous; expected {expected_id}, got {stage_id or '(missing)'}"
            )
        title = str(raw.get("title") or "").strip()
        goal = str(raw.get("goal") or "").strip()
        if not title or not goal:
            raise RunnerError(f"stage-plan {stage_id} requires title and goal")
        if len(title) > 120 or len(goal) > 1000:
            raise RunnerError(f"stage-plan {stage_id} title or goal is too long")

        writable = normalize_project_relative_paths(
            _string_list(raw.get("writable_paths"), f"{stage_id}.writable_paths"),
            f"{stage_id} writable path",
        )
        if not writable:
            raise RunnerError(f"stage-plan {stage_id} requires at least one writable path")
        readonly = normalize_project_relative_paths(
            _string_list(
                raw.get("readonly_evidence_paths"),
                f"{stage_id}.readonly_evidence_paths",
            ),
            f"{stage_id} readonly evidence path",
        )
        overlap = sorted(set(writable) & set(readonly))
        if overlap:
            raise RunnerError(
                f"stage-plan {stage_id} paths cannot be both writable and readonly: {', '.join(overlap)}"
            )
        test_commands = _string_list(raw.get("test_commands"), f"{stage_id}.test_commands")
        observables = _string_list(
            raw.get("required_observables"),
            f"{stage_id}.required_observables",
        )
        test_focus = _string_list(raw.get("test_focus"), f"{stage_id}.test_focus")
        api_profile = _api_profile_list(raw.get("api_profile"), f"{stage_id}.api_profile")
        max_rounds_raw = raw.get("max_rounds")
        max_rounds: int | None = None
        if max_rounds_raw is not None:
            try:
                max_rounds = int(max_rounds_raw)
            except (TypeError, ValueError) as exc:
                raise RunnerError(f"stage-plan {stage_id}.max_rounds must be an integer") from exc
            if max_rounds < 1 or max_rounds > 20:
                raise RunnerError(f"stage-plan {stage_id}.max_rounds must be between 1 and 20")
        stages.append(
            StageWorkItem(
                stage_id=stage_id,
                title=title,
                goal=goal,
                suggested_paths=tuple(writable),
                test_focus=tuple(test_focus or observables or test_commands or [title]),
                test_commands=tuple(test_commands),
                required_observables=tuple(observables),
                writable_paths=tuple(writable),
                repair_scope_paths=tuple(writable),
                readonly_evidence_paths=tuple(readonly),
                api_profile=tuple(api_profile),
                max_rounds=max_rounds,
            )
        )
    return stages


def _is_test_path(path: str) -> bool:
    candidate = path.replace("\\", "/")
    return candidate.startswith("tests/") or Path(candidate).name.startswith("test_")


def _path_affinity(left: str, right: str) -> int:
    """Return a lexical ownership score without assuming a package name."""
    left_stem = Path(left).stem.removeprefix("test_").lower()
    right_stem = Path(right).stem.removeprefix("test_").lower()
    if left_stem == right_stem:
        return 100
    left_tokens = set(filter(None, re.split(r"[^a-z0-9]+", left_stem)))
    right_tokens = set(filter(None, re.split(r"[^a-z0-9]+", right_stem)))
    return 10 * len(left_tokens & right_tokens)


def _cohesive_path_groups(paths: Sequence[str]) -> list[list[str]]:
    """Keep conventional tests beside the product path they exercise."""
    product_paths = [path for path in paths if not _is_test_path(path)]
    test_paths = [path for path in paths if _is_test_path(path)]
    if not product_paths or not test_paths:
        return [[path] for path in paths]

    groups = [[path] for path in product_paths]
    unmatched_tests: list[str] = []
    for test_path in test_paths:
        scores = [_path_affinity(test_path, product_path) for product_path in product_paths]
        best_score = max(scores, default=0)
        if best_score <= 0:
            unmatched_tests.append(test_path)
            continue
        groups[scores.index(best_score)].append(test_path)
    if unmatched_tests:
        groups[-1].extend(unmatched_tests)
    return groups


def _balanced_group_chunks(groups: Sequence[Sequence[str]], parts: int) -> list[list[str]]:
    chunk_count = max(1, min(int(parts), len(groups)))
    chunks: list[list[str]] = [[] for _ in range(chunk_count)]
    loads = [0] * chunk_count
    for group in groups:
        target = min(range(chunk_count), key=lambda index: (loads[index], index))
        chunks[target].extend(group)
        loads[target] += len(group)
    return [chunk for chunk in chunks if chunk]


def split_stage_work_item(stage: StageWorkItem, *, parts: int = 2) -> list[StageWorkItem]:
    paths = list(stage.writable_paths or stage.suggested_paths)
    if len(paths) < 2:
        return [stage]
    normalized_parts = max(2, min(int(parts), len(paths)))
    groups = _cohesive_path_groups(paths)
    chunks = _balanced_group_chunks(groups, normalized_parts)
    repair_scope = tuple(stage.repair_scope_paths or paths)
    children: list[StageWorkItem] = []
    for index, chunk in enumerate(chunks, start=1):
        is_last = index == len(chunks)
        children.append(
            StageWorkItem(
                stage_id=f"{stage.stage_id}.{index}",
                title=f"{stage.title} slice {index}/{len(chunks)}",
                goal=(
                    f"Complete a bounded slice of parent stage {stage.stage_id}: {stage.goal} "
                    f"Writable paths for this slice: {', '.join(chunk)}."
                ),
                suggested_paths=tuple(chunk),
                test_focus=stage.test_focus if is_last else tuple(f"compile:{path}" for path in chunk),
                test_commands=stage.test_commands if is_last else (),
                required_observables=stage.required_observables if is_last else (),
                writable_paths=tuple(chunk),
                repair_scope_paths=repair_scope,
                readonly_evidence_paths=stage.readonly_evidence_paths,
                api_profile=stage.api_profile,
                max_rounds=stage.max_rounds,
            )
        )
    return children


def split_oversized_stage_queue(
    stages: Sequence[StageWorkItem],
    *,
    max_writable_paths: int,
) -> list[StageWorkItem]:
    if max_writable_paths < 1:
        raise RunnerError("max_writable_paths must be at least 1")
    result: list[StageWorkItem] = []
    for stage in stages:
        paths = stage.writable_paths or stage.suggested_paths
        if len(paths) <= max_writable_paths:
            result.append(stage)
            continue
        parts = int(math.ceil(len(paths) / max_writable_paths))
        result.extend(split_stage_work_item(stage, parts=parts))
    return result


def verification_commands_from_spec(spec: str) -> list[str]:
    """Read only explicit commands from a verification-labelled section."""
    commands: list[str] = []
    active = False
    in_fence = False
    for line in spec.splitlines():
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if heading and not in_fence:
            title = heading.group(2).strip().lower()
            active = any(token in title for token in VERIFICATION_HEADING_TOKENS)
            continue
        stripped = line.strip()
        if active and stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if not active:
            continue
        candidates = [stripped] if in_fence else re.findall(r"`([^`\n]+)`", line)
        for candidate in candidates:
            command = candidate.strip()
            lowered = command.lower()
            if not command or "..." in command:
                continue
            if lowered == "make" or lowered.startswith(COMMAND_PREFIXES):
                commands.append(command)
        if len(commands) >= 16:
            break
    return unique_ordered(commands)
