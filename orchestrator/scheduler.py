from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from typing import Any

from orchestrator.adapters.contracts import (
    AdapterCallRequest,
    BackendAdapter,
    CallRef,
    CallSnapshot,
    CallState,
    Failure,
)
from orchestrator.call_runtime import execute_adapter_call, recover_starting_calls
from orchestrator.core.models import AuthorityToken, ControllerToken, utc_now
from orchestrator.storage.sqlite_store import (
    FencedAttemptError,
    FencedAuthorityError,
    FencedControllerError,
    SQLiteStateStore,
)


async def scheduler_tick(
    store: SQLiteStateStore,
    *,
    run_id: str,
    adapters: Mapping[str, BackendAdapter],
    authority: AuthorityToken,
    limit: int = 100,
    lease_seconds: int = 60,
    scheduler_owner: str | None = None,
    controller: ControllerToken | None = None,
    controller_lease_seconds: int = 300,
) -> dict[str, Any]:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if controller_lease_seconds < 1:
        raise ValueError("controller_lease_seconds must be at least 1")
    owner = scheduler_owner or f"scheduler-{uuid.uuid4().hex}"
    acquired_here = controller is None
    token = controller
    if token is None:
        token = store.acquire_run_controller(
            run_id,
            owner,
            lease_seconds=controller_lease_seconds,
        )
        if token is None:
            return {
                "run_id": run_id,
                "scheduler_owner": owner,
                "status": "busy",
                "dispatched": [],
                "outcomes": [],
            }
    elif token.run_id != run_id:
        raise FencedControllerError("controller token belongs to another Run")

    token = store.renew_run_controller(
        token,
        lease_seconds=controller_lease_seconds,
    )
    heartbeat_stop = asyncio.Event()

    async def renew_controller() -> None:
        interval = max(0.1, controller_lease_seconds / 3)
        while True:
            try:
                await asyncio.wait_for(heartbeat_stop.wait(), timeout=interval)
                return
            except TimeoutError:
                try:
                    store.renew_run_controller(
                        token,
                        lease_seconds=controller_lease_seconds,
                    )
                except FencedControllerError:
                    return

    heartbeat_task = asyncio.create_task(renew_controller())

    claims: list[AdapterCallRequest] = []

    async def execute_once(claim: AdapterCallRequest) -> dict[str, Any]:
        adapter = adapters.get(claim.session.backend)
        if adapter is None:
            terminal = CallSnapshot(
                ref=CallRef(
                    call_id=claim.call_id,
                    backend=claim.session.backend,
                    session=claim.session,
                ),
                state=CallState.BLOCKED,
                started_at=utc_now(),
                finished_at=utc_now(),
                failure=Failure(
                    kind="adapter_unavailable",
                    message=f"no adapter registered for {claim.session.backend}",
                    retryable=False,
                ),
                backend_invoked=False,
            )
            disposition = store.finish_backend_call(
                claim.call_id,
                terminal,
                reason="adapter-unavailable",
                controller=token,
            )
        else:
            try:
                terminal = await execute_adapter_call(
                    store,
                    adapter,
                    claim,
                    controller=token,
                )
                disposition = store.backend_call_snapshot(claim.call_id)[
                    "disposition"
                ]
            except Exception as error:
                recovery = "fenced"
                try:
                    recovery = store.recover_backend_call(
                        claim.call_id,
                        controller=token,
                        reason="adapter-execution-error",
                        allow_current_epoch=True,
                    )
                except FencedControllerError:
                    pass
                return {
                    "call_id": claim.call_id,
                    "task_id": claim.task_id,
                    "state": CallState.ORPHANED.value,
                    "disposition": recovery,
                    "error": type(error).__name__,
                }
        return {
            "call_id": claim.call_id,
            "task_id": claim.task_id,
            "state": terminal.state.value,
            "disposition": disposition,
        }

    async def execute(claim: AdapterCallRequest) -> dict[str, Any]:
        lease_stop = asyncio.Event()

        async def renew_assignment_lease() -> None:
            interval = max(0.1, lease_seconds / 3)
            while True:
                try:
                    await asyncio.wait_for(lease_stop.wait(), timeout=interval)
                    return
                except TimeoutError:
                    try:
                        store.heartbeat_backend_call_lease(
                            claim.call_id,
                            controller=token,
                            lease_seconds=lease_seconds,
                        )
                    except (FencedAttemptError, FencedControllerError):
                        return

        lease_heartbeat = asyncio.create_task(renew_assignment_lease())
        try:
            return await execute_once(claim)
        finally:
            lease_stop.set()
            await lease_heartbeat

    try:
        recovered = recover_starting_calls(
            store,
            run_id=run_id,
            controller=token,
        )
        dag_reconciled = store.reconcile_task_graph(
            run_id,
            controller=token,
            authority=authority,
        )
        for _ in range(limit):
            claim = store.claim_ready_dispatch(
                run_id,
                controller=token,
                authority=authority,
                lease_seconds=lease_seconds,
            )
            if claim is None:
                break
            claims.append(claim)
        outcomes = await asyncio.gather(*(execute(claim) for claim in claims))
        return {
            "run_id": run_id,
            "scheduler_owner": token.owner_id,
            "controller_epoch": token.epoch,
            "status": "ready",
            "recovered": recovered,
            "dag_reconciled": dag_reconciled,
            "dispatched": [claim.call_id for claim in claims],
            "outcomes": list(outcomes),
        }
    finally:
        heartbeat_stop.set()
        await heartbeat_task
        if acquired_here:
            try:
                store.release_run_controller(token)
            except FencedControllerError:
                pass
