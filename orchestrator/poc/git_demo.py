from __future__ import annotations

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from orchestrator.workspace.git_manager import (
    GitWorkspaceManager,
    fingerprint_checkout,
)


def _deterministic_verify(repository: Path) -> dict[str, bool]:
    return {
        "worker_a_content": (repository / "demo/a.txt").read_text(
            encoding="utf-8"
        )
        == "worker-a-result\n",
        "worker_b_content": (repository / "demo/b.txt").read_text(
            encoding="utf-8"
        )
        == "worker-b-result\n",
    }


def run_git_demo(cwd: Path) -> dict[str, Any]:
    run_id = f"run-git-{uuid.uuid4().hex[:12]}"
    run_root = cwd / ".agent-hub" / "poc-git" / run_id
    repository = run_root / "repository"
    worktrees = run_root / "worktrees"
    reports = cwd / ".agent-hub" / "reports"
    reports.mkdir(parents=True, exist_ok=True)

    checkout_before = fingerprint_checkout(cwd)
    manager = GitWorkspaceManager(repository, worktrees)
    base_commit = manager.initialize_repository()

    worker_a = manager.create_worktree("worker-a", base_commit)
    worker_b = manager.create_worktree("worker-b", base_commit)

    def worker_commit(name: str, worktree: Path, relative_path: str) -> dict[str, Any]:
        started_at = time.monotonic()
        time.sleep(0.05)
        commit = manager.commit_file(
            worktree,
            relative_path,
            f"{name}-result\n",
            f"{name}: produce isolated result",
        )
        ended_at = time.monotonic()
        return {
            "commit": commit,
            "started_at": started_at,
            "ended_at": ended_at,
        }

    with ThreadPoolExecutor(max_workers=2) as executor:
        future_a = executor.submit(worker_commit, "worker-a", worker_a, "demo/a.txt")
        future_b = executor.submit(worker_commit, "worker-b", worker_b, "demo/b.txt")
        execution_a = future_a.result()
        execution_b = future_b.result()
    commit_a = execution_a["commit"]
    commit_b = execution_b["commit"]
    worker_overlap = min(
        execution_a["ended_at"], execution_b["ended_at"]
    ) - max(execution_a["started_at"], execution_b["started_at"])
    integrate_a = manager.integrate(commit_a)
    integrate_b = manager.integrate(commit_b)
    verification = _deterministic_verify(repository)

    conflict_base = manager.head(repository)
    conflict_a = manager.create_worktree("conflict-a", conflict_base)
    conflict_b = manager.create_worktree("conflict-b", conflict_base)
    conflict_commit_a = manager.commit_file(
        conflict_a,
        "demo/conflict.txt",
        "conflict-from-a\n",
        "conflict-a: change shared path",
    )
    conflict_commit_b = manager.commit_file(
        conflict_b,
        "demo/conflict.txt",
        "conflict-from-b\n",
        "conflict-b: change shared path",
    )
    first_conflict_side = manager.integrate(conflict_commit_a)
    blocked_conflict_side = manager.integrate(conflict_commit_b)

    checkout_after = fingerprint_checkout(cwd)
    checks = {
        "worker_worktrees_are_distinct": worker_a != worker_b,
        "worker_commits_are_distinct": commit_a != commit_b,
        "worker_execution_overlapped": worker_overlap > 0,
        "worker_a_integrated": integrate_a.applied,
        "worker_b_integrated": integrate_b.applied,
        "deterministic_tests_passed": all(verification.values()),
        "first_conflict_side_integrated": first_conflict_side.applied,
        "same_path_conflict_blocked": not blocked_conflict_side.applied
        and blocked_conflict_side.conflicts == ("demo/conflict.txt",),
        "integration_repository_clean": manager.is_clean(),
        "user_checkout_head_unchanged": checkout_before.head == checkout_after.head,
        "user_checkout_status_unchanged": checkout_before.status_porcelain
        == checkout_after.status_porcelain,
        "user_checkout_contents_unchanged": checkout_before.workspace_sha256
        == checkout_after.workspace_sha256,
    }
    result = {
        "status": "ready" if all(checks.values()) else "error",
        "mode": "git-fake",
        "run_id": run_id,
        "checks": checks,
        "base_commit": base_commit,
        "worker_commits": {"worker-a": commit_a, "worker-b": commit_b},
        "worker_execution": {
            "worker-a": execution_a,
            "worker-b": execution_b,
            "overlap_seconds": worker_overlap,
        },
        "integration": {
            "worker-a": integrate_a.to_dict(),
            "worker-b": integrate_b.to_dict(),
            "conflict-a": first_conflict_side.to_dict(),
            "conflict-b": blocked_conflict_side.to_dict(),
        },
        "deterministic_verification": verification,
        "user_checkout_before": checkout_before.to_dict(),
        "user_checkout_after": checkout_after.to_dict(),
        "repository": str(repository),
        "worktrees_root": str(worktrees),
    }
    report_path = reports / f"{run_id}.json"
    report_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    result["report_path"] = str(report_path)
    return result
