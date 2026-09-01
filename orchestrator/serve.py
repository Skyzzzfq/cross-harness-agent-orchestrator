from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from typing import Any

from orchestrator.adapters.contracts import BackendAdapter
from orchestrator.agent_pool import reconcile_pool_once
from orchestrator.core.config import TeamSpec
from orchestrator.core.models import AuthorityToken
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
    interval: float = 1.0,
    controller_lease_seconds: int = 300,
    stop_event: asyncio.Event | None = None,
    max_ticks: int | None = None,
) -> dict[str, Any]:
    """Run a resident background loop for one Run.

    The loop holds the Run controller lease and the business AuthorityLease for
    its whole lifetime, renewing both in the background, and exits (releasing
    them) when it loses ownership. Each cycle reclaims expired assignment
    leases, dispatches ready tasks, sweeps cancel requests, and reconciles
    agent pools to the configured target count. The loop stops cleanly on
    ``stop_event`` or after ``max_ticks`` cycles.
    """
    if interval <= 0:
        raise ValueError("interval must be positive")
    if controller_lease_seconds < 1:
        raise ValueError("controller_lease_seconds must be at least 1")

    owner = f"serve-{uuid.uuid4().hex}"
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
        try:
            store.release_run_controller(token)
        except FencedControllerError:
            pass
