"""HTML and browser smoke harnesses."""

from __future__ import annotations

import functools
import http.server
import json
import re
import shutil
import subprocess
import textwrap
import threading
import time
from pathlib import Path
from typing import Sequence
from urllib.parse import quote

from ..verification import classify_failure, command_result_document, parse_command_result_document
from ..workspace import resolve_project_path
from .base import HarnessEvidence


def evidence_from_document(kind: str, name: str, document: str, ok: bool) -> HarnessEvidence:
    parsed = parse_command_result_document(document)
    try:
        exit_code = int(parsed.get("exit_code", "0" if ok else "1"))
    except ValueError:
        exit_code = 0 if ok else 1
    try:
        duration = float(parsed.get("duration_seconds", "0") or 0.0)
    except ValueError:
        duration = 0.0
    stdout_payload: dict[str, object] = {}
    stdout = parsed.get("stdout", "").strip()
    if stdout.startswith("{"):
        try:
            loaded = json.loads(stdout)
            if isinstance(loaded, dict):
                stdout_payload = loaded
        except json.JSONDecodeError:
            stdout_payload = {}
    covers = tuple(str(item) for item in stdout_payload.get("covers", []) if isinstance(item, str))
    observations = stdout_payload.get("observations", {})
    if not isinstance(observations, dict):
        observations = {}
    return HarnessEvidence(
        kind=kind,
        name=name,
        status="pass" if ok else "fail",
        command=parsed.get("command", name),
        exit_code=exit_code,
        duration_seconds=duration,
        document=document,
        failure_type=None
        if ok
        else classify_failure(exit_code, parsed.get("stdout", ""), parsed.get("stderr", ""), parsed.get("blocked_reason")),
        covers=covers,
        observations={str(key): value for key, value in observations.items()},
    )


def has_tetris_initial_render_sequence(text: str) -> bool:
    """Return true when startup initializes the board immediately before rendering.

    This is a structural static check, not a formatting or naming check. It
    accepts both the older initBoard/renderBoard startup pattern and DOM-cell
    implementations that create the visible board cells directly on load.
    """
    if re.search(r"\binitBoard\s*\(\s*\)\s*;\s*renderBoard\s*\(\s*\)\s*;", text):
        return True
    if "function createBoardCells" in text and re.search(r"\bcreateBoardCells\s*\(\s*\)\s*;", text):
        return True
    return False


def run_browser_tetris_evidence(project: Path, raw: str, run_dir: Path, timeout: float) -> HarnessEvidence | None:
    result = run_browser_tetris_check(project, raw, run_dir, timeout)
    if result is None:
        return None
    document, ok = result
    return evidence_from_document("browser_smoke", "browser-tetris-smoke", document, ok)


