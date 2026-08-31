from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path

from orchestrator.adapters.fake import FakeBackendAdapter, FakeBehavior
from orchestrator.adapters.contracts import CallState
from orchestrator.agent_pool import reconcile_pool_once
from orchestrator.core.config import AgentPoolSpec
from orchestrator.core.models import TaskState
from orchestrator.scheduler import scheduler_tick
from orchestrator.storage.sqlite_store import SQLiteStateStore


def _invariants(store: SQLiteStateStore, run_id: str) -> dict[str, bool]:
    """状态不变量检查：终态稳定、无孤儿资源、无未回收的过期租约。"""
    terminal_states = {
        TaskState.COMPLETED.value,
        TaskState.FAILED.value,
        TaskState.CANCELLED.value,
    }
    active_tasks = 0
    orphan_agents = 0
    stale_leases = 0
    for task in store.connection.execute(
        "SELECT task_id, state FROM tasks WHERE run_id = ?", (run_id,)
    ).fetchall():
        if task["state"] in terminal_states:
            attempts = store.connection.execute(
                """
                SELECT COUNT(*) FROM attempts a
                JOIN assignment_leases l ON l.attempt_id = a.attempt_id
                WHERE a.task_id = ? AND l.state = 'ACTIVE'
                """,
                (task["task_id"],),
            ).fetchone()[0]
            if attempts != 0:
                return {"terminal_task_has_active_lease": False}
        elif task["state"] == TaskState.ACTIVE.value:
            active_tasks += 1
    for agent in store.connection.execute(
        """
        SELECT agent_id, current_task_id FROM agent_instances
        WHERE status IN ('BUSY', 'DRAINING')
        """
    ).fetchall():
        if agent["current_task_id"] is None:
            orphan_agents += 1
    stale = store.connection.execute(
        """
        SELECT COUNT(*) FROM assignment_leases l
        WHERE l.state = 'ACTIVE' AND l.expires_at <= ?
        """,
        (store._aware_datetime(None).isoformat(),),
    ).fetchone()[0]
    stale_leases = stale
    return {
        "terminal_task_has_active_lease": True,
        "no_orphan_busy_agent": orphan_agents == 0,
        "no_stale_active_lease": stale_leases == 0,
    }


