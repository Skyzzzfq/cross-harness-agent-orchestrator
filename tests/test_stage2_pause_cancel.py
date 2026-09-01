from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from orchestrator.adapters.contracts import CallState
from orchestrator.adapters.fake import FakeBackendAdapter, FakeBehavior
from orchestrator.agent_pool import reconcile_pool_once
from orchestrator.core.config import AgentPoolSpec, TeamSpec
from orchestrator.core.models import AttemptState, TaskState
from orchestrator.scheduler import scheduler_tick
from orchestrator.storage.sqlite_store import (
    FencedControllerError,
    SQLiteStateStore,
)


class PauseResumeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteStateStore(Path(self.temp.name) / "state.db")
        self.store.create_run("run-1", "team-1")
        self.authority = self.store.acquire_authority(
            "run-1", "test-supervisor", "supervisor"
        )
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

    def _task(self, task_id: str) -> None:
        self.store.create_task(
            "run-1",
            task_id,
            required_role_id="worker",
            prompt=f"execute {task_id}",
            cwd="D:/workspace/connect",
            timeout_seconds=5,
        )
        self.store.transition_task(task_id, TaskState.READY, reason="ready")

    def _controller(self) -> object:
        return self.store.acquire_run_controller(
            "run-1", "operator", lease_seconds=60
        )

    async def test_pause_run_stops_dispatch_and_resume_restores(self) -> None:
        self._task("task-1")
        token = self._controller()
        self.store.pause_run("run-1", token, reason="manual-pause")
        self.assertIsNone(
            self.store.claim_ready_dispatch(
                "run-1", controller=token, authority=self.authority, lease_seconds=60
            )
        )
        self.store.resume_run("run-1", token, reason="manual-resume")
        claim = self.store.claim_ready_dispatch(
            "run-1", controller=token, authority=self.authority, lease_seconds=60
        )
        self.assertIsNotNone(claim)

    async def test_pause_task_skips_only_that_task(self) -> None:
        self._task("task-1")
        self._task("task-2")
        token = self._controller()
        self.store.pause_task("task-1", token, reason="hold-this-one")
        claim = self.store.claim_ready_dispatch(
            "run-1", controller=token, authority=self.authority, lease_seconds=60
        )
        self.assertIsNotNone(claim)
        self.assertEqual(claim.task_id, "task-2")

    async def test_pause_and_resume_are_idempotent(self) -> None:
        self._task("task-1")
        token = self._controller()
        self.store.pause_run("run-1", token, reason="pause")
        self.store.pause_run("run-1", token, reason="pause-again")
        self.store.resume_run("run-1", token, reason="resume")
        self.store.resume_run("run-1", token, reason="resume-again")
        claim = self.store.claim_ready_dispatch(
            "run-1", controller=token, authority=self.authority, lease_seconds=60
        )
        self.assertIsNotNone(claim)


class CancelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteStateStore(Path(self.temp.name) / "state.db")
        self.store.create_run("run-1", "team-1")
        self.authority = self.store.acquire_authority(
            "run-1", "test-supervisor", "supervisor"
        )
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

    def _task(self, task_id: str, *, ready: bool = True) -> None:
        self.store.create_task(
            "run-1",
            task_id,
            required_role_id="worker",
            prompt=f"execute {task_id}",
            cwd="D:/workspace/connect",
            timeout_seconds=5,
        )
        if ready:
            self.store.transition_task(task_id, TaskState.READY, reason="ready")

    def _controller(self, owner: str = "operator") -> object:
        return self.store.acquire_run_controller("run-1", owner, lease_seconds=60)

    async def test_cancel_ready_task_cancels_directly(self) -> None:
        self._task("task-1")
        token = self._controller()
        disposition = self.store.request_cancel_task(
            "task-1", controller=token, reason="operator-cancel"
        )
        self.assertEqual(disposition, "cancelled")
        self.assertEqual(self.store.task_state("task-1"), TaskState.CANCELLED)

    async def test_cancel_pending_task_cancels_directly(self) -> None:
        self._task("task-1", ready=False)
        token = self._controller()
        disposition = self.store.request_cancel_task(
            "task-1", controller=token, reason="operator-cancel"
        )
        self.assertEqual(disposition, "cancelled")
        self.assertEqual(self.store.task_state("task-1"), TaskState.CANCELLED)

    async def test_cancel_cascades_to_downstream_tasks(self) -> None:
        self.store.create_task_graph(
            "run-1",
            task_specs=[
                {
                    "task_id": "upstream",
                    "required_role_id": "worker",
                    "prompt": "upstream",
                    "cwd": "D:/workspace/connect",
                    "timeout_seconds": 5,
                },
                {
                    "task_id": "downstream",
                    "required_role_id": "worker",
                    "prompt": "downstream",
                    "cwd": "D:/workspace/connect",
                    "timeout_seconds": 5,
                },
            ],
            dependencies=[("downstream", "upstream")],
        )
        token = self._controller()
        self.store.request_cancel_task(
            "upstream", controller=token, reason="operator-cancel"
        )
        self.assertEqual(self.store.task_state("upstream"), TaskState.CANCELLED)
        self.assertEqual(self.store.task_state("downstream"), TaskState.CANCELLED)

    async def test_cancel_active_task_interrupts_running_call(self) -> None:
        self._task("task-1")
        adapter = FakeBackendAdapter(
            default_behavior=FakeBehavior(delay_seconds=0.5, text="done")
        )
        token = self._controller("scheduler-test")
        tick_task = asyncio.create_task(
            scheduler_tick(
                self.store,
                run_id="run-1",
                adapters={"fake": adapter},
                authority=self.authority,
                controller=token,
            )
        )
        await asyncio.sleep(0.06)
        self.store.request_cancel_task(
            "task-1", controller=token, reason="operator-cancel"
        )
        await tick_task
        self.assertGreaterEqual(adapter.cancel_count, 1)
        self.assertEqual(self.store.task_state("task-1"), TaskState.CANCELLED)
        attempts = self.store.connection.execute(
            "SELECT state FROM attempts WHERE task_id = 'task-1'"
        ).fetchall()
        self.assertEqual(
            [row["state"] for row in attempts],
            [AttemptState.CANCELLED.value],
        )

    async def test_unconfirmed_cancel_allows_natural_finish_but_cancels_task(self) -> None:
        self._task("task-1")
        adapter = FakeBackendAdapter(
            default_behavior=FakeBehavior(
                delay_seconds=0.2,
                text="late-work",
                cancel_mode="unconfirmed",
            )
        )
        token = self._controller("scheduler-test")
        tick_task = asyncio.create_task(
            scheduler_tick(
                self.store,
                run_id="run-1",
                adapters={"fake": adapter},
                authority=self.authority,
                controller=token,
            )
        )
        await asyncio.sleep(0.05)
        self.store.request_cancel_task(
            "task-1", controller=token, reason="operator-cancel"
        )
        await tick_task
        self.assertEqual(self.store.task_state("task-1"), TaskState.CANCELLED)
        call = self.store.connection.execute(
            "SELECT state, late_result, disposition FROM backend_calls WHERE task_id = 'task-1'"
        ).fetchone()
        self.assertEqual(call["late_result"], 1)
        self.assertNotEqual(self.store.task_state("task-1"), TaskState.REVIEW)

    async def test_cancel_starting_call_never_invokes_backend(self) -> None:
        self._task("task-1")
        adapter = FakeBackendAdapter(
            default_behavior=FakeBehavior(delay_seconds=0.5, text="done")
        )
        token = self._controller("scheduler-test")
        claim = self.store.claim_ready_dispatch(
            "run-1", controller=token, authority=self.authority, lease_seconds=60
        )
        self.assertIsNotNone(claim)
        self.store.request_cancel_task(
            "task-1", controller=token, reason="operator-cancel"
        )
        self.assertEqual(adapter.launch_count, 0)
        self.assertEqual(self.store.task_state("task-1"), TaskState.CANCELLED)

    async def test_repeat_cancel_is_idempotent(self) -> None:
        self._task("task-1")
        token = self._controller()
        first = self.store.request_cancel_task(
            "task-1", controller=token, reason="cancel-1"
        )
        second = self.store.request_cancel_task(
            "task-1", controller=token, reason="cancel-2"
        )
        self.assertEqual(first, "cancelled")
        self.assertEqual(second, "noop")
        self.assertEqual(self.store.task_state("task-1"), TaskState.CANCELLED)

    async def test_cancel_terminal_task_is_noop(self) -> None:
        self._task("task-1")
        adapter = FakeBackendAdapter(
            default_behavior=FakeBehavior(delay_seconds=0, terminal=CallState.BLOCKED)
        )
        await scheduler_tick(
            self.store,
            run_id="run-1",
            adapters={"fake": adapter},
            authority=self.authority,
        )
        self.assertEqual(self.store.task_state("task-1"), TaskState.FAILED)
        token = self._controller()
        disposition = self.store.request_cancel_task(
            "task-1", controller=token, reason="too-late"
        )
        self.assertEqual(disposition, "noop")
        self.assertEqual(self.store.task_state("task-1"), TaskState.FAILED)

    async def test_cancel_rejected_for_stale_controller(self) -> None:
        self._task("task-1")
        held = self._controller("first-controller")
        self.store.handoff_run_controller(
            held, "second-controller", lease_seconds=60
        )
        with self.assertRaises(FencedControllerError):
            self.store.request_cancel_task(
                "task-1", controller=held, reason="stale-cancel"
            )
        self.assertEqual(self.store.task_state("task-1"), TaskState.READY)

    async def test_cancel_run_cancels_all_nonterminal_tasks(self) -> None:
        self._task("task-1")
        self._task("task-2")
        token = self._controller()
        self.store.request_cancel_run("run-1", controller=token, reason="abort-run")
        self.assertEqual(self.store.task_state("task-1"), TaskState.CANCELLED)
        self.assertEqual(self.store.task_state("task-2"), TaskState.CANCELLED)


class ServeLoopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteStateStore(Path(self.temp.name) / "state.db")
        self.store.create_run("run-1", "team-1")
        self.authority = self.store.acquire_authority(
            "run-1", "test-supervisor", "supervisor"
        )
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
        self.team_spec = TeamSpec(
            schema_version=1,
            team_id="team-1",
            bootstrap_supervisor="worker",
            roles=(),
            agent_pools=(
                AgentPoolSpec(
                    pool_id="fake-workers",
                    backend="fake",
                    role_id="worker",
                    count=1,
                    max_count=1,
                    model="fake-v1",
                ),
            ),
        )

    async def asyncTearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _task(self, task_id: str) -> None:
        self.store.create_task(
            "run-1",
            task_id,
            required_role_id="worker",
            prompt=f"execute {task_id}",
            cwd="D:/workspace/connect",
            timeout_seconds=5,
        )
        self.store.transition_task(task_id, TaskState.READY, reason="ready")

    async def test_serve_loop_dispatches_and_stops(self) -> None:
        from orchestrator.serve import serve

        self._task("task-1")
        adapter = FakeBackendAdapter(
            default_behavior=FakeBehavior(delay_seconds=0.01, text="done")
        )
        result = await serve(
            self.store,
            run_id="run-1",
            adapters={"fake": adapter},
            authority=self.authority,
            team_spec=self.team_spec,
            interval=0.05,
            max_ticks=6,
        )
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(self.store.task_state("task-1"), TaskState.REVIEW)

    async def test_serve_loop_reclaims_expired_lease_quickly(self) -> None:
        from orchestrator.serve import serve

        self._task("task-1")
        agents = self.store.pool_agent_snapshots("run-1", "fake-workers")
        self.store.create_attempt_with_lease(
            "task-1",
            "attempt-ghost",
            agents[0]["agent_id"],
            lease_seconds=1,
        )
        self.assertEqual(self.store.task_state("task-1"), TaskState.ACTIVE)

        started = time.monotonic()
        adapter = FakeBackendAdapter(
            default_behavior=FakeBehavior(delay_seconds=0.01, text="done")
        )
        result = await serve(
            self.store,
            run_id="run-1",
            adapters={"fake": adapter},
            authority=self.authority,
            team_spec=self.team_spec,
            interval=0.3,
            max_ticks=30,
        )
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 90)
        self.assertEqual(result["status"], "stopped")
        recovered_events = [
            event
            for event in self.store.events()
            if event["kind"] == "attempt.transitioned"
            and "assignment_lease_expired" in event["data_json"]
        ]
        self.assertTrue(recovered_events)


if __name__ == "__main__":
    unittest.main()
