from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from orchestrator.storage.sqlite_store import SQLiteStateStore
from orchestrator.workspace.git_manager import GitWorkspaceManager


class GitMergeIdempotencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.manager = GitWorkspaceManager(root / "repo", root / "worktrees")
        self.base = self.manager.initialize_repository()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_integrate_same_commit_twice_is_idempotent(self) -> None:
        worktree = self.manager.create_worktree("worker", self.base)
        commit = self.manager.commit_file(
            worktree, "demo/a.txt", "idempotent-result\n", "worker: result"
        )
        first = self.manager.integrate(commit)
        self.assertTrue(first.applied)
        head_after_first = self.manager.head(self.manager.repository)
        # 第二次 integrate 同一 commit：不重复 cherry-pick，HEAD 不变
        second = self.manager.integrate(commit)
        self.assertTrue(second.applied)
        self.assertEqual(self.manager.head(self.manager.repository), head_after_first)

    def test_result_commit_in_integration_is_detected(self) -> None:
        worktree = self.manager.create_worktree("worker", self.base)
        commit = self.manager.commit_file(
            worktree, "demo/a.txt", "detected-result\n", "worker: result"
        )
        self.assertFalse(self.manager.result_commit_in_integration(commit))
        self.manager.integrate(commit)
        self.assertTrue(self.manager.result_commit_in_integration(commit))


class GitSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.manager = GitWorkspaceManager(root / "repo", root / "worktrees")
        self.base = self.manager.initialize_repository()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_dirty_checkout_is_rejected_for_write(self) -> None:
        worktree = self.manager.create_worktree("worker", self.base)
        # 制造脏状态：未提交改动
        (worktree / "demo" / "a.txt").write_text("dirty-change\n", encoding="utf-8")
        self.assertFalse(self.manager.is_clean(worktree))
        with self.assertRaises(RuntimeError):
            self.manager.assert_clean_for_write(worktree)

    def test_safe_write_text_never_leaves_partial_file(self) -> None:
        worktree = self.manager.create_worktree("worker", self.base)
        target = worktree / "demo" / "safe.txt"
        self.manager.safe_write_text(target, "complete-content\n")
        self.assertEqual(target.read_text(encoding="utf-8"), "complete-content\n")
        # 磁盘不足/文件占用等失败：模拟写入失败不留下半写入
        bad = Path(self.temp.name) / "nonexistent-dir" / "x.txt"
        with self.assertRaises(OSError):
            self.manager.safe_write_text(bad, "data")
        self.assertFalse(bad.exists())

    def test_worktree_creation_failure_does_not_leak_partial(self) -> None:
        # 非法 worktree 名被拒绝，不会创建任何目录
        with self.assertRaises(ValueError):
            self.manager.create_worktree("bad/name", self.base)
        leftovers = list(self.manager.worktrees_root.glob("*"))
        self.assertEqual(leftovers, [])


class GitMergeReconcileTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_already_applied_merge_is_marked_applied_not_reapplied(self) -> None:
        token = self._controller()
        self.store.create_task("run-1", "task-1")
        self.store.enqueue_merge(
            "run-1", "task-1", "attempt-1", "abc123", "base0", token,
            authority=self.authority, reason="review-passed",
        )
        claim = self.store.claim_merge_queue("run-1", token, authority=self.authority)
        # 重启后：result commit 已在集成分支（is_applied=True）→ 标记 APPLIED，不重复 merge
        result = self.store.reconcile_merge_with_git(
            "run-1", token, is_applied=lambda commit: True, authority=self.authority
        )
        self.assertEqual(result["reapplied"], [])
        self.assertEqual(result["marked_applied"], [claim["merge_id"]])
        row = self.store.connection.execute(
            "SELECT status FROM merge_queue WHERE merge_id=?", (claim["merge_id"],)
        ).fetchone()
        self.assertEqual(row["status"], "APPLIED")

    async def test_unapplied_merge_is_requeued_for_retry(self) -> None:
        token = self._controller()
        self.store.create_task("run-1", "task-1")
        self.store.enqueue_merge(
            "run-1", "task-1", "attempt-1", "abc123", "base0", token,
            authority=self.authority, reason="review-passed",
        )
        claim = self.store.claim_merge_queue("run-1", token, authority=self.authority)
        result = self.store.reconcile_merge_with_git(
            "run-1", token, is_applied=lambda commit: False, authority=self.authority
        )
        self.assertEqual(result["requeued"], [claim["merge_id"]])
        row = self.store.connection.execute(
            "SELECT status FROM merge_queue WHERE merge_id=?", (claim["merge_id"],)
        ).fetchone()
        self.assertEqual(row["status"], "PENDING")


class KillRestartMergeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.manager = GitWorkspaceManager(root / "repo", root / "worktrees")
        self.base = self.manager.initialize_repository()
        self.store = SQLiteStateStore(root / "state.db")
        self.store.create_run("run-1", "team-1")
        self.store.create_task("run-1", "task-1")
        self.authority = self.store.acquire_authority(
            "run-1", "test-supervisor", "supervisor"
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_kill_restart_marks_applied_without_duplicate_merge(self) -> None:
        # Worker 提交 result commit 并已集成到集成分支（模拟崩溃前已完成但状态未落库）
        worktree = self.manager.create_worktree("worker", self.base)
        result_commit = self.manager.commit_file(
            worktree, "demo/a.txt", "crash-recovery-result\n", "worker: result"
        )
        self.manager.integrate(result_commit)
        head_before = self.manager.head(self.manager.repository)

        # 崩溃前 merge_queue 处于 APPLYING
        token = self.store.acquire_run_controller("run-1", "ghost", lease_seconds=60)
        self.store.enqueue_merge(
            "run-1", "task-1", "attempt-1", result_commit, self.base,
            token, authority=self.authority, reason="review-passed",
        )
        claim = self.store.claim_merge_queue(
            "run-1", token, authority=self.authority
        )
        self.store.release_run_controller(token)

        # 重启后对账：result commit 已在集成分支 → 标记 APPLIED，不重复 merge
        new_token = self.store.acquire_run_controller("run-1", "restarted", lease_seconds=60)
        result = self.store.reconcile_merge_with_git(
            "run-1", new_token,
            authority=self.authority,
            is_applied=lambda commit: self.manager.result_commit_in_integration(commit),
        )
        self.assertIn(claim["merge_id"], result["marked_applied"])
        self.assertEqual(result["reapplied"], [])
        row = self.store.connection.execute(
            "SELECT status FROM merge_queue WHERE merge_id=?", (claim["merge_id"],)
        ).fetchone()
        self.assertEqual(row["status"], "APPLIED")

        # 再次 integrate 同一 commit：幂等，集成分支 HEAD 不变 → 0 重复 merge
        self.manager.integrate(result_commit)
        self.assertEqual(self.manager.head(self.manager.repository), head_before)


if __name__ == "__main__":
    unittest.main()
