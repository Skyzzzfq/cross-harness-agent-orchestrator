from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import re
import uuid
from pathlib import Path
from typing import Any

from orchestrator.adapters.stage1_real import run_codebuddy_write
from orchestrator.core.models import (
    AttemptState,
    MessageEnvelope,
    Recipient,
    TaskState,
    utc_now,
)
from orchestrator.platform import codex_transport_environment
from orchestrator.storage.sqlite_store import SQLiteStateStore
from orchestrator.workspace.git_manager import (
    GitWorkspaceManager,
    fingerprint_checkout,
)


SCENARIO_ID = "real-poc-v1"
TASK_A_CONTENT = "REAL_WORKER_A_OK"
TASK_B_INITIAL_CONTENT = "REAL_WORKER_B_NEEDS_REWORK"
TASK_B_FINAL_CONTENT = "REAL_WORKER_B_OK"
CRITERIA_VERSION = "real-poc-v1-criteria-1"

REQUIRED_REAL_CHECKS = frozenset(
    {
        "codex_plan_valid",
        "codex_chatgpt_auth",
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
    }
)

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "scenario": {"type": "string", "enum": [SCENARIO_ID]},
        "parallel_tasks": {
            "type": "array",
            "minItems": 2,
            "maxItems": 2,
            "items": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "enum": ["worker-a", "worker-b"]},
                    "write_scope": {"type": "string"},
                },
                "required": ["task_id", "write_scope"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["scenario", "parallel_tasks"],
    "additionalProperties": False,
}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["PASS", "REWORK"]},
        "reason_code": {
            "type": "string",
            "enum": ["CRITERIA_MET", "CONTENT_MISMATCH"],
        },
    },
    "required": ["decision", "reason_code"],
    "additionalProperties": False,
}


def _json_response(response: str | None) -> dict[str, Any]:
    if not response:
        raise ValueError("backend returned no structured response")
    value = json.loads(response)
    if not isinstance(value, dict):
        raise ValueError("structured response must be an object")
    return value


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _redact_credentials(value: Any) -> tuple[Any, int]:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        count = 0
        for key, item in value.items():
            cleaned, found = _redact_credentials(item)
            redacted[key] = cleaned
            count += found
        return redacted, count
    if isinstance(value, list):
        redacted_items: list[Any] = []
        count = 0
        for item in value:
            cleaned, found = _redact_credentials(item)
            redacted_items.append(cleaned)
            count += found
        return redacted_items, count
    if not isinstance(value, str):
        return value, 0
    patterns = (
        r"sk-[A-Za-z0-9_-]{20,}",
        r"Bearer\s+[A-Za-z0-9._-]{20,}",
        r"Authorization[\"']?\s*[:=]\s*[\"'][^\"']{16,}",
    )
    count = 0
    cleaned = value
    for pattern in patterns:
        cleaned, found = re.subn(
            pattern, "[REDACTED]", cleaned, flags=re.IGNORECASE
        )
        count += found
    return cleaned, count


def _artifact_credentials_found(paths: tuple[Path, ...]) -> bool:
    patterns = (
        re.compile(rb"sk-[A-Za-z0-9_-]{20,}", flags=re.IGNORECASE),
        re.compile(rb"Bearer\s+[A-Za-z0-9._-]{20,}", flags=re.IGNORECASE),
        re.compile(
            rb"Authorization[\"']?\s*[:=]\s*[\"'][^\"']{16,}",
            flags=re.IGNORECASE,
        ),
    )
    files: list[Path] = []
    for path in paths:
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(item for item in path.rglob("*") if item.is_file())
    for path in files:
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if any(pattern.search(data) for pattern in patterns):
            return True
    return False


def _record_acceptance_history(
    history_path: Path, *, run_id: str, passed: bool
) -> int:
    history_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"run_id": run_id, "passed": passed, "recorded_at": utc_now()}
    with history_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    consecutive = 0
    for line in reversed(history_path.read_text(encoding="utf-8").splitlines()):
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            break
        if item.get("passed") is not True:
            break
        consecutive += 1
    return consecutive


