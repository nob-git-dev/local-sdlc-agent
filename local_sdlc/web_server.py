"""Tiny local HTTP server for the Local SDLC Agent browser UI."""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .models import DEFAULT_BASE_URL, DEFAULT_MODEL, MODEL_PROFILE_ALIASES, RunnerError
from .web_jobs import JobRegistry, WebConfig, _repo_entrypoint, build_cli_command


MAX_REQUEST_BYTES = 1024 * 1024
ASSET_DIR = Path(__file__).resolve().parent / "web_assets"
INDEX_HTML_PATH = ASSET_DIR / "index.html"


def _index_html() -> str:
    if not INDEX_HTML_PATH.exists():
        raise RunnerError(f"web asset not found: {INDEX_HTML_PATH}")
    return INDEX_HTML_PATH.read_text(encoding="utf-8")


class AgentWebHandler(BaseHTTPRequestHandler):
    registry: JobRegistry
    config: WebConfig

    def log_message(self, format: str, *args: object) -> None:
        sys.stderr.write("web: " + format % args + "\n")

    def _send_json(
        self,
        payload: dict[str, object],
        status: HTTPStatus = HTTPStatus.OK,
        include_body: bool = True,
    ) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if include_body:
            self.wfile.write(data)

    def _send_html(self, include_body: bool = True) -> None:
        data = _index_html().encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if include_body:
            self.wfile.write(data)

    def _read_json(self) -> dict[str, object]:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise RunnerError("invalid Content-Length") from exc
        if length > MAX_REQUEST_BYTES:
            raise RunnerError("request body too large")
        body = self.rfile.read(length)
        if not body:
            return {}
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise RunnerError(f"invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise RunnerError("JSON body must be an object")
        return payload

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._send_html()
            return
        if path == "/api/config":
            self._send_json(
                {
                    "host": self.config.host,
                    "port": self.config.port,
                    "project": str(self.config.project),
                    "base_url": self.config.base_url,
                    "model": self.config.model,
                    "model_profile": self.config.model_profile,
                    "model_profiles": sorted(MODEL_PROFILE_ALIASES),
                }
            )
            return
        if path == "/api/jobs":
            self._send_json({"jobs": self.registry.list_jobs()})
            return
        if path.startswith("/api/jobs/"):
            job_id = path.removeprefix("/api/jobs/").strip("/")
            job = self.registry.get(job_id)
            if job is None:
                self._send_json({"error": "job not found"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json(job.to_dict())
            return
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_HEAD(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._send_html(include_body=False)
            return
        if path == "/api/config":
            self._send_json({"status": "ok"}, include_body=False)
            return
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND, include_body=False)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/jobs":
                payload = self._read_json()
                job = self.registry.start(payload)
                self._send_json({"job": job.to_dict(tail=20)}, HTTPStatus.CREATED)
                return
            if path.startswith("/api/jobs/") and path.endswith("/stop"):
                job_id = path.removeprefix("/api/jobs/").removesuffix("/stop").strip("/")
                stopped = self.registry.stop(job_id)
                self._send_json({"stopped": stopped})
                return
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except RunnerError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)


def serve(config: WebConfig) -> int:
    registry = JobRegistry(config)

    class BoundHandler(AgentWebHandler):
        pass

    BoundHandler.registry = registry
    BoundHandler.config = config

    server = ThreadingHTTPServer((config.host, config.port), BoundHandler)
    url = f"http://{config.host}:{server.server_port}/"
    print(f"Local SDLC Agent web UI: {url}")
    print("Press Ctrl+C to stop.")
    if config.open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nweb server stopped")
    finally:
        server.server_close()
    return 0


def command_web(args: argparse.Namespace) -> int:
    project = args.project.resolve()
    entrypoint = args.entrypoint.resolve()
    if not entrypoint.exists():
        raise RunnerError(f"entrypoint not found: {entrypoint}")
    config = WebConfig(
        host=args.host,
        port=args.port,
        project=project,
        entrypoint=entrypoint,
        base_url=args.base_url or DEFAULT_BASE_URL,
        model=args.model or DEFAULT_MODEL,
        model_profile=args.model_profile,
        open_browser=bool(args.open_browser),
    )
    return serve(config)
