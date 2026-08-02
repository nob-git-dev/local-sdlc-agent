"""HTML static checks and browser-smoke evidence adapters."""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Sequence

from ..verification import command_result_document
from .base import HarnessEvidence, evidence_from_command_result
from .browser_protocol import BrowserCheckRequest, BrowserCheckResult, BrowserProtocolError
from .browser_runtime import browser_runner_from_environment


def evidence_from_document(kind: str, name: str, document: str, ok: bool) -> HarnessEvidence:
    return evidence_from_command_result(kind, name, document, ok)


def has_tetris_initial_render_sequence(text: str) -> bool:
    """Return true when startup initializes the board immediately before rendering."""
    if re.search(r"\binitBoard\s*\(\s*\)\s*;\s*renderBoard\s*\(\s*\)\s*;", text):
        return True
    if "function createBoardCells" in text and re.search(r"\bcreateBoardCells\s*\(\s*\)\s*;", text):
        return True
    return False


def run_browser_tetris_evidence(
    project: Path,
    raw: str,
    run_dir: Path,
    timeout: float,
) -> HarnessEvidence | None:
    result = run_browser_tetris_check(project, raw, run_dir, timeout)
    if result is None:
        return None
    document, ok = result
    return evidence_from_document("browser_smoke", "browser-tetris-smoke", document, ok)


def run_browser_tetris_check(
    project: Path,
    raw: str,
    run_dir: Path,
    timeout: float,
) -> tuple[str, bool] | None:
    del run_dir  # Kept in the public signature for compatibility.
    started = time.monotonic()
    try:
        runner = browser_runner_from_environment(project=project)
        if runner is None:
            result = BrowserCheckResult(
                69,
                "",
                "verification infrastructure: no browser backend is available",
                time.monotonic() - started,
            )
        else:
            request = BrowserCheckRequest.create(project, raw, timeout)
            result = runner.run(request)
    except BrowserProtocolError as exc:
        result_document = command_result_document(
            "browser-tetris-smoke",
            65,
            "",
            f"invalid browser runner configuration: {exc}",
            time.monotonic() - started,
        )
        return result_document, False
    document = command_result_document(
        "browser-tetris-smoke",
        result.returncode,
        result.stdout,
        result.stderr,
        result.duration_seconds,
    )
    return document, result.ok


class HtmlBrowserHarness:
    name = "html_browser"

    def run(
        self,
        project: Path,
        paths: Sequence[str],
        run_dir: Path,
        timeout: float,
        tetris_checks: bool = False,
    ) -> list[HarnessEvidence]:
        return self.run_html_smoke(project, paths, run_dir, timeout, tetris_checks=tetris_checks)

    def run_html_smoke(
        self,
        project: Path,
        paths: Sequence[str],
        run_dir: Path,
        timeout: float,
        tetris_checks: bool = False,
    ) -> list[HarnessEvidence]:
        evidence: list[HarnessEvidence] = []
        node = shutil.which("node")
        for raw in paths:
            if not raw.lower().endswith((".html", ".htm")):
                continue
            path = (project / raw).resolve()
            try:
                path.relative_to(project.resolve())
            except ValueError:
                continue
            if not path.exists():
                document = command_result_document(f"html-smoke {raw}", 1, "", "file not found", 0.0)
                evidence.append(evidence_from_document("html_smoke", f"html-smoke {raw}", document, False))
                continue

            started = time.monotonic()
            text = path.read_text(encoding="utf-8", errors="replace")
            failures: list[str] = []
            if "<script" in text.lower() and "</script>" not in text.lower():
                failures.append("inline script tag is not closed")
            if "</html>" not in text.lower():
                failures.append("closing </html> tag is missing")
            if tetris_checks:
                required_fragments = [
                    "function startGame",
                    "function gameLoop",
                    "function movePiece",
                    "function rotate",
                    "function softDrop",
                    "function hardDrop",
                    "function gameOver",
                    "function clearLines",
                    "keydown",
                    "start-btn",
                ]
                for fragment in required_fragments:
                    if fragment not in text:
                        failures.append(f"missing tetris fragment: {fragment}")
                if not has_tetris_initial_render_sequence(text):
                    failures.append(
                        "initial board render is missing; initialize the DOM board on load or "
                        "call initBoard(); renderBoard(); at startup"
                    )
            scripts = re.findall(
                r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                text,
                flags=re.DOTALL | re.IGNORECASE,
            )
            stdout_parts = [
                f"file: {raw}",
                f"bytes: {path.stat().st_size}",
                f"inline_scripts: {len(scripts)}",
            ]
            stderr_parts = failures[:]
            ok = not failures

            if node and scripts:
                for index, script in enumerate(scripts, start=1):
                    js_path = run_dir / f"html-smoke-{Path(raw).name}-{index}.js"
                    js_path.write_text(script, encoding="utf-8")
                    result = subprocess.run(
                        [node, "--check", str(js_path)],
                        cwd=project,
                        text=True,
                        capture_output=True,
                        timeout=timeout,
                        check=False,
                    )
                    stdout_parts.append(result.stdout.strip())
                    if result.returncode != 0:
                        ok = False
                        stderr_parts.append(result.stderr.strip())

            duration = time.monotonic() - started
            document = command_result_document(
                f"html-smoke {raw}",
                0 if ok else 1,
                "\n".join(part for part in stdout_parts if part),
                "\n".join(part for part in stderr_parts if part),
                duration,
            )
            evidence.append(evidence_from_document("html_smoke", f"html-smoke {raw}", document, ok))
            if tetris_checks:
                browser_evidence = run_browser_tetris_evidence(project, raw, run_dir, timeout)
                if browser_evidence is not None:
                    evidence.append(browser_evidence)
        return evidence


def run_html_smoke_evidence(
    project: Path,
    paths: Sequence[str],
    run_dir: Path,
    timeout: float,
    tetris_checks: bool = False,
) -> list[HarnessEvidence]:
    return HtmlBrowserHarness().run_html_smoke(project, paths, run_dir, timeout, tetris_checks=tetris_checks)


def run_html_smoke_checks(
    project: Path,
    paths: Sequence[str],
    run_dir: Path,
    timeout: float,
    tetris_checks: bool = False,
) -> list[tuple[str, bool]]:
    return [item.to_legacy_result() for item in run_html_smoke_evidence(project, paths, run_dir, timeout, tetris_checks)]
