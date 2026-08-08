#!/usr/bin/env python3
"""Canonical tree and commit records for minigit."""

import json
from datetime import datetime, timezone

from minigit.errors import MiniGitError, NothingToCommitError
from minigit.objects import ObjectStore


class CommitGraph:
    """Build canonical trees and commit objects from the index."""

    def __init__(self, git_dir, index, objects: ObjectStore):
        self.git_dir = git_dir
        self.index = index
        self.objects = objects

    def build_tree(self) -> str:
        """Write a canonical tree object from the index and return its OID."""
        entries = self.index.entries()
        payload = json.dumps(
            entries, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return self.objects.write("tree", payload)

    def read_tree(self, oid: str) -> dict:
        """Read a tree object as a sorted mapping."""
        return self.objects.read_json(oid)

    def build_commit(
        self,
        tree_oid: str,
        parent_oid,
        message: str,
        author: str,
        timestamp,
    ) -> str:
        """Write a commit object and return its OID."""
        if not message or not message.strip():
            raise MiniGitError("empty commit message")
        if timestamp is None:
            timestamp = datetime.now(timezone.utc).isoformat()
        commit = {
            "tree": tree_oid,
            "parent": parent_oid,
            "message": message,
            "author": author,
            "timestamp": timestamp,
        }
        payload = json.dumps(
            commit, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return self.objects.write("commit", payload)

    def read_commit(self, oid: str) -> dict:
        """Read a commit object as a dictionary."""
        return self.objects.read_json(oid)

    def first_parent_log(self, head_oid, max_count=None):
        """Return commits from head following first parents, newest first."""
        items = []
        current = head_oid
        while current is not None:
            commit = self.read_commit(current)
            items.append({
                "oid": current,
                "tree": commit["tree"],
                "parent": commit.get("parent"),
                "message": commit["message"],
                "author": commit["author"],
                "timestamp": commit["timestamp"],
            })
            if max_count is not None and len(items) >= max_count:
                break
            current = commit.get("parent")
        return items