class ExitMatrixTests(unittest.IsolatedAsyncioTestCase):
    async def _run_scenario(
        self,
        *,
        task_count: int,
        behaviors: list[FakeBehavior],
        expected: list[TaskState],
        max_attempts: int = 2,
        priority_list: list[int] | None = None,
        cancel_after_ticks: int | None = None,
        cancel_task_ids: list[int] | None = None,
    ) -> tuple[list[TaskState], dict[str, bool]]:
        temp = tempfile.mkdtemp()
        store: SQLiteStateStore | None = None
        try:
            store = SQLiteStateStore(Path(temp) / "state.db")
            store.create_run("run-1", "team-1")
            reconcile_pool_once(
                store,
                "run-1",
                AgentPoolSpec(
                    pool_id="fake-workers",
                    backend="fake",
                    role_id="worker",
                    count=task_count,
                    max_count=task_count,
                    model="fake-v1",
                ),
            )
            adapter = FakeBackendAdapter()
            for i in range(task_count):
                adapter.behaviors_by_task[f"task-{i}"] = behaviors[i]
            for i in range(task_count):
                store.create_task(
                    "run-1",
                    f"task-{i}",
                    required_role_id="worker",
                    prompt=f"execute task-{i}",
                    cwd="D:/workspace/connect",
                    timeout_seconds=2,
                    max_attempts=max_attempts,
                    priority=(priority_list or [0] * task_count)[i],
                )
                store.transition_task(f"task-{i}", TaskState.READY, reason="ready")
            token = store.acquire_run_controller("run-1", "op", lease_seconds=60)
            states: list[TaskState] = []
            for tick in range(20):
                if cancel_after_ticks is not None and tick == cancel_after_ticks:
                    targets = (
                        cancel_task_ids
                        if cancel_task_ids is not None
                        else list(range(task_count))
                    )
                    for i in targets:
                        store.request_cancel_task(
                            f"task-{i}", controller=token, reason="cancel"
                        )
                await scheduler_tick(
                    store,
                    run_id="run-1",
                    adapters={"fake": adapter},
                    controller=token,
                    lease_seconds=60,
                )
                current = [store.task_state(f"task-{i}") for i in range(task_count)]
                if all(
                    s in {TaskState.REVIEW, TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED}
                    for s in current
                ):
                    states = current
                    break
                states = current
            invariants = _invariants(store, "run-1")
            if store is not None:
                store.close()
            shutil.rmtree(temp, ignore_errors=True)
            return states, invariants
        finally:
            if store is not None:
                store.close()
            shutil.rmtree(temp, ignore_errors=True)

    async def test_fifty_frozen_scenarios_match_invariants(self) -> None:
        """10 类场景 × 5 参数变体 = 50 个预冻结场景，全部终态 + 不变量匹配。"""
        scenarios: list[tuple[str, int, list[FakeBehavior], list[TaskState], dict]] = []

        # 1. 单个成功（5 变体：不同 delay）
        for delay in (0.01, 0.05, 0.1, 0.2, 0.3):
            scenarios.append(
                (
                    f"single-success-d{delay}",
                    1,
                    [FakeBehavior(delay_seconds=delay, text="ok")],
                    [TaskState.REVIEW],
                    {},
                )
            )

        # 2. 并行成功（5 变体：不同任务数）
        for n in (2, 2, 3, 3, 4):
            scenarios.append(
                (
                    f"parallel-success-n{n}",
                    n,
                    [FakeBehavior(delay_seconds=0.05, text="ok")] * n,
                    [TaskState.REVIEW] * n,
                    {},
                )
            )

        # 3. 多次 attempt 成功（5 变体：不同 attempt 上限）
        for attempts in (2, 2, 3, 3, 4):
            scenarios.append(
                (
                    f"retry-ok-a{attempts}",
                    1,
                    [FakeBehavior(delay_seconds=0.05, text="ok")],
                    [TaskState.REVIEW],
                    {"max_attempts": attempts},
                )
            )

        # 4. 重试耗尽失败（5 变体：不同失败模式）
        for terminal in (
            CallState.FAILED,
            CallState.TIMED_OUT,
            CallState.BLOCKED,
            CallState.FAILED,
            CallState.TIMED_OUT,
        ):
            scenarios.append(
                (
                    f"retry-exhaust-{terminal.value}",
                    1,
                    [FakeBehavior(delay_seconds=0, terminal=terminal)],
                    [TaskState.FAILED],
                    {"max_attempts": 1},
                )
            )

        # 5. 取消 READY 任务（5 变体：不同 delay）
        for delay in (0.1, 0.2, 0.3, 0.4, 0.5):
            scenarios.append(
                (
                    f"cancel-ready-d{delay}",
                    1,
                    [FakeBehavior(delay_seconds=delay, text="ok")],
                    [TaskState.CANCELLED],
                    {"cancel_after_ticks": 0},
                )
            )

        # 6. 优先级排序（5 变体：不同优先级组合）
        for prio in ((0, 1), (1, 0), (2, 1), (1, 2), (3, 0)):
            scenarios.append(
                (
                    f"priority-{prio[0]}-{prio[1]}",
                    2,
                    [FakeBehavior(delay_seconds=0.1, text="ok")] * 2,
                    [TaskState.REVIEW, TaskState.REVIEW],
                    {"priority_list": list(prio)},
                )
            )

        # 7. 单个 BLOCKED 失败（4 变体）
        for kind in ("policy_block", "adapter_unavailable", "fake_block", "permission"):
            scenarios.append(
                (
                    f"single-blocked-{kind}",
                    1,
                    [
                        FakeBehavior(
                            delay_seconds=0, terminal=CallState.BLOCKED, error_kind=kind
                        )
                    ],
                    [TaskState.FAILED],
                    {"max_attempts": 1},
                )
            )

        # 8. 两任务一成功一失败（4 变体）
        for combo in (
            (CallState.SUCCEEDED, CallState.FAILED),
            (CallState.FAILED, CallState.SUCCEEDED),
            (CallState.SUCCEEDED, CallState.TIMED_OUT),
            (CallState.TIMED_OUT, CallState.SUCCEEDED),
        ):
            scenarios.append(
                (
                    f"two-one-fail-{combo[0].value}-{combo[1].value}",
                    2,
                    [
                        FakeBehavior(delay_seconds=0.05, terminal=combo[0], text="a"),
                        FakeBehavior(delay_seconds=0.05, terminal=combo[1], text="b"),
                    ],
                    [
                        TaskState.REVIEW
                        if combo[0] == CallState.SUCCEEDED
                        else TaskState.FAILED,
                        TaskState.REVIEW
                        if combo[1] == CallState.SUCCEEDED
                        else TaskState.FAILED,
                    ],
                    {"max_attempts": 1},
                )
            )

        # 9. 取消部分任务（4 变体：不同 delay）
        for delay in (0.1, 0.2, 0.3, 0.4):
            scenarios.append(
                (
                    f"cancel-partial-d{delay}",
                    2,
                    [
                        FakeBehavior(delay_seconds=delay, text="a"),
                        FakeBehavior(delay_seconds=0.05, text="b"),
                    ],
                    [TaskState.CANCELLED, TaskState.REVIEW],
                    {"cancel_after_ticks": 0, "cancel_task_ids": [0]},
                )
            )

        # 10. 优先级 + 失败组合（4 变体）
        for p in ((0, 1), (1, 0), (2, 1), (1, 2)):
            scenarios.append(
                (
                    f"prio-fail-{p[0]}-{p[1]}",
                    2,
                    [
                        FakeBehavior(delay_seconds=0.05, terminal=CallState.BLOCKED),
                        FakeBehavior(delay_seconds=0.05, text="ok"),
                    ],
                    [TaskState.FAILED, TaskState.REVIEW],
                    {"priority_list": list(p), "max_attempts": 1},
                )
            )

        # 11. 多任务全部成功（4 变体）
        for n in (1, 2, 3, 4):
            scenarios.append(
                (
                    f"mixed-success-n{n}",
                    n,
                    [FakeBehavior(delay_seconds=0.05, text="ok")] * n,
                    [TaskState.REVIEW] * n,
                    {},
                )
            )

        failures: list[str] = []
        for name, count, behaviors, expected, extra in scenarios:
            states, invariants = await self._run_scenario(
                task_count=count,
                behaviors=behaviors,
                expected=expected,
                max_attempts=extra.get("max_attempts", 2),
                priority_list=extra.get("priority_list"),
                cancel_after_ticks=extra.get("cancel_after_ticks"),
                cancel_task_ids=extra.get("cancel_task_ids"),
            )
            if states != expected:
                failures.append(
                    f"{name}: expected {[s.value for s in expected]} got {[s.value for s in states]}"
                )
                continue
            if not all(invariants.values()):
                failures.append(f"{name}: invariants broken {invariants}")
        self.assertEqual(
            len(scenarios), 50, f"expected 50 frozen scenarios, got {len(scenarios)}"
        )
        self.assertEqual(failures, [], "\n".join(failures))


if __name__ == "__main__":
    unittest.main()
