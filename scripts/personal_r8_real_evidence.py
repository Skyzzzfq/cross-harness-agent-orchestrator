"""从已完成的真实 ``demo --real`` 报告生成 R8 A/B/D 证据。

该转换器只读取 ``.agent-hub/reports/run-real-*.json``，不会重新调用模型、
修改数据库或 checkout。它只把统一的真实 PoC 报告映射到
``personal-r8-v1`` 的证据契约：

* A：Codex 主管计划、两个 CodeBuddy Worker 调用和不同 session；
* B：两个 worktree/commit 的并行和用户 checkout 指纹；
* D：独立 Codex 审核发现缺陷、返工 session 和最终验证。

其余 R8 场景不会被推断为通过，需由固定样例的专门记录补充。

用法：

    .venv\\Scripts\\python.exe scripts\\personal_r8_real_evidence.py \\
        --reports .agent-hub/reports/run-real-*.json

PowerShell 不会自动展开脚本参数中的通配符，因此也可以省略 ``--reports``，
让脚本读取默认目录中的所有报告。
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:  # 作为 ``python -m``/测试导入和直接脚本两种入口均可用。
    from scripts.personal_r8_acceptance import FORMAT_VERSION, SAMPLE_ID
except ModuleNotFoundError:  # pragma: no cover - direct ``python scripts/...``
    from personal_r8_acceptance import FORMAT_VERSION, SAMPLE_ID

ROOT = Path(__file__).resolve().parent.parent

_REAL_CHECKS = (
    "codex_chatgpt_auth",
    "codex_plan_valid",
    "real_workers_overlapped",
    "codebuddy_sessions_distinct",
    "worker_commits_share_base",
    "review_a_passed",
    "review_b1_requested_rework",
    "codebuddy_rework_replaced_session",
    "review_b2_passed",
    "worker_a_completed",
    "worker_b_completed",
    "worker_b_first_attempt_rejected",
    "worker_b_second_attempt_accepted",
    "accepted_commits_integrated",
    "rejected_commit_not_integrated",
    "deterministic_content_passed",
    "integration_repository_clean",
    "structured_messages_persisted",
    "user_checkout_head_unchanged",
    "user_checkout_status_unchanged",
    "user_checkout_contents_unchanged",
    "plaintext_credentials_absent",
)


def _read(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _relative_ref(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT.resolve())).replace("\\", "/")
    except ValueError:
        return str(path.resolve()).replace("\\", "/")


def _all_checks_pass(report: dict[str, Any]) -> bool:
    checks = report.get("checks")
    return (
        report.get("scenario_id") == "real-poc-v1"
        and report.get("status") in {"run-passed", "ready"}
        and isinstance(checks, dict)
        and all(checks.get(key) is True for key in _REAL_CHECKS)
    )


def _distinct_worktrees(report: dict[str, Any]) -> bool:
    worktrees = report.get("evidence", {}).get("git", {}).get("worktrees", {})
    if not isinstance(worktrees, dict):
        return False
    paths = [str(value) for value in worktrees.values() if value]
    return len(paths) >= 3 and len(paths) == len(set(paths))


def _distinct_commits(report: dict[str, Any]) -> bool:
    git = report.get("evidence", {}).get("git", {})
    if not isinstance(git, dict):
        return False
    values = [
        git.get("worker_a_result_commit"),
        git.get("worker_b_rejected_commit"),
        git.get("worker_b_accepted_commit"),
    ]
    return all(isinstance(value, str) and value for value in values) and len(set(values)) == 3


def _worker_calls(report: dict[str, Any]) -> list[dict[str, Any]]:
    workers = report.get("evidence", {}).get("workers", {})
    if not isinstance(workers, dict):
        return []
    calls: list[dict[str, Any]] = []
    for label, item in workers.items():
        if not isinstance(item, dict) or not label.startswith("worker-"):
            continue
        if item.get("status") != "completed":
            continue
        calls.append(
            {
                "label": label,
                "backend": "codebuddy",
                "session_id": item.get("session_id"),
                "duration_ms": item.get("duration_ms"),
                "content_matched": item.get("content_matched") is True,
            }
        )
    return calls


def _has_supervisor_summary(report: dict[str, Any]) -> bool:
    summary = report.get("evidence", {}).get("supervisor_summary")
    if not isinstance(summary, dict):
        return False
    refs = summary.get("result_refs") or summary.get("worker_result_refs")
    return isinstance(refs, list) and len(refs) >= 2


def _has_independent_reviewer(report: dict[str, Any]) -> bool:
    evidence = report.get("evidence", {})
    supervisor_thread = evidence.get("codex", {}).get("thread_id")
    reviews = evidence.get("reviews", {})
    if not isinstance(reviews, dict) or not supervisor_thread:
        return False
    reviewer_sessions = {
        str(item.get("reviewer_session_id"))
        for item in reviews.values()
        if isinstance(item, dict) and item.get("reviewer_session_id")
    }
    return bool(reviewer_sessions) and all(
        reviewer_session != str(supervisor_thread)
        for reviewer_session in reviewer_sessions
    )


def _failure_summary(path: Path, report: dict[str, Any]) -> dict[str, Any]:
    failure = report.get("evidence", {}).get("failure")
    if not isinstance(failure, dict):
        failure = {"error_type": "unknown", "message": "run did not pass"}
    return {
        "report_ref": _relative_ref(path),
        "run_id": str(report.get("run_id") or ""),
        "status": str(report.get("status") or ""),
        "error_type": str(failure.get("error_type") or "unknown"),
        "message": str(failure.get("message") or "run did not pass")[:500],
    }


def _load_paths(root: Path, values: Iterable[str] | None) -> list[Path]:
    if values:
        paths: list[Path] = []
        for value in values:
            candidate = Path(value)
            if not candidate.is_absolute():
                candidate = root / candidate
            matches = [Path(item) for item in glob.glob(str(candidate))]
            paths.extend(matches or [candidate])
        return sorted(set(path.resolve() for path in paths))
    return sorted((root / ".agent-hub" / "reports").glob("run-real-*.json"))


def build_evidence(paths: Iterable[Path], *, root: Path = ROOT) -> dict[str, Any]:
    successful: list[tuple[Path, dict[str, Any]]] = []
    failures: list[dict[str, Any]] = []
    scanned: list[str] = []
    for path in paths:
        report = _read(path)
        if report is None:
            continue
        scanned.append(_relative_ref(path))
        if _all_checks_pass(report):
            successful.append((path, report))
        else:
            failures.append(_failure_summary(path, report))

    refs = [_relative_ref(path) for path, _ in successful]
    supervisor_success = [
        (path, report) for path, report in successful if _has_supervisor_summary(report)
    ]
    independent_review_success = [
        (path, report)
        for path, report in successful
        if _has_independent_reviewer(report)
    ]
    supervisor_refs = [_relative_ref(path) for path, _ in supervisor_success]
    reviewer_refs = [_relative_ref(path) for path, _ in independent_review_success]
    a_calls = [call for _, report in successful for call in _worker_calls(report)]
    sessions = sorted(
        {
            str(call["session_id"])
            for call in a_calls
            if call.get("session_id")
        }
    )
    b_overlap = all(
        report.get("checks", {}).get("real_workers_overlapped") is True
        for _, report in successful
    )
    b_checkout = all(
        report.get("checks", {}).get(key) is True
        for _, report in successful
        for key in (
            "user_checkout_head_unchanged",
            "user_checkout_status_unchanged",
            "user_checkout_contents_unchanged",
        )
    )
    b_worktrees = all(_distinct_worktrees(report) for _, report in successful)
    b_commits = all(_distinct_commits(report) for _, report in successful)
    d_defect = all(
        report.get("checks", {}).get("review_b1_requested_rework") is True
        for _, report in independent_review_success
    )
    d_rework = [
        {
            "run_id": report.get("run_id"),
            "attempt": "worker-b-attempt-2",
            "session_id": report.get("evidence", {})
            .get("workers", {})
            .get("worker-b-attempt-2", {})
            .get("session_id"),
        }
        for _, report in independent_review_success
    ]
    d_final = [
        {
            "run_id": report.get("run_id"),
            "review": report.get("evidence", {})
            .get("reviews", {})
            .get("worker-b-attempt-2", {})
            .get("decision"),
            "commit": report.get("evidence", {})
            .get("git", {})
            .get("worker_b_accepted_commit"),
        }
        for _, report in independent_review_success
    ]
    repetitions = len(successful)
    d_scenario: dict[str, Any] = {
        "repetitions": len(independent_review_success),
        "evidence_refs": reviewer_refs,
    }
    if independent_review_success:
        d_scenario.update(
            {
                "reviewer_detected_defect": d_defect,
                "rework_attempt": d_rework,
                "post_rework_verification": d_final,
            }
        )

    return {
        "format_version": FORMAT_VERSION,
        "sample_id": SAMPLE_ID,
        "run_id": "personal-r8-real-demo-batch-"
        + hashlib.sha256("\n".join(scanned).encode("utf-8")).hexdigest()[:12],
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "backend_mode": "real",
        "backends": [{"backend": "codex"}, {"backend": "codebuddy"}],
        "source_reports": scanned,
        "failure_records": failures,
        "scanned_reports": len(scanned),
        "successful_reports": repetitions,
        "scenarios": {
            "A": {
                "repetitions": len(supervisor_success),
                "worker_calls": a_calls,
                "agent_sessions": sessions,
                "supervisor_summary_refs": [
                    f"{ref}#evidence.supervisor_summary" for ref in supervisor_refs
                ],
                "evidence_refs": supervisor_refs,
            },
            "B": {
                "repetitions": repetitions,
                "overlap_observed": repetitions > 0 and b_overlap,
                "distinct_worktrees": repetitions > 0 and b_worktrees,
                "distinct_commits": repetitions > 0 and b_commits,
                "user_checkout_unchanged": repetitions > 0 and b_checkout,
                "evidence_refs": refs,
            },
            "D": d_scenario,
        },
        "notes": [
            "B is derived from real-poc reports whose frozen checks are all true.",
            "A additionally requires a recorded supervisor_summary referencing both worker results.",
            "D additionally requires an independent reviewer session distinct from the supervisor thread.",
            "failure_records retains every scanned report that did not meet the full frozen checks.",
            "C/E/F/G/H/I/J intentionally omitted; the R8 gate must keep them pending.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--reports", nargs="*", help="报告路径或通配符；默认读取 .agent-hub/reports")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / ".agent-hub" / "reports" / "personal-r8-evidence.json",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    paths = _load_paths(root, args.reports)
    report = build_evidence(paths, root=root)
    output = args.output if args.output.is_absolute() else root / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
