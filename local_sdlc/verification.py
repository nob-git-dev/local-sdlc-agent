"""Command verification, smoke checks, and evidence helpers."""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Mapping, Sequence

from .models import RunnerError
from .safety import *
from .action_gate import SafetyGateDenied, begin_action
from .budget import bounded_action_timeout, enforce_wall_budget
from .progress_monitor import (
    enforce_progress_deadline,
    observe_progress,
    remaining_progress_seconds,
)
from .utils import display_path, truncate_text, unique_ordered
from .workspace import resolve_project_path


def command_result_document(
    command: str,
    returncode: int,
    stdout: str,
    stderr: str,
    duration: float,
    blocked_reason: str | None = None,
) -> str:
    status = "BLOCKED" if blocked_reason else ("PASS" if returncode == 0 else "FAIL")
    return textwrap.dedent(
        f"""
        ## Command Result

        - command: `{command}`
        - status: {status}
        - exit_code: {returncode}
        - duration_seconds: {duration:.3f}
        {f"- blocked_reason: {blocked_reason}" if blocked_reason else ""}

        ### stdout
        ```text
        {truncate_text(stdout)}
        ```

        ### stderr
        ```text
        {truncate_text(stderr)}
        ```
        """
    ).strip()

def parse_command_result_document(document: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for key in ("command", "status", "exit_code", "duration_seconds", "blocked_reason"):
        match = re.search(rf"(?m)^\s*-\s+{re.escape(key)}:\s*(.*)$", document)
        if not match:
            continue
        value = match.group(1).strip()
        if value.startswith("`") and value.endswith("`"):
            value = value[1:-1]
        result[key] = value
    stdout_match = re.search(r"(?m)^\s*### stdout\s*\n\s*```text\n(.*?)\n\s*```", document, flags=re.DOTALL)
    stderr_match = re.search(r"(?m)^\s*### stderr\s*\n\s*```text\n(.*?)\n\s*```", document, flags=re.DOTALL)
    if stdout_match:
        result["stdout"] = stdout_match.group(1)
    if stderr_match:
        result["stderr"] = stderr_match.group(1)
    return result

def command_failure_count_from_text(text: str) -> int | None:
    summary = re.search(r"FAILED\s*\((?P<body>[^)]*)\)", text)
    if summary:
        total = 0
        for _name, value in re.findall(r"(failures|errors)=(\d+)", summary.group("body")):
            total += int(value)
        if total:
            return total
    markers = re.findall(r"(?m)^\s*(?:ERROR|FAIL):\s+", text)
    if markers:
        return len(markers)
    return None

def command_failure_score(command_docs: Sequence[tuple[str, str]]) -> int | None:
    total = 0
    observed = False
    for _name, document in command_docs:
        parsed = parse_command_result_document(document)
        if parsed.get("status") != "FAIL":
            continue
        text = f"{parsed.get('stdout', '')}\n{parsed.get('stderr', '')}"
        count = command_failure_count_from_text(text)
        if count is None:
            count = 1
        total += count
        observed = True
    return total if observed else None

def normalize_failure_line(line: str) -> str:
    normalized = re.sub(r'File "[^"]+", line \d+', 'File "<path>", line <n>', line.strip())
    normalized = re.sub(r"/tmp/[^/\s]+/project/", "<project>/", normalized)
    normalized = re.sub(r"/home/[^/\s]+/[^\s\"]+", "<path>", normalized)
    normalized = re.sub(r"\b0x[0-9a-fA-F]+\b", "0x<addr>", normalized)
    return normalized

def command_failure_signature(command_docs: Sequence[tuple[str, str]]) -> str | None:
    """Return a stable signature for the currently failing executable checks."""
    signatures: list[str] = []
    for _name, document in command_docs:
        parsed = parse_command_result_document(document)
        if parsed.get("status") != "FAIL":
            continue
        command = parsed.get("command", "")
        try:
            returncode = int(parsed.get("exit_code", "1"))
        except ValueError:
            returncode = 1
        stdout = parsed.get("stdout", "")
        stderr = parsed.get("stderr", "")
        failure_type = classify_failure(returncode, stdout, stderr, parsed.get("blocked_reason"))
        text = f"{stdout}\n{stderr}"
        focus_lines: list[str] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if re.match(r"^(?:FAIL|ERROR):\s+", line):
                focus_lines.append(normalize_failure_line(line))
            elif "AssertionError:" in line:
                focus_lines.append(normalize_failure_line(line))
            elif re.search(r"[A-Za-z_][A-Za-z0-9_]*(?:Error|Exception):", line):
                focus_lines.append(normalize_failure_line(line))
            elif line.startswith("FAILED ("):
                focus_lines.append(normalize_failure_line(line))
        if not focus_lines:
            fallback = [normalize_failure_line(line) for line in text.splitlines() if line.strip()]
            focus_lines = fallback[-6:]
        signatures.append("|".join([command, failure_type, *focus_lines[:8]]))
    return "\n".join(signatures) if signatures else None

def command_failure_family_signature(command_docs: Sequence[tuple[str, str]]) -> str | None:
    """Return a coarser signature for repeated failures in the same test family.

    Exact assertion text can legitimately change between repair attempts while
    the agent is still stuck on the same failing tests. This signature keeps the
    command, failure type, failing test identifiers, and failure count, but
    intentionally ignores AssertionError payload details.
    """
    signatures: list[str] = []
    for _name, document in command_docs:
        parsed = parse_command_result_document(document)
        if parsed.get("status") != "FAIL":
            continue
        command = parsed.get("command", "")
        try:
            returncode = int(parsed.get("exit_code", "1"))
        except ValueError:
            returncode = 1
        stdout = parsed.get("stdout", "")
        stderr = parsed.get("stderr", "")
        failure_type = classify_failure(returncode, stdout, stderr, parsed.get("blocked_reason"))
        text = f"{stdout}\n{stderr}"
        tests: list[str] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            match = re.match(r"^(?:FAIL|ERROR):\s+([^\s]+)(?:\s+\(([^)]+)\))?", line)
            if not match:
                continue
            test_name = match.group(1)
            test_owner = match.group(2) or ""
            tests.append(f"{test_owner}.{test_name}" if test_owner else test_name)
        summary = re.search(r"FAILED\s*\(([^)]*)\)", text)
        if tests:
            signatures.append("|".join([command, failure_type, *sorted(set(tests)), summary.group(0) if summary else ""]))
            continue
        exact = command_failure_signature([(_name, document)])
        if exact:
            signatures.append(exact)
    return "\n".join(signatures) if signatures else None

def classify_failure(returncode: int | None, stdout: str = "", stderr: str = "", blocked_reason: str | None = None) -> str:
    text = f"{stdout}\n{stderr}".lower()
    if blocked_reason:
        return "blocked_command"
    if returncode == 0:
        return "passed"
    if "start directory is not importable" in text and "tests" in text:
        return "missing_test_harness"
    if "required path missing" in text:
        return "missing_artifact"
    if "required path is empty" in text:
        return "empty_artifact"
    if returncode == 124 or "timed out" in text or "timeouterror" in text:
        return "timeout"
    if "syntaxerror" in text or "indentationerror" in text:
        return "syntax_error"
    if "assertionerror" in text or "failed (failures" in text:
        return "test_assertion_failed"
    if "failed (errors" in text or "traceback" in text:
        return "test_error"
    if returncode == 127 or re.search(
        r"(?:command not found|executable file not found|no such file or directory:\s*['\"]?[^'\"]+['\"]?)",
        text,
    ):
        return "missing_executable"
    if "connection refused" in text or "address already in use" in text:
        return "service_unavailable"
    return "command_failed"

def evidence_from_command_document(kind: str, name: str, ok: bool, path: Path, base: Path, document: str) -> dict[str, object]:
    parsed = parse_command_result_document(document)
    try:
        returncode = int(parsed.get("exit_code", "0" if ok else "1"))
    except ValueError:
        returncode = 0 if ok else 1
    failure_type = classify_failure(
        returncode,
        parsed.get("stdout", ""),
        parsed.get("stderr", ""),
        parsed.get("blocked_reason"),
    )
    stdout_payload: dict[str, object] = {}
    stdout = parsed.get("stdout", "").strip()
    if stdout.startswith("{"):
        try:
            loaded = json.loads(stdout)
            if isinstance(loaded, dict):
                stdout_payload = loaded
        except json.JSONDecodeError:
            stdout_payload = {}
    evidence = {
        "id": f"E{abs(hash((kind, name, display_path(path, base)))) % 1000000:06d}",
        "kind": kind,
        "name": name,
        "status": "pass" if ok else "fail",
        "command": parsed.get("command", name),
        "exit_code": returncode,
        "duration_seconds": parsed.get("duration_seconds"),
        "failure_type": None if ok else failure_type,
        "document": display_path(path, base),
    }
    if isinstance(stdout_payload.get("covers"), list):
        evidence["covers"] = [str(item) for item in stdout_payload["covers"] if isinstance(item, str)]
    if isinstance(stdout_payload.get("observations"), dict):
        evidence["observations"] = stdout_payload["observations"]
    command_name = str(parsed.get("command", name)).lower()
    covers = [str(item) for item in evidence.get("covers", []) if isinstance(item, str)]
    if "html-smoke" in command_name and ok:
        covers.extend(["static_html", "html_syntax"])
    if "browser-tetris-smoke" in command_name and ok:
        covers.extend(["browser_smoke"])
    if kind == "required_path" and ok:
        covers.extend(["required_path"])
    if kind == "command" and ok:
        covers.append("external_test_suite")
    if covers:
        evidence["covers"] = unique_ordered(covers)
    return evidence

def run_checked_command(
    project: Path,
    command: str,
    timeout: float,
    run_dir: Path | None = None,
    *,
    action: str = "command",
    control_dirs: Sequence[Path] = (),
    cancel_dirs: Sequence[Path] | None = None,
    budget_dirs: Sequence[Path] | None = None,
    progress_dirs: Sequence[Path] | None = None,
    metadata: Mapping[str, object] | None = None,
) -> tuple[str, bool]:
    cancellation_scope = tuple(control_dirs if cancel_dirs is None else cancel_dirs)
    budget_scope = tuple(control_dirs if budget_dirs is None else budget_dirs)
    progress_scope = tuple(control_dirs if progress_dirs is None else progress_dirs)
    reason = dangerous_command_reason(command)
    shell_reason = unsupported_shell_command_reason(command)
    decision = command_safety_decision(
        command,
        danger_reason=reason,
        shell_reason=shell_reason,
        action=action,
    )
    if run_dir:
        try:
            persisted = begin_action(
                run_dir,
                action,
                action_type="command",
                risk_class=decision.risk_class,
                command=command,
                metadata=metadata,
                cancel_dirs=cancellation_scope,
                budget_dirs=budget_scope,
                progress_dirs=progress_scope,
                decision=decision,
            )
        except SafetyGateDenied as exc:
            blocked_reason = blocked_reason_from_safety_decision(exc.decision)
            return command_result_document(command, 2, "", "", 0.0, blocked_reason), False
        blocked_reason = blocked_reason_from_safety_decision(persisted)
    else:
        blocked_reason = blocked_reason_from_safety_decision(decision)
    if blocked_reason:
        return command_result_document(command, 2, "", "", 0.0, blocked_reason), False

    effective_timeout = (
        bounded_action_timeout(timeout, run_dir, budget_scope)
        if run_dir is not None
        else timeout
    )
    started = time.monotonic()
    progress_enabled = (
        run_dir is not None
        and remaining_progress_seconds(run_dir, progress_scope) is not None
    )
    if not progress_enabled:
        try:
            result = subprocess.run(
                shlex.split(command),
                cwd=project,
                text=True,
                capture_output=True,
                timeout=effective_timeout,
                check=False,
            )
            duration = time.monotonic() - started
            return (
                command_result_document(
                    command,
                    result.returncode,
                    result.stdout,
                    result.stderr,
                    duration,
                ),
                result.returncode == 0,
            )
        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - started
            if run_dir is not None:
                enforce_wall_budget(
                    run_dir,
                    action,
                    action_type="command",
                    budget_dirs=budget_scope,
                )
            stdout = (
                exc.stdout
                if isinstance(exc.stdout, str)
                else (exc.stdout or b"").decode("utf-8", errors="replace")
            )
            stderr = (
                exc.stderr
                if isinstance(exc.stderr, str)
                else (exc.stderr or b"").decode("utf-8", errors="replace")
            )
            stderr = stderr + f"\ncommand timed out after {effective_timeout:g}s"
            diagnostic = unittest_timeout_diagnostic(project, command, effective_timeout)
            if diagnostic:
                stderr = stderr + "\n\n" + diagnostic
            return command_result_document(command, 124, stdout, stderr, duration), False
        except FileNotFoundError as exc:
            duration = time.monotonic() - started
            return command_result_document(command, 127, "", str(exc), duration), False

    try:
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            process = subprocess.Popen(
                shlex.split(command),
                cwd=project,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
            )

            def stop_process() -> None:
                if process.poll() is not None:
                    return
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=0.5)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    if process.poll() is None:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait(timeout=1.0)

            last_output_bytes = 0
            timed_out = False
            try:
                while process.poll() is None:
                    output_bytes = (
                        os.fstat(stdout_file.fileno()).st_size
                        + os.fstat(stderr_file.fileno()).st_size
                    )
                    if run_dir is not None and output_bytes > last_output_bytes:
                        observe_progress(
                            run_dir,
                            {
                                "current_function": action,
                                "command_output_bytes": output_bytes,
                            },
                            source="command_output",
                            control_dirs=progress_scope,
                        )
                        last_output_bytes = output_bytes
                    elapsed = time.monotonic() - started
                    hard_remaining = max(0.0, effective_timeout - elapsed)
                    idle_remaining = (
                        remaining_progress_seconds(run_dir, progress_scope)
                        if run_dir is not None
                        else None
                    )
                    if hard_remaining <= 0 or (
                        idle_remaining is not None and idle_remaining <= 0
                    ):
                        timed_out = True
                        break
                    waits = [hard_remaining]
                    if idle_remaining is not None:
                        waits.append(max(0.01, idle_remaining / 2.0))
                    wait_for = min(0.2, *waits)
                    if wait_for <= 0:
                        timed_out = True
                        break
                    try:
                        process.wait(timeout=wait_for)
                    except subprocess.TimeoutExpired:
                        continue
            except BaseException:
                stop_process()
                raise

            if timed_out and process.poll() is None:
                stop_process()
            else:
                process.wait()

            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read().decode("utf-8", errors="replace")
            stderr = stderr_file.read().decode("utf-8", errors="replace")
            duration = time.monotonic() - started
            if not timed_out:
                return (
                    command_result_document(command, process.returncode, stdout, stderr, duration),
                    process.returncode == 0,
                )

            if run_dir is not None:
                enforce_wall_budget(
                    run_dir,
                    action,
                    action_type="command",
                    budget_dirs=budget_scope,
                )
                enforce_progress_deadline(
                    run_dir,
                    action,
                    control_dirs=progress_scope,
                )
            stderr = stderr + f"\ncommand timed out after {effective_timeout:g}s"
            diagnostic = unittest_timeout_diagnostic(project, command, effective_timeout)
            if diagnostic:
                stderr = stderr + "\n\n" + diagnostic
            return command_result_document(command, 124, stdout, stderr, duration), False
    except FileNotFoundError as exc:
        duration = time.monotonic() - started
        return command_result_document(command, 127, "", str(exc), duration), False

