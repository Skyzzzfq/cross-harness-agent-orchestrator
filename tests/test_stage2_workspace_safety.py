from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator.core.models import TaskState
from orchestrator.workspace.policy import WorkspacePolicy
from orchestrator.storage.sqlite_store import SQLiteStateStore


class WorkspacePolicyValidationTests(unittest.TestCase):
    """P0-03：cwd/write_scope 的 canonical 边界（策略层单元）。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project_root = self.root / "project"
        self.worktrees_root = self.root / "worktrees"
        self.worktrees_root.mkdir(parents=True)
        self.managed = self.worktrees_root / "worker-1"
        self.managed.mkdir(parents=True)
        self.policy = WorkspacePolicy(
            project_root=self.project_root,
            worktrees_root=self.worktrees_root,
            managed_worktrees=(self.managed,),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_canonical_normalizes_case_and_resolves_dots(self) -> None:
        # Windows 大小写不敏感 + .. 消解
        raw = str(self.managed / "sub" / ".." / "demo" / "a.txt")
        canonical = self.policy.canonical(raw)
        # 与策略自身的大小写归一规则一致（normcase）
        from orchestrator.workspace.policy import _canonical as _c

        self.assertEqual(canonical, _c(self.managed / "demo" / "a.txt"))

    def test_write_scope_must_be_non_empty(self) -> None:
        with self.assertRaises(ValueError):
            self.policy.validate_write_scope(())

    def test_write_scope_rejects_dotdot_and_absolute_and_git(self) -> None:
        for bad in (
            ("a/../b",),
            ("/abs/path",),
            ("C:/abs",),
            (".git/config",),
            ("a/.git",),
        ):
            with self.assertRaises(ValueError):
                self.policy.validate_write_scope(bad)

    def test_write_scope_rejects_path_escape_via_symlink_target(self) -> None:
        # scope 指向受管 worktree 外的真实路径（symlink 或软链逃逸）
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("x", encoding="utf-8")
        link = self.managed / "leak"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink not available")
        with self.assertRaises(ValueError):
            self.policy.validate_write_scope(("leak/secret.txt",), base=self.managed)

    def test_scopes_conflict_detects_directory_containment(self) -> None:
        self.assertTrue(self.policy.scopes_conflict(("demo/",), ("demo/a.txt",)))
        self.assertTrue(self.policy.scopes_conflict(("demo",), ("demo/sub/b.txt",)))
        self.assertFalse(self.policy.scopes_conflict(("demo/a.txt",), ("demo/b.txt",)))
        # 大小写不敏感冲突（Windows）
        self.assertTrue(self.policy.scopes_conflict(("DEMO/A.TXT",), ("demo/a.txt",)))


class TaskCreationBoundaryTests(unittest.TestCase):
    """P0-03：Task 创建边界校验（store 集成）。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteStateStore(Path(self.temp.name) / "state.db")
        self.store.create_run("run-1", "team-1")

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_write_task_with_empty_scope_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.create_task(
                "run-1", "task-1", access_mode="write", write_scope=()
            )

    def test_write_task_with_dotdot_scope_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.create_task(
                "run-1",
                "task-1",
                access_mode="write",
                write_scope=("a/../b",),
            )

    def test_write_task_cwd_outside_managed_worktree_is_rejected(self) -> None:
        store = self._strict_store()
        store.create_run("run-2", "team-1")
        with self.assertRaises(ValueError):
            store.create_task(
                "run-2",
                "task-1",
                access_mode="write",
                write_scope=("demo/a.txt",),
                cwd="C:/Windows/System32",  # 项目外任意路径
            )

    def test_read_task_cwd_outside_project_root_is_rejected(self) -> None:
        store = self._strict_store()
        store.create_run("run-2", "team-1")
        with self.assertRaises(ValueError):
            store.create_task(
                "run-2", "task-1", access_mode="read_only", cwd="C:/Windows/System32"
            )

    def _strict_store(self) -> SQLiteStateStore:
        self.store.close()
        root = Path(self.temp.name)
        managed = root / "worktrees" / "worker-1"
        managed.mkdir(parents=True, exist_ok=True)
        policy = WorkspacePolicy(
            project_root=root,
            worktrees_root=root / "worktrees",
            managed_worktrees=(managed,),
        )
        self.store = SQLiteStateStore(root / "state.db", workspace_policy=policy)
        return self.store


class CertificateRootTests(unittest.TestCase):
    """P0-03：证书缓存根固定，不随任务 cwd 变化。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_certs_bundle_path_is_fixed_not_cwd_dependent(self) -> None:
        from orchestrator.platform import _certs_bundle_path

        fixed_root = self.root / "fixed-certs"
        for cwd in (self.root / "a", self.root / "b" / "c", self.root / "worker"):
            bundle = _certs_bundle_path(fixed_root)
            self.assertEqual(bundle, fixed_root / ".agent-hub" / "certs" / "windows-roots.pem")
            self.assertNotIn(str(cwd), str(bundle))


if __name__ == "__main__":
    unittest.main()
