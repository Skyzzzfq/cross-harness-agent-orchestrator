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
from orchestrator.poc.stage2_real import run_stage2_real
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
    serve_parser.add_argument("--max-ticks", type=int, default=None)
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
) -> dict[str, object]:
    store = SQLiteStateStore(_resolve_db(cwd, db))
    try:
        # S2-06 serves the deterministic Fake backend; real Codex/CodeBuddy
        # adapters are wired into the scheduler in S2-07.
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
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] in {"stopped", "interrupted"} else 1
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
