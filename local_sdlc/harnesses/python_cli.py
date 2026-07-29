"""Python and CLI command harnesses."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path
from typing import Sequence

from ..models import RunnerError
from ..verification import command_result_document, run_checked_command
from ..workspace import resolve_project_path
from .base import HarnessEvidence, evidence_from_command_result


class PythonCliHarness:
    """Run safe local command checks and return harness evidence."""

    name = "python_cli"

    def run(
        self,
        project: Path,
        commands: Sequence[str],
        run_dir: Path,
        timeout: float,
    ) -> list[HarnessEvidence]:
        return self.run_commands(project, commands, run_dir, timeout)

    def run_command(
        self,
        project: Path,
        command: str,
        run_dir: Path,
        timeout: float,
        *,
        kind: str = "command",
        name: str | None = None,
    ) -> HarnessEvidence:
        document, ok = run_checked_command(project, command, timeout, run_dir)
        return evidence_from_command_result(kind, name or command, document, ok)

    def run_commands(
        self,
        project: Path,
        commands: Sequence[str],
        run_dir: Path,
        timeout: float,
    ) -> list[HarnessEvidence]:
        return [
            self.run_command(
                project,
                command,
                run_dir,
                timeout,
                name=f"command {index}",
            )
            for index, command in enumerate(commands, start=1)
        ]

    def run_py_compile(
        self,
        project: Path,
        paths: Sequence[str],
        run_dir: Path,
        timeout: float,
    ) -> list[HarnessEvidence]:
        evidence: list[HarnessEvidence] = []
        for raw in paths:
            try:
                path = resolve_project_path(project, raw)
                rel_path = path.relative_to(project.resolve()).as_posix()
            except (RunnerError, ValueError) as exc:
                document = command_result_document(
                    f"py_compile {raw}",
                    1,
                    "",
                    str(exc),
                    0.0,
                )
                evidence.append(evidence_from_command_result("python_compile", f"py_compile {raw}", document, False))
                continue

            command = f"{shlex.quote(sys.executable)} -m py_compile {shlex.quote(rel_path)}"
            evidence.append(
                self.run_command(
                    project,
                    command,
                    run_dir,
                    timeout,
                    kind="python_compile",
                    name=f"py_compile {raw}",
                )
            )
        return evidence

    def run_unittest_discover(
        self,
        project: Path,
        start_dir: str,
        pattern: str,
        run_dir: Path,
        timeout: float,
    ) -> HarnessEvidence:
        command = (
            f"{shlex.quote(sys.executable)} -m unittest discover "
            f"-s {shlex.quote(start_dir)} -p {shlex.quote(pattern)}"
        )
        return self.run_command(
            project,
            command,
            run_dir,
            timeout,
            kind="python_unittest",
            name=f"unittest discover {start_dir} {pattern}",
        )


def run_command_evidence(project: Path, command: str, run_dir: Path, timeout: float) -> HarnessEvidence:
    return PythonCliHarness().run_command(project, command, run_dir, timeout)


def run_commands_evidence(
    project: Path,
    commands: Sequence[str],
    run_dir: Path,
    timeout: float,
) -> list[HarnessEvidence]:
    return PythonCliHarness().run_commands(project, commands, run_dir, timeout)


def run_py_compile_evidence(
    project: Path,
    paths: Sequence[str],
    run_dir: Path,
    timeout: float,
) -> list[HarnessEvidence]:
    return PythonCliHarness().run_py_compile(project, paths, run_dir, timeout)
