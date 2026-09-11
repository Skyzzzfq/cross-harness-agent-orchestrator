from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.agent_pool import reconcile_pool_once
from orchestrator.core.config import AgentPoolSpec, RoleSpec, TeamSpec
from orchestrator.core.models import TaskState
from orchestrator.storage.sqlite_store import FencedAttemptError, SQLiteStateStore
from orchestrator.workspace.git_manager import GitWorkspaceManager
from orchestrator.workspace.run_manager import RunWorkspaceManager


class R3HandoffTests(unittest.TestCase):
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
            team_id="r3-team",
            bootstrap_supervisor="supervisor",
            roles=(
                RoleSpec("supervisor", 1, "Supervisor", ("plan",)),
                RoleSpec("worker", 1, "Worker", ("read",)),
            ),
            agent_pools=(AgentPoolSpec("workers", "fake", "worker", 1, 1),),
        )
        self.store.create_run("run-r3", self.team.team_id, team_spec=self.team)
        reconcile_pool_once(self.store, "run-r3", self.team.agent_pools[0])

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _tokens(self):
        authority = self.store.acquire_authority(
            "run-r3", "agent-supervisor", "supervisor", lease_seconds=60
        )
        controller = self.store.acquire_run_controller(
            "run-r3", "controller", lease_seconds=60
        )
        assert authority is not None and controller is not None
        return controller, authority

    def test_claim_persists_hub_owned_handoff(self) -> None:
        self.store.create_task(
            "run-r3",
            "task-handoff",
            required_role_id="worker",
            required_backend="fake",
            prompt="inspect the project",
            cwd=str(self.root),
            acceptance_criteria=("return a structured summary",),
            input_refs=("input-v1",),
            task_kind="research",
            output_contract={"type": "summary"},
            budget={"calls": 1},
            dispatch_source="supervisor_plan",
        )
        self.store.transition_task("task-handoff", TaskState.READY, reason="test")
        controller, authority = self._tokens()
        request = self.store.claim_ready_dispatch(
            "run-r3", controller=controller, authority=authority
        )
        self.assertIsNotNone(request)
        assert request is not None
        handoff = self.store.get_handoff("task-handoff", request.attempt_id)
        self.assertEqual(handoff["package"]["run_id"], "run-r3")
        self.assertEqual(handoff["package"]["task_id"], "task-handoff")
        self.assertEqual(handoff["package"]["agent_id"], request.agent_id)
        self.assertEqual(handoff["package"]["write_scope"], [])
        self.assertIn("handoff package", request.prompt)
        attempt = self.store.connection.execute(
            "SELECT handoff_digest FROM attempts WHERE attempt_id = ?",
            (request.attempt_id,),
        ).fetchone()
        self.assertEqual(attempt["handoff_digest"], handoff["package_digest"])

    def test_forged_result_rejected_and_verified_dependency_released(self) -> None:
        self.store.create_task_graph(
            "run-r3",
            [
                {"task_id": "upstream", "required_role_id": "worker", "required_backend": "fake", "cwd": str(self.root)},
                {"task_id": "downstream", "required_role_id": "worker", "required_backend": "fake", "cwd": str(self.root)},
            ],
            [("downstream", "upstream")],
        )
        if self.store.task_state("upstream") != TaskState.READY:
            self.store.transition_task("upstream", TaskState.READY, reason="test")
        controller, authority = self._tokens()
        request = self.store.claim_ready_dispatch(
            "run-r3", controller=controller, authority=authority
        )
        assert request is not None
        with self.assertRaisesRegex(ValueError, "task_id"):
            self.store.record_worker_result(
                "run-r3", "upstream", request.attempt_id,
                {"task_id": "downstream", "status": "completed"},
            )
        submitted = self.store.record_worker_result(
            "run-r3", "upstream", request.attempt_id,
            {"status": "completed", "summary": "verified input"},
        )
        verified = self.store.verify_worker_result(
            submitted["result_id"], verified_by="r3-test"
        )
        self.assertEqual(verified["status"], "verified")
        self.store.reconcile_task_graph(
            "run-r3", controller=controller, authority=authority
        )
        self.assertEqual(self.store.task_state("downstream"), TaskState.READY)

    def test_parent_requires_all_children_and_summary_references(self) -> None:
        self.store.create_task(
            "run-r3", "parent", required_role_id="supervisor", cwd=str(self.root)
        )
        for task_id in ("child-1", "child-2"):
            self.store.create_task(
                "run-r3", task_id, required_role_id="worker", cwd=str(self.root),
                parent_task_id="parent", dispatch_source="supervisor_plan",
            )
        self.store.transition_task("parent", TaskState.READY, reason="test")
        parent_attempt = "attempt-parent"
        self.store.create_attempt_with_lease("parent", parent_attempt, "agent-parent")
        self.store.transition_task("parent", TaskState.REVIEW, reason="test")
        child_results = []
        for task_id in ("child-1", "child-2"):
            if self.store.task_state(task_id) != TaskState.READY:
                self.store.transition_task(task_id, TaskState.READY, reason="test")
            attempt_id = f"attempt-{task_id}"
            self.store.create_attempt_with_lease(task_id, attempt_id, f"agent-{task_id}")
            submitted = self.store.record_worker_result(
                "run-r3", task_id, attempt_id,
                {"status": "completed", "summary": task_id},
            )
            child_results.append(
                self.store.verify_worker_result(submitted["result_id"], verified_by="r3-test")["result_id"]
            )
        summary = self.store.record_worker_result(
            "run-r3", "parent", parent_attempt,
            {"status": "completed", "child_result_refs": child_results, "summary": "1 and 2"},
        )
        summary = self.store.verify_worker_result(summary["result_id"], verified_by="r3-test")
        aggregate = self.store.aggregate_parent_task(
            "run-r3", "parent", summary_result_id=summary["result_id"]
        )
        self.assertTrue(aggregate["completed"])
        self.assertEqual(self.store.task_state("parent"), TaskState.COMPLETED)


if __name__ == "__main__":
    unittest.main()
