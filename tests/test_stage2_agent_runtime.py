from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path

from orchestrator.adapters.contracts import (
    AccessPolicy,
    AdapterCallRequest,
    CallRef,
    CallSnapshot,
    CallState,
    SessionRef,
)
from orchestrator.adapters.fake import FakeBackendAdapter, FakeBehavior
from orchestrator.agent_pool import reconcile_pool_once
from orchestrator.call_runtime import execute_adapter_call, recover_starting_calls
from orchestrator.scheduler import scheduler_tick
from orchestrator.core.config import AgentPoolSpec
from orchestrator.core.models import (
    AgentState,
    AttemptState,
    ControllerToken,
    RoleBindingState,
    SessionState,
    TaskState,
)
from orchestrator.core.state_machine import (
    AGENT_TRANSITIONS,
    SESSION_TRANSITIONS,
    InvalidTransition,
    ensure_agent_transition,
    ensure_session_transition,
)
from orchestrator.storage.sqlite_store import FencedControllerError, SQLiteStateStore


class AgentRuntimeStateMachineTests(unittest.TestCase):
    def test_all_declared_agent_and_session_transitions_are_accepted(self) -> None:
        for current, targets in AGENT_TRANSITIONS.items():
            for target in targets:
                ensure_agent_transition(current, target)
        for current, targets in SESSION_TRANSITIONS.items():
            for target in targets:
                ensure_session_transition(current, target)

    def test_terminal_agent_and_session_cannot_restart(self) -> None:
        with self.assertRaises(InvalidTransition):
            ensure_agent_transition(AgentState.STOPPED, AgentState.STARTING)
        with self.assertRaises(InvalidTransition):
            ensure_session_transition(SessionState.CLOSED, SessionState.OPENING)


class AgentRuntimeStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "state.db"
        self.store = SQLiteStateStore(self.database)
        self.store.create_run("run-1", "team-1")

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _register_agent(self, agent_id: str = "agent-1") -> None:
        self.store.register_agent(
            agent_id=agent_id,
            team_id="team-1",
            pool_id="worker-pool",
            backend="fake",
            model="fake-v1",
            capabilities_actual=("read", "write"),
        )

    def test_agent_lifecycle_and_snapshot_are_persisted(self) -> None:
        self._register_agent()
        self.store.transition_agent("agent-1", AgentState.IDLE, reason="ready")
        self.store.transition_agent("agent-1", AgentState.BUSY, reason="assigned")
        snapshot = self.store.agent_snapshot("agent-1")
        self.assertEqual(snapshot["team_id"], "team-1")
        self.assertEqual(snapshot["status"], AgentState.BUSY.value)
        self.assertEqual(snapshot["capabilities_actual"], ["read", "write"])
        self.assertEqual(snapshot["authority_epoch"], 0)

    def test_only_one_active_primary_role_binding_is_allowed(self) -> None:
        self._register_agent()
        self.store.bind_role(
            binding_id="binding-1",
            run_id="run-1",
            agent_id="agent-1",
            role_id="worker",
            role_version=1,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.bind_role(
                binding_id="binding-2",
                run_id="run-1",
                agent_id="agent-1",
                role_id="reviewer",
                role_version=1,
            )
        self.store.end_role_binding("binding-1", reason="role-switch")
        self.store.bind_role(
            binding_id="binding-2",
            run_id="run-1",
            agent_id="agent-1",
            role_id="reviewer",
            role_version=1,
        )
        self.assertEqual(
            self.store.role_binding_snapshot("binding-1")["status"],
            RoleBindingState.ENDED.value,
        )

    def test_primary_role_uniqueness_is_scoped_to_agent_and_run(self) -> None:
        self._register_agent("agent-1")
        self._register_agent("agent-2")
        self.store.create_run("run-2", "team-1")
        for binding_id, run_id, agent_id in (
            ("binding-a", "run-1", "agent-1"),
            ("binding-b", "run-1", "agent-2"),
            ("binding-c", "run-2", "agent-1"),
        ):
            self.store.bind_role(
                binding_id=binding_id,
                run_id=run_id,
                agent_id=agent_id,
                role_id="worker",
                role_version=1,
            )
        self.assertEqual(
            self.store.role_binding_snapshot("binding-c")["status"],
            RoleBindingState.ACTIVE.value,
        )

    def test_session_is_owned_by_one_agent_and_run(self) -> None:
        self._register_agent()
        self.store.create_run("run-2", "team-2")
        with self.assertRaises(ValueError):
            self.store.create_backend_session(
                session_ref_id="session-wrong-run",
                run_id="run-2",
                agent_id="agent-1",
                backend="fake",
                provider_session_id="provider-1",
                cwd="D:/workspace/connect",
            )

        self.store.create_backend_session(
            session_ref_id="session-1",
            run_id="run-1",
            agent_id="agent-1",
            backend="fake",
            provider_session_id="provider-1",
            cwd="D:/workspace/connect",
        )
        self.store.transition_backend_session(
            "session-1", SessionState.IDLE, reason="opened"
        )
        self.store.transition_backend_session(
            "session-1", SessionState.ACTIVE, reason="turn-started"
        )
        self.assertEqual(
            self.store.backend_session_snapshot("session-1")["state"],
            SessionState.ACTIVE.value,
        )

    def test_provider_session_identity_is_unique_per_backend(self) -> None:
        self._register_agent("agent-1")
        self._register_agent("agent-2")
        self.store.create_backend_session(
            session_ref_id="session-1",
            run_id="run-1",
            agent_id="agent-1",
            backend="fake",
            provider_session_id="provider-shared",
            cwd="D:/workspace/connect",
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.create_backend_session(
                session_ref_id="session-2",
                run_id="run-1",
                agent_id="agent-2",
                backend="fake",
                provider_session_id="provider-shared",
                cwd="D:/workspace/connect",
            )


class SchemaMigrationTests(unittest.TestCase):
    def test_v2_database_migrates_without_losing_existing_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "v2.db"
            with SQLiteStateStore(database) as initial:
                initial.create_run("run-old", "team-old")
                initial.create_task("run-old", "task-old")
                initial.connection.execute("PRAGMA user_version=2")
            with SQLiteStateStore(database) as migrated:
                self.assertEqual(migrated.task_state("task-old").value, "PENDING")
                version = migrated.connection.execute(
                    "PRAGMA user_version"
                ).fetchone()[0]
                self.assertEqual(version, 12)
                tables = {
                    row[0]
                    for row in migrated.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertTrue(
                    {
                        "agent_instances",
                        "role_bindings",
                        "backend_sessions",
                        "backend_calls",
                        "task_dependencies",
                    }
                    <= tables
                )

    def test_v7_database_adds_dag_and_retry_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "v7.db"
            with SQLiteStateStore(database) as initial:
                initial.create_run("run-old", "team-old")
                initial.create_task("run-old", "task-old")
                initial.connection.execute("DROP TABLE task_dependencies")
                initial.connection.execute(
                    "ALTER TABLE task_dispatch_specs DROP COLUMN retry_backoff_base_seconds"
                )
                initial.connection.execute(
                    "ALTER TABLE task_dispatch_specs DROP COLUMN retry_backoff_max_seconds"
                )
                initial.connection.execute("PRAGMA user_version=7")
            with SQLiteStateStore(database) as migrated:
                self.assertEqual(
                    migrated.connection.execute("PRAGMA user_version").fetchone()[0],
                    12,
                )
                self.assertIsNotNone(
                    migrated.connection.execute(
                        """
                        SELECT name FROM sqlite_master
                        WHERE type = 'table' AND name = 'task_dependencies'
                        """
                    ).fetchone()
                )
                dispatch = migrated.connection.execute(
                    """
                    SELECT retry_backoff_base_seconds,
                           retry_backoff_max_seconds
                    FROM task_dispatch_specs WHERE task_id = 'task-old'
                    """
                ).fetchone()
                self.assertEqual(
                    tuple(dispatch),
                    (1, 60),
                )


class AgentPoolReconcilerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "state.db"
        self.store = SQLiteStateStore(self.database)
        self.store.create_run("run-1", "team-1")

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _spec(self, count: int) -> AgentPoolSpec:
        return AgentPoolSpec(
            pool_id="fake-workers",
            backend="fake",
            role_id="worker",
            count=count,
            max_count=4,
            model="fake-v1",
        )

    def test_pool_scales_from_one_to_two_to_four_idempotently(self) -> None:
        for expected in (1, 2, 4):
            result = reconcile_pool_once(self.store, "run-1", self._spec(expected))
            self.assertEqual(result["active"], expected)
            self.assertEqual(
                len(self.store.pool_agent_snapshots("run-1", "fake-workers")),
                expected,
            )
        repeated = reconcile_pool_once(self.store, "run-1", self._spec(4))
        self.assertEqual(repeated["created"], [])
        self.assertEqual(repeated["active"], 4)

    def test_scale_down_drains_busy_agent_and_stops_only_idle_agents(self) -> None:
        reconcile_pool_once(self.store, "run-1", self._spec(4))
        agents = self.store.pool_agent_snapshots("run-1", "fake-workers")
        for index, agent in enumerate(agents[-2:], start=1):
            task_id = f"task-busy-{index}"
            self.store.create_task("run-1", task_id)
            self.store.transition_task(task_id, TaskState.READY, reason="ready")
            self.store.assign_agent_task(
                agent["agent_id"], task_id, reason="scheduled"
            )

        reduced = reconcile_pool_once(self.store, "run-1", self._spec(1))
        self.assertEqual(len(reduced["stopped"]), 2)
        self.assertEqual(len(reduced["draining"]), 1)
        busy_agent = reduced["draining"][0]
        snapshot = self.store.agent_snapshot(busy_agent)
        self.assertEqual(snapshot["status"], AgentState.DRAINING.value)
        self.assertTrue(snapshot["current_task_id"].startswith("task-busy-"))

        self.store.release_agent_task(busy_agent, reason="task-finished")
        drained = reconcile_pool_once(self.store, "run-1", self._spec(1))
        self.assertIn(busy_agent, drained["stopped"])
        self.assertEqual(
            self.store.agent_snapshot(busy_agent)["status"], AgentState.STOPPED.value
        )
        self.assertEqual(drained["active"], 1)

    def test_zero_count_and_other_run_are_isolated(self) -> None:
        reconcile_pool_once(self.store, "run-1", self._spec(2))
        self.store.create_run("run-2", "team-1")
        reconcile_pool_once(self.store, "run-2", self._spec(1))
        self.assertEqual(
            len(self.store.pool_agent_snapshots("run-1", "fake-workers")), 2
        )
        stopped = reconcile_pool_once(self.store, "run-1", self._spec(0))
        self.assertEqual(stopped["active"], 0)
        self.assertEqual(len(stopped["stopped"]), 2)
        self.assertEqual(
            len(self.store.pool_agent_snapshots("run-2", "fake-workers")), 1
        )

    def test_two_connections_cannot_overprovision_same_pool(self) -> None:
        self.store.close()
        first_started = threading.Event()
        allow_first_to_continue = threading.Event()
        first_result: dict[str, object] = {}

        class PausingStore(SQLiteStateStore):
            def provision_fake_pool_agent(self, **kwargs):
                first_started.set()
                if not allow_first_to_continue.wait(timeout=5):
                    raise TimeoutError("test did not release first reconciler")
                return super().provision_fake_pool_agent(**kwargs)

        def run_first() -> None:
            with PausingStore(self.database) as store:
                first_result.update(
                    reconcile_pool_once(store, "run-1", self._spec(2))
                )

        worker = threading.Thread(target=run_first)
        worker.start()
        self.assertTrue(first_started.wait(timeout=5))
        with SQLiteStateStore(self.database) as second_store:
            second = reconcile_pool_once(second_store, "run-1", self._spec(2))
        allow_first_to_continue.set()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        with SQLiteStateStore(self.database) as verification:
            agents = verification.pool_agent_snapshots("run-1", "fake-workers")
            total_agents = verification.connection.execute(
                "SELECT COUNT(*) FROM agent_instances WHERE pool_id = 'fake-workers'"
            ).fetchone()[0]
        self.assertEqual(len(agents), 2)
        self.assertEqual(total_agents, 2)
        self.assertEqual(second["status"], "busy")
        self.assertEqual(first_result["active"], 2)
        self.store = SQLiteStateStore(self.database)

    def test_expired_pool_lock_is_recovered(self) -> None:
        owner = self.store.acquire_pool_reconcile_lock(
            "run-1", "fake-workers", lease_seconds=30
        )
        self.assertIsNotNone(owner)
        with self.store.connection:
            self.store.connection.execute(
                """
                UPDATE pool_reconcile_locks SET expires_at = ?
                WHERE run_id = ? AND pool_id = ?
                """,
                ("2000-01-01T00:00:00+00:00", "run-1", "fake-workers"),
            )
        result = reconcile_pool_once(self.store, "run-1", self._spec(1))
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["active"], 1)


class AgentPoolParallelExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_four_pool_sessions_execute_with_real_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.db")
            try:
                store.create_run("run-1", "team-1")
                reconcile_pool_once(
                    store,
                    "run-1",
                    AgentPoolSpec(
                        pool_id="fake-workers",
                        backend="fake",
                        role_id="worker",
                        count=4,
                        max_count=4,
                        model="fake-v1",
                    ),
                )
                agents = store.pool_agent_snapshots("run-1", "fake-workers")
                adapter = FakeBackendAdapter(
                    behaviors={
                        f"call-{index}": FakeBehavior(delay_seconds=0.06)
                        for index in range(4)
                    }
                )
                requests = [
                    AdapterCallRequest(
                        call_id=f"call-{index}",
                        run_id="run-1",
                        task_id=f"task-{index}",
                        attempt_id=f"attempt-{index}",
                        generation=1,
                        agent_id=agent["agent_id"],
                        session=SessionRef(
                            session_id=agent["session_ref_id"], backend="fake"
                        ),
                        prompt="overlap",
                        policy=AccessPolicy(
                            access_mode="read_only",
                            cwd="D:/workspace/connect",
                            timeout_seconds=1,
                        ),
                    )
                    for index, agent in enumerate(agents)
                ]
                started = time.monotonic()
                running = await asyncio.gather(
                    *(adapter.start(request) for request in requests)
                )
                results = await asyncio.gather(*(call.wait() for call in running))
                elapsed = time.monotonic() - started
                self.assertEqual(
                    [result.state for result in results],
                    [CallState.SUCCEEDED] * 4,
                )
                self.assertLess(elapsed, 0.16)
            finally:
                store.close()


class RunControllerLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteStateStore(Path(self.temp.name) / "state.db")
        self.store.create_run("run-1", "team-1")

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_acquire_renew_and_expired_takeover_keep_epoch_monotonic(self) -> None:
        for invalid_seconds in (0, -1):
            with self.assertRaises(ValueError):
                self.store.acquire_run_controller(
                    "run-1", "invalid", lease_seconds=invalid_seconds
                )
        first = self.store.acquire_run_controller(
            "run-1",
            "scheduler-a",
            lease_seconds=30,
            now="2030-01-01T00:00:00+00:00",
        )
        self.assertEqual(first.epoch, 1)
        with self.assertRaises(ValueError):
            self.store.renew_run_controller(first, lease_seconds=0)
        with self.assertRaises(ValueError):
            self.store.handoff_run_controller(
                first, "scheduler-invalid", lease_seconds=0
            )
        renewed = self.store.renew_run_controller(
            first,
            lease_seconds=30,
            now="2030-01-01T00:00:05+00:00",
        )
        self.assertEqual(renewed.epoch, 1)
        self.assertIsNone(
            self.store.acquire_run_controller(
                "run-1",
                "scheduler-b",
                lease_seconds=30,
                now="2030-01-01T00:00:06+00:00",
            )
        )
        takeover = self.store.acquire_run_controller(
            "run-1",
            "scheduler-b",
            lease_seconds=30,
            now="2030-01-01T00:00:36+00:00",
        )
        self.assertEqual(takeover.epoch, 2)
        with self.assertRaises(FencedControllerError):
            self.store.renew_run_controller(
                first,
                lease_seconds=30,
                now="2030-01-01T00:00:37+00:00",
            )

    def test_handoff_increments_once_and_old_token_has_zero_claim_side_effects(self) -> None:
        reconcile_pool_once(
            self.store,
            "run-1",
            AgentPoolSpec(
                pool_id="fake-workers",
                backend="fake",
                role_id="worker",
                count=1,
                max_count=1,
            ),
        )
        self.store.create_task("run-1", "task-1", prompt="work")
        self.store.transition_task("task-1", TaskState.READY, reason="ready")
        first = self.store.acquire_run_controller(
            "run-1",
            "scheduler-a",
            lease_seconds=60,
            now="2030-01-01T00:00:00+00:00",
        )
        second = self.store.handoff_run_controller(
            first,
            "scheduler-b",
            lease_seconds=60,
            now="2030-01-01T00:00:01+00:00",
        )
        self.assertEqual(second.epoch, first.epoch + 1)
        event_count = len(self.store.events())
        with self.assertRaises(FencedControllerError):
            self.store.claim_ready_dispatch(
                "run-1",
                controller=first,
                lease_seconds=60,
                now="2030-01-01T00:00:02+00:00",
            )
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0],
            0,
        )
        self.assertEqual(len(self.store.events()), event_count)
        claim = self.store.claim_ready_dispatch(
            "run-1",
            controller=second,
            lease_seconds=60,
            now="2030-01-01T00:00:02+00:00",
        )
        self.assertIsNotNone(claim)

    def test_old_controller_callback_and_recovery_have_zero_side_effects(self) -> None:
        reconcile_pool_once(
            self.store,
            "run-1",
            AgentPoolSpec(
                pool_id="fake-workers",
                backend="fake",
                role_id="worker",
                count=1,
                max_count=1,
            ),
        )
        self.store.create_task("run-1", "task-1", prompt="work")
        self.store.transition_task("task-1", TaskState.READY, reason="ready")
        first = self.store.acquire_run_controller(
            "run-1",
            "scheduler-a",
            lease_seconds=60,
            now="2030-01-01T00:00:00+00:00",
        )
        claim = self.store.claim_ready_dispatch(
            "run-1",
            controller=first,
            lease_seconds=60,
            now="2030-01-01T00:00:01+00:00",
        )
        second = self.store.handoff_run_controller(
            first,
            "scheduler-b",
            lease_seconds=60,
            now="2030-01-01T00:00:02+00:00",
        )
        snapshot = CallSnapshot(
            ref=CallRef(
                call_id=claim.call_id,
                backend="fake",
                session=claim.session,
                provider_call_id="provider-call-1",
            ),
            state=CallState.RUNNING,
            started_at=None,
            backend_invoked=True,
        )
        before = (
            self.store.backend_call_snapshot(claim.call_id),
            self.store.task_state("task-1"),
            self.store.attempt_state(claim.attempt_id),
            len(self.store.events()),
        )
        with self.assertRaises(FencedControllerError):
            self.store.mark_backend_call_running(
                claim.call_id,
                snapshot,
                reason="stale-start",
                controller=first,
            )
        with self.assertRaises(FencedControllerError):
            self.store.finish_backend_call(
                claim.call_id,
                CallSnapshot(
                    ref=snapshot.ref,
                    state=CallState.SUCCEEDED,
                    started_at="2030-01-01T00:00:01+00:00",
                    finished_at="2030-01-01T00:00:03+00:00",
                    text="stale result",
                ),
                reason="stale-finish",
                controller=first,
            )
        with self.assertRaises(FencedControllerError):
            self.store.recover_backend_call(
                claim.call_id,
                controller=first,
                reason="stale-recovery",
            )
        self.assertEqual(self.store.backend_call_snapshot(claim.call_id), before[0])
        self.assertEqual(self.store.task_state("task-1"), before[1])
        self.assertEqual(self.store.attempt_state(claim.attempt_id), before[2])
        self.assertEqual(len(self.store.events()), before[3])
        self.assertEqual(
            self.store.recover_backend_call(
                claim.call_id,
                controller=second,
                reason="controller-takeover",
            ),
            "requeued",
        )
        self.assertEqual(self.store.task_state("task-1"), TaskState.READY)
        self.assertEqual(
            self.store.attempt_state(claim.attempt_id), AttemptState.STALE
        )


class TaskDagTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteStateStore(Path(self.temp.name) / "state.db")
        self.store.create_run("run-1", "team-1")

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _complete(self, task_id: str) -> None:
        self.store.transition_task(task_id, TaskState.ACTIVE, reason="started")
        self.store.transition_task(task_id, TaskState.REVIEW, reason="submitted")
        self.store.transition_task(task_id, TaskState.COMPLETED, reason="accepted")

    def test_fan_in_becomes_ready_only_after_every_dependency_completes(self) -> None:
        self.store.create_task_graph(
            "run-1",
            [
                {"task_id": "root-a"},
                {"task_id": "root-b"},
                {"task_id": "join"},
            ],
            [("join", "root-a"), ("join", "root-b")],
        )
        self.assertEqual(self.store.task_state("root-a"), TaskState.READY)
        self.assertEqual(self.store.task_state("root-b"), TaskState.READY)
        self.assertEqual(self.store.task_state("join"), TaskState.PENDING)
        self._complete("root-a")
        self.assertEqual(self.store.task_state("join"), TaskState.PENDING)
        self._complete("root-b")
        self.assertEqual(self.store.task_state("join"), TaskState.READY)

    def test_failure_cancels_all_transitive_dependents(self) -> None:
        self.store.create_task_graph(
            "run-1",
            [{"task_id": "a"}, {"task_id": "b"}, {"task_id": "c"}],
            [("b", "a"), ("c", "b")],
        )
        self.store.transition_task("a", TaskState.ACTIVE, reason="started")
        self.store.transition_task("a", TaskState.FAILED, reason="failed")
        self.assertEqual(self.store.task_state("b"), TaskState.CANCELLED)
        self.assertEqual(self.store.task_state("c"), TaskState.CANCELLED)

    def test_upstream_cancel_also_cancels_dependents(self) -> None:
        self.store.create_task_graph(
            "run-1",
            [{"task_id": "root"}, {"task_id": "dependent"}],
            [("dependent", "root")],
        )
        self.store.transition_task(
            "root", TaskState.CANCEL_REQUESTED, reason="cancel-requested"
        )
        self.store.transition_task("root", TaskState.CANCELLED, reason="cancelled")
        self.assertEqual(self.store.task_state("dependent"), TaskState.CANCELLED)

    def test_cycle_and_missing_dependency_roll_back_entire_graph(self) -> None:
        for dependencies in (
            [("a", "a")],
            [("a", "b"), ("b", "a")],
            [("a", "missing")],
        ):
            with self.subTest(dependencies=dependencies):
                with self.assertRaises(ValueError):
                    self.store.create_task_graph(
                        "run-1",
                        [{"task_id": "a"}, {"task_id": "b"}],
                        dependencies,
                    )
                self.assertEqual(
                    self.store.connection.execute(
                        "SELECT COUNT(*) FROM tasks"
                    ).fetchone()[0],
                    0,
                )

    def test_manual_ready_is_rejected_while_dependency_is_incomplete(self) -> None:
        self.store.create_task_graph(
            "run-1",
            [{"task_id": "root"}, {"task_id": "dependent"}],
            [("dependent", "root")],
        )
        event_count = len(self.store.events())
        with self.assertRaises(ValueError):
            self.store.transition_task(
                "dependent", TaskState.READY, reason="manual-bypass"
            )
        self.assertEqual(self.store.task_state("dependent"), TaskState.PENDING)
        self.assertEqual(len(self.store.events()), event_count)

    def test_cross_run_dependency_is_rejected(self) -> None:
        self.store.create_run("run-2", "team-1")
        self.store.create_task("run-2", "other-run-task")
        with self.assertRaises(ValueError):
            self.store.create_task_graph(
                "run-1",
                [{"task_id": "local"}],
                [("local", "other-run-task")],
            )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE run_id = 'run-1'"
            ).fetchone()[0],
            0,
        )

    def test_concurrent_fan_in_completion_readies_dependent_once(self) -> None:
        self.store.create_task_graph(
            "run-1",
            [
                {"task_id": "root-a"},
                {"task_id": "root-b"},
                {"task_id": "join"},
            ],
            [("join", "root-a"), ("join", "root-b")],
        )
        database = self.store.path
        barrier = threading.Barrier(2)

        def complete(task_id: str) -> None:
            with SQLiteStateStore(database) as store:
                store.transition_task(task_id, TaskState.ACTIVE, reason="started")
                store.transition_task(task_id, TaskState.REVIEW, reason="submitted")
                barrier.wait(timeout=5)
                store.transition_task(task_id, TaskState.COMPLETED, reason="accepted")

        first = threading.Thread(target=complete, args=("root-a",))
        second = threading.Thread(target=complete, args=("root-b",))
        first.start()
        second.start()
        first.join(timeout=5)
        second.join(timeout=5)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(self.store.task_state("join"), TaskState.READY)
        ready_events = [
            event
            for event in self.store.events(task_id="join")
            if event["to_state"] == TaskState.READY.value
        ]
        self.assertEqual(len(ready_events), 1)


class BackendCallPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteStateStore(Path(self.temp.name) / "state.db")
        self.store.create_run("run-1", "team-1")
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
        self.agent = self.store.pool_agent_snapshots(
            "run-1", "fake-workers"
        )[0]
        self.store.create_task("run-1", "task-1")
        self.store.transition_task("task-1", TaskState.READY, reason="ready")
        self.store.assign_agent_task(
            self.agent["agent_id"], "task-1", reason="scheduled"
        )
        lease = self.store.create_attempt_with_lease(
            "task-1", "attempt-1", self.agent["agent_id"]
        )
        self.generation = lease["generation"]

    async def asyncTearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _request(self, call_id: str = "call-1") -> AdapterCallRequest:
        return AdapterCallRequest(
            call_id=call_id,
            run_id="run-1",
            task_id="task-1",
            attempt_id="attempt-1",
            generation=self.generation,
            agent_id=self.agent["agent_id"],
            session=SessionRef(
                session_id=self.agent["session_ref_id"],
                backend="fake",
                provider_session_id=None,
            ),
            prompt="persisted only as a digest",
            policy=AccessPolicy(
                access_mode="read_only",
                cwd="D:/workspace/connect",
                timeout_seconds=1,
            ),
        )

    async def test_intent_exists_before_adapter_start_and_success_submits_atomically(self) -> None:
        store = self.store

        class InspectingFake(FakeBackendAdapter):
            async def start(self, request: AdapterCallRequest):
                self.seen_state = store.backend_call_snapshot(request.call_id)["state"]
                return await super().start(request)

        adapter = InspectingFake(
            behaviors={"call-1": FakeBehavior(delay_seconds=0, text="done")}
        )
        terminal = await execute_adapter_call(store, adapter, self._request())
        persisted = store.backend_call_snapshot("call-1")
        self.assertEqual(adapter.seen_state, "starting")
        self.assertEqual(terminal.state, CallState.SUCCEEDED)
        self.assertEqual(persisted["state"], CallState.SUCCEEDED.value)
        self.assertEqual(persisted["attempt_id"], "attempt-1")
        self.assertEqual(persisted["generation"], self.generation)
        self.assertEqual(persisted["late_result"], 0)
        self.assertIsNotNone(persisted["request_digest"])
        self.assertNotIn("persisted only as a digest", str(persisted))
        self.assertEqual(
            store.attempt_state("attempt-1"), AttemptState.SUBMITTED
        )
        self.assertEqual(store.task_state("task-1"), TaskState.REVIEW)
        event_count = len(store.events())
        repeated = store.finish_backend_call(
            "call-1", terminal, reason="duplicate-callback"
        )
        self.assertEqual(repeated, "submitted")
        self.assertEqual(len(store.events()), event_count)

    async def test_ambiguous_start_is_orphaned_and_attempt_is_fenced(self) -> None:
        request = self._request()
        self.store.create_backend_call(request)
        recovered = recover_starting_calls(self.store, run_id="run-1")
        self.assertEqual(recovered, ["call-1"])
        call = self.store.backend_call_snapshot("call-1")
        self.assertEqual(call["state"], CallState.ORPHANED.value)
        self.assertEqual(self.store.attempt_state("attempt-1"), AttemptState.STALE)
        self.assertEqual(self.store.task_state("task-1"), TaskState.READY)

    async def test_old_generation_success_is_recorded_as_late_only(self) -> None:
        request = self._request()
        self.store.create_backend_call(request)
        self.store.transition_attempt(
            "attempt-1", AttemptState.RUNNING, reason="backend-started"
        )
        self.store.recover_lost_attempt(
            "attempt-1", self.generation, reason="cancel-unconfirmed"
        )
        adapter = FakeBackendAdapter(
            behaviors={"call-1": FakeBehavior(delay_seconds=0, text="late")}
        )
        running = await adapter.start(request)
        terminal = await running.wait()
        outcome = self.store.finish_backend_call(
            "call-1", terminal, reason="late-terminal"
        )
        self.assertEqual(outcome, "late")
        self.assertEqual(self.store.backend_call_snapshot("call-1")["late_result"], 1)
        self.assertEqual(self.store.attempt_state("attempt-1"), AttemptState.STALE)
        self.assertEqual(self.store.task_state("task-1"), TaskState.READY)


class SchedulerClosedLoopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteStateStore(Path(self.temp.name) / "state.db")
        self.store.create_run("run-1", "team-1")
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

    async def asyncTearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _task(
        self,
        task_id: str,
        *,
        max_attempts: int = 2,
        priority: int = 0,
        retry_backoff_base_seconds: int = 1,
        retry_backoff_max_seconds: int = 60,
    ) -> None:
        self.store.create_task(
            "run-1",
            task_id,
            required_role_id="worker",
            prompt=f"execute {task_id}",
            cwd="D:/workspace/connect",
            timeout_seconds=1,
            max_attempts=max_attempts,
            priority=priority,
            retry_backoff_base_seconds=retry_backoff_base_seconds,
            retry_backoff_max_seconds=retry_backoff_max_seconds,
        )
        self.store.transition_task(task_id, TaskState.READY, reason="ready")

    async def test_two_tasks_run_in_parallel_and_repeat_tick_is_idempotent(self) -> None:
        self._task("task-1")
        self._task("task-2")
        adapter = FakeBackendAdapter(
            default_behavior=FakeBehavior(delay_seconds=0.06, text="done")
        )
        started = time.monotonic()
        result = await scheduler_tick(
            self.store,
            run_id="run-1",
            adapters={"fake": adapter},
        )
        elapsed = time.monotonic() - started
        self.assertEqual(len(result["dispatched"]), 2)
        self.assertLess(elapsed, 0.16)
        for task_id in ("task-1", "task-2"):
            self.assertEqual(self.store.task_state(task_id), TaskState.REVIEW)
        attempts = self.store.connection.execute(
            "SELECT state FROM attempts ORDER BY attempt_id"
        ).fetchall()
        self.assertEqual(
            [row["state"] for row in attempts],
            [AttemptState.SUBMITTED.value] * 2,
        )
        agents = self.store.pool_agent_snapshots("run-1", "fake-workers")
        self.assertTrue(
            all(
                agent["status"] == AgentState.IDLE.value
                and agent["session_state"] == SessionState.IDLE.value
                and agent["current_task_id"] is None
                for agent in agents
            )
        )
        business_event_count = len(
            [
                event
                for event in self.store.events()
                if not event["kind"].startswith("controller.")
            ]
        )
        repeated = await scheduler_tick(
            self.store,
            run_id="run-1",
            adapters={"fake": adapter},
        )
        self.assertEqual(repeated["dispatched"], [])
        self.assertEqual(
            len(
                [
                    event
                    for event in self.store.events()
                    if not event["kind"].startswith("controller.")
                ]
            ),
            business_event_count,
        )

    async def test_busy_run_controller_prevents_dispatch(self) -> None:
        self._task("task-1")
        held = self.store.acquire_run_controller(
            "run-1", "scheduler-a", lease_seconds=60
        )
        result = await scheduler_tick(
            self.store,
            run_id="run-1",
            adapters={"fake": FakeBackendAdapter()},
            scheduler_owner="scheduler-b",
        )
        self.assertEqual(result["status"], "busy")
        self.assertEqual(result["dispatched"], [])
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0],
            0,
        )
        self.store.release_run_controller(held)

    async def test_retryable_failure_requeues_then_exhausts_attempts(self) -> None:
        self._task("task-1", max_attempts=2)
        adapter = FakeBackendAdapter(
            default_behavior=FakeBehavior(
                delay_seconds=0, terminal=CallState.FAILED
            )
        )
        first = await scheduler_tick(
            self.store, run_id="run-1", adapters={"fake": adapter}
        )
        self.assertEqual(first["outcomes"][0]["disposition"], "requeued")
        self.assertEqual(self.store.task_state("task-1"), TaskState.READY)
        delayed = await scheduler_tick(
            self.store, run_id="run-1", adapters={"fake": adapter}
        )
        self.assertEqual(delayed["dispatched"], [])
        retry_window = self.store.connection.execute(
            """
            SELECT d.available_at, c.settled_at
            FROM task_dispatch_specs d
            JOIN backend_calls c ON c.task_id = d.task_id
            WHERE d.task_id = 'task-1'
            ORDER BY c.requested_at DESC LIMIT 1
            """
        ).fetchone()
        self.assertGreater(retry_window["available_at"], retry_window["settled_at"])
        with self.store.connection:
            self.store.connection.execute(
                """
                UPDATE task_dispatch_specs
                SET available_at = '2000-01-01T00:00:00+00:00'
                WHERE task_id = 'task-1'
                """
            )
        second = await scheduler_tick(
            self.store, run_id="run-1", adapters={"fake": adapter}
        )
        self.assertEqual(second["outcomes"][0]["disposition"], "failed")
        self.assertEqual(self.store.task_state("task-1"), TaskState.FAILED)
        attempts = self.store.connection.execute(
            "SELECT state FROM attempts WHERE task_id = 'task-1'"
        ).fetchall()
        self.assertEqual(
            [row["state"] for row in attempts],
            [AttemptState.FAILED.value, AttemptState.FAILED.value],
        )

    async def test_blocked_is_terminal_without_backend_launch_or_capacity_leak(self) -> None:
        self._task("task-1")
        adapter = FakeBackendAdapter(
            default_behavior=FakeBehavior(
                delay_seconds=0, terminal=CallState.BLOCKED
            )
        )
        result = await scheduler_tick(
            self.store, run_id="run-1", adapters={"fake": adapter}
        )
        self.assertEqual(result["outcomes"][0]["disposition"], "failed")
        self.assertEqual(adapter.launch_count, 0)
        self.assertEqual(self.store.task_state("task-1"), TaskState.FAILED)
        self.assertEqual(
            self.store.connection.execute(
                "SELECT state FROM attempts WHERE task_id = 'task-1'"
            ).fetchone()["state"],
            AttemptState.FAILED.value,
        )
        agents = self.store.pool_agent_snapshots("run-1", "fake-workers")
        self.assertTrue(all(agent["status"] == "IDLE" for agent in agents))

    async def test_priority_orders_ready_tasks_and_future_high_does_not_block_low(self) -> None:
        self._task("low", priority=1)
        self._task("high", priority=10)
        result = await scheduler_tick(
            self.store,
            run_id="run-1",
            adapters={"fake": FakeBackendAdapter()},
        )
        self.assertEqual(
            [outcome["task_id"] for outcome in result["outcomes"]],
            ["high", "low"],
        )

        self._task("low-2", priority=1)
        self._task("high-future", priority=100)
        with self.store.connection:
            self.store.connection.execute(
                """
                UPDATE task_dispatch_specs
                SET available_at = '2099-01-01T00:00:00+00:00'
                WHERE task_id = 'high-future'
                """
            )
        result = await scheduler_tick(
            self.store,
            run_id="run-1",
            adapters={"fake": FakeBackendAdapter()},
            limit=1,
        )
        self.assertEqual(result["outcomes"][0]["task_id"], "low-2")
        self.assertEqual(self.store.task_state("high-future"), TaskState.READY)

    async def test_retry_backoff_doubles_and_caps(self) -> None:
        self._task(
            "task-1",
            max_attempts=4,
            retry_backoff_base_seconds=2,
            retry_backoff_max_seconds=3,
        )
        adapter = FakeBackendAdapter(
            default_behavior=FakeBehavior(delay_seconds=0, terminal=CallState.FAILED)
        )
        for expected_delay in (2, 3, 3):
            result = await scheduler_tick(
                self.store,
                run_id="run-1",
                adapters={"fake": adapter},
            )
            self.assertEqual(result["outcomes"][0]["disposition"], "requeued")
            row = self.store.connection.execute(
                """
                SELECT d.available_at, c.settled_at
                FROM task_dispatch_specs d
                JOIN backend_calls c ON c.task_id = d.task_id
                WHERE d.task_id = 'task-1'
                ORDER BY c.requested_at DESC LIMIT 1
                """
            ).fetchone()
            delay = (
                datetime.fromisoformat(row["available_at"])
                - datetime.fromisoformat(row["settled_at"])
            ).total_seconds()
            self.assertEqual(delay, expected_delay)
            with self.store.connection:
                self.store.connection.execute(
                    """
                    UPDATE task_dispatch_specs
                    SET available_at = '2000-01-01T00:00:00+00:00'
                    WHERE task_id = 'task-1'
                    """
                )

    async def test_draining_agent_is_never_dispatched(self) -> None:
        agents = self.store.pool_agent_snapshots("run-1", "fake-workers")
        draining_id = agents[-1]["agent_id"]
        self.store.mark_agent_draining(
            draining_id, run_id="run-1", reason="scale-down"
        )
        self._task("task-1")
        self._task("task-2")
        result = await scheduler_tick(
            self.store,
            run_id="run-1",
            adapters={"fake": FakeBackendAdapter()},
        )
        self.assertEqual(len(result["dispatched"]), 1)
        states = {
            task_id: self.store.task_state(task_id)
            for task_id in ("task-1", "task-2")
        }
        self.assertEqual(list(states.values()).count(TaskState.REVIEW), 1)
        self.assertEqual(list(states.values()).count(TaskState.READY), 1)
        self.assertEqual(
            self.store.agent_snapshot(draining_id)["status"],
            AgentState.DRAINING.value,
        )

    async def test_claim_then_restart_recovery_releases_agent_and_session(self) -> None:
        self._task("task-1")
        first = self.store.acquire_run_controller(
            "run-1", "scheduler-1", lease_seconds=60
        )
        claim = self.store.claim_ready_dispatch(
            "run-1", controller=first, lease_seconds=60
        )
        self.assertIsNotNone(claim)
        second = self.store.handoff_run_controller(
            first, "scheduler-2", lease_seconds=60
        )
        recovered = recover_starting_calls(
            self.store, run_id="run-1", controller=second
        )
        self.assertEqual(recovered, [claim.call_id])
        self.assertEqual(self.store.task_state("task-1"), TaskState.READY)
        self.assertEqual(
            self.store.agent_snapshot(claim.agent_id)["status"],
            AgentState.IDLE.value,
        )
        self.assertEqual(
            self.store.backend_session_snapshot(claim.session.session_id)["state"],
            SessionState.IDLE.value,
        )

    async def test_authorized_start_is_isolated_after_controller_handoff(self) -> None:
        self._task("task-1")
        first = self.store.acquire_run_controller(
            "run-1", "scheduler-1", lease_seconds=60
        )
        claim = self.store.claim_ready_dispatch(
            "run-1", controller=first, lease_seconds=60
        )
        self.store.authorize_backend_call(claim.call_id, controller=first)
        second = self.store.handoff_run_controller(
            first, "scheduler-2", lease_seconds=60
        )
        recovered = recover_starting_calls(
            self.store, run_id="run-1", controller=second
        )
        self.assertEqual(recovered, [claim.call_id])
        self.assertEqual(
            self.store.agent_snapshot(claim.agent_id)["status"],
            AgentState.DRAINING.value,
        )
        self.assertEqual(
            self.store.backend_session_snapshot(claim.session.session_id)["state"],
            SessionState.FAILED.value,
        )

    async def test_legacy_unfenced_active_call_is_recovered_conservatively(self) -> None:
        self._task("task-1")
        controller = self.store.acquire_run_controller(
            "run-1", "scheduler-1", lease_seconds=60
        )
        claim = self.store.claim_ready_dispatch(
            "run-1", controller=controller, lease_seconds=60
        )
        with self.store.connection:
            self.store.connection.execute(
                """
                UPDATE backend_calls
                SET controller_epoch = NULL, scheduler_owner = NULL
                WHERE call_id = ?
                """,
                (claim.call_id,),
            )
        recovered = recover_starting_calls(
            self.store, run_id="run-1", controller=controller
        )
        self.assertEqual(recovered, [claim.call_id])
        self.assertEqual(self.store.task_state("task-1"), TaskState.READY)
        self.assertEqual(
            self.store.backend_session_snapshot(claim.session.session_id)["state"],
            SessionState.FAILED.value,
        )

    async def test_scheduler_renews_controller_during_long_call(self) -> None:
        self._task("task-1")
        with self.store.connection:
            self.store.connection.execute(
                """
                UPDATE task_dispatch_specs SET timeout_seconds = 3
                WHERE task_id = 'task-1'
                """
            )
        result = await scheduler_tick(
            self.store,
            run_id="run-1",
            adapters={
                "fake": FakeBackendAdapter(
                    default_behavior=FakeBehavior(delay_seconds=1.1, text="done")
                )
            },
            controller_lease_seconds=1,
        )
        self.assertEqual(result["outcomes"][0]["disposition"], "submitted")
        self.assertEqual(self.store.task_state("task-1"), TaskState.REVIEW)

    async def test_scheduler_renews_assignment_lease_during_long_call(self) -> None:
        self._task("task-1")
        with self.store.connection:
            self.store.connection.execute(
                """
                UPDATE task_dispatch_specs SET timeout_seconds = 3
                WHERE task_id = 'task-1'
                """
            )
        result = await scheduler_tick(
            self.store,
            run_id="run-1",
            adapters={
                "fake": FakeBackendAdapter(
                    default_behavior=FakeBehavior(delay_seconds=1.1, text="done")
                )
            },
            lease_seconds=1,
            controller_lease_seconds=3,
        )
        self.assertEqual(result["outcomes"][0]["disposition"], "submitted")
        self.assertEqual(self.store.task_state("task-1"), TaskState.REVIEW)

    async def test_two_scheduler_connections_claim_task_exactly_once(self) -> None:
        self._task("task-1")
        database = self.store.path
        barrier = threading.Barrier(2)
        controller = self.store.acquire_run_controller(
            "run-1", "scheduler-a", lease_seconds=60
        )

        def claim():
            with SQLiteStateStore(database) as store:
                barrier.wait(timeout=5)
                return store.claim_ready_dispatch(
                    "run-1", controller=controller, lease_seconds=60
                )

        first, second = await asyncio.gather(
            asyncio.to_thread(claim),
            asyncio.to_thread(claim),
        )
        claims = [item for item in (first, second) if item is not None]
        self.assertEqual(len(claims), 1)
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM attempts WHERE task_id = 'task-1'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM backend_calls WHERE task_id = 'task-1'"
            ).fetchone()[0],
            1,
        )

    async def test_running_call_recovery_isolates_session_and_pool_replaces_agent(self) -> None:
        self._task("task-1")
        first = self.store.acquire_run_controller(
            "run-1", "scheduler-1", lease_seconds=60
        )
        claim = self.store.claim_ready_dispatch(
            "run-1", controller=first, lease_seconds=60
        )
        adapter = FakeBackendAdapter(
            default_behavior=FakeBehavior(delay_seconds=1)
        )
        running = await adapter.start(claim)
        initial = await running.wait(timeout_seconds=0)
        self.store.mark_backend_call_running(
            claim.call_id,
            initial,
            reason="adapter-started",
            controller=first,
        )
        second = self.store.handoff_run_controller(
            first, "scheduler-2", lease_seconds=60
        )
        recovered = recover_starting_calls(
            self.store, run_id="run-1", controller=second
        )
        self.assertEqual(recovered, [claim.call_id])
        self.assertEqual(self.store.task_state("task-1"), TaskState.READY)
        self.assertEqual(
            self.store.agent_snapshot(claim.agent_id)["status"],
            AgentState.DRAINING.value,
        )
        self.assertEqual(
            self.store.backend_session_snapshot(claim.session.session_id)["state"],
            SessionState.FAILED.value,
        )
        replacement = reconcile_pool_once(
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
        self.assertEqual(replacement["active"], 2)
        self.assertEqual(
            self.store.agent_snapshot(claim.agent_id)["status"],
            AgentState.STOPPED.value,
        )
        await running.cancel(reason="test-cleanup")

class FakeBackendAdapterTests(unittest.IsolatedAsyncioTestCase):
    def _request(self, call_id: str, session_id: str) -> AdapterCallRequest:
        return AdapterCallRequest(
            call_id=call_id,
            run_id="run-1",
            task_id=f"task-{call_id}",
            attempt_id=f"attempt-{call_id}",
            generation=1,
            agent_id=f"agent-{session_id}",
            session=SessionRef(
                session_id=session_id,
                backend="fake",
                provider_session_id=f"provider-{session_id}",
            ),
            prompt="perform deterministic fake work",
            policy=AccessPolicy(
                access_mode="read_only",
                cwd="D:/workspace/connect",
                timeout_seconds=1.0,
            ),
        )

    async def test_wait_timeout_is_only_a_poll_and_terminal_wait_is_idempotent(self) -> None:
        adapter = FakeBackendAdapter(
            behaviors={"call-1": FakeBehavior(delay_seconds=0.05, text="done")}
        )
        running = await adapter.start(self._request("call-1", "session-1"))
        polled = await running.wait(timeout_seconds=0)
        self.assertEqual(polled.state, CallState.RUNNING)
        finished = await running.wait()
        repeated = await running.wait(timeout_seconds=0)
        self.assertEqual(finished.state, CallState.SUCCEEDED)
        self.assertEqual(repeated, finished)
        self.assertEqual(adapter.cancel_count, 0)

    async def test_cancel_is_idempotent_and_prevents_success(self) -> None:
        adapter = FakeBackendAdapter(
            behaviors={"call-1": FakeBehavior(delay_seconds=1.0, text="too-late")}
        )
        running = await adapter.start(self._request("call-1", "session-1"))
        first = await running.cancel(reason="user-request")
        second = await running.cancel(reason="duplicate")
        terminal = await running.wait()
        self.assertEqual(first.state, CallState.CANCELLED)
        self.assertEqual(second, first)
        self.assertEqual(terminal, first)
        self.assertEqual(adapter.cancel_count, 1)

    async def test_same_session_rejects_parallel_call_but_distinct_sessions_overlap(self) -> None:
        adapter = FakeBackendAdapter(
            behaviors={
                "call-1": FakeBehavior(delay_seconds=0.08),
                "call-2": FakeBehavior(delay_seconds=0.08),
                "call-3": FakeBehavior(delay_seconds=0.08),
            }
        )
        first = await adapter.start(self._request("call-1", "session-1"))
        with self.assertRaises(RuntimeError):
            await adapter.start(self._request("call-2", "session-1"))

        started = time.monotonic()
        second = await adapter.start(self._request("call-3", "session-2"))
        one, two = await asyncio.gather(first.wait(), second.wait())
        elapsed = time.monotonic() - started
        self.assertEqual(one.state, CallState.SUCCEEDED)
        self.assertEqual(two.state, CallState.SUCCEEDED)
        self.assertLess(elapsed, 0.14)

    async def test_call_id_is_idempotent_and_policy_timeout_is_terminal(self) -> None:
        adapter = FakeBackendAdapter(
            behaviors={"call-1": FakeBehavior(delay_seconds=0.05)}
        )
        request = self._request("call-1", "session-1")
        first = await adapter.start(request)
        same = await adapter.start(request)
        self.assertIs(first, same)
        self.assertEqual(adapter.launch_count, 1)
        await first.wait()

        timed_request = AdapterCallRequest(
            **{
                **self._request("call-timeout", "session-2").__dict__,
                "policy": AccessPolicy(
                    access_mode="read_only",
                    cwd="D:/workspace/connect",
                    timeout_seconds=0.01,
                ),
            }
        )
        adapter.behaviors["call-timeout"] = FakeBehavior(delay_seconds=0.1)
        timed = await adapter.start(timed_request)
        self.assertEqual((await timed.wait()).state, CallState.TIMED_OUT)

    async def test_failure_and_policy_block_have_distinct_invocation_semantics(self) -> None:
        adapter = FakeBackendAdapter(
            behaviors={
                "call-failed": FakeBehavior(
                    delay_seconds=0, terminal=CallState.FAILED
                ),
                "call-blocked": FakeBehavior(
                    delay_seconds=0, terminal=CallState.BLOCKED
                ),
            }
        )
        failed = await (
            await adapter.start(self._request("call-failed", "session-1"))
        ).wait()
        blocked = await (
            await adapter.start(self._request("call-blocked", "session-2"))
        ).wait()
        self.assertEqual(failed.state, CallState.FAILED)
        self.assertTrue(failed.backend_invoked)
        self.assertEqual(blocked.state, CallState.BLOCKED)
        self.assertFalse(blocked.backend_invoked)
        self.assertEqual(adapter.launch_count, 1)

    async def test_cancel_after_success_preserves_success(self) -> None:
        adapter = FakeBackendAdapter(
            behaviors={"call-1": FakeBehavior(delay_seconds=0)}
        )
        running = await adapter.start(self._request("call-1", "session-1"))
        succeeded = await running.wait()
        cancelled = await running.cancel(reason="too-late")
        self.assertEqual(succeeded.state, CallState.SUCCEEDED)
        self.assertEqual(cancelled, succeeded)
        self.assertEqual(adapter.cancel_count, 0)

    async def test_unconfirmed_cancel_allows_a_late_terminal_result(self) -> None:
        adapter = FakeBackendAdapter(
            behaviors={
                "call-1": FakeBehavior(
                    delay_seconds=0.03,
                    text="late-result",
                    cancel_mode="unconfirmed",
                )
            }
        )
        running = await adapter.start(self._request("call-1", "session-1"))
        receipt = await running.cancel(reason="backend-cannot-confirm")
        self.assertEqual(receipt.state, CallState.CANCEL_REQUESTED)
        self.assertTrue(receipt.backend_may_still_run)
        terminal = await running.wait()
        self.assertEqual(terminal.state, CallState.SUCCEEDED)
        self.assertEqual(terminal.text, "late-result")
        self.assertEqual(adapter.cancel_count, 1)

    async def test_cancelling_one_waiter_does_not_cancel_backend_work(self) -> None:
        adapter = FakeBackendAdapter(
            behaviors={"call-1": FakeBehavior(delay_seconds=0.03)}
        )
        running = await adapter.start(self._request("call-1", "session-1"))
        waiter = asyncio.create_task(running.wait())
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertEqual((await running.wait()).state, CallState.SUCCEEDED)

    async def test_result_payload_is_deeply_immutable(self) -> None:
        source = {"items": [{"value": 1}]}
        adapter = FakeBackendAdapter(
            behaviors={
                "call-1": FakeBehavior(delay_seconds=0, structured=source)
            }
        )
        running = await adapter.start(self._request("call-1", "session-1"))
        finished = await running.wait()
        source["items"][0]["value"] = 99
        with self.assertRaises(TypeError):
            finished.structured["items"][0]["value"] = 2
        repeated = await running.wait()
        self.assertEqual(repeated.structured["items"][0]["value"], 1)

    async def test_cancel_complete_race_has_one_stable_terminal(self) -> None:
        adapter = FakeBackendAdapter(
            behaviors={"call-1": FakeBehavior(delay_seconds=0.001)}
        )
        running = await adapter.start(self._request("call-1", "session-1"))
        waited, cancelled = await asyncio.gather(
            running.wait(), running.cancel(reason="race")
        )
        final = await running.wait()
        self.assertTrue(final.state.is_terminal)
        self.assertEqual(waited, final)
        self.assertEqual(cancelled, final)


if __name__ == "__main__":
    unittest.main()
