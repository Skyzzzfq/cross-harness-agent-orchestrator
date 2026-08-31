from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from orchestrator.core.models import AttemptState, TaskState
from orchestrator.reconciler import reconcile_once
from orchestrator.storage.sqlite_store import FencedAttemptError, SQLiteStateStore


def _start_and_kill_worker() -> dict[str, Any]:
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(
        (sys.executable, "-c", "import time; time.sleep(30)"),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    time.sleep(0.08)
    was_running = process.poll() is None
    process.kill()
    exit_code = process.wait(timeout=5)
    return {
        "pid": process.pid,
        "was_running_before_kill": was_running,
        "exit_code": exit_code,
        "is_stopped": process.poll() is not None,
    }


def run_recovery_demo(
    cwd: Path,
    *,
    database_path: Path = Path(".agent-hub/state/agent-hub.db"),
) -> dict[str, Any]:
    run_id = f"run-recovery-{uuid.uuid4().hex[:12]}"
    resolved_database = (
        database_path if database_path.is_absolute() else cwd / database_path
    )
    reports = cwd / ".agent-hub" / "reports"
    reports.mkdir(parents=True, exist_ok=True)

    with SQLiteStateStore(resolved_database) as store:
        store.create_run(run_id, "cross-harness-poc")

        requeue_task = f"{run_id}-requeue"
        store.create_task(run_id, requeue_task, max_attempts=2)
        store.transition_task(requeue_task, TaskState.READY, reason="deps-ready")
        attempt_1 = f"{requeue_task}-attempt-1"
        lease_1 = store.create_attempt_with_lease(
            requeue_task, attempt_1, "worker-before-kill"
        )
        store.transition_attempt(
            attempt_1, AttemptState.RUNNING, reason="worker-process-started"
        )
        killed_requeue_worker = _start_and_kill_worker()
        first_reconcile = reconcile_once(
            store,
            now="9999-01-01T00:00:00+00:00",
            run_id=run_id,
        )
        recovery_outcome = first_reconcile["recovered"][0]["outcome"]
        events_after_first_recovery = len(store.events(task_id=requeue_task))
        repeated_reconcile = reconcile_once(
            store,
            now="9999-01-01T00:00:00+00:00",
            run_id=run_id,
        )
        events_after_second_recovery = len(store.events(task_id=requeue_task))

        attempt_2 = f"{requeue_task}-attempt-2"
        lease_2 = store.create_attempt_with_lease(
            requeue_task, attempt_2, "replacement-worker"
        )
        store.transition_attempt(
            attempt_2, AttemptState.RUNNING, reason="replacement-started"
        )
        stale_submit_rejected = False
        try:
            store.submit_attempt(
                attempt_1,
                lease_1["generation"],
                reason="late-result-from-killed-worker",
            )
        except FencedAttemptError:
            stale_submit_rejected = True
        store.submit_attempt(
            attempt_2,
            lease_2["generation"],
            reason="replacement-submitted",
        )
        store.transition_attempt(
            attempt_2, AttemptState.ACCEPTED, reason="review-passed"
        )
        store.transition_task(
            requeue_task, TaskState.COMPLETED, reason="read-only-result-accepted"
        )

        failed_task = f"{run_id}-failed"
        store.create_task(run_id, failed_task, max_attempts=1)
        store.transition_task(failed_task, TaskState.READY, reason="deps-ready")
        failed_attempt = f"{failed_task}-attempt-1"
        failed_lease = store.create_attempt_with_lease(
            failed_task, failed_attempt, "worker-without-retry"
        )
        store.transition_attempt(
            failed_attempt, AttemptState.RUNNING, reason="worker-process-started"
        )
        killed_failed_worker = _start_and_kill_worker()
        failed_reconcile = reconcile_once(
            store,
            now="9999-01-01T00:00:00+00:00",
            run_id=run_id,
        )
        failed_outcome = failed_reconcile["recovered"][0]["outcome"]

        checks = {
            "worker_process_was_killed": killed_requeue_worker["was_running_before_kill"]
            and killed_requeue_worker["is_stopped"],
            "lost_attempt_became_stale": store.attempt_state(attempt_1)
            == AttemptState.STALE,
            "lost_task_was_requeued": recovery_outcome == "requeued",
            "recovery_is_idempotent": repeated_reconcile["recovered"] == []
            and events_after_first_recovery == events_after_second_recovery,
            "replacement_generation_incremented": lease_2["generation"]
            > lease_1["generation"],
            "late_submit_was_fenced": stale_submit_rejected,
            "replacement_completed_task": store.task_state(requeue_task)
            == TaskState.COMPLETED,
            "retry_exhaustion_failed_task": failed_outcome == "failed"
            and store.task_state(failed_task) == TaskState.FAILED,
            "failed_task_reason_recorded": store.task_terminal_reason(failed_task)
            == "assignment_lease_expired",
            "no_worker_process_left_running": killed_requeue_worker["is_stopped"]
            and killed_failed_worker["is_stopped"],
        }
        result = {
            "status": "ready" if all(checks.values()) else "error",
            "mode": "recovery-fake",
            "run_id": run_id,
            "checks": checks,
            "killed_workers": {
                "requeued": killed_requeue_worker,
                "failed": killed_failed_worker,
            },
            "requeued_attempts": {
                "stale": {
                    "attempt_id": attempt_1,
                    "generation": lease_1["generation"],
                },
                "replacement": {
                    "attempt_id": attempt_2,
                    "generation": lease_2["generation"],
                },
            },
            "summary": store.summary(),
        }

    report_path = reports / f"{run_id}.json"
    report_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    result["report_path"] = str(report_path)
    return result
