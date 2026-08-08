#!/usr/bin/env python3
"""Status classification helpers for minigit."""

from pathlib import Path


def classify_status(index_entries, head_tree, worktree_root):
    """Classify repository state into the four status keys.

    index_entries: dict mapping normalized repo-relative paths to blob OIDs.
    head_tree: dict mapping tracked paths to blob OIDs from the HEAD commit.
    worktree_root: pathlib.Path of the repository root.

    Returns {"staged": [], "modified": [], "deleted": [], "untracked": []}
    with each list sorted and containing repo-relative POSIX paths.
    """
    staged = []
    modified = []
    deleted = []
    untracked = []

    for path in sorted(set(index_entries) | set(head_tree)):
        indexed_oid = index_entries.get(path)
        head_oid = head_tree.get(path)
        if head_oid != indexed_oid:
            staged.append(path)
        if path not in index_entries:
            continue
        work_path = worktree_root / path
        if not work_path.exists():
            deleted.append(path)
        elif work_path.is_file():
            blob_oid = _blob_oid(work_path)
            if blob_oid != indexed_oid:
                modified.append(path)
        else:
            deleted.append(path)

    for path in _walk_untracked(worktree_root, index_entries):
        untracked.append(path)

    return {
        "staged": sorted(staged),
        "modified": sorted(modified),
        "deleted": sorted(deleted),
        "untracked": sorted(untracked),
    }


def _blob_oid(work_path: Path) -> str:
    """Return the blob OID for a working file's exact bytes."""
    from minigit.objects import hash_object

    return hash_object("blob", work_path.read_bytes())


def _walk_untracked(worktree_root: Path, index_entries: dict):
    """Yield ordinary working files absent from the index, excluding .minigit."""
    import os

    for current, dirs, files in os.walk(worktree_root):
        dirs[:] = [d for d in dirs if d != ".minigit"]
        for name in files:
            file_path = Path(current) / name
            if file_path.is_symlink():
                continue
            rel = file_path.relative_to(worktree_root)
            if rel.parts[0] == ".minigit":
                continue
            if str(rel) not in index_entries:
                yield str(rel)
