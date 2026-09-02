from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator.storage.sqlite_store import SQLiteStateStore


class OutboxRetryTests(unittest.IsolatedAsyncioTestCase):
    """B3（P1-06）：Outbox 持久 claim、退避重试、死信。"""

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteStateStore(Path(self.temp.name) / "state.db")
        self.store.create_run("run-1", "team-1")
        self.authority = self.store.acquire_authority(
            "run-1", "test-supervisor", "supervisor"
        )
        self.controller = self.store.acquire_run_controller(
            "run-1", "op", lease_seconds=60
        )
        self.outbox_id = self.store.record_outbox_intent(
            "run-1", "merge", "merge-1", "merge.applied", {"commit": "x"},
            self.controller,
        )

    async def asyncTearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _row(self) -> dict[str, object]:
        return dict(
            self.store.connection.execute(
                "SELECT status, attempts, available_at FROM outbox "
                "WHERE outbox_id=?",
                (self.outbox_id,),
            ).fetchone()
        )

    def _expire_backoff(self) -> None:
        # 模拟时间流逝：让退避到期，便于再次 claim
        self.store.connection.execute(
            "UPDATE outbox SET available_at='2000-01-01T00:00:00+00:00' "
            "WHERE outbox_id=?",
            (self.outbox_id,),
        )
        self.store.connection.commit()

    def test_failed_delivery_schedules_backoff_retry(self) -> None:
        claim = self.store.claim_outbox("run-1", self.controller)
        self.assertIsNotNone(claim)
        self.assertEqual(claim["attempts"], 1)
        self.store.finish_outbox(self.outbox_id, "failed", self.controller)
        row = self._row()
        self.assertEqual(row["status"], "PENDING", "失败应保持 PENDING 等待重试")
        self.assertGreater(str(row["available_at"]), "2026-01-01")
        # 退避未到期前不可再 claim
        self.assertIsNone(self.store.claim_outbox("run-1", self.controller))

    def test_dead_letter_after_max_attempts(self) -> None:
        self.store.MAX_OUTBOX_ATTEMPTS = 3
        for _ in range(3):
            self._expire_backoff()
            claim = self.store.claim_outbox("run-1", self.controller)
            self.assertIsNotNone(claim)
            self.store.finish_outbox(self.outbox_id, "failed", self.controller)
        row = self._row()
        self.assertEqual(row["status"], "FAILED", "超过最大尝试次数进入死信")
        self.assertEqual(row["attempts"], 3)
        self.assertIsNone(self.store.claim_outbox("run-1", self.controller))
        dead_letter = self.store.connection.execute(
            "SELECT kind FROM events WHERE kind='outbox.dead_letter'"
        ).fetchone()
        self.assertIsNotNone(dead_letter)

    def test_sent_marks_delivered(self) -> None:
        self.store.claim_outbox("run-1", self.controller)
        self.store.finish_outbox(self.outbox_id, "sent", self.controller)
        row = self._row()
        self.assertEqual(row["status"], "SENT")


if __name__ == "__main__":
    unittest.main()