def unsupported_shell_command_reason(command: str) -> str:
    """Return a user-facing reason when a command needs shell semantics."""
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        return f"invalid command quoting: {exc}"
    shell_tokens = {"&&", "||", ";", "|"}
    if any(token in shell_tokens for token in tokens):
        return (
            "unsupported shell operator in --test-command; pass each check as a "
            "separate --test-command instead"
        )
    return ""

def unittest_discover_pattern(command: str) -> tuple[str, str] | None:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if len(tokens) < 6:
        return None
    if tokens[:4] != ["python3", "-m", "unittest", "discover"]:
        return None
    start_dir = ""
    pattern = ""
    index = 4
    while index < len(tokens):
        token = tokens[index]
        if token in {"-s", "--start-directory"} and index + 1 < len(tokens):
            start_dir = tokens[index + 1]
            index += 2
            continue
        if token in {"-p", "--pattern"} and index + 1 < len(tokens):
            pattern = tokens[index + 1]
            index += 2
            continue
        index += 1
    if not start_dir or not pattern:
        return None
    return start_dir, pattern

def unittest_method_ids(project: Path, start_dir: str, pattern: str, limit: int = 24) -> list[str]:
    test_file = resolve_project_path(project, str(Path(start_dir) / pattern))
    if not test_file.exists() or not test_file.is_file() or test_file.suffix != ".py":
        return []
    try:
        tree = ast.parse(test_file.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return []
    rel_module = test_file.relative_to(project).with_suffix("")
    module_name = ".".join(rel_module.parts)
    ids: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        class_bases = [getattr(base, "attr", getattr(base, "id", "")) for base in node.bases]
        is_test_class = node.name.startswith("Test") or "TestCase" in class_bases
        if not is_test_class:
            continue
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test"):
                ids.append(f"{module_name}.{node.name}.{item.name}")
                if len(ids) >= limit:
                    return ids
    return ids

def unittest_timeout_diagnostic(project: Path, command: str, timeout: float) -> str:
    parsed = unittest_discover_pattern(command)
    if not parsed:
        return ""
    start_dir, pattern = parsed
    method_ids = unittest_method_ids(project, start_dir, pattern)
    if not method_ids:
        return ""
    per_test_timeout = max(1.0, min(5.0, timeout / 12.0))
    lines = [
        "## Timeout Localization",
        "",
        f"- rule: `{command}` timed out; reran individual unittest methods with {per_test_timeout:g}s timeout each.",
        "- invariant: a lexer/parser/test loop must make progress on every iteration; a timeout is executable evidence of a non-progress path.",
        "- results:",
    ]
    env = {**os.environ, "PYTHONPATH": str(project)}
    for test_id in method_ids:
        started = time.monotonic()
        try:
            result = subprocess.run(
                ["python3", "-m", "unittest", test_id],
                cwd=project,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=per_test_timeout,
                check=False,
            )
            elapsed = time.monotonic() - started
            status = "PASS" if result.returncode == 0 else "FAIL"
            detail = (result.stderr or result.stdout).strip().splitlines()[-1:] or [""]
            lines.append(f"  - {test_id}: {status} exit={result.returncode} duration={elapsed:.3f}s {detail[0]}")
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - started
            lines.append(f"  - {test_id}: TIMEOUT duration={elapsed:.3f}s")
    return "\n".join(lines)

def run_required_path_checks(project: Path, paths: Sequence[str]) -> list[tuple[str, bool]]:
    results: list[tuple[str, bool]] = []
    for raw in unique_ordered(paths):
        started = time.monotonic()
        command = f"require-path {raw}"
        try:
            path = resolve_project_path(project, raw)
        except RunnerError as exc:
            duration = time.monotonic() - started
            results.append((command_result_document(command, 1, "", str(exc), duration), False))
            continue
        if not path.exists():
            duration = time.monotonic() - started
            results.append((command_result_document(command, 1, "", f"required path missing: {raw}", duration), False))
            continue
        if not path.is_file():
            duration = time.monotonic() - started
            results.append((command_result_document(command, 1, "", f"required path is not a file: {raw}", duration), False))
            continue
        size = path.stat().st_size
        if size <= 0:
            duration = time.monotonic() - started
            results.append((command_result_document(command, 1, "", f"required path is empty: {raw}", duration), False))
            continue
        duration = time.monotonic() - started
        results.append((command_result_document(command, 0, f"{raw}: {size} bytes\n", "", duration), True))
    return results

def is_tetris_request(brief: str, paths: Sequence[str]) -> bool:
    combined = " ".join([brief, *paths]).lower()
    return "tetris" in combined or "テトリス" in combined

def is_redis_request(brief: str, paths: Sequence[str]) -> bool:
    combined = " ".join([brief, *paths]).lower()
    return "redis" in combined or "resp" in combined or "kvs" in combined or "key-value" in combined

def should_run_redis_smoke(mode: str, brief: str, paths: Sequence[str]) -> bool:
    if mode == "always":
        return True
    if mode == "never":
        return False
    if mode == "auto":
        return is_redis_request(brief, paths)
    raise RunnerError(f"invalid redis smoke mode: {mode}")

def find_free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])

