#!/usr/bin/env python3
"""Reference and HEAD handling for minigit."""

from pathlib import Path

from minigit.errors import RevisionNotFoundError


class Refs:
    """Manage symbolic and detached HEAD plus branch refs."""

    def __init__(self, git_dir: Path):
        self.git_dir = Path(git_dir)
        self.head_path = self.git_dir / "HEAD"
        self.heads_dir = self.git_dir / "refs" / "heads"

    def read_head(self) -> str:
        """Return the raw HEAD content."""
        return self.head_path.read_text(encoding="utf-8")

    def head_is_detached(self) -> bool:
        """Return True if HEAD stores a full OID rather than a symbolic ref."""
        return not self.read_head().startswith("ref: ")

    def current_branch(self):
        """Return the current branch name, or None when detached."""
        raw = self.read_head().strip()
        if raw.startswith("ref: "):
            ref_name = raw[len("ref: "):]
            if ref_name.startswith("refs/heads/"):
                return ref_name[len("refs/heads/"):]
            return ref_name
        return None

    def read_branch(self, name: str):
        """Return the branch ref value, or None if absent."""
        path = self.heads_dir / name
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8").strip() or None

    def write_branch(self, name: str, oid: str) -> None:
        """Atomically set a branch ref to an OID."""
        path = self.heads_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(oid + "\n", encoding="utf-8")
        tmp.replace(path)

    def write_head_symbolic(self, name: str) -> None:
        """Store a symbolic HEAD pointing at a branch."""
        self.head_path.write_text(f"ref: refs/heads/{name}\n", encoding="utf-8")

    def write_head_detached(self, oid: str) -> None:
        """Store a detached HEAD containing a full OID."""
        self.head_path.write_text(oid + "\n", encoding="utf-8")

    def resolve_head(self):
        """Resolve HEAD to a commit OID, or None when unborn."""
        raw = self.read_head().strip()
        if raw.startswith("ref: "):
            ref_name = raw[len("ref: "):]
            if ref_name.startswith("refs/heads/"):
                return self.read_branch(ref_name[len("refs/heads/"):])
            return None
        return raw or None

    def resolve(self, revision: str):
        """Resolve a revision name to a commit OID, or None."""
        if revision == "HEAD":
            return self.resolve_head()
        branch = self.read_branch(revision)
        if branch is not None:
            return branch
        return None