def run_browser_tetris_check(project: Path, raw: str, run_dir: Path, timeout: float) -> tuple[str, bool] | None:
    chromium = shutil.which("chromium") or shutil.which("google-chrome") or shutil.which("chromium-browser")
    if not chromium:
        return None

    project_root = project.resolve()
    path = resolve_project_path(project, raw)
    target_rel = path.resolve().relative_to(project_root)
    harness = run_dir.resolve() / f"browser-smoke-{Path(raw).name}.html"
    try:
        harness_rel = harness.relative_to(project_root)
    except ValueError:
        harness = project_root / ".sdlc-runner" / "browser-smoke" / harness.name
        harness.parent.mkdir(parents=True, exist_ok=True)
        harness_rel = harness.relative_to(project_root)

    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

    handler = functools.partial(QuietHandler, directory=str(project_root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    file_url = f"{base_url}/{quote(target_rel.as_posix(), safe='/')}"
    harness_url = f"{base_url}/{quote(harness_rel.as_posix(), safe='/')}"

    started = time.monotonic()
    try:
        harness.write_text(
            textwrap.dedent(
                f"""
            <!doctype html>
            <meta charset="utf-8">
            <pre id="result">pending</pre>
            <iframe id="game" src="{file_url}"></iframe>
            <script>
            const failures = [];
            const covers = ['browser_smoke', 'html_visible', 'required_dom', 'required_window_functions', 'board_200_cells', 'start_button'];
            const observations = {{}};
            function finish() {{
              document.getElementById('result').textContent =
                '__TETRIS_RESULT__' + JSON.stringify({{ ok: failures.length === 0, failures, covers, observations }});
            }}
            window.onerror = (message) => {{
              failures.push('window error: ' + message);
              finish();
            }};
            function visibleCellIndexes(d, w) {{
              return Array.from(d.querySelectorAll('#game-board .cell'))
                .map((cell, index) => {{
                  const style = w.getComputedStyle(cell);
                  return {{
                    index,
                    background: style.backgroundColor,
                    className: cell.className
                  }};
                }})
                .filter((item) =>
                  item.background &&
                  item.background !== 'rgb(0, 0, 0)' &&
                  item.background !== 'rgba(0, 0, 0, 0)' &&
                  item.background !== 'transparent'
                )
                .map((item) => item.index);
            }}
            function sameIndexes(left, right) {{
              return left.length === right.length && left.every((value, index) => value === right[index]);
            }}
            function runChecks() {{
              try {{
                const frame = document.getElementById('game');
                const w = frame.contentWindow;
                const d = frame.contentDocument;
                ['start-btn', 'game-board', 'score', 'level', 'lines'].forEach((id) => {{
                  if (!d.getElementById(id)) failures.push('missing #' + id);
                }});
                ['startGame', 'gameLoop', 'movePiece', 'rotate', 'softDrop', 'hardDrop', 'gameOver', 'clearLines'].forEach((name) => {{
                  if (typeof w[name] !== 'function') failures.push('missing function ' + name);
                }});
                const board = d.getElementById('game-board');
                if (board && board.querySelectorAll('.cell').length !== 200) failures.push('board does not have 200 cells');
                const button = d.getElementById('start-btn');
                const beforeStart = visibleCellIndexes(d, w);
                if (button) button.click();
                setTimeout(() => {{
                  try {{
                    const afterStart = visibleCellIndexes(d, w);
                    observations.before_start_visible_cells = beforeStart.length;
                    observations.after_start_visible_cells = afterStart.length;
                    observations.after_start_indexes = afterStart;
                    if (afterStart.length === 0) {{
                      failures.push('active piece is not visible after start');
                    }} else {{
                      covers.push('active_piece_visible');
                    }}
                    d.dispatchEvent(new w.KeyboardEvent('keydown', {{ key: 'ArrowLeft', bubbles: true }}));
                    setTimeout(() => {{
                      try {{
                        const afterLeft = visibleCellIndexes(d, w);
                        observations.after_left_visible_cells = afterLeft.length;
                        observations.after_left_indexes = afterLeft;
                        if (afterStart.length > 0 && sameIndexes(afterStart, afterLeft)) {{
                          failures.push('active piece did not visibly move after ArrowLeft');
                        }} else if (afterStart.length > 0) {{
                          covers.push('keyboard_move');
                          covers.push('keyboard_interaction');
                        }}
                        ['ArrowRight', 'ArrowUp', 'ArrowDown', ' ', 'p'].forEach((key) => {{
                          d.dispatchEvent(new w.KeyboardEvent('keydown', {{ key, bubbles: true }}));
                        }});
                        if (typeof w.gameOver === 'function') w.gameOver();
                        const title = d.querySelector('.overlay-title');
                        if (title && title.textContent !== 'GAME OVER') failures.push('gameOver did not show GAME OVER');
                        if (title && title.textContent === 'GAME OVER') covers.push('game_over');
                        finish();
                      }} catch (error) {{
                        failures.push('interaction error: ' + error.message);
                        finish();
                      }}
                    }}, 150);
                  }} catch (error) {{
                    failures.push('interaction error: ' + error.message);
                    finish();
                  }}
                }}, 300);
              }} catch (error) {{
                failures.push('setup error: ' + error.message);
                finish();
              }}
            }}
            const frame = document.getElementById('game');
            frame.addEventListener('load', () => setTimeout(runChecks, 100));
            setTimeout(() => {{
              if (document.getElementById('result').textContent === 'pending') {{
                failures.push('iframe did not finish loading in time');
                finish();
              }}
            }}, 2500);
            </script>
            """
            ).strip(),
            encoding="utf-8",
        )
        try:
            result = subprocess.run(
                [
                    chromium,
                    "--headless=new",
                    "--no-sandbox",
                    "--disable-gpu",
                    "--allow-file-access-from-files",
                    "--disable-web-security",
                    "--virtual-time-budget=3000",
                    "--dump-dom",
                    harness_url,
                ],
                cwd=project,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - started
            stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", errors="replace")
            stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", errors="replace")
            return command_result_document("browser-tetris-smoke", 124, stdout, stderr, duration), False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)

    duration = time.monotonic() - started
    marker = "__TETRIS_RESULT__"
    marker_index = result.stdout.find(marker)
    if marker_index < 0:
        return command_result_document(
            "browser-tetris-smoke",
            1,
            result.stdout,
            "browser smoke did not produce a __TETRIS_RESULT__ marker\n" + result.stderr,
            duration,
        ), False
    try:
        payload, _end = json.JSONDecoder().raw_decode(result.stdout[marker_index + len(marker) :].lstrip())
    except json.JSONDecodeError as exc:
        return command_result_document("browser-tetris-smoke", 1, result.stdout, str(exc), duration), False

    ok = bool(payload.get("ok"))
    stdout = json.dumps(payload, ensure_ascii=False, indent=2)
    stderr = result.stderr if ok else "\n".join(str(item) for item in payload.get("failures", []))
    return command_result_document("browser-tetris-smoke", 0 if ok else 1, stdout, stderr, duration), ok


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
                        "initial board render is missing; initialize the DOM board on load or call initBoard(); renderBoard(); at startup"
                    )
            scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", text, flags=re.DOTALL | re.IGNORECASE)
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
