from __future__ import annotations

import asyncio
import unittest

from orchestrator.adapters.contracts import (
    AccessPolicy,
    AdapterCallRequest,
    CallState,
    Failure,
    SessionRef,
)
from orchestrator.adapters.real import (
    CodeBuddyBackendAdapter,
    CodexBackendAdapter,
    _BlockedRunningCall,
)


def _request(call_id: str = "call-1", backend: str = "codex") -> AdapterCallRequest:
    return AdapterCallRequest(
        call_id=call_id,
        run_id="run-1",
        task_id="task-1",
        attempt_id="attempt-1",
        generation=1,
        agent_id="agent-1",
        session=SessionRef(f"session-{backend}", backend),
        prompt="Reply with exactly OK.",
        policy=AccessPolicy("read_only", "D:/workspace/connect", 5),
    )


class RealBackendAdapterShapeTests(unittest.TestCase):
    def test_codex_adapter_shape(self) -> None:
        adapter = CodexBackendAdapter()
        self.assertEqual(adapter.backend, "codex")
        self.assertTrue(asyncio.iscoroutinefunction(adapter.start))

    def test_codebuddy_adapter_shape(self) -> None:
        adapter = CodeBuddyBackendAdapter()
        self.assertEqual(adapter.backend, "codebuddy")
        self.assertTrue(asyncio.iscoroutinefunction(adapter.start))


class BlockedRunningCallContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_is_terminal_without_backend_invocation(self) -> None:
        request = _request(backend="codebuddy")
        running = _BlockedRunningCall(
            request,
            Failure(
                kind="interactive_login_required",
                message="CodeBuddy requires interactive sign-in",
                retryable=False,
            ),
        )
        snapshot = await running.wait(timeout_seconds=2)
        self.assertEqual(snapshot.state, CallState.BLOCKED)
        self.assertFalse(snapshot.backend_invoked)
        self.assertEqual(snapshot.ref.call_id, "call-1")

    async def test_blocked_cancel_returns_blocked_snapshot(self) -> None:
        request = _request(backend="codebuddy")
        running = _BlockedRunningCall(
            request,
            Failure(kind="cli_unavailable", message="no cli", retryable=False),
        )
        snapshot = await running.cancel("operator-cancel")
        self.assertEqual(snapshot.state, CallState.BLOCKED)

    async def test_blocked_rejects_empty_cancel_reason(self) -> None:
        request = _request(backend="codebuddy")
        running = _BlockedRunningCall(
            request,
            Failure(kind="cli_unavailable", message="no cli", retryable=False),
        )
        with self.assertRaises(ValueError):
            await running.cancel("")


if __name__ == "__main__":
    unittest.main()
