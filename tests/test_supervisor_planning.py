from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from orchestrator.adapters.fake import FakeBackendAdapter, FakeBehavior
from orchestrator.agent_pool import reconcile_pool_once
from orchestrator.core.config import AgentPoolSpec, RoleSpec, TeamSpec
from orchestrator.core.models import TaskState
from orchestrator.core.supervisor_plan import (
    PlanValidationError,
    validate_supervisor_plan,
)
from orchestrator.core.supervisor_planning import materialize_ready_supervisor_plans
from orchestrator.scheduler import scheduler_tick
from orchestrator.serve import serve
from orchestrator.storage.sqlite_store import (
    FencedControllerError,
    SQLiteStateStore,
)


def _valid_plan() -> dict[str, object]:
    return {
        "plan_version": 1,
        "summary": "Split the work into two independent changes.",
        "tasks": [
            {
                "task_id": "worker-a",
                "role_id": "worker",
                "backend": "fake",
                "prompt": "Implement the first change.",
                "access_mode": "write",
                "write_scope": ["demo/a.txt"],
                "depends_on": [],
            },
            {
                "task_id": "worker-b",
                "role_id": "worker",
                "backend": "fake",
                "prompt": "Implement the second change.",
                "access_mode": "write",
                "write_scope": ["demo/b.txt"],
                "depends_on": [],
            },
        ],
    }


class SupervisorPlanValidationTests(unittest.TestCase):
    def _validate(self, payload: object) -> object:
        return validate_supervisor_plan(
            payload,
            allowed_role_ids={"worker", "supervisor"},
            allowed_backends={"fake", "codex"},
            max_tasks=2,
            existing_task_ids=set(),
        )

    def test_valid_plan_is_normalized_and_hashed(self) -> None:
        plan = self._validate(_valid_plan())
        self.assertEqual(plan.plan_version, 1)
        self.assertEqual([task.task_id for task in plan.tasks], ["worker-a", "worker-b"])
        self.assertTrue(plan.digest)

    def test_invalid_json_shape_is_rejected(self) -> None:
        with self.assertRaises(PlanValidationError):
            self._validate("not-json-object")

    def test_unknown_fields_are_rejected(self) -> None:
        payload = _valid_plan()
        payload["unexpected"] = True
        with self.assertRaisesRegex(PlanValidationError, "unknown plan fields"):
            self._validate(payload)

    def test_unknown_role_or_backend_is_rejected(self) -> None:
        payload = _valid_plan()
        payload["tasks"][0]["role_id"] = "admin"  # type: ignore[index]
        with self.assertRaises(PlanValidationError):
            self._validate(payload)

        payload = _valid_plan()
        payload["tasks"][0]["backend"] = "unknown"  # type: ignore[index]
        with self.assertRaises(PlanValidationError):
            self._validate(payload)

    def test_task_count_and_existing_ids_are_rejected(self) -> None:
        payload = _valid_plan()
        payload["tasks"].append({  # type: ignore[union-attr]
            "task_id": "worker-c",
            "role_id": "worker",
            "backend": "fake",
            "prompt": "third",
            "access_mode": "write",
            "write_scope": ["demo/c.txt"],
            "depends_on": [],
        })
        with self.assertRaisesRegex(PlanValidationError, "maximum"):
            self._validate(payload)

        payload = _valid_plan()
        with self.assertRaisesRegex(PlanValidationError, "already exists"):
            validate_supervisor_plan(
                payload,
                allowed_role_ids={"worker"},
                allowed_backends={"fake"},
                max_tasks=2,
                existing_task_ids={"worker-a"},
            )

    def test_scope_escape_and_conflict_are_rejected(self) -> None:
        payload = _valid_plan()
        payload["tasks"][0]["write_scope"] = ["../outside.txt"]  # type: ignore[index]
        with self.assertRaises(PlanValidationError):
            self._validate(payload)

        payload = _valid_plan()
        payload["tasks"][1]["write_scope"] = ["demo"]  # type: ignore[index]
        with self.assertRaisesRegex(PlanValidationError, "conflict"):
            self._validate(payload)

    def test_dependency_must_be_known_and_acyclic(self) -> None:
        payload = _valid_plan()
        payload["tasks"][1]["depends_on"] = ["missing"]  # type: ignore[index]
        with self.assertRaises(PlanValidationError):
            self._validate(payload)

        payload = _valid_plan()
        payload["tasks"][0]["depends_on"] = ["worker-b"]  # type: ignore[index]
        payload["tasks"][1]["depends_on"] = ["worker-a"]  # type: ignore[index]
        with self.assertRaisesRegex(PlanValidationError, "cycle"):
            self._validate(payload)

    def test_sensitive_prompt_is_rejected_before_persistence(self) -> None:
        payload = _valid_plan()
        payload["tasks"][0]["prompt"] = "Use api_key=sk-test-secret-value"  # type: ignore[index]
        with self.assertRaisesRegex(PlanValidationError, "sensitive"):
            self._validate(payload)

    def test_read_only_child_cannot_declare_write_scope(self) -> None:
        payload = _valid_plan()
        payload["tasks"][0]["access_mode"] = "read_only"  # type: ignore[index]
        with self.assertRaises(PlanValidationError):
            self._validate(payload)


class SupervisorPlanMaterializationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = SQLiteStateStore(self.root / "state.db")
        self.store.create_run("run-1", "team-1")
        self.team = TeamSpec(
            schema_version=1,
            team_id="team-1",
            bootstrap_supervisor="supervisor",
            roles=(
                RoleSpec("supervisor", 1, "Supervisor", ("plan",)),
                RoleSpec("worker", 1, "Worker", ("write",)),
            ),
            agent_pools=(
                AgentPoolSpec("supervisors", "fake", "supervisor", 1, 1),
                AgentPoolSpec("workers", "fake", "worker", 2, 2),
            ),
        )
        for pool in self.team.agent_pools:
            reconcile_pool_once(self.store, "run-1", pool)
        self.authority = self.store.acquire_authority(
            "run-1", "supervisor-agent", "supervisor", lease_seconds=60
        )
        self.controller = self.store.acquire_run_controller(
            "run-1", "test-controller", lease_seconds=60
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    async def _run_supervisor(self, plan_text: str | None = None, structured=None):
        self.store.create_task(
            "run-1",
            "supervisor-task",
            required_role_id="supervisor",
            prompt="Plan the work as strict JSON.",
            cwd=str(self.root),
        )
        self.store.transition_task("supervisor-task", TaskState.READY, reason="test")
        behavior = FakeBehavior(
            delay_seconds=0.01,
            text=plan_text or "",
            structured=structured or {},
        )
        adapter = FakeBackendAdapter(default_behavior=behavior)
        await scheduler_tick(
            self.store,
            run_id="run-1",
            adapters={"fake": adapter},
            authority=self.authority,
            controller=self.controller,
        )
        return adapter

    async def test_valid_plan_materializes_two_worker_tasks(self) -> None:
        await self._run_supervisor(structured=_valid_plan())
        self.assertEqual(self.store.task_state("supervisor-task"), TaskState.REVIEW)
        result = materialize_ready_supervisor_plans(
            self.store,
            run_id="run-1",
            team_spec=self.team,
            controller=self.controller,
            authority=self.authority,
            worker_cwd=str(self.root),
        )
        self.assertEqual(result[0]["status"], "materialized")
        for task_id in ("worker-a", "worker-b"):
            self.assertEqual(self.store.task_state(task_id), TaskState.READY)
        events = [
            event for event in self.store.events()
            if event["kind"] == "plan.materialized"
        ]
        self.assertEqual(len(events), 1)

    async def test_plan_materialization_is_idempotent(self) -> None:
        await self._run_supervisor(structured=_valid_plan())
        first = materialize_ready_supervisor_plans(
            self.store,
            run_id="run-1",
            team_spec=self.team,
            controller=self.controller,
            authority=self.authority,
            worker_cwd=str(self.root),
        )
        second = materialize_ready_supervisor_plans(
            self.store,
            run_id="run-1",
            team_spec=self.team,
            controller=self.controller,
            authority=self.authority,
            worker_cwd=str(self.root),
        )
        self.assertEqual(first[0]["status"], "materialized")
        self.assertEqual(second, [])
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE run_id='run-1'"
            ).fetchone()[0],
            3,
        )

    async def test_invalid_plan_creates_no_children(self) -> None:
        await self._run_supervisor(plan_text="not-json")
        result = materialize_ready_supervisor_plans(
            self.store,
            run_id="run-1",
            team_spec=self.team,
            controller=self.controller,
            authority=self.authority,
            worker_cwd=str(self.root),
        )
        self.assertEqual(result[0]["status"], "rejected")
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE run_id='run-1'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            len([event for event in self.store.events() if event["kind"] == "plan.rejected"]),
            1,
        )

    async def test_two_materialized_workers_run_in_parallel(self) -> None:
        await self._run_supervisor(structured=_valid_plan())
        materialize_ready_supervisor_plans(
            self.store,
            run_id="run-1",
            team_spec=self.team,
            controller=self.controller,
            authority=self.authority,
            worker_cwd=str(self.root),
        )
        adapter = FakeBackendAdapter(
            default_behavior=FakeBehavior(delay_seconds=0.05, text="done")
        )
        await scheduler_tick(
            self.store,
            run_id="run-1",
            adapters={"fake": adapter},
            authority=self.authority,
            controller=self.controller,
        )
        self.assertEqual(self.store.task_state("worker-a"), TaskState.REVIEW)
        self.assertEqual(self.store.task_state("worker-b"), TaskState.REVIEW)
        started = self.store.connection.execute(
            """
            SELECT COUNT(*) FROM backend_calls
            WHERE task_id IN ('worker-a', 'worker-b') AND state='succeeded'
            """
        ).fetchone()[0]
        self.assertEqual(started, 2)

    async def test_supervisor_cancel_does_not_spawn_children(self) -> None:
        self.store.create_task(
            "run-1",
            "supervisor-task",
            required_role_id="supervisor",
            prompt="Plan the work as strict JSON.",
            cwd=str(self.root),
        )
        self.store.transition_task("supervisor-task", TaskState.READY, reason="test")
        self.store.request_cancel_task(
            "supervisor-task", self.controller, reason="user-cancel"
        )
        result = materialize_ready_supervisor_plans(
            self.store,
            run_id="run-1",
            team_spec=self.team,
            controller=self.controller,
            authority=self.authority,
            worker_cwd=str(self.root),
        )
        self.assertEqual(result, [])
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE run_id='run-1'"
            ).fetchone()[0],
            1,
        )

    async def test_stale_epoch_plan_has_zero_side_effects(self) -> None:
        await self._run_supervisor(structured=_valid_plan())
        self.store.handoff_run_controller(self.controller, "new-controller")
        with self.assertRaises(FencedControllerError):
            materialize_ready_supervisor_plans(
                self.store,
                run_id="run-1",
                team_spec=self.team,
                controller=self.controller,
                authority=self.authority,
                worker_cwd=str(self.root),
            )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE run_id='run-1'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            len([event for event in self.store.events() if event["kind"].startswith("plan.")]),
            0,
        )

    async def test_serve_auto_materializes_a_supervisor_result(self) -> None:
        self.store.create_task(
            "run-1",
            "supervisor-task",
            required_role_id="supervisor",
            prompt="Plan the work as strict JSON.",
            cwd=str(self.root),
        )
        self.store.transition_task("supervisor-task", TaskState.READY, reason="test")
        adapter = FakeBackendAdapter(
            default_behavior=FakeBehavior(structured=_valid_plan(), delay_seconds=0.01)
        )
        result = await serve(
            self.store,
            "run-1",
            {"fake": adapter},
            team_spec=self.team,
            authority=self.authority,
            controller=self.controller,
            interval=0.001,
            max_ticks=1,
        )
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(self.store.task_state("worker-a"), TaskState.READY)
        self.assertEqual(self.store.task_state("worker-b"), TaskState.READY)


if __name__ == "__main__":
    unittest.main()
