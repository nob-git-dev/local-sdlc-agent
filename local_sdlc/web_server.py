"""Tiny local HTTP server for the Local SDLC Agent browser UI."""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .llm_client import build_config
from .models import DEFAULT_API_KEY, MODEL_PROFILE_ALIASES, RunnerError
from .web_jobs import JobRegistry, WebConfig, _repo_entrypoint, build_cli_command, resolve_project_path


MAX_REQUEST_BYTES = 1024 * 1024
MAX_PREVIEW_BYTES = 10 * 1024 * 1024
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

    def _send_project_file(self, query: str, include_body: bool = True) -> None:
        raw_path = (parse_qs(query).get("path") or [""])[0]
        try:
            path, _relative = resolve_project_path(self.config.project, raw_path)
        except RunnerError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST, include_body=include_body)
            return
        if not path.exists() or not path.is_file():
            self._send_json({"error": "file not found"}, HTTPStatus.NOT_FOUND, include_body=include_body)
            return
        try:
            size = path.stat().st_size
        except OSError:
            self._send_json({"error": "file cannot be read"}, HTTPStatus.NOT_FOUND, include_body=include_body)
            return
        if size > MAX_PREVIEW_BYTES:
            self._send_json({"error": "file is too large to preview"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, include_body=include_body)
            return
        data = path.read_bytes() if include_body else b""
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f"inline; filename={path.name!r}")
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
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self._send_html()
            return
        if path == "/files":
            self._send_project_file(parsed.query)
            return
        if path == "/api/config":
            self._send_json(
                {
                    "host": self.config.host,
                    "port": self.config.port,
                    "project": str(self.config.project),
                    "config_file": str(self.config.config_file or ""),
                    "base_url": self.config.base_url,
                    "model": self.config.model,
                    "model_profile": self.config.model_profile,
                    "api_key_configured": self.config.api_key_configured,
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
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self._send_html(include_body=False)
            return
        if path == "/files":
            self._send_project_file(parsed.query, include_body=False)
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
            if path.startswith("/api/jobs/") and path.endswith("/approve"):
                job_id = path.removeprefix("/api/jobs/").removesuffix("/approve").strip("/")
                payload = self._read_json()
                decision_id = str(payload.get("decision_id") or "").strip()
                if not decision_id:
                    raise RunnerError("decision_id is required")
                approval = self.registry.approve(
                    job_id,
                    decision_id,
                    str(payload.get("note") or ""),
                    str(payload.get("run_dir") or ""),
                )
                self._send_json({"approval": approval})
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
    llm_config = build_config(args)
    config = WebConfig(
        host=args.host,
        port=args.port,
        project=project,
        entrypoint=entrypoint,
        config_file=Path(llm_config.config_file) if llm_config.config_file else None,
        base_url=llm_config.base_url,
        model=llm_config.model,
        model_profile=llm_config.model_profile,
        api_key_configured=bool(llm_config.api_key and llm_config.api_key != DEFAULT_API_KEY),
        open_browser=bool(args.open_browser),
    )
    return serve(config)
