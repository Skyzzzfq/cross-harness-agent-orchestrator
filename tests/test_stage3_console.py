from __future__ import annotations

import json
import tempfile
import threading
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
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class ConsoleServerTests(unittest.TestCase):
    """T3：状态页 + 管理控制台 API。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteStateStore(Path(self.temp.name) / "state.db")
        self.store.create_run("run-1", "team-1")
        self.server = ConsoleHTTPServer(
            self.store, "run-1", host="127.0.0.1", port=0, owner="human-ops"
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

    def test_status_reports_writable_when_holding_controller(self) -> None:
        status, payload = _request(self.port, "/api/status")
        self.assertEqual(status, 200)
        self.assertTrue(payload["writable"])
        self.assertEqual(payload["run_id"], "run-1")
        self.assertEqual(payload["owner"], "human-ops")
        self.assertIn("summary", payload)

    def test_create_task_via_console(self) -> None:
        status, payload = _request(
            self.port,
            "/api/runs/run-1/tasks",
            method="POST",
            body={
                "task_id": "task-1",
                "access_mode": "read_only",
                "prompt": "hello",
                "cwd": str(self.temp.name),
            },
        )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(self.store.task_state("task-1"), TaskState.READY)

    def test_tasks_endpoint_lists_tasks(self) -> None:
        self.store.create_task("run-1", "task-1", cwd=str(self.temp.name))
        self.store.transition_task("task-1", TaskState.READY, reason="test")
        status, payload = _request(self.port, "/api/runs/run-1/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["tasks"]), 1)
        self.assertEqual(payload["tasks"][0]["task_id"], "task-1")

    def test_events_and_merges_and_approvals_endpoints(self) -> None:
        self.store.create_task("run-1", "task-1", cwd=str(self.temp.name))
        self.store.transition_task("task-1", TaskState.READY, reason="test")
        for resource in ("events", "merges", "approvals", "outbox", "agents"):
            status, payload = _request(self.port, f"/api/runs/run-1/{resource}")
            self.assertEqual(status, 200, resource)
            self.assertEqual(payload["run_id"], "run-1")
            self.assertIn(resource, payload)

    def test_cancel_task_via_console(self) -> None:
        self.store.create_task("run-1", "task-1", cwd=str(self.temp.name))
        self.store.transition_task("task-1", TaskState.READY, reason="test")
        status, payload = _request(
            self.port,
            "/api/runs/run-1/tasks/task-1/cancel",
            method="POST",
            body={"reason": "console-cancel"},
        )
        self.assertEqual(status, 200, payload)
        self.assertIn(self.store.task_state("task-1"), {TaskState.CANCELLED, TaskState.CANCEL_REQUESTED})

    def test_pause_and_resume_via_console(self) -> None:
        status, payload = _request(
            self.port,
            "/api/runs/run-1/pause",
            method="POST",
            body={"reason": "console-pause"},
        )
        self.assertEqual(status, 200, payload)
        control = self.store.connection.execute(
            "SELECT control_state FROM runs WHERE run_id='run-1'"
        ).fetchone()["control_state"]
        self.assertEqual(control, "PAUSED")
        status, payload = _request(
            self.port,
            "/api/runs/run-1/resume",
            method="POST",
            body={"reason": "console-resume"},
        )
        self.assertEqual(status, 200, payload)
        control = self.store.connection.execute(
            "SELECT control_state FROM runs WHERE run_id='run-1'"
        ).fetchone()["control_state"]
        self.assertEqual(control, "RUNNING")


class ConsoleReadOnlyTests(unittest.TestCase):
    """T3：serve 持有协调权时，控制台退化为只读，写操作返回 409。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteStateStore(Path(self.temp.name) / "state.db")
        self.store.create_run("run-1", "team-1")
        # serve 身份先持有 controller + authority
        self.serve_controller = self.store.acquire_run_controller(
            "run-1", "serve", lease_seconds=60
        )
        self.store.acquire_authority("run-1", "serve", "supervisor", lease_seconds=60)

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_console_is_read_only_when_serve_holds_controller(self) -> None:
        server = ConsoleHTTPServer(
            self.store, "run-1", host="127.0.0.1", port=0, owner="human-ops"
        )
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, payload = _request(port, "/api/status")
            self.assertEqual(status, 200)
            self.assertFalse(payload["writable"])
            status, payload = _request(
                port,
                "/api/runs/run-1/tasks",
                method="POST",
                body={"task_id": "task-1", "prompt": "x"},
            )
            self.assertEqual(status, 409)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