def encode_resp_array(items: Sequence[bytes | str]) -> bytes:
    chunks = [f"*{len(items)}\r\n".encode("ascii")]
    for item in items:
        raw = item.encode("utf-8") if isinstance(item, str) else item
        chunks.append(f"${len(raw)}\r\n".encode("ascii"))
        chunks.append(raw + b"\r\n")
    return b"".join(chunks)

def read_resp_response(sock: socket.socket) -> bytes:
    first = sock.recv(1)
    if not first:
        raise RuntimeError("connection closed before response")
    line = bytearray(first)
    while not line.endswith(b"\r\n"):
        chunk = sock.recv(1)
        if not chunk:
            raise RuntimeError("connection closed while reading response line")
        line.extend(chunk)
    if first == b"$":
        size = int(bytes(line[1:-2]).decode("ascii"))
        if size < 0:
            return bytes(line)
        body = bytearray()
        expected = size + 2
        while len(body) < expected:
            chunk = sock.recv(expected - len(body))
            if not chunk:
                raise RuntimeError("connection closed while reading bulk response")
            body.extend(chunk)
        return bytes(line + body)
    return bytes(line)

def wait_for_tcp(host: str, port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False

def run_redis_smoke_check(project: Path, run_dir: Path, timeout: float) -> tuple[str, bool]:
    server_path = project / "server.py"
    if not server_path.exists():
        return command_result_document("redis-smoke", 1, "", "server.py not found", 0.0), False

    port = find_free_tcp_port()
    started = time.monotonic()
    process = subprocess.Popen(
        [sys.executable, "server.py", "--port", str(port)],
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    failures: list[str] = []
    observations: list[str] = [f"port: {port}"]

    def expect(items: Sequence[bytes | str], expected: bytes, label: str, sock: socket.socket | None = None) -> None:
        owns_socket = sock is None
        active = sock
        if active is None:
            active = socket.create_connection(("127.0.0.1", port), timeout=2.0)
        try:
            active.sendall(encode_resp_array(items))
            actual = read_resp_response(active)
            observations.append(f"{label}: {actual!r}")
            if actual != expected:
                failures.append(f"{label}: expected {expected!r}, got {actual!r}")
        finally:
            if owns_socket:
                active.close()

    try:
        if not wait_for_tcp("127.0.0.1", port, min(timeout, 5.0)):
            failures.append("server did not accept TCP connections")
        else:
            with socket.create_connection(("127.0.0.1", port), timeout=2.0) as conn:
                expect(["PING"], b"+PONG\r\n", "PING", conn)
                expect(["ECHO", "hello"], b"$5\r\nhello\r\n", "ECHO", conn)
                expect(["SET", "foo", "bar"], b"+OK\r\n", "SET", conn)
                expect(["GET", "foo"], b"$3\r\nbar\r\n", "GET", conn)
                expect(["GET", "missing"], b"$-1\r\n", "GET missing", conn)
                expect(["DEL", "foo"], b":1\r\n", "DEL existing", conn)
                expect(["DEL", "foo"], b":0\r\n", "DEL missing", conn)
                expect(["set", "lower", "ok"], b"+OK\r\n", "lowercase set", conn)
                expect(["get", "lower"], b"$2\r\nok\r\n", "lowercase get", conn)
                expect(["SET", "empty", ""], b"+OK\r\n", "empty SET", conn)
                expect(["GET", "empty"], b"$0\r\n\r\n", "empty GET", conn)
                huge = "x" * 4096
                expect(["SET", "huge", huge], b"+OK\r\n", "huge SET", conn)
                expect(["GET", "huge"], f"$4096\r\n{huge}\r\n".encode("utf-8"), "huge GET", conn)
                expect(["SET", "ttlkey", "alive"], b"+OK\r\n", "ttl SET", conn)
                expect(["EXPIRE", "ttlkey", "1"], b":1\r\n", "EXPIRE", conn)
                conn.sendall(encode_resp_array(["TTL", "ttlkey"]))
                ttl_response = read_resp_response(conn)
                observations.append(f"TTL ttlkey: {ttl_response!r}")
                if not re.fullmatch(rb":[01]\r\n", ttl_response):
                    failures.append(f"TTL ttlkey: expected :0 or :1, got {ttl_response!r}")
                time.sleep(1.2)
                expect(["GET", "ttlkey"], b"$-1\r\n", "expired GET", conn)
                expect(["TTL", "ttlkey"], b":-2\r\n", "expired TTL", conn)
                conn.sendall(encode_resp_array(["ECHO"]))
                wrong_arity = read_resp_response(conn)
                observations.append(f"wrong arity ECHO: {wrong_arity!r}")
                if not wrong_arity.startswith(b"-ERR"):
                    failures.append(f"wrong arity ECHO: expected -ERR, got {wrong_arity!r}")

            expect(["SET", "shared", "one"], b"+OK\r\n", "client1 SET")
            expect(["GET", "shared"], b"$3\r\none\r\n", "client2 GET")

            try:
                with socket.create_connection(("127.0.0.1", port), timeout=2.0) as bad:
                    bad.sendall(b"not-resp\r\n")
                    response = read_resp_response(bad)
                    observations.append(f"invalid RESP: {response!r}")
                    if not response.startswith(b"-ERR"):
                        failures.append(f"invalid RESP: expected -ERR, got {response!r}")
            except Exception as exc:
                observations.append(f"invalid RESP connection ended: {exc}")

            expect(["PING"], b"+PONG\r\n", "server survived invalid input")
    except Exception as exc:
        failures.append(f"smoke exception: {type(exc).__name__}: {exc}")
    finally:
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=2.0)

    duration = time.monotonic() - started
    stdout_doc = json.dumps({"ok": not failures, "observations": observations}, ensure_ascii=False, indent=2)
    stderr_doc = "\n".join(failures)
    if stderr:
        stderr_doc = (stderr_doc + "\n\nserver stderr:\n" + truncate_text(stderr)).strip()
    if stdout:
        stdout_doc += "\n\nserver stdout:\n" + truncate_text(stdout)
    return command_result_document("redis-smoke", 0 if not failures else 1, stdout_doc, stderr_doc, duration), not failures

def run_browser_tetris_check(project: Path, raw: str, run_dir: Path, timeout: float) -> tuple[str, bool] | None:
    from .harnesses.html_browser import run_browser_tetris_check as _run_browser_tetris_check

    return _run_browser_tetris_check(project, raw, run_dir, timeout)

def run_html_smoke_checks(
    project: Path,
    paths: Sequence[str],
    run_dir: Path,
    timeout: float,
    tetris_checks: bool = False,
) -> list[tuple[str, bool]]:
    from .harnesses.html_browser import run_html_smoke_checks as _run_html_smoke_checks

    return _run_html_smoke_checks(project, paths, run_dir, timeout, tetris_checks=tetris_checks)

def has_tetris_initial_render_sequence(text: str) -> bool:
    from .harnesses.html_browser import has_tetris_initial_render_sequence as _has_tetris_initial_render_sequence

    return _has_tetris_initial_render_sequence(text)

def dangerous_command_reason(command: str) -> str | None:
    patterns = [
        (r"\bDROP\s+(DATABASE|TABLE|SCHEMA|INDEX)\b", "DROP 系コマンドは危険です"),
        (r"\bTRUNCATE\s+(TABLE\s+)?[a-zA-Z_]+", "TRUNCATE は全件削除リスクがあります"),
        (r"\bgit\s+reset\s+--hard\b", "git reset --hard は未コミット変更を失います"),
        (r"\bgit\s+clean\s+-[fd]{1,2}\b", "git clean は未追跡ファイルを削除します"),
        (r"(^|[;&|]\s*)sudo\s", "sudo は人間の明示承認が必要です"),
        (r"(^|[;&|]\s*)rm\s+-[rRfF]{1,2}\s+(/\s*$|~\s*$|\$HOME\s*$)", "root/home の rm -rf は危険です"),
    ]
    for pattern, reason in patterns:
        if re.search(pattern, command, flags=re.IGNORECASE):
            return reason
    delete = re.search(r"\bDELETE\s+FROM\s+[a-zA-Z_][a-zA-Z0-9_]*\b", command, re.IGNORECASE)
    if delete and not re.search(r"\bWHERE\b", command, re.IGNORECASE):
        return "WHERE 句なし DELETE は全件削除です"
    update = re.search(r"\bUPDATE\s+[a-zA-Z_][a-zA-Z0-9_]*\s+SET\b", command, re.IGNORECASE)
    if update and not re.search(r"\bWHERE\b", command, re.IGNORECASE):
        return "WHERE 句なし UPDATE は全件更新です"
    return None
