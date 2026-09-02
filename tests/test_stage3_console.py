from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
