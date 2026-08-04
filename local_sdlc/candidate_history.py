"""Mechanical identity and replay checks for rejected code candidates."""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from collections import defaultdict
from typing import Mapping, Sequence

from .models import FileArtifact, SearchReplaceArtifact


def _normalized_line(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip())


def _changed_line_delta(before: str, after: str) -> tuple[list[str], list[str]]:
    """Return only changed lines, excluding shared search context."""

    before_lines = before.splitlines()
    after_lines = after.splitlines()
    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    removed: list[str] = []
    added: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        removed.extend(_normalized_line(line) for line in before_lines[i1:i2])
        added.extend(_normalized_line(line) for line in after_lines[j1:j2])
    return [line for line in removed if line], [line for line in added if line]


def _hypothesis_record(
    *,
    kind: str,
    path: str,
    removed: Sequence[str],
    added: Sequence[str],
) -> dict[str, object]:
    payload = {
        "kind": kind,
        "path": path,
        "removed": list(removed),
        "added": list(added),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "signature": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "kind": kind,
        "path": path,
        "removed_line_count": len(removed),
        "added_line_count": len(added),
    }


def search_replace_hypothesis(artifact: SearchReplaceArtifact) -> dict[str, object]:
    removed, added = _changed_line_delta(artifact.search, artifact.replace)
    return _hypothesis_record(
        kind="search_replace_delta",
        path=artifact.path,
        removed=removed,
        added=added,
    )


def file_artifact_hypothesis(artifact: FileArtifact) -> dict[str, object]:
    normalized = [line.rstrip() for line in artifact.content.splitlines()]
    return _hypothesis_record(
        kind=f"file_{artifact.mode}",
        path=artifact.path,
        removed=(),
        added=normalized,
    )


def unified_diff_hypotheses(patch: str) -> list[dict[str, object]]:
    deltas: dict[str, dict[str, list[str]]] = defaultdict(lambda: {"removed": [], "added": []})
    current_path = ""
    in_hunk = False
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            current_path = ""
            in_hunk = False
            continue
        if line.startswith("+++ "):
            raw = line[4:].strip()
            current_path = raw[2:] if raw.startswith("b/") else raw
            if current_path == "/dev/null":
                current_path = ""
            continue
        if line.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk or not current_path:
            continue
        if line.startswith("-") and not line.startswith("---"):
            value = _normalized_line(line[1:])
            if value:
                deltas[current_path]["removed"].append(value)
        elif line.startswith("+") and not line.startswith("+++"):
            value = _normalized_line(line[1:])
            if value:
                deltas[current_path]["added"].append(value)
    return [
        _hypothesis_record(
            kind="unified_diff_delta",
            path=path,
            removed=delta["removed"],
            added=delta["added"],
        )
        for path, delta in sorted(deltas.items())
    ]


def candidate_hypotheses(
    replacements: Sequence[SearchReplaceArtifact] = (),
    artifacts: Sequence[FileArtifact] = (),
    patch: str = "",
) -> list[dict[str, object]]:
    records = [search_replace_hypothesis(item) for item in replacements]
    records.extend(file_artifact_hypothesis(item) for item in artifacts)
    if patch:
        records.extend(unified_diff_hypotheses(patch))
    seen: set[str] = set()
    unique: list[dict[str, object]] = []
    for record in records:
        signature = str(record.get("signature") or "")
        if not signature or signature in seen:
            continue
        seen.add(signature)
        unique.append(record)
    return unique


def replayed_regression_hypotheses(
    candidates: Sequence[Mapping[str, object]],
    regressions: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    candidate_by_signature = {
        str(candidate.get("signature") or ""): dict(candidate)
        for candidate in candidates
        if str(candidate.get("signature") or "")
    }
    candidate_signatures = set(candidate_by_signature)
    if not candidate_signatures:
        return []

    for regression in regressions:
        round_index = regression.get("round")
        raw_hypotheses = regression.get("candidate_hypotheses")
        if not isinstance(raw_hypotheses, list):
            continue
        rejected_signatures: set[str] = set()
        for raw in raw_hypotheses:
            if not isinstance(raw, dict):
                continue
            signature = str(raw.get("signature") or "")
            if signature:
                rejected_signatures.add(signature)
        # A candidate that only replays all or a subset of a regressing edit
        # adds no new causal action. A strict superset is allowed because a new
        # compensating delta may change the hypothesis and must be tested.
        if candidate_signatures.issubset(rejected_signatures):
            return [
                {**candidate_by_signature[signature], "rejected_round": round_index}
                for signature in sorted(candidate_signatures)
            ]
    return []
