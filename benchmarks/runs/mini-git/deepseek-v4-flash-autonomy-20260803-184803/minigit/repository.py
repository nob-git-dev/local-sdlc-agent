#!/usr/bin/env python3
"""Repository discovery, initialization, and index operations."""

import os
from pathlib import Path

from minigit.commits import CommitGraph
from minigit.errors import (
    CorruptObjectError,
    DirtyWorktreeError,
    InvalidPathError,
    MiniGitError,
    NothingToCommitError,
    RepositoryNotFoundError,
    RevisionNotFoundError,
)
from minigit.index import Index, normalize_path
from minigit.objects import ObjectStore
from minigit.refs import Refs
from minigit.status import classify_status


class Repository:
    """A minigit repository rooted at a directory containing .minigit."""

    def __init__(self, root: Path, git_dir: Path):
        self.root = Path(root)
        self.git_dir = Path(git_dir)
        self.index = Index(self.git_dir)
        self.objects = ObjectStore(self.git_dir)
        self.refs = Refs(self.git_dir)
        self.commits = CommitGraph(self.git_dir, self.index, self.objects)

    @classmethod
    def init(cls, path) -> "Repository":
        """Initialize a repository at path, idempotently."""
        root = Path(path).resolve()
        git_dir = root / ".minigit"
        git_dir.mkdir(parents=True, exist_ok=True)
        (git_dir / "objects").mkdir(parents=True, exist_ok=True)
        (git_dir / "refs" / "heads").mkdir(parents=True, exist_ok=True)
        head_path = git_dir / "HEAD"
        if not head_path.exists():
            head_path.write_text("ref: refs/heads/main\n", encoding="utf-8")
        main_ref = git_dir / "refs" / "heads" / "main"
        if not main_ref.exists():
            main_ref.write_text("", encoding="utf-8")
        index_path = git_dir / "index.json"
        if not index_path.exists():
            index_path.write_text("{}", encoding="utf-8")
        return cls(root, git_dir)

    @classmethod
    def open(cls, path) -> "Repository":
        """Open an existing repository, raising RepositoryNotFoundError if absent."""
        root = Path(path).resolve()
        git_dir = root / ".minigit"
        if not git_dir.is_dir():
            raise RepositoryNotFoundError(f"not a repository: {path}")
        return cls(root, git_dir)

    def add(self, paths) -> None:
        """Stage files or directories, rejecting unsafe paths before mutation."""
        if not paths:
            return
        normalized = []
        for path in paths:
            normalized.append(normalize_path(self.root, path))
        self.index.load()
        for norm in normalized:
            if norm == ".":
                self._add_recursive(".")
            else:
                self._add_one(norm)
        self.index.save()

    def _add_recursive(self, rel: str) -> None:
        """Recursively stage ordinary files under rel, excluding .minigit."""
        base = self.root / rel
        for current, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d != ".minigit"]
            for name in files:
                file_path = Path(current) / name
                if file_path.is_symlink():
                    continue
                rel_path = file_path.relative_to(self.root)
                if _is_inside_minigit(rel_path):
                    continue
                self._stage_file(str(rel_path), file_path)

    def _add_one(self, norm: str) -> None:
        """Stage a single normalized path (file or directory)."""
        target = self.root / norm
        if target.is_dir():
            self._add_recursive(norm)
            return
        if target.exists():
            self._stage_file(norm, target)
        else:
            self.index.remove(norm)

    def _stage_file(self, rel: str, file_path: Path) -> None:
        """Write a blob for file_path and stage it under rel."""
        payload = file_path.read_bytes()
        oid = self.objects.write("blob", payload)
        self.index.set(rel, oid)

    @property
    def head_commit(self):
        """Return the current HEAD commit OID, or None."""
        return self.refs.resolve_head()

    def commit(self, message, author="unknown", timestamp=None):
        """Create a commit from the index and advance the current branch."""
        if not message or not message.strip():
            raise MiniGitError("empty commit message")
        self.index.load()
        tree_oid = self.commits.build_tree()
        parent_oid = self.refs.resolve_head()
        if parent_oid is not None:
            parent_tree = self.commits.read_commit(parent_oid)["tree"]
            if parent_tree == tree_oid:
                raise NothingToCommitError("nothing to commit")
        commit_oid = self.commits.build_commit(
            tree_oid, parent_oid, message, author, timestamp
        )
        branch = self.refs.current_branch()
        if branch is not None:
            self.refs.write_branch(branch, commit_oid)
        else:
            self.refs.write_head_detached(commit_oid)
        self.index.save()
        return commit_oid

    def status(self):
        """Return the four-key status classification."""
        self.index.load()
        head_oid = self.refs.resolve_head()
        if head_oid is None:
            head_tree = {}
        else:
            head_commit = self.commits.read_commit(head_oid)
            head_tree = self.commits.read_tree(head_commit["tree"])
        return classify_status(self.index.entries(), head_tree, self.root)

    def log(self, max_count=None):
        """Return first-parent history from HEAD, newest first."""
        head = self.refs.resolve_head()
        if head is None:
            return []
        return self.commits.first_parent_log(head, max_count=max_count)

    def checkout(self, revision, force=False):
        """Resolve and restore a commit's tree, protecting dirty state."""
        target_oid = self.resolve_revision(revision)
        target_commit = self.commits.read_commit(target_oid)
        target_tree = self.commits.read_tree(target_commit["tree"])
        self.index.load()
        current_oid = self.refs.resolve_head()
        if current_oid is None:
            current_tree = {}
        else:
            current_commit = self.commits.read_commit(current_oid)
            current_tree = self.commits.read_tree(current_commit["tree"])
        if not force:
            status = self.status()
            if status["staged"] or status["modified"] or status["deleted"]:
                raise DirtyWorktreeError("dirty worktree")
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
                self.index.remove(path)
        for path, oid in target_tree.items():
            self.index.set(path, oid)
        self.index.save()
        if self.refs.read_branch(revision) is not None:
            self.refs.write_head_symbolic(revision)
        else:
            self.refs.write_head_detached(target_oid)
        return target_oid

    def resolve_revision(self, revision):
        """Resolve HEAD, a branch, a full OID, or an unambiguous OID prefix."""
        resolved = self.refs.resolve(revision)
        if resolved is not None:
            return resolved
        if len(revision) == 64 and all(c in "0123456789abcdef" for c in revision):
            try:
                self.objects.read(revision)
            except Exception:
                raise RevisionNotFoundError(f"unknown revision: {revision}") from None
            return revision
        if len(revision) >= 4 and all(c in "0123456789abcdef" for c in revision):
            matches = []
            prefix_dir = self.git_dir / "objects" / revision[:2]
            if prefix_dir.is_dir():
                for path in prefix_dir.iterdir():
                    if path.name.startswith(revision[2:]):
                        oid = revision[:2] + path.name
                        try:
                            kind, _ = self.objects.read(oid)
                        except Exception:
                            continue
                        if kind == "commit":
                            matches.append(oid)
            if len(matches) == 1:
                return matches[0]
            raise RevisionNotFoundError(f"unknown revision: {revision}")
        raise RevisionNotFoundError(f"unknown revision: {revision}")


def _is_inside_minigit(rel: Path) -> bool:
    """Return True if the relative path points inside .minigit."""
    parts = rel.parts
    return len(parts) > 0 and parts[0] == ".minigit"
