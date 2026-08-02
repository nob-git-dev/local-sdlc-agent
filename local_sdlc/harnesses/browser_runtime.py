"""Fixed local and authenticated remote browser execution backends."""

from __future__ import annotations

import functools
import html
import http.server
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import tempfile
import textwrap
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Mapping, Protocol
from urllib.parse import quote, urlparse

from ..config import config_string, load_app_config
from .browser_protocol import (
    MAX_BROWSER_RESPONSE_BYTES,
    BrowserCheckRequest,
    BrowserCheckResult,
    BrowserProtocolError,
    TETRIS_DOM_CHECK,
    truncate_browser_output,
)


BROWSER_WORKER_URL_ENV = "LOCAL_SDLC_BROWSER_WORKER_URL"
BROWSER_WORKER_TOKEN_ENV = "LOCAL_SDLC_BROWSER_WORKER_TOKEN"
BROWSER_EXECUTABLE_ENV = "LOCAL_SDLC_BROWSER_EXECUTABLE"
HARNESS_PATH = "/__local_sdlc_browser_check__.html"
RESULT_MARKER = "__TETRIS_RESULT__"


class BrowserRunner(Protocol):
    def run(self, request: BrowserCheckRequest) -> BrowserCheckResult:
        """Execute one allowlisted browser check."""


