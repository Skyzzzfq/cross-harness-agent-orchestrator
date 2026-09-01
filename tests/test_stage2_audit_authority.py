from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator.agent_pool import reconcile_pool_once
from orchestrator.core.config import AgentPoolSpec
from orchestrator.core.models import TaskState
from orchestrator.storage.sqlite_store import (
    FencedAuthorityError,
    SQLiteStateStore,
)


class AuthorityDispatchFencingTests(unittest.IsolatedAsyncioTestCase):
    """P0-01：旧 authority epoch 对派发/集成必须零副作用。"""

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteStateStore(Path(self.temp.name) / "state.db")
        self.store.create_run("run-1", "team-1")
        self.authority = self.store.acquire_authority(
            "run-1", "supervisor-a", "supervisor"
        )
        # 经 handoff 切到 supervisor-b，supervisor-a 的旧 token 失效
        request = self.store.request_authority_handoff(
            "run-1", self.authority, "supervisor-b", reason="rotate"
        )
        self.store.accept_authority_handoff(
            "run-1", request["request_id"], "supervisor-b"
        )
        self.current = self.store.commit_authority_handoff(
            "run-1", request["request_id"], "supervisor-b"
        )
        self.controller = self.store.acquire_run_controller(
            "run-1", "op", lease_seconds=60
        )

    async def asyncTearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _ready_task(self, task_id: str = "task-1") -> None:
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
        self.store.create_task(
            "run-1",
            task_id,
            required_role_id="worker",
            prompt="x",
            cwd=".",
            timeout_seconds=5,
        )
        self.store.transition_task(task_id, TaskState.READY, reason="ready")

    async def test_stale_authority_cannot_dispatch(self) -> None:
        self._ready_task()
        with self.assertRaises(FencedAuthorityError):
            self.store.claim_ready_dispatch(
                "run-1",
                controller=self.controller,
                authority=self.authority,  # 旧 epoch
                lease_seconds=60,
            )
        # 零副作用：不新增 attempt
        attempts = self.store.connection.execute(
            "SELECT COUNT(*) FROM attempts WHERE task_id='task-1'"
        ).fetchone()[0]
        self.assertEqual(attempts, 0)

    async def test_stale_authority_cannot_enqueue_merge(self) -> None:
        self._ready_task()
        self.store.transition_task("task-1", TaskState.ACTIVE, reason="dispatch")
        self.store.transition_task("task-1", TaskState.REVIEW, reason="submitted")
        with self.assertRaises(FencedAuthorityError):
            self.store.enqueue_merge(
                "run-1",
                "task-1",
                "attempt-1",
                "abc123",
                "base0",
                self.controller,
                authority=self.authority,  # 旧 epoch
                reason="review-passed",
            )
        merges = self.store.connection.execute(
            "SELECT COUNT(*) FROM merge_queue WHERE run_id='run-1'"
        ).fetchone()[0]
        self.assertEqual(merges, 0)

    async def test_stale_authority_cannot_claim_merge(self) -> None:
        self.store.create_task("run-1", "task-1")
        self.store.enqueue_merge(
            "run-1",
            "task-1",
            "attempt-1",
            "abc123",
            "base0",
            self.controller,
            authority=self.current,
            reason="review-passed",
        )
        with self.assertRaises(FencedAuthorityError):
            self.store.claim_merge_queue(
                "run-1", self.controller, authority=self.authority  # 旧 epoch
            )
        row = self.store.connection.execute(
            "SELECT status FROM merge_queue WHERE run_id='run-1'"
        ).fetchone()
        self.assertEqual(row["status"], "PENDING")  # 未被领取

    async def test_stale_authority_cannot_finish_merge(self) -> None:
        self.store.create_task("run-1", "task-1")
        self.store.transition_task("task-1", TaskState.READY, reason="ready")
        self.store.transition_task("task-1", TaskState.ACTIVE, reason="dispatch")
        self.store.transition_task("task-1", TaskState.REVIEW, reason="submitted")
        self.store.enqueue_merge(
            "run-1",
            "task-1",
            "attempt-1",
            "abc123",
            "base0",
            self.controller,
            authority=self.current,
            reason="review-passed",
        )
        claim = self.store.claim_merge_queue(
            "run-1", self.controller, authority=self.current
        )
        self.assertIsNotNone(claim)
        with self.assertRaises(FencedAuthorityError):
            self.store.finish_merge(
                claim["merge_id"],
                "applied",
                self.controller,
                authority=self.authority,  # 旧 epoch
                result_commit="abc123",
            )
        self.assertEqual(self.store.task_state("task-1"), TaskState.REVIEW)
        row = self.store.connection.execute(
            "SELECT status FROM merge_queue WHERE merge_id=?", (claim["merge_id"],)
        ).fetchone()
        self.assertEqual(row["status"], "APPLYING")  # 未被推进

    async def test_current_authority_can_dispatch_after_handoff(self) -> None:
        """新主管可以派发——fencing 不误伤当前持有者。"""
        self._ready_task()
        claim = self.store.claim_ready_dispatch(
            "run-1",
            controller=self.controller,
            authority=self.current,
            lease_seconds=60,
        )
        self.assertIsNotNone(claim)
        self.assertEqual(self.store.task_state("task-1"), TaskState.ACTIVE)


