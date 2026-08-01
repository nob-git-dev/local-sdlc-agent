"""Failure history normalization and reusable regression memory."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Iterable, Sequence

from .models import StageWorkItem
from .utils import unique_ordered


REGRESSION_MEMORY_SCHEMA_VERSION = 1


@dataclasses.dataclass(frozen=True)
class RegressionMemory:
    failure_family: str
    trigger: dict[str, object]
    false_positive_pattern: str
    required_future_observables: tuple[str, ...]
    fixed_by: str
    regression_tests: tuple[str, ...]
    scope: dict[str, object]

    @property
    def memory_id(self) -> str:
        basis = json.dumps(
            {
                "failure_family": self.failure_family,
                "trigger": self.trigger,
                "scope": self.scope,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return "RM-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:12]

    def to_manifest(self) -> dict[str, object]:
        return {
            "id": self.memory_id,
            "failure_family": self.failure_family,
            "trigger": dict(self.trigger),
            "false_positive_pattern": self.false_positive_pattern,
            "required_future_observables": list(self.required_future_observables),
            "fixed_by": self.fixed_by,
            "regression_tests": list(self.regression_tests),
            "scope": dict(self.scope),
        }


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str) and item.strip()]


def regression_memory_from_dict(payload: dict[str, object]) -> RegressionMemory | None:
    failure_family = str(payload.get("failure_family") or "").strip()
    trigger = payload.get("trigger")
    scope = payload.get("scope")
    if not failure_family or not isinstance(trigger, dict) or not isinstance(scope, dict):
        return None
    return RegressionMemory(
        failure_family=failure_family,
        trigger={str(key): value for key, value in trigger.items()},
        false_positive_pattern=str(payload.get("false_positive_pattern") or "").strip(),
        required_future_observables=tuple(_string_list(payload.get("required_future_observables"))),
        fixed_by=str(payload.get("fixed_by") or "").strip(),
        regression_tests=tuple(_string_list(payload.get("regression_tests"))),
        scope={str(key): value for key, value in scope.items()},
    )


def regression_memory_document(memories: Sequence[RegressionMemory]) -> dict[str, object]:
    return {
        "schema_version": REGRESSION_MEMORY_SCHEMA_VERSION,
        "record_count": len(memories),
        "records": [memory.to_manifest() for memory in memories],
    }


def _memory_from_stage_recovery(plan: dict[str, object]) -> RegressionMemory | None:
    stage_id = str(plan.get("failed_stage_id") or "").strip()
    if not stage_id:
        return None
    action = plan.get("next_required_action")
    action = action if isinstance(action, dict) else {}
    failure_summary = plan.get("failure_summary")
    failure_summary = failure_summary if isinstance(failure_summary, dict) else {}
    failure_type = str(plan.get("failure_type") or "unknown")
    required_paths = _string_list(action.get("required_paths"))
    observables = _string_list(action.get("required_observables"))
    tests = _string_list(action.get("test_commands"))
    pattern = str(
        failure_summary.get("failure_signature")
        or failure_summary.get("name")
        or failure_summary.get("document")
        or failure_type
    )
    return RegressionMemory(
        failure_family=f"stage:{failure_type}",
        trigger={
            "stage_id": stage_id,
            "stage_title": str(plan.get("failed_stage_title") or ""),
            "required_paths": required_paths,
            "failure_type": failure_type,
        },
        false_positive_pattern=pattern,
        required_future_observables=tuple(unique_ordered(observables)),
        fixed_by=str(action.get("kind") or "repair_failed_stage"),
        regression_tests=tuple(unique_ordered(tests)),
        scope={"kind": "project_stage", "stage_id": stage_id},
    )


def _memory_from_failure_analysis(analysis: dict[str, object]) -> RegressionMemory | None:
    if analysis.get("analysis_status") == "aborted":
        return None
    failure_type = str(analysis.get("failure_type") or "unknown")
    signature = str(analysis.get("failure_family_signature") or analysis.get("failure_signature") or "").strip()
    action = analysis.get("next_required_action")
    action = action if isinstance(action, dict) else {}
    required_paths = _string_list(action.get("required_paths"))
    required_focus = _string_list(action.get("required_focus"))
    observables = unique_ordered(
        [*(f"required_path:{path}" for path in required_paths), *(f"inspect:{item}" for item in required_focus)]
    )


def _memory_from_run_failure(manifest: dict[str, object]) -> RegressionMemory | None:
    final_verdict = str(manifest.get("final_verdict") or manifest.get("status") or "")
    if final_verdict in {"", "approved", "pass", "dry_run"}:
        return None
    summary = manifest.get("failure_summary")
    summary = summary if isinstance(summary, dict) else {}
    failure_type = str(summary.get("failure_type") or manifest.get("final_failure_type") or final_verdict)
    family = str(manifest.get("last_functional_failure_family_signature") or "").strip()
    signature = str(manifest.get("last_functional_failure_signature") or "").strip()
    required_paths = _string_list(manifest.get("required_paths"))
    observables: list[str] = []
    blockers = manifest.get("acceptance_blockers")
    if isinstance(blockers, list):
        for blocker in blockers:
            if not isinstance(blocker, dict):
                continue
            observables.extend(
                f"coverage:{cover}"
                for cover in _string_list(blocker.get("required_covers"))
            )
    observables.extend(f"command:{command}" for command in _string_list(manifest.get("test_commands")))
    observables.extend(f"required_path:{path}" for path in required_paths)
    if not observables and not required_paths and not family and not signature:
        return None
    return RegressionMemory(
        failure_family=family or f"run:{failure_type}",
        trigger={
            "failure_type": failure_type,
            "failure_signature": signature,
            "required_paths": required_paths,
        },
        false_positive_pattern=signature or str(summary.get("name") or failure_type),
        required_future_observables=tuple(unique_ordered(observables)),
        fixed_by="produce passing evidence for the failed run before approval",
        regression_tests=tuple(_string_list(manifest.get("test_commands"))),
        scope={"kind": "project_run", "failure_type": failure_type},
    )
    rejected = analysis.get("rejected_hypotheses")
    rejected_text: list[str] = []
    if isinstance(rejected, list):
        for item in rejected:
            if isinstance(item, dict) and item.get("hypothesis"):
                rejected_text.append(str(item["hypothesis"]))
    pattern = "; ".join(rejected_text) or signature or failure_type
    family = signature or f"analysis:{failure_type}"
    return RegressionMemory(
        failure_family=family,
        trigger={
            "failure_type": failure_type,
            "failure_signature": signature,
            "required_paths": required_paths,
        },
        false_positive_pattern=pattern,
        required_future_observables=tuple(observables),
        fixed_by=str(action.get("minimal_patch_goal") or action.get("goal") or "root_cause_repair"),
        regression_tests=(),
        scope={"kind": "failure_family", "failure_type": failure_type},
    )


def regression_memories_from_manifest(manifest: dict[str, object]) -> list[RegressionMemory]:
    memories: list[RegressionMemory] = []
    plan = manifest.get("stage_recovery_plan")
    if isinstance(plan, dict):
        memory = _memory_from_stage_recovery(plan)
        if memory:
            memories.append(memory)
    analyses = manifest.get("failure_analyses")
    if isinstance(analyses, list):
        for item in analyses:
            if isinstance(item, dict):
                memory = _memory_from_failure_analysis(item)
                if memory:
                    memories.append(memory)
    run_failure = _memory_from_run_failure(manifest)
    if run_failure:
        memories.append(run_failure)
    return merge_regression_memories([], memories)


def merge_regression_memories(
    current: Sequence[RegressionMemory],
    additions: Iterable[RegressionMemory],
) -> list[RegressionMemory]:
    merged: dict[str, RegressionMemory] = {item.memory_id: item for item in current}
    for item in additions:
        previous = merged.get(item.memory_id)
        if previous is None:
            merged[item.memory_id] = item
            continue
        merged[item.memory_id] = dataclasses.replace(
            item,
            required_future_observables=tuple(
                unique_ordered([*previous.required_future_observables, *item.required_future_observables])
            ),
            regression_tests=tuple(unique_ordered([*previous.regression_tests, *item.regression_tests])),
        )
    return sorted(merged.values(), key=lambda item: item.memory_id)


def regression_memory_store_path(project: Path) -> Path:
    return project / ".sdlc-runner" / "regression-memory.json"


def load_regression_memories(project: Path) -> list[RegressionMemory]:
    path = regression_memory_store_path(project)
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        return []
    parsed: list[RegressionMemory] = []
    for record in records:
        if isinstance(record, dict):
            memory = regression_memory_from_dict(record)
            if memory:
                parsed.append(memory)
    return merge_regression_memories([], parsed)


def save_regression_memories(project: Path, memories: Sequence[RegressionMemory]) -> Path:
    path = regression_memory_store_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(regression_memory_document(memories), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def persist_regression_memories_for_manifest(
    project: Path,
    run_dir: Path,
    manifest: dict[str, object],
    filename: str = "07-regression-memory.json",
) -> tuple[Path, Path, int, int] | None:
    new_memories = regression_memories_from_manifest(manifest)
    if not new_memories:
        return None
    run_path = run_dir / filename
    run_path.parent.mkdir(parents=True, exist_ok=True)
    run_path.write_text(
        json.dumps(regression_memory_document(new_memories), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    stored = merge_regression_memories(load_regression_memories(project), new_memories)
    store_path = save_regression_memories(project, stored)
    return run_path, store_path, len(new_memories), len(stored)


def memory_applies_to_stage(memory: RegressionMemory, stage: StageWorkItem) -> bool:
    trigger = memory.trigger
    memory_paths = set(_string_list(trigger.get("required_paths")))
    stage_paths = set(stage.suggested_paths) | set(stage.writable_paths) | set(stage.readonly_evidence_paths)
    if memory_paths:
        return bool(memory_paths.intersection(stage_paths))
    stage_title = str(trigger.get("stage_title") or "").strip().lower()
    if stage_title:
        return stage_title == stage.title.strip().lower()
    stage_id = str(trigger.get("stage_id") or "")
    return bool(stage_id and stage_id == stage.stage_id)


def apply_regression_memories_to_stages(
    stages: Sequence[StageWorkItem],
    memories: Sequence[RegressionMemory],
) -> list[StageWorkItem]:
    updated: list[StageWorkItem] = []
    for stage in stages:
        injected = [
            observable
            for memory in memories
            if memory_applies_to_stage(memory, stage)
            for observable in memory.required_future_observables
        ]
        if not injected:
            updated.append(stage)
            continue
        updated.append(
            dataclasses.replace(
                stage,
                required_observables=tuple(
                    unique_ordered([*stage.required_observables, *injected])
                ),
            )
        )
    return updated
