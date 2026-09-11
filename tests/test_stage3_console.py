from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from orchestrator.console.server import ConsoleHTTPServer
from orchestrator.console.server import _human_result_text
from orchestrator.core.models import TaskState
from orchestrator.storage.sqlite_store import SQLiteStateStore


def _request(port: int, path: str, *, method: str = "GET", body: dict | None = None):
    url = f"http://127.0.0.1:{port}{path}"
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class ConsoleServerTests(unittest.TestCase):
    """网页控制台 API：Runs / Teams / Connections / 详情与协调写。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "proj"
        self.project.mkdir()
        (self.project / "config").mkdir(parents=True)
        # 默认 team
        (self.project / "config" / "team.yaml").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "team_id": "default",
                    "bootstrap_supervisor": "supervisor",
                    "roles": [
                        {
                            "role_id": "worker",
                            "version": 1,
                            "title": "W",
                            "required_capabilities": ["read", "write"],
                        },
                        {
                            "role_id": "supervisor",
                            "version": 1,
                            "title": "S",
                            "required_capabilities": ["plan", "review"],
                        },
                    ],
                    "agent_pools": [
                        {
                            "pool_id": "pool-fake",
                            "backend": "fake",
                            "role_id": "worker",
                            "count": 2,
                            "max_count": 2,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.db_path = self.project / "state.db"
        self.store = SQLiteStateStore(self.db_path)
        self.store.create_run("run-1", "default")
        self.server = ConsoleHTTPServer(
            self.store,
            project_root=self.project,
            db_path=self.db_path,
            host="127.0.0.1",
            port=0,
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server.close()
        self.store.close()
        self.temp.cleanup()

    def test_status_reports_project(self) -> None:
        status, payload = _request(self.port, "/api/status")
        self.assertEqual(status, 200)
        self.assertIn("project_root", payload)

    def test_runs_list_and_create(self) -> None:
        status, payload = _request(self.port, "/api/runs")
        self.assertEqual(status, 200)
        ids = [run["run_id"] for run in payload["runs"]]
        self.assertIn("run-1", ids)
        status, created = _request(
            self.port,
            "/api/runs",
            method="POST",
            body={"run_id": "run-2", "team_id": "default"},
        )
        self.assertEqual(status, 200, created)
        self.assertEqual(created["run_id"], "run-2")
        # 重复创建冲突
        status, payload = _request(
            self.port, "/api/runs", method="POST", body={"run_id": "run-1"}
        )
        self.assertEqual(status, 409)

    def test_console_can_delete_run_and_only_remove_managed_directories(self) -> None:
        hub = self.project / ".agent-hub"
        worktree = hub / "worktrees" / "run-1"
        runtime = hub / "runs" / "run-1"
        worktree.mkdir(parents=True)
        runtime.mkdir(parents=True)
        sentinel = self.project / "keep-me.txt"
        sentinel.write_text("project root stays", encoding="utf-8")

        status, payload = _request(
            self.port, "/api/runs/run-1", method="DELETE", body={"reason": "test"}
        )
        self.assertEqual(status, 200, payload)
        self.assertFalse(worktree.exists())
        self.assertFalse(runtime.exists())
        self.assertTrue(sentinel.exists())
        status, listed = _request(self.port, "/api/runs")
        self.assertEqual(status, 200)
        self.assertNotIn("run-1", {item["run_id"] for item in listed["runs"]})
        deleted = self.store.connection.execute(
            "SELECT reason FROM deleted_runs WHERE run_id='run-1'"
        ).fetchone()
        self.assertEqual(deleted["reason"], "console-delete")
        event = self.store.connection.execute(
            "SELECT kind FROM events WHERE run_id='run-1' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(event["kind"], "run.deleted")

    def test_run_detail_endpoints(self) -> None:
        self.store.create_task("run-1", "task-1", cwd=str(self.project))
        self.store.transition_task("task-1", TaskState.READY, reason="test")
        for resource in ("summary", "tasks", "events", "merges", "approvals"):
            status, payload = _request(self.port, f"/api/runs/run-1/{resource}")
            self.assertEqual(status, 200, resource)
        status, payload = _request(self.port, "/api/runs/run-1/tasks")
        self.assertEqual(payload["tasks"][0]["task_id"], "task-1")

    def test_chat_endpoint_scopes_task_context(self) -> None:
        status, payload = _request(
            self.port,
            "/api/runs/run-1/tasks",
            method="POST",
            body={
                "task_id": "task-chat",
                "prompt": "Summarize this task",
                "cwd": str(self.project),
            },
        )
        self.assertEqual(status, 200, payload)
        status, payload = _request(self.port, "/api/runs/run-1/chat")
        self.assertEqual(status, 200, payload)
        task = next(item for item in payload["chat"]["tasks"] if item["task_id"] == "task-chat")
        self.assertEqual(task["messages"][0]["role"], "user")
        self.assertEqual(task["messages"][0]["text"], "Summarize this task")

    def test_chat_tree_includes_all_bound_team_agents_even_when_idle(self) -> None:
        supervisor = self.store.provision_fake_pool_agent(
            run_id="run-1", pool_id="codex-supervisor", backend="codex",
            model="gpt-test", role_id="supervisor",
        )
        worker_one = self.store.provision_fake_pool_agent(
            run_id="run-1", pool_id="codebuddy-workers", backend="codebuddy",
            model="worker-a", role_id="worker",
        )
        worker_two = self.store.provision_fake_pool_agent(
            run_id="run-1", pool_id="codebuddy-workers", backend="codebuddy",
            model="worker-b", role_id="worker",
        )
        status, payload = _request(self.port, "/api/runs/run-1/chat")
        self.assertEqual(status, 200, payload)
        chat = payload["chat"]
        by_role = {item["role_id"]: item for item in chat["roles"]}
        self.assertEqual(len(by_role["worker"]["agents"]), 2)
        self.assertEqual(by_role["supervisor"]["agents"][0]["agent_id"], supervisor["agent_id"])
        self.assertEqual(
            {item["agent_id"] for item in by_role["worker"]["agents"]},
            {worker_one["agent_id"], worker_two["agent_id"]},
        )
        self.assertEqual(
            {item["agent_id"] for item in chat["agents"]},
            {supervisor["agent_id"], worker_one["agent_id"], worker_two["agent_id"]},
        )

    def test_chat_renders_supervisor_plan_as_natural_language(self) -> None:
        text = _human_result_text(
            {
                "text": json.dumps(
                    {
                        "plan_version": 2,
                        "supervisor_response": "我会协调两个 Worker。",
                        "tasks": [
                            {"task_id": "worker-1", "role_id": "worker", "instruction": "介绍自己"},
                            {"task_id": "worker-2", "role_id": "worker", "instruction": "介绍自己"},
                        ],
                    },
                    ensure_ascii=False,
                )
            },
            {},
            role_id="supervisor",
        )
        self.assertIn("主管已完成规划", text)
        self.assertIn("worker-1", text)
        self.assertNotIn("plan_version", text)
        self.assertNotIn('"tasks"', text)

    def test_console_can_delete_task_and_keep_audit_event(self) -> None:
        self.store.create_task("run-1", "task-delete", cwd=str(self.project))
        self.store.transition_task("task-delete", TaskState.READY, reason="test")
        self.store.create_attempt("task-delete", "attempt-delete", "agent-delete")
        self.store.transition_task("task-delete", TaskState.REVIEW, reason="test")
        status, payload = _request(
            self.port,
            "/api/runs/run-1/tasks/task-delete/delete",
            method="POST",
            body={"reason": "test-delete"},
        )
        self.assertEqual(status, 200, payload)
        self.assertIsNone(
            self.store.connection.execute(
                "SELECT 1 FROM tasks WHERE task_id='task-delete'"
            ).fetchone()
        )
        event = self.store.connection.execute(
            "SELECT kind FROM events WHERE task_id='task-delete' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(event["kind"], "task.deleted")

    def test_create_task_and_cancel_via_console(self) -> None:
        status, payload = _request(
            self.port,
            "/api/runs/run-1/tasks",
            method="POST",
            body={"task_id": "task-1", "prompt": "hello", "cwd": str(self.project)},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.store.task_state("task-1"), TaskState.READY)
        status, payload = _request(
            self.port,
            "/api/runs/run-1/tasks/task-1/cancel",
            method="POST",
            body={"reason": "console-cancel"},
        )
        self.assertEqual(status, 200, payload)

    def test_console_can_select_backend_and_model_for_task(self) -> None:
        status, payload = _request(
            self.port,
            "/api/runs/run-1/tasks",
            method="POST",
            body={
                "task_id": "task-model",
                "prompt": "use selected model",
                "cwd": str(self.project),
                "required_backend": "codex",
                "required_model": "gpt-test",
            },
        )
        self.assertEqual(status, 200, payload)
        row = self.store.connection.execute(
            "SELECT required_backend, required_model FROM task_dispatch_specs "
            "WHERE task_id='task-model'"
        ).fetchone()
        self.assertEqual(row["required_backend"], "codex")
        self.assertEqual(row["required_model"], "gpt-test")
        status, payload = _request(self.port, "/api/runs/run-1/tasks")
        self.assertEqual(status, 200)
        task = next(item for item in payload["tasks"] if item["task_id"] == "task-model")
        self.assertEqual(task["required_model"], "gpt-test")

    def test_console_lists_models_configured_by_run_team(self) -> None:
        team_path = self.project / "config" / "team.yaml"
        team = json.loads(team_path.read_text(encoding="utf-8"))
        team["agent_pools"] = [
            {
                "pool_id": "codex-pool",
                "backend": "codex",
                "role_id": "worker",
                "count": 1,
                "max_count": 1,
                "model": "gpt-test",
            },
            {
                "pool_id": "codebuddy-pool",
                "backend": "codebuddy",
                "role_id": "worker",
                "count": 1,
                "max_count": 1,
                "model": "glm-test",
            },
        ]
        team_path.write_text(json.dumps(team), encoding="utf-8")
        status, payload = _request(self.port, "/api/runs/run-1/models")
        self.assertEqual(status, 200, payload)
        self.assertEqual(
            {(item["backend"], item["model"]) for item in payload["models"]},
            {("codex", "gpt-test"), ("codebuddy", "glm-test")},
        )

    def test_console_model_catalog_uses_read_only_discovery_result(self) -> None:
        with mock.patch(
            "orchestrator.console.model_catalog.discover_codex_models",
            return_value={
                "backend": "codex",
                "status": "ok",
                "source": "codex",
                "models": [{"id": "gpt-live", "label": "GPT Live"}],
            },
        ):
            status, payload = _request(
                self.port, "/api/model-catalog?backend=codex"
            )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["source"], "codex")
        self.assertEqual(payload["models"][0]["id"], "gpt-live")

    def test_console_probe_keeps_only_reachable_codebuddy_models(self) -> None:
        catalog = {
            "status": "ok",
            "source": "product.internal.json",
            "models": [
                {"id": "works", "label": "Works"},
                {"id": "blocked", "label": "Blocked"},
            ],
        }
        probe_results = [
            {"id": "works", "provider_id": None, "ok": True},
            {
                "id": "blocked",
                "provider_id": None,
                "ok": False,
                "error_kind": "ExecutionError",
            },
        ]
        with mock.patch(
            "orchestrator.console.model_catalog.discover_codebuddy_models",
            return_value=catalog,
        ), mock.patch(
            "orchestrator.console.model_catalog._run_codebuddy_probe",
            new=mock.AsyncMock(return_value=probe_results),
        ):
            status, payload = _request(
                self.port,
                "/api/model-catalog/probe",
                method="POST",
                body={"backend": "codebuddy"},
            )
            self.assertEqual(status, 200, payload)
            self.assertEqual(payload["probe"]["available"], 1)
            self.assertEqual([item["id"] for item in payload["models"]], ["works"])

            status, payload = _request(
                self.port,
                "/api/model-catalog?backend=codebuddy",
            )
        self.assertEqual(status, 200, payload)
        self.assertEqual([item["id"] for item in payload["models"]], ["works"])

    def test_console_probe_without_provider_targets_builtins_only(self) -> None:
        catalog = {
            "status": "ok",
            "source": "product.internal.json",
            "models": [
                {"id": "builtin-works", "label": "Builtin Works"},
                {
                    "id": "volc-works",
                    "label": "Volc Works",
                    "provider_id": "volc-codingplan",
                },
            ],
        }
        probe_results = [
            {"id": "builtin-works", "provider_id": None, "ok": True},
        ]
        with mock.patch(
            "orchestrator.console.model_catalog.discover_codebuddy_models",
            return_value=catalog,
        ), mock.patch(
            "orchestrator.console.model_catalog._run_codebuddy_probe",
            new=mock.AsyncMock(return_value=probe_results),
        ) as probe:
            status, payload = _request(
                self.port,
                "/api/model-catalog/probe",
                method="POST",
                body={"backend": "codebuddy"},
            )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["probe"]["tested"], 1)
        self.assertEqual(
            {item["id"] for item in payload["models"]},
            {"builtin-works", "volc-works"},
        )
        probe.assert_awaited_once_with(self.project, [{"id": "builtin-works", "label": "Builtin Works"}])

    def test_codebuddy_catalog_imports_arkcli_user_models_without_secret(self) -> None:
        from orchestrator.console import model_catalog

        home = Path(self.temp.name) / "home"
        (home / ".workbuddy-ai").mkdir(parents=True)
        (home / ".workbuddy-ai" / "models.json").write_text(
            json.dumps(
                {
                    "availableModels": ["arkcli-code"],
                    "models": [
                        {
                            "id": "arkcli-code",
                            "name": "ArkCLI Code",
                            "apiKey": "do-not-leak",
                            "url": "https://example.invalid/v1",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with mock.patch("pathlib.Path.home", return_value=home):
            result = model_catalog.discover_codebuddy_models(self.project)
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertIn("arkcli-code", encoded)
        self.assertIn("arkcli-user-config", encoded)
        self.assertNotIn("do-not-leak", encoded)

    def test_console_html_auto_probes_codebuddy_models(self) -> None:
        html = Path("orchestrator/console/static/index.html").read_text(
            encoding="utf-8"
        )
        self.assertNotIn('id="probeCodeBuddyModels"', html)
        self.assertIn("选择 CodeBuddy 后自动测试", html)

    def test_console_html_uses_team_first_task_defaults_and_stable_run_details(self) -> None:
        html = Path("orchestrator/console/static/index.html").read_text(encoding="utf-8")
        self.assertIn('data-delete-run=', html)
        self.assertIn('"收起"', html)
        self.assertIn("团队配置自动选择（推荐）", html)
        self.assertIn("交给团队主管（推荐）", html)
        self.assertIn("默认按当前 Run 团队", html)
        self.assertIn('name="approval_mode"', html)
        self.assertIn("自动协作（推荐）", html)
        self.assertIn("display_instruction", html)

    def test_console_can_save_custom_model_provider_without_secret(self) -> None:
        status, payload = _request(
            self.port,
            "/api/model-providers",
            method="POST",
            body={
                "provider_id": "volc-codingplan",
                "label": "火山 CodingPlan",
                "backend": "codebuddy",
                "base_url": "https://example.invalid/api/coding/v3",
                "api_key_env": "VOLC_CODINGPLAN_API_KEY",
                "models": [{"id": "doubao-seed-code", "label": "Doubao Seed Code"}],
                "api_key": "must-not-be-stored",
            },
        )
        self.assertEqual(status, 400, payload)

        status, payload = _request(
            self.port,
            "/api/model-providers",
            method="POST",
            body={
                "provider_id": "volc-codingplan",
                "label": "火山 CodingPlan",
                "backend": "codebuddy",
                "base_url": "https://example.invalid/api/coding/v3",
                "api_key_env": "VOLC_CODINGPLAN_API_KEY",
                "models": [{"id": "doubao-seed-code", "label": "Doubao Seed Code"}],
            },
        )
        self.assertEqual(status, 200, payload)
        status, payload = _request(self.port, "/api/model-providers")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["providers"][0]["provider_id"], "volc-codingplan")
        self.assertNotIn('"api_key"', json.dumps(payload, ensure_ascii=False))

    def test_console_html_has_model_dropdown_without_manual_refresh_action(self) -> None:
        html = Path("orchestrator/console/static/index.html").read_text(encoding="utf-8")
        self.assertIn('id="taskModelSelect"', html)
        self.assertNotIn('id="refreshTaskModels"', html)
        self.assertIn("内置模型（CodeBuddy 默认）", html)
        self.assertIn('label="自定义模型提供方"', html)
        self.assertIn("自定义模型 · ${providerLabel(providerId)}", html)
        self.assertIn("后台打开中国站授权页", html)
        self.assertNotIn("点击后会在桌面打开登录窗口", html)

    def test_web_console_can_create_supervisor_task(self) -> None:
        status, payload = _request(
            self.port,
            "/api/runs/run-1/tasks",
            method="POST",
            body={
                "mode": "supervisor",
                "task_id": "plan-1",
                "prompt": "Split this request into two worker tasks.",
                "cwd": str(self.project),
                "access_mode": "write",
                "write_scope": ["should-be-ignored"],
            },
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["mode"], "supervisor")
        row = self.store.connection.execute(
            """
            SELECT t.access_mode, d.required_role_id
            FROM tasks t JOIN task_dispatch_specs d ON d.task_id=t.task_id
            WHERE t.task_id='plan-1'
            """
        ).fetchone()
        self.assertEqual(row["access_mode"], "read_only")
        self.assertEqual(row["required_role_id"], "supervisor")

    def test_teams_save_and_list(self) -> None:
        team = {
            "schema_version": 1,
            "team_id": "ad-hoc",
            "bootstrap_supervisor": "supervisor",
            "roles": [
                {
                    "role_id": "worker",
                    "version": 1,
                    "title": "W",
                    "required_capabilities": ["read"],
                },
                {
                    "role_id": "supervisor",
                    "version": 1,
                    "title": "S",
                    "required_capabilities": ["plan", "review"],
                },
            ],
            "agent_pools": [
                {
                    "pool_id": "p1",
                    "backend": "fake",
                    "role_id": "worker",
                    "count": 1,
                    "max_count": 1,
                }
            ],
        }
        status, payload = _request(
            self.port,
            "/api/teams",
            method="POST",
            body={"team": team},
        )
        self.assertEqual(status, 200, payload)
        status, payload = _request(self.port, "/api/teams")
        self.assertEqual(status, 200)
        ids = [item["team_id"] for item in payload["teams"]]
        self.assertIn("default", ids)
        self.assertIn("ad-hoc", ids)

    def test_connections_probe(self) -> None:
        status, payload = _request(self.port, "/api/connections")
        self.assertEqual(status, 200)
        backends = {item["backend"] for item in payload["connections"]}
        self.assertIn("fake", backends)
        self.assertIn("codex", backends)
        self.assertIn("codebuddy", backends)

    def test_serve_start_uses_run_team_when_path_is_omitted(self) -> None:
        team = {
            "schema_version": 1,
            "team_id": "saved-team",
            "bootstrap_supervisor": "supervisor",
            "roles": [
                {"role_id": "worker", "version": 1, "title": "W", "required_capabilities": ["read"]},
                {"role_id": "supervisor", "version": 1, "title": "S", "required_capabilities": ["plan"]},
            ],
            "agent_pools": [
                {"pool_id": "saved-workers", "backend": "fake", "role_id": "worker", "count": 1, "max_count": 1},
            ],
        }
        status, payload = _request(self.port, "/api/teams", method="POST", body={"team": team})
        self.assertEqual(status, 200, payload)
        self.store.create_run("saved-run", "saved-team")
        with mock.patch.object(
            self.server.serve_manager,
            "start",
            return_value={"ok": True, "run_id": "saved-run", "status": "started"},
        ) as start:
            status, payload = _request(
                self.port,
                "/api/runs/saved-run/serve/start",
                method="POST",
                body={},
            )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["team_id"], "saved-team")
        self.assertTrue(payload["team_path"].endswith("saved-team.json"))
        start.assert_called_once()
        self.assertTrue(str(start.call_args.args[1]).endswith("saved-team.json"))

    def test_serve_start_migrates_legacy_default_team_alias(self) -> None:
        team_path = self.project / "config" / "team.yaml"
        team = json.loads(team_path.read_text(encoding="utf-8"))
        team["team_id"] = "actual-default"
        team_path.write_text(json.dumps(team), encoding="utf-8")
        self.store.create_run("legacy-default", "default")
        with mock.patch.object(
            self.server.serve_manager,
            "start",
            return_value={"ok": True, "run_id": "legacy-default", "status": "started"},
        ):
            status, payload = _request(
                self.port,
                "/api/runs/legacy-default/serve/start",
                method="POST",
                body={},
            )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["team_id"], "actual-default")
        row = self.store.connection.execute(
            "SELECT team_id FROM runs WHERE run_id='legacy-default'"
        ).fetchone()
        self.assertEqual(row["team_id"], "actual-default")

    def test_serve_start_stop_with_saved_team(self) -> None:
        # 用默认 team 启动 serve 子进程（fake 后端），随后停止
        status, payload = _request(
            self.port,
            "/api/runs/run-1/serve/start",
            method="POST",
            body={"team_path": "config/team.yaml"},
        )
        self.assertEqual(status, 200, payload)
        self.assertIn("started", str(payload.get("status")))
        try:
            status, payload = _request(self.port, "/api/runs/run-1/serve/status")
            self.assertTrue(payload["running"])
            # 子进程起来后应能创建任务并派发到 REVIEW
            time.sleep(2)
            status, payload = _request(
                self.port,
                "/api/runs/run-1/tasks",
                method="POST",
                body={
                    "task_id": "serve-task",
                    "prompt": "hello",
                    "cwd": str(self.project),
                },
            )
            self.assertEqual(status, 200)
            time.sleep(3)
            state = self.store.task_state("serve-task")
            self.assertEqual(state, TaskState.REVIEW)
        finally:
            _request(self.port, "/api/runs/run-1/serve/stop", method="POST", body={})
            status, payload = _request(self.port, "/api/runs/run-1/serve/status")
            self.assertFalse(payload["running"])

    def test_serve_start_missing_team_400(self) -> None:
        status, payload = _request(
            self.port,
            "/api/runs/run-1/serve/start",
            method="POST",
            body={"team_path": "no/such/team.yaml"},
        )
        self.assertEqual(status, 400)

    def test_connections_carry_login_fields(self) -> None:
        status, payload = _request(self.port, "/api/connections")
        self.assertEqual(status, 200)
        for item in payload["connections"]:
            self.assertIn("login_capable", item)
            self.assertIn("logged_in", item)
        by_name = {item["backend"]: item for item in payload["connections"]}
        self.assertFalse(by_name["fake"]["login_capable"])
        self.assertIsInstance(by_name["codex"]["login_capable"], bool)
        self.assertIn(
            by_name["codebuddy"]["logged_in"], (True, False, None)
        )
        self.assertNotIn("/login", by_name["codebuddy"]["login_command"])
        self.assertNotIn("/login", by_name["codebuddy"]["note"])

    def test_codebuddy_sdk_probe_fallback_is_used_when_auth_files_are_unreadable(self) -> None:
        from orchestrator.console import settings as s

        with mock.patch.object(s, "codebuddy_login_state", return_value=None), mock.patch.object(
            s, "_codebuddy_auth_dir", return_value=self.project
        ), mock.patch.object(
            s, "_codebuddy_sdk_login_state", return_value=True
        ) as sdk_probe, mock.patch.object(
            s.shutil, "which", return_value=str(self.project / "codebuddy.cmd")
        ), mock.patch.object(
            s, "_local_cli_path", return_value=None
        ), mock.patch.object(
            s, "_run_version", return_value="test"
        ):
            result = s.probe_connections(self.project)

        codebuddy = next(item for item in result if item["backend"] == "codebuddy")
        self.assertTrue(codebuddy["logged_in"])
        sdk_probe.assert_called_once()

    def test_codebuddy_unreadable_auth_store_is_not_reported_as_logged_out(self) -> None:
        from orchestrator.console import settings as s

        with mock.patch.object(s, "codebuddy_login_state", return_value=None), mock.patch.object(
            s, "_codebuddy_auth_visibility", return_value="unreadable"
        ), mock.patch.object(
            s, "_codebuddy_sdk_login_state", return_value=False
        ) as sdk_probe, mock.patch.object(
            s.shutil, "which", return_value=str(self.project / "codebuddy.cmd")
        ), mock.patch.object(
            s, "_local_cli_path", return_value=None
        ), mock.patch.object(
            s, "_run_version", return_value="test"
        ):
            result = s.probe_connections(self.project)

        codebuddy = next(item for item in result if item["backend"] == "codebuddy")
        self.assertIsNone(codebuddy["logged_in"])
        self.assertEqual(codebuddy["login_probe"], "unreadable")
        self.assertIn("无权读取", codebuddy["login_status_hint"])
        sdk_probe.assert_not_called()

    def test_connection_login_rejects_fake(self) -> None:
        status, payload = _request(
            self.port,
            "/api/connections/login",
            method="POST",
            body={"backend": "fake"},
        )
        self.assertEqual(status, 400)

    def test_connection_login_calls_launcher(self) -> None:
        with mock.patch(
            "orchestrator.console.login_flow.start",
            return_value={"ok": True, "message": "opened", "script": "x.cmd"},
        ) as launcher:
            status, payload = _request(
                self.port,
                "/api/connections/login",
                method="POST",
                body={"backend": "codebuddy"},
            )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        launcher.assert_called_once()
        self.assertEqual(launcher.call_args.args[0], self.project)


class ConsoleBusyTests(unittest.TestCase):
    """serve 持权时，协调写（cancel/pause）返回 409，控制台不绕过。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "proj"
        self.project.mkdir()
        self.store = SQLiteStateStore(self.project / "state.db")
        self.store.create_run("run-1", "default")
        # serve 身份先持有 controller + authority
        self.serve_controller = self.store.acquire_run_controller(
            "run-1", "serve", lease_seconds=60
        )
        self.store.acquire_authority("run-1", "serve", "supervisor", lease_seconds=60)
        self.server = ConsoleHTTPServer(
            self.store,
            project_root=self.project,
            db_path=self.project / "state.db",
            host="127.0.0.1",
            port=0,
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self.temp.cleanup()

    def test_coordinated_writes_are_busy(self) -> None:
        self.store.create_task("run-1", "task-1", cwd=str(self.project))
        status, payload = _request(
            self.port,
            "/api/runs/run-1/pause",
            method="POST",
            body={"reason": "x"},
        )
        self.assertEqual(status, 409, payload)

    def test_review_can_use_active_serve_lease(self) -> None:
        # 只有以 serve-* 命名的控制器允许人工审核复用租约；普通控制器仍 busy。
        self.store.release_run_controller(self.serve_controller)
        self.serve_controller = self.store.acquire_run_controller(
            "run-1", "serve-test", lease_seconds=60
        )
        with self.store.connection:
            self.store.connection.execute(
                "UPDATE authority_leases SET owner_agent_id='serve-test' WHERE run_id='run-1'"
            )
        self.store.create_task("run-1", "task-review", cwd=str(self.project))
        self.store.transition_task("task-review", TaskState.READY, reason="test")
        self.store.create_attempt("task-review", "attempt-review", "agent-review")
        self.store.transition_task("task-review", TaskState.REVIEW, reason="test")
        status, payload = _request(
            self.port,
            "/api/runs/run-1/tasks/task-review/review",
            method="POST",
            body={"decision": "approve"},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.store.task_state("task-review"), TaskState.COMPLETED)


class FindFreePortTests(unittest.TestCase):
    """端口自动顺延：请求端口被占（如 Steam 占 8080）时选下一个空闲端口。"""

    def test_skips_busy_port(self) -> None:
        import socket

        from orchestrator.console.server import find_free_port

        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        try:
            busy = blocker.getsockname()[1]
            pick = find_free_port("127.0.0.1", busy, tries=50)
            self.assertIsNotNone(pick)
            self.assertNotEqual(pick, busy)
            # 选出的端口确实可绑定
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe.bind(("127.0.0.1", pick))
            probe.close()
        finally:
            blocker.close()

    def test_all_busy_returns_none(self) -> None:
        import socket

        from orchestrator.console.server import find_free_port

        blockers: list[socket.socket] = []
        start = 0
        try:
            for _ in range(5):
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.bind(("127.0.0.1", 0))
                s.listen(1)
                blockers.append(s)
            # 找一个被占端口作为起点，tries=1 保证范围只有它自己
            busy = blockers[0].getsockname()[1]
            self.assertIsNone(find_free_port("127.0.0.1", busy, tries=1))
        finally:
            for s in blockers:
                s.close()


class LoginHelperTests(unittest.TestCase):
    """登录引导脚本生成（dry_run，不弹窗）与未知后端拒绝。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "proj"
        self.project.mkdir(parents=True)
        self.codex_cli = self.project / "codex.cmd"
        self.codex_cli.write_text("@echo off\r\n", encoding="ascii")
        self.cb_cli = self.project / "codebuddy.cmd"
        self.cb_cli.write_text("@echo off\r\n", encoding="ascii")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _launch(self, backend: str):
        import os

        from orchestrator.console import settings as s

        with mock.patch.dict(
            os.environ,
            {"AGENT_HUB_CODEBUDDY_BIN": "", "CODEBUDDY_CODE_PATH": ""},
            clear=False,
        ), mock.patch.object(
            s.shutil, "which", return_value=str(self.codex_cli)
        ), mock.patch.object(
            s, "_local_cli_path", return_value=self.cb_cli
        ):
            return s.launch_login(self.project, backend, dry_run=True)

    def test_codebuddy_script_has_env_and_cli(self) -> None:
        result = self._launch("codebuddy")
        self.assertTrue(result["ok"], result)
        script = Path(result["script"])
        self.assertTrue(script.is_file())
        text = script.read_text(encoding="ascii")
        self.assertIn("CODEBUDDY_SKIP_GIT_BASH_CHECK=1", text)
        self.assertIn("CODEBUDDY_INTERNET_ENVIRONMENT=internal", text)
        self.assertIn(f'cd /d "{self.project}"', text)
        self.assertIn("-m orchestrator auth codebuddy --open-browser", text)
        self.assertNotIn("/login", text)

    def test_codebuddy_login_uses_background_browser_auth(self) -> None:
        import os

        from orchestrator.console import settings as s

        with mock.patch.dict(
            os.environ,
            {"AGENT_HUB_CODEBUDDY_BIN": "", "CODEBUDDY_CODE_PATH": ""},
            clear=False,
        ), mock.patch.object(s.shutil, "which", return_value=str(self.codex_cli)), mock.patch.object(
            s, "_local_cli_path", return_value=self.cb_cli
        ), mock.patch("sys.platform", "win32"), mock.patch.object(
            s.subprocess, "CREATE_NO_WINDOW", 1, create=True
        ), mock.patch.object(
            s.subprocess, "DETACHED_PROCESS", 2, create=True
        ), mock.patch.object(s.subprocess, "Popen") as popen:
            popen.return_value = mock.Mock(pid=1234, poll=mock.Mock(return_value=None))
            result = s.launch_login(self.project, "codebuddy")

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["mode"], "background-browser")
        self.assertEqual(result["pid"], 1234)
        argv = popen.call_args.args[0]
        self.assertEqual(argv[-3:], ["auth", "codebuddy", "--open-browser"])
        self.assertEqual(popen.call_args.kwargs["cwd"], str(self.project))
        launch_env = popen.call_args.kwargs["env"]
        self.assertEqual(launch_env["CODEBUDDY_INTERNET_ENVIRONMENT"], "internal")
        self.assertTrue(popen.call_args.kwargs["creationflags"])

    def test_codebuddy_login_prefers_project_venv_python(self) -> None:
        import os

        from orchestrator.console import settings as s

        venv_python = self.project / ".venv" / "Scripts" / "python.exe"
        venv_python.parent.mkdir(parents=True)
        venv_python.write_text("", encoding="ascii")
        with mock.patch.dict(
            os.environ,
            {"AGENT_HUB_CODEBUDDY_BIN": "", "CODEBUDDY_CODE_PATH": ""},
            clear=False,
        ), mock.patch.object(s.shutil, "which", return_value=str(self.codex_cli)), mock.patch.object(
            s, "_local_cli_path", return_value=self.cb_cli
        ), mock.patch("sys.platform", "win32"), mock.patch.object(
            s.subprocess, "CREATE_NO_WINDOW", 1, create=True
        ), mock.patch.object(
            s.subprocess, "DETACHED_PROCESS", 2, create=True
        ), mock.patch.object(s.subprocess, "Popen") as popen:
            popen.return_value = mock.Mock(pid=5678, poll=mock.Mock(return_value=None))
            result = s.launch_login(self.project, "codebuddy")

        self.assertTrue(result["ok"], result)
        self.assertEqual(popen.call_args.args[0][0], str(venv_python))

    def test_codebuddy_login_reports_immediate_child_exit(self) -> None:
        import os

        from orchestrator.console import settings as s

        with mock.patch.dict(
            os.environ,
            {"AGENT_HUB_CODEBUDDY_BIN": "", "CODEBUDDY_CODE_PATH": ""},
            clear=False,
        ), mock.patch.object(s.shutil, "which", return_value=str(self.codex_cli)), mock.patch.object(
            s, "_local_cli_path", return_value=self.cb_cli
        ), mock.patch("sys.platform", "win32"), mock.patch.object(
            s.subprocess, "CREATE_NO_WINDOW", 1, create=True
        ), mock.patch.object(
            s.subprocess, "DETACHED_PROCESS", 2, create=True
        ), mock.patch.object(s.subprocess, "Popen") as popen:
            popen.return_value = mock.Mock(pid=9876, poll=mock.Mock(return_value=2))
            result = s.launch_login(self.project, "codebuddy")

        self.assertFalse(result["ok"])
        self.assertIn("exit code 2", result["message"])

    def test_codex_script_runs_login(self) -> None:
        result = self._launch("codex")
        self.assertTrue(result["ok"], result)
        text = Path(result["script"]).read_text(encoding="ascii")
        self.assertIn("codex.cmd", text)
        self.assertIn("login", text)

    def test_unknown_backend_ok_false(self) -> None:
        from orchestrator.console import settings as s

        result = s.launch_login(self.project, "fake", dry_run=True)
        self.assertFalse(result["ok"])


class ConsoleWriteLoopTests(unittest.TestCase):
    """写任务闭环 UI 后端：worktree 自动准备 + REVIEW 通过/打回。

    项目根是受管 git 仓库（GitWorkspaceManager 初始化）；写任务产出
    commit 后通过网页审核触发真实集成（MergeExecutor）→ COMPLETED。
    """

    def setUp(self) -> None:
        from orchestrator.workspace.git_manager import GitWorkspaceManager

        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "proj"
        self.manager = GitWorkspaceManager(
            self.project, self.project / ".agent-hub" / "worktrees"
        )
        self.base = self.manager.initialize_repository()
        # db 放项目外（避免污染受管主仓库工作区，影响集成前 clean 检查）
        self.db_path = Path(self.temp.name) / "state.db"
        self.store = SQLiteStateStore(self.db_path)
        self.store.create_run("run-1", "default")
        self.server = ConsoleHTTPServer(
            self.store,
            project_root=self.project,
            db_path=self.db_path,
            host="127.0.0.1",
            port=0,
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server.close()
        self.store.close()
        self.temp.cleanup()

    def _review_task(self, task_id: str, *, scope: tuple[str, ...] = ("demo/x.txt",)):
        """造一个停在 REVIEW 的写任务（带一个 attempt）。"""
        self.store.create_task(
            "run-1",
            task_id,
            access_mode="write",
            write_scope=scope,
            required_role_id="worker",
            prompt=f"write {task_id}",
            cwd=str(self.project / ".agent-hub" / "worktrees" / "run-1"),
            timeout_seconds=5,
        )
        self.store.transition_task(task_id, TaskState.READY, reason="test")
        self.store.create_attempt(task_id, f"attempt-{task_id}", f"agent-{task_id}")
        self.store.transition_task(task_id, TaskState.REVIEW, reason="test")

    def test_worktree_prepare_is_idempotent(self) -> None:
        status, payload = _request(
            self.port, "/api/runs/run-1/worktree", method="POST", body={}
        )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["created"])
        worktree = Path(payload["worktree"])
        self.assertTrue(worktree.is_dir())
        self.assertEqual(payload["base_commit"], self.base)
        # 幂等：再次准备返回同一 worktree，created=False
        status, payload2 = _request(
            self.port, "/api/runs/run-1/worktree", method="POST", body={}
        )
        self.assertEqual(status, 200, payload2)
        self.assertEqual(payload2["worktree"], payload["worktree"])
        self.assertFalse(payload2["created"])
        # GET 资源同样可见
        status, payload3 = _request(self.port, "/api/runs/run-1/worktree")
        self.assertEqual(status, 200, payload3)
        self.assertTrue(payload3["worktree"]["exists"])

    def test_review_rework_returns_task_to_ready(self) -> None:
        self._review_task("task-rw")
        status, payload = _request(
            self.port,
            "/api/runs/run-1/tasks/task-rw/review",
            method="POST",
            body={"decision": "rework", "comment": "needs more work"},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.store.task_state("task-rw"), TaskState.READY)
        row = self.store.connection.execute(
            "SELECT decision FROM review_decisions WHERE task_id='task-rw'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["decision"], "REWORK")

    def test_review_approve_write_integrates_to_completed(self) -> None:
        # 先准备 worktree（网页自动做），再写任务产出文件
        status, wt = _request(
            self.port, "/api/runs/run-1/worktree", method="POST", body={}
        )
        self.assertEqual(status, 200, wt)
        worktree = Path(wt["worktree"])
        target = worktree / "demo" / "x.txt"
        target.write_text("console-result\n", encoding="utf-8")

        self._review_task("task-ap")
        status, payload = _request(
            self.port,
            "/api/runs/run-1/tasks/task-ap/review",
            method="POST",
            body={"decision": "approve", "comment": "looks good"},
        )
        self.assertEqual(status, 200, payload)
        # 真实集成：任务 COMPLETED，主仓库含产出文件
        self.assertEqual(self.store.task_state("task-ap"), TaskState.COMPLETED)
        blob = self.manager.read_blob(
            self.manager.head(self.manager.repository), "demo/x.txt"
        )[1]
        self.assertIn(b"console-result", blob)
        # human APPROVED 落库
        row = self.store.connection.execute(
            "SELECT decision FROM review_decisions WHERE task_id='task-ap'"
        ).fetchone()
        self.assertEqual(row["decision"], "APPROVED")

    def test_review_approve_read_only_completes_without_git(self) -> None:
        self.store.create_task(
            "run-1", "task-ro", required_role_id="worker", prompt="read",
            cwd=str(self.project), timeout_seconds=5,
        )
        self.store.transition_task("task-ro", TaskState.READY, reason="test")
        self.store.create_attempt("task-ro", "attempt-ro", "agent-ro")
        self.store.transition_task("task-ro", TaskState.REVIEW, reason="test")
        status, payload = _request(
            self.port,
            "/api/runs/run-1/tasks/task-ro/review",
            method="POST",
            body={"decision": "approve"},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.store.task_state("task-ro"), TaskState.COMPLETED)
        # 没有 merge 入队（只读无 git 产出）
        rows = self.store.connection.execute(
            "SELECT COUNT(*) AS c FROM merge_queue WHERE task_id='task-ro'"
        ).fetchone()
        self.assertEqual(rows["c"], 0)

    def test_review_rejects_bad_decision_and_non_review(self) -> None:
        self._review_task("task-bad")
        status, payload = _request(
            self.port,
            "/api/runs/run-1/tasks/task-bad/review",
            method="POST",
            body={"decision": "maybe"},
        )
        self.assertEqual(status, 400, payload)
        # 非 REVIEW 状态拒绝
        self.store.transition_task("task-bad", TaskState.READY, reason="reset")
        status, payload = _request(
            self.port,
            "/api/runs/run-1/tasks/task-bad/review",
            method="POST",
            body={"decision": "approve"},
        )
        self.assertEqual(status, 400, payload)


if __name__ == "__main__":
    unittest.main()
