"""Content-addressed object storage."""

import hashlib
import json
from pathlib import Path

from minigit.errors import CorruptObjectError


SUPPORTED_KINDS = {"blob", "tree", "commit"}


def hash_object(kind: str, payload: bytes) -> str:
    """Return the SHA-256 object ID for an ASCII kind and byte payload."""
    encoded = kind.encode("ascii") + b"\0" + payload
    return hashlib.sha256(encoded).hexdigest()


class ObjectStore:
    """Persistent content-addressed object store under a git directory."""

    def __init__(self, git_dir: Path):
        self.git_dir = Path(git_dir)
        self.objects_dir = self.git_dir / "objects"

    def _object_path(self, oid: str) -> Path:
        return self.objects_dir / oid[:2] / oid[2:]

    def write(self, kind: str, payload: bytes) -> str:
        """Store an object and return its OID."""
        if kind not in SUPPORTED_KINDS:
            raise CorruptObjectError(f"unsupported object kind: {kind}")
        encoded = kind.encode("ascii") + b"\0" + payload
        oid = hashlib.sha256(encoded).hexdigest()
        path = self._object_path(oid)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(encoded)
        return oid

    def read(self, oid: str) -> tuple[str, bytes]:
        """Read an object, verifying its digest and kind."""
        path = self._object_path(oid)
        if not path.exists():
            raise CorruptObjectError(f"missing object: {oid}")
        encoded = path.read_bytes()
        separator = encoded.find(b"\0")
        if separator <= 0:
            raise CorruptObjectError(f"malformed object: {oid}")
        kind_bytes = encoded[:separator]
        payload = encoded[separator + 1 :]
        try:
            kind = kind_bytes.decode("ascii")
        except UnicodeDecodeError:
            raise CorruptObjectError(f"malformed object kind: {oid}") from None
        if kind not in SUPPORTED_KINDS:
            raise CorruptObjectError(f"unsupported object kind: {kind}")
        if hash_object(kind, payload) != oid:
            raise CorruptObjectError(f"digest mismatch: {oid}")
        return kind, payload

    def read_json(self, oid: str) -> dict:
        """Read a tree or commit object as a JSON dictionary."""
        kind, payload = self.read(oid)
        if kind not in {"tree", "commit"}:
            raise CorruptObjectError(f"expected JSON object, got {kind}")
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise CorruptObjectError(f"malformed JSON object: {oid}") from None
