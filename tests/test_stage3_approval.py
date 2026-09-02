from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator.core.models import TaskState
from orchestrator.storage.sqlite_store import SQLiteStateStore


class ApprovalAtomicConsumptionTests(unittest.TestCase):
    """B2（P1-03）：审批 expiry / params 原子消费。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteStateStore(Path(self.temp.name) / "state.db")
        self.store.create_run("run-1", "team-1")
        self.authority = self.store.acquire_authority(
            "run-1", "test-supervisor", "supervisor"
        )
        self.store.create_task("run-1", "task-1", cwd=str(Path(self.temp.name)))

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _create_approval(self, *, expires_at: str | None = None, params=None) -> str:
        return self.store.create_approval_request(
            "run-1",
            task_id="task-1",
            action_summary="merge into integration",
            params=params or {"commit": "abc123"},
            requested_by="supervisor",
            scope="integration-branch",
            single_use=True,
            expires_at=expires_at,
        )

    def test_expired_approval_cannot_be_decided(self) -> None:
        request_id = self._create_approval(
            expires_at="2020-01-01T00:00:00+00:00"
        )
        with self.assertRaises(ValueError):
            self.store.decide_approval(
                request_id, "APPROVED", decided_by="human-admin"
            )
        status = self.store.connection.execute(
            "SELECT status FROM approval_requests WHERE request_id=?", (request_id,)
        ).fetchone()["status"]
        self.assertEqual(status, "PENDING", "过期审批不得被消费")

    def test_params_mismatch_rejected_atomically(self) -> None:
        request_id = self._create_approval(params={"commit": "abc123"})
        with self.assertRaises(ValueError):
            self.store.decide_approval(
                request_id,
                "APPROVED",
                decided_by="human-admin",
                params={"commit": "different"},
            )
        status = self.store.connection.execute(
            "SELECT status FROM approval_requests WHERE request_id=?", (request_id,)
        ).fetchone()["status"]
        self.assertEqual(status, "PENDING")

    def test_matching_params_consumes_approval(self) -> None:
        request_id = self._create_approval(params={"commit": "abc123"})
        self.store.decide_approval(
            request_id, "APPROVED", decided_by="human-admin",
            params={"commit": "abc123"},
        )
        status = self.store.connection.execute(
            "SELECT status FROM approval_requests WHERE request_id=?", (request_id,)
        ).fetchone()["status"]
        self.assertEqual(status, "APPROVED")


class ReassignTaskTests(unittest.IsolatedAsyncioTestCase):
    """B2（P1-03）：重新分配命令——REVIEW/FAILED 任务回到 READY 重新派发。"""

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = SQLiteStateStore(root / "state.db")
        self.store.create_run("run-1", "team-1")
        self.authority = self.store.acquire_authority(
            "run-1", "test-supervisor", "supervisor"
        )
        self.controller = self.store.acquire_run_controller(
            "run-1", "op", lease_seconds=60
        )
        self.store.create_task("run-1", "task-1", cwd=str(root))

    async def asyncTearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _to_state(self, state: TaskState) -> None:
        self.store.transition_task("task-1", TaskState.READY, reason="ready")
        self.store.transition_task("task-1", TaskState.ACTIVE, reason="assigned")
        self.store.transition_task("task-1", state, reason="test")

    async def test_review_task_reassigned_to_ready(self) -> None:
        self._to_state(TaskState.REVIEW)
        self.store.reassign_task(
            "run-1", "task-1", self.controller, self.authority, reason="redo"
        )
        self.assertEqual(self.store.task_state("task-1"), TaskState.READY)
        event = self.store.connection.execute(
            "SELECT kind FROM events WHERE task_id='task-1' AND kind='task.reassigned'"
        ).fetchone()
        self.assertIsNotNone(event)

    async def test_failed_task_reassigned_to_ready(self) -> None:
        self._to_state(TaskState.FAILED)
        self.store.reassign_task(
            "run-1", "task-1", self.controller, self.authority, reason="retry"
        )
        self.assertEqual(self.store.task_state("task-1"), TaskState.READY)

    async def test_running_task_cannot_be_reassigned(self) -> None:
        self.store.transition_task("task-1", TaskState.READY, reason="ready")
        self.store.transition_task("task-1", TaskState.ACTIVE, reason="assigned")
        with self.assertRaises(ValueError):
            self.store.reassign_task(
                "run-1", "task-1", self.controller, self.authority, reason="x"
            )


if __name__ == "__main__":
    unittest.main()
