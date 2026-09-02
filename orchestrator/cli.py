from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from collections.abc import Sequence
from pathlib import Path

from orchestrator.adapters.fake import FakeBackendAdapter
from orchestrator.bootstrap import hub_status, initialize_hub
from orchestrator.core.config import load_team_spec
from orchestrator.poc.fake_demo import run_fake_demo
from orchestrator.poc.git_demo import run_git_demo
from orchestrator.poc.recovery_demo import run_recovery_demo
from orchestrator.poc.real_demo import run_real_demo
from orchestrator.poc.stage2_real import run_mixed_parallel, run_stage2_real
from orchestrator.reconciler import run_reconciler_once
from orchestrator.serve import serve
from orchestrator.adapters.codebuddy_spike import run_codebuddy_session_spike
from orchestrator.adapters.codebuddy_safety_spike import run_codebuddy_safety_spike
from orchestrator.adapters.codex_spike import run_codex_lifecycle_spike
from orchestrator.adapters.live import run_live
from orchestrator.adapters.probes import probe_all
from orchestrator.auth import login
from orchestrator.storage.sqlite_store import SQLiteStateStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-hub")
    subcommands = parser.add_subparsers(dest="command", required=True)
    init = subcommands.add_parser("init", help="validate team config and initialize state")
    init.add_argument("--team", type=Path, default=Path("config/team.yaml"))
    init.add_argument(
        "--db", type=Path, default=Path(".agent-hub/state/agent-hub.db")
    )
    status = subcommands.add_parser("status", help="show the local state summary")
    status.add_argument(
        "--db", type=Path, default=Path(".agent-hub/state/agent-hub.db")
    )
    status.add_argument("--run", dest="run_id")
    demo = subcommands.add_parser("demo", help="run a Stage 1 walking skeleton")
    demo_mode = demo.add_mutually_exclusive_group(required=True)
    demo_mode.add_argument("--fake", action="store_true")
    demo_mode.add_argument("--git-fake", action="store_true")
    demo_mode.add_argument("--recovery-fake", action="store_true")
    demo_mode.add_argument("--real", action="store_true")
    demo.add_argument(
        "--db", type=Path, default=Path(".agent-hub/state/agent-hub.db")
    )
    probe = subcommands.add_parser("probe", help="inspect local backend readiness")
    probe.add_argument(
        "--live",
        choices=("codex", "codebuddy"),
        help="run one read-only authenticated backend request",
    )
    auth = subcommands.add_parser("auth", help="sign in a local backend")
    auth.add_argument("backend", choices=("codex", "codebuddy"))
    spike = subcommands.add_parser("spike", help="run a Stage 0 capability spike")
    spike.add_argument(
        "target",
        choices=("codebuddy-sessions", "codebuddy-safety", "codex-lifecycle"),
    )
    reconcile = subcommands.add_parser(
        "reconcile", help="recover attempts whose assignment leases expired"
    )
    reconcile.add_argument(
        "--db", type=Path, default=Path(".agent-hub/state/agent-hub.db")
    )
    reconcile.add_argument("--limit", type=int, default=100)
    reconcile.add_argument("--run", dest="run_id")
    serve_parser = subcommands.add_parser(
        "serve", help="run the resident background loop for one Run"
    )
    serve_parser.add_argument("--run", dest="run_id", required=True)
    serve_parser.add_argument(
        "--db", type=Path, default=Path(".agent-hub/state/agent-hub.db")
    )
    serve_parser.add_argument("--interval", type=float, default=1.0)
    serve_parser.add_argument("--lease", type=int, default=300)
    # 网页控制台：按 team 配置启动 serve（多后端 pools）
    serve_team_parser = subcommands.add_parser(
        "serve-team",
        help="run the resident loop for one Run using a saved team config",
    )
    serve_team_parser.add_argument("--run", dest="run_id", required=True)
    serve_team_parser.add_argument("--team", type=Path, required=True)
    serve_team_parser.add_argument(
        "--db", type=Path, default=Path(".agent-hub/state/agent-hub.db")
    )
    serve_team_parser.add_argument("--interval", type=float, default=1.0)
    serve_team_parser.add_argument("--lease", type=int, default=300)
    serve_parser.add_argument("--max-ticks", type=int, default=None)
    # P1-04：常驻服务可选接入真实 Adapter（写任务需配合受管 worktree）
    serve_parser.add_argument(
        "--backend",
        choices=("fake", "codex", "codebuddy"),
        default="fake",
        help="adapter backend for the resident loop (default: fake)",
    )
    # T3：本地状态页 + 管理控制台
    console_parser = subcommands.add_parser(
        "console", help="web console: runs / teams / connections (HTTP)"
    )
    console_parser.add_argument(
        "--run", dest="run_id", default=None, help="初始打开的 Run（可选）"
    )
    console_parser.add_argument(
        "--db", type=Path, default=Path(".agent-hub/state/agent-hub.db")
    )
    console_parser.add_argument("--port", type=int, default=8080)
    console_parser.add_argument("--host", default="127.0.0.1")
    console_parser.add_argument("--worktree", default=None)
    pause = subcommands.add_parser("pause", help="pause dispatch for a Run")
    pause.add_argument("--run", dest="run_id", required=True)
    pause.add_argument("--reason", default="manual-pause")
    pause.add_argument(
        "--db", type=Path, default=Path(".agent-hub/state/agent-hub.db")
    )
    resume = subcommands.add_parser("resume", help="resume dispatch for a Run")
    resume.add_argument("--run", dest="run_id", required=True)
    resume.add_argument("--reason", default="manual-resume")
    resume.add_argument(
        "--db", type=Path, default=Path(".agent-hub/state/agent-hub.db")
    )
    cancel = subcommands.add_parser("cancel", help="cancel a Run or Task")
    cancel_group = cancel.add_mutually_exclusive_group(required=True)
    cancel_group.add_argument("--run", dest="run_id")
    cancel_group.add_argument("--task", dest="task_id")
    cancel.add_argument("--reason", default="manual-cancel")
    cancel.add_argument(
        "--db", type=Path, default=Path(".agent-hub/state/agent-hub.db")
    )
    stage2_real = subcommands.add_parser(
        "stage2-real",
        help="run frozen real Codex/CodeBuddy scenarios through the unified scheduler",
    )
    stage2_real.add_argument(
        "--db", type=Path, default=Path(".agent-hub/state/agent-hub.db")
    )
    stage2_real.add_argument("--run", dest="run_id")
    stage2_real.add_argument("--max-ticks", type=int, default=40)
    stage2_real.add_argument(
        "--backends",
        default="codex,codebuddy",
        help="comma-separated backends to exercise",
    )
    stage2_mixed = subcommands.add_parser(
        "stage2-mixed",
        help="run 2 CodeBuddy workers + 1 Codex worker in parallel",
    )
    stage2_mixed.add_argument(
        "--db", type=Path, default=Path(".agent-hub/state/agent-hub.db")
    )
    stage2_mixed.add_argument("--run", dest="run_id")
    stage2_mixed.add_argument("--max-ticks", type=int, default=20)
    approvals = subcommands.add_parser(
        "approvals", help="list human approval requests"
    )
    approvals.add_argument("--run", dest="run_id", required=True)
    approvals.add_argument("--status", default="PENDING")
    approvals.add_argument(
        "--db", type=Path, default=Path(".agent-hub/state/agent-hub.db")
    )
    approve = subcommands.add_parser(
        "approve", help="approve a human approval request"
    )
    approve.add_argument("--request", dest="request_id", required=True)
    approve.add_argument("--by", required=True, help="human operator id")
    approve.add_argument("--comment", default=None)
    approve.add_argument(
        "--db", type=Path, default=Path(".agent-hub/state/agent-hub.db")
    )
    reject = subcommands.add_parser(
        "reject", help="reject a human approval request"
    )
    reject.add_argument("--request", dest="request_id", required=True)
    reject.add_argument("--by", required=True, help="human operator id")
    reject.add_argument("--comment", default=None)
    reject.add_argument(
        "--db", type=Path, default=Path(".agent-hub/state/agent-hub.db")
    )
    # T6：数据库备份 / 恢复 / 校验（rollback 演练基础）
    db_backup = subcommands.add_parser("db-backup", help="backup the SQLite database")
    db_backup.add_argument(
        "--db", type=Path, default=Path(".agent-hub/state/agent-hub.db")
    )
    db_backup.add_argument(
        "--backup-dir", type=Path, default=Path(".agent-hub/backups")
    )
    # B1（P1-02）：预算硬上限
    budget = subcommands.add_parser("budget", help="set or show run budget limits")
    budget.add_argument("--run", dest="run_id", required=True)
    budget.add_argument("--db", type=Path, default=Path(".agent-hub/state/agent-hub.db"))
    budget.add_argument("--max-seconds", type=int, default=None)
    budget.add_argument("--max-calls", type=int, default=None)
    budget.add_argument("--max-turns", type=int, default=None)
    budget.add_argument("--max-tasks", type=int, default=None)
    budget.add_argument("--max-cost", type=str, default=None)
    budget.add_argument("--show", action="store_true", help="show current budget status")
    # B2（P1-03）：重新分配命令
    reassign = subcommands.add_parser(
        "reassign", help="reassign a REVIEW/FAILED task back to READY"
    )
    reassign.add_argument("--run", dest="run_id", required=True)
    reassign.add_argument("--task", dest="task_id", required=True)
    reassign.add_argument("--reason", default="manual-reassign")
    reassign.add_argument(
        "--db", type=Path, default=Path(".agent-hub/state/agent-hub.db")
    )
    db_restore = subcommands.add_parser(
        "db-restore", help="restore the database from a backup"
    )
    db_restore.add_argument(
        "--db", type=Path, default=Path(".agent-hub/state/agent-hub.db")
    )
    db_restore.add_argument("--backup", type=Path, required=True)
    db_verify = subcommands.add_parser(
        "db-verify", help="verify database integrity and schema version"
    )
    db_verify.add_argument(
        "--db", type=Path, default=Path(".agent-hub/state/agent-hub.db")
    )
    return parser


