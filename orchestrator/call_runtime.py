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
        terminal = await running.wait()
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
