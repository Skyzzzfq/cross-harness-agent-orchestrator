from __future__ import annotations

from typing import Any

from orchestrator.core.config import AgentPoolSpec
from orchestrator.core.models import AgentState
from orchestrator.storage.sqlite_store import SQLiteStateStore


CAPACITY_STATES = {
    AgentState.STARTING.value,
    AgentState.IDLE.value,
    AgentState.BUSY.value,
}


def reconcile_pool_once(
    store: SQLiteStateStore,
    run_id: str,
    spec: AgentPoolSpec,
) -> dict[str, Any]:
    if spec.count < 0 or spec.count > spec.max_count:
        raise ValueError("pool count is outside configured bounds")

    lock_owner = store.acquire_pool_reconcile_lock(run_id, spec.pool_id)
    if lock_owner is None:
        current = store.pool_agent_snapshots(run_id, spec.pool_id)
        return {
            "status": "busy",
            "run_id": run_id,
            "pool_id": spec.pool_id,
            "desired": spec.count,
            "active": sum(
                agent["status"] in CAPACITY_STATES for agent in current
            ),
            "created": [],
            "draining": [
                agent["agent_id"]
                for agent in current
                if agent["status"] == AgentState.DRAINING.value
            ],
            "stopped": [],
        }

    try:
        return _reconcile_locked(store, run_id, spec)
    finally:
        store.release_pool_reconcile_lock(run_id, spec.pool_id, lock_owner)


def _reconcile_locked(
    store: SQLiteStateStore,
    run_id: str,
    spec: AgentPoolSpec,
) -> dict[str, Any]:

    created: list[str] = []
    draining: list[str] = []
    stopped: list[str] = []

    agents = store.pool_agent_snapshots(run_id, spec.pool_id)
    for agent in agents:
        if (
            agent["status"] == AgentState.DRAINING.value
            and agent["current_task_id"] is None
            and agent["session_state"] != "ACTIVE"
        ):
            store.finalize_drained_agent(agent["agent_id"], run_id=run_id)
            stopped.append(agent["agent_id"])

    agents = store.pool_agent_snapshots(run_id, spec.pool_id)
    capacity = [agent for agent in agents if agent["status"] in CAPACITY_STATES]
    deficit = spec.count - len(capacity)
    for _ in range(max(deficit, 0)):
        provisioned = store.provision_fake_pool_agent(
            run_id=run_id,
            pool_id=spec.pool_id,
            backend=spec.backend,
            model=spec.model,
            role_id=spec.role_id,
        )
        created.append(provisioned["agent_id"])

    agents = store.pool_agent_snapshots(run_id, spec.pool_id)
    capacity = [agent for agent in agents if agent["status"] in CAPACITY_STATES]
    surplus = len(capacity) - spec.count
    if surplus > 0:
        candidates: list[dict[str, Any]] = []
        for state in (
            AgentState.STARTING.value,
            AgentState.IDLE.value,
            AgentState.BUSY.value,
        ):
            candidates.extend(
                sorted(
                    (item for item in capacity if item["status"] == state),
                    key=lambda item: (item["created_at"], item["agent_id"]),
                    reverse=True,
                )
            )
        candidates = candidates[:surplus]
        for agent in candidates:
            agent_id = agent["agent_id"]
            store.mark_agent_draining(
                agent_id, run_id=run_id, reason="pool-count-reduced"
            )
            if agent["status"] == AgentState.BUSY.value:
                draining.append(agent_id)
            else:
                store.finalize_drained_agent(agent_id, run_id=run_id)
                stopped.append(agent_id)

    current = store.pool_agent_snapshots(run_id, spec.pool_id)
    active = sum(agent["status"] in CAPACITY_STATES for agent in current)
    still_draining = [
        agent["agent_id"]
        for agent in current
        if agent["status"] == AgentState.DRAINING.value
    ]
    return {
        "status": "ready",
        "run_id": run_id,
        "pool_id": spec.pool_id,
        "desired": spec.count,
        "active": active,
        "created": created,
        "draining": sorted(set(draining + still_draining)),
        "stopped": stopped,
    }
