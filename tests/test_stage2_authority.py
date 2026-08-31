from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator.core.models import TaskState
from orchestrator.agent_pool import reconcile_pool_once
from orchestrator.core.config import AgentPoolSpec
from orchestrator.storage.sqlite_store import (
    FencedAuthorityError,
    SQLiteStateStore,
)


class AuthorityLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteStateStore(Path(self.temp.name) / "state.db")
        self.store.create_run("run-1", "team-1")

    async def asyncTearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    async def test_acquire_authority_starts_epoch_at_one(self) -> None:
        token = self.store.acquire_authority(
            "run-1", "codex-supervisor-01", "supervisor"
        )
        self.assertEqual(token.epoch, 1)
        self.assertEqual(token.owner_agent_id, "codex-supervisor-01")
        self.assertEqual(self.store.active_authority("run-1")["epoch"], 1)

    async def test_acquire_authority_while_active_increments_epoch(self) -> None:
        first = self.store.acquire_authority("run-1", "codex-supervisor-01", "supervisor")
        self.assertEqual(first.epoch, 1)
        # 主管权转移：新 owner 接管时 epoch 递增，旧 token 失效
        second = self.store.acquire_authority("run-1", "cb-supervisor-01", "supervisor")
        self.assertEqual(second.epoch, 2)
        self.assertEqual(self.store.active_authority("run-1")["owner_agent_id"], "cb-supervisor-01")
        with self.assertRaises(FencedAuthorityError):
            self.store.renew_authority(first)

    async def test_handoff_commits_atomically_and_old_epoch_is_fenced(self) -> None:
        token = self.store.acquire_authority("run-1", "codex-supervisor-01", "supervisor")
        request = self.store.request_authority_handoff(
            "run-1", token, "cb-supervisor-01", reason="checkpoint-rotation"
        )
        self.store.accept_authority_handoff(
            "run-1", request["request_id"], "cb-supervisor-01"
        )
        new_token = self.store.commit_authority_handoff(
            "run-1", request["request_id"], "cb-supervisor-01"
        )
        self.assertEqual(new_token.epoch, 2)
        self.assertEqual(
            self.store.active_authority("run-1")["owner_agent_id"], "cb-supervisor-01"
        )
        # 旧 epoch 的任何主管动作被拒
        with self.assertRaises(FencedAuthorityError):
            self.store.renew_authority(token)

    async def test_handoff_rejected_when_merge_is_active(self) -> None:
        token = self.store.acquire_authority("run-1", "codex-supervisor-01", "supervisor")
        # 制造活动 merge（APPLYING）
        self.store.create_task("run-1", "task-1")
        controller = self.store.acquire_run_controller("run-1", "op", lease_seconds=60)
        self.store.enqueue_merge(
            "run-1", "task-1", "attempt-1", "abc123", "base0", controller,
            reason="review-passed",
        )
        self.store.claim_merge_queue("run-1", controller)
        with self.assertRaises(RuntimeError):
            self.store.request_authority_handoff(
                "run-1", token, "cb-supervisor-01", reason="mid-merge"
            )


class ApprovalRequestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteStateStore(Path(self.temp.name) / "state.db")
        self.store.create_run("run-1", "team-1")

    async def asyncTearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _authority(self) -> object:
        return self.store.acquire_authority("run-1", "codex-supervisor-01", "supervisor")

    async def test_create_and_approve_request(self) -> None:
        authority = self._authority()
        self.store.create_task("run-1", "task-1")
        request_id = self.store.create_approval_request(
            "run-1",
            task_id="task-1",
            action_summary="push to production",
            params={"target": "prod"},
            requested_by=authority.owner_agent_id,
            scope="deploy",
            single_use=True,
            expires_at=None,
        )
        self.store.decide_approval(
            request_id, "APPROVED", decided_by="human-admin", comment="ok"
        )
        row = self.store.connection.execute(
            "SELECT status FROM approval_requests WHERE request_id=?",
            (request_id,),
        ).fetchone()
        self.assertEqual(row["status"], "APPROVED")

    async def test_single_use_request_cannot_be_decided_twice(self) -> None:
        authority = self._authority()
        self.store.create_task("run-1", "task-1")
        request_id = self.store.create_approval_request(
            "run-1",
            task_id="task-1",
            action_summary="deploy",
            params={},
            requested_by=authority.owner_agent_id,
            scope="deploy",
            single_use=True,
            expires_at=None,
        )
        self.store.decide_approval(request_id, "APPROVED", decided_by="human-admin")
        with self.assertRaises(RuntimeError):
            self.store.decide_approval(request_id, "APPROVED", decided_by="human-admin")


class BudgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteStateStore(Path(self.temp.name) / "state.db")
        self.store.create_run("run-1", "team-1")

    async def asyncTearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    async def test_record_budget_and_status(self) -> None:
        self.store.record_budget("run-1", max_calls=3, max_tasks=5, max_run_seconds=3600)
        self.store.create_task("run-1", "task-1")
        self.store.create_task("run-1", "task-2")
        status = self.store.budget_status("run-1")
        self.assertFalse(status["exceeded"])
        # 达到任务数上限
        self.store.record_budget("run-1", max_tasks=2)
        self.store.create_task("run-1", "task-3")
        exceeded = self.store.budget_status("run-1")
        self.assertTrue(exceeded["exceeded"])
        self.assertEqual(exceeded["tasks"], 3)


class BudgetDispatchTests(unittest.IsolatedAsyncioTestCase):
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
        self.store.create_task("run-1", "task-1")
        self.store.transition_task("task-1", TaskState.READY, reason="ready")

    async def asyncTearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _controller(self) -> object:
        return self.store.acquire_run_controller("run-1", "op", lease_seconds=60)

    async def test_claim_stops_when_budget_exceeded(self) -> None:
        token = self._controller()
        self.store.record_budget("run-1", max_tasks=0)
        claim = self.store.claim_ready_dispatch("run-1", controller=token, lease_seconds=60)
        self.assertIsNone(claim)
        # 提高预算后恢复派发
        self.store.record_budget("run-1", max_tasks=10)
        claim = self.store.claim_ready_dispatch("run-1", controller=token, lease_seconds=60)
        self.assertIsNotNone(claim)

    async def test_claim_normal_without_budget(self) -> None:
        token = self._controller()
        claim = self.store.claim_ready_dispatch("run-1", controller=token, lease_seconds=60)
        self.assertIsNotNone(claim)


class ReviewLayerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteStateStore(Path(self.temp.name) / "state.db")
        self.store.create_run("run-1", "team-1")
        self.store.create_task("run-1", "task-1")

    async def asyncTearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _authority(self) -> object:
        return self.store.acquire_authority("run-1", "codex-supervisor-01", "supervisor")

    async def test_record_review_decision_three_layers(self) -> None:
        authority = self._authority()
        self.store.record_review_decision(
            "run-1", "task-1", attempt_id="attempt-1", layer="deterministic",
            decision="PASS", decided_by="verify-script", detail={"tests": 12},
            authority=authority,
        )
        self.store.record_review_decision(
            "run-1", "task-1", attempt_id="attempt-1", layer="model",
            decision="PASS", decided_by="codex-reviewer-01", detail={},
            authority=authority,
        )
        self.store.record_review_decision(
            "run-1", "task-1", attempt_id="attempt-1", layer="human",
            decision="APPROVED", decided_by="human-admin", detail={},
            authority=authority,
        )
        rows = self.store.connection.execute(
            "SELECT layer, decision FROM review_decisions WHERE task_id='task-1'"
        ).fetchall()
        self.assertEqual(len(rows), 3)
        layers = {row["layer"] for row in rows}
        self.assertEqual(layers, {"deterministic", "model", "human"})

    async def test_review_decision_fenced_by_stale_authority(self) -> None:
        authority = self._authority()
        second = self.store.acquire_authority("run-1", "cb-supervisor-01", "supervisor")
        with self.assertRaises(Exception):
            self.store.record_review_decision(
                "run-1", "task-1", attempt_id="attempt-1", layer="human",
                decision="APPROVED", decided_by="admin", detail={},
                authority=authority,
            )


class ApprovalListTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteStateStore(Path(self.temp.name) / "state.db")
        self.store.create_run("run-1", "team-1")
        self.store.create_task("run-1", "task-1")

    async def asyncTearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _authority(self) -> object:
        return self.store.acquire_authority("run-1", "codex-supervisor-01", "supervisor")

    async def test_list_pending_approval_requests(self) -> None:
        authority = self._authority()
        self.store.create_approval_request(
            "run-1", task_id="task-1", action_summary="deploy",
            params={}, requested_by="supervisor", scope="deploy", single_use=True,
        )
        pending = self.store.list_approval_requests("run-1", status="PENDING")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["action_summary"], "deploy")
        self.store.decide_approval(
            pending[0]["request_id"], "APPROVED", decided_by="human-admin"
        )
        remaining = self.store.list_approval_requests("run-1", status="PENDING")
        self.assertEqual(len(remaining), 0)


if __name__ == "__main__":
    unittest.main()