def build_tetris_harness(target_url: str) -> str:
    """Build the fixed DOM/interaction probe for the Tetris contract."""
    escaped_url = html.escape(target_url, quote=True)
    return textwrap.dedent(
        f"""
        <!doctype html>
        <meta charset="utf-8">
        <pre id="result">pending</pre>
        <iframe id="game" src="{escaped_url}"></iframe>
        <script>
        const failures = [];
        const covers = ['browser_smoke', 'html_visible', 'required_dom', 'required_window_functions', 'board_200_cells', 'start_button'];
        const observations = {{}};
        function finish() {{
          document.getElementById('result').textContent =
            '{RESULT_MARKER}' + JSON.stringify({{ ok: failures.length === 0, failures, covers, observations }});
        }}
        window.onerror = (message) => {{
          failures.push('window error: ' + message);
          finish();
        }};
        function visibleCellIndexes(d, w) {{
          return Array.from(d.querySelectorAll('#game-board .cell'))
            .map((cell, index) => {{
              const style = w.getComputedStyle(cell);
              return {{ index, background: style.backgroundColor, className: cell.className }};
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
    ).strip()


def _browser_environment() -> dict[str, str]:
    allowed = {
        "DBUS_SESSION_BUS_ADDRESS",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "TMPDIR",
        "XDG_RUNTIME_DIR",
    }
    return {key: value for key, value in os.environ.items() if key in allowed}


def _text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _result_from_browser_process(
    returncode: int,
    stdout: str,
    stderr: str,
    duration: float,
) -> BrowserCheckResult:
    marker_index = stdout.find(RESULT_MARKER)
    if marker_index < 0:
        infrastructure_markers = (
            "cannot create transient scope",
            "timeout waiting for snap system profiles",
            "running \"chromium\" failed",
        )
        prefix = (
            "verification infrastructure: browser runtime unavailable\n"
            if any(marker in stderr.lower() for marker in infrastructure_markers)
            else ""
        )
        return BrowserCheckResult(
            returncode=returncode or 1,
            stdout=truncate_browser_output(stdout),
            stderr=truncate_browser_output(
                prefix + f"browser smoke did not produce a {RESULT_MARKER} marker\n" + stderr
            ),
            duration_seconds=duration,
        )
    try:
        payload, _end = json.JSONDecoder().raw_decode(stdout[marker_index + len(RESULT_MARKER) :].lstrip())
    except json.JSONDecodeError as exc:
        return BrowserCheckResult(
            returncode=1,
            stdout=truncate_browser_output(stdout),
            stderr=f"invalid browser result JSON: {exc}",
            duration_seconds=duration,
        )
    if not isinstance(payload, dict):
        return BrowserCheckResult(
            1,
            truncate_browser_output(stdout),
            "browser result must be an object",
            duration,
        )
    failures = payload.get("failures", [])
    if not isinstance(failures, list):
        failures = ["browser result failures must be a list"]
    ok = bool(payload.get("ok")) and returncode == 0 and not failures
    normalized_stdout = json.dumps(payload, ensure_ascii=False, indent=2)
    normalized_stderr = stderr if ok else "\n".join(str(item) for item in failures)
    if returncode != 0:
        normalized_stderr = (normalized_stderr + f"\nbrowser exited with status {returncode}\n" + stderr).strip()
    return BrowserCheckResult(
        returncode=0 if ok else (returncode or 1),
        stdout=truncate_browser_output(normalized_stdout),
        stderr=truncate_browser_output(normalized_stderr),
        duration_seconds=duration,
    )


class LocalBrowserRunner:
    """Run a fixed browser check without accepting arbitrary arguments."""

    def __init__(self, executable: str):
        self.executable = executable

    def run(self, request: BrowserCheckRequest) -> BrowserCheckResult:
        if request.check != TETRIS_DOM_CHECK:
            return BrowserCheckResult(65, "", f"unknown browser check: {request.check}", 0.0)

        class CheckHandler(http.server.SimpleHTTPRequestHandler):
            harness: bytes = b""

            def log_message(self, format: str, *args: object) -> None:
                return

            def end_headers(self) -> None:
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self' data: blob:; "
                    "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                    "img-src 'self' data: blob:; connect-src 'none'; frame-src 'self'; "
                    "object-src 'none'; base-uri 'none'; form-action 'none'",
                )
                self.send_header("Cache-Control", "no-store")
                super().end_headers()

            def do_GET(self) -> None:
                if urlparse(self.path).path == HARNESS_PATH:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(self.harness)))
                    self.end_headers()
                    self.wfile.write(self.harness)
                    return
                super().do_GET()

        handler = functools.partial(CheckHandler, directory=str(request.project))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        target_url = f"http://127.0.0.1:{server.server_port}/{quote(request.entrypoint, safe='/')}"
        CheckHandler.harness = build_tetris_harness(target_url).encode("utf-8")
        harness_url = f"http://127.0.0.1:{server.server_port}{HARNESS_PATH}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        started = time.monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix="local-sdlc-browser-") as profile_dir:
                command = [
                    self.executable,
                    "--headless=new",
                    "--disable-gpu",
                    "--disable-dev-shm-usage",
                    "--disable-background-networking",
                    "--disable-component-update",
                    "--disable-extensions",
                    "--disable-sync",
                    "--metrics-recording-only",
                    "--no-first-run",
                    f"--user-data-dir={profile_dir}",
                    "--virtual-time-budget=3000",
                    "--dump-dom",
                    harness_url,
                ]
                try:
                    completed = subprocess.run(
                        command,
                        cwd=request.project,
                        text=True,
                        capture_output=True,
                        timeout=request.timeout_seconds,
                        check=False,
                        env=_browser_environment(),
                    )
                    duration = time.monotonic() - started
                    return _result_from_browser_process(
                        completed.returncode,
                        completed.stdout,
                        completed.stderr,
                        duration,
                    )
                except subprocess.TimeoutExpired as exc:
                    duration = time.monotonic() - started
                    return BrowserCheckResult(
                        124,
                        truncate_browser_output(_text(exc.stdout)),
                        truncate_browser_output(
                            (_text(exc.stderr) + f"\nbrowser check timed out after {request.timeout_seconds:g}s").strip()
                        ),
                        duration,
                    )
                except OSError as exc:
                    return BrowserCheckResult(127, "", f"browser executable failed: {exc}", time.monotonic() - started)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1.0)


def _validated_worker_endpoint(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password:
        raise BrowserProtocolError("browser worker URL must be an unauthenticated http loopback URL")
    host = parsed.hostname
    if host != "localhost":
        try:
            if not ipaddress.ip_address(host).is_loopback:
                raise BrowserProtocolError("browser worker URL must use a loopback host")
        except ValueError as exc:
            raise BrowserProtocolError("browser worker URL must use a loopback host") from exc
    if parsed.query or parsed.fragment:
        raise BrowserProtocolError("browser worker URL must not contain query or fragment data")
    return value.rstrip("/")


class RemoteBrowserRunner:
    """Call the bounded localhost worker and never fall back to local execution."""

    def __init__(self, endpoint: str, token: str):
        self.endpoint = _validated_worker_endpoint(endpoint)
        self.token = token

    def run(self, request: BrowserCheckRequest) -> BrowserCheckResult:
        if not self.token:
            return BrowserCheckResult(
                77,
                "",
                "verification infrastructure: browser worker token is not configured",
                0.0,
            )
        body = json.dumps(request.to_payload()).encode("utf-8")
        http_request = urllib.request.Request(
            f"{self.endpoint}/v1/checks",
            data=body,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(http_request, timeout=request.timeout_seconds + 2.0) as response:
                raw_response = response.read(MAX_BROWSER_RESPONSE_BYTES + 1)
            if len(raw_response) > MAX_BROWSER_RESPONSE_BYTES:
                raise BrowserProtocolError("browser worker response is too large")
            payload = json.loads(raw_response.decode("utf-8"))
            if not isinstance(payload, dict):
                raise BrowserProtocolError("browser worker returned a non-object response")
            return BrowserCheckResult.from_payload(payload)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                message = "browser worker authentication failed"
            elif exc.code == 429:
                message = "browser worker is busy"
            else:
                message = f"browser worker rejected request with HTTP {exc.code}"
            return BrowserCheckResult(
                69,
                "",
                f"verification infrastructure: {message}",
                time.monotonic() - started,
            )
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            reason = getattr(exc, "reason", exc)
            return BrowserCheckResult(
                69,
                "",
                f"verification infrastructure: browser worker unavailable: {reason}",
                time.monotonic() - started,
            )
        except (BrowserProtocolError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            return BrowserCheckResult(
                65,
                "",
                f"verification infrastructure: invalid browser worker response: {exc}",
                time.monotonic() - started,
            )


def browser_runner_from_environment(
    environment: Mapping[str, str] | None = None,
    *,
    project: Path | None = None,
) -> BrowserRunner | None:
    env = environment or os.environ
    browser_config: Mapping[str, object] = {}
    if project is not None:
        loaded = load_app_config(project.resolve())
        raw_browser_config = loaded.root.get("browser", {})
        if raw_browser_config is not None and not isinstance(raw_browser_config, dict):
            raise BrowserProtocolError("config key 'browser' must be an object")
        browser_config = raw_browser_config or {}
    endpoint = str(env.get(BROWSER_WORKER_URL_ENV, "")).strip() or config_string(
        dict(browser_config), "worker_url"
    ) or ""
    if endpoint:
        token_env = config_string(dict(browser_config), "worker_token_env") or BROWSER_WORKER_TOKEN_ENV
        token = str(env.get(BROWSER_WORKER_TOKEN_ENV, "")).strip() or str(env.get(token_env, "")).strip()
        return RemoteBrowserRunner(endpoint, token)
    executable = str(env.get(BROWSER_EXECUTABLE_ENV, "")).strip() or config_string(
        dict(browser_config), "executable"
    ) or ""
    if not executable:
        executable = shutil.which("chromium") or shutil.which("google-chrome") or shutil.which("chromium-browser") or ""
    return LocalBrowserRunner(executable) if executable else None


def browser_backend_status(
    environment: Mapping[str, str] | None = None,
    *,
    project: Path | None = None,
    timeout: float = 2.0,
) -> dict[str, object]:
    """Return a token-free health summary for doctor output."""
    env = environment or os.environ
    browser_config: Mapping[str, object] = {}
    if project is not None:
        try:
            loaded = load_app_config(project.resolve())
            raw_browser_config = loaded.root.get("browser", {})
            if isinstance(raw_browser_config, dict):
                browser_config = raw_browser_config
        except Exception:
            browser_config = {}
    endpoint = str(env.get(BROWSER_WORKER_URL_ENV, "")).strip() or config_string(
        dict(browser_config), "worker_url"
    ) or ""
    if endpoint:
        try:
            validated = _validated_worker_endpoint(endpoint)
            with urllib.request.urlopen(f"{validated}/health", timeout=timeout) as response:
                payload = json.load(response)
            if not isinstance(payload, dict):
                raise ValueError("health response is not an object")
            return {
                "backend": "remote",
                "status": str(payload.get("status") or "unknown"),
                "browser_available": bool(payload.get("browser_available")),
                "protocol_version": payload.get("schema_version"),
            }
        except Exception as exc:
            return {
                "backend": "remote",
                "status": "unreachable",
                "browser_available": False,
                "error": type(exc).__name__,
            }
    executable = str(env.get(BROWSER_EXECUTABLE_ENV, "")).strip() or config_string(
        dict(browser_config), "executable"
    ) or ""
    if not executable:
        executable = shutil.which("chromium") or shutil.which("google-chrome") or shutil.which("chromium-browser") or ""
    return {
        "backend": "local",
        "status": "ok" if executable else "unavailable",
        "browser_available": bool(executable),
        "browser": Path(executable).name if executable else "",
    }
