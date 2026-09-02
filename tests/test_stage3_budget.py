from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.agent_pool import reconcile_pool_once
from orchestrator.core.config import AgentPoolSpec
from orchestrator.core.models import TaskState
from orchestrator.storage.sqlite_store import SQLiteStateStore


class BudgetHardLimitTests(unittest.TestCase):
    """B1（P1-02）：turn / Token / 金额硬预算。

    用权威 usage_json 聚合 turns / cost / tokens，任一达到上限即停止派发。
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = SQLiteStateStore(root / "state.db")
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

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _seed_call_with_usage(self, usage: dict[str, object]) -> None:
        self.store.create_task("run-1", "task-1", cwd=str(self.temp.name))
        agent_id = self.store.connection.execute(
            "SELECT agent_id FROM agent_instances LIMIT 1"
        ).fetchone()["agent_id"]
        self.store.connection.execute(
            "INSERT INTO attempts(attempt_id, task_id, agent_id, state, "
            "attempt_number, created_at, updated_at) VALUES(?, ?, ?, 'SUBMITTED', 1, "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
            ("attempt-1", "task-1", agent_id),
        )
        session = self.store.connection.execute(
            "SELECT session_ref_id FROM backend_sessions "
            "WHERE run_id='run-1' AND agent_id=? LIMIT 1",
            (agent_id,),
        ).fetchone()
        if session is None:
            self.store.connection.execute(
                "INSERT INTO backend_sessions(session_ref_id, run_id, agent_id, "
                "backend, state, cwd, created_at, updated_at) VALUES(?, 'run-1', ?, "
                "'fake', 'IDLE', ?, '2026-01-01T00:00:00+00:00', "
                "'2026-01-01T00:00:00+00:00')",
                ("sess-1", agent_id, str(self.temp.name)),
            )
            session_ref = "sess-1"
        else:
            session_ref = str(session["session_ref_id"])
        self.store.connection.execute(
            "INSERT INTO backend_calls(call_id, run_id, task_id, attempt_id, "
            "generation, agent_id, session_ref_id, backend, state, request_digest, "
            "requested_at, usage_json) VALUES(?, 'run-1', 'task-1', 'attempt-1', 1, "
            "?, ?, 'fake', 'succeeded', 'digest', "
            "'2026-01-01T00:00:00+00:00', ?)",
            ("call-1", agent_id, session_ref, json.dumps(usage)),
        )
        self.store.connection.commit()

    def test_status_aggregates_turns_tokens_cost(self) -> None:
        self.store.record_budget("run-1", max_turns=100, max_cost_decimal="5.00")
        self._seed_call_with_usage(
            {
                "turns": 3,
                "input_tokens": 100,
                "output_tokens": 50,
                "cost_decimal": "0.25",
            }
        )
        status = self.store.budget_status("run-1")
        self.assertEqual(status["turns"], 3)
        self.assertEqual(status["tokens"], 150)
        self.assertEqual(status["cost"], 0.25)
        self.assertFalse(status["exceeded"])

    def test_turns_limit_exceeds_and_stops_dispatch(self) -> None:
        self.store.record_budget("run-1", max_turns=2)
        self._seed_call_with_usage({"turns": 2, "cost_decimal": "0.1"})
        status = self.store.budget_status("run-1")
        self.assertTrue(status["exceeded"], "turns 达到上限必须 exceeded")

    def test_cost_limit_exceeds(self) -> None:
        self.store.record_budget("run-1", max_cost_decimal="1.00")
        self._seed_call_with_usage({"turns": 1, "cost_decimal": "1.50"})
        self.assertTrue(self.store.budget_status("run-1")["exceeded"])

    def test_no_budget_means_never_exceeded(self) -> None:
        status = self.store.budget_status("run-1")
        self.assertFalse(status["exceeded"])
        self.assertEqual(status["turns"], 0)
        self.assertEqual(status["tokens"], 0)
        self.assertEqual(status["cost"], 0)


if __name__ == "__main__":
    unittest.main()
