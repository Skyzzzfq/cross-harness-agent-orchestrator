from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from orchestrator.core.models import utc_now
from orchestrator.storage.sqlite_store import FencedAttemptError, SQLiteStateStore


def reconcile_once(
    store: SQLiteStateStore,
    *,
    now: str | None = None,
    limit: int = 100,
    run_id: str | None = None,
) -> dict[str, Any]:
    cutoff = now or utc_now()
    recovered: list[dict[str, Any]] = []
    concurrently_closed: list[dict[str, Any]] = []
    for candidate in store.expired_active_attempts(
        now=cutoff, limit=limit, run_id=run_id
    ):
        attempt_id = str(candidate["attempt_id"])
        generation = int(candidate["generation"])
        try:
            outcome = store.recover_lost_attempt(
                attempt_id,
                generation,
                reason="assignment_lease_expired",
                expired_at_or_before=cutoff,
            )
        except FencedAttemptError:
            concurrently_closed.append(
                {"attempt_id": attempt_id, "generation": generation}
            )
            continue
        recovered.append(
            {
                "attempt_id": attempt_id,
                "generation": generation,
                "outcome": outcome,
            }
        )
    return {
        "status": "ready",
        "examined": len(recovered) + len(concurrently_closed),
        "recovered": recovered,
        "concurrently_closed": concurrently_closed,
    }


def run_reconciler_once(
    cwd: Path,
    *,
    database_path: Path = Path(".agent-hub/state/agent-hub.db"),
    limit: int = 100,
    run_id: str | None = None,
) -> dict[str, Any]:
    resolved_database = (
        database_path if database_path.is_absolute() else cwd / database_path
    )
    operation_id = f"reconcile-{uuid.uuid4().hex[:12]}"
    with SQLiteStateStore(resolved_database) as store:
        before = store.summary()
        result = reconcile_once(store, limit=limit, run_id=run_id)
        after = store.summary()
    report = {
        **result,
        "mode": "reconcile-once",
        "operation_id": operation_id,
        "database": str(resolved_database),
        "run_id": run_id,
        "summary_before": before,
        "summary_after": after,
    }
    reports = cwd / ".agent-hub" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    report_path = reports / f"{operation_id}.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report["report_path"] = str(report_path)
    return report
