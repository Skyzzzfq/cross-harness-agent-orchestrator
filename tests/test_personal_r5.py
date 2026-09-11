from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orchestrator.adapters.contracts import CallRef, CallSnapshot, CallState
from orchestrator.agent_pool import reconcile_pool_once
from orchestrator.core.config import AgentPoolSpec
from orchestrator.core.models import (
    AttemptState,
    MessageEnvelope,
    Recipient,
    TaskState,
)
from orchestrator.storage.sqlite_store import (
    CURRENT_SCHEMA_VERSION,
    FencedAttemptError,
    SQLiteStateStore,
)


class R5DeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "state.db"
        self.store = SQLiteStateStore(self.database)
        self.store.create_run("run-r5", "team-r5")
        self.store.create_task("run-r5", "task-r5", prompt="message test")
        self.controller = self.store.acquire_run_controller(
            "run-r5", "serve-r5", lease_seconds=300
        )
        self.assertIsNotNone(self.controller)

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _message(self, *, kind: str = "dispatch", attempt_id: str | None = None) -> MessageEnvelope:
        return MessageEnvelope(
            message_id=f"msg-{kind}-r5",
            team_id="team-r5",
            run_id="run-r5",
            task_id="task-r5",
            sender_agent_id="hub",
            recipients=(Recipient("agent", "worker-1"), Recipient("agent", "worker-2")),
            kind=kind,
            message_type=kind,
            source="user" if kind == "user_guidance" else "hub",
            attempt_id=attempt_id,
            target_agent_id="worker-1" if attempt_id else None,
            payload={"text": "hello"},
            correlation_id="corr-r5",
            idempotency_key=f"idem-{kind}-r5",
        )

    def test_per_recipient_delivery_is_at_least_once_and_ack_is_idempotent(self) -> None:
        self.store.append_message(self._message())
        deliveries = self.store.list_message_deliveries("run-r5")
        self.assertEqual(len(deliveries), 2)
        claimed = self.store.claim_message_deliveries(
            "run-r5", controller=self.controller, consumer_id="serve-a", limit=10
        )
        self.assertEqual(len(claimed), 2)
        self.assertEqual(claimed[0]["status"], "DELIVERED")
        self.assertEqual(
            self.store.acknowledge_message(
                claimed[0]["delivery_id"], controller=self.controller, consumer_id="serve-a"
            ),
            "acknowledged",
        )
        self.assertEqual(
            self.store.acknowledge_message(
                claimed[0]["delivery_id"], controller=self.controller, consumer_id="serve-a"
            ),
            "acknowledged",
        )
        statuses = [item["status"] for item in self.store.list_message_deliveries("run-r5")]
        self.assertEqual(statuses.count("ACKNOWLEDGED"), 1)
        self.assertEqual(statuses.count("DELIVERED"), 1)

    def test_unacked_delivery_is_reclaimed_after_controller_restart(self) -> None:
        self.store.append_message(self._message())
        claimed = self.store.claim_message_deliveries(
            "run-r5", controller=self.controller, consumer_id="serve-a", lease_seconds=1
        )
        self.assertEqual(len(claimed), 2)
        later = (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat()
        reclaimed = self.store.claim_message_deliveries(
            "run-r5",
            controller=self.controller,
            consumer_id="serve-b",
            now=later,
            limit=10,
        )
        self.assertEqual(len(reclaimed), 2)
        self.assertTrue(all(item["attempts"] == 2 for item in reclaimed))
        self.assertEqual(
            self.store.acknowledge_message(
                reclaimed[0]["delivery_id"], controller=self.controller, consumer_id="serve-b"
            ),
            "acknowledged",
        )

    def test_old_attempt_guidance_is_failed_instead_of_delivered(self) -> None:
        self.store.transition_task("task-r5", TaskState.READY, reason="ready")
        self.store.create_attempt("task-r5", "attempt-old", "worker-1")
        self.store.transition_attempt(
            "attempt-old", AttemptState.RUNNING, reason="started"
        )
        self.store.transition_attempt(
            "attempt-old", AttemptState.FAILED, reason="failed"
        )
        self.store.append_message(self._message(kind="user_guidance", attempt_id="attempt-old"))
        claimed = self.store.claim_message_deliveries(
            "run-r5", controller=self.controller, consumer_id="serve-a", limit=10
        )
        self.assertEqual(claimed, [])
        delivery = self.store.list_message_deliveries("run-r5")[0]
        self.assertEqual(delivery["status"], "FAILED")
        self.assertEqual(delivery["last_error"], "stale_attempt")

    def test_expired_delivery_is_not_claimed(self) -> None:
        now = datetime.now(timezone.utc)
        message = MessageEnvelope(
            message_id="msg-expired-r5",
            team_id="team-r5",
            run_id="run-r5",
            task_id="task-r5",
            sender_agent_id="hub",
            recipients=(Recipient("agent", "worker-1"),),
            kind="dispatch",
            message_type="dispatch",
            payload={"text": "expired"},
            correlation_id="corr-expired-r5",
            idempotency_key="idem-expired-r5",
            expires_at=(now - timedelta(seconds=1)).isoformat(),
        )
        self.store.append_message(message)
        self.assertEqual(
            self.store.expire_message_deliveries(
                "run-r5", controller=self.controller, now=now.isoformat()
            ),
            1,
        )
        self.assertEqual(
            {item["status"] for item in self.store.list_message_deliveries("run-r5")},
            {"EXPIRED"},
        )

    def test_progress_heartbeat_and_inactivity_are_attempt_fenced(self) -> None:
        self.store.transition_task("task-r5", TaskState.READY, reason="ready")
        self.store.create_attempt("task-r5", "attempt-progress", "worker-1")
        generation = self.store.attempt_generation("attempt-progress")
        heartbeat = self.store.record_progress_heartbeat(
            "attempt-progress",
            generation,
            phase="working",
            progress={"completed": 1},
        )
        self.assertEqual(heartbeat["phase"], "working")
        self.assertEqual(self.store.list_inactive_attempts("run-r5", idle_seconds=3600), [])
        self.store.transition_attempt(
            "attempt-progress", AttemptState.RUNNING, reason="started"
        )
        with self.assertRaises(FencedAttemptError):
            self.store.record_progress_heartbeat(
                "attempt-progress", generation + 1, phase="bad"
            )


class R5CancellationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteStateStore(Path(self.temp.name) / "state.db")
        self.store.create_run("run-cancel-r5", "team-r5")
        self.authority = self.store.acquire_authority(
            "run-cancel-r5", "supervisor-r5", "supervisor", lease_seconds=300
        )
        reconcile_pool_once(
            self.store,
            "run-cancel-r5",
            AgentPoolSpec(
                pool_id="workers", backend="fake", role_id="worker", count=1,
                max_count=1, model="fake-v1",
            ),
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_unconfirmed_cancel_holds_agent_until_stop_confirmation(self) -> None:
        self.store.create_task(
            "run-cancel-r5", "task-cancel-r5", required_role_id="worker",
            prompt="cancel me", cwd="D:/workspace/connect", timeout_seconds=5,
        )
        self.store.transition_task("task-cancel-r5", TaskState.READY, reason="ready")
        controller = self.store.acquire_run_controller(
            "run-cancel-r5", "serve-r5", lease_seconds=300
        )
        claim = self.store.claim_ready_dispatch(
            "run-cancel-r5", controller=controller, authority=self.authority,
        )
        self.assertIsNotNone(claim)
        running = CallSnapshot(
            ref=CallRef(claim.call_id, claim.session.backend, claim.session),
            state=CallState.RUNNING,
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        self.store.mark_backend_call_running(
            claim.call_id, running, reason="started", controller=controller
        )
        self.assertEqual(
            self.store.request_cancel_task(
                "task-cancel-r5", controller=controller, reason="operator-cancel"
            ),
            "cancel_requested",
        )
        delivery = self.store.list_message_deliveries("run-cancel-r5")[0]
        self.assertEqual(delivery["status"], "QUEUED")
        cancelled = CallSnapshot(
            ref=running.ref,
            state=CallState.CANCELLED,
            started_at=running.started_at,
            finished_at=datetime.now(timezone.utc).isoformat(),
            backend_may_still_run=True,
        )
        self.store.finish_backend_call(
            claim.call_id, cancelled, reason="cancel-unconfirmed", controller=controller
        )
        agent = self.store.agent_snapshot(claim.agent_id)
        self.assertEqual(agent["status"], "BUSY")
        self.assertEqual(
            self.store.backend_call_snapshot(claim.call_id)["backend_may_still_run"], 1
        )
        self.assertEqual(
            self.store.confirm_backend_stopped(claim.call_id, controller=controller),
            "confirmed",
        )
        self.assertEqual(self.store.agent_snapshot(claim.agent_id)["status"], "IDLE")
        self.assertEqual(
            self.store.list_message_deliveries("run-cancel-r5")[0]["status"],
            "ACKNOWLEDGED",
        )


class R5BudgetTests(unittest.TestCase):
    def test_schema_and_reservation_are_created_atomically_with_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.db")
            self.assertEqual(CURRENT_SCHEMA_VERSION, 20)
            store.create_run("run-budget-r5", "team-r5")
            store.record_budget("run-budget-r5", max_calls=1)
            authority = store.acquire_authority(
                "run-budget-r5", "supervisor-r5", "supervisor", lease_seconds=300
            )
            reconcile_pool_once(
                store,
                "run-budget-r5",
                AgentPoolSpec(
                    pool_id="workers", backend="fake", role_id="worker", count=1,
                    max_count=1, model="fake-v1",
                ),
            )
            store.create_task(
                "run-budget-r5", "task-budget-r5", required_role_id="worker",
                prompt="budget", cwd="D:/workspace/connect", timeout_seconds=5,
            )
            store.transition_task("task-budget-r5", TaskState.READY, reason="ready")
            controller = store.acquire_run_controller(
                "run-budget-r5", "serve-r5", lease_seconds=300
            )
            claim = store.claim_ready_dispatch(
                "run-budget-r5", controller=controller, authority=authority
            )
            self.assertIsNotNone(claim)
            reservations = store.list_budget_reservations("run-budget-r5")
            self.assertEqual(len(reservations), 1)
            self.assertEqual(reservations[0]["status"], "RESERVED")
            store.close()
