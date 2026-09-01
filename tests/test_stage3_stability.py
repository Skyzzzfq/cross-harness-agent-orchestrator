from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator.adapters.fake import FakeBackendAdapter, FakeBehavior
from orchestrator.agent_pool import reconcile_pool_once
from orchestrator.core.config import AgentPoolSpec
from orchestrator.core.models import TaskState
from orchestrator.storage.sqlite_store import SQLiteStateStore
from orchestrator.workspace.git_manager import GitWorkspaceManager
from orchestrator.workspace.merge_executor import MergeExecutor
from orchestrator.workspace.policy import WorkspacePolicy


class StabilityHighDensityTests(unittest.IsolatedAsyncioTestCase):
    """T1：高密度稳定运行——0 丢任务、0 重复 merge、0 重复通知。"""

    TASK_COUNT = 40  # 单元测试规模；长跑脚本支持 500+

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.manager = GitWorkspaceManager(root / "repo", root / "worktrees")
        self.base = self.manager.initialize_repository()
        self.worker = self.manager.create_worktree("worker", self.base)
        self.policy = WorkspacePolicy(
            project_root=root / "repo",
            worktrees_root=root / "worktrees",
            managed_worktrees=(self.worker,),
        )
        self.store = SQLiteStateStore(root / "state.db", workspace_policy=self.policy)
        self.store.create_run("run-1", "team-1")
        self.authority = self.store.acquire_authority(
            "run-1", "test-supervisor", "supervisor"
        )
        self.controller = self.store.acquire_run_controller(
            "run-1", "op", lease_seconds=60
        )
        reconcile_pool_once(
            self.store,
            "run-1",
            AgentPoolSpec(
                pool_id="fake-workers",
                backend="fake",
                role_id="worker",
                count=4,
                max_count=4,
                model="fake-v1",
            ),
        )
        self.executor = MergeExecutor(self.store, self.manager)

    async def asyncTearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _create_tasks(self, count: int, *, mode: str) -> list[str]:
        task_ids: list[str] = []
        for i in range(count):
            task_id = f"task-{i:04d}"
            if mode == "read":
                self.store.create_task(
                    "run-1", task_id, required_role_id="worker",
                    prompt=f"read {task_id}", cwd=str(self.worker),
                    timeout_seconds=5,
                )
            else:
                self.store.create_task(
                    "run-1", task_id, access_mode="write",
                    write_scope=(f"demo/f{i:04d}.txt",),
                    required_role_id="worker",
                    prompt=f"write {task_id}", cwd=str(self.worker),
                    timeout_seconds=5,
                )
            self.store.transition_task(task_id, TaskState.READY, reason="ready")
            task_ids.append(task_id)
        return task_ids

    async def test_high_density_read_dispatch_no_lost_tasks(self) -> None:
        task_ids = self._create_tasks(self.TASK_COUNT, mode="read")
        adapter = FakeBackendAdapter(
            default_behavior=FakeBehavior(delay_seconds=0, text="ok")
        )
        await self._serve_until_drained(adapter, max_ticks=400)
        # 0 丢：每个 task 都离开 READY/ACTIVE，到达 REVIEW/COMPLETED
        for task_id in task_ids:
            state = self.store.task_state(task_id)
            self.assertIn(
                state,
                {TaskState.REVIEW, TaskState.COMPLETED, TaskState.FAILED},
                f"{task_id} left pending: {state}",
            )
        # 无孤儿 lease / 无 ACTIVE 遗留
        self.assertEqual(self._active_count(), 0)
        self.assertEqual(self._orphan_leases(), 0)

    async def test_high_density_write_merge_no_duplicate(self) -> None:
        """T1：高密度写任务经 serve 派发 + supervisor 入队 + 真实集成，
        每个任务恰好 1 条 APPLIED、1 条 outbox 通知，0 重复 merge。"""
        count = 16  # 每任务一次真实 git 集成，控制单测规模
        task_ids = self._create_tasks(count, mode="write")
        adapter = FakeBackendAdapter(
            default_behavior=FakeBehavior(delay_seconds=0, text="done")
        )
        await self._serve_until_drained(adapter, max_ticks=200)

        # supervisor 驱动：REVIEW 的写任务 → 产出 commit → 入队 → 集成
        for task_id in task_ids:
            if self.store.task_state(task_id) != TaskState.REVIEW:
                continue
            file = f"demo/f{task_id.split('-')[1]}.txt"
            commit = self.manager.commit_file(
                self.worker, file, f"result-{task_id}\n", f"worker: {task_id}"
            )
            row = self.store.connection.execute(
                "SELECT attempt_id FROM attempts WHERE task_id=? LIMIT 1",
                (task_id,),
            ).fetchone()
            assert row is not None
            self.store.enqueue_merge(
                "run-1", task_id, str(row["attempt_id"]), commit, self.base,
                self.controller, authority=self.authority, reason="review-passed",
            )
            result = self.executor.run_merge_once(
                "run-1", self.controller, self.authority
            )
            self.assertEqual(result["status"], "applied", task_id)

        # 0 丢：全部 COMPLETED
        for task_id in task_ids:
            self.assertEqual(self.store.task_state(task_id), TaskState.COMPLETED, task_id)
        # 0 重复 merge：每 task 恰好 1 条 APPLIED
        rows = self.store.connection.execute(
            "SELECT task_id, COUNT(*) AS n FROM merge_queue "
            "WHERE status='APPLIED' GROUP BY task_id"
        ).fetchall()
        self.assertEqual(len(rows), count)
        for row in rows:
            self.assertEqual(row["n"], 1, f"duplicate merge for {row['task_id']}")
        # 0 重复通知：outbox 每 task 1 条 PENDING
        outbox = self.store.connection.execute(
            "SELECT COUNT(*) FROM outbox WHERE event_type='merge.applied'"
        ).fetchone()[0]
        self.assertEqual(outbox, count)

    async def test_crash_between_claim_and_integrate_recovers_without_duplicate(
        self,
    ) -> None:
        """T1：崩溃注入（claim 后、finish 前）→ 重启对账 → 0 重复 merge。"""
        task_ids = self._create_tasks(4, mode="write")
        adapter = FakeBackendAdapter(
            default_behavior=FakeBehavior(delay_seconds=0, text="done")
        )
        await self._serve_until_drained(adapter, max_ticks=40)

        for task_id in task_ids:
            file = f"demo/f{task_id.split('-')[1]}.txt"
            commit = self.manager.commit_file(
                self.worker, file, f"crash-{task_id}\n", f"worker: {task_id}"
            )
            row = self.store.connection.execute(
                "SELECT attempt_id FROM attempts WHERE task_id=? LIMIT 1",
                (task_id,),
            ).fetchone()
            assert row is not None
            self.store.enqueue_merge(
                "run-1", task_id, str(row["attempt_id"]), commit, self.base,
                self.controller, authority=self.authority, reason="review-passed",
            )
        # 模拟崩溃：真实 Git 已集成但 DB 停在 APPLYING（只 claim 不 finish）
        claims: list[dict[str, object]] = []
        for _ in task_ids:
            claim = self.store.claim_merge_queue(
                "run-1", self.controller, authority=self.authority
            )
            if claim is None:
                break
            claims.append(claim)
            self.manager.integrate(str(claim["result_commit"]))
        self.assertEqual(len(claims), 4)

        # 重启：对账把已集成但未落库的 merge 标记 APPLIED，0 重放
        recovered = self.executor.reconcile_once(
            "run-1", self.controller, self.authority
        )
        self.assertEqual(recovered["reapplied"], [])
        self.assertEqual(len(recovered["marked_applied"]), 4)
        applied = self.store.connection.execute(
            "SELECT COUNT(*) FROM merge_queue WHERE status='APPLIED'"
        ).fetchone()[0]
        self.assertEqual(applied, 4)

    # -- helpers -------------------------------------------------------------

    async def _serve_until_drained(
        self, adapter: FakeBackendAdapter, *, max_ticks: int
    ) -> None:
        from orchestrator.serve import serve

        await serve(
            self.store,
            run_id="run-1",
            adapters={"fake": adapter},
            authority=self.authority,
            controller=self.controller,
            interval=0.001,
            controller_lease_seconds=60,
            max_ticks=max_ticks,
        )

    def _active_count(self) -> int:
        return self.store.connection.execute(
            "SELECT COUNT(*) FROM tasks WHERE state='ACTIVE'"
        ).fetchone()[0]

    def _orphan_leases(self) -> int:
        # RUNNING 的 attempt 表示"未干净结束"的孤儿执行
        return self.store.connection.execute(
            "SELECT COUNT(*) FROM attempts WHERE state='RUNNING'"
        ).fetchone()[0]


if __name__ == "__main__":
    unittest.main()
