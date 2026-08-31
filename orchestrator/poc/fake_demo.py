from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.core.models import (
    AttemptState,
    MessageEnvelope,
    Recipient,
    TaskState,
)
from orchestrator.storage.sqlite_store import SQLiteStateStore


@dataclass(frozen=True)
class FakeExecution:
    task_id: str
    attempt_id: str
    agent_id: str
    started_at: float
    ended_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "agent_id": self.agent_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_seconds": self.ended_at - self.started_at,
        }


async def _execute_fake_attempt(
    store: SQLiteStateStore,
    *,
    run_id: str,
    team_id: str,
    task_id: str,
    attempt_id: str,
    generation: int,
    agent_id: str,
    delay: float,
) -> FakeExecution:
    store.transition_attempt(
        attempt_id, AttemptState.RUNNING, reason="fake-worker-started"
    )
    started_at = time.monotonic()
    await asyncio.sleep(delay)
    ended_at = time.monotonic()
    store.submit_attempt(
        attempt_id, generation, reason="fake-artifact-submitted"
    )
    store.append_message(
        MessageEnvelope(
            message_id=f"msg-{uuid.uuid4()}",
            team_id=team_id,
            run_id=run_id,
            task_id=task_id,
            sender_agent_id=agent_id,
            recipients=(Recipient("role", "supervisor"),),
            kind="artifact.submitted",
            payload={"attempt_id": attempt_id},
            correlation_id=f"corr-{task_id}",
            idempotency_key=f"submit-{attempt_id}",
        )
    )
    return FakeExecution(
        task_id=task_id,
        attempt_id=attempt_id,
        agent_id=agent_id,
        started_at=started_at,
        ended_at=ended_at,
    )


def _accept(
    store: SQLiteStateStore, task_id: str, attempt_id: str, *, reason: str
) -> None:
    store.transition_attempt(attempt_id, AttemptState.ACCEPTED, reason=reason)
    store.transition_task(task_id, TaskState.INTEGRATION, reason=reason)
    store.transition_task(
        task_id, TaskState.COMPLETED, reason="fake-deterministic-test-passed"
    )


async def _run_fake_demo(cwd: Path, database_path: Path) -> dict[str, Any]:
    run_id = f"run-fake-{uuid.uuid4().hex[:12]}"
    team_id = "cross-harness-poc"
    resolved_database = (
        database_path if database_path.is_absolute() else cwd / database_path
    )
    report_dir = cwd / ".agent-hub" / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)

    with SQLiteStateStore(resolved_database) as store:
        store.create_run(run_id, team_id)
        for task_id, write_scope in (
            ("worker-a", ("demo/a.txt",)),
            ("worker-b", ("demo/b.txt",)),
        ):
            store.create_task(
                run_id,
                f"{run_id}-{task_id}",
                access_mode="write",
                write_scope=write_scope,
            )
            store.transition_task(
                f"{run_id}-{task_id}", TaskState.READY, reason="dependencies-ready"
            )

        task_a = f"{run_id}-worker-a"
        task_b = f"{run_id}-worker-b"
        attempt_a1 = f"{task_a}-attempt-1"
        attempt_b1 = f"{task_b}-attempt-1"
        store.create_attempt(task_a, attempt_a1, "codebuddy-worker-a")
        store.create_attempt(task_b, attempt_b1, "codebuddy-worker-b")
        generation_a1 = store.attempt_generation(attempt_a1)
        generation_b1 = store.attempt_generation(attempt_b1)

        first_a, first_b = await asyncio.gather(
            _execute_fake_attempt(
                store,
                run_id=run_id,
                team_id=team_id,
                task_id=task_a,
                attempt_id=attempt_a1,
                generation=generation_a1,
                agent_id="codebuddy-worker-a",
                delay=0.08,
            ),
            _execute_fake_attempt(
                store,
                run_id=run_id,
                team_id=team_id,
                task_id=task_b,
                attempt_id=attempt_b1,
                generation=generation_b1,
                agent_id="codebuddy-worker-b",
                delay=0.08,
            ),
        )
        overlap_seconds = min(first_a.ended_at, first_b.ended_at) - max(
            first_a.started_at, first_b.started_at
        )

        _accept(store, task_a, attempt_a1, reason="review-passed")
        store.transition_attempt(
            attempt_b1, AttemptState.REJECTED, reason="review-requested-rework"
        )
        store.transition_task(task_b, TaskState.READY, reason="review-requested-rework")

        attempt_b2 = f"{task_b}-attempt-2"
        attempt_number = store.create_attempt(
            task_b, attempt_b2, "codebuddy-worker-b"
        )
        generation_b2 = store.attempt_generation(attempt_b2)
        rework = await _execute_fake_attempt(
            store,
            run_id=run_id,
            team_id=team_id,
            task_id=task_b,
            attempt_id=attempt_b2,
            generation=generation_b2,
            agent_id="codebuddy-worker-b",
            delay=0.02,
        )
        _accept(store, task_b, attempt_b2, reason="rework-review-passed")

        checks = {
            "workers_overlapped": overlap_seconds > 0,
            "worker_a_completed": store.task_state(task_a) == TaskState.COMPLETED,
            "worker_b_completed": store.task_state(task_b) == TaskState.COMPLETED,
            "worker_b_first_attempt_rejected": store.attempt_state(attempt_b1)
            == AttemptState.REJECTED,
            "worker_b_rework_created": attempt_number == 2,
            "worker_b_rework_accepted": store.attempt_state(attempt_b2)
            == AttemptState.ACCEPTED,
        }
        result = {
            "status": "ready" if all(checks.values()) else "error",
            "mode": "fake",
            "run_id": run_id,
            "checks": checks,
            "overlap_seconds": overlap_seconds,
            "initial_executions": [first_a.to_dict(), first_b.to_dict()],
            "rework_execution": rework.to_dict(),
            "summary": store.summary(),
        }

    report_path = report_dir / f"{run_id}.json"
    report_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    result["report_path"] = str(report_path)
    return result


def run_fake_demo(
    cwd: Path,
    *,
    database_path: Path = Path(".agent-hub/state/agent-hub.db"),
) -> dict[str, Any]:
    return asyncio.run(_run_fake_demo(cwd, database_path))
