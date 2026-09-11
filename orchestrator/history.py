"""Read-only history retention helpers for the personal edition.

The first release deliberately exposes a preview instead of deleting Runs.  A
preview makes the retention decision visible while preserving task events,
artifacts and verification evidence until a future, explicitly approved
archive format exists.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from orchestrator.storage.sqlite_store import SQLiteStateStore

def _cutoff(*, older_than_days: int, now: str | None) -> str:
    if older_than_days < 0:
        raise ValueError("older_than_days must be non-negative")
    if now:
        parsed = datetime.fromisoformat(now)
        if parsed.tzinfo is None:
            raise ValueError("now must include a timezone")
    else:
        parsed = datetime.now(timezone.utc)
    return (parsed.astimezone(timezone.utc) - timedelta(days=older_than_days)).isoformat()


def history_cleanup_preview(
    store: SQLiteStateStore,
    *,
    older_than_days: int = 30,
    now: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Return a non-destructive list of Runs that could be archived later.

    A Run is eligible only when it is older than the requested cutoff and has
    no non-terminal task.  The function never deletes rows or worktree files;
    active Runs are returned with a blocking reason for troubleshooting.
    """

    cutoff = _cutoff(older_than_days=older_than_days, now=now)
    query = """
        SELECT r.run_id, r.team_id, r.created_at, r.approval_mode,
               COUNT(t.task_id) AS task_count,
               SUM(CASE WHEN t.state NOT IN ('COMPLETED', 'FAILED', 'CANCELLED')
                        THEN 1 ELSE 0 END) AS active_task_count,
               (SELECT COUNT(*) FROM events e WHERE e.run_id=r.run_id) AS event_count,
               (SELECT COUNT(*) FROM artifacts a WHERE a.run_id=r.run_id) AS artifact_count,
               (SELECT COUNT(*) FROM verification_evidence v WHERE v.run_id=r.run_id) AS evidence_count
        FROM runs r
        LEFT JOIN tasks t ON t.run_id=r.run_id
        WHERE (? IS NULL OR r.run_id=?)
        GROUP BY r.run_id
        ORDER BY r.created_at, r.run_id
    """
    rows = store.connection.execute(query, (run_id, run_id)).fetchall()
    entries: list[dict[str, Any]] = []
    for row in rows:
        created_at = str(row["created_at"] or "")
        old_enough = created_at <= cutoff
        active_count = int(row["active_task_count"] or 0)
        reasons: list[str] = []
        if not old_enough:
            reasons.append("younger_than_cutoff")
        if active_count:
            reasons.append("active_tasks")
        entries.append(
            {
                "run_id": str(row["run_id"]),
                "team_id": str(row["team_id"]),
                "created_at": created_at,
                "approval_mode": row["approval_mode"],
                "task_count": int(row["task_count"] or 0),
                "active_task_count": active_count,
                "event_count": int(row["event_count"] or 0),
                "artifact_count": int(row["artifact_count"] or 0),
                "evidence_count": int(row["evidence_count"] or 0),
                "eligible": not reasons,
                "reasons": reasons,
            }
        )
    return {
        "status": "preview",
        "cutoff": cutoff,
        "older_than_days": older_than_days,
        "run_id": run_id,
        "runs": entries,
        "eligible_count": sum(1 for item in entries if item["eligible"]),
        "blocked_count": sum(1 for item in entries if not item["eligible"]),
        "destructive_action": False,
    }
