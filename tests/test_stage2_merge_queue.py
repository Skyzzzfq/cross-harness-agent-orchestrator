from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from orchestrator.adapters.fake import FakeBackendAdapter, FakeBehavior
from orchestrator.agent_pool import reconcile_pool_once
from orchestrator.adapters.contracts import CallState
from orchestrator.core.config import AgentPoolSpec
from orchestrator.core.models import AttemptState, TaskState
from orchestrator.scheduler import scheduler_tick
from orchestrator.storage.sqlite_store import SQLiteStateStore


class WriteScopeSerializationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteStateStore(Path(self.temp.name) / "state.db")
        self.store.create_run("run-1", "team-1")
        self.authority = self.store.acquire_authority(
            "run-1", "test-supervisor", "supervisor"
        )
        reconcile_pool_once(
            self.store,
            "run-1",
            AgentPoolSpec(
                pool_id="fake-workers",
                backend="fake",
                role_id="worker",
                count=2,
                max_count=2,
                model="fake-v1",
            ),
        )

    async def asyncTearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _task(self, task_id: str, *, access_mode: str, write_scope: tuple[str, ...] = ()) -> None:
        self.store.create_task(
            "run-1",
            task_id,
            access_mode=access_mode,
            write_scope=write_scope,
            required_role_id="worker",
            prompt=f"execute {task_id}",
            cwd="D:/workspace/connect",
            timeout_seconds=5,
        )
        self.store.transition_task(task_id, TaskState.READY, reason="ready")

    async def test_overlapping_write_scope_serializes_dispatch(self) -> None:
        # 两个写任务声明重叠 scope：第一个被派发后，第二个必须等待
        self._task("task-a", access_mode="write", write_scope=("demo/a.txt",))
        self._task("task-b", access_mode="write", write_scope=("demo/a.txt",))
        adapter = FakeBackendAdapter(
            default_behavior=FakeBehavior(delay_seconds=0.1, text="done")
        )
        # 第一个 tick：task-a 被派发（ACTIVE），task-b 因重叠被阻止
        await scheduler_tick(
            self.store,
            run_id="run-1",
            adapters={"fake": adapter},
            authority=self.authority,
        )
        self.assertEqual(self.store.task_state("task-a"), TaskState.REVIEW)
        self.assertIn(
            self.store.task_state("task-b"),
            {TaskState.READY, TaskState.ACTIVE},
        )
        # task-a 完成后（REVIEW），下一个 tick 才能派发 task-b
        await scheduler_tick(
            self.store,
            run_id="run-1",
            adapters={"fake": adapter},
            authority=self.authority,
        )
        self.assertEqual(self.store.task_state("task-b"), TaskState.REVIEW)

    async def test_non_overlapping_write_scope_dispatches_in_parallel(self) -> None:
        self._task("task-a", access_mode="write", write_scope=("demo/a.txt",))
        self._task("task-b", access_mode="write", write_scope=("demo/b.txt",))
        adapter = FakeBackendAdapter(
            default_behavior=FakeBehavior(delay_seconds=0.05, text="done")
        )
        await scheduler_tick(
            self.store,
            run_id="run-1",
            adapters={"fake": adapter},
            authority=self.authority,
        )
        self.assertEqual(self.store.task_state("task-a"), TaskState.REVIEW)
        self.assertEqual(self.store.task_state("task-b"), TaskState.REVIEW)


class MergeQueueOutboxTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteStateStore(Path(self.temp.name) / "state.db")
        self.store.create_run("run-1", "team-1")
        self.authority = self.store.acquire_authority(
            "run-1", "test-supervisor", "supervisor"
        )

    async def asyncTearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _controller(self) -> object:
        return self.store.acquire_run_controller("run-1", "operator", lease_seconds=60)

    def _reviewed_task(self, task_id: str) -> None:
        self.store.create_task("run-1", task_id)
        self.store.transition_task(task_id, TaskState.READY, reason="ready")
        self.store.transition_task(task_id, TaskState.ACTIVE, reason="assigned")
        self.store.transition_task(task_id, TaskState.REVIEW, reason="submitted")

    async def test_enqueue_and_claim_merge_is_atomic(self) -> None:
        token = self._controller()
        self.store.create_task("run-1", "task-1")
        self.store.create_task("run-1", "task-2")
        self.store.enqueue_merge(
            "run-1", "task-1", "attempt-1", "abc123", "base0", token,
            authority=self.authority, reason="review-passed",
        )
        self.store.enqueue_merge(
            "run-1", "task-2", "attempt-2", "def456", "base0", token,
            authority=self.authority, reason="review-passed",
        )
        claimed = self.store.claim_merge_queue("run-1", token, authority=self.authority)
        self.assertEqual(claimed["task_id"], "task-1")
        # 同一事务内重复 claim 拿不到已领取的
        second = self.store.claim_merge_queue("run-1", token, authority=self.authority)
        self.assertEqual(second["task_id"], "task-2")

    async def test_merge_apply_success_completes_task_and_writes_outbox(self) -> None:
        token = self._controller()
        self._reviewed_task("task-1")
        self.store.enqueue_merge(
            "run-1", "task-1", "attempt-1", "abc123", "base0", token,
            authority=self.authority, reason="review-passed",
        )
        claim = self.store.claim_merge_queue("run-1", token, authority=self.authority)
        self.store.record_outbox_intent(
            "run-1", "merge", claim["merge_id"], "merge.applied", {"commit": "abc123"}, token
        )
        self.store.finish_merge(
            claim["merge_id"], "applied", token,
            authority=self.authority, result_commit="abc123",
        )
        self.assertEqual(self.store.task_state("task-1"), TaskState.COMPLETED)
        outbox = self.store.connection.execute(
            "SELECT event_type, status FROM outbox WHERE run_id='run-1'"
        ).fetchall()
        self.assertEqual(len(outbox), 1)
        self.assertEqual(outbox[0]["event_type"], "merge.applied")

    async def test_merge_conflict_records_integration_issue(self) -> None:
        token = self._controller()
        self._reviewed_task("task-1")
        self.store.enqueue_merge(
            "run-1", "task-1", "attempt-1", "abc123", "base0", token,
            authority=self.authority, reason="review-passed",
        )
        claim = self.store.claim_merge_queue("run-1", token, authority=self.authority)
        self.store.finish_merge(
            claim["merge_id"],
            "conflict",
            token,
            authority=self.authority,
            issue_kind="content_conflict",
            issue_detail={"paths": ["demo/a.txt"]},
        )
        self.assertEqual(self.store.task_state("task-1"), TaskState.REVIEW)
        issues = self.store.connection.execute(
            "SELECT kind FROM integration_issues WHERE task_id='task-1'"
        ).fetchall()
        self.assertEqual(issues[0]["kind"], "content_conflict")

    async def test_merge_reconcile_does_not_duplicate_applied_merge(self) -> None:
        token = self._controller()
        self.store.create_task("run-1", "task-1")
        self.store.enqueue_merge(
            "run-1", "task-1", "attempt-1", "abc123", "base0", token,
            authority=self.authority, reason="review-passed",
        )
        claim = self.store.claim_merge_queue("run-1", token, authority=self.authority)
        self.store.finish_merge(
            claim["merge_id"], "applied", token,
            authority=self.authority, result_commit="abc123",
        )
        # 重启后对账：已 APPLIED 的记录不应被再次 claim 或 apply
        reconciled = self.store.reconcile_merge_queue("run-1", token)
        self.assertEqual(reconciled["reapplied"], [])


class OutboxDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteStateStore(Path(self.temp.name) / "state.db")
        self.store.create_run("run-1", "team-1")

    async def asyncTearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _controller(self) -> object:
        return self.store.acquire_run_controller("run-1", "operator", lease_seconds=60)

    async def test_outbox_intent_and_delivery_flow(self) -> None:
        token = self._controller()
        self.store.record_outbox_intent(
            "run-1", "merge", "merge-1", "merge.applied", {"commit": "x"}, token
        )
        intent = self.store.claim_outbox("run-1", token)
        self.assertIsNotNone(intent)
        self.store.finish_outbox(intent["outbox_id"], "sent", token)
        row = self.store.connection.execute(
            "SELECT status FROM outbox WHERE outbox_id=?", (intent["outbox_id"],)
        ).fetchone()
        self.assertEqual(row["status"], "SENT")


if __name__ == "__main__":
    unittest.main()
