"""Bridge supervisor backend results to validated, persisted worker plans."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from orchestrator.core.models import AuthorityToken, ControllerToken
from orchestrator.core.sanitize import redact_sensitive
from orchestrator.core.supervisor_plan import (
    PlanValidationError,
    validate_supervisor_plan,
)
from orchestrator.storage.sqlite_store import (
    FencedAuthorityError,
    FencedControllerError,
    SQLiteStateStore,
)


def materialize_ready_supervisor_plans(
    store: SQLiteStateStore,
    *,
    run_id: str,
    team_spec: Any,
    controller: ControllerToken,
    authority: AuthorityToken,
    worker_cwd: str | None = None,
) -> list[dict[str, Any]]:
    """Process successful supervisor calls once per Run tick."""
    roles = {str(role.role_id) for role in team_spec.roles}
    supervisor_role = str(team_spec.bootstrap_supervisor)
    child_roles = roles - {supervisor_role}
    backends = {str(pool.backend) for pool in team_spec.agent_pools}
    pairs = {
        (str(pool.role_id), str(pool.backend)) for pool in team_spec.agent_pools
    }
    max_tasks = sum(
        int(pool.max_count)
        for pool in team_spec.agent_pools
        if str(pool.role_id) in child_roles
    )
    max_tasks = max(1, max_tasks)
    rows = store.connection.execute(
        """
        SELECT t.task_id, d.cwd
        FROM tasks t
        JOIN task_dispatch_specs d ON d.task_id = t.task_id
        WHERE t.run_id = ? AND t.state = 'REVIEW'
          AND d.required_role_id = ?
        ORDER BY t.created_at, t.task_id
        """,
        (run_id, supervisor_role),
    ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        supervisor_task_id = str(row["task_id"])
        if _already_processed(store, run_id, supervisor_task_id):
            continue
        call = store.connection.execute(
            """
            SELECT call_id, result_json, state, disposition
            FROM backend_calls
            WHERE task_id = ?
            ORDER BY requested_at DESC, call_id DESC LIMIT 1
            """,
            (supervisor_task_id,),
        ).fetchone()
        if call is None or call["state"] != "succeeded":
            continue
        call_id = str(call["call_id"])
        if call["disposition"] != "submitted":
            continue
        try:
            payload = _extract_plan_payload(call["result_json"])
            existing_ids = {
                str(item["task_id"])
                for item in store.connection.execute(
                    "SELECT task_id FROM tasks WHERE run_id = ?", (run_id,)
                )
            }
            plan = validate_supervisor_plan(
                payload,
                allowed_role_ids=roles,
                allowed_backends=backends,
                allowed_child_role_ids=child_roles,
                role_backend_pairs=pairs,
                max_tasks=max_tasks,
                existing_task_ids=existing_ids,
            )
            materialized = store.materialize_supervisor_plan(
                run_id,
                supervisor_task_id,
                plan,
                controller=controller,
                authority=authority,
                worker_cwd=worker_cwd or str(row["cwd"]),
            )
            results.append(
                {
                    "supervisor_task_id": supervisor_task_id,
                    "call_id": call_id,
                    **materialized,
                }
            )
        except (PlanValidationError, ValueError, KeyError) as exc:
            reason = redact_sensitive(f"{type(exc).__name__}: {exc}")
            store.record_supervisor_plan_rejection(
                run_id,
                supervisor_task_id,
                call_id=call_id,
                reason=reason,
                controller=controller,
                authority=authority,
            )
            results.append(
                {
                    "supervisor_task_id": supervisor_task_id,
                    "call_id": call_id,
                    "status": "rejected",
                    "reason": reason,
                }
            )
        except (FencedControllerError, FencedAuthorityError):
            raise
    return results


def _already_processed(
    store: SQLiteStateStore, run_id: str, supervisor_task_id: str
) -> bool:
    row = store.connection.execute(
        """
        SELECT 1 FROM events
        WHERE run_id = ? AND task_id = ?
          AND kind IN ('plan.materialized', 'plan.rejected')
        LIMIT 1
        """,
        (run_id, supervisor_task_id),
    ).fetchone()
    return row is not None


def _extract_plan_payload(result_json: str | None) -> Mapping[str, Any]:
    if not result_json:
        raise PlanValidationError("supervisor result is empty")
    try:
        result = json.loads(result_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise PlanValidationError("backend result is not valid JSON") from exc
    if not isinstance(result, Mapping):
        raise PlanValidationError("backend result envelope must be an object")
    structured = result.get("structured")
    if isinstance(structured, Mapping) and structured:
        return structured
    text = result.get("text")
    if not isinstance(text, str) or not text.strip():
        raise PlanValidationError("supervisor result has no structured plan")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlanValidationError("supervisor text is not valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise PlanValidationError("supervisor text plan must be an object")
    return payload
