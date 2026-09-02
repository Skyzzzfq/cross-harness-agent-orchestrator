from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator.adapters.fake import FakeBackendAdapter, FakeBehavior
from orchestrator.agent_pool import reconcile_pool_once
from orchestrator.core.config import AgentPoolSpec
from orchestrator.core.models import TaskState
from orchestrator.poc.stage3_scenarios import (
    SCENARIO_BY_ID,
    STAGE3_FROZEN_SCENARIOS,
    FrozenScenario,
)
from orchestrator.storage.sqlite_store import SQLiteStateStore
from orchestrator.workspace.git_manager import GitWorkspaceManager
from orchestrator.workspace.merge_executor import MergeExecutor
from orchestrator.workspace.policy import WorkspacePolicy


class FrozenScenarioCatalogTests(unittest.TestCase):
    """T2：预冻结清单完整性。"""

    def test_catalog_has_exactly_twenty_scenarios(self) -> None:
        self.assertEqual(len(STAGE3_FROZEN_SCENARIOS), 20)
        self.assertEqual(len(SCENARIO_BY_ID), 20)

    def test_scenario_ids_are_unique_and_kind_valid(self) -> None:
        kinds = {
            "read_marker",
            "write_pipeline",
            "parallel_write",
            "boundary_injection",
            "boundary_read_scope",
            "conflict",
            "cancel",
            "dependency",
            "recovery",
        }
        seen: set[str] = set()
        for scenario in STAGE3_FROZEN_SCENARIOS:
            self.assertNotIn(scenario.scenario_id, seen)
            seen.add(scenario.scenario_id)
            self.assertIn(scenario.backend, {"codex", "codebuddy"})
            self.assertIn(scenario.kind, kinds)

    def test_write_scenarios_declare_scope_and_expected_completed(self) -> None:
        for scenario in STAGE3_FROZEN_SCENARIOS:
            if scenario.kind in {"write_pipeline", "parallel_write", "conflict",
                                 "dependency", "recovery"}:
                self.assertTrue(scenario.write_scope, scenario.scenario_id)
            if scenario.kind in {"write_pipeline", "parallel_write", "dependency",
                                 "recovery"}:
                self.assertEqual(scenario.expected_state, "COMPLETED", scenario.scenario_id)