class AuthorityTakeoverTests(unittest.IsolatedAsyncioTestCase):
    """P0-01：ACTIVE authority 不得被普通 acquire 无条件覆盖；接管走人工审批。"""

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteStateStore(Path(self.temp.name) / "state.db")
        self.store.create_run("run-1", "team-1")

    async def asyncTearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    async def test_acquire_cannot_overwrite_active_authority(self) -> None:
        first = self.store.acquire_authority("run-1", "supervisor-a", "supervisor")
        with self.assertRaises(FencedAuthorityError):
            self.store.acquire_authority("run-1", "supervisor-b", "supervisor")
        # owner 未被覆盖
        self.assertEqual(
            self.store.active_authority("run-1")["owner_agent_id"], "supervisor-a"
        )
        self.assertEqual(first.epoch, 1)

    async def test_acquire_allowed_when_previous_lease_expired(self) -> None:
        self.store.acquire_authority(
            "run-1", "supervisor-a", "supervisor", lease_seconds=1
        )
        # 等租约过期（不依赖真实等待：直接推进时间不可行，改为用过去时间戳的旧 lease）
        # 直接构造一个已过期的 ACTIVE lease
        self.store.connection.execute(
            """
            UPDATE authority_leases
            SET expires_at = '2020-01-01T00:00:00+00:00'
            WHERE run_id = 'run-1'
            """
        )
        self.store.connection.commit()
        second = self.store.acquire_authority("run-1", "supervisor-b", "supervisor")
        self.assertEqual(second.epoch, 2)
        self.assertEqual(
            self.store.active_authority("run-1")["owner_agent_id"], "supervisor-b"
        )

    async def test_force_takeover_requires_approved_approval(self) -> None:
        first = self.store.acquire_authority("run-1", "supervisor-a", "supervisor")
        # 没有已批准的人工审批 → 拒绝
        with self.assertRaises(RuntimeError):
            self.store.force_takeover_authority(
                "run-1",
                "supervisor-b",
                "supervisor",
                requested_by="human-admin",
                approval_request_id="approval-nonexistent",
            )
        self.assertEqual(
            self.store.active_authority("run-1")["owner_agent_id"], "supervisor-a"
        )

    async def test_force_takeover_commits_and_fences_old_epoch(self) -> None:
        first = self.store.acquire_authority("run-1", "supervisor-a", "supervisor")
        self.store.create_task("run-1", "task-takeover")
        request_id = self.store.create_approval_request(
            "run-1",
            task_id="task-takeover",
            action_summary="force authority takeover",
            params={"new_owner": "supervisor-b"},
            requested_by="human-admin",
            scope="supervisor",
            single_use=True,
        )
        self.store.decide_approval(
            request_id, "APPROVED", decided_by="human-admin", comment="approved"
        )
        new_token = self.store.force_takeover_authority(
            "run-1",
            "supervisor-b",
            "supervisor",
            requested_by="human-admin",
            approval_request_id=request_id,
        )
        self.assertEqual(new_token.epoch, first.epoch + 1)
        self.assertEqual(
            self.store.active_authority("run-1")["owner_agent_id"], "supervisor-b"
        )
        # 审批被消费（single-use → USED）
        row = self.store.connection.execute(
            "SELECT status FROM approval_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        self.assertEqual(row["status"], "USED")
        # 旧 token 失效
        with self.assertRaises(FencedAuthorityError):
            self.store.renew_authority(first)
        # 审计事件存在
        kinds = [
            event["kind"]
            for event in self.store.events()
            if event["kind"] == "authority.takeover_forced"
        ]
        self.assertTrue(kinds)

    async def test_force_takeover_rejects_wrong_approval_scope(self) -> None:
        self.store.acquire_authority("run-1", "supervisor-a", "supervisor")
        self.store.create_task("run-1", "task-takeover")
        request_id = self.store.create_approval_request(
            "run-1",
            task_id="task-takeover",
            action_summary="deploy something",
            params={"env": "prod"},
            requested_by="human-admin",
            scope="deploy",  # 与 takeover 不匹配
            single_use=True,
        )
        self.store.decide_approval(
            request_id, "APPROVED", decided_by="human-admin", comment="ok"
        )
        with self.assertRaises(RuntimeError):
            self.store.force_takeover_authority(
                "run-1",
                "supervisor-b",
                "supervisor",
                requested_by="human-admin",
                approval_request_id=request_id,
            )


if __name__ == "__main__":
    unittest.main()
