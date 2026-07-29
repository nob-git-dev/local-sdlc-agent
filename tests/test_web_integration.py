import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tests.helpers import ENTRYPOINT_PATH, ROOT


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _url_json(url: str, timeout: float = 2.0) -> dict[str, object]:
    data = urllib.request.urlopen(url, timeout=timeout).read().decode("utf-8")
    loaded = json.loads(data)
    return loaded if isinstance(loaded, dict) else {}


def _wait_json(url: str, timeout_seconds: float = 10.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return _url_json(url, timeout=0.5)
        except Exception as exc:
            last_error = exc
            time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {url}: {last_error}")


def _start_fake_llm(port: int) -> ThreadingHTTPServer:
    html_artifact = """BEGIN_FILE: index.html
<!doctype html>
<html lang="ja">
<head><meta charset="utf-8"><title>Smoke Game</title></head>
<body><main id="app">OK</main><script>window.smokeReady = true;</script></body>
</html>
END_FILE"""

    class FakeLLMHandler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _send_json(self, payload: dict[str, object], status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path.endswith("/models"):
                self._send_json({"data": [{"id": "fake-web-fullsite"}]})
            else:
                self._send_json({"error": "not found"}, 404)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            text = "\n".join(str(message.get("content", "")) for message in payload.get("messages", []))
            if "judge-level agent" in text:
                content = "判定: 承認\n\nEvidence: command evidence passes and index.html exists."
            elif "BEGIN_FILE" in text or "artifact" in text.lower() or "coder" in text.lower():
                content = html_artifact
            else:
                content = "PM_CONTROL\n- target: index.html\n- acceptance: create a previewable HTML file."
            self._send_json(
                {
                    "id": "fake",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": content},
                            "finish_reason": "stop",
                        }
                    ],
                }
            )

    server = ThreadingHTTPServer(("127.0.0.1", port), FakeLLMHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class WebIntegrationTest(unittest.TestCase):
    def test_web_job_runs_agent_generates_artifact_and_serves_preview(self):
        llm_port = _free_port()
        web_port = _free_port()
        fake_llm = _start_fake_llm(llm_port)
        web_process: subprocess.Popen[str] | None = None
        with tempfile.TemporaryDirectory(prefix="local-sdlc-web-integration-") as temp:
            project = Path(temp)
            try:
                web_process = subprocess.Popen(
                    [
                        sys.executable,
                        str(ENTRYPOINT_PATH),
                        "web",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(web_port),
                        "--project",
                        str(project),
                        "--base-url",
                        f"http://127.0.0.1:{llm_port}/v1",
                        "--model",
                        "fake-web-fullsite",
                        "--model-profile",
                        "default",
                    ],
                    cwd=str(ROOT),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                _wait_json(f"http://127.0.0.1:{web_port}/api/config")
                payload = {
                    "mode": "agent",
                    "brief": "ブラウザで遊べるHTMLゲームを作って",
                    "test_command": [
                        "python3 -c \"from pathlib import Path; p=Path('index.html'); assert p.exists(); assert 'smokeReady' in p.read_text()\""
                    ],
                    "apply": True,
                }
                request = urllib.request.Request(
                    f"http://127.0.0.1:{web_port}/api/jobs",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                created = json.loads(urllib.request.urlopen(request, timeout=5).read().decode("utf-8"))
                job_id = created["job"]["id"]

                final: dict[str, object] = {}
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    final = _url_json(f"http://127.0.0.1:{web_port}/api/jobs/{job_id}")
                    if final.get("status") in {"completed", "failed", "stopped"}:
                        break
                    time.sleep(0.2)

                self.assertEqual(final.get("status"), "completed", json.dumps(final, ensure_ascii=False, indent=2))
                result = final.get("result")
                self.assertIsInstance(result, dict)
                artifacts = result.get("artifacts")
                self.assertIsInstance(artifacts, list)
                previewable = [
                    item
                    for item in artifacts
                    if isinstance(item, dict)
                    and item.get("path") == "index.html"
                    and item.get("exists")
                    and item.get("previewable")
                ]
                self.assertTrue(previewable, json.dumps(final, ensure_ascii=False, indent=2))
                preview_body = urllib.request.urlopen(
                    f"http://127.0.0.1:{web_port}{previewable[0]['preview_url']}",
                    timeout=5,
                ).read().decode("utf-8")
                self.assertIn("smokeReady", preview_body)
                self.assertIn("created_spec_file:", final.get("output", ""))
                self.assertEqual(result.get("final_verdict"), "approved")
            finally:
                if web_process is not None and web_process.poll() is None:
                    web_process.terminate()
                    try:
                        web_process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        web_process.kill()
                        web_process.wait(timeout=5)
                if web_process is not None:
                    try:
                        web_process.communicate(timeout=1)
                    except subprocess.TimeoutExpired:
                        web_process.kill()
                        web_process.communicate(timeout=1)
                fake_llm.shutdown()
                fake_llm.server_close()
