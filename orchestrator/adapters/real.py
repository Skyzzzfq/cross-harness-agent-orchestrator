from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.adapters.codebuddy_config import (
    CODEBUDDY_REGION,
    codebuddy_china_environment,
    preferred_codebuddy_cli,
)
from orchestrator.adapters.contracts import (
    AdapterCallRequest,
    BackendAdapter,
    CallRef,
    CallSnapshot,
    CallState,
    Failure,
    UsageReport,
)
from orchestrator.core.models import utc_now
from orchestrator.platform import codex_transport_environment


class _BaseRunningCall:
    def __init__(self, request: AdapterCallRequest, started_at: str) -> None:
        self._request = request
        self._started_at = started_at
        self._snapshot = CallSnapshot(
            ref=CallRef(
                call_id=request.call_id,
                backend=request.session.backend,
                session=request.session,
                provider_call_id=f"{request.session.backend}-{request.call_id}",
            ),
            state=CallState.RUNNING,
            started_at=started_at,
        )
        self._lock = asyncio.Lock()
        self._cancel_sent = False

    @property
    def ref(self) -> CallRef:
        return self._snapshot.ref

    async def _finish(
        self,
        state: CallState,
        *,
        text: str = "",
        structured: dict[str, object] | None = None,
        failure: Failure | None = None,
        usage: UsageReport | None = None,
        backend_invoked: bool = True,
        backend_may_still_run: bool = False,
    ) -> CallSnapshot:
        async with self._lock:
            if self._snapshot.state.is_terminal:
                return self._snapshot
            self._snapshot = CallSnapshot(
                ref=self._snapshot.ref,
                state=state,
                started_at=self._started_at,
                finished_at=utc_now(),
                text=text,
                structured=structured or {},
                failure=failure,
                usage=usage,
                backend_invoked=backend_invoked,
                backend_may_still_run=backend_may_still_run,
            )
            return self._snapshot

    async def wait(self, timeout_seconds: float | None = None) -> CallSnapshot:
        if timeout_seconds is not None and timeout_seconds < 0:
            raise ValueError("timeout_seconds must not be negative")
        task = self._task
        if task is None:
            return self._snapshot
        try:
            if timeout_seconds is None:
                await asyncio.shield(task)
            else:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout_seconds)
        except TimeoutError:
            pass
        except asyncio.CancelledError:
            if not self._snapshot.state.is_terminal:
                raise
        return self._snapshot


class CodexBackendAdapter:
    backend = "codex"

    async def start(self, request: AdapterCallRequest) -> _BaseRunningCall:
        from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, Sandbox

        codex = AsyncCodex(
            CodexConfig(env=codex_transport_environment(Path(request.policy.cwd)))
        )
        await codex.__aenter__()
        thread = await codex.thread_start(
            approval_mode=ApprovalMode.deny_all,
            cwd=request.policy.cwd,
            ephemeral=True,
            sandbox=Sandbox.read_only,
        )
        turn = await thread.turn(request.prompt)
        return _CodexRunningCall(codex, thread.id, turn, request)


