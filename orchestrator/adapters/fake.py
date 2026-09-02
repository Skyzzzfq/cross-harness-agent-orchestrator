from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from orchestrator.adapters.contracts import (
    AdapterCallRequest,
    BackendCapabilities,
    CallRef,
    CallSnapshot,
    CallState,
    Failure,
    UsageReport,
)
from orchestrator.core.models import utc_now


@dataclass(frozen=True)
class FakeBehavior:
    delay_seconds: float = 0.01
    terminal: CallState = CallState.SUCCEEDED
    text: str = "ok"
    structured: dict[str, object] = field(default_factory=dict)
    error_kind: str = "fake_error"
    cancel_mode: str = "confirmed"

    def __post_init__(self) -> None:
        if self.delay_seconds < 0:
            raise ValueError("delay_seconds must not be negative")
        if not self.terminal.is_terminal:
            raise ValueError("fake behavior must declare a terminal state")
        if self.cancel_mode not in {"confirmed", "unconfirmed", "unsupported"}:
            raise ValueError("unsupported fake cancel_mode")


class _FakeRunningCall:
    def __init__(
        self,
        adapter: FakeBackendAdapter,
        request: AdapterCallRequest,
        behavior: FakeBehavior,
    ) -> None:
        self._adapter = adapter
        self._request = request
        self._behavior = behavior
        self._started_monotonic = time.monotonic()
        self._lock = asyncio.Lock()
        self._cancel_sent = False
        self._snapshot = CallSnapshot(
            ref=CallRef(
                call_id=request.call_id,
                backend=adapter.backend,
                session=request.session,
                provider_call_id=f"fake-{request.call_id}",
            ),
            state=CallState.RUNNING,
            started_at=utc_now(),
        )
        if behavior.terminal == CallState.BLOCKED:
            self._snapshot = CallSnapshot(
                ref=self._snapshot.ref,
                state=CallState.BLOCKED,
                started_at=self._snapshot.started_at,
                finished_at=utc_now(),
                failure=Failure(
                    kind=behavior.error_kind,
                    message="fake blocked",
                    retryable=False,
                ),
                usage=UsageReport(duration_ms=0),
                backend_invoked=False,
            )
            self._task = None
        else:
            self._task = asyncio.create_task(self._execute())

    @property
    def ref(self) -> CallRef:
        return self._snapshot.ref

    async def _execute(self) -> None:
        try:
            await asyncio.wait_for(
                asyncio.sleep(self._behavior.delay_seconds),
                timeout=self._request.policy.timeout_seconds,
            )
            terminal = self._behavior.terminal
            failure = None
            invoked = terminal != CallState.BLOCKED
            if terminal in {CallState.FAILED, CallState.BLOCKED, CallState.ORPHANED}:
                failure = Failure(
                    kind=self._behavior.error_kind,
                    message=f"fake {terminal.value}",
                    retryable=terminal == CallState.FAILED,
                )
            await self._finish(
                terminal,
                text=self._behavior.text if terminal == CallState.SUCCEEDED else "",
                structured=self._behavior.structured,
                failure=failure,
                backend_invoked=invoked,
            )
        except TimeoutError:
            await self._finish(
                CallState.TIMED_OUT,
                failure=Failure(
                    kind="deadline_exceeded",
                    message="fake execution deadline exceeded",
                    retryable=True,
                ),
            )
        except asyncio.CancelledError:
            return

    async def _finish(
        self,
        terminal: CallState,
        *,
        text: str = "",
        structured: dict[str, object] | None = None,
        failure: Failure | None = None,
        backend_invoked: bool = True,
    ) -> CallSnapshot:
        async with self._lock:
            if self._snapshot.state.is_terminal:
                return self._snapshot
            duration_ms = int((time.monotonic() - self._started_monotonic) * 1000)
            self._snapshot = CallSnapshot(
                ref=self._snapshot.ref,
                state=terminal,
                started_at=self._snapshot.started_at,
                finished_at=utc_now(),
                text=text,
                structured=structured or {},
                failure=failure,
                usage=UsageReport(duration_ms=duration_ms),
                backend_invoked=backend_invoked,
            )
            return self._snapshot

    async def wait(self, timeout_seconds: float | None = None) -> CallSnapshot:
        if timeout_seconds is not None and timeout_seconds < 0:
            raise ValueError("timeout_seconds must not be negative")
        if not self._snapshot.state.is_terminal:
            try:
                if self._task is None:
                    return self._snapshot
                if timeout_seconds is None:
                    await asyncio.shield(self._task)
                else:
                    await asyncio.wait_for(
                        asyncio.shield(self._task), timeout=timeout_seconds
                    )
            except TimeoutError:
                pass
            except asyncio.CancelledError:
                if not self._snapshot.state.is_terminal:
                    raise
        return self._snapshot

    async def cancel(self, reason: str) -> CallSnapshot:
        if not reason.strip():
            raise ValueError("cancel reason must not be empty")
        async with self._lock:
            if self._snapshot.state.is_terminal:
                return self._snapshot
            if self._cancel_sent:
                return self._snapshot
            self._cancel_sent = True
            self._adapter.cancel_count += 1
            if self._behavior.cancel_mode != "confirmed":
                self._snapshot = CallSnapshot(
                    ref=self._snapshot.ref,
                    state=CallState.CANCEL_REQUESTED,
                    started_at=self._snapshot.started_at,
                    failure=Failure(
                        kind=f"cancel_{self._behavior.cancel_mode}",
                        message=reason,
                        retryable=False,
                    ),
                    backend_invoked=True,
                    backend_may_still_run=True,
                )
                return self._snapshot
            duration_ms = int((time.monotonic() - self._started_monotonic) * 1000)
            self._snapshot = CallSnapshot(
                ref=self._snapshot.ref,
                state=CallState.CANCELLED,
                started_at=self._snapshot.started_at,
                finished_at=utc_now(),
                failure=Failure(
                    kind="cancelled",
                    message=reason,
                    retryable=False,
                ),
                usage=UsageReport(duration_ms=duration_ms),
            )
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        return self._snapshot