def _resolve_db(cwd: Path, db: Path) -> Path:
    return db if db.is_absolute() else cwd / db


def _run_control_action(
    cwd: Path, db: Path, run_id: str, action: str, reason: str
) -> dict[str, object]:
    store = SQLiteStateStore(_resolve_db(cwd, db))
    owner = f"cli-{uuid.uuid4().hex}"
    try:
        token = store.acquire_run_controller(run_id, owner, lease_seconds=30)
        if token is None:
            return {
                "status": "busy",
                "run_id": run_id,
                "error": "run is controlled by another instance",
            }
        try:
            if action == "pause":
                store.pause_run(run_id, token, reason=reason)
            elif action == "resume":
                store.resume_run(run_id, token, reason=reason)
            else:
                raise ValueError(f"unknown control action: {action}")
            return {
                "status": "ready",
                "run_id": run_id,
                "action": action,
                "summary": store.summary(run_id=run_id),
            }
        finally:
            store.release_run_controller(token)
    finally:
        store.close()


def _run_cancel(cwd: Path, db: Path, run_id: str | None, task_id: str | None, reason: str) -> dict[str, object]:
    store = SQLiteStateStore(_resolve_db(cwd, db))
    owner = f"cli-{uuid.uuid4().hex}"
    try:
        if task_id is not None:
            row = store.connection.execute(
                "SELECT run_id FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return {"status": "not-found", "task_id": task_id}
            run_id = str(row["run_id"])
        assert run_id is not None
        token = store.acquire_run_controller(run_id, owner, lease_seconds=30)
        if token is None:
            return {
                "status": "busy",
                "run_id": run_id,
                "task_id": task_id,
                "error": "run is controlled by another instance",
            }
        try:
            if task_id is not None:
                disposition = store.request_cancel_task(
                    task_id, token, reason=reason
                )
                return {
                    "status": "ready",
                    "task_id": task_id,
                    "disposition": disposition,
                }
            result = store.request_cancel_run(run_id, token, reason=reason)
            return {"status": "ready", "run_id": run_id, **result}
        finally:
            store.release_run_controller(token)
    finally:
        store.close()


def _run_serve(
    cwd: Path,
    db: Path,
    run_id: str,
    interval: float,
    lease: int,
    max_ticks: int | None,
    backend: str = "fake",
) -> dict[str, object]:
    store = SQLiteStateStore(_resolve_db(cwd, db))
    try:
        # P1-04：常驻服务可配置 Fake/Codex/中国站 CodeBuddy Adapter。
        # 真实写任务需配合受管 worktree 注册与 WorkspacePolicy。
        if backend == "codex":
            from orchestrator.adapters.real import CodexBackendAdapter

            adapters = {"codex": CodexBackendAdapter()}
        elif backend == "codebuddy":
            from orchestrator.adapters.real import CodeBuddyBackendAdapter

            adapters = {"codebuddy": CodeBuddyBackendAdapter()}
        else:
            adapters = {"fake": FakeBackendAdapter()}
        try:
            return asyncio.run(
                serve(
                    store,
                    run_id,
                    adapters,
                    team_spec=None,
                    interval=interval,
                    controller_lease_seconds=lease,
                    max_ticks=max_ticks,
                )
            )
        except KeyboardInterrupt:
            return {"status": "interrupted", "run_id": run_id}
    finally:
        store.close()


def _run_serve_team(
    cwd: Path,
    db: Path,
    run_id: str,
    team_path: Path,
    interval: float,
    lease: int,
) -> dict[str, object]:
    """按保存的 team 配置启动常驻循环（支持多后端 pools）。"""
    from orchestrator.core.config import load_team_spec

    store = SQLiteStateStore(_resolve_db(cwd, db))
    try:
        resolved_team = team_path if team_path.is_absolute() else cwd / team_path
        spec = load_team_spec(resolved_team)
        # 需要真实登录态的写任务受管 worktree 由 WorkspacePolicy 控制；
        # 这里按 team 的 backend 构建 adapter（fake 用于离线跑通）。
        adapters: dict[str, object] = {}
        from orchestrator.adapters.fake import FakeBackendAdapter

        for pool in spec.agent_pools:
            backend = pool.backend
            if backend in adapters:
                continue
            if backend == "fake":
                adapters["fake"] = FakeBackendAdapter()
            elif backend == "codex":
                from orchestrator.adapters.real import CodexBackendAdapter

                adapters["codex"] = CodexBackendAdapter()
            elif backend == "codebuddy":
                from orchestrator.adapters.real import CodeBuddyBackendAdapter

                adapters["codebuddy"] = CodeBuddyBackendAdapter()
            else:
                return {"status": "error", "error": f"unknown backend {backend}"}
        try:
            return asyncio.run(
                serve(
                    store,
                    run_id,
                    adapters,
                    team_spec=spec,
                    interval=interval,
                    controller_lease_seconds=lease,
                )
            )
        except KeyboardInterrupt:
            return {"status": "interrupted", "run_id": run_id}
    finally:
        store.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "init":
        print(
            json.dumps(
                initialize_hub(Path.cwd(), team_path=args.team, database_path=args.db),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "status":
        result = hub_status(Path.cwd(), database_path=args.db, run_id=args.run_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "ready" else 1
    if args.command == "demo":
        if args.git_fake:
            result = run_git_demo(Path.cwd())
        elif args.recovery_fake:
            result = run_recovery_demo(Path.cwd(), database_path=args.db)
        elif args.real:
            result = run_real_demo(Path.cwd(), database_path=args.db)
        else:
            result = run_fake_demo(Path.cwd(), database_path=args.db)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] in {"ready", "run-passed"} else 1
    if args.command == "auth":
        return login(args.backend, Path.cwd())
    if args.command == "reconcile":
        result = run_reconciler_once(
            Path.cwd(), database_path=args.db, limit=args.limit, run_id=args.run_id
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "ready" else 1
    if args.command == "serve":
        result = _run_serve(
            Path.cwd(),
            args.db,
            args.run_id,
            args.interval,
            args.lease,
            args.max_ticks,
            args.backend,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] in {"stopped", "interrupted"} else 1
    if args.command == "serve-team":
        result = _run_serve_team(
            Path.cwd(),
            args.db,
            args.run_id,
            args.team,
            args.interval,
            args.lease,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] in {"stopped", "interrupted"} else 1
    if args.command == "console":
        from orchestrator.console.server import run_console

        run_console(
            db=args.db,
            project_root=Path.cwd(),
            port=args.port,
            host=args.host,
            initial_run_id=args.run_id,
            worktree=args.worktree,
        )
        return 0
    if args.command == "budget":
        db = _resolve_db(Path.cwd(), args.db)
        with SQLiteStateStore(db) as store:
            if args.show:
                status = store.budget_status(args.run_id)
                print(json.dumps(status, ensure_ascii=False, indent=2))
                return 0 if not status["exceeded"] else 2
            store.record_budget(
                args.run_id,
                max_run_seconds=args.max_seconds,
                max_calls=args.max_calls,
                max_turns=args.max_turns,
                max_tasks=args.max_tasks,
                max_cost_decimal=args.max_cost,
            )
            print(
                json.dumps(
                    {"status": "ok", "run_id": args.run_id, **store.budget_status(args.run_id)},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
    if args.command == "reassign":
        from orchestrator.storage.sqlite_store import (
            FencedAuthorityError,
            FencedControllerError,
        )

        db = _resolve_db(Path.cwd(), args.db)
        with SQLiteStateStore(db) as store:
            controller = store.acquire_run_controller(
                args.run_id, "reassign-op", lease_seconds=60
            )
            if controller is None:
                print(json.dumps({"error": "run controller busy"}, indent=2))
                return 1
            try:
                try:
                    authority = store.acquire_authority(
                        args.run_id, "reassign-op", "supervisor", lease_seconds=60
                    )
                except FencedAuthorityError as exc:
                    print(json.dumps({"error": str(exc)}, indent=2))
                    return 1
                try:
                    store.reassign_task(
                        args.run_id, args.task_id, controller, authority,
                        reason=args.reason,
                    )
                    print(
                        json.dumps(
                            {
                                "status": "ok",
                                "task_id": args.task_id,
                                "state": store.task_state(args.task_id).value,
                            },
                            indent=2,
                        )
                    )
                    return 0
                except (ValueError, KeyError, FencedControllerError) as exc:
                    print(json.dumps({"error": str(exc)}, indent=2))
                    return 1
            finally:
                store.release_run_controller(controller)
    if args.command == "db-backup":
        from orchestrator.db_ops import backup_database

        db = _resolve_db(Path.cwd(), args.db)
        backup = backup_database(db, _resolve_db(Path.cwd(), args.backup_dir))
        print(json.dumps({"backup": str(backup), "status": "ok"}, indent=2))
        return 0
    if args.command == "db-restore":
        from orchestrator.db_ops import restore_database, verify_database

        db = _resolve_db(Path.cwd(), args.db)
        restore_database(db, args.backup)
        check = verify_database(db)
        print(json.dumps({"restored": str(args.backup), **check, "status": "ok"}, indent=2))
        return 0 if check["integrity_check"] == "ok" else 1
    if args.command == "db-verify":
        from orchestrator.db_ops import verify_database

        db = _resolve_db(Path.cwd(), args.db)
        check = verify_database(db)
        print(json.dumps(check, indent=2))
        return 0 if check["integrity_check"] == "ok" and not check["foreign_key_errors"] else 1
    if args.command == "pause":
        result = _run_control_action(
            Path.cwd(), args.db, args.run_id, "pause", args.reason
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "ready" else 1
    if args.command == "resume":
        result = _run_control_action(
            Path.cwd(), args.db, args.run_id, "resume", args.reason
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "ready" else 1
    if args.command == "cancel":
        result = _run_cancel(
            Path.cwd(), args.db, args.run_id, args.task_id, args.reason
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "ready" else 1
    if args.command == "stage2-real":
        result = run_stage2_real(
            Path.cwd(),
            database_path=args.db,
            run_id=args.run_id,
            max_ticks=args.max_ticks,
            backends=tuple(
                item.strip()
                for item in args.backends.split(",")
                if item.strip()
            ),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "ready" else 1
    if args.command == "stage2-mixed":
        result = run_mixed_parallel(
            Path.cwd(),
            database_path=args.db,
            run_id=args.run_id,
            max_ticks=args.max_ticks,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "ready" else 1
    if args.command == "approvals":
        store = SQLiteStateStore(_resolve_db(Path.cwd(), args.db))
        try:
            requests = store.list_approval_requests(
                args.run_id, status=args.status
            )
        finally:
            store.close()
        print(
            json.dumps(
                {"status": "ready", "run_id": args.run_id, "requests": requests},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command in ("approve", "reject"):
        decision = "APPROVED" if args.command == "approve" else "REJECTED"
        store = SQLiteStateStore(_resolve_db(Path.cwd(), args.db))
        try:
            store.decide_approval(
                args.request_id,
                decision,
                decided_by=args.by,
                comment=args.comment,
            )
        finally:
            store.close()
        print(
            json.dumps(
                {
                    "status": "ready",
                    "request_id": args.request_id,
                    "decision": decision,
                    "decided_by": args.by,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "spike":
        runners = {
            "codebuddy-sessions": run_codebuddy_session_spike,
            "codebuddy-safety": run_codebuddy_safety_spike,
            "codex-lifecycle": run_codex_lifecycle_spike,
        }
        runner = runners[args.target]
        result = runner(Path.cwd())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") == "ready" else 1
    if args.command != "probe":
        return 2

    if args.live:
        print(
            json.dumps(
                run_live(args.live, Path.cwd()),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print(
        json.dumps(
            {"backends": [result.to_dict() for result in probe_all()]},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0
