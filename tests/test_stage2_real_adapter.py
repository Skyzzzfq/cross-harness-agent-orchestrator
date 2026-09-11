from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

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
    _CodexRunningCall,
    _BlockedRunningCall,
)
from orchestrator.console.settings import save_model_provider


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

    def test_custom_codebuddy_provider_reads_key_from_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".agent-hub").mkdir()
            save_model_provider(
                root,
                {
                    "provider_id": "volc-codingplan",
                    "label": "Volc",
                    "backend": "codebuddy",
                    "base_url": "https://example.invalid/api/coding/v3",
                    "api_key_env": "TEST_CODINGPLAN_KEY",
                    "models": ["doubao-seed-code"],
                },
            )
            with mock.patch.dict(
                "os.environ", {"TEST_CODINGPLAN_KEY": "secret-value"}, clear=False
            ):
                from orchestrator.adapters.codebuddy_config import (
                    codebuddy_environment_for_provider,
                )

                environment = codebuddy_environment_for_provider(
                    root, "volc-codingplan"
                )
            self.assertEqual(
                environment,
                {
                    "CODEBUDDY_BASE_URL": "https://example.invalid/api/coding/v3",
                    "CODEBUDDY_API_KEY": "secret-value",
                },
            )
            saved = json.loads(
                (root / ".agent-hub" / "settings.json").read_text(encoding="utf-8")
            )
            self.assertNotIn('"api_key"', json.dumps(saved, ensure_ascii=False))


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


class CodexRunningCallTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_finishes_without_self_await_or_orphaning(self) -> None:
        class Turn:
            def __init__(self) -> None:
                self.released = asyncio.Event()
                self.interrupt_calls = 0

            async def run(self) -> SimpleNamespace:
                await self.released.wait()
                return SimpleNamespace(
                    status="completed",
                    final_response="late",
                    usage=None,
                    duration_ms=1,
                    num_turns=1,
                    total_cost_usd=None,
                )

            async def interrupt(self) -> None:
                self.interrupt_calls += 1
                self.released.set()

        class Codex:
            def __init__(self) -> None:
                self.archived: list[str] = []

            async def thread_archive(self, thread_id: str) -> None:
                self.archived.append(thread_id)

            async def __aexit__(self, *_: object) -> None:
                return None

        request = AdapterCallRequest(
            **{
                **_request("codex-timeout").__dict__,
                "policy": AccessPolicy(
                    access_mode="read_only",
                    cwd="D:/workspace/connect",
                    timeout_seconds=0.01,
                ),
            }
        )
        turn = Turn()
        codex = Codex()
        running = _CodexRunningCall(codex, "thread-timeout", turn, request)

        snapshot = await running.wait(timeout_seconds=1)

        self.assertEqual(snapshot.state, CallState.TIMED_OUT)
        self.assertEqual(snapshot.failure.kind, "deadline_exceeded")
        self.assertEqual(turn.interrupt_calls, 1)
        self.assertEqual(codex.archived, ["thread-timeout"])


if __name__ == "__main__":
    unittest.main()
