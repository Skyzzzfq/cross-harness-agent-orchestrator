from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator.core.models import TaskState
from orchestrator.storage.sqlite_store import SQLiteStateStore
from orchestrator.workspace.git_manager import GitWorkspaceManager
from orchestrator.workspace.merge_executor import MergeExecutor, OutboxDispatcher


def _make_reviewed_task(
    store: SQLiteStateStore, task_id: str = "task-1", attempt_id: str = "attempt-1"
) -> None:
    store.create_task("run-1", task_id)
    store.transition_task(task_id, TaskState.READY, reason="ready")
    store.transition_task(task_id, TaskState.ACTIVE, reason="assigned")
    store.transition_task(task_id, TaskState.REVIEW, reason="submitted")
    store.connection.execute(
        """
        INSERT INTO attempts(
            attempt_id, task_id, agent_id, state, attempt_number,
            generation, created_at, updated_at
        ) VALUES (?, ?, 'ag-1', 'SUBMITTED', 1, 1,
                  '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')
        """,
        (attempt_id, task_id),
    )
    store.connection.commit()


class MergeExecutorTests(unittest.IsolatedAsyncioTestCase):
    """P0-02：真实 Git 集成 + 常驻消费者（崩溃恢复）。"""

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.manager = GitWorkspaceManager(root / "repo", root / "worktrees")
        self.base = self.manager.initialize_repository()
        self.store = SQLiteStateStore(root / "state.db")
        self.store.create_run("run-1", "team-1")
        self.authority = self.store.acquire_authority(
            "run-1", "test-supervisor", "supervisor"
        )
        self.controller = self.store.acquire_run_controller(
            "run-1", "op", lease_seconds=60
        )
        self.executor = MergeExecutor(self.store, self.manager)

    async def asyncTearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _result_commit(self, path: str, content: str, msg: str) -> str:
        worktree = self.manager.create_worktree("worker", self.base)
        return self.manager.commit_file(worktree, path, content, msg)

    async def test_run_merge_applies_and_writes_outbox_same_flow(self) -> None:
        _make_reviewed_task(self.store)
        commit = self._result_commit("demo/a.txt", "result\n", "worker: result")
        self.store.enqueue_merge(
            "run-1", "task-1", "attempt-1", commit, self.base,
            self.controller, authority=self.authority, reason="review-passed",
        )
        result = self.executor.run_merge_once("run-1", self.controller, self.authority)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(self.store.task_state("task-1"), TaskState.COMPLETED)
        row = self.store.connection.execute(
            "SELECT status FROM merge_queue WHERE run_id='run-1'"
        ).fetchone()
        self.assertEqual(row["status"], "APPLIED")
        outbox = self.store.connection.execute(
            "SELECT status FROM outbox WHERE run_id='run-1'"
        ).fetchall()
        self.assertEqual(len(outbox), 1)
        self.assertEqual(outbox[0]["status"], "PENDING")

    async def test_run_merge_conflict_records_integration_issue(self) -> None:
        # 先有基线提交占住 demo/a.txt
        base_wt = self.manager.create_worktree("baseline", self.base)
        base_commit = self.manager.commit_file(
            base_wt, "demo/a.txt", "baseline\n", "baseline: a.txt"
        )
        self.manager.integrate(base_commit)
        # worker 也改同一文件 → cherry-pick 冲突
        _make_reviewed_task(self.store)
        conflicting = self.manager.commit_file(
            self.manager.create_worktree("worker2", self.base),
            "demo/a.txt",
            "worker change\n",
            "worker: conflict",
        )
        self.store.enqueue_merge(
            "run-1", "task-1", "attempt-1", conflicting, self.base,
            self.controller, authority=self.authority, reason="review-passed",
        )
        result = self.executor.run_merge_once("run-1", self.controller, self.authority)
        self.assertEqual(result["status"], "conflict")
        row = self.store.connection.execute(
            "SELECT status FROM merge_queue WHERE run_id='run-1'"
        ).fetchone()
        self.assertEqual(row["status"], "CONFLICT")
        self.assertEqual(self.store.task_state("task-1"), TaskState.REVIEW)
        issues = self.store.connection.execute(
            "SELECT kind FROM integration_issues WHERE task_id='task-1'"
        ).fetchall()
        self.assertEqual(issues[0]["kind"], "content_conflict")

    async def test_crash_after_claim_reconciles_without_duplicate_merge(self) -> None:
        _make_reviewed_task(self.store)
        commit = self._result_commit("demo/a.txt", "crash\n", "worker: crash")
        self.store.enqueue_merge(
            "run-1", "task-1", "attempt-1", commit, self.base,
            self.controller, authority=self.authority, reason="review-passed",
        )
        claim = self.store.claim_merge_queue(
            "run-1", self.controller, authority=self.authority
        )
        self.assertIsNotNone(claim)
        # 模拟崩溃：真实 Git 已 integrate 但 DB 停在 APPLYING
        self.manager.integrate(commit)
        recovered = self.executor.reconcile_once("run-1", self.controller, self.authority)
        self.assertIn(claim["merge_id"], recovered["marked_applied"])
        self.assertEqual(recovered["reapplied"], [])
        row = self.store.connection.execute(
            "SELECT status FROM merge_queue WHERE merge_id=?", (claim["merge_id"],)
        ).fetchone()
        self.assertEqual(row["status"], "APPLIED")
        # 再次 integrate：幂等，HEAD 不变 → 0 重复 merge
        head_before = self.manager.head(self.manager.repository)
        self.manager.integrate(commit)
        self.assertEqual(self.manager.head(self.manager.repository), head_before)

    async def test_run_merge_with_unknown_commit_fails_cleanly(self) -> None:
        _make_reviewed_task(self.store)
        self.store.enqueue_merge(
            "run-1", "task-1", "attempt-1", "deadbeef", self.base,
            self.controller, authority=self.authority, reason="review-passed",
        )
        result = self.executor.run_merge_once("run-1", self.controller, self.authority)
        self.assertEqual(result["status"], "failed")
        row = self.store.connection.execute(
            "SELECT status FROM merge_queue WHERE run_id='run-1'"
        ).fetchone()
        self.assertEqual(row["status"], "FAILED")
        # 未知 commit 不能把 task 置 COMPLETED
        self.assertEqual(self.store.task_state("task-1"), TaskState.REVIEW)


class OutboxDispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteStateStore(Path(self.temp.name) / "state.db")
        self.store.create_run("run-1", "team-1")
        self.authority = self.store.acquire_authority(
            "run-1", "test-supervisor", "supervisor"
        )
        self.controller = self.store.acquire_run_controller(
            "run-1", "op", lease_seconds=60
        )

    async def asyncTearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    async def test_dispatch_sent_and_failed_states(self) -> None:
        delivered: list[str] = []
        dispatcher = OutboxDispatcher(self.store, deliver=lambda intent: delivered.append(str(intent["outbox_id"])))
        self.store.record_outbox_intent(
            "run-1", "merge", "merge-1", "merge.applied", {"commit": "x"},
            self.controller,
        )
        result = dispatcher.run_once("run-1", self.controller, self.authority)
        self.assertEqual(result["status"], "sent")
        self.assertEqual(len(delivered), 1)
        row = self.store.connection.execute(
            "SELECT status FROM outbox WHERE outbox_id=?", (result["outbox_id"],)
        ).fetchone()
        self.assertEqual(row["status"], "SENT")

    async def test_dispatch_failure_marks_failed(self) -> None:
        def _boom(_: dict[str, object]) -> None:
            raise RuntimeError("delivery backend down")

        dispatcher = OutboxDispatcher(self.store, deliver=_boom)
        self.store.record_outbox_intent(
            "run-1", "merge", "merge-1", "merge.applied", {"commit": "x"},
            self.controller,
        )
        result = dispatcher.run_once("run-1", self.controller, self.authority)
        self.assertEqual(result["status"], "failed")
        row = self.store.connection.execute(
            "SELECT status, attempts FROM outbox WHERE run_id='run-1'"
        ).fetchone()
        # B3：失败不直接死信，而是保持 PENDING 并安排退避重试
        self.assertEqual(row["status"], "PENDING")
        self.assertEqual(row["attempts"], 1)


