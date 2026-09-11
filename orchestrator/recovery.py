"""Restart-safe recovery inspection for the personal edition.

Recovery is intentionally split into a read-only snapshot and the existing
fenced reconciler.  The snapshot tells the user what a restart would need to
reconcile; ``reconcile`` remains the command that performs lease recovery.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from orchestrator.core.models import utc_now
from orchestrator.storage.sqlite_store import SQLiteStateStore


def _run_ids(store: SQLiteStateStore, run_id: str | None) -> list[str]:
    if run_id:
        row = store.connection.execute(
            "SELECT run_id FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        return [] if row is None else [str(row["run_id"])]
    return [
        str(row["run_id"])
        for row in store.connection.execute(
            "SELECT run_id FROM runs ORDER BY created_at, run_id"
        ).fetchall()
    ]


def recovery_snapshot(
    store: SQLiteStateStore,
    *,
    run_id: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Inspect leases and in-flight records without changing database state."""

    cutoff = now or utc_now()
    runs: list[dict[str, Any]] = []
    for current_run_id in _run_ids(store, run_id):
        active_controller = store.active_run_controller(current_run_id)
        active_authority = store.active_authority(current_run_id)
        expired_attempts = store.expired_active_attempts(
            now=cutoff, run_id=current_run_id, limit=100
        )
        starting_calls = store.starting_backend_calls(run_id=current_run_id)
        queued_deliveries = store.list_message_deliveries(
            current_run_id, status="QUEUED"
        )
        delivered_deliveries = store.list_message_deliveries(
            current_run_id, status="DELIVERED"
        )
        reserved_budget = store.list_budget_reservations(
            current_run_id, status="RESERVED"
        )
        merge_rows = store.connection.execute(
            "SELECT merge_id, status, claim_owner FROM merge_queue "
            "WHERE run_id=? AND status IN ('PENDING','APPLYING') "
            "ORDER BY created_at, merge_id",
            (current_run_id,),
        ).fetchall()
        outbox_rows = store.connection.execute(
            "SELECT outbox_id, status, attempts, available_at FROM outbox "
            "WHERE run_id=? AND status='PENDING' ORDER BY created_at, outbox_id",
            (current_run_id,),
        ).fetchall()
        active_tasks = store.connection.execute(
            "SELECT task_id, state FROM tasks WHERE run_id=? "
            "AND state IN ('ACTIVE','INTEGRATION','CANCEL_REQUESTED') "
            "ORDER BY created_at, task_id",
            (current_run_id,),
        ).fetchall()
        blockers = []
        if expired_attempts:
            blockers.append("expired_assignment_leases")
        if starting_calls:
            blockers.append("inflight_backend_calls")
        if active_tasks:
            blockers.append("active_tasks")
        runs.append(
            {
                "run_id": current_run_id,
                "controller": active_controller,
                "authority": active_authority,
                "active_tasks": [dict(row) for row in active_tasks],
                "expired_attempts": expired_attempts,
                "starting_calls": starting_calls,
                "queued_deliveries": [dict(row) for row in queued_deliveries],
                "delivered_deliveries": [dict(row) for row in delivered_deliveries],
                "reserved_budget": [dict(row) for row in reserved_budget],
                "pending_merges": [dict(row) for row in merge_rows],
                "pending_outbox": [dict(row) for row in outbox_rows],
                "recovery_required": bool(blockers),
                "blockers": blockers,
            }
        )
    return {
        "status": "ready",
        "observed_at": cutoff,
        "run_id": run_id,
        "runs": runs,
        "recovery_required": any(item["recovery_required"] for item in runs),
        "read_only": True,
    }


def run_recovery_check(
    cwd: Path,
    *,
    database_path: Path = Path(".agent-hub/state/agent-hub.db"),
    run_id: str | None = None,
) -> dict[str, Any]:
    """Write a timestamped, credential-free recovery inspection report."""

    resolved_database = (
        database_path if database_path.is_absolute() else cwd / database_path
    )
    operation_id = f"recovery-{uuid.uuid4().hex[:12]}"
    with SQLiteStateStore(resolved_database) as store:
        report = recovery_snapshot(store, run_id=run_id)
        report["database"] = str(resolved_database)
    report["operation_id"] = operation_id
    report_path = cwd / ".agent-hub" / "reports" / f"{operation_id}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report["report_path"] = str(report_path)
    return report