def assess_real_poc(checks: dict[str, bool]) -> str:
    return (
        "ready"
        if REQUIRED_REAL_CHECKS.issubset(checks)
        and all(checks[name] for name in REQUIRED_REAL_CHECKS)
        else "error"
    )


def _message(
    *,
    run_id: str,
    task_id: str,
    sequence: int,
    sender: str,
    recipient: str,
    kind: str,
    payload: dict[str, Any],
) -> MessageEnvelope:
    return MessageEnvelope(
        message_id=f"{task_id}-message-{sequence}",
        team_id="cross-harness-poc",
        run_id=run_id,
        task_id=task_id,
        sender_agent_id=sender,
        recipients=(Recipient("agent", recipient),),
        kind=kind,
        payload=payload,
        correlation_id=task_id,
        sequence=sequence,
        idempotency_key=f"{task_id}:{sequence}:{kind}",
    )


async def _run_real_demo(cwd: Path, database_path: Path) -> dict[str, Any]:
    from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, Sandbox

    run_id = f"run-real-{uuid.uuid4().hex[:12]}"
    run_root = cwd / ".agent-hub" / "real-poc" / run_id
    repository = run_root / "repository"
    worktrees_root = run_root / "worktrees"
    reports = cwd / ".agent-hub" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    resolved_database = (
        database_path if database_path.is_absolute() else cwd / database_path
    )
    checkout_before = fingerprint_checkout(cwd)
    thread_id: str | None = None
    evidence: dict[str, Any] = {
        "scenario_id": SCENARIO_ID,
        "run_id": run_id,
        "codex": {},
        "workers": {},
        "reviews": {},
        "git": {},
    }
    checks: dict[str, bool] = {}
    failure: dict[str, Any] | None = None

    manager = GitWorkspaceManager(repository, worktrees_root)
    try:
        base_commit = manager.initialize_repository()
        evidence["base_commit"] = base_commit
        evidence["workspace_rules_sha256"] = hashlib.sha256(
            (repository / "AGENTS.md").read_bytes()
        ).hexdigest()
        evidence["team_spec_sha256"] = hashlib.sha256(
            (cwd / "config/team.yaml").read_bytes()
        ).hexdigest()
        evidence["versions"] = {
            "openai_codex": importlib.metadata.version("openai-codex"),
            "codebuddy_agent_sdk": importlib.metadata.version("codebuddy-agent-sdk"),
        }

        plan_prompt = (
            f"You are the supervisor for the fixed {SCENARIO_ID} acceptance run. "
            "Return two parallel leaf tasks only: worker-a writes demo/a.txt and "
            "worker-b writes demo/b.txt. Do not call tools or modify files."
        )
        evidence["plan_prompt_sha256"] = _hash_text(plan_prompt)
        codex_environment = codex_transport_environment(cwd)
        codex_environment["OPENAI_API_KEY"] = ""
        codex_environment["CODEX_ACCESS_TOKEN"] = ""
        async with AsyncCodex(
            CodexConfig(
                env=codex_environment,
                config_overrides=('forced_login_method="chatgpt"',),
            )
        ) as codex:
            try:
                account = await codex.account()
                account_root = account.account.root if account.account else None
                auth_type = getattr(account_root, "type", None)
                plan_type = getattr(account_root, "plan_type", None)
                plan_value = getattr(plan_type, "value", None)
                checks["codex_chatgpt_auth"] = auth_type == "chatgpt"
                evidence["codex"]["auth"] = {
                    "type": auth_type,
                    "plan_type": plan_value,
                    "forced_login_method": "chatgpt",
                    "api_key_environment_cleared": True,
                }
                if not checks["codex_chatgpt_auth"]:
                    raise RuntimeError("Codex is not using ChatGPT subscription auth")
                thread = await codex.thread_start(
                    approval_mode=ApprovalMode.deny_all,
                    cwd=str(repository),
                    ephemeral=False,
                    sandbox=Sandbox.read_only,
                )
                thread_id = thread.id
                plan_turn = await asyncio.wait_for(
                    thread.run(plan_prompt, output_schema=PLAN_SCHEMA), timeout=90.0
                )
                plan = _json_response(plan_turn.final_response)
                plan_tasks = {
                    (item["task_id"], item["write_scope"])
                    for item in plan.get("parallel_tasks", [])
                }
                checks["codex_plan_valid"] = plan.get("scenario") == SCENARIO_ID and plan_tasks == {
                    ("worker-a", "demo/a.txt"),
                    ("worker-b", "demo/b.txt"),
                }
                if not checks["codex_plan_valid"]:
                    raise ValueError("Codex supervisor plan did not match frozen schema")
                evidence["codex"]["thread_id"] = thread.id
                evidence["codex"]["plan"] = {
                    "turn_id": plan_turn.id,
                    "status": plan_turn.status.value,
                    "duration_ms": plan_turn.duration_ms,
                    "output": plan,
                    "usage": (
                        plan_turn.usage.model_dump(by_alias=True)
                        if plan_turn.usage
                        else None
                    ),
                }

                worker_a = manager.create_worktree("worker-a", base_commit)
                worker_b = manager.create_worktree("worker-b-attempt-1", base_commit)
                task_a = f"{run_id}-worker-a"
                task_b = f"{run_id}-worker-b"
                attempt_a1 = f"{task_a}-attempt-1"
                attempt_b1 = f"{task_b}-attempt-1"
                session_a1 = f"agent-hub-{run_id}-worker-a-1"
                session_b1 = f"agent-hub-{run_id}-worker-b-1"

                with SQLiteStateStore(resolved_database) as store:
                    store.create_run(run_id, "cross-harness-poc")
                    store.create_task(
                        run_id,
                        task_a,
                        access_mode="write",
                        write_scope=("demo/a.txt",),
                        max_attempts=1,
                    )
                    store.create_task(
                        run_id,
                        task_b,
                        access_mode="write",
                        write_scope=("demo/b.txt",),
                        max_attempts=2,
                    )
                    store.transition_task(task_a, TaskState.READY, reason="planned")
                    store.transition_task(task_b, TaskState.READY, reason="planned")
                    store.append_message(
                        _message(
                            run_id=run_id,
                            task_id=task_a,
                            sequence=1,
                            sender="codex-supervisor-reviewer",
                            recipient="codebuddy-worker-a",
                            kind="task.delegated",
                            payload={
                                "attempt_id": attempt_a1,
                                "session_id": session_a1,
                                "write_scope": ["demo/a.txt"],
                            },
                        )
                    )
                    store.append_message(
                        _message(
                            run_id=run_id,
                            task_id=task_b,
                            sequence=1,
                            sender="codex-supervisor-reviewer",
                            recipient="codebuddy-worker-b",
                            kind="task.delegated",
                            payload={
                                "attempt_id": attempt_b1,
                                "session_id": session_b1,
                                "write_scope": ["demo/b.txt"],
                            },
                        )
                    )
                    lease_a1 = store.create_attempt_with_lease(
                        task_a, attempt_a1, "codebuddy-worker-a", lease_seconds=180
                    )
                    lease_b1 = store.create_attempt_with_lease(
                        task_b, attempt_b1, "codebuddy-worker-b", lease_seconds=180
                    )
                    store.transition_attempt(
                        attempt_a1, AttemptState.RUNNING, reason="backend-started"
                    )
                    store.transition_attempt(
                        attempt_b1, AttemptState.RUNNING, reason="backend-started"
                    )

                    worker_result_a, worker_result_b = await asyncio.gather(
                        asyncio.to_thread(
                            run_codebuddy_write,
                            worker_a,
                            "demo/a.txt",
                            TASK_A_CONTENT,
                            session_id=session_a1,
                        ),
                        asyncio.to_thread(
                            run_codebuddy_write,
                            worker_b,
                            "demo/b.txt",
                            TASK_B_INITIAL_CONTENT,
                            session_id=session_b1,
                        ),
                    )
                    evidence["workers"]["worker-a-attempt-1"] = worker_result_a
                    evidence["workers"]["worker-b-attempt-1"] = worker_result_b
                    if worker_result_a["status"] != "completed" or worker_result_b["status"] != "completed":
                        raise RuntimeError("one or more real CodeBuddy workers failed")
                    overlap = min(
                        worker_result_a["ended_at"], worker_result_b["ended_at"]
                    ) - max(
                        worker_result_a["started_at"], worker_result_b["started_at"]
                    )
                    checks["real_workers_overlapped"] = overlap > 0
                    checks["codebuddy_sessions_distinct"] = bool(
                        worker_result_a.get("session_id")
                        and worker_result_b.get("session_id")
                        and worker_result_a["session_id"] != worker_result_b["session_id"]
                    )
                    evidence["workers"]["initial_overlap_seconds"] = overlap

                    commit_a1 = manager.commit_managed_changes(
                        worker_a,
                        ("demo/a.txt",),
                        f"{task_a} {attempt_a1}",
                    )
                    commit_b1 = manager.commit_managed_changes(
                        worker_b,
                        ("demo/b.txt",),
                        f"{task_b} {attempt_b1}",
                    )
                    checks["worker_commits_share_base"] = (
                        manager.parent_of(commit_a1) == base_commit
                        and manager.parent_of(commit_b1) == base_commit
                    )
                    store.submit_attempt(
                        attempt_a1,
                        lease_a1["generation"],
                        reason="artifact-submitted",
                    )
                    store.append_message(
                        _message(
                            run_id=run_id,
                            task_id=task_a,
                            sequence=2,
                            sender="codebuddy-worker-a",
                            recipient="codex-supervisor-reviewer",
                            kind="artifact.submitted",
                            payload={
                                "attempt_id": attempt_a1,
                                "result_commit": commit_a1,
                            },
                        )
                    )
                    store.submit_attempt(
                        attempt_b1,
                        lease_b1["generation"],
                        reason="artifact-submitted",
                    )
                    store.append_message(
                        _message(
                            run_id=run_id,
                            task_id=task_b,
                            sequence=2,
                            sender="codebuddy-worker-b",
                            recipient="codex-supervisor-reviewer",
                            kind="artifact.submitted",
                            payload={
                                "attempt_id": attempt_b1,
                                "result_commit": commit_b1,
                            },
                        )
                    )

                    async def review(
                        task_name: str,
                        attempt_name: str,
                        artifact_path: str,
                        expected: str,
                        result_commit: str,
                    ) -> dict[str, Any]:
                        blob_oid, artifact_bytes = manager.read_blob(
                            result_commit, artifact_path
                        )
                        submitted = artifact_bytes.decode("utf-8")
                        deterministic_match = artifact_bytes == f"{expected}\n".encode(
                            "utf-8"
                        )
                        prompt = (
                            f"Act as reviewer for {task_name}/{attempt_name}. "
                            f"Criteria version {CRITERIA_VERSION} requires exact content "
                            f"{expected!r} followed by one newline. Immutable result commit "
                            f"is {result_commit} and blob is {blob_oid}. "
                            f"Submitted content is {submitted!r}. Return PASS with "
                            "CRITERIA_MET only when exact; otherwise REWORK with "
                            "CONTENT_MISMATCH. Do not call tools."
                        )
                        turn = await asyncio.wait_for(
                            thread.run(prompt, output_schema=REVIEW_SCHEMA), timeout=90.0
                        )
                        decision = _json_response(turn.final_response)
                        return {
                            "turn_id": turn.id,
                            "status": turn.status.value,
                            "duration_ms": turn.duration_ms,
                            "usage": (
                                turn.usage.model_dump(by_alias=True)
                                if turn.usage
                                else None
                            ),
                            "decision": decision,
                            "result_commit": result_commit,
                            "artifact_path": artifact_path,
                            "blob_oid": blob_oid,
                            "artifact_sha256": hashlib.sha256(artifact_bytes).hexdigest(),
                            "deterministic_match": deterministic_match,
                            "criteria_version": CRITERIA_VERSION,
                            "reviewer_id": "codex-supervisor-reviewer",
                            "prompt_sha256": _hash_text(prompt),
                        }

                    review_a = await review(
                        task_a, attempt_a1, "demo/a.txt", TASK_A_CONTENT, commit_a1
                    )
                    review_b1 = await review(
                        task_b,
                        attempt_b1,
                        "demo/b.txt",
                        TASK_B_FINAL_CONTENT,
                        commit_b1,
                    )
                    evidence["reviews"]["worker-a-attempt-1"] = review_a
                    evidence["reviews"]["worker-b-attempt-1"] = review_b1
                    checks["review_a_passed"] = review_a["decision"] == {
                        "decision": "PASS",
                        "reason_code": "CRITERIA_MET",
                    } and review_a["deterministic_match"]
                    checks["review_b1_requested_rework"] = review_b1["decision"] == {
                        "decision": "REWORK",
                        "reason_code": "CONTENT_MISMATCH",
                    } and not review_b1["deterministic_match"]
                    if not checks["review_a_passed"] or not checks["review_b1_requested_rework"]:
                        raise RuntimeError("Codex review decisions violated frozen criteria")
                    store.append_message(
                        _message(
                            run_id=run_id,
                            task_id=task_a,
                            sequence=3,
                            sender="codex-supervisor-reviewer",
                            recipient="codebuddy-worker-a",
                            kind="review.decided",
                            payload={"attempt_id": attempt_a1, **review_a["decision"]},
                        )
                    )
                    store.append_message(
                        _message(
                            run_id=run_id,
                            task_id=task_b,
                            sequence=3,
                            sender="codex-supervisor-reviewer",
                            recipient="codebuddy-worker-b",
                            kind="review.decided",
                            payload={"attempt_id": attempt_b1, **review_b1["decision"]},
                        )
                    )

                    store.transition_attempt(
                        attempt_a1, AttemptState.ACCEPTED, reason="codex-review-pass"
                    )
                    store.transition_task(
                        task_a, TaskState.INTEGRATION, reason="review-passed"
                    )
                    integrated_a = manager.integrate(commit_a1)
                    if not integrated_a.applied:
                        raise RuntimeError("accepted worker-a commit conflicted")
                    store.transition_task(
                        task_a, TaskState.COMPLETED, reason="integration-passed"
                    )
                    store.transition_attempt(
                        attempt_b1, AttemptState.REJECTED, reason="codex-review-rework"
                    )
                    store.transition_task(
                        task_b, TaskState.READY, reason="review-requested-rework"
                    )

                    worker_b2 = manager.create_worktree(
                        "worker-b-attempt-2", base_commit
                    )
                    attempt_b2 = f"{task_b}-attempt-2"
                    session_b2 = f"agent-hub-{run_id}-worker-b-2"
                    store.append_message(
                        _message(
                            run_id=run_id,
                            task_id=task_b,
                            sequence=4,
                            sender="codex-supervisor-reviewer",
                            recipient="codebuddy-worker-b",
                            kind="task.rework_delegated",
                            payload={
                                "attempt_id": attempt_b2,
                                "session_id": session_b2,
                                "replaces_session_id": worker_result_b["session_id"],
                                "write_scope": ["demo/b.txt"],
                            },
                        )
                    )
                    lease_b2 = store.create_attempt_with_lease(
                        task_b, attempt_b2, "codebuddy-worker-b", lease_seconds=180
                    )
                    store.transition_attempt(
                        attempt_b2, AttemptState.RUNNING, reason="backend-resumed"
                    )
                    worker_result_b2 = await asyncio.to_thread(
                        run_codebuddy_write,
                        worker_b2,
                        "demo/b.txt",
                        TASK_B_FINAL_CONTENT,
                        session_id=session_b2,
                    )
                    evidence["workers"]["worker-b-attempt-2"] = worker_result_b2
                    if worker_result_b2["status"] != "completed":
                        raise RuntimeError("CodeBuddy rework attempt failed")
                    checks["codebuddy_rework_replaced_session"] = (
                        worker_result_b2.get("session_id") == session_b2
                        and worker_result_b2.get("session_id")
                        != worker_result_b.get("session_id")
                    )
                    commit_b2 = manager.commit_managed_changes(
                        worker_b2,
                        ("demo/b.txt",),
                        f"{task_b} {attempt_b2}",
                    )
                    store.submit_attempt(
                        attempt_b2,
                        lease_b2["generation"],
                        reason="rework-artifact-submitted",
                    )
                    store.append_message(
                        _message(
                            run_id=run_id,
                            task_id=task_b,
                            sequence=5,
                            sender="codebuddy-worker-b",
                            recipient="codex-supervisor-reviewer",
                            kind="artifact.submitted",
                            payload={
                                "attempt_id": attempt_b2,
                                "result_commit": commit_b2,
                            },
                        )
                    )
                    review_b2 = await review(
                        task_b,
                        attempt_b2,
                        "demo/b.txt",
                        TASK_B_FINAL_CONTENT,
                        commit_b2,
                    )
                    evidence["reviews"]["worker-b-attempt-2"] = review_b2
                    checks["review_b2_passed"] = review_b2["decision"] == {
                        "decision": "PASS",
                        "reason_code": "CRITERIA_MET",
                    } and review_b2["deterministic_match"]
                    if not checks["review_b2_passed"]:
                        raise RuntimeError("Codex rejected valid rework")
                    store.append_message(
                        _message(
                            run_id=run_id,
                            task_id=task_b,
                            sequence=6,
                            sender="codex-supervisor-reviewer",
                            recipient="codebuddy-worker-b",
                            kind="review.decided",
                            payload={"attempt_id": attempt_b2, **review_b2["decision"]},
                        )
                    )
                    store.transition_attempt(
                        attempt_b2, AttemptState.ACCEPTED, reason="codex-review-pass"
                    )
                    store.transition_task(
                        task_b, TaskState.INTEGRATION, reason="review-passed"
                    )
                    integrated_b = manager.integrate(commit_b2)
                    if not integrated_b.applied:
                        raise RuntimeError("accepted worker-b commit conflicted")
                    store.transition_task(
                        task_b, TaskState.COMPLETED, reason="integration-passed"
                    )

                    final_head = manager.head(repository)
                    checks.update(
                        {
                            "worker_a_completed": store.task_state(task_a)
                            == TaskState.COMPLETED,
                            "worker_b_completed": store.task_state(task_b)
                            == TaskState.COMPLETED,
                            "worker_b_first_attempt_rejected": store.attempt_state(
                                attempt_b1
                            )
                            == AttemptState.REJECTED,
                            "worker_b_second_attempt_accepted": store.attempt_state(
                                attempt_b2
                            )
                            == AttemptState.ACCEPTED,
                            "accepted_commits_integrated": integrated_a.applied
                            and integrated_b.applied,
                            "rejected_commit_not_integrated": not manager.is_ancestor(
                                commit_b1, final_head
                            ),
                            "deterministic_content_passed": (
                                (repository / "demo/a.txt").read_bytes()
                                == f"{TASK_A_CONTENT}\n".encode("utf-8")
                                and (repository / "demo/b.txt").read_bytes()
                                == f"{TASK_B_FINAL_CONTENT}\n".encode("utf-8")
                            ),
                            "integration_repository_clean": manager.is_clean(),
                            "structured_messages_persisted": (
                                sum(
                                    event["kind"] == "message.persisted"
                                    for event in store.events(task_id=task_a)
                                )
                                == 3
                                and sum(
                                    event["kind"] == "message.persisted"
                                    for event in store.events(task_id=task_b)
                                )
                                == 6
                            ),
                        }
                    )
                    evidence["git"] = {
                        "base_commit": base_commit,
                        "worker_a_result_commit": commit_a1,
                        "worker_b_rejected_commit": commit_b1,
                        "worker_b_accepted_commit": commit_b2,
                        "integration_a": integrated_a.to_dict(),
                        "integration_b": integrated_b.to_dict(),
                        "final_head": final_head,
                        "worktrees": {
                            "worker-a": str(worker_a),
                            "worker-b-attempt-1": str(worker_b),
                            "worker-b-attempt-2": str(worker_b2),
                        },
                    }
                    evidence["state_summary"] = store.summary(run_id=run_id)
            finally:
                if thread_id:
                    await asyncio.wait_for(
                        codex.thread_archive(thread_id), timeout=20.0
                    )
    except Exception as exc:
        failure = {"error_type": type(exc).__name__, "message": str(exc)}

    checkout_after = fingerprint_checkout(cwd)
    checks.update(
        {
            "user_checkout_head_unchanged": checkout_before.head == checkout_after.head,
            "user_checkout_status_unchanged": checkout_before.status_porcelain
            == checkout_after.status_porcelain,
            "user_checkout_contents_unchanged": checkout_before.workspace_sha256
            == checkout_after.workspace_sha256,
        }
    )
    evidence["user_checkout_before"] = checkout_before.to_dict()
    evidence["user_checkout_after"] = checkout_after.to_dict()
    evidence["usage"] = {
        "codex": "ChatGPT-authenticated; per-turn token usage recorded above",
        "codebuddy": "per-call provider usage fields recorded when returned",
        "automatic_purchase": False,
    }
    evidence["failure"] = failure
    preliminary = {
        "status": "pending",
        "mode": "real-poc",
        "run_id": run_id,
        "scenario_id": SCENARIO_ID,
        "checks": checks,
        "evidence": evidence,
    }
    sanitized, redaction_count = _redact_credentials(preliminary)
    preliminary = sanitized
    artifacts_contain_credentials = _artifact_credentials_found(
        (run_root, resolved_database)
    )
    preliminary["checks"]["plaintext_credentials_absent"] = (
        redaction_count == 0 and not artifacts_contain_credentials
    )
    run_passed = (
        failure is None and assess_real_poc(preliminary["checks"]) == "ready"
    )
    consecutive_passes = _record_acceptance_history(
        cwd / ".agent-hub" / "state" / "real-poc-history.jsonl",
        run_id=run_id,
        passed=run_passed,
    )
    stage_ready = run_passed and consecutive_passes >= 3
    preliminary["status"] = (
        "ready" if stage_ready else "run-passed" if run_passed else "error"
    )
    preliminary["acceptance"] = {
        "run_passed": run_passed,
        "consecutive_passes": consecutive_passes,
        "required_consecutive_passes": 3,
        "stage_ready": stage_ready,
        "redactions_applied": redaction_count,
    }
    report_path = reports / f"{run_id}.json"
    report_path.write_text(
        json.dumps(preliminary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    preliminary["report_path"] = str(report_path)
    return preliminary


def run_real_demo(
    cwd: Path,
    *,
    database_path: Path = Path(".agent-hub/state/agent-hub.db"),
) -> dict[str, Any]:
    return asyncio.run(_run_real_demo(cwd, database_path))
