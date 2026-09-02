from __future__ import annotations

import asyncio
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


class EightAgentConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    """T8：8 Agent 并发验证——并发峰值达到池上限，无串行化。"""

    POOL_SIZE = 8
    TASK_COUNT = 16

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
                count=self.POOL_SIZE,
                max_count=self.POOL_SIZE,
                model="fake-v1",
            ),
        )
        self.executor = MergeExecutor(self.store, self.manager)

    async def asyncTearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    async def _tick(self, adapter: FakeBackendAdapter) -> None:
        from orchestrator.scheduler import scheduler_tick

        await scheduler_tick(
            self.store,
            run_id="run-1",
            adapters={"fake": adapter},
            authority=self.authority,
            controller=self.controller,
            lease_seconds=60,
        )

    def _busy_agents(self) -> int:
        return self.store.connection.execute(
            "SELECT COUNT(*) FROM agent_instances WHERE status='BUSY'"
        ).fetchone()[0]

    async def test_concurrency_peaks_at_pool_size(self) -> None:
        for i in range(self.TASK_COUNT):
            self.store.create_task(
                "run-1", f"task-{i:02d}", cwd=str(self.worker),
                prompt=f"read {i}", timeout_seconds=5,
            )
            self.store.transition_task(f"task-{i:02d}", TaskState.READY, reason="ready")

        adapter = FakeBackendAdapter(
            default_behavior=FakeBehavior(delay_seconds=0.5, text="done")
        )
        # scheduler_tick 会等待启动的 call 完成，因此并发采样要在 tick 运行期间进行
        peak_busy = 0
        for _ in range(6):
            tick_task = asyncio.create_task(self._tick(adapter))
            while not tick_task.done():
                peak_busy = max(peak_busy, self._busy_agents())
                await asyncio.sleep(0.02)
            await tick_task
        # 并发峰值应达到池上限（8）——证明并行而非串行
        self.assertEqual(peak_busy, self.POOL_SIZE, "并发峰值应达到 8 个 agent")
        # 所有任务最终离开 PENDING/READY
        for i in range(self.TASK_COUNT):
            state = self.store.task_state(f"task-{i:02d}")
            self.assertIn(state, {TaskState.REVIEW, TaskState.COMPLETED, TaskState.FAILED})

    async def test_eight_write_tasks_parallel_to_completed(self) -> None:
        task_ids: list[str] = []
        for i in range(self.POOL_SIZE):
            task_id = f"write-{i}"
            self.store.create_task(
                "run-1", task_id, access_mode="write",
                write_scope=(f"demo/w{i}.txt",), prompt=f"write {i}",
                cwd=str(self.worker), timeout_seconds=5,
            )
            self.store.transition_task(task_id, TaskState.READY, reason="ready")
            task_ids.append(task_id)

        adapter = FakeBackendAdapter(
            default_behavior=FakeBehavior(delay_seconds=0.1, text="done")
        )
        for _ in range(20):
            await self._tick(adapter)
            await asyncio.sleep(0.1)

        # 全部到 REVIEW 后，产出 + 集成
        for task_id in task_ids:
            self.assertEqual(self.store.task_state(task_id), TaskState.REVIEW, task_id)
            idx = task_id.split("-")[1]
            commit = self.manager.commit_file(
                self.worker, f"demo/w{idx}.txt", f"result-{idx}\n", f"worker: {idx}"
            )
            attempt = self.store.connection.execute(
                "SELECT attempt_id FROM attempts WHERE task_id=? LIMIT 1",
                (task_id,),
            ).fetchone()
            assert attempt is not None
            self.store.enqueue_merge(
                "run-1", task_id, str(attempt["attempt_id"]), commit, self.base,
                self.controller, authority=self.authority, reason="review-passed",
            )
            result = self.executor.run_merge_once(
                "run-1", self.controller, self.authority
            )
            self.assertEqual(result["status"], "applied", task_id)

        for task_id in task_ids:
            self.assertEqual(self.store.task_state(task_id), TaskState.COMPLETED)
        applied = self.store.connection.execute(
            "SELECT COUNT(*) FROM merge_queue WHERE status='APPLIED'"
        ).fetchone()[0]
        self.assertEqual(applied, self.POOL_SIZE)


if __name__ == "__main__":
    unittest.main()
