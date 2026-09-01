from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orchestrator.adapters.contracts import BackendAdapter
from orchestrator.adapters.real import CodeBuddyBackendAdapter, CodexBackendAdapter
from orchestrator.agent_pool import reconcile_pool_once
from orchestrator.core.config import AgentPoolSpec
from orchestrator.core.models import TaskState
from orchestrator.scheduler import scheduler_tick
from orchestrator.storage.sqlite_store import SQLiteStateStore

# 冻结场景：每个场景是一个只读任务，要求模型输出精确 marker。
# 厂商故障（adapter 层 BLOCKED/ERROR/配额）与模型内容质量分开统计。
FROZEN_SCENARIOS: tuple[tuple[str, str, str], ...] = (
    # (backend, task_id, expected_marker)
    ("codex", "scenario-01-codex-readonly", "MARKER_CODEX_01"),
    ("codex", "scenario-02-codex-readonly", "MARKER_CODEX_02"),
    ("codebuddy", "scenario-03-codebuddy-readonly", "MARKER_CODEBUDDY_01"),
    ("codebuddy", "scenario-04-codebuddy-readonly", "MARKER_CODEBUDDY_02"),
    ("codebuddy", "scenario-05-codebuddy-readonly", "MARKER_CODEBUDDY_03"),
    ("codex", "scenario-06-codex-readonly", "MARKER_CODEX_03"),
    ("codex", "scenario-07-codex-readonly", "MARKER_CODEX_04"),
    ("codebuddy", "scenario-08-codebuddy-readonly", "MARKER_CODEBUDDY_04"),
    ("codex", "scenario-09-codex-readonly", "MARKER_CODEX_05"),
    ("codebuddy", "scenario-10-codebuddy-readonly", "MARKER_CODEBUDDY_05"),
)


def _build_adapters(cwd: Path) -> dict[str, BackendAdapter]:
    return {
        "codex": CodexBackendAdapter(),
        "codebuddy": CodeBuddyBackendAdapter(),
    }


def _task_prompt(marker: str) -> str:
    return (
        "Reply with exactly the marker text: "
        f"{marker}. Do not call tools and do not modify any files. "
        "Your entire reply must be only that marker."
    )


