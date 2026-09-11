from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import Mapping
from typing import Any

from orchestrator.adapters.contracts import BackendAdapter
from orchestrator.agent_pool import reconcile_pool_once
from orchestrator.core.config import TeamSpec
from orchestrator.core.models import AuthorityToken, ControllerToken
from orchestrator.reconciler import reconcile_once
from orchestrator.scheduler import scheduler_tick
from orchestrator.storage.sqlite_store import (
    FencedAuthorityError,
    FencedControllerError,
    SQLiteStateStore,
)


async def serve(
    store: SQLiteStateStore,
    run_id: str,
    adapters: Mapping[str, BackendAdapter],
    team_spec: TeamSpec | None = None,
    *,
    authority: AuthorityToken | None = None,
    controller: ControllerToken | None = None,
    git_manager: Any | None = None,
    outbox_deliver: Any | None = None,
    message_handler: Any | None = None,
    message_consumer_id: str | None = None,
    interval: float = 1.0,
    controller_lease_seconds: int = 300,
    stop_event: asyncio.Event | None = None,
    max_ticks: int | None = None,
) -> dict[str, Any]:
    """Run a resident background loop for one Run.

    The loop holds the Run controller lease and the business AuthorityLease for
    its whole lifetime, renewing both in the background, and exits (releasing
    them) when it loses ownership. Each cycle reclaims expired assignment
    leases, dispatches ready tasks, sweeps cancel requests, reconciles agent
    pools, and—when a ``git_manager`` is provided—consumes the persistent
    Merge Queue and Transactional Outbox with real Git integration and the
    optional ``outbox_deliver`` hook. Message delivery is also owned by this
    fenced loop when ``message_handler`` is supplied; callers only enqueue
    intents and never mutate delivery state directly. The loop stops cleanly
    on ``stop_event`` or after ``max_ticks`` cycles.
    """
    if interval <= 0:
        raise ValueError("interval must be positive")
    if controller_lease_seconds < 1:
        raise ValueError("controller_lease_seconds must be at least 1")

    owner = f"serve-{uuid.uuid4().hex}"
    owns_controller = controller is None
    if controller is not None:
        if controller.run_id != run_id:
            raise FencedControllerError("controller token belongs to another Run")
        token = controller
    else:
        token = store.acquire_run_controller(
            run_id, owner, lease_seconds=controller_lease_seconds
        )
        if token is None:
            return {
                "status": "busy",
                "run_id": run_id,
                "scheduler_owner": owner,
            }

    authority_token = authority
    if authority_token is None:
        try:
            authority_token = store.acquire_authority(
                run_id,
                owner,
                "supervisor",
                lease_seconds=controller_lease_seconds,
            )
        except FencedAuthorityError:
            store.release_run_controller(token)
            return {
                "status": "busy",
                "run_id": run_id,
                "scheduler_owner": owner,
                "reason": "authority held by another supervisor",
            }
    elif authority_token.run_id != run_id:
        store.release_run_controller(token)
        raise FencedAuthorityError("authority token belongs to another Run")

    # P0-02：常驻循环可选消费 Merge Queue 与 Outbox（提供 git_manager 时启用）
    merge_executor = None
    outbox_dispatcher = None
    if git_manager is not None:
        from orchestrator.workspace.merge_executor import (
            MergeExecutor,
            OutboxDispatcher,
        )

        merge_executor = MergeExecutor(store, git_manager)
        outbox_dispatcher = OutboxDispatcher(store, deliver=outbox_deliver)

    heartbeat_stop = asyncio.Event()

    async def renew_controller() -> None:
        renewal = max(0.1, controller_lease_seconds / 3)
        while True:
            try:
                await asyncio.wait_for(heartbeat_stop.wait(), timeout=renewal)
                return
            except TimeoutError:
                try:
                    store.renew_run_controller(
                        token, lease_seconds=controller_lease_seconds
                    )
                    store.renew_authority(
                        authority_token, lease_seconds=controller_lease_seconds
                    )
                except (FencedControllerError, FencedAuthorityError):
                    return

    heartbeat_task = asyncio.create_task(renew_controller())
    ticks = 0
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            if max_ticks is not None and ticks >= max_ticks:
                break
            try:
                reconcile_once(store, run_id=run_id)
                await scheduler_tick(
                    store,
                    run_id=run_id,
                    adapters=adapters,
                    authority=authority_token,
                    controller=token,
                    controller_lease_seconds=controller_lease_seconds,
                )
                if team_spec is not None:
                    from orchestrator.core.supervisor_planning import (
                        materialize_ready_supervisor_plans,
                    )

                    materialize_ready_supervisor_plans(
                        store,
                        run_id=run_id,
                        team_spec=team_spec,
                        controller=token,
                        authority=authority_token,
                    )
                # P0-02：常驻循环消费 Merge Queue 与 Transactional Outbox
                if merge_executor is not None:
                    merge_executor.run_merge_once(
                        run_id, token, authority_token
                    )
                if outbox_dispatcher is not None:
                    outbox_dispatcher.run_once(run_id, token, authority_token)
                store.expire_message_deliveries(run_id, controller=token)
                if message_handler is not None:
                    consumer = message_consumer_id or owner
                    deliveries = store.claim_message_deliveries(
                        run_id,
                        controller=token,
                        consumer_id=consumer,
                        limit=100,
                    )
                    for delivery in deliveries:
                        try:
                            outcome = message_handler(delivery)
                            if inspect.isawaitable(outcome):
                                await outcome
                            store.acknowledge_message(
                                delivery["delivery_id"],
                                controller=token,
                                consumer_id=consumer,
                            )
                        except Exception as error:  # noqa: BLE001
                            store.fail_message_delivery(
                                delivery["delivery_id"],
                                controller=token,
                                consumer_id=consumer,
                                reason=f"handler:{type(error).__name__}: {error}",
                            )
                if team_spec is not None:
                    for pool in team_spec.agent_pools:
                        reconcile_pool_once(store, run_id, pool)
            except (FencedControllerError, FencedAuthorityError):
                return {
                    "status": "lost-controller",
                    "run_id": run_id,
                    "scheduler_owner": owner,
                    "controller_epoch": token.epoch,
                    "authority_epoch": authority_token.epoch,
                    "ticks": ticks,
                }
            ticks += 1
            if stop_event is not None and stop_event.is_set():
                break
            if max_ticks is not None and ticks >= max_ticks:
                break
            await asyncio.sleep(interval)
        return {
            "status": "stopped",
            "run_id": run_id,
            "scheduler_owner": owner,
            "controller_epoch": token.epoch,
            "authority_epoch": authority_token.epoch,
            "ticks": ticks,
        }
    finally:
        heartbeat_stop.set()
        await heartbeat_task
        if owns_controller:
            try:
                store.release_run_controller(token)
            except FencedControllerError:
                pass
