import json
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from local_sdlc.browser_worker import BrowserWorkerConfig, create_browser_worker_server
from local_sdlc.harnesses.browser_protocol import (
    BROWSER_PROTOCOL_VERSION,
    MAX_BROWSER_OUTPUT_BYTES,
    MAX_BROWSER_REQUEST_BYTES,
    TETRIS_DOM_CHECK,
    BrowserCheckRequest,
    BrowserCheckResult,
    BrowserProtocolError,
)
from local_sdlc.harnesses.browser_runtime import (
    LocalBrowserRunner,
    RemoteBrowserRunner,
    browser_runner_from_environment,
)
from local_sdlc.harnesses.html_browser import run_browser_tetris_check
from local_sdlc.verification import classify_failure


def write_functional_tetris_fixture(project: Path) -> None:
    (project / "tetris.html").write_text(
        """<!doctype html>
<html><head><style>.cell.active { background: rgb(255, 0, 0); }</style></head><body>
<button id="start-btn">Start</button><div id="score">0</div><div id="level">1</div><div id="lines">0</div>
<div id="game-board"></div><div class="overlay-title"></div>
<script>
const board = document.getElementById('game-board');
let active = [];
function createBoardCells() { for (let i = 0; i < 200; i++) { const c = document.createElement('div'); c.className = 'cell'; board.appendChild(c); } }
function render() { board.querySelectorAll('.cell').forEach((c, i) => c.classList.toggle('active', active.includes(i))); }
function startGame() { active = [4, 5, 14, 15]; render(); }
function gameLoop() {}
function movePiece(direction) { if (direction === 'left') active = active.map((i) => i - 1); render(); }
function rotate() {}
function softDrop() {}
function hardDrop() {}
function clearLines() {}
function gameOver() { document.querySelector('.overlay-title').textContent = 'GAME OVER'; }
createBoardCells();
document.getElementById('start-btn').addEventListener('click', startGame);
document.addEventListener('keydown', (event) => { if (event.key === 'ArrowLeft') movePiece('left'); });
Object.assign(window, { startGame, gameLoop, movePiece, rotate, softDrop, hardDrop, clearLines, gameOver });
</script></body></html>""",
        encoding="utf-8",
    )


class BrowserProtocolTests(unittest.TestCase):
    def test_request_rejects_unknown_check(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "project"
            project.mkdir()
            (project / "index.html").write_text("<!doctype html>", encoding="utf-8")

            with self.assertRaisesRegex(BrowserProtocolError, "unknown browser check"):
                BrowserCheckRequest.from_payload(
                    {
                        "schema_version": BROWSER_PROTOCOL_VERSION,
                        "check": "arbitrary-command",
                        "project": str(project),
                        "entrypoint": "index.html",
                        "timeout_seconds": 5,
                    },
                    (root,),
                )

    def test_request_rejects_project_outside_allowed_root(self):
        with tempfile.TemporaryDirectory() as allowed_temp, tempfile.TemporaryDirectory() as outside_temp:
            outside = Path(outside_temp)
            (outside / "index.html").write_text("<!doctype html>", encoding="utf-8")

            with self.assertRaisesRegex(BrowserProtocolError, "outside allowed roots"):
                BrowserCheckRequest.from_payload(
                    {
                        "schema_version": BROWSER_PROTOCOL_VERSION,
                        "check": TETRIS_DOM_CHECK,
                        "project": str(outside),
                        "entrypoint": "index.html",
                        "timeout_seconds": 5,
                    },
                    (Path(allowed_temp),),
                )

    def test_request_rejects_entrypoint_path_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "project"
            project.mkdir()
            (root / "outside.html").write_text("<!doctype html>", encoding="utf-8")

            with self.assertRaisesRegex(BrowserProtocolError, "entrypoint must stay inside project"):
                BrowserCheckRequest.from_payload(
                    {
                        "schema_version": BROWSER_PROTOCOL_VERSION,
                        "check": TETRIS_DOM_CHECK,
                        "project": str(project),
                        "entrypoint": "../outside.html",
                        "timeout_seconds": 5,
                    },
                    (root,),
                )

    def test_request_rejects_entrypoint_that_is_not_a_regular_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "directory.html").mkdir()

            with self.assertRaisesRegex(BrowserProtocolError, "existing regular file"):
                BrowserCheckRequest.create(root, "directory.html", 5)

    def test_request_rejects_timeout_above_machine_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "index.html").write_text("<!doctype html>", encoding="utf-8")

            with self.assertRaisesRegex(BrowserProtocolError, "between 0.1 and 120"):
                BrowserCheckRequest.create(root, "index.html", 121)

    def test_result_serialization_truncates_browser_output(self):
        oversized = "界" * MAX_BROWSER_OUTPUT_BYTES

        payload = BrowserCheckResult(1, oversized, oversized, 0.1).to_payload()

        self.assertLessEqual(len(payload["stdout"].encode("utf-8")), MAX_BROWSER_OUTPUT_BYTES)
        self.assertLessEqual(len(payload["stderr"].encode("utf-8")), MAX_BROWSER_OUTPUT_BYTES)


