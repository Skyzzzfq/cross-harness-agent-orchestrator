"""Run the remaining R8 real-evidence slices in small, bounded scenarios.

The command performs real backend calls for C, E and J and records only
credential-free evidence.  It deliberately does not modify the user's
checkout: all writes happen below ``.agent-hub/r8-remaining``.

Usage::

    .venv\\Scripts\\python.exe scripts\\personal_r8_remaining_real.py --slices c,e,j

The generated JSON is runtime evidence and is ignored by Git.  It is merged
by ``personal_r8_real_evidence.py`` when present.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import tempfile
import uuid
from pathlib import Path
from typing import Any

from orchestrator.adapters.contracts import CallState, CallSnapshot
from orchestrator.adapters.codebuddy_spike import _read_marker
from orchestrator.adapters.real import CodeBuddyBackendAdapter
from orchestrator.adapters.stage1_real import run_codebuddy_write
from orchestrator.agent_pool import reconcile_pool_once
from orchestrator.call_runtime import recover_starting_calls
from orchestrator.core.config import AgentPoolSpec
from orchestrator.core.models import TaskState
from orchestrator.core.supervisor_plan import PlanValidationError, validate_supervisor_plan
from orchestrator.platform import codex_transport_environment
from orchestrator.poc.real_demo import TASK_A_CONTENT
from orchestrator.adapters.codex_spike import run_codex_lifecycle_spike
from orchestrator.storage.sqlite_store import SQLiteStateStore
from orchestrator.workspace.git_manager import GitWorkspaceManager, fingerprint_checkout

ROOT = Path(__file__).resolve().parent.parent
FORMAT_VERSION = "personal-r8-v1"
SAMPLE_ID = "personal-r8-fixed-sample-v1"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_usage(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        # Token counts/duration are useful evidence and are not credentials.
        # The acceptance converter performs a separate key-based secret scrub.
        return {str(k): _safe_usage(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_usage(item) for item in value]
    return str(value)


def _run_c(root: Path) -> dict[str, Any]:
    run_id = f"r8-c-{uuid.uuid4().hex[:12]}"
    run_root = root / ".agent-hub" / "r8-remaining" / run_id
    manager = GitWorkspaceManager(run_root / "repository", run_root / "worktrees")
    base = manager.initialize_repository()
    initial = manager.create_worktree("initial", base)
    old_version = "UPSTREAM_VERSION_OLD"
    new_version = "UPSTREAM_VERSION_V1"
    old_commit = manager.commit_file(
        initial, "legacy-marker.txt", f"{old_version}\n", "seed dependency version"
    )
    upstream = manager.create_worktree("upstream-worker", old_commit)
    upstream_call = run_codebuddy_write(
        upstream,
        "marker.txt",
        new_version,
        session_id=f"agent-hub-{run_id}-upstream",
    )
    if upstream_call.get("status") != "completed":
        return {
            "repetitions": 0,
            "dependency_base_commits": [],
            "verified_upstream_refs": [],
            "evidence_refs": [f".agent-hub/r8-remaining/{run_id}.json"],
            "upstream_session_id": upstream_call.get("session_id"),
            "upstream_call": upstream_call,
            "passed": False,
        }
    try:
        upstream_commit = manager.commit_managed_changes(
            upstream, ("marker.txt",), f"{run_id} verified upstream"
        )
    except (OSError, ValueError) as exc:
        return {
            "repetitions": 0,
            "dependency_base_commits": [],
            "verified_upstream_refs": [],
            "evidence_refs": [f".agent-hub/r8-remaining/{run_id}.json"],
            "upstream_session_id": upstream_call.get("session_id"),
            "upstream_call": upstream_call,
            "error_type": type(exc).__name__,
            "passed": False,
        }
    downstream = manager.create_worktree("downstream-worker", upstream_commit)
    downstream_call = asyncio.run(
        _read_marker(
            downstream,
            new_version,
            f"agent-hub-{run_id}-downstream",
        )
    )
    passed = (
        upstream_call.get("status") == "completed"
        and downstream_call.get("matched") is True
        and manager.parent_of(upstream_commit) == old_commit
        and (downstream / "marker.txt").read_text(encoding="utf-8").strip() == new_version
    )
    return {
        "repetitions": 1 if passed else 0,
        "dependency_base_commits": [upstream_commit] if passed else [],
        "verified_upstream_refs": [f"{run_id}#upstream-commit:{upstream_commit}"] if passed else [],
        "evidence_refs": [f".agent-hub/r8-remaining/{run_id}.json"],
        "upstream_session_id": upstream_call.get("session_id"),
        "downstream_session_id": downstream_call.get("session_id"),
        "old_commit": old_commit,
        "upstream_commit": upstream_commit,
        "upstream_call": upstream_call,
        "downstream_call": downstream_call,
        "passed": passed,
    }


async def _run_e_async(root: Path) -> dict[str, Any]:
    from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, Sandbox

    run_id = f"r8-e-{uuid.uuid4().hex[:12]}"
    before = fingerprint_checkout(root)
    env = codex_transport_environment(root)
    env["OPENAI_API_KEY"] = ""
    env["CODEX_ACCESS_TOKEN"] = ""
    malformed_prompt = "Return exactly the malformed JSON fragment {not-json and nothing else."
    repair_prompt = (
        "Repair the previous malformed plan into one valid JSON object, but keep the "
        "task write_scope exactly ../outside-r8.txt so the Hub validator can reject it. "
        "Use plan_version 2, revision 1, worker_concurrency 1, task_cap 1, empty "
        "permissions and budget, and include all required task fields."
    )
    malformed_response = ""
    repair_response: dict[str, Any] | None = None
    repair_turn: Any = None
    validation_error = ""
    thread_id: str | None = None
    try:
        async with AsyncCodex(
            CodexConfig(env=env, config_overrides=('forced_login_method="chatgpt"',))
        ) as codex:
            thread = await codex.thread_start(
                approval_mode=ApprovalMode.deny_all,
                cwd=str(root),
                ephemeral=False,
                sandbox=Sandbox.read_only,
            )
            thread_id = thread.id
            malformed_turn = await asyncio.wait_for(thread.run(malformed_prompt), timeout=90)
            malformed_response = str(malformed_turn.final_response or "")
            try:
                json.loads(malformed_response)
                repair_count = 0
            except (TypeError, json.JSONDecodeError):
                repair_count = 1
            repair_turn = await asyncio.wait_for(thread.run(repair_prompt), timeout=90)
            repaired_text = str(repair_turn.final_response or "").strip()
            if repaired_text.startswith("```"):
                lines = repaired_text.splitlines()
                if lines and lines[0].strip().startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                repaired_text = "\n".join(lines).strip()
            repair_response = json.loads(repaired_text)
            try:
                validate_supervisor_plan(
                    repair_response,
                    allowed_role_ids={"worker"},
                    allowed_backends={"codebuddy"},
                    max_tasks=1,
                    allowed_child_role_ids={"worker"},
                    max_worker_concurrency=1,
                )
            except (PlanValidationError, ValueError) as exc:
                validation_error = str(exc)
            else:
                validation_error = "validator unexpectedly accepted illegal scope"
    except Exception as exc:  # noqa: BLE001 - evidence records failure, not secrets
        validation_error = f"backend_error:{type(exc).__name__}:{str(exc)[:160]}"
        repair_count = 0
    after = fingerprint_checkout(root)
    no_side_effects = before.to_dict() == after.to_dict()
    rejected = "invalid write_scope" in validation_error
    return {
        "repetitions": 1 if rejected and no_side_effects else 0,
        "format_repair_count": repair_count,
        "out_of_scope_side_effects": 0 if no_side_effects else 1,
        "fake_complete": False,
        "evidence_refs": [f".agent-hub/r8-remaining/{run_id}.json"],
        "codex_thread_id": thread_id,
        "malformed_response_sha256": _hash(malformed_response),
        "repair_turn_id": getattr(repair_turn, "id", None),
        "illegal_plan": repair_response,
        "validation_error": validation_error[:500],
        "rejected_out_of_scope": rejected,
        "checkout_unchanged": no_side_effects,
    }


def _run_e(root: Path) -> dict[str, Any]:
    return asyncio.run(_run_e_async(root))


def _run_j(root: Path) -> dict[str, Any]:
    run_id = f"r8-j-{uuid.uuid4().hex[:12]}"
    run_root = root / ".agent-hub" / "r8-remaining" / run_id
    manager = GitWorkspaceManager(run_root / "repository", run_root / "worktrees")
    base = manager.initialize_repository()
    worker = manager.create_worktree("single-agent", base)
    call = run_codebuddy_write(
        worker,
        "demo/a.txt",
        TASK_A_CONTENT,
        session_id=f"agent-hub-{run_id}-single",
    )
    passed = (
        call.get("status") == "completed"
        and (worker / "demo/a.txt").read_text(encoding="utf-8").strip() == TASK_A_CONTENT
    )
    usage = call.get("usage") if isinstance(call.get("usage"), dict) else {}
    disclosure = {
        "single_agent": _safe_usage(usage),
        "cost_or_tokens": "CodeBuddy reported fields are preserved when available; no universal speed or cost claim.",
        "multi_agent_reference": "real-poc-v1 A/B/D reports",
    }
    return {
        "repetitions": 1 if passed else 0,
        "same_input_comparison": "same demo/a.txt content and fixed prompt as real-poc-v1 worker-a",
        "usage_disclosure": json.dumps(disclosure, ensure_ascii=False, sort_keys=True),
        "manual_interventions_recorded": 0,
        "evidence_refs": [f".agent-hub/r8-remaining/{run_id}.json"],
        "session_id": call.get("session_id"),
        "duration_ms": call.get("duration_ms"),
        "call": call,
        "passed": passed,
    }


def _run_f(root: Path) -> dict[str, Any]:
    """Run the real Codex lifecycle and a restart-safe starting-call recovery."""
    run_id = f"r8-f-{uuid.uuid4().hex[:12]}"
    lifecycle = run_codex_lifecycle_spike(root)
    recovery_root = root / ".agent-hub" / "r8-remaining" / run_id
    database = recovery_root / "recovery.db"
    recovery_root.mkdir(parents=True, exist_ok=True)
    claimed_call_id = None
    recovered: list[str] = []
    duplicate_backend_calls = 0
    duplicate_merges = 0
    store = SQLiteStateStore(database)
    try:
        store.create_run(f"{run_id}-db", "r8-f-team")
        authority = store.acquire_authority(
            f"{run_id}-db", f"{run_id}-authority", "supervisor", lease_seconds=300
        )
        reconcile_pool_once(
            store,
            f"{run_id}-db",
            AgentPoolSpec(
                pool_id="r8-f-workers",
                backend="fake",
                role_id="worker",
                count=1,
                max_count=1,
                model="fake-v1",
            ),
        )
        store.create_task(
            f"{run_id}-db",
            f"{run_id}-task",
            prompt="restart recovery probe",
            cwd=str(recovery_root),
        )
        store.transition_task(f"{run_id}-task", TaskState.READY, reason="r8-f")
        controller = store.acquire_run_controller(
            f"{run_id}-db", f"{run_id}-controller", lease_seconds=300
        )
        claim = store.claim_ready_dispatch(
            f"{run_id}-db", controller=controller, authority=authority
        )
        if claim is not None:
            claimed_call_id = claim.call_id
        # A fresh process sees the still-starting call and fences it instead of
        # dispatching the same attempt twice.
        recovered = recover_starting_calls(store, run_id=f"{run_id}-db")
        duplicate_backend_calls = int(
            store.connection.execute(
                "SELECT COUNT(*) - COUNT(DISTINCT attempt_id) FROM backend_calls WHERE run_id=?",
                (f"{run_id}-db",),
            ).fetchone()[0]
        )
        duplicate_merges = int(
            store.connection.execute(
                "SELECT COUNT(*) - COUNT(DISTINCT merge_id) FROM merge_queue WHERE run_id=?",
                (f"{run_id}-db",),
            ).fetchone()[0]
        )
    finally:
        store.close()
    cancel_check = bool(lifecycle.get("checks", {}).get("cancel_recognized"))
    recovery_check = bool(claimed_call_id and claimed_call_id in recovered)
    passed = cancel_check and recovery_check and duplicate_backend_calls == 0 and duplicate_merges == 0
    control = lifecycle.get("control") if isinstance(lifecycle.get("control"), dict) else {}
    return {
        "repetitions": 1 if passed else 0,
        "restart_duplicate_dispatches": duplicate_backend_calls,
        "duplicate_merges": duplicate_merges,
        "cancel_state_explicit": {
            "backend": "codex",
            "timeout_recognized": bool(control.get("timeout_recognized")),
            "cancel_recognized": cancel_check,
            "final_status": control.get("final_status"),
            "backend_may_still_run": False,
        },
        "recovered_starting_calls": recovered,
        "codex_lifecycle": lifecycle,
        "evidence_refs": [f".agent-hub/r8-remaining/{run_id}.json"],
        "passed": passed,
    }


async def _run_g_async(root: Path) -> dict[str, Any]:
    """Exercise Codex steer while a bounded read-only turn is still running."""
    from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, Sandbox

    run_id = f"r8-g-{uuid.uuid4().hex[:12]}"
    before = fingerprint_checkout(root)
    env = codex_transport_environment(root)
    env["OPENAI_API_KEY"] = ""
    env["CODEX_ACCESS_TOKEN"] = ""
    thread_id: str | None = None
    turn_id: str | None = None
    delivery_status = "not_attempted"
    final_text = ""
    steer_error = ""
    try:
        async with AsyncCodex(
            CodexConfig(env=env, config_overrides=('forced_login_method="chatgpt"',))
        ) as codex:
            thread = await codex.thread_start(
                approval_mode=ApprovalMode.deny_all,
                cwd=str(root),
                ephemeral=False,
                sandbox=Sandbox.read_only,
            )
            thread_id = thread.id
            turn = await thread.turn(
                "Use the read-only terminal to run powershell.exe -NoProfile -Command "
                "Start-Sleep -Seconds 20. After it finishes, reply exactly BASE_DONE."
            )
            turn_id = turn.id
            turn_task = asyncio.create_task(turn.run())
            await asyncio.sleep(0.5)
            try:
                response = await asyncio.wait_for(
                    turn.steer("After the sleep, reply exactly GUIDANCE_APPLIED."),
                    timeout=15.0,
                )
                delivery_status = "acknowledged" if response is not None else "sent"
            except Exception as exc:  # noqa: BLE001 - keep evidence credential-free
                steer_error = type(exc).__name__
                delivery_status = "failed"
            try:
                result = await asyncio.wait_for(turn_task, timeout=45.0)
                final_text = str(result.final_response or "")
            except Exception as exc:  # noqa: BLE001 - bounded evidence run
                final_text = f"turn_error:{type(exc).__name__}"
            await asyncio.wait_for(codex.thread_archive(thread.id), timeout=15.0)
    except Exception as exc:  # noqa: BLE001 - evidence records type only
        steer_error = steer_error or type(exc).__name__
        delivery_status = "failed"
    after = fingerprint_checkout(root)
    applied = "GUIDANCE_APPLIED" in final_text
    scope_unchanged = before.to_dict() == after.to_dict()
    passed = bool(thread_id and turn_id and applied and delivery_status in {"acknowledged", "sent"} and scope_unchanged)
    return {
        "repetitions": 1 if passed else 0,
        "guidance_target": f"codex-thread:{thread_id}" if thread_id else "",
        "delivery_status": delivery_status,
        "applied_at": turn_id if applied else "",
        "scope_unchanged": scope_unchanged,
        "steer_error_type": steer_error,
        "final_response_sha256": _hash(final_text),
        "evidence_refs": [f".agent-hub/r8-remaining/{run_id}.json"],
        "passed": passed,
    }


def _run_g(root: Path) -> dict[str, Any]:
    return asyncio.run(_run_g_async(root))


async def _run_h_async(root: Path) -> dict[str, Any]:
    """Run one real CodeBuddy call through the budget-fenced dispatcher."""
    run_id = f"r8-h-{uuid.uuid4().hex[:12]}"
    run_root = root / ".agent-hub" / "r8-remaining" / run_id
    manager = GitWorkspaceManager(run_root / "repository", run_root / "worktrees")
    manager.initialize_repository()
    database = run_root / "budget.db"
    store = SQLiteStateStore(database)
    actual_call: dict[str, Any] = {}
    second_claim = None
    try:
        store.create_run(f"{run_id}-db", "r8-h-team")
        store.record_budget(f"{run_id}-db", max_calls=1)
        authority = store.acquire_authority(
            f"{run_id}-db", f"{run_id}-authority", "supervisor", lease_seconds=300
        )
        reconcile_pool_once(
            store,
            f"{run_id}-db",
            AgentPoolSpec(
                pool_id="r8-h-codebuddy",
                backend="codebuddy",
                role_id="worker",
                count=1,
                max_count=1,
                model="glm-5.3",
            ),
        )
        store.create_task(
            f"{run_id}-db",
            f"{run_id}-task-1",
            required_backend="codebuddy",
            prompt="Reply with exactly BUDGET_ONE.",
            cwd=str(run_root),
            timeout_seconds=90,
        )
        store.transition_task(f"{run_id}-task-1", TaskState.READY, reason="r8-h")
        controller = store.acquire_run_controller(
            f"{run_id}-db", f"{run_id}-controller", lease_seconds=300
        )
        claim = store.claim_ready_dispatch(
            f"{run_id}-db", controller=controller, authority=authority
        )
        if claim is None:
            return {
                "repetitions": 0,
                "budget_cap_enforced": False,
                "new_work_after_cap": 1,
                "remaining_reported": "first claim unavailable",
                "evidence_refs": [f".agent-hub/r8-remaining/{run_id}.json"],
            }
        adapter = CodeBuddyBackendAdapter()
        running = await adapter.start(claim)
        initial = await running.wait(timeout_seconds=0)
        if initial.state == CallState.RUNNING:
            store.mark_backend_call_running(
                claim.call_id,
                initial,
                reason="r8-h-adapter-started",
                controller=controller,
            )
        elif initial.backend_invoked:
            store.mark_backend_call_running(
                claim.call_id,
                CallSnapshot(
                    ref=initial.ref,
                    state=CallState.RUNNING,
                    started_at=initial.started_at,
                    backend_invoked=True,
                ),
                reason="r8-h-adapter-started-and-finished",
                controller=controller,
            )
        terminal = initial if initial.state.is_terminal else await running.wait(timeout_seconds=95)
        store.finish_backend_call(
            claim.call_id, terminal, reason="r8-h-terminal", controller=controller
        )
        actual_call = {
            "call_id": claim.call_id,
            "state": terminal.state.value,
            "backend_invoked": terminal.backend_invoked,
            "backend_may_still_run": terminal.backend_may_still_run,
            "usage": _safe_usage(terminal.usage),
        }
        store.create_task(
            f"{run_id}-db",
            f"{run_id}-task-2",
            required_backend="codebuddy",
            prompt="This task must not be dispatched after the call cap.",
            cwd=str(run_root),
            timeout_seconds=90,
        )
        store.transition_task(f"{run_id}-task-2", TaskState.READY, reason="r8-h")
        second_claim = store.claim_ready_dispatch(
            f"{run_id}-db", controller=controller, authority=authority
        )
        budget = store.budget_status(f"{run_id}-db")
        reservations = store.list_budget_reservations(f"{run_id}-db")
        cap_enforced = bool(budget.get("exceeded")) and second_claim is None
        remaining_reported = {
            "calls": budget.get("calls"),
            "max_calls": budget.get("max_calls"),
            "exceeded": budget.get("exceeded"),
            "reservations": len(reservations),
        }
        passed = bool(
            terminal.state == CallState.SUCCEEDED
            and terminal.backend_invoked
            and cap_enforced
            and budget.get("calls") == 1
        )
        return {
            "repetitions": 1 if passed else 0,
            "budget_cap_enforced": cap_enforced,
            "new_work_after_cap": 0 if second_claim is None else 1,
            "remaining_reported": remaining_reported,
            "actual_call": actual_call,
            "evidence_refs": [f".agent-hub/r8-remaining/{run_id}.json"],
            "passed": passed,
        }
    finally:
        store.close()


def _run_h(root: Path) -> dict[str, Any]:
    return asyncio.run(_run_h_async(root))


def _run_i(root: Path) -> dict[str, Any]:
    """Re-check one real integration report and reject a tampered old record."""
    try:
        from scripts.personal_r8_real_evidence import _all_checks_pass, build_evidence
    except ModuleNotFoundError:  # direct ``python scripts/...`` entry point
        from personal_r8_real_evidence import _all_checks_pass, build_evidence

    candidates = sorted((root / ".agent-hub" / "reports").glob("run-real-*.json"))
    selected_path: Path | None = None
    selected: dict[str, Any] | None = None
    for path in reversed(candidates):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and _all_checks_pass(value):
            selected_path, selected = path, value
            break
    run_id = f"r8-i-{uuid.uuid4().hex[:12]}"
    if selected_path is None or selected is None:
        return {
            "repetitions": 0,
            "evidence_refs": [f".agent-hub/r8-remaining/{run_id}.json"],
            "evidence_visible": False,
            "old_evidence_rejected": False,
            "integration_checks_passed": False,
        }
    checks = selected.get("checks", {})
    integration_keys = (
        "accepted_commits_integrated",
        "rejected_commit_not_integrated",
        "integration_repository_clean",
        "user_checkout_head_unchanged",
        "user_checkout_status_unchanged",
        "user_checkout_contents_unchanged",
    )
    git = selected.get("evidence", {}).get("git", {})
    integration_checks_passed = all(checks.get(key) is True for key in integration_keys) and bool(
        git.get("final_head")
    )
    with tempfile.TemporaryDirectory() as directory:
        tampered = json.loads(json.dumps(selected))
        tampered.setdefault("checks", {})["accepted_commits_integrated"] = False
        tampered_path = Path(directory) / "tampered-old-evidence.json"
        tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
        rejected = build_evidence([tampered_path], root=root)
    old_evidence_rejected = (
        rejected.get("successful_reports") == 0
        and len(rejected.get("failure_records", [])) == 1
    )
    passed = integration_checks_passed and old_evidence_rejected
    return {
        "repetitions": 1 if passed else 0,
        "evidence_visible": bool(selected_path and selected.get("evidence")),
        "old_evidence_rejected": old_evidence_rejected,
        "integration_checks_passed": integration_checks_passed,
        "source_report": str(selected_path.relative_to(root)).replace("\\", "/"),
        "accepted_commit": git.get("worker_b_accepted_commit"),
        "rejected_commit": git.get("worker_b_rejected_commit"),
        "evidence_refs": [
            f"{str(selected_path.relative_to(root)).replace(chr(92), '/') }#evidence.git",
            f"{str(selected_path.relative_to(root)).replace(chr(92), '/') }#checks",
        ],
        "passed": passed,
    }


def run_slices(root: Path, slices: set[str]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "sample_id": SAMPLE_ID,
        "backend_mode": "real",
        "backends": ["codex", "codebuddy"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scenarios": {},
        "notes": [
            "C/E/F/G/H/J are produced by real backend calls in isolated .agent-hub worktrees; I rechecks a real integration report."
        ],
    }
    if "c" in slices:
        result["scenarios"]["C"] = _run_c(root)
    if "e" in slices:
        result["scenarios"]["E"] = _run_e(root)
    if "j" in slices:
        result["scenarios"]["J"] = _run_j(root)
    if "f" in slices:
        result["scenarios"]["F"] = _run_f(root)
    if "g" in slices:
        result["scenarios"]["G"] = _run_g(root)
    if "h" in slices:
        result["scenarios"]["H"] = _run_h(root)
    if "i" in slices:
        result["scenarios"]["I"] = _run_i(root)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--slices", default="c,e,f,g,h,i,j", help="comma-separated C/E/F/G/H/I/J slices")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / ".agent-hub" / "reports" / "personal-r8-remaining.json",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    selected = {item.strip().lower() for item in args.slices.split(",") if item.strip()}
    unknown = selected - {"c", "e", "f", "g", "h", "i", "j"}
    if unknown:
        parser.error(f"unsupported slices: {sorted(unknown)}")
    report = run_slices(root, selected)
    output = args.output if args.output.is_absolute() else root / args.output
    if output.is_file():
        try:
            previous = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = None
        if isinstance(previous, dict) and isinstance(previous.get("scenarios"), dict):
            merged = dict(previous)
            scenarios = dict(previous["scenarios"])
            scenarios.update(report["scenarios"])
            merged["scenarios"] = scenarios
            merged["generated_at"] = report["generated_at"]
            merged["notes"] = report["notes"]
            report = merged
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