class EnqueueMergeValidationTests(unittest.IsolatedAsyncioTestCase):
    """P0-02：入队前原子验证 Task/Attempt/审核链。"""

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()

        self.store = SQLiteStateStore(Path(self.temp.name) / "state.db")
        self.store.create_run("run-1", "team-1")
        self.authority = self.store.acquire_authority(
            "run-1", "test-supervisor", "supervisor"
        )
        self.controller = self.store.acquire_run_controller(
            "run-1", "op", lease_seconds=60
        )

    async def asyncTearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _reviewed_task_with_attempt(self, task_id: str = "task-1") -> None:
        self.store.create_task("run-1", task_id)
        self.store.transition_task(task_id, TaskState.READY, reason="ready")
        self.store.transition_task(task_id, TaskState.ACTIVE, reason="assigned")
        self.store.transition_task(task_id, TaskState.REVIEW, reason="submitted")
        self.store.connection.execute(
            """
            INSERT INTO attempts(
                attempt_id, task_id, agent_id, state, attempt_number,
                generation, created_at, updated_at
            ) VALUES ('attempt-1', ?, 'ag-1', 'SUBMITTED', 1, 1,
                      '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')
            """,
            (task_id,),
        )
        self.store.connection.commit()

    def _enqueue(self, task_id: str, attempt_id: str) -> None:
        self.store.enqueue_merge(
            "run-1", task_id, attempt_id, "abc123", "base0",
            self.controller, authority=self.authority, reason="review-passed",
        )

    async def test_enqueue_rejects_task_not_in_review(self) -> None:
        self.store.create_task("run-1", "task-1")  # PENDING
        with self.assertRaises(ValueError):
            self._enqueue("task-1", "attempt-1")
        merges = self.store.connection.execute(
            "SELECT COUNT(*) FROM merge_queue WHERE run_id='run-1'"
        ).fetchone()[0]
        self.assertEqual(merges, 0)

    async def test_enqueue_rejects_missing_attempt(self) -> None:
        self.store.create_task("run-1", "task-1")
        self.store.transition_task("task-1", TaskState.READY, reason="ready")
        self.store.transition_task("task-1", TaskState.ACTIVE, reason="assigned")
        self.store.transition_task("task-1", TaskState.REVIEW, reason="submitted")
        with self.assertRaises(ValueError):
            self._enqueue("task-1", "attempt-nonexistent")
        merges = self.store.connection.execute(
            "SELECT COUNT(*) FROM merge_queue WHERE run_id='run-1'"
        ).fetchone()[0]
        self.assertEqual(merges, 0)

    async def test_enqueue_accepts_reviewed_task_with_attempt(self) -> None:
        self._reviewed_task_with_attempt()
        self._enqueue("task-1", "attempt-1")
        row = self.store.connection.execute(
            "SELECT status FROM merge_queue WHERE run_id='run-1'"
        ).fetchone()
        self.assertEqual(row["status"], "PENDING")