class FakeBackendAdapter:
    backend = "fake"

    def __init__(
        self,
        *,
        behaviors: dict[str, FakeBehavior] | None = None,
        default_behavior: FakeBehavior | None = None,
        behaviors_by_task: dict[str, FakeBehavior] | None = None,
    ) -> None:
        self.behaviors = behaviors or {}
        self.behaviors_by_task = behaviors_by_task or {}
        self.default_behavior = default_behavior or FakeBehavior()
        self.launch_count = 0
        self.cancel_count = 0
        self._calls: dict[str, tuple[AdapterCallRequest, _FakeRunningCall]] = {}
        self._active_sessions: dict[str, str] = {}

    async def start(self, request: AdapterCallRequest) -> _FakeRunningCall:
        if request.session.backend != self.backend:
            raise ValueError("session backend does not match adapter")
        existing = self._calls.get(request.call_id)
        if existing is not None:
            original_request, running = existing
            if original_request != request:
                raise ValueError("call_id already belongs to a different request")
            return running
        active_call_id = self._active_sessions.get(request.session.session_id)
        if active_call_id is not None:
            active = self._calls[active_call_id][1]
            if not (await active.wait(timeout_seconds=0)).state.is_terminal:
                raise RuntimeError("session already has an active call")
        behavior = self.behaviors.get(
            request.call_id,
            self.behaviors_by_task.get(request.task_id, self.default_behavior),
        )
        running = _FakeRunningCall(self, request, behavior)
        self._calls[request.call_id] = (request, running)
        if behavior.terminal != CallState.BLOCKED:
            self._active_sessions[request.session.session_id] = request.call_id
            self.launch_count += 1
        return running

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            backend=self.backend,
            version="fake-1",
            supports_write=True,
            supports_cancel=True,
            supports_structured_output=True,
        )
