"""Content-independent impact classification for knowledge promotion."""

from __future__ import annotations

from typing import Callable, Mapping, Sequence

from .knowledge_schema import KnowledgeItem


HIGH_IMPACT_TERMS = {
    "access_control",
    "approval",
    "authorization",
    "permission",
    "privilege",
    "safety",
}


def _contains_high_impact_term(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            str(key).lower() in HIGH_IMPACT_TERMS
            or _contains_high_impact_term(item)
            for key, item in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(_contains_high_impact_term(item) for item in value)
    return isinstance(value, str) and value.lower() in HIGH_IMPACT_TERMS


def is_high_impact(item: KnowledgeItem) -> bool:
    return (
        item.effect in {"require", "forbid"}
        or item.kind == "normative"
        or _contains_high_impact_term(item.antecedents)
        or _contains_high_impact_term(item.conclusion)
    )


def active_item(item: KnowledgeItem) -> KnowledgeItem:
    payload = item.to_dict()
    payload["state"] = "active"
    return KnowledgeItem.from_dict(payload)


def non_active_snapshot_items(
    snapshot: Mapping[str, object],
    state_for: Callable[[str, int], str],
) -> tuple[str, ...]:
    return tuple(
        f"{item['knowledge_id']}:{item['version']}"
        for item in snapshot["active_items"]
        if state_for(str(item["knowledge_id"]), int(item["version"])) != "active"
    )


def snapshot_matches_active_items(
    snapshot: Mapping[str, object],
    items: Sequence[KnowledgeItem],
) -> bool:
    expected = {(item.knowledge_id, item.version) for item in items}
    actual = {
        (str(item["knowledge_id"]), int(item["version"]))
        for item in snapshot["active_items"]
    }
    return actual == expected


__all__ = [
    "active_item",
    "is_high_impact",
    "non_active_snapshot_items",
    "snapshot_matches_active_items",
]
