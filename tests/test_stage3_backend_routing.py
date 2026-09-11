from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator.agent_pool import reconcile_pool_once
from orchestrator.core.config import AgentPoolSpec
from orchestrator.core.models import TaskState
from orchestrator.storage.sqlite_store import SQLiteStateStore


class TaskBackendRoutingTests(unittest.TestCase):
    """任务按 required_backend 路由：只绑定同后端的空闲 agent。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "state.db"
        self.store = SQLiteStateStore(self.db)
        self.store.create_run("run-1", "team-1")
        reconcile_pool_once(
            self.store,
            "run-1",
            AgentPoolSpec(
                pool_id="pool-fake", backend="fake", role_id="worker",
                count=2, max_count=2, model="fake-v1",
            ),
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _ready(self, task_id: str, backend: str | None) -> None:
        kwargs = {"required_backend": backend} if backend else {}
        self.store.create_task(
            "run-1", task_id, cwd=str(self.temp.name), prompt=task_id, **kwargs
        )
        self.store.transition_task(task_id, TaskState.READY, reason="ready")

    def _claim(self, run_id: str) -> object | None:
        authority = self.store.acquire_authority(run_id, "op", "supervisor")
        controller = self.store.acquire_run_controller(run_id, "op", lease_seconds=60)
        try:
            return self.store.claim_ready_dispatch(
                run_id, controller=controller, authority=authority, lease_seconds=60
            )
        finally:
            self.store.release_run_controller(controller)

    def test_task_without_backend_claims_any_agent(self) -> None:
        self._ready("task-any", None)
        claim = self._claim("run-1")
        self.assertIsNotNone(claim)

    def test_codex_task_not_routed_to_fake_pool(self) -> None:
        # 只有 fake pool：codex 任务无人可接，claim 返回 None（不会错配）
        self._ready("task-codex", "codex")
        claim = self._claim("run-1")
        self.assertIsNone(claim, "codex 任务不能路由到 fake agent")
        # 任务仍处于 READY，等待 codex pool
        self.assertEqual(self.store.task_state("task-codex"), TaskState.READY)

    def test_fake_task_routes_to_fake_pool(self) -> None:
        self._ready("task-fake", "fake")
        claim = self._claim("run-1")
        self.assertIsNotNone(claim)
        # 绑定到的 agent backend 应为 fake（经 session.backend）
        self.assertEqual(claim.session.backend, "fake")
        bound = self.store.connection.execute(
            "SELECT a.backend FROM attempts att "
            "JOIN agent_instances a ON a.agent_id = att.agent_id "
            "WHERE att.task_id='task-fake'"
        ).fetchone()
        self.assertEqual(bound["backend"], "fake")

    def test_task_model_routes_to_matching_agent_model(self) -> None:
        self.store.create_run("run-2", "team-2")
        reconcile_pool_once(
            self.store,
            "run-2",
            AgentPoolSpec(
                pool_id="pool-codex", backend="codex", role_id="worker",
                count=1, max_count=1, model="gpt-test",
            ),
        )
        self.store.create_task(
            "run-2", "task-model", cwd=str(self.temp.name),
            required_backend="codex", required_model="gpt-test",
        )
        self.store.transition_task("task-model", TaskState.READY, reason="ready")
        authority = self.store.acquire_authority("run-2", "op", "supervisor")
        controller = self.store.acquire_run_controller("run-2", "op", lease_seconds=60)
        try:
            claim = self.store.claim_ready_dispatch(
                "run-2", controller=controller, authority=authority, lease_seconds=60
            )
        finally:
            self.store.release_run_controller(controller)
        self.assertIsNotNone(claim)
        self.assertEqual(claim.session.backend, "codex")
        self.assertEqual(claim.session.model, "gpt-test")

    def test_task_model_does_not_fall_back_to_another_model(self) -> None:
        self.store.create_run("run-2", "team-2")
        reconcile_pool_once(
            self.store,
            "run-2",
            AgentPoolSpec(
                pool_id="pool-codex", backend="codex", role_id="worker",
                count=1, max_count=1, model="gpt-test",
            ),
        )
        self.store.create_task(
            "run-2", "task-model-mismatch", cwd=str(self.temp.name),
            required_backend="codex", required_model="other-model",
        )
        self.store.transition_task(
            "task-model-mismatch", TaskState.READY, reason="ready"
        )
        authority = self.store.acquire_authority("run-2", "op", "supervisor")
        controller = self.store.acquire_run_controller("run-2", "op", lease_seconds=60)
        try:
            claim = self.store.claim_ready_dispatch(
                "run-2", controller=controller, authority=authority, lease_seconds=60
            )
        finally:
            self.store.release_run_controller(controller)
        self.assertIsNone(claim)

    def test_task_provider_routes_to_matching_codebuddy_provider(self) -> None:
        self.store.create_run("run-provider", "team-provider")
        pool = AgentPoolSpec(
            pool_id="codebuddy-volc",
            backend="codebuddy",
            role_id="worker",
            count=1,
            max_count=1,
            model="doubao-seed-code",
            provider_id="volc-codingplan",
        )
        reconcile_pool_once(self.store, "run-provider", pool)
        self.store.create_task(
            "run-provider",
            "task-provider",
            cwd=str(self.temp.name),
            required_backend="codebuddy",
            required_model="doubao-seed-code",
            required_provider_id="volc-codingplan",
        )
        self.store.transition_task("task-provider", TaskState.READY, reason="ready")
        authority = self.store.acquire_authority(
            "run-provider", "op", "supervisor"
        )
        controller = self.store.acquire_run_controller(
            "run-provider", "op", lease_seconds=60
        )
        try:
            claim = self.store.claim_ready_dispatch(
                "run-provider", controller=controller, authority=authority, lease_seconds=60
            )
        finally:
            self.store.release_run_controller(controller)
        self.assertIsNotNone(claim)
        self.assertEqual(claim.session.provider_id, "volc-codingplan")


if __name__ == "__main__":
    unittest.main()
