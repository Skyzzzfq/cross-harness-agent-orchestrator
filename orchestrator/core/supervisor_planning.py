"""Bridge supervisor backend results to validated, persisted worker plans."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from orchestrator.core.models import AuthorityToken, ControllerToken, TaskState
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
    worker_slots = max(1, max_tasks)
    # A slot is a concurrency limit, not a task-count limit.  The bounded
    # default allows sequential work to be planned without permitting an
    # unbounded supervisor fan-out.
    max_tasks = max(1, worker_slots * 4)
    model_pairs = {
        (str(pool.role_id), str(pool.backend), str(pool.model))
        for pool in team_spec.agent_pools
        if pool.model
    }
    rows = store.connection.execute(
        """
        SELECT t.task_id, d.cwd, t.task_kind
        FROM tasks t
        JOIN task_dispatch_specs d ON d.task_id = t.task_id
        WHERE t.run_id = ? AND t.state = 'REVIEW'
          AND d.required_role_id = ?
          AND COALESCE(t.task_kind, '') <> 'supervisor_summary'
        ORDER BY t.created_at, t.task_id
        """,
        (run_id, supervisor_role),
    ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        supervisor_task_id = str(row["task_id"])
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
        latest_plan = store.latest_supervisor_plan(run_id, supervisor_task_id)
        if (
            latest_plan is None
            or str(latest_plan.get("status")) not in {"approved", "executing", "verifying", "delivered"}
        ) and _already_processed(store, run_id, supervisor_task_id, call_id):
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
                role_model_pairs=model_pairs,
                max_worker_concurrency=worker_slots,
            )
            latest = store.latest_supervisor_plan(run_id, supervisor_task_id)
            approval_mode = store.run_approval_mode(run_id)
            if approval_mode == "manual":
                if latest is not None and latest.get("plan_digest") == plan.digest:
                    if latest.get("status") in {"approved", "executing", "verifying", "delivered"}:
                        materialized = store.materialize_supervisor_plan(
                            run_id,
                            supervisor_task_id,
                            plan,
                            controller=controller,
                            authority=authority,
                            worker_cwd=worker_cwd or str(row["cwd"]),
                        )
                        results.append({"supervisor_task_id": supervisor_task_id, "call_id": call_id, **materialized})
                    continue
                preview = store.save_supervisor_plan(
                    run_id,
                    supervisor_task_id,
                    plan,
                    call_id=call_id,
                    status="pending_approval",
                )
                results.append(
                    {
                        "supervisor_task_id": supervisor_task_id,
                        "call_id": call_id,
                        "status": "pending_approval",
                        "revision": plan.revision,
                        "plan_digest": plan.digest,
                        "preview": json.loads(str(preview["plan_json"])),
                    }
                )
            else:
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
            prior_rejections = store.connection.execute(
                "SELECT COUNT(*) FROM events WHERE run_id=? AND task_id=? "
                "AND kind='plan.rejected'",
                (run_id, supervisor_task_id),
            ).fetchone()[0]
            repair_scheduled = False
            if int(prior_rejections) <= 1:
                # One bounded format repair gets a fresh backend call and
                # attempt; the original failure remains in the event log.
                try:
                    store.reassign_task(
                        run_id,
                        supervisor_task_id,
                        controller,
                        authority,
                        reason="supervisor-plan-format-repair",
                    )
                    repair_scheduled = True
                except ValueError:
                    repair_scheduled = False
            else:
                store.record_supervisor_plan_needs_input(
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
                    "repair_scheduled": repair_scheduled,
                }
            )
        except (FencedControllerError, FencedAuthorityError):
            raise
    # Once every planned Worker has returned, create one ordinary natural-
    # language follow-up for the Supervisor.  Planning remains strict JSON so
    # the Hub can validate and dispatch safely; the final user-facing answer
    # is deliberately a normal conversation task.
    results.extend(
        _ensure_supervisor_summaries(
            store,
            run_id=run_id,
            team_spec=team_spec,
            controller=controller,
            authority=authority,
        )
    )
    return results


def _ensure_supervisor_summaries(
    store: SQLiteStateStore,
    *,
    run_id: str,
    team_spec: Any,
    controller: ControllerToken,
    authority: AuthorityToken,
) -> list[dict[str, Any]]:
    """Create at most one natural-language summary task per materialized plan."""
    parent_rows = store.connection.execute(
        """
        SELECT DISTINCT e.task_id
        FROM events e
        JOIN tasks t ON t.task_id = e.task_id
        WHERE e.run_id=? AND e.kind='plan.materialized'
          AND t.task_kind <> 'supervisor_summary'
        ORDER BY e.task_id
        """,
        (run_id,),
    ).fetchall()
    created: list[dict[str, Any]] = []
    supervisor_pool = next(
        (
            pool
            for pool in team_spec.agent_pools
            if str(pool.role_id) == str(team_spec.bootstrap_supervisor)
        ),
        None,
    )
    for parent_row in parent_rows:
        parent_id = str(parent_row["task_id"])
        existing = store.connection.execute(
            "SELECT task_id, state FROM tasks WHERE run_id=? AND parent_task_id=? "
            "AND task_kind='supervisor_summary' LIMIT 1",
            (run_id, parent_id),
        ).fetchone()
        if existing is not None:
            continue
        parent = store.connection.execute(
            """
            SELECT t.state, d.cwd, d.instruction_text,
                   d.required_backend, d.required_model, d.required_provider_id
            FROM tasks t JOIN task_dispatch_specs d ON d.task_id=t.task_id
            WHERE t.run_id=? AND t.task_id=?
            """,
            (run_id, parent_id),
        ).fetchone()
        if parent is None:
            continue
        children = store.connection.execute(
            """
            SELECT t.task_id, t.state, d.instruction_text, d.required_backend,
                   c.state AS call_state, c.result_json, c.failure_json
            FROM tasks t
            JOIN task_dispatch_specs d ON d.task_id=t.task_id
            LEFT JOIN backend_calls c ON c.call_id=(
                SELECT cx.call_id FROM backend_calls cx
                WHERE cx.task_id=t.task_id
                ORDER BY cx.requested_at DESC, cx.call_id DESC LIMIT 1
            )
            WHERE t.run_id=? AND t.parent_task_id=?
              AND t.task_kind <> 'supervisor_summary'
            ORDER BY t.created_at, t.task_id
            """,
            (run_id, parent_id),
        ).fetchall()
        if not children:
            continue
        terminal_states = {"REVIEW", "COMPLETED", "FAILED", "CANCELLED", "BLOCKED"}
        if any(str(child["state"]) not in terminal_states for child in children):
            continue
        if any(child["call_state"] is None for child in children):
            continue
        worker_lines: list[str] = []
        for child in children:
            worker_lines.append(
                f"- {child['task_id']} ({child['call_state']}): "
                f"{_call_text(child['result_json'], child['failure_json'])[:4000]}"
            )
        user_goal = _extract_user_goal(str(parent["instruction_text"] or ""))
        summary_id = f"{parent_id[:48]}-summary"
        prompt = (
            "你是本次任务的主管。请用自然语言向用户汇总 Worker 的结果，"
            "不要返回 JSON，不要创建新任务。先说明用户目标，再逐一列出每个 Worker 的结果，"
            "最后给出一句总体结论。\n\n"
            f"用户目标：{user_goal}\n\nWorker 结果：\n" + "\n".join(worker_lines)
        )
        store.create_task(
            run_id,
            summary_id,
            access_mode="read_only",
            max_attempts=2,
            required_role_id=str(team_spec.bootstrap_supervisor),
            required_backend=(str(parent["required_backend"] or "").strip() or (
                str(supervisor_pool.backend) if supervisor_pool is not None else None
            )),
            required_model=(str(parent["required_model"] or "").strip() or (
                str(supervisor_pool.model) if supervisor_pool is not None and supervisor_pool.model else None
            )),
            required_provider_id=(str(parent["required_provider_id"] or "").strip() or (
                str(supervisor_pool.provider_id) if supervisor_pool is not None and supervisor_pool.provider_id else None
            )),
            prompt=redact_sensitive(prompt),
            cwd=str(parent["cwd"] or "."),
            timeout_seconds=120,
            parent_task_id=parent_id,
            dispatch_source="supervisor_summary",
            required_delivery=False,
            task_kind="supervisor_summary",
            output_contract={"type": "text"},
        )
        store.transition_task(summary_id, TaskState.READY, reason="supervisor-summary-ready")
        created.append(
            {
                "status": "summary-created",
                "supervisor_task_id": parent_id,
                "summary_task_id": summary_id,
                "worker_count": len(children),
            }
        )
    return created


def _extract_user_goal(prompt: str) -> str:
    marker = "USER GOAL:\n"
    if marker in prompt:
        value = prompt.split(marker, 1)[1]
        return value.split("\n\nROLE/POOL DIRECTORY", 1)[0].strip()
    return prompt.strip()


def _call_text(result_json: str | None, failure_json: str | None) -> str:
    try:
        result = json.loads(result_json) if result_json else {}
    except (TypeError, json.JSONDecodeError):
        result = {}
    if isinstance(result, Mapping):
        text = result.get("text")
        if isinstance(text, str) and text.strip():
            return redact_sensitive(text.strip())
        structured = result.get("structured")
        if structured:
            return redact_sensitive(json.dumps(structured, ensure_ascii=False))
    try:
        failure = json.loads(failure_json) if failure_json else {}
    except (TypeError, json.JSONDecodeError):
        failure = {}
    if isinstance(failure, Mapping):
        return redact_sensitive(str(failure.get("message") or failure))
    return "（Worker 没有返回可读内容）"


def _already_processed(
    store: SQLiteStateStore, run_id: str, supervisor_task_id: str, call_id: str
) -> bool:
    row = store.connection.execute(
        """
        SELECT 1 FROM events
        WHERE run_id = ? AND task_id = ?
          AND (
              kind = 'plan.materialized'
              OR (kind IN ('plan.rejected', 'plan.pending', 'plan.needs_input')
                  AND json_extract(data_json, '$.call_id') = ?)
          )
        LIMIT 1
        """,
        (run_id, supervisor_task_id, call_id),
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
