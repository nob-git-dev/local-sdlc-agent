"""Generic repair actions derived from acceptance and command evidence."""

from __future__ import annotations

import json
from typing import Sequence

from ..models import RepairAction, RepairAdvice
from ..utils import unique_ordered
from ..verification import parse_command_result_document

def acceptance_gate_blockers_from_command_docs(
    command_docs: Sequence[tuple[str, str]],
) -> list[dict[str, object]]:
    blockers: list[dict[str, object]] = []
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
            blockers.extend(item for item in raw_blockers if isinstance(item, dict))
    return blockers


def repair_action_from_acceptance_blocker(
    blocker: dict[str, object],
    index: int,
    focus_files: Sequence[str] = (),
) -> RepairAction:
    blocker_id = str(blocker.get("id") or f"A{index:02d}")
    status = str(blocker.get("status") or "unverified")
    text = str(blocker.get("text") or "").strip()
    raw_covers = blocker.get("required_covers")
    covers = tuple(
        unique_ordered(str(item) for item in raw_covers if isinstance(item, str))
    ) if isinstance(raw_covers, list) else ()
    if status == "unverified":
        kind = "produce_acceptance_evidence"
    elif status == "fail":
        kind = "fix_acceptance_failure"
    else:
        kind = "resolve_acceptance_blocker"
    instruction = f"Satisfy acceptance criterion {blocker_id}"
    if text:
        instruction += f": {text}"
    if covers:
        instruction += ". Required evidence covers: " + ", ".join(covers)
    return RepairAction(
        action_id=f"R{index:02d}",
        kind=kind,
        source="acceptance_gate",
        target_paths=tuple(unique_ordered(focus_files)),
        required_covers=covers,
        instruction=instruction,
        evidence=(f"acceptance_blocker={blocker_id}:{status}",),
    )


def repair_actions_from_advice(
    advice: RepairAdvice,
    command_docs: Sequence[tuple[str, str]] = (),
) -> tuple[RepairAction, ...]:
    blockers = acceptance_gate_blockers_from_command_docs(command_docs)
    if blockers:
        return tuple(
            repair_action_from_acceptance_blocker(blocker, index, advice.focus_files)
            for index, blocker in enumerate(blockers, start=1)
        )
    if not advice.instructions:
        return ()
    return (
        RepairAction(
            action_id="R01",
            kind="apply_repair_strategy",
            source="repair_advice",
            target_paths=advice.focus_files,
            required_covers=(),
            instruction=advice.instructions[0],
            evidence=advice.evidence,
        ),
    )


def repair_action_to_dict(action: RepairAction) -> dict[str, object]:
    return {
        "id": action.action_id,
        "kind": action.kind,
        "source": action.source,
        "target_paths": list(action.target_paths),
        "required_covers": list(action.required_covers),
        "instruction": action.instruction,
        "evidence": list(action.evidence),
    }


def repair_advice_to_manifest(
    advice: RepairAdvice,
    command_docs: Sequence[tuple[str, str]] = (),
) -> dict[str, object]:
    actions = repair_actions_from_advice(advice, command_docs)
    return {
        "strategy": advice.strategy,
        "focus_files": list(advice.focus_files),
        "instructions": list(advice.instructions),
        "evidence": list(advice.evidence),
        "repair_actions": [repair_action_to_dict(action) for action in actions],
    }
