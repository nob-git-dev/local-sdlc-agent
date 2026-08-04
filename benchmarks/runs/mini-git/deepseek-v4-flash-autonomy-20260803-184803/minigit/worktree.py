#!/usr/bin/env python3
"""Worktree restoration and status classification for minigit."""

from pathlib import Path

from minigit.errors import CorruptObjectError, DirtyWorktreeError
from minigit.objects import ObjectStore
from minigit.status import classify_status


class Worktree:
    """Restore and inspect a repository working tree."""

    def __init__(self, root: Path, git_dir: Path, objects: ObjectStore):
        self.root = Path(root)
        self.git_dir = Path(git_dir)
        self.objects = objects

    def restore(self, target_tree: dict, current_tree: dict) -> None:
        """Write target tracked files and remove paths absent from target.

        target_tree: mapping of repo-relative paths to blob OIDs.
        current_tree: mapping of tracked paths in the current commit.
        """
        for path, oid in target_tree.items():
            kind, payload = self.objects.read(oid)
            if kind != "blob":
                raise CorruptObjectError(f"expected blob: {path}")
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        for path in current_tree:
            if path not in target_tree:
                candidate = self.root / path
                if candidate.exists():
                    candidate.unlink()

    def assert_clean(self, index_entries: dict, head_tree: dict) -> None:
        """Raise DirtyWorktreeError if staged, modified, or deleted paths exist."""
        status = classify_status(index_entries, head_tree, self.root)
        if status["staged"] or status["modified"] or status["deleted"]:
            raise DirtyWorktreeError("dirty worktree")