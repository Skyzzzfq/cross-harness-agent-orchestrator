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


class WritableTaskClosedLoopTests(unittest.IsolatedAsyncioTestCase):
    """P1-04：写任务只进受管 worktree，两路并行→审核→集成→COMPLETED，
    用户 checkout（主仓库工作区）保持干净、不被污染。"""

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
        self.store = SQLiteStateStore(
            root / "state.db", workspace_policy=self.policy
        )
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
                count=2,
                max_count=2,
                model="fake-v1",
            ),
        )
        self.executor = MergeExecutor(self.store, self.manager)

    async def asyncTearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _write_task(self, task_id: str, path: str) -> None:
        self.store.create_task(
            "run-1",
            task_id,
            access_mode="write",
            write_scope=(path,),
            required_role_id="worker",
            prompt=f"write {path}",
            cwd=str(self.worker),
            timeout_seconds=5,
        )
        self.store.transition_task(task_id, TaskState.READY, reason="ready")

    def _enqueue(self, task_id: str, commit: str) -> None:
        row = self.store.connection.execute(
            "SELECT attempt_id FROM attempts WHERE task_id=? LIMIT 1",
            (task_id,),
        ).fetchone()
        assert row is not None
        self.store.enqueue_merge(
            "run-1", task_id, str(row["attempt_id"]), commit, self.base,
            self.controller, authority=self.authority, reason="review-passed",
        )

    async def test_two_write_tasks_parallel_then_integrate_and_fingerprint_clean(
        self,
    ) -> None:
        self._write_task("task-a", "demo/a.txt")
        self._write_task("task-b", "demo/b.txt")
        adapter = FakeBackendAdapter(
            default_behavior=FakeBehavior(delay_seconds=0.02, text="done")
        )
        await _tick(self.store, self.authority, adapter, self.controller)
        # 两个不重叠写任务并行完成到 REVIEW
        self.assertEqual(self.store.task_state("task-a"), TaskState.REVIEW)
        self.assertEqual(self.store.task_state("task-b"), TaskState.REVIEW)

        # 用户 checkout（主仓库）在 worker 写之前是干净的
        self.assertTrue(self.manager.is_clean(self.manager.repository))

        # 模拟 worker 在受管 worktree 产出（各自独立文件）
        commit_a = self.manager.commit_file(
            self.worker, "demo/a.txt", "result-a\n", "worker: a"
        )
        commit_b = self.manager.commit_file(
            self.worker, "demo/b.txt", "result-b\n", "worker: b"
        )
        # 集成期间用户 checkout 仍干净（写只发生在 worktree）
        self.assertTrue(self.manager.is_clean(self.manager.repository))

        # 逐个入队并真实集成
        self._enqueue("task-a", commit_a)
        first = self.executor.run_merge_once("run-1", self.controller, self.authority)
        self.assertEqual(first["status"], "applied")
        self.assertEqual(self.store.task_state("task-a"), TaskState.COMPLETED)

        self._enqueue("task-b", commit_b)
        second = self.executor.run_merge_once("run-1", self.controller, self.authority)
        self.assertEqual(second["status"], "applied")
        self.assertEqual(self.store.task_state("task-b"), TaskState.COMPLETED)

        # 集成后主仓库包含两个产出且工作区干净（0 残留、0 污染）
        self.assertTrue(self.manager.is_clean(self.manager.repository))
        blob_a = self.manager.read_blob(
            self.manager.head(self.manager.repository), "demo/a.txt"
        )[1]
        self.assertEqual(blob_a.decode().strip(), "result-a")

    async def test_write_task_with_cwd_outside_managed_worktree_is_rejected(
        self,
    ) -> None:
        with self.assertRaises(ValueError):
            self.store.create_task(
                "run-1",
                "bad-write",
                access_mode="write",
                write_scope=("demo/a.txt",),
                cwd=str(self.temp.name),  # 项目外路径，不是受管 worktree
            )

    async def test_write_task_scope_escape_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.create_task(
                "run-1",
                "escape",
                access_mode="write",
                write_scope=("../outside.txt",),
                cwd=str(self.worker),
            )

    async def test_full_pipeline_review_audit_integrate_to_completed(self) -> None:
        """P1-01：正确结果必须经过三层审核 + 集成才到 COMPLETED（REVIEW 不是终态）。"""
        self._write_task("task-1", "demo/a.txt")
        adapter = FakeBackendAdapter(
            default_behavior=FakeBehavior(delay_seconds=0.01, text="done")
        )
        await _tick(self.store, self.authority, adapter, self.controller)
        self.assertEqual(self.store.task_state("task-1"), TaskState.REVIEW)

        # 三层审核：deterministic + model + human
        row = self.store.connection.execute(
            "SELECT attempt_id FROM attempts WHERE task_id='task-1' LIMIT 1"
        ).fetchone()
        attempt_id = str(row["attempt_id"])
        for layer, decision, by in (
            ("deterministic", "PASS", "verify-script"),
            ("model", "PASS", "codex-reviewer"),
            ("human", "APPROVED", "human-admin"),
        ):
            self.store.record_review_decision(
                "run-1", "task-1", attempt_id=attempt_id, layer=layer,
                decision=decision, decided_by=by, detail={},
                authority=self.authority,
            )

        # worker 产出 + 入队 + 真实集成 → COMPLETED
        commit = self.manager.commit_file(
            self.worker, "demo/a.txt", "pipeline-result\n", "worker: full pipeline"
        )
        self._enqueue("task-1", commit)
        result = self.executor.run_merge_once("run-1", self.controller, self.authority)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(self.store.task_state("task-1"), TaskState.COMPLETED)

        # 审核链落库 + 集成后主仓库干净
        reviews = self.store.connection.execute(
            "SELECT layer FROM review_decisions WHERE task_id='task-1'"
        ).fetchall()
        self.assertEqual(
            {r["layer"] for r in reviews}, {"deterministic", "model", "human"}
        )
        self.assertTrue(self.manager.is_clean(self.manager.repository))


async def _tick(
    store: SQLiteStateStore,
    authority: object,
    adapter: FakeBackendAdapter,
    controller: object | None = None,
) -> None:
    from orchestrator.scheduler import scheduler_tick

    await scheduler_tick(
        store,
        run_id="run-1",
        adapters={"fake": adapter},
        authority=authority,
        controller=controller,
    )


if __name__ == "__main__":
    unittest.main()
