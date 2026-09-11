from __future__ import annotations

import json
import tempfile
import threading
import urllib.error
import urllib.request
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orchestrator.console.server import ConsoleHTTPServer
from orchestrator.history import history_cleanup_preview
from orchestrator.recovery import recovery_snapshot, run_recovery_check
from orchestrator.reconciler import reconcile_once
from orchestrator.core.models import TaskState
from orchestrator.storage.sqlite_store import SQLiteStateStore


def request(port: int, path: str):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class PersonalR7RecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        self.db = self.root / "state.db"
        self.store = SQLiteStateStore(self.db)
        self.store.create_run("run-r7", "default")

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_recovery_snapshot_is_read_only_and_reports_expired_attempt(self) -> None:
        self.store.create_task("run-r7", "task-r7", prompt="recover", cwd=str(self.root))
        self.store.transition_task("task-r7", TaskState.READY, reason="r7-test")
        lease = self.store.create_attempt_with_lease(
            "task-r7", "attempt-r7", "agent-r7", lease_seconds=1
        )
        observed = (
            datetime.now(timezone.utc) + timedelta(seconds=5)
        ).isoformat()
        before = self.store.connection.execute(
            "SELECT state FROM attempts WHERE attempt_id='attempt-r7'"
        ).fetchone()["state"]
        report = recovery_snapshot(self.store, run_id="run-r7", now=observed)
        after = self.store.connection.execute(
            "SELECT state FROM attempts WHERE attempt_id='attempt-r7'"
        ).fetchone()["state"]
        self.assertEqual(before, "ASSIGNED")
        self.assertEqual(after, before)
        self.assertEqual(report["status"], "ready")
        self.assertTrue(report["recovery_required"])
        self.assertEqual(report["runs"][0]["expired_attempts"][0]["attempt_id"], "attempt-r7")
        self.assertEqual(report["runs"][0]["expired_attempts"][0]["generation"], 1)
        self.assertEqual(lease["generation"], 1)

    def test_reconciler_recovers_expired_attempt_once(self) -> None:
        self.store.create_task("run-r7", "task-reconcile", prompt="recover", cwd=str(self.root))
        self.store.transition_task("task-reconcile", TaskState.READY, reason="r7-test")
        self.store.create_attempt_with_lease(
            "task-reconcile", "attempt-reconcile", "agent-r7", lease_seconds=1
        )
        observed = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
        first = reconcile_once(self.store, run_id="run-r7", now=observed)
        second = reconcile_once(self.store, run_id="run-r7", now=observed)
        self.assertEqual(len(first["recovered"]), 1)
        self.assertEqual(second["recovered"], [])
        self.assertEqual(self.store.attempt_state("attempt-reconcile").value, "STALE")

    def test_recovery_check_writes_credential_free_report(self) -> None:
        result = run_recovery_check(self.root, database_path=self.db, run_id="run-r7")
        self.assertEqual(result["status"], "ready")
        report_path = Path(result["report_path"])
        self.assertTrue(report_path.is_file())
        text = report_path.read_text(encoding="utf-8")
        self.assertNotIn("api_key", text.lower())
        self.assertTrue(result["read_only"])

    def test_history_preview_blocks_active_runs_and_marks_empty_old_run(self) -> None:
        self.store.create_run("run-empty", "default")
        self.store.create_task("run-r7", "task-active", prompt="active", cwd=str(self.root))
        self.store.transition_task("task-active", TaskState.READY, reason="r7-test")
        now = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        preview = history_cleanup_preview(
            self.store, older_than_days=0, now=now
        )
        by_id = {item["run_id"]: item for item in preview["runs"]}
        self.assertFalse(by_id["run-r7"]["eligible"])
        self.assertIn("active_tasks", by_id["run-r7"]["reasons"])
        self.assertTrue(by_id["run-empty"]["eligible"])
        self.assertFalse(preview["destructive_action"])

    def test_console_exposes_recovery_and_history_preview(self) -> None:
        server = ConsoleHTTPServer(
            self.store,
            project_root=self.root,
            db_path=self.db,
            host="127.0.0.1",
            port=0,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, payload = request(self.port(server), "/api/recovery?run_id=run-r7")
            self.assertEqual(status, 200, payload)
            self.assertEqual(payload["runs"][0]["run_id"], "run-r7")
            status, payload = request(
                self.port(server), "/api/history/preview?older_than_days=0"
            )
            self.assertEqual(status, 200, payload)
            self.assertIn("runs", payload)
        finally:
            server.shutdown()
            server.server_close()
            server.close()

    @staticmethod
    def port(server: ConsoleHTTPServer) -> int:
        return int(server.server_address[1])


if __name__ == "__main__":
    unittest.main()
