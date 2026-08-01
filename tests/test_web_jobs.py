import json
import sys
import tempfile
from pathlib import Path

from tests.helpers import ENTRYPOINT_PATH, LocalSDLCTestCase


class WebJobsTest(LocalSDLCTestCase):
    def test_web_parser_accepts_local_ui_command(self):
        args = self.local_sdlc.build_parser().parse_args(
            ["web", "--host", "127.0.0.1", "--port", "0", "--model-profile", "qwen-agent"]
        )

        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 0)
        self.assertEqual(args.model_profile, "qwen-agent")

    def test_web_build_cli_command_uses_argv_not_shell(self):
        from local_sdlc import web_server

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            config = web_server.WebConfig(
                host="127.0.0.1",
                port=0,
                project=project,
                entrypoint=ENTRYPOINT_PATH,
                base_url="http://localhost:30000/v1",
                model="",
                model_profile="qwen-agent",
            )
            built = web_server.build_cli_command(
                {
                    "mode": "agent",
                    "brief": "fix app",
                    "include": ["app.py"],
                    "new_file": ["tests/test_app.py"],
                    "test_command": ["python3 -m unittest"],
                    "apply": True,
                },
                config,
            )

        self.assertEqual(built.argv[0], sys.executable)
        self.assertIn("agent", built.argv)
        self.assertIn("--include", built.argv)
        self.assertIn("app.py", built.argv)
        self.assertIn("--new-file", built.argv)
        self.assertIn("tests/test_app.py", built.argv)
        self.assertIn("--test-command", built.argv)
        self.assertIn("python3 -m unittest", built.argv)
        self.assertIn("--apply", built.argv)
        self.assertNotIn("&&", built.display)

    def test_web_build_cli_command_propagates_config_without_leaking_api_key(self):
        from local_sdlc import web_server

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            config_path = project / "local_sdlc.json"
            config_path.write_text('{"llm": {"base_url": "https://api.example/v1"}}\n', encoding="utf-8")
            config = web_server.WebConfig(
                host="127.0.0.1",
                port=0,
                project=project,
                entrypoint=ENTRYPOINT_PATH,
                config_file=config_path,
                base_url="https://api.example/v1",
                model="remote-model",
                model_profile="default",
            )
            built = web_server.build_cli_command(
                {
                    "mode": "health",
                    "api_key": "secret-value",
                },
                config,
            )

        self.assertIn("--config-file", built.argv)
        self.assertIn(str(config_path), built.argv)
        self.assertNotIn("secret-value", built.display)
        self.assertEqual(built.env_overrides["LOCAL_SDLC_API_KEY"], "secret-value")

    def test_web_build_cli_command_uses_repo_skills_dir_for_external_project(self):
        from local_sdlc import web_server

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            external_project = project / "new-work"
            config = web_server.WebConfig(
                host="127.0.0.1",
                port=0,
                project=project,
                entrypoint=ENTRYPOINT_PATH,
                base_url="http://localhost:30000/v1",
                model="qwen3.5-122b",
                model_profile="qwen-agent",
            )
            built = web_server.build_cli_command(
                {
                    "mode": "doctor",
                    "project": str(external_project),
                    "skip_llm": True,
                },
                config,
            )

        self.assertIn("--skills-dir", built.argv)
        self.assertIn(str(ENTRYPOINT_PATH.parent / "sdlc-skills" / "skills"), built.argv)
        self.assertEqual(built.cwd, external_project.resolve())

    def test_web_build_cli_command_supports_supervisor_consult_without_context(self):
        from local_sdlc import web_server

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            config = web_server.WebConfig(
                host="127.0.0.1",
                port=0,
                project=project,
                entrypoint=ENTRYPOINT_PATH,
                base_url="http://localhost:30000/v1",
                model="qwen3.5-122b",
                model_profile="qwen-agent",
            )
            built = web_server.build_cli_command(
                {
                    "mode": "supervisor",
                    "brief": "エラーの原因を分析して",
                },
                config,
            )

        self.assertIn("supervisor", built.argv)
        self.assertIn("エラーの原因を分析して", built.argv)
        self.assertNotIn("--allow-no-context", built.argv)
        self.assertNotIn("--include", built.argv)

    def test_web_ensure_project_directory_creates_new_project_under_workspace_root(self):
        from local_sdlc.web_jobs import ensure_project_directory

        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            current_project = workspace / "local-sdlc-agent"
            current_project.mkdir(parents=True)
            new_project = workspace / "run260728"

            created = ensure_project_directory(new_project, current_project.parent)

            self.assertTrue(created)
            self.assertTrue(new_project.is_dir())

    def test_web_ensure_project_directory_rejects_new_project_outside_workspace_root(self):
        from local_sdlc.web_jobs import ensure_project_directory

        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            current_project = workspace / "local-sdlc-agent"
            current_project.mkdir(parents=True)
            outside_project = Path(temp) / "outside" / "run260728"

            with self.assertRaisesRegex(self.local_sdlc.RunnerError, "inside web workspace root"):
                ensure_project_directory(outside_project, current_project.parent)

            self.assertFalse(outside_project.exists())

    def test_web_ensure_project_directory_rejects_file_path(self):
        from local_sdlc.web_jobs import ensure_project_directory

        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            file_path = workspace / "not-a-directory"
            file_path.write_text("not dir\n", encoding="utf-8")

            with self.assertRaisesRegex(self.local_sdlc.RunnerError, "not a directory"):
                ensure_project_directory(file_path, workspace)

    def test_web_bootstrap_spec_creates_minimal_spec_for_first_agent_run(self):
        from local_sdlc.web_jobs import ensure_web_bootstrap_spec

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)

            created = ensure_web_bootstrap_spec(
                project,
                {
                    "mode": "agent",
                    "brief": "ブラウザで動く小さなゲームを作って",
                    "new_file": ["game.html"],
                    "require_path": ["game.html"],
                    "test_command": ["python3 -m unittest"],
                },
                "agent",
            )

            self.assertTrue(created)
            spec = (project / "SPEC.md").read_text(encoding="utf-8")
            self.assertIn("ブラウザで動く小さなゲームを作って", spec)
            self.assertIn("`game.html`", spec)
            self.assertIn("game.html exists and is non-empty", spec)
            self.assertIn("python3 -m unittest", spec)

    def test_web_bootstrap_spec_preserves_existing_or_explicit_spec(self):
        from local_sdlc.web_jobs import ensure_web_bootstrap_spec

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            spec_path = project / "SPEC.md"
            spec_path.write_text("# Existing SPEC\n", encoding="utf-8")

            created_existing = ensure_web_bootstrap_spec(
                project,
                {"brief": "作って", "new_file": ["app.py"]},
                "agent",
            )
            created_explicit = ensure_web_bootstrap_spec(
                project,
                {"brief": "作って", "new_file": ["app.py"], "spec_file": "custom.md"},
                "agent",
            )

            self.assertFalse(created_existing)
            self.assertFalse(created_explicit)
            self.assertEqual(spec_path.read_text(encoding="utf-8"), "# Existing SPEC\n")

    def test_web_normalizes_clear_creation_request_to_safe_new_file_target(self):
        from local_sdlc.web_jobs import normalize_web_job_payload

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            normalized = normalize_web_job_payload(
                {"mode": "agent", "brief": "ブラウザで遊べるテトリスを作って"},
                project,
                "20260101-010203-deadbeef",
            )

        self.assertEqual(normalized["new_file"], ["tetris.html"])
        self.assertEqual(normalized["require_path"], ["tetris.html"])
        self.assertTrue(str(normalized["run_dir"]).endswith("20260101-010203-deadbeef"))

    def test_web_normalization_leaves_ambiguous_agent_request_strict(self):
        from local_sdlc.web_jobs import normalize_web_job_payload

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            normalized = normalize_web_job_payload(
                {"mode": "agent", "brief": "新しい機能を作って"},
                project,
                "20260101-010203-deadbeef",
            )

        self.assertNotIn("new_file", normalized)
        self.assertIn("run_dir", normalized)

    def test_web_build_cli_command_rejects_agent_without_target_with_helpful_message(self):
        from local_sdlc import web_server

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            config = web_server.WebConfig(
                host="127.0.0.1",
                port=0,
                project=project,
                entrypoint=ENTRYPOINT_PATH,
                base_url="http://localhost:30000/v1",
                model="qwen3.5-122b",
                model_profile="qwen-agent",
            )
            with self.assertRaisesRegex(self.local_sdlc.RunnerError, "Consult / Analyze"):
                web_server.build_cli_command(
                    {
                        "mode": "agent",
                        "brief": "エラーの原因を分析して",
                    },
                    config,
                )

    def test_web_ui_beginner_ux_contract_scores_at_least_90_percent(self):
        from local_sdlc import web_server

        html = web_server._index_html()
        ux_markers = [
            "相談・分析",
            "新規作成",
            "既存修正",
            "仕様から段階実行",
            "状態確認",
            "読むファイル",
            "作るファイル",
            "確認コマンド",
            "詳細設定",
            "コード作成/修正には対象が必要です",
            "Webで開く",
            "このファイルを対象に続ける",
            "仕様書を作って続ける",
            "結果と次の操作",
            "作成依頼として検出",
            "suggestedNewFilesFromBrief",
            "partial: 実行中の途中結果です",
            "まだ実行準備中です",
        ]
        passed = sum(1 for marker in ux_markers if marker in html)

        self.assertGreaterEqual(passed / len(ux_markers), 0.9)

    def test_web_persisted_job_summary_exposes_preview_and_followup_actions(self):
        from local_sdlc import web_server
        from local_sdlc.web_jobs import JobRegistry

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "game.html").write_text("<!doctype html><title>game</title>", encoding="utf-8")
            run_dir = project / ".sdlc-runner" / "runs" / "20260101-010203"
            run_dir.mkdir(parents=True)
            (run_dir / "run.json").write_text(
                json.dumps({"changed_paths": ["game.html"], "final_verdict": "approved"}),
                encoding="utf-8",
            )
            job_id = "20260101-010203-deadbeef"
            job_dir = project / ".sdlc-runner" / "web" / "jobs" / job_id
            job_dir.mkdir(parents=True)
            (job_dir / "job.json").write_text(
                json.dumps(
                    {
                        "id": job_id,
                        "mode": "agent",
                        "brief": "make game",
                        "status": "completed",
                        "returncode": 0,
                        "started_at": "2026-01-01T01:02:03Z",
                        "ended_at": "2026-01-01T01:02:04Z",
                        "cwd": str(project),
                        "command": f"{sys.executable} local_sdlc.py agent make --new-file game.html --apply",
                        "log_dir": str(job_dir),
                    }
                ),
                encoding="utf-8",
            )
            (job_dir / "output.log").write_text(f"run_dir: {run_dir}\nfinal_verdict: approved\n", encoding="utf-8")
            registry = JobRegistry(
                web_server.WebConfig(
                    host="127.0.0.1",
                    port=0,
                    project=project,
                    entrypoint=ENTRYPOINT_PATH,
                    base_url="http://localhost:30000/v1",
                    model="qwen3.5-122b",
                    model_profile="qwen-agent",
                )
            )
            jobs = registry.list_jobs()
            job = registry.get(job_id)

            self.assertEqual(jobs[0]["id"], job_id)
            self.assertIsNotNone(job)
            result = job.to_dict()["result"]
            self.assertEqual(result["final_verdict"], "approved")
            self.assertEqual(result["artifacts"][0]["path"], "game.html")
            self.assertEqual(result["artifacts"][0]["preview_url"], "/files?path=game.html")
            self.assertIn("continue_with_files", {action["type"] for action in result["next_actions"]})

    def test_web_job_exposes_and_records_explicit_safety_approval(self):
        from local_sdlc import web_server
        from local_sdlc.web_jobs import JobRegistry

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            run_dir = project / ".sdlc-runner" / "runs" / "20260101-010203"
            run_dir.mkdir(parents=True)
            self.local_sdlc.run_checked_command(project, "docker ps", 5, run_dir)
            decision_id = self.local_sdlc.read_safety_decisions(run_dir)[0]["decision_id"]
            job_id = "20260101-010203-deadbeef"
            job_dir = project / ".sdlc-runner" / "web" / "jobs" / job_id
            job_dir.mkdir(parents=True)
            (job_dir / "job.json").write_text(
                json.dumps(
                    {
                        "id": job_id,
                        "mode": "agent",
                        "brief": "risky check",
                        "status": "failed",
                        "returncode": 1,
                        "started_at": "2026-01-01T01:02:03Z",
                        "ended_at": "2026-01-01T01:02:04Z",
                        "cwd": str(project),
                        "command": "python3 local_sdlc.py agent check",
                        "log_dir": str(job_dir),
                    }
                ),
                encoding="utf-8",
            )
            (job_dir / "output.log").write_text(f"run_dir: {run_dir}\n", encoding="utf-8")
            registry = JobRegistry(
                web_server.WebConfig(
                    host="127.0.0.1",
                    port=0,
                    project=project,
                    entrypoint=ENTRYPOINT_PATH,
                )
            )

            before = registry.get(job_id).to_dict()["result"]
            approval = registry.approve(job_id, decision_id)
            after = registry.get(job_id).to_dict()["result"]

        self.assertEqual(before["safety_state"], "APPROVAL_REQUIRED")
        self.assertEqual(before["pending_safety_decisions"][0]["decision_id"], decision_id)
        self.assertEqual(approval["source"], "web")
        self.assertEqual(after["pending_safety_decisions"], [])

    def test_web_approval_rejects_decision_outside_job_run_directory(self):
        from local_sdlc import web_server
        from local_sdlc.web_jobs import JobRegistry

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            run_dir = project / ".sdlc-runner" / "runs" / "parent-run"
            outside = project / "outside-run"
            run_dir.mkdir(parents=True)
            decision = self.local_sdlc.action_safety_decision(
                "service_restart",
                action_type="service_control",
                risk_class="service_control",
            )
            persisted = self.local_sdlc.authorize_safety_decision(outside, decision)
            (run_dir / "run.json").write_text(
                json.dumps(
                    {
                        "pending_safety_decisions": [
                            {**persisted, "run_dir": str(outside.resolve())}
                        ]
                    }
                ),
                encoding="utf-8",
            )
            job_id = "20260101-010203-outside1"
            job_dir = project / ".sdlc-runner" / "web" / "jobs" / job_id
            job_dir.mkdir(parents=True)
            (job_dir / "job.json").write_text(
                json.dumps(
                    {
                        "id": job_id,
                        "mode": "run-stages",
                        "status": "failed",
                        "cwd": str(project),
                        "command": "python3 local_sdlc.py run-stages test",
                    }
                ),
                encoding="utf-8",
            )
            (job_dir / "output.log").write_text(f"run_dir: {run_dir}\n", encoding="utf-8")
            registry = JobRegistry(
                web_server.WebConfig(
                    host="127.0.0.1",
                    port=0,
                    project=project,
                    entrypoint=ENTRYPOINT_PATH,
                )
            )

            with self.assertRaises(self.local_sdlc.RunnerError):
                registry.approve(
                    job_id,
                    str(persisted["decision_id"]),
                    decision_run_dir=str(outside.resolve()),
                )

    def test_web_job_exposes_child_safety_blocked_state(self):
        from local_sdlc import web_server
        from local_sdlc.web_jobs import JobRegistry

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            run_dir = project / ".sdlc-runner" / "runs" / "parent-run"
            child_dir = run_dir / "s01-child"
            decision = self.local_sdlc.action_safety_decision(
                "history_rewrite",
                action_type="command",
                risk_class="git_history_rewrite",
                command="git reset --hard",
            )
            persisted = self.local_sdlc.authorize_safety_decision(child_dir, decision)
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "run.json").write_text(
                json.dumps(
                    {
                        "blocked_safety_decisions": [
                            {**persisted, "run_dir": str(child_dir.resolve()), "stage_id": "S01"}
                        ]
                    }
                ),
                encoding="utf-8",
            )
            job_id = "20260101-010203-blocked1"
            job_dir = project / ".sdlc-runner" / "web" / "jobs" / job_id
            job_dir.mkdir(parents=True)
            (job_dir / "job.json").write_text(
                json.dumps(
                    {
                        "id": job_id,
                        "mode": "run-stages",
                        "status": "failed",
                        "cwd": str(project),
                        "command": "python3 local_sdlc.py run-stages test",
                    }
                ),
                encoding="utf-8",
            )
            (job_dir / "output.log").write_text(f"run_dir: {run_dir}\n", encoding="utf-8")
            registry = JobRegistry(
                web_server.WebConfig(
                    host="127.0.0.1",
                    port=0,
                    project=project,
                    entrypoint=ENTRYPOINT_PATH,
                )
            )

            result = registry.get(job_id).to_dict()["result"]

        self.assertEqual(result["safety_state"], "SAFETY_BLOCKED")
        self.assertEqual(result["blocked_safety_decisions"][0]["stage_id"], "S01")

    def test_web_stop_before_process_start_is_absorbing(self):
        from local_sdlc import web_server
        from local_sdlc.web_jobs import AgentJob, BuiltCommand, JobRegistry

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            job_id = "20260101-010203-aabbccdd"
            run_dir = project / ".sdlc-runner" / "runs" / job_id
            run_dir.mkdir(parents=True)
            registry = JobRegistry(
                web_server.WebConfig(
                    host="127.0.0.1",
                    port=0,
                    project=project,
                    entrypoint=ENTRYPOINT_PATH,
                )
            )
            log_dir = registry.jobs_root / job_id
            log_dir.mkdir(parents=True)
            job = AgentJob(
                job_id,
                BuiltCommand((sys.executable, "-c", "print('must not run')"), project, "hidden"),
                "queued",
                "agent",
                log_dir,
            )
            registry.jobs[job_id] = job

            stopped = registry.stop(job_id)
            log_cancelled = self.local_sdlc.cancel_requested(log_dir)
            run_cancelled = self.local_sdlc.cancel_requested(run_dir)

        self.assertTrue(stopped)
        self.assertEqual(job.status, "stopped")
        self.assertTrue(log_cancelled)
        self.assertTrue(run_cancelled)

    def test_web_running_job_summary_reads_partial_run_progress_without_stdout(self):
        from local_sdlc.web_jobs import summarize_job_result

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            run_dir = project / ".sdlc-runner" / "runs" / "20260101-030405"
            log_dir = project / ".sdlc-runner" / "web" / "jobs" / "20260101-030405-aabbccdd"
            run_dir.mkdir(parents=True)
            log_dir.mkdir(parents=True)
            (project / "tetris.html").write_text("<!doctype html>", encoding="utf-8")
            (run_dir / "run.partial.json").write_text(
                json.dumps(
                    {
                        "current_round": 1,
                        "api_calls": 2,
                        "streaming": {"label": "judge round 1", "status": "starting"},
                    }
                ),
                encoding="utf-8",
            )
            result = summarize_job_result(
                {
                    "mode": "agent",
                    "brief": "テトリスを作って",
                    "status": "running",
                    "command": "python3 local_sdlc.py agent 'テトリスを作って' --new-file tetris.html",
                },
                project,
                log_dir,
                [],
            )

        self.assertEqual(result["run_dir"], str(run_dir))
        self.assertTrue(result["partial_manifest"])
        self.assertIn("judge round 1", result["progress"])
        self.assertEqual(result["current_round"], 1)
        self.assertEqual(result["artifacts"][0]["path"], "tetris.html")
        self.assertTrue(result["artifacts"][0]["exists"])

    def test_web_final_job_summary_does_not_report_running_progress(self):
        from local_sdlc.web_jobs import summarize_job_result

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            run_dir = project / ".sdlc-runner" / "runs" / "20260101-040506"
            log_dir = project / ".sdlc-runner" / "web" / "jobs" / "20260101-040506-aabbccdd"
            run_dir.mkdir(parents=True)
            log_dir.mkdir(parents=True)
            (run_dir / "run.json").write_text(
                json.dumps(
                    {
                        "final_verdict": "test_failed",
                        "api_calls": 8,
                        "streaming": {"label": "judge round 3", "status": "completed"},
                    }
                ),
                encoding="utf-8",
            )
            (log_dir / "output.log").write_text(f"run_dir: {run_dir}\n", encoding="utf-8")
            result = summarize_job_result(
                {
                    "mode": "agent",
                    "brief": "テトリスを作って",
                    "status": "failed",
                    "command": "python3 local_sdlc.py agent 'テトリスを作って' --new-file tetris.html",
                },
                project,
                log_dir,
                [],
            )

        self.assertIn("最終処理: judge round 3", result["progress"])
        self.assertNotIn("実行中:", result["progress"])
        self.assertFalse(result["partial_manifest"])

    def test_web_planned_supervisor_job_offers_execute_and_create_actions(self):
        from local_sdlc.web_jobs import summarize_job_result

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            run_dir = project / ".sdlc-runner" / "runs" / "20260101-020304"
            log_dir = project / ".sdlc-runner" / "web" / "jobs" / "20260101-020304-feedface"
            run_dir.mkdir(parents=True)
            log_dir.mkdir(parents=True)
            (run_dir / "run.json").write_text(
                json.dumps({"final_verdict": "planned"}),
                encoding="utf-8",
            )
            (log_dir / "output.log").write_text(f"run_dir: {run_dir}\nfinal_verdict: planned\n", encoding="utf-8")
            result = summarize_job_result(
                {
                    "mode": "supervisor",
                    "brief": "テトリスを作って",
                    "status": "completed",
                    "command": "python3 local_sdlc.py supervisor 'テトリスを作って'",
                },
                project,
                log_dir,
                [],
            )

        actions = {action["type"]: action for action in result["next_actions"]}
        self.assertIn("execute_supervisor_plan", actions)
        self.assertIn("create_from_prompt", actions)
        self.assertEqual(actions["create_from_prompt"]["new_file"], ["tetris.html"])

    def test_web_project_file_resolution_rejects_paths_outside_project(self):
        from local_sdlc.web_jobs import resolve_project_path

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            project.mkdir()
            (project / "index.html").write_text("ok", encoding="utf-8")
            path, relative = resolve_project_path(project, "index.html")

            self.assertEqual(path, project / "index.html")
            self.assertEqual(relative, "index.html")
            with self.assertRaisesRegex(self.local_sdlc.RunnerError, "inside the project"):
                resolve_project_path(project, "../secret.txt")