async def _run_scenarios(
    cwd: Path,
    database_path: Path,
    *,
    run_id: str,
    adapters: Mapping[str, BackendAdapter],
    lease_seconds: int = 60,
    max_ticks: int = 40,
    scenarios: tuple[tuple[str, str, str], ...] = FROZEN_SCENARIOS,
) -> dict[str, Any]:
    resolved = database_path if database_path.is_absolute() else cwd / database_path
    with SQLiteStateStore(resolved) as store:
        store.create_run(run_id, "cross-harness-poc")
        pool_ids: set[str] = set()
        for backend, task_id, marker in scenarios:
            pool_id = f"pool-{backend}"
            if pool_id not in pool_ids:
                pool_ids.add(pool_id)
                reconcile_pool_once(
                    store,
                    run_id,
                    AgentPoolSpec(
                        pool_id=pool_id,
                        backend=backend,
                        role_id="worker",
                        count=1,
                        max_count=1,
                        model="real",
                    ),
                )
            store.create_task(
                run_id,
                task_id,
                required_role_id="worker",
                prompt=_task_prompt(marker),
                cwd=str(cwd),
                timeout_seconds=60,
            )
            store.transition_task(task_id, TaskState.READY, reason="frozen-scenario")

        results: dict[str, Any] = {}
        authority = store.acquire_authority(run_id, "stage2-supervisor", "supervisor")
        for _ in range(max_ticks):
            await scheduler_tick(
                store,
                run_id=run_id,
                adapters=adapters,
                authority=authority,
                lease_seconds=lease_seconds,
                controller_lease_seconds=300,
            )
            states = {task_id: store.task_state(task_id) for _, task_id, _ in scenarios}
            if all(
                state
                in {TaskState.REVIEW, TaskState.COMPLETED, TaskState.FAILED}
                for state in states.values()
            ):
                break

        for backend, task_id, marker in scenarios:
            task_state = store.task_state(task_id)
            calls = store.connection.execute(
                """
                SELECT state, disposition, late_result, failure_json, result_json
                FROM backend_calls WHERE task_id = ?
                ORDER BY requested_at DESC LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            failure_kind = None
            if calls and calls["failure_json"]:
                try:
                    failure_kind = json.loads(calls["failure_json"]).get("kind")
                except (json.JSONDecodeError, TypeError):
                    failure_kind = None
            text = ""
            if calls and calls["result_json"]:
                try:
                    text = str(json.loads(calls["result_json"]).get("text", ""))
                except (json.JSONDecodeError, TypeError):
                    text = ""
            results[task_id] = {
                "backend": backend,
                "expected_marker": marker,
                "task_state": task_state.value,
                "call_state": calls["state"] if calls else None,
                "disposition": calls["disposition"] if calls else None,
                "late_result": bool(calls["late_result"]) if calls else None,
                "failure_kind": failure_kind,
                "content_matched": marker in text if text else False,
            }
        return {"run_id": run_id, "results": results, "summary": store.summary(run_id=run_id)}


async def _run_mixed_parallel(
    cwd: Path,
    database_path: Path,
    *,
    run_id: str,
    adapters: Mapping[str, BackendAdapter],
    lease_seconds: int = 60,
    max_ticks: int = 20,
) -> dict[str, Any]:
    """Run 2 CodeBuddy workers + 1 Codex worker in one Run and verify they
    execute with real time overlap (parallel dispatch through the unified
    scheduler)."""
    import time

    resolved = database_path if database_path.is_absolute() else cwd / database_path
    pool_specs = [
        AgentPoolSpec(
            pool_id="codebuddy-workers",
            backend="codebuddy",
            role_id="worker",
            count=2,
            max_count=2,
            model="real",
        ),
        AgentPoolSpec(
            pool_id="codex-reviewer",
            backend="codex",
            role_id="worker",
            count=1,
            max_count=1,
            model="real",
        ),
    ]
    markers = [
        ("codebuddy", "mixed-worker-a", "MARKER_MIXED_A"),
        ("codebuddy", "mixed-worker-b", "MARKER_MIXED_B"),
        ("codex", "mixed-reviewer", "MARKER_MIXED_C"),
    ]
    with SQLiteStateStore(resolved) as store:
        store.create_run(run_id, "cross-harness-poc")
        for spec in pool_specs:
            reconcile_pool_once(store, run_id, spec)
        for backend, task_id, marker in markers:
            store.create_task(
                run_id,
                task_id,
                required_role_id="worker",
                prompt=_task_prompt(marker),
                cwd=str(cwd),
                timeout_seconds=90,
            )
            store.transition_task(task_id, TaskState.READY, reason="mixed-parallel")

        started = time.monotonic()
        authority = store.acquire_authority(run_id, "stage2-supervisor", "supervisor")
        for _ in range(max_ticks):
            await scheduler_tick(
                store,
                run_id=run_id,
                adapters=adapters,
                authority=authority,
                lease_seconds=lease_seconds,
                controller_lease_seconds=300,
            )
            states = {tid: store.task_state(tid) for _, tid, _ in markers}
            if all(
                s in {TaskState.REVIEW, TaskState.COMPLETED, TaskState.FAILED}
                for s in states.values()
            ):
                break
        elapsed = time.monotonic() - started

        results: dict[str, Any] = {}
        for _, task_id, marker in markers:
            row = store.connection.execute(
                """
                SELECT state, disposition, late_result, started_at, finished_at,
                       failure_json, result_json
                FROM backend_calls WHERE task_id = ?
                ORDER BY requested_at DESC LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            text = ""
            if row and row["result_json"]:
                try:
                    text = str(json.loads(row["result_json"]).get("text", ""))
                except (json.JSONDecodeError, TypeError):
                    text = ""
            results[task_id] = {
                "task_state": store.task_state(task_id).value,
                "call_state": row["state"] if row else None,
                "disposition": row["disposition"] if row else None,
                "content_matched": marker in text if text else False,
                "started_at": row["started_at"] if row else None,
                "finished_at": row["finished_at"] if row else None,
            }
        windows = [
            (results[tid]["started_at"], results[tid]["finished_at"])
            for _, tid, _ in markers
            if results[tid]["started_at"] and results[tid]["finished_at"]
        ]
        overlapped = False
        if len(windows) >= 2:
            try:
                from datetime import datetime

                parsed = [
                    (
                        datetime.fromisoformat(start),
                        datetime.fromisoformat(end),
                    )
                    for start, end in windows
                ]
                for i in range(len(parsed)):
                    for j in range(i + 1, len(parsed)):
                        s1, e1 = parsed[i]
                        s2, e2 = parsed[j]
                        if s1 < e2 and s2 < e1:
                            overlapped = True
            except (ValueError, TypeError):
                overlapped = False
        all_review = all(
            item["task_state"] in {"REVIEW", "COMPLETED"}
            for item in results.values()
        )
        return {
            "run_id": run_id,
            "mode": "stage2-mixed-parallel",
            "elapsed_seconds": round(elapsed, 2),
            "results": results,
            "workers_overlapped": overlapped,
            "all_terminal_correct": all_review,
            "status": (
                "ready"
                if all_review and overlapped
                and all(item["content_matched"] for item in results.values())
                else "pending"
            ),
            "summary": store.summary(run_id=run_id),
        }


def run_mixed_parallel(
    cwd: Path,
    *,
    database_path: Path = Path(".agent-hub/state/agent-hub.db"),
    run_id: str | None = None,
    max_ticks: int = 20,
) -> dict[str, Any]:
    run = run_id or f"run-stage2-mixed-{uuid.uuid4().hex[:12]}"
    report_dir = cwd / ".agent-hub" / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    result = asyncio.run(
        _run_mixed_parallel(
            cwd,
            database_path,
            run_id=run,
            adapters=_build_adapters(cwd),
            max_ticks=max_ticks,
        )
    )
    report_path = report_dir / f"{run}.json"
    report_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    result["report_path"] = str(report_path)
    return result


def run_stage2_real(
    cwd: Path,
    *,
    database_path: Path = Path(".agent-hub/state/agent-hub.db"),
    run_id: str | None = None,
    max_ticks: int = 40,
    backends: tuple[str, ...] = ("codex", "codebuddy"),
) -> dict[str, Any]:
    run = run_id or f"run-stage2-real-{uuid.uuid4().hex[:12]}"
    report_dir = cwd / ".agent-hub" / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    scenarios = tuple(item for item in FROZEN_SCENARIOS if item[0] in backends)
    adapters = _build_adapters(cwd)
    result = asyncio.run(
        _run_scenarios(
            cwd,
            database_path,
            run_id=run,
            adapters=adapters,
            max_ticks=max_ticks,
            scenarios=scenarios,
        )
    )
    # P1-01：终态口径修正——REVIEW 是 adapter 调用完成点，不是 Task 终态。
    # 分别统计：adapter 完成（到 REVIEW/终态）、Task 终态、完整流水线终态（COMPLETED）。
    adapter_completed = [
        item
        for item in result["results"].values()
        if item["task_state"] in {"REVIEW", "COMPLETED", "FAILED", "CANCELLED"}
    ]
    at_review = [
        item
        for item in result["results"].values()
        if item["task_state"] == "REVIEW"
    ]
    task_terminal = [
        item
        for item in result["results"].values()
        if item["task_state"] in {"COMPLETED", "FAILED", "CANCELLED"}
    ]
    full_pipeline_terminal = [
        item
        for item in result["results"].values()
        if item["task_state"] == "COMPLETED"
    ]
    vendor_faults = [
        item
        for item in result["results"].values()
        if item["task_state"] == "FAILED"
        and item["failure_kind"]
        in {"sdk_error", "adapter_unavailable", "cli_unavailable", "interactive_login_required", "model_error", "empty_result"}
    ]
    quality_failures = [
        item
        for item in result["results"].values()
        if item["task_state"] == "REVIEW"
        and item["call_state"] == "succeeded"
        and item["disposition"] == "submitted"
        and not item.get("content_matched", True)
    ]
    result.update(
        {
            "mode": "stage2-real",
            "scenarios_frozen": len(scenarios),
            "scenarios_adapter_completed": len(adapter_completed),
            "scenarios_at_review": len(at_review),
            "scenarios_task_terminal": len(task_terminal),
            "scenarios_full_pipeline_terminal": len(full_pipeline_terminal),
            "vendor_faults": vendor_faults,
            "quality_failures": quality_failures,
            "status": (
                "ready"
                if len(adapter_completed) >= len(scenarios)
                and len(vendor_faults) + len(quality_failures) == 0
                else "pending"
            ),
        }
    )
    report_path = report_dir / f"{run}.json"
    report_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    result["report_path"] = str(report_path)
    return result
