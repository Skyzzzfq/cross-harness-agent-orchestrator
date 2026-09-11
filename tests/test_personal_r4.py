from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator.agent_pool import reconcile_pool_once
from orchestrator.core.config import AgentPoolSpec, RoleSpec, TeamSpec
from orchestrator.core.models import TaskState
from orchestrator.storage.sqlite_store import SQLiteStateStore
from orchestrator.verification import VerificationService
from orchestrator.workspace.git_manager import GitWorkspaceManager
from orchestrator.workspace.run_manager import RunWorkspaceManager


class R4VerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.git = GitWorkspaceManager(self.root, self.root.parent / "worktrees")
        self.base = self.git.initialize_repository()
        self.workspace = RunWorkspaceManager(self.root)
        self.store = SQLiteStateStore(
            self.root / ".agent-hub" / "state.db", workspace_manager=self.workspace
        )
        self.team = TeamSpec(
            schema_version=1,
            team_id="r4-team",
            bootstrap_supervisor="supervisor",
            roles=(
                RoleSpec("supervisor", 1, "Supervisor", ("review",)),
                RoleSpec("worker", 1, "Worker", ("write",)),
            ),
            agent_pools=(AgentPoolSpec("workers", "fake", "worker", 1, 1),),
        )
        self.store.create_run("run-r4", self.team.team_id, team_spec=self.team)
        reconcile_pool_once(self.store, "run-r4", self.team.agent_pools[0])

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _tokens(self):
        authority = self.store.acquire_authority(
            "run-r4", "agent-supervisor", "supervisor", lease_seconds=60
        )
        controller = self.store.acquire_run_controller(
            "run-r4", "controller", lease_seconds=60
        )
        assert authority is not None and controller is not None
        return controller, authority

    def _candidate(self):
        self.store.create_task(
            "run-r4", "task-write", access_mode="write",
            write_scope=("demo/a.txt",), required_role_id="worker",
            required_backend="fake", cwd=str(self.root), prompt="write a candidate",
        )
        self.store.transition_task("task-write", TaskState.READY, reason="test")
        controller, authority = self._tokens()
        request = self.store.claim_ready_dispatch(
            "run-r4", controller=controller, authority=authority
        )
        assert request is not None
        worktree = Path(request.policy.cwd)
        source = worktree / "demo" / "a.txt"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("candidate", encoding="utf-8")
        manager = self.workspace._git_manager_for("run-r4", worktree.parent)
        manager.adopt_worktree(worktree)
        candidate = manager.commit_managed_changes(
            worktree, ("demo/a.txt",), "r4 candidate"
        )
        submitted = self.store.record_worker_result(
            "run-r4", "task-write", request.attempt_id,
            {"status": "completed", "candidate_commit": candidate, "summary": "done"},
        )
        self.store.verify_worker_result(submitted["result_id"], verified_by="r4-test")
        self.store.transition_task("task-write", TaskState.REVIEW, reason="candidate-ready")
        return controller, authority, request, candidate

    def test_fixed_checks_and_evidence_bound_review_allow_merge(self) -> None:
        controller, authority, request, candidate = self._candidate()
        evidence = VerificationService(self.store).verify_candidate(
            "run-r4", "task-write", request.attempt_id, candidate
        )
        self.assertTrue(evidence)
        self.assertTrue(all(item.status == "PASS" for item in evidence))
        detail = {
            "candidate_commit": candidate,
            "evidence_refs": [item.evidence_id for item in evidence],
        }
        self.store.record_review_decision(
            "run-r4", "task-write", attempt_id=request.attempt_id,
            layer="human", decision="APPROVED", decided_by="r4-test",
            detail=detail, authority=authority,
        )
        merge_id = self.store.enqueue_merge(
            "run-r4", "task-write", request.attempt_id, candidate,
            str(self.base), controller, authority=authority, reason="verified",
        )
        self.assertTrue(merge_id.startswith("merge-"))

    def test_old_evidence_cannot_approve_another_candidate(self) -> None:
        controller, authority, request, candidate = self._candidate()
        evidence = VerificationService(self.store).verify_candidate(
            "run-r4", "task-write", request.attempt_id, candidate,
            checks=("commit_exists",),
        )
        with self.assertRaisesRegex(ValueError, "missing or mismatched"):
            self.store.record_review_decision(
                "run-r4", "task-write", attempt_id=request.attempt_id,
                layer="human", decision="APPROVED", decided_by="r4-test",
                detail={
                    "candidate_commit": "0" * 40,
                    "evidence_refs": [evidence[0].evidence_id],
                }, authority=authority,
            )
        with self.assertRaisesRegex(ValueError, "before PASS verification"):
            self.store.enqueue_merge(
                "run-r4", "task-write", request.attempt_id, "0" * 40,
                str(self.base), controller, authority=authority, reason="forged",
            )

    def test_rework_is_bounded_to_two_rounds(self) -> None:
        store = SQLiteStateStore(Path(self.temp.name) / "legacy.db")
        try:
            store.create_run("legacy", "team-legacy")
            store.create_task("legacy", "task-rework", cwd=str(self.root))
            store.transition_task("task-rework", TaskState.READY, reason="test")
            controller = store.acquire_run_controller("legacy", "controller", lease_seconds=60)
            authority = store.acquire_authority("legacy", "supervisor", "supervisor", lease_seconds=60)
            assert controller is not None
            store.transition_task("task-rework", TaskState.ACTIVE, reason="test")
            store.transition_task("task-rework", TaskState.REVIEW, reason="test")
            for _ in range(2):
                store.reassign_task("legacy", "task-rework", controller, authority)
                store.transition_task("task-rework", TaskState.ACTIVE, reason="retry")
                store.transition_task("task-rework", TaskState.REVIEW, reason="retry-result")
            with self.assertRaisesRegex(ValueError, "rework limit"):
                store.reassign_task("legacy", "task-rework", controller, authority)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
