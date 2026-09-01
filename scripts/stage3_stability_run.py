"""T1：24h Fake 稳定运行脚本（阶段 3）。

复用 serve（调度）+ MergeExecutor/OutboxDispatcher（集成/投递），持续注入
Fake 写任务，周期校验不变量（0 丢任务、0 重复 merge、0 重复通知），并把
汇总报告写入 .agent-hub/reports/stage3-stability.json。

用法：
    & '.venv\\Scripts\\python.exe' scripts/stage3_stability_run.py --hours 24 --tasks 600
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time
from pathlib import Path
from typing import Any

from orchestrator.adapters.fake import FakeBackendAdapter, FakeBehavior
from orchestrator.agent_pool import reconcile_pool_once
from orchestrator.core.config import AgentPoolSpec
from orchestrator.core.models import TaskState
from orchestrator.storage.sqlite_store import SQLiteStateStore
from orchestrator.workspace.git_manager import GitWorkspaceManager
from orchestrator.workspace.merge_executor import MergeExecutor
from orchestrator.workspace.policy import WorkspacePolicy


class StabilityRunner:
    def __init__(
        self,
        *,
        root: Path,
        total_tasks: int,
        duration_seconds: float,
        pool_size: int = 4,
        interval: float = 0.05,
    ) -> None:
        self.root = root
        self.total_tasks = total_tasks
        self.duration_seconds = duration_seconds
        self.pool_size = pool_size
        self.interval = interval
        self.manager = GitWorkspaceManager(root / "repo", root / "worktrees")
        self.base = self.manager.initialize_repository()
        self.worker = self.manager.create_worktree("worker", self.base)
        self.policy = WorkspacePolicy(
            project_root=root / "repo",
            worktrees_root=root / "worktrees",
            managed_worktrees=(self.worker,),
        )
        self.store = SQLiteStateStore(root / "state.db", workspace_policy=self.policy)
        self.store.create_run("run-1", "team-1")
        self.authority = self.store.acquire_authority(
            "run-1", "stability-supervisor", "supervisor"
        )
        self.controller = self.store.acquire_run_controller(
            "run-1", "stability-op", lease_seconds=60
        )
        reconcile_pool_once(
            self.store,
            "run-1",
            AgentPoolSpec(
                pool_id="fake-workers",
                backend="fake",
                role_id="worker",
                count=self.pool_size,
                max_count=self.pool_size,
                model="fake-v1",
            ),
        )
        self.executor = MergeExecutor(self.store, self.manager)
        self.injected = 0
        self.invariant_checks = 0
        self.last_error: str | None = None

    def close(self) -> None:
        self.store.close()

    def _inject_write_task(self) -> None:
        task_id = f"task-{self.injected:05d}"
        self.store.create_task(
            "run-1",
            task_id,
            access_mode="write",
            write_scope=(f"demo/f{self.injected:05d}.txt",),
            required_role_id="worker",
            prompt=f"write {task_id}",
            cwd=str(self.worker),
            timeout_seconds=10,
        )
        self.store.transition_task(task_id, TaskState.READY, reason="ready")
        self.injected += 1

    def _settle_reviewed_merge(self) -> None:
        """把 REVIEW 的写任务 → 产出 commit → 入队 → 集成。"""
        rows = self.store.connection.execute(
            "SELECT task_id FROM tasks WHERE run_id='run-1' AND state='REVIEW'"
        ).fetchall()
        for row in rows:
            task_id = str(row["task_id"])
            if self.store.connection.execute(
                "SELECT 1 FROM merge_queue WHERE task_id=? AND status IN "
                "('PENDING','APPLYING','APPLIED')",
                (task_id,),
            ).fetchone():
                continue
            seq = task_id.split("-")[1]
            commit = self.manager.commit_file(
                self.worker,
                f"demo/f{seq}.txt",
                f"result-{task_id}\n",
                f"worker: {task_id}",
            )
            attempt = self.store.connection.execute(
                "SELECT attempt_id FROM attempts WHERE task_id=? LIMIT 1",
                (task_id,),
            ).fetchone()
            if attempt is None:
                continue
            self.store.enqueue_merge(
                "run-1", task_id, str(attempt["attempt_id"]), commit, self.base,
                self.controller, authority=self.authority, reason="review-passed",
            )
            self.executor.run_merge_once("run-1", self.controller, self.authority)

    def check_invariants(self) -> list[str]:
        """0 丢任务 / 0 重复 merge / 0 重复通知。返回违规列表。"""
        violations: list[str] = []
        total = self.injected
        if total == 0:
            return violations
        # 0 丢：每个已注入任务必须到达终态（COMPLETED/FAILED/CANCELLED）
        stuck = self.store.connection.execute(
            "SELECT COUNT(*) FROM tasks WHERE run_id='run-1' "
            "AND state NOT IN ('COMPLETED','FAILED','CANCELLED')"
        ).fetchone()[0]
        if stuck:
            violations.append(f"{stuck} tasks not in terminal state")
        # 0 重复 merge：每 task 至多 1 条 APPLIED
        dup = self.store.connection.execute(
            "SELECT COUNT(*) FROM (SELECT task_id FROM merge_queue "
            "WHERE status='APPLIED' GROUP BY task_id HAVING COUNT(*) > 1)"
        ).fetchone()[0]
        if dup:
            violations.append(f"{dup} tasks have duplicate APPLIED merges")
        # 0 重复通知：每 merge 至多 1 条 outbox intent
        dup_outbox = self.store.connection.execute(
            "SELECT COUNT(*) FROM (SELECT aggregate_id FROM outbox "
            "WHERE event_type='merge.applied' GROUP BY aggregate_id "
            "HAVING COUNT(*) > 1)"
        ).fetchone()[0]
        if dup_outbox:
            violations.append(f"{dup_outbox} merges have duplicate outbox intents")
        return violations

    async def run(self) -> dict[str, Any]:
        from orchestrator.serve import serve

        adapter = FakeBackendAdapter(
            default_behavior=FakeBehavior(delay_seconds=0, text="done")
        )
        started = time.monotonic()
        deadline = started + self.duration_seconds
        drain_deadline: float | None = None
        adapter = FakeBackendAdapter(
            default_behavior=FakeBehavior(delay_seconds=0, text="done")
        )

        async def scheduler() -> None:
            nonlocal drain_deadline
            while True:
                if self.injected < self.total_tasks and time.monotonic() < deadline:
                    self._inject_write_task()
                await serve(
                    self.store,
                    run_id="run-1",
                    adapters={"fake": adapter},
                    authority=self.authority,
                    controller=self.controller,
                    interval=self.interval,
                    controller_lease_seconds=120,
                    max_ticks=1,
                )
                self._settle_reviewed_merge()
                violations = self.check_invariants()
                self.invariant_checks += 1
                if violations:
                    self.last_error = "; ".join(violations)
                    break
                pending = self.store.connection.execute(
                    "SELECT COUNT(*) FROM tasks WHERE run_id='run-1' "
                    "AND state NOT IN ('COMPLETED','FAILED','CANCELLED')"
                ).fetchone()[0]
                if self.injected >= self.total_tasks and pending == 0:
                    break  # 全部注入并完全 drain
                if time.monotonic() >= deadline and drain_deadline is None:
                    drain_deadline = time.monotonic() + 1800  # 最多再等 30 分钟
                if drain_deadline is not None and time.monotonic() >= drain_deadline:
                    self.last_error = "drain timeout with pending tasks"
                    break
                await asyncio.sleep(self.interval)

        await scheduler()
        elapsed = time.monotonic() - started
        violations = self.check_invariants()
        summary = {
            "mode": "stage3-stability",
            "duration_seconds": round(elapsed, 3),
            "injected_tasks": self.injected,
            "invariant_checks": self.invariant_checks,
            "violations": violations or None,
            "terminal_tasks": self.store.connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE run_id='run-1' "
                "AND state IN ('COMPLETED','FAILED','CANCELLED')"
            ).fetchone()[0],
            "applied_merges": self.store.connection.execute(
                "SELECT COUNT(*) FROM merge_queue WHERE status='APPLIED'"
            ).fetchone()[0],
            "status": "pass" if not violations else "fail",
        }
        if self.last_error:
            summary["last_error"] = self.last_error
        return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--tasks", type=int, default=600)
    parser.add_argument("--pool", type=int, default=4)
    parser.add_argument("--interval", type=float, default=0.05)
    parser.add_argument(
        "--root", type=Path, default=Path(".agent-hub/stage3-stability")
    )
    args = parser.parse_args(argv)

    temp_root = Path(tempfile.mkdtemp())  # 每次独立环境
    runner = StabilityRunner(
        root=temp_root,
        total_tasks=args.tasks,
        duration_seconds=args.hours * 3600,
        pool_size=args.pool,
        interval=args.interval,
    )
    try:
        summary = asyncio.run(runner.run())
    finally:
        runner.close()
        import shutil

        shutil.rmtree(temp_root, ignore_errors=True)

    report_dir = args.root
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "stage3-stability.json"
    report_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"report: {report_path}")
    return 0 if summary["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