class BrowserRuntimeTests(unittest.TestCase):
    def _request(self, root: Path) -> BrowserCheckRequest:
        project = root / "project"
        project.mkdir()
        (project / "tetris.html").write_text("<!doctype html><html></html>", encoding="utf-8")
        return BrowserCheckRequest.create(project, "tetris.html", 5.0)

    def test_local_runner_keeps_chromium_sandbox_enabled(self):
        with tempfile.TemporaryDirectory() as temp:
            request = self._request(Path(temp))
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=(
                    '<pre id="result">__TETRIS_RESULT__'
                    '{"ok":false,"failures":["active piece is not visible after start"],'
                    '"covers":["browser_smoke"],"observations":{}}</pre>'
                ),
                stderr="",
            )
            with mock.patch("local_sdlc.harnesses.browser_runtime.subprocess.run", return_value=completed) as run:
                result = LocalBrowserRunner("/usr/bin/chromium").run(request)

        command = run.call_args.args[0]
        self.assertNotIn("--no-sandbox", command)
        self.assertNotIn("--disable-web-security", command)
        self.assertNotIn("--allow-file-access-from-files", command)
        self.assertFalse(result.ok)
        self.assertIn("active piece is not visible after start", result.stderr)

    def test_remote_runner_does_not_fallback_when_worker_is_unreachable(self):
        with tempfile.TemporaryDirectory() as temp:
            request = self._request(Path(temp))
            runner = RemoteBrowserRunner("http://127.0.0.1:1", "test-token")

            with mock.patch("local_sdlc.harnesses.browser_runtime.subprocess.run") as local_run:
                result = runner.run(request)

        local_run.assert_not_called()
        self.assertEqual(result.returncode, 69)
        self.assertIn("browser worker unavailable", result.stderr)
        self.assertEqual(
            classify_failure(result.returncode, result.stdout, result.stderr),
            "verification_infrastructure",
        )

    def test_project_config_selects_remote_runner_without_storing_token(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "local_sdlc.json").write_text(
                json.dumps(
                    {
                        "browser": {
                            "worker_url": "http://127.0.0.1:8766",
                            "worker_token_env": "TEST_BROWSER_TOKEN",
                        }
                    }
                ),
                encoding="utf-8",
            )

            runner = browser_runner_from_environment(
                {"TEST_BROWSER_TOKEN": "secret-from-environment"},
                project=root,
            )

        self.assertIsInstance(runner, RemoteBrowserRunner)
        self.assertEqual(runner.token, "secret-from-environment")

    def test_local_runner_accepts_functional_dom_fixture(self):
        browser = shutil.which("chromium") or shutil.which("google-chrome") or shutil.which("chromium-browser")
        if not browser:
            self.skipTest("chromium is not available")
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            write_functional_tetris_fixture(project)
            request = BrowserCheckRequest.create(project, "tetris.html", 10.0)

            result = LocalBrowserRunner(browser).run(request)

        self.assertTrue(result.ok, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIn("keyboard_interaction", payload["covers"])
        self.assertIn("game_over", payload["covers"])

    def test_selected_runner_accepts_functional_dom_fixture(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            write_functional_tetris_fixture(project)

            result = run_browser_tetris_check(project, "tetris.html", project, 10.0)

        self.assertIsNotNone(result)
        document, ok = result
        self.assertTrue(ok, document)

    def test_missing_browser_backend_is_an_explicit_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            write_functional_tetris_fixture(project)
            with mock.patch(
                "local_sdlc.harnesses.html_browser.browser_runner_from_environment",
                return_value=None,
            ):
                result = run_browser_tetris_check(project, "tetris.html", project, 10.0)

        self.assertIsNotNone(result)
        document, ok = result
        self.assertFalse(ok)
        self.assertIn("verification infrastructure: no browser backend is available", document)


class BrowserWorkerHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "tetris.html").write_text("<!doctype html><html></html>", encoding="utf-8")
        self.token = "worker-test-token"
        self.seen_requests: list[BrowserCheckRequest] = []

        def execute(request: BrowserCheckRequest) -> BrowserCheckResult:
            self.seen_requests.append(request)
            return BrowserCheckResult(
                returncode=0,
                stdout=json.dumps({"ok": True, "failures": [], "covers": ["browser_smoke"], "observations": {}}),
                stderr="",
                duration_seconds=0.01,
            )

        config = BrowserWorkerConfig(
            host="127.0.0.1",
            port=0,
            allowed_roots=(self.root,),
            token=self.token,
            browser_executable="/bin/true",
            max_concurrency=1,
        )
        self.server = create_browser_worker_server(config, execute=execute)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def _post(self, payload: dict[str, object], token: str | None) -> tuple[int, dict[str, object]]:
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            f"{self.endpoint}/v1/checks",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": BROWSER_PROTOCOL_VERSION,
            "check": TETRIS_DOM_CHECK,
            "project": str(self.project),
            "entrypoint": "tetris.html",
            "timeout_seconds": 5,
        }

    def test_worker_requires_bearer_token(self):
        status, body = self._post(self._payload(), None)

        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")
        self.assertEqual(self.seen_requests, [])

    def test_worker_rejects_incorrect_bearer_token(self):
        status, body = self._post(self._payload(), "incorrect-token")

        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")
        self.assertEqual(self.seen_requests, [])

    def test_worker_executes_allowlisted_check_and_remote_runner_parses_result(self):
        request = BrowserCheckRequest.create(self.project, "tetris.html", 5.0)

        result = RemoteBrowserRunner(self.endpoint, self.token).run(request)

        self.assertTrue(result.ok)
        self.assertEqual(len(self.seen_requests), 1)
        self.assertEqual(self.seen_requests[0].check, TETRIS_DOM_CHECK)

    def test_worker_rejects_unknown_check_before_execution(self):
        payload = self._payload()
        payload["check"] = "shell"

        status, body = self._post(payload, self.token)

        self.assertEqual(status, 400)
        self.assertIn("unknown browser check", str(body["error"]))
        self.assertEqual(self.seen_requests, [])

    def test_worker_rejects_oversized_request(self):
        payload = self._payload()
        payload["padding"] = "x" * MAX_BROWSER_REQUEST_BYTES

        status, body = self._post(payload, self.token)

        self.assertEqual(status, 413)
        self.assertEqual(body["error"], "browser request is too large")
        self.assertEqual(self.seen_requests, [])

    def test_worker_rejects_request_when_concurrency_is_exhausted(self):
        semaphore = self.server.RequestHandlerClass.semaphore
        self.assertTrue(semaphore.acquire(blocking=False))
        try:
            status, body = self._post(self._payload(), self.token)
        finally:
            semaphore.release()

        self.assertEqual(status, 429)
        self.assertEqual(body["error"], "worker busy")
        self.assertEqual(self.seen_requests, [])

    def test_worker_health_does_not_expose_token(self):
        with urllib.request.urlopen(f"{self.endpoint}/health", timeout=2) as response:
            body = json.load(response)

        self.assertEqual(body["status"], "ok")
        self.assertNotIn(self.token, json.dumps(body))

    def test_worker_does_not_execute_when_browser_is_unavailable(self):
        seen: list[BrowserCheckRequest] = []
        config = BrowserWorkerConfig(
            host="127.0.0.1",
            port=0,
            allowed_roots=(self.root,),
            token=self.token,
            browser_executable="/path/that/does/not/exist/chromium",
            max_concurrency=1,
        )
        server = create_browser_worker_server(config, execute=lambda request: seen.append(request))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            endpoint = f"http://127.0.0.1:{server.server_port}"
            request = BrowserCheckRequest.create(self.project, "tetris.html", 5.0)

            result = RemoteBrowserRunner(endpoint, self.token).run(request)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(result.returncode, 69)
        self.assertIn("HTTP 503", result.stderr)
        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
