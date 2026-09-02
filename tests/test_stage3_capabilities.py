from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from orchestrator.adapters.contracts import BackendCapabilities
from orchestrator.adapters.fake import FakeBackendAdapter, FakeBehavior
from orchestrator.agent_pool import reconcile_pool_once
from orchestrator.core.config import AgentPoolSpec
from orchestrator.core.models import TaskState
from orchestrator.storage.sqlite_store import SQLiteStateStore


class ReadOnlyFakeAdapter(FakeBackendAdapter):
    """不支持写任务的后端（能力协商测试用）。"""

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            backend="fake", version="fake-readonly", supports_write=False
        )


class CapabilitiesContractTests(unittest.TestCase):
    """T4：能力声明与版本探测。"""

    def test_fake_declares_full_capabilities(self) -> None:
        caps = FakeBackendAdapter().capabilities().to_dict()
        self.assertEqual(caps["backend"], "fake")
        self.assertTrue(caps["supports_write"])
        self.assertTrue(caps["supports_cancel"])

    def test_codex_declares_write_and_cancel(self) -> None:
        from orchestrator.adapters.real import CodexBackendAdapter

        caps = CodexBackendAdapter().capabilities()
        self.assertEqual(caps.backend, "codex")
        self.assertTrue(caps.supports_write)  # Sandbox.workspace_write
        self.assertTrue(caps.supports_cancel)  # thread.interrupt()
        self.assertIsInstance(caps.version, (str, type(None)))

    def test_codebuddy_declares_no_hard_cancel(self) -> None:
        from orchestrator.adapters.real import CodeBuddyBackendAdapter

        caps = CodeBuddyBackendAdapter().capabilities()
        self.assertEqual(caps.backend, "codebuddy")
        self.assertTrue(caps.supports_write)
        self.assertFalse(caps.supports_cancel)  # 无硬中断：cancel_unconfirmed


class CapabilityNegotiationTests(unittest.IsolatedAsyncioTestCase):
    """T4：scheduler 派发前按能力协商。"""

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = SQLiteStateStore(root / "state.db")
        self.store.create_run("run-1", "team-1")
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
                count=2,
                max_count=2,
                model="fake-v1",
            ),
        )

    async def asyncTearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    async def _tick(self, adapter: FakeBackendAdapter) -> None:
        from orchestrator.scheduler import scheduler_tick

        await scheduler_tick(
            self.store,
            run_id="run-1",
            adapters={"fake": adapter},
            authority=self.authority,
            controller=self.controller,
            lease_seconds=60,
        )

    async def test_write_task_is_blocked_when_backend_readonly(self) -> None:
        self.store.create_task(
            "run-1",
            "task-w",
            access_mode="write",
            write_scope=("demo/a.txt",),
            prompt="write demo/a.txt",
            cwd=str(self.temp.name),
        )
        self.store.transition_task("task-w", TaskState.READY, reason="ready")
        await self._tick(ReadOnlyFakeAdapter())
        # 拒绝语义：写任务绝不能被"完成"或进入 REVIEW；调用 BLOCKED（capability_unsupported）
        state = self.store.task_state("task-w")
        self.assertNotIn(state, {TaskState.REVIEW, TaskState.COMPLETED})
        call = self.store.connection.execute(
            "SELECT state, failure_json FROM backend_calls WHERE task_id='task-w'"
        ).fetchone()
        self.assertIsNotNone(call)
        self.assertEqual(call["state"], "blocked")
        self.assertIn("capability_unsupported", call["failure_json"])

    async def test_read_task_is_dispatched_by_readonly_backend(self) -> None:
        self.store.create_task(
            "run-1",
            "task-r",
            prompt="read only",
            cwd=str(self.temp.name),
        )
        self.store.transition_task("task-r", TaskState.READY, reason="ready")
        await self._tick(ReadOnlyFakeAdapter())
        self.assertEqual(self.store.task_state("task-r"), TaskState.REVIEW)

    async def test_write_task_dispatched_by_capable_backend(self) -> None:
        self.store.create_task(
            "run-1",
            "task-w2",
            access_mode="write",
            write_scope=("demo/b.txt",),
            prompt="write demo/b.txt",
            cwd=str(self.temp.name),
        )
        self.store.transition_task("task-w2", TaskState.READY, reason="ready")
        await self._tick(
            FakeBackendAdapter(
                default_behavior=FakeBehavior(delay_seconds=0, text="done")
            )
        )
        self.assertEqual(self.store.task_state("task-w2"), TaskState.REVIEW)


class FeatureFlagTests(unittest.TestCase):
    """T4：运行时功能开关。"""

    def setUp(self) -> None:
        self._old = os.environ.get("AGENT_HUB_FEATURES")
        os.environ.pop("AGENT_HUB_FEATURES", None)

    def tearDown(self) -> None:
        if self._old is None:
            os.environ.pop("AGENT_HUB_FEATURES", None)
        else:
            os.environ["AGENT_HUB_FEATURES"] = self._old

    def test_unset_enables_everything(self) -> None:
        from orchestrator.core.features import feature_enabled

        self.assertTrue(feature_enabled("write"))
        self.assertTrue(feature_enabled("cancel"))
        self.assertTrue(feature_enabled("merge"))

    def test_disable_prefix(self) -> None:
        from orchestrator.core.features import feature_enabled

        os.environ["AGENT_HUB_FEATURES"] = "-merge"
        self.assertFalse(feature_enabled("merge"))
        self.assertTrue(feature_enabled("write"))

    def test_allowlist(self) -> None:
        from orchestrator.core.features import feature_enabled, enabled_features

        os.environ["AGENT_HUB_FEATURES"] = "write,cancel"
        self.assertTrue(feature_enabled("write"))
        self.assertTrue(feature_enabled("cancel"))
        self.assertFalse(feature_enabled("merge"))
        self.assertEqual(enabled_features(), ("cancel", "write"))


if __name__ == "__main__":
    unittest.main()
