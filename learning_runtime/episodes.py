"""Causal recovery-episode normalization for the learning control plane."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Mapping, Sequence

from sdlc_events import EventType, canonical_json

from .privacy import sanitize_shared, sensitive_values
from .storage import ExperienceStore


RECOVERY_ROLES = {
    EventType.RECOVERY_PLANNED.value: "decision",
    EventType.RECOVERY_STARTED.value: "intervention",
    EventType.RECOVERY_COMPLETED.value: "outcome",
}


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _items(value: object) -> list[object]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _integer(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _identifier(prefix: str, value: object) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:24]}"


def _failure_context(plan: Mapping[str, object]) -> dict[str, object]:
    family = str(plan.get("failure_family") or "")
    parts = family.split("|") if family else []
    failure_type = parts[1] if len(parts) > 1 else "unknown"
    subjects = [
        part
        for part in parts[2:]
        if part and not part.startswith("FAILED (")
    ]
    family_count = max(0, _integer(plan.get("failure_family_count")))
    return {
        "failure_type": failure_type,
        "failure_subject_count": len(subjects),
        "repeat_class": "repeated" if family_count >= 2 else "single_or_unknown",
        "plateau_detected": bool(plan.get("plateau_detected")),
    }


def _normalized_change(payload: Mapping[str, object]) -> tuple[dict[str, object], list[str]]:
    paths = [str(item) for item in _items(payload.get("changed_paths")) if str(item).strip()]
    suffixes = sorted(Path(path).suffix.lower() or "<none>" for path in paths)
    files = [
        {"id": f"file_{index}", "suffix": suffix}
        for index, suffix in enumerate(suffixes, start=1)
    ]
    atomic = bool(payload.get("atomic_change")) and len(paths) == 1
    return {
        "count": len(paths),
        "atomic": atomic,
        "files": files,
    }, paths


def _evidence_summary(events: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    evidence: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()
    for event in events:
        role = RECOVERY_ROLES.get(str(event.get("event_type") or ""), "context")
        for raw_reference in _items(event.get("evidence_refs")):
            reference = _mapping(raw_reference)
            key = (
                str(reference.get("sha256") or ""),
                str(reference.get("media_type") or ""),
                role,
            )
            if not key[0] or key in seen:
                continue
            seen.add(key)
            evidence.append(
                {
                    "role": role,
                    "sha256": key[0],
                    "media_type": key[1] or "application/octet-stream",
                }
            )
    return evidence


def _causal_graph(
    selected: Mapping[str, Mapping[str, object]],
    exact: bool,
) -> tuple[dict[str, object], bool]:
    nodes: list[dict[str, object]] = []
    event_to_node: dict[str, str] = {}
    for event_type, role in RECOVERY_ROLES.items():
        event = selected.get(event_type)
        if not event:
            continue
        node_id = f"n{len(nodes) + 1}"
        nodes.append({"id": node_id, "role": role, "event_type": event_type})
        event_to_node[str(event.get("event_id") or "")] = node_id

    edges: list[dict[str, str]] = []
    for event_type in (EventType.RECOVERY_STARTED.value, EventType.RECOVERY_COMPLETED.value):
        event = selected.get(event_type)
        if not event:
            continue
        source_node = event_to_node.get(str(event.get("causation_id") or ""))
        target_node = event_to_node.get(str(event.get("event_id") or ""))
        if source_node and target_node:
            edges.append({"from": source_node, "to": target_node, "kind": "causes"})

    planned = selected.get(EventType.RECOVERY_PLANNED.value, {})
    started = selected.get(EventType.RECOVERY_STARTED.value, {})
    completed = selected.get(EventType.RECOVERY_COMPLETED.value, {})
    complete = (
        exact
        and bool(planned.get("causation_id"))
        and started.get("causation_id") == planned.get("event_id")
        and completed.get("causation_id") == started.get("event_id")
        and len(edges) == 2
    )
    return {"complete": complete, "nodes": nodes, "edges": edges}, complete


def _build_group(events: Sequence[Mapping[str, object]]) -> dict[str, object]:
    ordered = sorted(
        events,
        key=lambda item: (_integer(item.get("sequence")), str(item.get("event_id"))),
    )
    by_type = {
        event_type: [
            event for event in ordered if str(event.get("event_type") or "") == event_type
        ]
        for event_type in RECOVERY_ROLES
    }
    reasons: list[str] = []
    selected: dict[str, Mapping[str, object]] = {}
    exact = True
    for event_type, matches in by_type.items():
        label = event_type.removeprefix("recovery_")
        if not matches:
            reasons.append(f"missing_{label}")
            exact = False
            continue
        selected[event_type] = matches[0]
        if len(matches) > 1:
            reasons.append(f"duplicate_{label}")
            exact = False

    graph, causal_complete = _causal_graph(selected, exact)
    if not causal_complete:
        reasons.append("broken_causal_chain")

    planned = selected.get(EventType.RECOVERY_PLANNED.value, {})
    plan_payload = _mapping(_mapping(planned.get("payload")).get("recovery_plan"))
    completed = selected.get(EventType.RECOVERY_COMPLETED.value, {})
    completion_payload = _mapping(completed.get("payload"))
    strategy = str(
        plan_payload.get("strategy")
        or completion_payload.get("strategy")
        or "unknown"
    )
    change, _raw_paths = _normalized_change(completion_payload)
    isolation = str(completion_payload.get("change_isolation") or "unknown").lower()
    concurrent = [
        str(item)
        for item in _items(completion_payload.get("concurrent_changed_paths"))
        if str(item).strip()
    ]
    verified = (
        completion_payload.get("verification_passed") is True
        and completion_payload.get("outcome") == "completed"
        and completion_payload.get("target_final_verdict") == "approved"
    )
    if not verified:
        reasons.append("outcome_not_verified")
    if not change["atomic"]:
        reasons.append("non_atomic_change")
    if isolation == "unisolated":
        reasons.append("unisolated_change")
    elif isolation != "isolated":
        reasons.append("change_isolation_unknown")
    if concurrent:
        reasons.append("concurrent_changes")

    context = _failure_context(plan_payload)
    hypothesis = {
        "analysis_available": bool(plan_payload.get("analysis_available")),
        "strategy_class": (
            "analytic"
            if strategy in {"failure_analysis", "root_cause_recovery"}
            else "ordinary"
        ),
    }
    intervention = {"strategy": strategy, "change_isolation": isolation}
    outcome = {
        "status": str(completion_payload.get("outcome") or "unknown"),
        "verdict": str(completion_payload.get("target_final_verdict") or "unknown"),
        "verified": verified,
    }
    structural_core = {
        "episode_kind": "stalled_recovery",
        "context": context,
        "hypothesis": hypothesis,
        "intervention": intervention,
        "change": change,
        "outcome": outcome,
    }
    event_ids = [str(event.get("event_id") or "") for event in ordered]
    episode = {
        "schema_version": 1,
        "episode_id": _identifier("EP", event_ids),
        "episode_kind": "stalled_recovery",
        "source_run_id": str(ordered[0].get("run_id") or "") if ordered else "",
        "project_fingerprint": str(ordered[0].get("project_fingerprint") or "") if ordered else "",
        "eligibility": "eligible" if not reasons else "case_only",
        "reason_codes": sorted(set(reasons)),
        "structural_signature": _identifier("ES", structural_core),
        "context": context,
        "hypothesis": hypothesis,
        "intervention": intervention,
        "change": change,
        "outcome": outcome,
        "causal_graph": graph,
        "evidence": _evidence_summary(ordered),
        "provenance": {
            "source_event_ids": event_ids,
            "source_event_hashes": [
                str(event.get("event_hash") or "")
                for event in ordered
                if str(event.get("event_hash") or "")
            ],
        },
    }
    sanitized = sanitize_shared(episode)
    unsafe = sensitive_values(sanitized)
    if unsafe:
        raise ValueError("normalized episode contains sensitive values: " + ", ".join(unsafe))
    if not isinstance(sanitized, dict):
        raise ValueError("normalized episode must be an object")
    return sanitized


def build_recovery_episode_documents(
    events: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    groups: dict[tuple[str, str], list[Mapping[str, object]]] = {}
    for event in events:
        event_type = str(event.get("event_type") or "")
        if event_type not in RECOVERY_ROLES:
            continue
        run_id = str(event.get("run_id") or "")
        aggregate_id = str(event.get("aggregate_id") or event.get("event_id") or "")
        groups.setdefault((run_id, aggregate_id), []).append(event)
    episodes = [_build_group(group) for group in groups.values()]
    return sorted(episodes, key=lambda item: str(item["episode_id"]))


def build_and_store_recovery_episodes(store: ExperienceStore) -> dict[str, object]:
    episodes = build_recovery_episode_documents(store.events())
    inserted_count = sum(1 for episode in episodes if store.put_episode(episode))
    return {
        "status": "pass",
        "episode_count": len(episodes),
        "inserted_count": inserted_count,
        "duplicate_count": len(episodes) - inserted_count,
        "eligible_count": sum(item["eligibility"] == "eligible" for item in episodes),
        "case_only_count": sum(item["eligibility"] == "case_only" for item in episodes),
        "stored_episode_count": store.episode_count(),
    }
