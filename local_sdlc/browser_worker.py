"""Authenticated localhost browser worker for constrained agent sandboxes."""

from __future__ import annotations

import argparse
import dataclasses
import hmac
import http.server
import json
import os
import shutil
import threading
from collections.abc import Callable
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlparse

from .harnesses.browser_protocol import (
    BROWSER_PROTOCOL_VERSION,
    MAX_BROWSER_REQUEST_BYTES,
    BrowserCheckRequest,
    BrowserCheckResult,
    BrowserProtocolError,
)
from .harnesses.browser_runtime import (
    BROWSER_EXECUTABLE_ENV,
    BROWSER_WORKER_TOKEN_ENV,
    LocalBrowserRunner,
)
from .models import RunnerError


BrowserExecute = Callable[[BrowserCheckRequest], BrowserCheckResult]


def _browser_available(executable: str) -> bool:
    path = Path(executable)
    return path.is_file() if path.is_absolute() else bool(shutil.which(executable))


@dataclasses.dataclass(frozen=True)
class BrowserWorkerConfig:
    host: str
    port: int
    allowed_roots: tuple[Path, ...]
    token: str
    browser_executable: str
    max_concurrency: int = 1

    def validated(self) -> "BrowserWorkerConfig":
        if self.host not in {"127.0.0.1", "localhost", "::1"}:
            raise RunnerError("browser worker must bind to a loopback host")
        if not 0 <= self.port <= 65535:
            raise RunnerError("browser worker port must be between 0 and 65535")
        if not self.token:
            raise RunnerError("browser worker token is required")
        if self.max_concurrency < 1 or self.max_concurrency > 8:
            raise RunnerError("browser worker max concurrency must be between 1 and 8")
        roots: list[Path] = []
        for root in self.allowed_roots:
            resolved = root.expanduser().resolve()
            if not resolved.is_dir():
                raise RunnerError(f"browser worker allowed root is not a directory: {resolved}")
            roots.append(resolved)
        if not roots:
            raise RunnerError("browser worker requires at least one allowed root")
        executable = self.browser_executable.strip()
        if not executable:
            raise RunnerError("browser worker executable is required")
        return dataclasses.replace(self, allowed_roots=tuple(roots), browser_executable=executable)


class BrowserWorkerHandler(http.server.BaseHTTPRequestHandler):
    config: BrowserWorkerConfig
    execute: BrowserExecute
    semaphore: threading.BoundedSemaphore

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_json(
        self,
        payload: dict[str, object],
        status: HTTPStatus = HTTPStatus.OK,
        *,
        include_body: bool = True,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if include_body else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return False
        return hmac.compare_digest(header[len(prefix) :], self.config.token)

    def _read_payload(self) -> dict[str, object]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise BrowserProtocolError("Content-Length is required")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise BrowserProtocolError("invalid Content-Length") from exc
        if length < 1:
            raise BrowserProtocolError("request body is required")
        if length > MAX_BROWSER_REQUEST_BYTES:
            raise BrowserProtocolError("browser request is too large", status_code=413)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BrowserProtocolError(f"invalid JSON request: {exc}") from exc
        if not isinstance(payload, dict):
            raise BrowserProtocolError("browser request must be an object")
        return payload

    def do_GET(self) -> None:
        if urlparse(self.path).path != "/health":
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        executable = Path(self.config.browser_executable)
        available = _browser_available(self.config.browser_executable)
        self._send_json(
            {
                "status": "ok" if available else "degraded",
                "schema_version": BROWSER_PROTOCOL_VERSION,
                "browser_available": available,
                "browser": executable.name,
                "max_concurrency": self.config.max_concurrency,
            }
        )

    def do_HEAD(self) -> None:
        if urlparse(self.path).path == "/health":
            self._send_json({"status": "ok"}, include_body=False)
            return
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND, include_body=False)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/v1/checks":
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        if not self._authorized():
            self._send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return
        if not _browser_available(self.config.browser_executable):
            self._send_json(
                {"error": "browser executable is unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        if not self.semaphore.acquire(blocking=False):
            self._send_json({"error": "worker busy"}, HTTPStatus.TOO_MANY_REQUESTS)
            return
        try:
            payload = self._read_payload()
            request = BrowserCheckRequest.from_payload(payload, self.config.allowed_roots)
            result = self.execute(request)
            self._send_json(result.to_payload())
        except BrowserProtocolError as exc:
            status = HTTPStatus(exc.status_code)
            self._send_json({"error": str(exc)}, status)
        except Exception:
            self._send_json({"error": "browser worker internal error"}, HTTPStatus.INTERNAL_SERVER_ERROR)
        finally:
            self.semaphore.release()


def create_browser_worker_server(
    config: BrowserWorkerConfig,
    *,
    execute: BrowserExecute | None = None,
) -> http.server.ThreadingHTTPServer:
    validated = config.validated()
    runner = LocalBrowserRunner(validated.browser_executable)

    class BoundHandler(BrowserWorkerHandler):
        pass

    BoundHandler.config = validated
    BoundHandler.execute = staticmethod(execute or runner.run)
    BoundHandler.semaphore = threading.BoundedSemaphore(validated.max_concurrency)
    server = http.server.ThreadingHTTPServer((validated.host, validated.port), BoundHandler)
    server.daemon_threads = True
    return server


def serve_browser_worker(config: BrowserWorkerConfig) -> int:
    server = create_browser_worker_server(config)
    host, port = server.server_address[:2]
    print(f"Local SDLC browser worker: http://{host}:{port}")
    print("The worker accepts only allowlisted checks under configured roots.")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbrowser worker stopped")
    finally:
        server.server_close()
    return 0


def command_browser_worker(args: argparse.Namespace) -> int:
    token_env = str(args.token_env).strip()
    token = os.environ.get(token_env, "")
    if not token:
        raise RunnerError(f"browser worker token environment variable is not set: {token_env}")
    executable = str(args.browser or os.environ.get(BROWSER_EXECUTABLE_ENV, "")).strip()
    if not executable:
        executable = shutil.which("chromium") or shutil.which("google-chrome") or shutil.which("chromium-browser") or ""
    roots = tuple(args.allowed_root or [args.project])
    return serve_browser_worker(
        BrowserWorkerConfig(
            host=args.host,
            port=args.port,
            allowed_roots=roots,
            token=token,
            browser_executable=executable,
            max_concurrency=args.max_concurrency,
        )
    )


def add_browser_worker_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "browser-worker",
        help="serve an authenticated localhost browser verification worker",
    )
    parser.add_argument("--project", type=Path, default=Path.cwd(), help="default allowed project root")
    parser.add_argument("--host", default="127.0.0.1", help="loopback bind host")
    parser.add_argument("--port", type=int, default=8766, help="bind port; use 0 for an ephemeral port")
    parser.add_argument("--allowed-root", action="append", type=Path, default=[], help="readable project root; repeatable")
    parser.add_argument("--browser", default="", help="fixed Chromium-compatible executable")
    parser.add_argument("--token-env", default=BROWSER_WORKER_TOKEN_ENV, help="environment variable containing the Bearer token")
    parser.add_argument("--max-concurrency", type=int, default=1, help="maximum simultaneous checks (1-8)")
    parser.set_defaults(func=command_browser_worker)
