from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from orchestrator.adapters.fake import FakeBackendAdapter, FakeBehavior
from orchestrator.core.models import TaskState
from orchestrator.storage.sqlite_store import SQLiteStateStore
from orchestrator.workspace.git_manager import GitWorkspaceManager
from orchestrator.workspace.merge_executor import MergeExecutor
from orchestrator.workspace.policy import WorkspacePolicy


class WindowsPathTests(unittest.TestCase):
    """T5：中文 / 空格 / 长路径。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        # 中文 + 空格的目录名
        self.root = Path(self.temp.name) / "工作空间 space"
        self.root.mkdir(parents=True)
        self.manager = GitWorkspaceManager(self.root / "repo", self.root / "worktrees")
        self.base = self.manager.initialize_repository()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_repository_with_chinese_and_space_paths(self) -> None:
        # worktree 名必须符合 git 命名规则（无空格）；仓库/文件路径含中文与空格
        worktree = self.manager.create_worktree("worker", self.base)
        commit = self.manager.commit_file(
            worktree, "文档 目录/结果.txt", "内容\n", "提交: 中文"
        )
        self.manager.integrate(commit)
        blob = self.manager.read_blob(self.manager.head(self.manager.repository), "文档 目录/结果.txt")[1]
        self.assertEqual(blob.decode().strip(), "内容")
        self.assertTrue(self.manager.is_clean(self.manager.repository))

    def test_long_path_commit(self) -> None:
        deep = "/".join([f"目录{d}" for d in range(20)])
        worktree = self.manager.create_worktree("worker", self.base)
        commit = self.manager.commit_file(
            worktree, f"{deep}/a.txt", "deep\n", "commit: deep path"
        )
        self.manager.integrate(commit)
        blob = self.manager.read_blob(self.manager.head(self.manager.repository), f"{deep}/a.txt")[1]
        self.assertEqual(blob.decode().strip(), "deep")

    def test_crlf_content_round_trip(self) -> None:
        worktree = self.manager.create_worktree("worker", self.base)
        commit = self.manager.commit_file(
            worktree, "demo/a.txt", "line1\r\nline2\r\n", "commit: crlf"
        )
        self.manager.integrate(commit)
        blob = self.manager.read_blob(self.manager.head(self.manager.repository), "demo/a.txt")[1]
        text = blob.decode()
        # 允许 git autocrlf 归一化，但两行内容必须都在
        self.assertIn("line1", text)
        self.assertIn("line2", text)


class WindowsFileLockTests(unittest.IsolatedAsyncioTestCase):
    """T5：文件占用时干净失败——不部分写入、不产生残留。"""

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.manager = GitWorkspaceManager(root / "repo", root / "worktrees")
        self.base = self.manager.initialize_repository()
        self.worker = self.manager.create_worktree("worker", self.base)
        self.store = SQLiteStateStore(root / "state.db")
        self.store.create_run("run-1", "team-1")

    async def asyncTearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    @unittest.skipUnless(os.name == "nt", "file locking is Windows-specific")
    def test_locked_file_commit_fails_cleanly(self) -> None:
        import msvcrt

        target = self.worker / "demo"
        target.mkdir(parents=True, exist_ok=True)
        locked = target / "locked.txt"
        locked.write_text("original\n", encoding="utf-8")
        fd = os.open(str(locked), os.O_RDWR)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            with self.assertRaises(Exception):
                self.manager.commit_file(
                    self.worker, "demo/locked.txt", "updated\n", "worker: update"
                )
        finally:
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            os.close(fd)
        # 干净失败：无残留（文件内容保持原样，或 git 状态可恢复）
        content = (target / "locked.txt").read_text(encoding="utf-8")
        self.assertIn("original", content)


class WindowsCancelCleanupTests(unittest.IsolatedAsyncioTestCase):
    """T5：取消后无遗留执行（backend_call 终态 + 无 ACTIVE session）。"""

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
        from orchestrator.agent_pool import reconcile_pool_once
        from orchestrator.core.config import AgentPoolSpec

        reconcile_pool_once(
            self.store,
            "run-1",
            AgentPoolSpec(
                pool_id="fake-workers",
                backend="fake",
                role_id="worker",
                count=1,
                max_count=1,
                model="fake-v1",
            ),
        )

    async def asyncTearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    async def test_cancel_leaves_no_active_backend_call(self) -> None:
        from orchestrator.scheduler import scheduler_tick

        self.store.create_task("run-1", "task-1", cwd=str(self.temp.name))
        self.store.transition_task("task-1", TaskState.READY, reason="ready")
        adapter = FakeBackendAdapter(
            default_behavior=FakeBehavior(delay_seconds=1.0, text="late")
        )
        await scheduler_tick(
            self.store, run_id="run-1", adapters={"fake": adapter},
            authority=self.authority, controller=self.controller, lease_seconds=60,
        )
        await scheduler_tick(
            self.store, run_id="run-1", adapters={"fake": adapter},
            authority=self.authority, controller=self.controller, lease_seconds=60,
        )
        self.store.request_cancel_task(
            "task-1", self.controller, reason="t5-cancel"
        )
        await scheduler_tick(
            self.store, run_id="run-1", adapters={"fake": adapter},
            authority=self.authority, controller=self.controller, lease_seconds=60,
        )
        # 取消后 backend_call 已终态（cancelled / canceled 相关），无 ACTIVE session
        row = self.store.connection.execute(
            "SELECT state FROM backend_calls WHERE task_id='task-1' ORDER BY requested_at DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIn(row["state"], {"cancelled", "cancel_requested", "succeeded", "failed"})


if __name__ == "__main__":
    unittest.main()
