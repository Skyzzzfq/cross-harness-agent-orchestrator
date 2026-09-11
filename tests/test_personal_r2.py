from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator.agent_pool import reconcile_pool_once
from orchestrator.core.config import AgentPoolSpec, RoleSpec, TeamSpec
from orchestrator.core.models import TaskState
from orchestrator.storage.sqlite_store import SQLiteStateStore
from orchestrator.workspace.git_manager import GitWorkspaceManager, fingerprint_checkout
from orchestrator.workspace.run_manager import RunWorkspaceManager


class R2WorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.git = GitWorkspaceManager(self.root, self.root.parent / "legacy-worktrees")
        self.base = self.git.initialize_repository()
        self.workspace = RunWorkspaceManager(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_manifest_zones_and_attempt_worktrees_are_distinct(self) -> None:
        before = fingerprint_checkout(self.root)
        manifest = self.workspace.ensure_run("run-r2", base_commit=self.base)
        self.assertEqual(set(manifest.zones), {"scratch", "shared", "mounts", "resources", "worktrees", "integration"})
        first = self.workspace.allocate_attempt(
            "run-r2", "task-a", "attempt-a", access_mode="write", base_commit=self.base, agent_id="agent-a"
        )
        second = self.workspace.allocate_attempt(
            "run-r2", "task-b", "attempt-b", access_mode="write", base_commit=self.base, agent_id="agent-b"
        )
        self.assertNotEqual(first["workspace_path"], second["workspace_path"])
        self.assertTrue(Path(first["scratch_path"]).is_dir())
        self.assertTrue(Path(second["scratch_path"]).is_dir())
        self.assertEqual(before, fingerprint_checkout(self.root))
        self.assertTrue((Path(manifest.root) / "mounts" / "manifest.json").is_file())

    def test_actual_diff_outside_scope_is_rejected(self) -> None:
        worktree = self.git.create_worktree("strict", self.base)
        (worktree / "demo" / "a.txt").write_text("changed\n", encoding="utf-8")
        (worktree / "demo" / "unexpected.txt").write_text("bad\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "outside declared scope"):
            self.git.commit_managed_changes(worktree, ("demo/a.txt",), "strict")
        self.assertTrue(self.git.is_clean(worktree) is False)

    def test_artifact_manifest_records_immutable_entry(self) -> None:
        allocation = self.workspace.allocate_attempt(
            "run-r2", "task-a", "attempt-a", access_mode="write", base_commit=self.base
        )
        source = Path(allocation["workspace_path"]) / "demo" / "a.txt"
        source.write_text("candidate\n", encoding="utf-8")
        entry = self.workspace.publish_artifact(
            "run-r2", "task-a", "attempt-a", source,
            candidate_commit=self.base, producer="agent-a",
        )
        self.assertEqual(entry["version"], 1)
        self.assertTrue(entry["sha256"])
        index = self.root / ".agent-hub" / "runs" / "run-r2" / "shared" / "artifacts" / "manifest.json"
        self.assertEqual(len(__import__("json").loads(index.read_text(encoding="utf-8"))["artifacts"]), 1)


class R2StoreWiringTests(unittest.TestCase):
    def test_write_dispatch_uses_attempt_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            git = GitWorkspaceManager(root, root.parent / "worktrees")
            base = git.initialize_repository()
            workspace = RunWorkspaceManager(root)
            store = SQLiteStateStore(root / ".agent-hub" / "state.db", workspace_manager=workspace)
            team = TeamSpec(
                schema_version=1,
                team_id="r2-team",
                bootstrap_supervisor="supervisor",
                roles=(RoleSpec("supervisor", 1, "Supervisor", ("plan",)), RoleSpec("worker", 1, "Worker", ("write",))),
                agent_pools=(AgentPoolSpec("workers", "fake", "worker", 1, 1),),
            )
            store.create_run("run-r2", team.team_id, team_spec=team)
            reconcile_pool_once(store, "run-r2", team.agent_pools[0])
            store.create_task(
                "run-r2", "task-r2", access_mode="write", write_scope=("demo/a.txt",),
                required_role_id="worker", required_backend="fake", cwd=str(root),
            )
            store.transition_task("task-r2", TaskState.READY, reason="test")
            authority = store.acquire_authority("run-r2", "sup", "supervisor", lease_seconds=60)
            controller = store.acquire_run_controller("run-r2", "controller", lease_seconds=60)
            assert authority is not None and controller is not None
            request = store.claim_ready_dispatch("run-r2", controller=controller, authority=authority)
            assert request is not None
            self.assertIn(".agent-hub", request.policy.cwd)
            attempt = store.connection.execute(
                "SELECT workspace_path, scratch_path, base_commit FROM attempts WHERE task_id='task-r2'"
            ).fetchone()
            self.assertEqual(str(attempt["workspace_path"]), request.policy.cwd)
            self.assertTrue(Path(str(attempt["scratch_path"])).is_dir())
            self.assertEqual(str(attempt["base_commit"]), base)
            store.close()


if __name__ == "__main__":
    unittest.main()
