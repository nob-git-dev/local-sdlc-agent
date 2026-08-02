"""Validated protocol shared by browser harness clients and workers."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Mapping, Sequence

from ..models import RunnerError


BROWSER_PROTOCOL_VERSION = 1
TETRIS_DOM_CHECK = "tetris-dom-v1"
ALLOWED_BROWSER_CHECKS = frozenset({TETRIS_DOM_CHECK})
MAX_BROWSER_REQUEST_BYTES = 32 * 1024
MAX_BROWSER_OUTPUT_BYTES = 1024 * 1024
MAX_BROWSER_RESPONSE_BYTES = 2 * MAX_BROWSER_OUTPUT_BYTES + 64 * 1024
MIN_BROWSER_TIMEOUT_SECONDS = 0.1
MAX_BROWSER_TIMEOUT_SECONDS = 120.0


class BrowserProtocolError(RunnerError):
    """Raised when an untrusted browser request violates the protocol."""

    def __init__(self, message: str, *, status_code: int = 400):
        self.status_code = status_code
        super().__init__(message)


def truncate_browser_output(value: str) -> str:
    """Limit a text field by UTF-8 bytes without returning broken text."""
    encoded = value.encode("utf-8")
    if len(encoded) <= MAX_BROWSER_OUTPUT_BYTES:
        return value
    return encoded[:MAX_BROWSER_OUTPUT_BYTES].decode("utf-8", errors="ignore")


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise BrowserProtocolError(f"{key} must be a non-empty string")
    return value.strip()


def _validated_timeout(value: object) -> float:
    if isinstance(value, bool):
        raise BrowserProtocolError("timeout_seconds must be a number")
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise BrowserProtocolError("timeout_seconds must be a number") from exc
    if not MIN_BROWSER_TIMEOUT_SECONDS <= timeout <= MAX_BROWSER_TIMEOUT_SECONDS:
        raise BrowserProtocolError(
            f"timeout_seconds must be between {MIN_BROWSER_TIMEOUT_SECONDS:g} and "
            f"{MAX_BROWSER_TIMEOUT_SECONDS:g}"
        )
    return timeout


@dataclasses.dataclass(frozen=True)
class BrowserCheckRequest:
    check: str
    project: Path
    entrypoint: str
    timeout_seconds: float

    @classmethod
    def create(
        cls,
        project: Path,
        entrypoint: str,
        timeout_seconds: float,
        *,
        check: str = TETRIS_DOM_CHECK,
    ) -> "BrowserCheckRequest":
        resolved = project.expanduser().resolve()
        return cls.from_payload(
            {
                "schema_version": BROWSER_PROTOCOL_VERSION,
                "check": check,
                "project": str(resolved),
                "entrypoint": entrypoint,
                "timeout_seconds": timeout_seconds,
            },
            (resolved,),
        )

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, object],
        allowed_roots: Sequence[Path],
    ) -> "BrowserCheckRequest":
        version = payload.get("schema_version")
        if version != BROWSER_PROTOCOL_VERSION:
            raise BrowserProtocolError(
                f"unsupported browser protocol version: {version!r}"
            )

        check = _required_string(payload, "check")
        if check not in ALLOWED_BROWSER_CHECKS:
            raise BrowserProtocolError(f"unknown browser check: {check}")

        raw_project = Path(_required_string(payload, "project")).expanduser()
        if not raw_project.is_absolute():
            raise BrowserProtocolError("project must be an absolute path")
        try:
            project = raw_project.resolve(strict=True)
        except OSError as exc:
            raise BrowserProtocolError(f"project cannot be resolved: {exc}") from exc
        if not project.is_dir():
            raise BrowserProtocolError("project must be a directory")

        roots = tuple(root.expanduser().resolve() for root in allowed_roots)
        if not roots or not any(_is_inside(project, root) for root in roots):
            raise BrowserProtocolError("project is outside allowed roots", status_code=403)

        entrypoint = _required_string(payload, "entrypoint")
        raw_entrypoint = Path(entrypoint)
        if raw_entrypoint.is_absolute():
            raise BrowserProtocolError("entrypoint must be project-relative")
        candidate = (project / raw_entrypoint).resolve()
        if not _is_inside(candidate, project):
            raise BrowserProtocolError("entrypoint must stay inside project", status_code=403)
        if not candidate.exists() or not candidate.is_file():
            raise BrowserProtocolError("entrypoint must be an existing regular file")
        if candidate.suffix.lower() not in {".html", ".htm"}:
            raise BrowserProtocolError("entrypoint must be an HTML file")

        timeout = _validated_timeout(payload.get("timeout_seconds"))
        normalized_entrypoint = candidate.relative_to(project).as_posix()
        return cls(
            check=check,
            project=project,
            entrypoint=normalized_entrypoint,
            timeout_seconds=timeout,
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": BROWSER_PROTOCOL_VERSION,
            "check": self.check,
            "project": str(self.project),
            "entrypoint": self.entrypoint,
            "timeout_seconds": self.timeout_seconds,
        }


@dataclasses.dataclass(frozen=True)
class BrowserCheckResult:
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": BROWSER_PROTOCOL_VERSION,
            "status": "pass" if self.ok else "fail",
            "returncode": self.returncode,
            "stdout": truncate_browser_output(self.stdout),
            "stderr": truncate_browser_output(self.stderr),
            "duration_seconds": self.duration_seconds,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "BrowserCheckResult":
        if payload.get("schema_version") != BROWSER_PROTOCOL_VERSION:
            raise BrowserProtocolError("browser worker returned an unsupported protocol version")
        status = payload.get("status")
        if status not in {"pass", "fail"}:
            raise BrowserProtocolError("browser worker returned an invalid status")
        returncode = payload.get("returncode")
        if isinstance(returncode, bool) or not isinstance(returncode, int):
            raise BrowserProtocolError("browser worker returned an invalid returncode")
        stdout = payload.get("stdout")
        stderr = payload.get("stderr")
        if not isinstance(stdout, str) or not isinstance(stderr, str):
            raise BrowserProtocolError("browser worker returned invalid output fields")
        try:
            duration = float(payload.get("duration_seconds"))
        except (TypeError, ValueError) as exc:
            raise BrowserProtocolError("browser worker returned an invalid duration") from exc
        if duration < 0:
            raise BrowserProtocolError("browser worker returned a negative duration")
        if (returncode == 0) != (status == "pass"):
            raise BrowserProtocolError("browser worker returned an inconsistent status")
        return cls(
            returncode=returncode,
            stdout=truncate_browser_output(stdout),
            stderr=truncate_browser_output(stderr),
            duration_seconds=duration,
        )
