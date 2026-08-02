"""Immutable run-local knowledge binding persistence."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Iterator, Mapping

from sdlc_events import canonical_json, stable_identifier


KNOWLEDGE_BINDING_FILENAME = "knowledge-snapshot.json"
KNOWLEDGE_BINDING_LOCK = ".knowledge-snapshot.lock"


def knowledge_binding_path(run_dir: Path) -> Path:
    return run_dir / KNOWLEDGE_BINDING_FILENAME


@contextmanager
def binding_lock(run_dir: Path) -> Iterator[None]:
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / KNOWLEDGE_BINDING_LOCK).open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _binding_hash(record: Mapping[str, object]) -> str:
    core = {
        key: value
        for key, value in record.items()
        if key not in {"binding_hash", "binding_id"}
    }
    return hashlib.sha256(canonical_json(core).encode("utf-8")).hexdigest()


def verified_binding(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("invalid knowledge binding schema")
    expected = str(payload.get("binding_hash") or "")
    calculated = _binding_hash(payload)
    if not expected or expected != calculated:
        raise ValueError("knowledge binding hash mismatch")
    if payload.get("binding_id") != stable_identifier("KB", calculated):
        raise ValueError("knowledge binding identity mismatch")
    return dict(payload)


def read_learning_binding(run_dir: Path) -> dict[str, object]:
    path = knowledge_binding_path(run_dir)
    if not path.is_file():
        raise ValueError(f"knowledge binding is missing: {path.name}")
    return verified_binding(json.loads(path.read_text(encoding="utf-8")))


def write_binding_unlocked(
    run_dir: Path,
    core: Mapping[str, object],
) -> dict[str, object]:
    record = dict(core)
    digest = _binding_hash(record)
    record["binding_hash"] = digest
    record["binding_id"] = stable_identifier("KB", digest)
    serialized = canonical_json(record)
    target = knowledge_binding_path(run_dir)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(serialized + "\n", encoding="utf-8")
    os.replace(temporary, target)
    return verified_binding(record)


def inherit_learning_binding(
    parent_run_dir: Path,
    child_run_dir: Path,
) -> dict[str, object]:
    source = read_learning_binding(parent_run_dir)
    with binding_lock(child_run_dir):
        target = knowledge_binding_path(child_run_dir)
        if target.is_file():
            existing = read_learning_binding(child_run_dir)
            if existing != source:
                raise ValueError("child run already has a different knowledge binding")
            return existing
        serialized = canonical_json(source)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        temporary.write_text(serialized + "\n", encoding="utf-8")
        os.replace(temporary, target)
        return read_learning_binding(child_run_dir)


__all__ = [
    "KNOWLEDGE_BINDING_FILENAME",
    "binding_lock",
    "inherit_learning_binding",
    "knowledge_binding_path",
    "read_learning_binding",
    "verified_binding",
    "write_binding_unlocked",
]