class FinishMergeAtomicTests(unittest.IsolatedAsyncioTestCase):
    """P0-02：finish_merge 严格 APPLYING + Git 证明 + 同事务 Outbox。"""

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteStateStore(Path(self.temp.name) / "state.db")
        self.store.create_run("run-1", "team-1")
        self.authority = self.store.acquire_authority(
            "run-1", "test-supervisor", "supervisor"
        )
        self.controller = self.store.acquire_run_controller(
            "run-1", "op", lease_seconds=60
        )
        self.store.create_task("run-1", "task-1")
        self.store.transition_task("task-1", TaskState.READY, reason="ready")
        self.store.transition_task("task-1", TaskState.ACTIVE, reason="assigned")
        self.store.transition_task("task-1", TaskState.REVIEW, reason="submitted")
        self.store.connection.execute(
            """
            INSERT INTO attempts(
                attempt_id, task_id, agent_id, state, attempt_number,
                generation, created_at, updated_at
            ) VALUES ('attempt-1', 'task-1', 'ag-1', 'SUBMITTED', 1, 1,
                      '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')
            """
        )
        self.store.connection.commit()

    async def asyncTearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _enqueue_and_claim(self) -> dict[str, object]:
        self.store.enqueue_merge(
            "run-1", "task-1", "attempt-1", "abc123", "base0",
            self.controller, authority=self.authority, reason="review-passed",
        )
        claim = self.store.claim_merge_queue(
            "run-1", self.controller, authority=self.authority
        )
        assert claim is not None
        return claim

    async def test_finish_applied_requires_integration_proof(self) -> None:
        claim = self._enqueue_and_claim()
        # 未提供 is_integrated 证明 → 拒绝，不能直接把 task 置 COMPLETED
        with self.assertRaises(ValueError):
            self.store.finish_merge(
                claim["merge_id"], "applied", self.controller,
                authority=self.authority, result_commit="abc123",
            )
        self.assertEqual(self.store.task_state("task-1"), TaskState.REVIEW)
        row = self.store.connection.execute(
            "SELECT status FROM merge_queue WHERE merge_id=?", (claim["merge_id"],)
        ).fetchone()
        self.assertEqual(row["status"], "APPLYING")

    async def test_finish_applied_with_failed_proof_is_rejected(self) -> None:
        claim = self._enqueue_and_claim()
        with self.assertRaises(ValueError):
            self.store.finish_merge(
                claim["merge_id"], "applied", self.controller,
                authority=self.authority, result_commit="abc123",
                is_integrated=lambda commit: False,
            )
        self.assertEqual(self.store.task_state("task-1"), TaskState.REVIEW)

    async def test_finish_applied_writes_outbox_in_same_transaction(self) -> None:
        claim = self._enqueue_and_claim()
        self.store.finish_merge(
            claim["merge_id"], "applied", self.controller,
            authority=self.authority, result_commit="abc123",
            is_integrated=lambda commit: True,
            outbox_payload={"commit": "abc123"},
        )
        self.assertEqual(self.store.task_state("task-1"), TaskState.COMPLETED)
        row = self.store.connection.execute(
            "SELECT status FROM merge_queue WHERE merge_id=?", (claim["merge_id"],)
        ).fetchone()
        self.assertEqual(row["status"], "APPLIED")
        outbox = self.store.connection.execute(
            "SELECT status FROM outbox WHERE run_id='run-1'"
        ).fetchall()
        self.assertEqual(len(outbox), 1)
        self.assertEqual(outbox[0]["status"], "PENDING")

    async def test_finish_repeated_call_after_applied_is_zero_side_effect(self) -> None:
        claim = self._enqueue_and_claim()
        self.store.finish_merge(
            claim["merge_id"], "applied", self.controller,
            authority=self.authority, result_commit="abc123",
            is_integrated=lambda commit: True,
            outbox_payload={"commit": "abc123"},
        )
        event_before = len(self.store.events())
        with self.assertRaises(RuntimeError):
            self.store.finish_merge(
                claim["merge_id"], "applied", self.controller,
                authority=self.authority, result_commit="abc123",
                is_integrated=lambda commit: True,
                outbox_payload={"commit": "abc123"},
            )
        self.assertEqual(len(self.store.events()), event_before)
        self.assertEqual(self.store.task_state("task-1"), TaskState.COMPLETED)
        outbox = self.store.connection.execute(
            "SELECT COUNT(*) FROM outbox WHERE run_id='run-1'"
        ).fetchone()[0]
        self.assertEqual(outbox, 1)  # 不重复写 outbox


if __name__ == "__main__":
    unittest.main()
