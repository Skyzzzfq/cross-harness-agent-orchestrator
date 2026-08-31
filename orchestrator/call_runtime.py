from __future__ import annotations

from orchestrator.adapters.contracts import (
    AdapterCallRequest,
    BackendAdapter,
    CallSnapshot,
    CallState,
)
from orchestrator.core.models import ControllerToken
from orchestrator.storage.sqlite_store import FencedAttemptError, SQLiteStateStore


async def execute_adapter_call(
    store: SQLiteStateStore,
    adapter: BackendAdapter,
    request: AdapterCallRequest,
    *,
    controller: ControllerToken | None = None,
) -> CallSnapshot:
    store.create_backend_call(request)
    store.authorize_backend_call(request.call_id, controller=controller)
    running = await adapter.start(request)
    initial = await running.wait(timeout_seconds=0)
    if initial.state == CallState.RUNNING:
        store.mark_backend_call_running(
            request.call_id,
            initial,
            reason="adapter-started",
            controller=controller,
        )
        terminal = await _wait_for_terminal_with_cancel(
            running, store, request.call_id
        )
    else:
        terminal = initial
        if terminal.backend_invoked:
            store.mark_backend_call_running(
                request.call_id,
                CallSnapshot(
                    ref=terminal.ref,
                    state=CallState.RUNNING,
                    started_at=terminal.started_at,
                    backend_invoked=True,
                ),
                reason="adapter-started-and-finished",
                controller=controller,
            )
    store.finish_backend_call(
        request.call_id,
        terminal,
        reason="adapter-terminal",
        controller=controller,
    )
    return terminal


async def _wait_for_terminal_with_cancel(
    running: object,
    store: SQLiteStateStore,
    call_id: str,
) -> CallSnapshot:
    """Wait for a running call, interrupting it when a cancel is requested.

    The persisted ``cancel_requested`` flag is polled so that a concurrent
    ``request_cancel_task`` (from another connection or a CLI operator) can
    interrupt an in-flight call well inside the 10-second cancel SLA.
    """
    cancel_sent = False
    while True:
        if not cancel_sent and store.backend_call_cancel_requested(call_id):
            snapshot = await running.cancel(reason="task-cancelled")
            if snapshot.state.is_terminal:
                return snapshot
            cancel_sent = True
        terminal = await running.wait(timeout_seconds=0.05)
        if terminal.state.is_terminal:
            return terminal


def recover_starting_calls(
    store: SQLiteStateStore,
    *,
    run_id: str,
    controller: ControllerToken | None = None,
) -> list[str]:
    recovered: list[str] = []
    for call in store.starting_backend_calls(run_id=run_id):
        if controller is not None:
            call_epoch = call["controller_epoch"]
            if call_epoch is not None and int(call_epoch) >= controller.epoch:
                continue
            store.recover_backend_call(
                call["call_id"],
                controller=controller,
                reason="controller-takeover",
            )
            recovered.append(call["call_id"])
            continue
        store.mark_backend_call_orphaned(
            call["call_id"], reason="hub-restarted-before-start-confirmation"
        )
        try:
            store.recover_lost_attempt(
                call["attempt_id"],
                call["generation"],
                reason="backend-call-orphaned",
            )
        except FencedAttemptError:
            pass
        store.release_dispatch_resources(
            call["call_id"],
            reason="backend-call-orphaned",
            isolate_session=(
                call["state"] != "starting" or bool(call["backend_invoked"])
            ),
        )
        recovered.append(call["call_id"])
    return recovered
