from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from orchestrator.bootstrap import hub_status, initialize_hub
from orchestrator.poc.fake_demo import run_fake_demo
from orchestrator.poc.git_demo import run_git_demo
from orchestrator.poc.recovery_demo import run_recovery_demo
from orchestrator.poc.real_demo import run_real_demo
from orchestrator.reconciler import run_reconciler_once
from orchestrator.adapters.codebuddy_spike import run_codebuddy_session_spike
from orchestrator.adapters.codebuddy_safety_spike import run_codebuddy_safety_spike
from orchestrator.adapters.codex_spike import run_codex_lifecycle_spike
from orchestrator.adapters.live import run_live
from orchestrator.adapters.probes import probe_all
from orchestrator.auth import login


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
    return parser


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