class _CodexRunningCall(_BaseRunningCall):
    def __init__(
        self,
        codex: Any,
        thread_id: str,
        turn: Any,
        request: AdapterCallRequest,
    ) -> None:
        super().__init__(request, utc_now())
        self._codex = codex
        self._thread_id = thread_id
        self._turn = turn
        self._task = asyncio.create_task(self._execute())

    async def _execute(self) -> None:
        try:
            result = await asyncio.wait_for(
                asyncio.shield(self._turn.run()),
                timeout=self._request.policy.timeout_seconds,
            )
            status = getattr(result, "status", None)
            final = getattr(result, "final_response", None) or ""
            if status is not None and "complete" in str(status).lower():
                await self._finish(
                    CallState.SUCCEEDED,
                    text=str(final).strip(),
                    usage=self._codex_usage(result),
                )
            else:
                await self._finish(
                    CallState.FAILED,
                    failure=Failure(
                        kind="model_unexpected_status",
                        message=f"codex turn ended with status {status}",
                        retryable=True,
                    ),
                )
        except TimeoutError:
            await self._finish(
                CallState.TIMED_OUT,
                failure=Failure(
                    kind="deadline_exceeded",
                    message="codex turn exceeded the configured deadline",
                    retryable=True,
                ),
            )
        except asyncio.CancelledError:
            return
        except Exception as exc:  # SDK errors are mapped at the adapter boundary.
            await self._finish(
                CallState.FAILED,
                failure=Failure(
                    kind="sdk_error",
                    message=str(exc)[:500],
                    retryable=True,
                ),
            )
        finally:
            try:
                await asyncio.wait_for(
                    self._codex.thread_archive(self._thread_id), timeout=10.0
                )
            except Exception:
                pass
            try:
                await self._codex.__aexit__(None, None, None)
            except Exception:
                pass

    @staticmethod
    def _codex_usage(result: Any) -> UsageReport:
        usage = getattr(result, "usage", None)
        if usage is None:
            return UsageReport(
                duration_ms=getattr(result, "duration_ms", None),
                turns=getattr(result, "num_turns", None),
            )
        return UsageReport(
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            duration_ms=getattr(result, "duration_ms", None),
            turns=getattr(result, "num_turns", None),
            cost_decimal=getattr(result, "total_cost_usd", None),
        )

    async def cancel(self, reason: str) -> CallSnapshot:
        if not reason.strip():
            raise ValueError("cancel reason must not be empty")
        async with self._lock:
            if self._snapshot.state.is_terminal:
                return self._snapshot
            if self._cancel_sent:
                return self._snapshot
            self._cancel_sent = True
        try:
            await asyncio.wait_for(self._turn.interrupt(), timeout=15.0)
            await asyncio.wait_for(self._task, timeout=30.0)
        except TimeoutError:
            return await self._finish(
                CallState.CANCELLED,
                failure=Failure(
                    kind="cancelled",
                    message=reason,
                    retryable=False,
                ),
                backend_may_still_run=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return await self._finish(
                CallState.CANCELLED,
                failure=Failure(
                    kind="cancelled",
                    message=f"{reason}; interrupt error: {exc}",
                    retryable=False,
                ),
                backend_may_still_run=True,
            )
        return await self._finish(
            CallState.CANCELLED,
            failure=Failure(kind="cancelled", message=reason, retryable=False),
        )


class CodeBuddyBackendAdapter:
    backend = "codebuddy"

    async def start(self, request: AdapterCallRequest) -> _BaseRunningCall:
        from codebuddy_agent_sdk import (
            CodeBuddyAgentOptions,
            authenticate,
            query,
        )

        cli_path = preferred_codebuddy_cli(Path(request.policy.cwd))
        if cli_path is None:
            return _BlockedRunningCall(
                request,
                Failure(
                    kind="cli_unavailable",
                    message="no CodeBuddy CLI found for the China service",
                    retryable=False,
                ),
            )
        auth = await authenticate(
            environment=CODEBUDDY_REGION,
            env=codebuddy_china_environment(),
            codebuddy_code_path=cli_path,
            timeout=15.0,
        )
        if auth.auth_url:
            await auth.cancel()
            return _BlockedRunningCall(
                request,
                Failure(
                    kind="interactive_login_required",
                    message="CodeBuddy requires interactive sign-in",
                    retryable=False,
                ),
            )
        await auth
        options = CodeBuddyAgentOptions(
            cwd=request.policy.cwd,
            codebuddy_code_path=cli_path,
            max_turns=1,
            permission_mode="plan",
            request_timeout_ms=int(request.policy.timeout_seconds * 1000),
            setting_sources=[],
            env=codebuddy_china_environment(),
        )
        return _CodeBuddyRunningCall(request, query, options)


class _CodeBuddyRunningCall(_BaseRunningCall):
    def __init__(
        self,
        request: AdapterCallRequest,
        query: Any,
        options: Any,
    ) -> None:
        super().__init__(request, utc_now())
        self._query = query
        self._options = options
        self._task = asyncio.create_task(self._execute())

    async def _execute(self) -> None:
        from codebuddy_agent_sdk import AssistantMessage, ResultMessage, TextBlock

        text_parts: list[str] = []
        result_message = None
        try:
            async for message in self._query(
                prompt=self._request.prompt, options=self._options
            ):
                if isinstance(message, AssistantMessage):
                    text_parts.extend(
                        block.text
                        for block in message.content
                        if isinstance(block, TextBlock)
                    )
                elif isinstance(message, ResultMessage):
                    result_message = message
            if (
                result_message is not None
                and not result_message.is_error
                and result_message.duration_ms is not None
            ):
                await self._finish(
                    CallState.SUCCEEDED,
                    text="".join(text_parts).strip(),
                    usage=UsageReport(
                        duration_ms=result_message.duration_ms,
                        turns=getattr(result_message, "num_turns", None),
                    ),
                )
            elif result_message is not None:
                await self._finish(
                    CallState.FAILED,
                    failure=Failure(
                        kind="model_error",
                        message=(
                            getattr(result_message, "error_message", None)
                            or "codebuddy returned an error result"
                        )[:500],
                        retryable=True,
                    ),
                )
            else:
                await self._finish(
                    CallState.FAILED,
                    failure=Failure(
                        kind="empty_result",
                        message="codebuddy stream ended without a result",
                        retryable=True,
                    ),
                )
        except TimeoutError:
            await self._finish(
                CallState.TIMED_OUT,
                failure=Failure(
                    kind="deadline_exceeded",
                    message="codebuddy query exceeded the configured deadline",
                    retryable=True,
                ),
            )
        except asyncio.CancelledError:
            return
        except Exception as exc:
            await self._finish(
                CallState.FAILED,
                failure=Failure(
                    kind="sdk_error",
                    message=str(exc)[:500],
                    retryable=True,
                ),
            )

    async def cancel(self, reason: str) -> CallSnapshot:
        if not reason.strip():
            raise ValueError("cancel reason must not be empty")
        async with self._lock:
            if self._snapshot.state.is_terminal:
                return self._snapshot
            if self._cancel_sent:
                return self._snapshot
            self._cancel_sent = True
        # The CodeBuddy SDK does not expose a hard interrupt for an in-flight
        # query, so we stop observing and let the backend call finish naturally;
        # its late result is isolated by the orchestrator.
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        return await self._finish(
            CallState.CANCEL_REQUESTED,
            failure=Failure(
                kind="cancel_unconfirmed",
                message=reason,
                retryable=False,
            ),
            backend_may_still_run=True,
        )


class _BlockedRunningCall(_BaseRunningCall):
    def __init__(
        self,
        request: AdapterCallRequest,
        failure: Failure,
    ) -> None:
        super().__init__(request, utc_now())
        self._failure = failure
        self._task = asyncio.create_task(
            self._finish(
                CallState.BLOCKED,
                failure=failure,
                backend_invoked=False,
            )
        )

    async def cancel(self, reason: str) -> CallSnapshot:
        if not reason.strip():
            raise ValueError("cancel reason must not be empty")
        await self.wait(timeout_seconds=5)
        return self._snapshot
