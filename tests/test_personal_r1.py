from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator.agent_pool import reconcile_pool_once
from orchestrator.adapters.fake import FakeBackendAdapter, FakeBehavior
from orchestrator.core.config import AgentPoolSpec, RoleSpec, TeamSpec
from orchestrator.core.role_registry import build_role_prompt, build_supervisor_plan_prompt
from orchestrator.core.models import TaskState
from orchestrator.core.supervisor_plan import validate_supervisor_plan
from orchestrator.core.supervisor_planning import materialize_ready_supervisor_plans
from orchestrator.scheduler import scheduler_tick
from orchestrator.storage.sqlite_store import SQLiteStateStore


def _team() -> TeamSpec:
    return TeamSpec(
        schema_version=1,
        team_id="r1-team",
        bootstrap_supervisor="supervisor",
        roles=(
            RoleSpec("supervisor", 1, "Supervisor", ("plan",), goal="plan"),
            RoleSpec("worker", 1, "Worker", ("write",), goal="execute"),
        ),
        agent_pools=(
            AgentPoolSpec("supervisors", "fake", "supervisor", 1, 1),
            AgentPoolSpec("workers", "fake", "worker", 2, 2),
        ),
    )


def _v2_plan(count: int = 6) -> dict[str, object]:
    return {
        "plan_version": 2,
        "revision": 1,
        "summary": "bounded sequential work",
        "worker_concurrency": 2,
        "task_cap": count,
        "permissions": {"access_mode": "write"},
        "budget": {"max_calls": 8},
        "tasks": [
            {
                "task_id": f"worker-{index}",
                "role_id": "worker",
                "backend": "fake",
                "prompt": f"do item {index}",
                "access_mode": "write",
                "write_scope": [f"demo/{index}.txt"],
                "depends_on": [],
                "acceptance_criteria": ["result is present"],
                "input_refs": [],
                "task_kind": "implementation",
                "output_contract": {"status": "completed"},
                "budget": {"max_attempts": 1},
            }
            for index in range(count)
        ],
    }


class R1ContractTests(unittest.TestCase):
    def test_legacy_role_gets_template_and_prompt(self) -> None:
        role = _team().roles[0]
        self.assertTrue(role.responsibilities == ())  # explicit custom role remains explicit
        prompt = build_role_prompt(role, task_package={"goal": "x"})
        self.assertIn("ROLE: Supervisor", prompt)
        self.assertIn("APPROVED TASK PACKAGE", prompt)
        self.assertIn("LEGAL PLAN SHAPE", build_supervisor_plan_prompt(_team(), user_goal="x"))

    def test_two_slots_allow_six_sequential_tasks(self) -> None:
        plan = validate_supervisor_plan(
            _v2_plan(),
            allowed_role_ids={"worker"},
            allowed_backends={"fake"},
            allowed_child_role_ids={"worker"},
            role_backend_pairs={("worker", "fake")},
            max_tasks=6,
            max_worker_concurrency=2,
        )
        self.assertEqual(len(plan.tasks), 6)
        self.assertEqual(plan.worker_concurrency, 2)


class R1ApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_plan_is_previewed_then_approved_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = SQLiteStateStore(root / "state.db")
            team = _team()
            store.create_run(
                "run-r1",
                team.team_id,
                team_spec=team,
                approval_mode="manual",
            )
            for pool in team.agent_pools:
                reconcile_pool_once(store, "run-r1", pool)
            authority = store.acquire_authority(
                "run-r1", "sup", "supervisor", lease_seconds=60
            )
            controller = store.acquire_run_controller(
                "run-r1", "controller", lease_seconds=60
            )
            assert authority is not None and controller is not None
            store.create_task(
                "run-r1",
                "supervisor-task",
                required_role_id="supervisor",
                required_backend="fake",
                prompt="plan",
                cwd=str(root),
            )
            store.transition_task("supervisor-task", TaskState.READY, reason="test")
            adapter = FakeBackendAdapter(
                default_behavior=FakeBehavior(structured=_v2_plan(2), delay_seconds=0.001)
            )
            await scheduler_tick(
                store,
                run_id="run-r1",
                adapters={"fake": adapter},
                authority=authority,
                controller=controller,
            )
            preview = materialize_ready_supervisor_plans(
                store,
                run_id="run-r1",
                team_spec=team,
                controller=controller,
                authority=authority,
                worker_cwd=str(root),
            )
            self.assertEqual(preview[0]["status"], "pending_approval")
            self.assertEqual(
                store.connection.execute(
                    "SELECT COUNT(*) FROM tasks WHERE run_id='run-r1'"
                ).fetchone()[0],
                1,
            )
            plan = store.latest_supervisor_plan("run-r1", "supervisor-task")
            assert plan is not None
            approved = store.approve_supervisor_plan(
                "run-r1",
                "supervisor-task",
                int(plan["revision"]),
                str(plan["plan_digest"]),
                controller=controller,
                authority=authority,
            )
            again = store.approve_supervisor_plan(
                "run-r1",
                "supervisor-task",
                int(plan["revision"]),
                str(plan["plan_digest"]),
                controller=controller,
                authority=authority,
            )
            self.assertEqual(approved["status"], "approved")
            self.assertEqual(again["status"], "approved")
            materialized = materialize_ready_supervisor_plans(
                store,
                run_id="run-r1",
                team_spec=team,
                controller=controller,
                authority=authority,
                worker_cwd=str(root),
            )
            self.assertEqual(materialized[0]["status"], "materialized")
            self.assertEqual(
                store.connection.execute(
                    "SELECT COUNT(*) FROM tasks WHERE run_id='run-r1'"
                ).fetchone()[0],
                3,
            )
            store.close()


if __name__ == "__main__":
    unittest.main()
