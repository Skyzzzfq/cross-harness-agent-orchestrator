from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from orchestrator.console.server import ConsoleHTTPServer
from orchestrator.core.models import AttemptState, TaskState
from orchestrator.storage.sqlite_store import SQLiteStateStore


def request(port: int, path: str, *, method: str = "GET", body: dict | None = None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class PersonalR6ConsoleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        (self.project / "config").mkdir()
        (self.project / "config" / "team.yaml").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "team_id": "default",
                    "bootstrap_supervisor": "supervisor",
                    "roles": [
                        {"role_id": "worker", "version": 1, "title": "Worker", "required_capabilities": ["read"]},
                        {"role_id": "supervisor", "version": 1, "title": "Supervisor", "required_capabilities": ["plan"]},
                    ],
                    "agent_pools": [
                        {"pool_id": "pool-fake", "backend": "fake", "role_id": "worker", "count": 1, "max_count": 1},
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.db = self.project / "state.db"
        self.store = SQLiteStateStore(self.db)
        self.store.create_run("run-r6", "default")
        self.server = ConsoleHTTPServer(
            self.store,
            project_root=self.project,
            db_path=self.db,
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

    def _active_task(self, task_id: str = "task-r6") -> None:
        self.store.create_task(
            "run-r6", task_id, prompt="initial request", cwd=str(self.project)
        )
        self.store.transition_task(task_id, TaskState.READY, reason="r6-test")
        self.store.create_attempt(task_id, "attempt-r6", "agent-r6")
        self.store.transition_attempt("attempt-r6", AttemptState.RUNNING, reason="r6-test")

    def test_projects_api_persists_metadata_without_touching_workspace(self) -> None:
        workspace = Path(self.temp.name) / "other-workspace"
        workspace.mkdir()
        status, payload = request(
            self.port,
            "/api/projects",
            method="POST",
            body={
                "project_id": "other",
                "name": "Other Workspace",
                "workspace": str(workspace),
                "default_team": "config/team.json",
            },
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["project"]["workspace"], str(workspace.resolve()))
        status, payload = request(self.port, "/api/projects")
        self.assertEqual(status, 200)
        saved = next(item for item in payload["projects"] if item["project_id"] == "other")
        self.assertFalse(saved["current"])
        self.assertTrue(workspace.is_dir())
        status, payload = request(self.port, "/api/projects/other", method="DELETE")
        self.assertEqual(status, 200, payload)
        self.assertTrue(workspace.is_dir())

    def test_chat_returns_project_tree_cursor_and_agent_filter(self) -> None:
        self._active_task()
        self.store.register_agent(
            agent_id="agent-r6",
            team_id="default",
            pool_id="pool-fake",
            backend="fake",
            model="fake-model",
        )
        self.store.bind_role(
            binding_id="binding-r6",
            run_id="run-r6",
            agent_id="agent-r6",
            role_id="worker",
            role_version=1,
        )
        self.store.create_task(
            "run-r6", "task-unassigned", prompt="another task", cwd=str(self.project)
        )
        self.store.transition_task("task-unassigned", TaskState.READY, reason="r6-test")
        status, payload = request(
            self.port, "/api/runs/run-r6/chat?cursor=0&agent_id=agent-r6"
        )
        self.assertEqual(status, 200, payload)
        chat = payload["chat"]
        self.assertEqual(chat["requested_cursor"], 0)
        self.assertGreater(chat["cursor"], 0)
        self.assertEqual(chat["project"]["team_id"], "default")
        self.assertEqual([item["task_id"] for item in chat["tasks"]], ["task-r6"])
        self.assertEqual(chat["roles"][0]["agents"][0]["agent_id"], "agent-r6")
        self.assertTrue(chat["events"])
        status, payload = request(
            self.port, f"/api/runs/run-r6/chat?cursor={chat['cursor']}"
        )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["chat"]["incremental"])
        self.assertEqual(payload["chat"]["events"], [])

    def test_guidance_is_queued_for_the_latest_active_attempt(self) -> None:
        self._active_task()
        status, payload = request(
            self.port,
            "/api/runs/run-r6/tasks/task-r6/guidance",
            method="POST",
            body={"text": "先验证输入，再报告阻塞原因。"},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["status"], "queued")
        deliveries = self.store.list_message_deliveries("run-r6", task_id="task-r6")
        self.assertEqual(len(deliveries), 1)
        envelope = json.loads(deliveries[0]["envelope_json"])
        self.assertEqual(envelope["message_type"], "user_guidance")
        self.assertEqual(envelope["target_agent_id"], "agent-r6")
        self.assertEqual(envelope["payload"]["text"], "先验证输入，再报告阻塞原因。")

    def test_guidance_rejects_closed_attempt(self) -> None:
        self._active_task()
        self.store.transition_attempt("attempt-r6", AttemptState.FAILED, reason="r6-test")
        status, payload = request(
            self.port,
            "/api/runs/run-r6/tasks/task-r6/guidance",
            method="POST",
            body={"text": "too late"},
        )
        self.assertEqual(status, 409, payload)
        self.assertIn("active attempt", payload["error"])


if __name__ == "__main__":
    unittest.main()
