#!/usr/bin/env python3
"""Index persistence and safe path normalization for minigit."""

import json
import os
from pathlib import Path, PurePosixPath

from minigit.errors import InvalidPathError


class Index:
    """Persistent mapping of normalized repo-relative paths to blob OIDs."""

    def __init__(self, git_dir: Path):
        self.git_dir = Path(git_dir)
        self.index_path = self.git_dir / "index.json"
        self._entries = {}

    def load(self) -> None:
        """Load the index from disk, defaulting to an empty mapping."""
        if self.index_path.exists():
            with self.index_path.open("r", encoding="utf-8") as fh:
                self._entries = json.load(fh)
        else:
            self._entries = {}

    def entries(self) -> dict:
        """Return the current index entries."""
        return dict(self._entries)

    def get(self, path: str):
        """Return the blob OID for a path, or None."""
        return self._entries.get(path)

    def set(self, path: str, oid: str) -> None:
        """Set a path to a blob OID."""
        self._entries[path] = oid

    def remove(self, path: str) -> None:
        """Remove a path from the index."""
        self._entries.pop(path, None)

    def save(self) -> None:
        """Atomically persist the index as UTF-8 JSON."""
        payload = json.dumps(
            self._entries, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        self.git_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = _temp_file(self.index_path)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
            os.replace(tmp_path, self.index_path)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)


def _temp_file(target: Path) -> tuple:
    """Create a temporary file next to the target for atomic replacement."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = _mkstemp(dir=str(target.parent), prefix=".index.tmp")
    return fd, name


def _mkstemp(dir: str, prefix: str) -> tuple:
    """Wrap tempfile.mkstemp for atomic index writes."""
    import tempfile

    return tempfile.mkstemp(dir=dir, prefix=prefix)


def normalize_path(root: Path, path: str) -> str:
    """Normalize a user-supplied path to a safe repo-relative POSIX path.

    Raises InvalidPathError for absolute paths, escaping paths, symlinks,
    and paths inside .minigit.
    """
    root = Path(root).resolve()
    raw = Path(path)
    if raw.is_absolute():
        raise InvalidPathError(f"absolute path not allowed: {path}")
    candidate = (root / raw).resolve()
    if not _is_within(candidate, root):
        raise InvalidPathError(f"path escapes repository: {path}")
    rel = candidate.relative_to(root)
    if _is_inside_minigit(rel):
        raise InvalidPathError(f"path inside .minigit not allowed: {path}")
    if _has_symlink_component(root, raw):
        raise InvalidPathError(f"symlink path not allowed: {path}")
    return str(PurePosixPath(rel))


def _is_within(candidate: Path, root: Path) -> bool:
    """Return True if candidate is root or below it."""
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _is_inside_minigit(rel: Path) -> bool:
    """Return True if the relative path points inside .minigit."""
    parts = rel.parts
    return len(parts) > 0 and parts[0] == ".minigit"


def _has_symlink_component(root: Path, raw: Path) -> bool:
    """Return True if any path component is a symlink."""
    current = root
    for part in raw.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False