class ScenarioDriverTests(unittest.IsolatedAsyncioTestCase):
    """T2：框架能驱动各类场景到正确终态（Fake 验证）。"""

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.manager = GitWorkspaceManager(root / "repo", root / "worktrees")
        self.base = self.manager.initialize_repository()
        self.worker = self.manager.create_worktree("worker", self.base)
        self.policy = WorkspacePolicy(
            project_root=root / "repo",
            worktrees_root=root / "worktrees",
            managed_worktrees=(self.worker,),
        )
        self.store = SQLiteStateStore(root / "state.db", workspace_policy=self.policy)
        self.store.create_run("run-1", "cross-harness-poc")
        self.authority = self.store.acquire_authority(
            "run-1", "test-supervisor", "supervisor"
        )
        self.controller = self.store.acquire_run_controller(
            "run-1", "op", lease_seconds=60
        )
        reconcile_pool_once(
            self.store,
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
        self.executor = MergeExecutor(self.store, self.manager)

    async def asyncTearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _scenario(self, scenario_id: str) -> FrozenScenario:
        return SCENARIO_BY_ID[scenario_id]

    def _create_task(self, scenario: FrozenScenario, task_id: str) -> None:
        self.store.create_task(
            "run-1",
            task_id,
            access_mode=scenario.access_mode,
            write_scope=scenario.write_scope,
            required_role_id="worker",
            prompt=scenario.prompt,
            cwd=str(self.worker),
            timeout_seconds=5,
        )
        self.store.transition_task(task_id, TaskState.READY, reason="frozen-scenario")

    async def _dispatch(self, adapter: FakeBackendAdapter) -> None:
        from orchestrator.scheduler import scheduler_tick

        await scheduler_tick(
            self.store,
            run_id="run-1",
            adapters={"fake": adapter},
            authority=self.authority,
            controller=self.controller,
            lease_seconds=60,
        )

    async def test_write_pipeline_scenario_reaches_completed(self) -> None:
        scenario = self._scenario("s11-codex-write-commit")
        self._create_task(scenario, "task-11")
        await self._dispatch(
            FakeBackendAdapter(default_behavior=FakeBehavior(delay_seconds=0, text="done"))
        )
        self.assertEqual(self.store.task_state("task-11"), TaskState.REVIEW)

        # 三层审核
        attempt = self.store.connection.execute(
            "SELECT attempt_id FROM attempts WHERE task_id='task-11' LIMIT 1"
        ).fetchone()
        assert attempt is not None
        for layer, decision, by in (
            ("deterministic", "PASS", "verify-script"),
            ("model", "PASS", "codex-reviewer"),
            ("human", "APPROVED", "human-admin"),
        ):
            self.store.record_review_decision(
                "run-1", "task-11", attempt_id=str(attempt["attempt_id"]),
                layer=layer, decision=decision, decided_by=by, detail={},
                authority=self.authority,
            )
        # worker 产出 + 入队 + 集成
        commit = self.manager.commit_file(
            self.worker, "demo/s11.txt", "RESULT_S11\n", "worker: s11"
        )
        self.store.enqueue_merge(
            "run-1", "task-11", str(attempt["attempt_id"]), commit, self.base,
            self.controller, authority=self.authority, reason="review-passed",
        )
        result = self.executor.run_merge_once("run-1", self.controller, self.authority)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(self.store.task_state("task-11"), TaskState.COMPLETED)
        reviews = self.store.connection.execute(
            "SELECT COUNT(*) FROM review_decisions WHERE task_id='task-11'"
        ).fetchone()[0]
        self.assertEqual(reviews, 3)

    async def test_boundary_read_scope_is_rejected(self) -> None:
        scenario = self._scenario("s16-read-with-scope")
        # 只读场景：默认 access_mode=read_only；若给只读任务声明 write_scope 应被拒
        with self.assertRaises(ValueError):
            self.store.create_task(
                "run-1", "task-16", access_mode="read_only",
                write_scope=("demo/s16.txt",), prompt=scenario.prompt,
                cwd=str(self.worker),
            )

    async def test_parallel_write_scenarios_both_complete(self) -> None:
        a = self._scenario("s13-parallel-write")
        b = self._scenario("s14-parallel-write-2")
        self._create_task(a, "task-13")
        self._create_task(b, "task-14")
        await self._dispatch(
            FakeBackendAdapter(default_behavior=FakeBehavior(delay_seconds=0, text="done"))
        )
        self.assertEqual(self.store.task_state("task-13"), TaskState.REVIEW)
        self.assertEqual(self.store.task_state("task-14"), TaskState.REVIEW)
        for task_id, path, content in (
            ("task-13", "demo/s13a.txt", "RESULT_S13_A\n"),
            ("task-14", "demo/s13b.txt", "RESULT_S13_B\n"),
        ):
            commit = self.manager.commit_file(
                self.worker, path, content, f"worker: {task_id}"
            )
            attempt = self.store.connection.execute(
                "SELECT attempt_id FROM attempts WHERE task_id=? LIMIT 1",
                (task_id,),
            ).fetchone()
            assert attempt is not None
            self.store.enqueue_merge(
                "run-1", task_id, str(attempt["attempt_id"]), commit, self.base,
                self.controller, authority=self.authority, reason="review-passed",
            )
            self.assertEqual(
                self.executor.run_merge_once("run-1", self.controller, self.authority)[
                    "status"
                ],
                "applied",
            )
            self.assertEqual(self.store.task_state(task_id), TaskState.COMPLETED)


if __name__ == "__main__":
    unittest.main()
