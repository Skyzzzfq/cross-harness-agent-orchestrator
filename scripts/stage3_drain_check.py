"""E3：真实多 agent 运行后 drain 孤儿检查脚本（阶段 3）。

退出条件（实施计划 §13）：
    drain 后 5 分钟内孤儿进程和无引用 worktree 为 0，数据库无持续锁。

脚本做法：
  1. 在 run 专属目录建受管 Git 仓库 + 一个 worker worktree，配 codex/codebuddy
     两个 agent 池（codex×1、codebuddy×2，共 3 个真实 agent）。
  2. 注入少量真实任务：只读 marker（codex/codebuddy）、写任务（真实产出 →
     REVIEW → 人工结算 → merge → COMPLETED）、并行写、以及一次人为取消。
     全部跑到不再有可调度工作（调度器视角 drain）。
  3. 完全 drain：两个池 count 归零 → agent 进入 DRAINING → finalize，直到
     run 内 agent 实例 = 0 且 backend call / session 全部关闭。
  4. 三条断言（5 分钟内反复核验，报告最后一刻状态）：
     a. 孤儿进程 = 0：drain 后仍在运行的、run 期间新出现的 codex/codebuddy
        相关进程（含 CLI 子进程），pid 不在 run 前基线内。
     b. 无引用 worktree = 0：run 仓库 `git worktree list` 中除主工作树外，
        每个注册 worktree 都必须被 ≥1 个任务的 cwd 引用；worktrees 根目录下
        不允许存在未注册的残留目录。
     c. 数据库无持续锁：store 关闭后用新连接反复探测写锁、integrity_check、
        wal_checkpoint，均须在 busy_timeout 内成功。

无账号时可 `--backends fake` 跑通框架（进程断言天然 0，用于 CI 冒烟）。

用法：
    & '.venv\\Scripts\\python.exe' scripts/stage3_drain_check.py --backends fake
    & '.venv\\Scripts\\python.exe' scripts/stage3_drain_check.py --backends codex,codebuddy
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from orchestrator.adapters.fake import FakeBackendAdapter, FakeBehavior
from orchestrator.agent_pool import reconcile_pool_once
from orchestrator.core.config import AgentPoolSpec
from orchestrator.core.models import TaskState
from orchestrator.storage.sqlite_store import SQLiteStateStore
from orchestrator.workspace.git_manager import GitWorkspaceManager

TERMINAL_STATES = {"COMPLETED", "FAILED", "CANCELLED", "REVIEW", "REJECTED", "BLOCKED"}
RELATED_NAMES = {
    "codex",
    "codex.exe",
    "codebuddy",
    "codebuddy.exe",
    "codebuddy.cmd",
}


def _read_marker_prompt(marker: str, extra: str = "") -> str:
    text = (
        "Reply with exactly the marker text: "
        f"{marker}. Do not call tools and do not modify any files. "
        "Your entire reply must be only that marker."
    )
    return extra + text if extra else text


def _read_marker_verbose_prompt(marker: str) -> str:
    return (
        "Reply with exactly the marker text: "
        f"{marker}. Think step by step out loud first (three bullet points about "
        "the architecture of a distributed scheduler), then reply with only the marker."
    )


class DrainRun:
    """一次真实多 agent run + 完全 drain + 孤儿/锁断言。"""

    def __init__(self, root: Path, backends: tuple[str, ...]) -> None:
        self.root = root
        self.backends = backends
        self.run_id = f"run-stage3-drain-{uuid.uuid4().hex[:12]}"
        self.repo = root / "repo"
        self.worktrees_root = root / "worktrees"
        self.manager = GitWorkspaceManager(self.repo, self.worktrees_root)
        self.errors: list[str] = []
        self.summary: dict[str, Any] = {}

    # -- 构造 ---------------------------------------------------------------

    def _pool_specs(self, active: bool) -> list[AgentPoolSpec]:
        base_counts = {"fake": 2, "codex": 1, "codebuddy": 2}
        specs: list[AgentPoolSpec] = []
        for backend in self.backends:
            count = base_counts.get(backend, 0) if active else 0
            specs.append(
                AgentPoolSpec(
                    pool_id=f"pool-{backend}",
                    backend=backend,
                    role_id="worker",
                    count=count,
                    max_count=max(count, 1),
                    model="real" if backend != "fake" else "fake",
                )
            )
        return specs

    def _build_adapters(self) -> dict[str, Any]:
        adapters: dict[str, Any] = {}
        for backend in self.backends:
            if backend == "fake":
                adapters["fake"] = FakeBackendAdapter(
                    default_behavior=FakeBehavior(delay_seconds=0, text="done")
                )
            elif backend == "codex":
                from orchestrator.adapters.real import CodexBackendAdapter

                adapters["codex"] = CodexBackendAdapter()
            elif backend == "codebuddy":
                from orchestrator.adapters.real import CodeBuddyBackendAdapter

                adapters["codebuddy"] = CodeBuddyBackendAdapter()
        return adapters

    # -- 调度工具 -----------------------------------------------------------

    async def _drive(self, store, adapters, authority, controller, ticks: int) -> None:
        from orchestrator.serve import serve

        for _ in range(ticks):
            await serve(
                store,
                run_id=self.run_id,
                adapters=adapters,
                authority=authority,
                controller=controller,
                interval=0.4,
                controller_lease_seconds=120,
                max_ticks=1,
            )

    def _agents_query(self, store, extra: str) -> int:
        return store.connection.execute(
            f"SELECT COUNT(*) FROM agent_instances WHERE team_id=? {extra}",
            (self.team_id,),
        ).fetchone()[0]

    async def _wait_quiet(
        self,
        store,
        adapters,
        authority,
        controller,
        *,
        deadline_seconds: float,
        quiet_rounds: int = 2,
    ) -> bool:
        """跑到没有 READY 任务 / 非终态在途 call / BUSY agent。"""
        quiet = 0
        start = time.monotonic()
        while time.monotonic() - start < deadline_seconds:
            await self._drive(store, adapters, authority, controller, ticks=1)
            ready = store.connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE run_id=? AND state IN "
                "('READY','IN_PROGRESS','CANCEL_REQUESTED')",
                (self.run_id,),
            ).fetchone()[0]
            inflight = store.connection.execute(
                "SELECT COUNT(*) FROM backend_calls WHERE run_id=? AND state IN "
                "('starting','running')",
                (self.run_id,),
            ).fetchone()[0]
            busy = self._agents_query(
                store, "AND status IN ('BUSY','STARTING')"
            )
            if ready == 0 and inflight == 0 and busy == 0:
                quiet += 1
                if quiet >= quiet_rounds:
                    return True
            else:
                quiet = 0
            await asyncio.sleep(0.2)
        return False

    # -- E3 断言 -------------------------------------------------------------

    def run(self) -> dict[str, Any]:  # noqa: C901 - 线性编排步骤偏多
        started = time.monotonic()
        base_proc = snapshot_processes()
        self.summary = {
            "mode": "stage3-drain",
            "run_id": self.run_id,
            "backends": list(self.backends),
            "run_seconds": None,
            "drain": {},
            "processes": {"baseline_pids": len(base_proc), "checks": []},
            "worktrees": {},
            "db_locks": {},
            "tasks": {},
            "status": "fail",
        }

        base = self.manager.initialize_repository()
        worker = self.manager.create_worktree("worker", base)
        self.worker = worker  # resolved absolute worktree 路径
        self.base = base
        is_fake = set(self.backends) == {"fake"}

        adapters = self._build_adapters()
        db_path = self.root / "state.db"
        store = SQLiteStateStore(db_path)
        try:
            store.create_run(self.run_id, "cross-harness-poc")
            self.team_id = str(
                store.connection.execute(
                    "SELECT team_id FROM runs WHERE run_id=?", (self.run_id,)
                ).fetchone()[0]
            )
            for spec in self._pool_specs(active=True):
                reconcile_pool_once(store, self.run_id, spec)
            authority = store.acquire_authority(
                self.run_id, "stage3-drain-supervisor", "supervisor", lease_seconds=1800
            )
            controller = store.acquire_run_controller(
                self.run_id, "stage3-drain-op", lease_seconds=1800
            )

            # 1) 注入真实任务
            scenarios = [
                ("r1-codex-read", "codex", _read_marker_prompt("MARKER_DRAIN_R1"),
                 "read", None),
                ("r2-codebuddy-read", "codebuddy",
                 _read_marker_prompt("MARKER_DRAIN_R2"), "read", None),
                ("w1-codex-write", "codex",
                 "In the provided workspace, create file demo/d1.txt whose content "
                 "is exactly RESULT_DRAIN_D1. Do not touch any other file.",
                 "write", ("demo/d1.txt",)),
                ("w2-codebuddy-write", "codebuddy",
                 "In the provided workspace, create file demo/d2.txt whose content "
                 "is exactly RESULT_DRAIN_D2. Do not touch any other file.",
                 "write", ("demo/d2.txt",)),
                ("w3-codebuddy-write-2", "codebuddy",
                 "In the provided workspace, create file demo/d3.txt whose content "
                 "is exactly RESULT_DRAIN_D3. Do not touch any other file.",
                 "write", ("demo/d3.txt",)),
                ("c1-codebuddy-cancel", "codebuddy",
                 _read_marker_verbose_prompt("MARKER_DRAIN_C1"), "read", None),
            ]
            for tid, backend, prompt, mode, scope in scenarios:
                if backend not in self.backends and not is_fake:
                    continue
                store.create_task(
                    self.run_id,
                    tid,
                    required_role_id="worker",
                    required_backend=None if is_fake else backend,
                    access_mode="write" if mode == "write" else "read_only",
                    write_scope=scope or (),
                    prompt=prompt,
                    cwd=str(worker),
                    timeout_seconds=150 if mode == "write" else 60,
                )
                store.transition_task(tid, TaskState.READY, reason="drain-scenario")

            # 2) 先派发 2 tick，让 c1 进入在途；随后人为取消（竞态可接受）
            asyncio.run(self._drive(store, adapters, authority, controller, ticks=2))
            cancel_state = store.connection.execute(
                "SELECT state FROM tasks WHERE task_id='c1-codebuddy-cancel'"
            ).fetchone()
            if cancel_state and str(cancel_state["state"]) not in TERMINAL_STATES:
                try:
                    store.request_cancel_task(
                        "c1-codebuddy-cancel", controller, reason="drain-operator-cancel"
                    )
                except Exception as exc:  # noqa: BLE001
                    self.errors.append(f"cancel c1: {type(exc).__name__}: {exc}")

            # 3) 跑到无在途工作
            quiet = asyncio.run(
                self._wait_quiet(
                    store, adapters, authority, controller, deadline_seconds=1200
                )
            )
            if not quiet:
                self.errors.append("run did not reach quiet state within deadline")

            # 4) 结算 REVIEW 写任务 → COMPLETED（评审人工动作的最小等价物）
            self._settle_writes(store, authority, controller)

            # 5) 完全 drain：pool count 归零并驱动到 agent 全部 finalize
            drain_ok = self._drain_pools(store, adapters, authority, controller)

            # 6) 记录任务终态 + cwd 引用（store 关闭前缓存）
            rows = store.connection.execute(
                "SELECT task_id, state FROM tasks WHERE run_id=?", (self.run_id,)
            ).fetchall()
            self.summary["tasks"] = {
                str(r["task_id"]): str(r["state"]) for r in rows
            }
            self._cwds_cache = [
                {"cwd": str(r["cwd"])}
                for r in store.connection.execute(
                    "SELECT DISTINCT cwd FROM task_dispatch_specs "
                    "WHERE task_id IN (SELECT task_id FROM tasks WHERE run_id=?) "
                    "AND cwd IS NOT NULL",
                    (self.run_id,),
                )
            ]
            self.summary["drain"]["agents_finalized"] = drain_ok
        finally:
            store.close()

        # 7) 三类断言（store 已关闭）
        self.summary["run_seconds"] = round(time.monotonic() - started, 2)
        self.summary["worktrees"] = self._check_worktrees(worker)
        self.summary["db_locks"] = self._check_db_locks(db_path)
        self.summary["processes"]["checks"] = self._watch_processes(
            base_proc, wait_seconds=300
        )

        violated = (
            self.summary["worktrees"].get("pass") is not True
            or self.summary["db_locks"].get("pass") is not True
            or self.summary["processes"]["checks"].get("pass") is not True
            or not drain_ok
            or bool(self.errors)
        )
        self.summary["violations"] = self.errors[:10] or None
        self.summary["status"] = "fail" if violated else "pass"
        return self.summary

    # -- 结算与 drain --------------------------------------------------------

    def _settle_writes(self, store, authority, controller) -> None:
        from orchestrator.workspace.merge_executor import MergeExecutor

        executor = MergeExecutor(store, self.manager)
        is_fake = set(self.backends) == {"fake"}
        rows = store.connection.execute(
            "SELECT task_id, write_scope_json FROM tasks WHERE run_id=? "
            "AND state='REVIEW' AND access_mode='write'",
            (self.run_id,),
        ).fetchall()
        for row in rows:
            tid = str(row["task_id"])
            scope = tuple(json.loads(row["write_scope_json"] or "[]"))
            if not scope:
                continue
            try:
                if is_fake:
                    # fake 模式：runner 写预期内容（无真实 agent 产出）
                    commit = self.manager.commit_file(
                        self.worker,
                        scope[0],
                        f"RESULT_{tid}\n",
                        f"worker: {tid}",
                    )
                else:
                    commit = self.manager.commit_managed_changes(
                        self.worker, scope, f"worker: {tid}"
                    )
            except Exception as exc:  # noqa: BLE001 - 真实产出缺失不阻断
                self.errors.append(f"settle {tid}: {type(exc).__name__}: {exc}")
                continue
            attempt = store.connection.execute(
                "SELECT attempt_id FROM attempts WHERE task_id=? LIMIT 1",
                (tid,),
            ).fetchone()
            if attempt is None:
                continue
            store.enqueue_merge(
                self.run_id,
                tid,
                str(attempt["attempt_id"]),
                commit,
                self.base,
                controller,
                authority=authority,
                reason="review-passed",
            )
            executor.run_merge_once(self.run_id, controller, authority)

    def _drain_pools(self, store, adapters, authority, controller) -> bool:
        ok = False
        for _ in range(120):
            for spec in self._pool_specs(active=False):
                reconcile_pool_once(store, self.run_id, spec)
            asyncio.run(self._drive(store, adapters, authority, controller, ticks=1))
            left = store.connection.execute(
                "SELECT COUNT(*) FROM agent_instances WHERE team_id=? AND status "
                "IN ('STARTING','IDLE','BUSY','DRAINING')",
                (self.team_id,),
            ).fetchone()[0]
            active_calls = store.connection.execute(
                "SELECT COUNT(*) FROM backend_calls WHERE run_id=? AND state IN "
                "('starting','running')",
                (self.run_id,),
            ).fetchone()[0]
            if left == 0 and active_calls == 0:
                ok = True
                break
            time.sleep(0.2)
        if not ok:
            detail = store.connection.execute(
                "SELECT agent_id, status, current_task_id FROM agent_instances "
                "WHERE team_id=? AND status IN ('STARTING','IDLE','BUSY','DRAINING')",
                (self.team_id,),
            ).fetchall()
            self.errors.append(
                "drain incomplete: "
                + "; ".join(
                    f"{a['agent_id']}={a['status']} task={a['current_task_id']}"
                    for a in detail
                )
            )
        self.summary["drain"]["ticks"] = _
        return ok

    # -- 断言实现 -------------------------------------------------------------

    def _check_worktrees(self, worker: Path) -> dict[str, Any]:
        listed = subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        registered: list[str] = []
        for block in listed.stdout.split("\n\n"):
            path = next(
                (ln[8:].strip() for ln in block.splitlines() if ln.startswith("worktree ")),
                None,
            )
            if path:
                registered.append(Path(path).resolve().as_posix())
        repo_self = self.repo.resolve().as_posix()
        linked = [p for p in registered if p != repo_self]

        cwds = {
            str(r["cwd"]).replace("\\", "/")
            for r in self._task_cwds()
            if r["cwd"]
        }
        referenced = sorted(cwds)
        unreferenced = sorted(set(linked) - cwds)
        stray = [
            p.resolve().as_posix()
            for p in self.worktrees_root.iterdir()
            if p.resolve().as_posix() not in registered
        ] if self.worktrees_root.exists() else []

        result = {
            "repo": repo_self,
            "registered_linked": sorted(linked),
            "task_referenced_cwds": referenced,
            "unreferenced": unreferenced,
            "stray_dirs": stray,
            "pass": not unreferenced and not stray,
        }
        return result

    def _task_cwds(self):
        """从任务库读 cwd（E3 断言与 run 库解耦，避免重开 store 的复杂性）。"""
        # run() 内已用 self._latest_cwds 由主 store 填充；此处经属性延迟读取。
        return getattr(self, "_cwds_cache", [])

    def _check_db_locks(self, db_path: Path) -> dict[str, Any]:
        events: list[str] = []
        pass_ok = True
        for i in range(3):
            try:
                con = sqlite3.connect(str(db_path), timeout=2.0)
                con.execute("PRAGMA busy_timeout=2000")
                con.execute("BEGIN IMMEDIATE")
                con.execute("CREATE TABLE IF NOT EXISTS _lock_probe (x INTEGER)")
                con.execute("INSERT INTO _lock_probe VALUES (1)")
                con.execute("DELETE FROM _lock_probe")
                con.execute("COMMIT")
                integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
                checkpoint = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                con.execute("DROP TABLE IF EXISTS _lock_probe")
                con.commit()
                con.close()
                ok = integrity == "ok" and str(checkpoint[0]) in {"0", "1"}
                events.append(
                    f"probe{i}: write_lock=ok integrity={integrity} "
                    f"checkpoint={checkpoint}"
                )
                pass_ok = pass_ok and ok
            except sqlite3.OperationalError as exc:
                events.append(f"probe{i}: locked/error: {exc}")
                pass_ok = False
            time.sleep(0.3)
        return {"probes": events, "pass": pass_ok}

    def _watch_processes(self, baseline: list[int], wait_seconds: int) -> dict[str, Any]:
        """drain 后监视最多 wait_seconds，相关新进程全部退出才算 0。"""
        anchor = self.root.resolve().as_posix().replace("\\", "/")
        base_set = set(baseline)
        samples: list[dict[str, Any]] = []
        deadline = time.monotonic() + wait_seconds
        watch_start = time.monotonic()
        leftover: list[dict[str, Any]] = []
        while True:
            current = [
                p for p in snapshot_processes() if is_related_process(p, anchor)
            ]
            fresh = [p for p in current if p["pid"] not in base_set]
            samples.append(
                {
                    "t": round(time.monotonic() - watch_start, 1),
                    "related_alive": len(current),
                    "fresh_alive": len(fresh),
                    "fresh_pids": sorted(p["pid"] for p in fresh),
                }
            )
            if not fresh:
                leftover = []
                break
            leftover = fresh
            if time.monotonic() >= deadline:
                break
            time.sleep(5)
        pass_ok = not leftover
        return {
            "wait_seconds": wait_seconds,
            "samples": samples[-1:] if samples else [],
            "samples_total": len(samples),
            "leftover": [
                {"pid": p["pid"], "name": p.get("name"), "cmdline": p.get("cmdline")}
                for p in leftover
            ],
            "pass": pass_ok,
        }


# ---------------------------------------------------------------------------
# Windows 进程枚举（无 psutil 依赖，走 PowerShell CIM）
# ---------------------------------------------------------------------------


def snapshot_processes() -> list[dict[str, Any]]:
    script = (
        "Get-CimInstance Win32_Process | Select-Object ProcessId,Name,"
        "CommandLine | ConvertTo-Json -Compress"
    )
    try:
        out = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        raw = out.stdout.strip()
        if not raw:
            return []
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        return [
            {
                "pid": int(p["ProcessId"]),
                "name": str(p.get("Name") or ""),
                "cmdline": str(p.get("CommandLine") or ""),
            }
            for p in data
            if p.get("ProcessId") is not None
        ]
    except Exception:  # noqa: BLE001
        return []


def is_related_process(p: dict[str, Any], anchor: str) -> bool:
    name = (p.get("name") or "").lower()
    cl = (p.get("cmdline") or "").lower()
    if name in RELATED_NAMES:
        return True
    if "codebuddy" in cl or "codex" in cl:
        return True
    if anchor and anchor in cl.replace("\\", "/"):
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backends", default="codex,codebuddy", help="逗号分隔: fake|codex|codebuddy"
    )
    parser.add_argument(
        "--base",
        type=Path,
        default=Path(".agent-hub/stage3-drain"),
        help="run 目录父目录（每轮唯一子目录，不删除历史）",
    )
    args = parser.parse_args(argv)

    backends = tuple(b.strip() for b in args.backends.split(",") if b.strip())
    # 必须用绝对路径：GitWorkspaceManager 相对路径会让 git worktree 在 repo
    # 目录内嵌套解析（E3 断言基于路径一致性，root 一律 resolve）。
    base = args.base.resolve()
    base.mkdir(parents=True, exist_ok=True)
    root = base / f"run-{uuid.uuid4().hex[:10]}"

    summary = DrainRun(root, backends).run()
    local = root / "stage3-drain.json"
    local.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 稳定副本供退出核验读取
    stable = base.parent / "reports" / "stage3-drain.json"
    stable.parent.mkdir(parents=True, exist_ok=True)
    stable.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"report: {local}")
    print(f"stable: {stable}")
    return 0 if summary["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
