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

    def test_run_detail_endpoints(self) -> None:
        self.store.create_task("run-1", "task-1", cwd=str(self.project))
        self.store.transition_task("task-1", TaskState.READY, reason="test")
        for resource in ("summary", "tasks", "events", "merges", "approvals"):
            status, payload = _request(self.port, f"/api/runs/run-1/{resource}")
            self.assertEqual(status, 200, resource)
        status, payload = _request(self.port, "/api/runs/run-1/tasks")
        self.assertEqual(payload["tasks"][0]["task_id"], "task-1")

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
            "orchestrator.console.settings.launch_login",
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
        self.assertEqual(launcher.call_args.args[1], "codebuddy")


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
        self.assertIn("codebuddy.cmd", text)
        self.assertIn("/login", text)

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


if __name__ == "__main__":
    unittest.main()
