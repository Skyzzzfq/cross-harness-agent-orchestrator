from __future__ import annotations

import json
import hashlib
import sqlite3
import uuid
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from orchestrator.adapters.contracts import (
    AccessPolicy,
    AdapterCallRequest,
    CallSnapshot,
    CallState,
    SessionRef,
)

from orchestrator.core.models import (
    AgentState,
    AttemptState,
    AuthorityToken,
    ControllerToken,
    MessageEnvelope,
    Recipient,
    RoleBindingState,
    SessionState,
    TaskState,
    utc_now,
)
from orchestrator.core.state_machine import (
    ensure_agent_transition,
    ensure_attempt_transition,
    ensure_session_transition,
    ensure_task_transition,
)


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {item.name: _jsonable(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value


def _call_request_digest(request: AdapterCallRequest) -> str:
    request_data = {
        "call_id": request.call_id,
        "run_id": request.run_id,
        "task_id": request.task_id,
        "attempt_id": request.attempt_id,
        "generation": request.generation,
        "agent_id": request.agent_id,
        "session_id": request.session.session_id,
        "backend": request.session.backend,
        "model": request.session.model,
        "provider_id": request.session.provider_id,
        "prompt": request.prompt,
        "policy": {
            "access_mode": request.policy.access_mode,
            "cwd": request.policy.cwd,
            "timeout_seconds": request.policy.timeout_seconds,
            "write_scope": request.policy.write_scope,
        },
    }
    return hashlib.sha256(
        json.dumps(request_data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    state TEXT NOT NULL,
    access_mode TEXT NOT NULL CHECK(access_mode IN ('read_only', 'write')),
    write_scope_json TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attempts (
    attempt_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    agent_id TEXT NOT NULL,
    state TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(task_id, attempt_number)
);

CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    envelope_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    task_id TEXT,
    attempt_id TEXT,
    kind TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT,
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class FencedAttemptError(RuntimeError):
    """Raised when an old or closed assignment tries to mutate active state."""


class FencedControllerError(RuntimeError):
    """Raised when a stale Run controller tries to mutate orchestrator state."""


class FencedAuthorityError(RuntimeError):
    """Raised when a stale business supervisor (old authority epoch) acts."""


# 当前数据库 schema 版本；迁移与测试共用，新增迁移时同步 +1。
CURRENT_SCHEMA_VERSION = 20


class SQLiteStateStore:
    # B3（P1-06）：Outbox 投递失败的最大重试次数，超过进入死信
    MAX_OUTBOX_ATTEMPTS = 5
    # R5：消息投递采用 at-least-once；达到上限后进入 FAILED，等待人工处理。
    MAX_MESSAGE_ATTEMPTS = 5
    MESSAGE_KINDS = {
        "dispatch",
        "status_update",
        "result",
        "review",
        "user_guidance",
        "terminate",
        "ack",
    }

    def __init__(
        self,
        path: Path,
        *,
        workspace_policy: Any | None = None,
        workspace_manager: Any | None = None,
    ) -> None:
        self.path = path
        self.workspace_policy = workspace_policy
        self.workspace_manager = workspace_manager
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 允许跨线程使用同一连接（控制台等本地工具在服务线程访问）；
        # SQLite 会串行化写事务，调用方仍需避免并发修改同一连接。
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        self._migrate_schema()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> SQLiteStateStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def create_run(
        self,
        run_id: str,
        team_id: str,
        *,
        team_spec: Any | None = None,
        approval_mode: str = "auto",
    ) -> None:
        if approval_mode not in {"auto", "manual"}:
            raise ValueError("approval_mode must be auto or manual")
        snapshot_json = None
        snapshot_digest = None
        if team_spec is not None:
            from orchestrator.core.role_registry import (
                snapshot_digest as calculate_snapshot_digest,
                team_snapshot,
            )

            snapshot = team_snapshot(team_spec)
            snapshot_json = json.dumps(
                snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            snapshot_digest = calculate_snapshot_digest(snapshot)
        now = utc_now()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO runs(
                    run_id, team_id, created_at, team_snapshot_json,
                    team_snapshot_digest, approval_mode
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, team_id, now, snapshot_json, snapshot_digest, approval_mode),
            )
            self._append_event(run_id, None, None, "run.created", None, None, {})

    def run_approval_mode(self, run_id: str) -> str:
        row = self.connection.execute(
            "SELECT approval_mode FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return str(row["approval_mode"] or "auto")

    def run_snapshot(self, run_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT team_snapshot_json, team_snapshot_digest, approval_mode "
            "FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        snapshot = None
        if row["team_snapshot_json"]:
            snapshot = json.loads(str(row["team_snapshot_json"]))
        return {
            "snapshot": snapshot,
            "digest": row["team_snapshot_digest"],
            "approval_mode": str(row["approval_mode"] or "auto"),
        }

    def list_artifacts(self, run_id: str, *, task_id: str | None = None) -> list[dict[str, Any]]:
        if task_id is None:
            rows = self.connection.execute(
                "SELECT * FROM artifacts WHERE run_id=? ORDER BY published_at, artifact_id",
                (run_id,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM artifacts WHERE run_id=? AND task_id=? "
                "ORDER BY published_at, artifact_id",
                (run_id, task_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_artifact(self, entry: Mapping[str, Any]) -> str:
        required = {
            "artifact_id", "run_id", "task_id", "attempt_id", "path",
            "sha256", "producer", "version", "published_at",
        }
        missing = required - set(entry)
        if missing:
            raise ValueError(f"artifact entry missing fields: {sorted(missing)}")
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO artifacts(
                    artifact_id, run_id, task_id, attempt_id, path, sha256,
                    candidate_commit, producer, version, published_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(entry["artifact_id"]), str(entry["run_id"]),
                    str(entry["task_id"]), str(entry["attempt_id"]),
                    str(entry["path"]), str(entry["sha256"]),
                    entry.get("candidate_commit"), str(entry["producer"]),
                    int(entry["version"]), str(entry["published_at"]),
                ),
            )
        return str(entry["artifact_id"])

    def list_handoffs(
        self, run_id: str, *, task_id: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM handoffs WHERE run_id = ?"
        args: list[Any] = [run_id]
        if task_id is not None:
            sql += " AND task_id = ?"
            args.append(task_id)
        sql += " ORDER BY created_at, handoff_id"
        return [dict(row) for row in self.connection.execute(sql, args).fetchall()]

    def get_handoff(self, task_id: str, attempt_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM handoffs WHERE task_id = ? AND attempt_id = ?",
            (task_id, attempt_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"handoff:{task_id}:{attempt_id}")
        result = dict(row)
        result["package"] = json.loads(str(result["package_json"]))
        return result

    def list_task_results(
        self, run_id: str, *, task_id: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM task_results WHERE run_id = ?"
        args: list[Any] = [run_id]
        if task_id is not None:
            sql += " AND task_id = ?"
            args.append(task_id)
        sql += " ORDER BY created_at, result_id"
        rows = self.connection.execute(sql, args).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["result"] = json.loads(str(item["result_json"]))
            item["artifact_refs"] = json.loads(str(item["artifact_refs_json"]))
            results.append(item)
        return results

    def list_verification_evidence(
        self, run_id: str, *, task_id: str | None = None,
        attempt_id: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM verification_evidence WHERE run_id = ?"
        args: list[Any] = [run_id]
        if task_id is not None:
            sql += " AND task_id = ?"
            args.append(task_id)
        if attempt_id is not None:
            sql += " AND attempt_id = ?"
            args.append(attempt_id)
        sql += " ORDER BY created_at, evidence_id"
        return [dict(row) for row in self.connection.execute(sql, args).fetchall()]

    def record_verification_evidence(
        self,
        run_id: str,
        task_id: str,
        attempt_id: str,
        *,
        candidate_commit: str,
        check_name: str,
        check_definition_digest: str,
        command: Any,
        cwd: str,
        exit_code: int,
        status: str,
        output_sha256: str,
        output_summary: str,
        environment: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in {"PASS", "FAIL", "BLOCKED"}:
            raise ValueError("verification status must be PASS, FAIL, or BLOCKED")
        for name, value in (
            ("candidate_commit", candidate_commit),
            ("check_name", check_name),
            ("check_definition_digest", check_definition_digest),
            ("cwd", cwd),
            ("output_sha256", output_sha256),
        ):
            if not str(value).strip():
                raise ValueError(f"{name} must not be empty")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            owner = self.connection.execute(
                """
                SELECT t.run_id, a.workspace_path
                FROM tasks t JOIN attempts a ON a.task_id = t.task_id
                WHERE t.run_id = ? AND t.task_id = ? AND a.attempt_id = ?
                """,
                (run_id, task_id, attempt_id),
            ).fetchone()
            if owner is None:
                raise FencedAttemptError("verification references an unknown attempt")
            candidate = self.connection.execute(
                """
                SELECT 1 FROM task_results
                WHERE run_id = ? AND task_id = ? AND attempt_id = ?
                  AND candidate_commit = ?
                  AND status IN ('submitted', 'verified')
                LIMIT 1
                """,
                (run_id, task_id, attempt_id, candidate_commit),
            ).fetchone()
            if candidate is None:
                raise ValueError("verification candidate is not a published worker result")
            now = utc_now()
            command_json = json.dumps(
                command, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            environment_json = json.dumps(
                dict(environment or {}), ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            )
            existing = self.connection.execute(
                """
                SELECT * FROM verification_evidence
                WHERE attempt_id = ? AND candidate_commit = ? AND check_name = ?
                  AND check_definition_digest = ?
                """,
                (attempt_id, candidate_commit, check_name, check_definition_digest),
            ).fetchone()
            if existing is not None:
                self.connection.commit()
                return dict(existing)
            evidence_id = f"evidence-{uuid.uuid4().hex[:16]}"
            self.connection.execute(
                """
                INSERT INTO verification_evidence(
                    evidence_id, run_id, task_id, attempt_id, candidate_commit,
                    check_name, check_definition_digest, command_json, cwd,
                    exit_code, status, output_sha256, output_summary,
                    environment_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence_id, run_id, task_id, attempt_id, candidate_commit,
                    check_name, check_definition_digest, command_json, cwd,
                    int(exit_code), status, output_sha256, str(output_summary)[:4000],
                    environment_json, now,
                ),
            )
            self._append_event(
                run_id, task_id, attempt_id, "verification.evidence.recorded",
                None, status,
                {
                    "evidence_id": evidence_id,
                    "candidate_commit": candidate_commit,
                    "check_name": check_name,
                    "status": status,
                },
            )
            self.connection.commit()
            return dict(
                self.connection.execute(
                    "SELECT * FROM verification_evidence WHERE evidence_id = ?",
                    (evidence_id,),
                ).fetchone()
            )
        except BaseException:
            self.connection.rollback()
            raise

    def verification_gate(
        self, run_id: str, task_id: str, attempt_id: str, candidate_commit: str
    ) -> dict[str, Any]:
        evidence = self.connection.execute(
            """
            SELECT evidence_id, check_name, status, candidate_commit
            FROM verification_evidence
            WHERE run_id = ? AND task_id = ? AND attempt_id = ?
              AND candidate_commit = ?
            ORDER BY created_at, evidence_id
            """,
            (run_id, task_id, attempt_id, candidate_commit),
        ).fetchall()
        decisions = self.connection.execute(
            """
            SELECT decision_id, layer, decision, detail_json
            FROM review_decisions
            WHERE run_id = ? AND task_id = ? AND attempt_id = ?
              AND decision IN ('PASS', 'APPROVED')
            ORDER BY created_at, decision_id
            """,
            (run_id, task_id, attempt_id),
        ).fetchall()
        passed = [row for row in evidence if row["status"] == "PASS"]
        return {
            "candidate_commit": candidate_commit,
            "evidence": [dict(row) for row in evidence],
            "passed_checks": [str(row["check_name"]) for row in passed],
            "review_decisions": [dict(row) for row in decisions],
            "ready": bool(passed and decisions),
        }

    def _candidate_commit_exists_tx(self, workspace_path: str | None, commit: str) -> bool:
        if not commit.strip():
            return False
        if workspace_path:
            try:
                from orchestrator.workspace.git_manager import _git

                return (
                    _git(
                        Path(workspace_path),
                        "cat-file",
                        "-e",
                        f"{commit}^{{commit}}",
                        check=False,
                    ).returncode
                    == 0
                )
            except (OSError, RuntimeError):
                return False
        return False

    def record_worker_result(
        self,
        run_id: str,
        task_id: str,
        attempt_id: str,
        result: Mapping[str, Any],
        *,
        verified: bool = False,
        verified_by: str | None = None,
    ) -> dict[str, Any]:
        """Record one fenced, structured result without granting completion by text."""
        from orchestrator.core.handoff import result_digest, validate_result_payload

        if not isinstance(result, Mapping):
            raise ValueError("worker result must be a structured object")
        if verified:
            raise ValueError("worker results cannot self-verify; call verify_worker_result")
        payload = validate_result_payload(
            result, run_id=run_id, task_id=task_id, attempt_id=attempt_id
        )
        candidate_commit = payload.get("candidate_commit")
        if candidate_commit is not None and not isinstance(candidate_commit, str):
            raise ValueError("candidate_commit must be a string or null")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                """
                SELECT t.*, a.state AS attempt_state, a.workspace_path,
                       a.generation, a.result_json AS attempt_result_json
                FROM tasks t JOIN attempts a ON a.task_id = t.task_id
                WHERE t.run_id = ? AND t.task_id = ? AND a.attempt_id = ?
                """,
                (run_id, task_id, attempt_id),
            ).fetchone()
            if row is None:
                raise FencedAttemptError("result references an unknown task attempt")
            if row["attempt_state"] not in {
                AttemptState.ASSIGNED.value,
                AttemptState.RUNNING.value,
                AttemptState.SUBMITTED.value,
            }:
                raise FencedAttemptError("closed or stale attempt cannot publish a result")
            if str(row["access_mode"]) == "write" and payload["status"] == "completed":
                if not candidate_commit:
                    raise ValueError("completed write result must reference candidate_commit")
                if not self._candidate_commit_exists_tx(row["workspace_path"], candidate_commit):
                    raise ValueError("candidate_commit does not exist in the attempt repository")
            raw_refs = payload.get("artifact_refs", ())
            normalized_refs: list[str] = []
            for ref in raw_refs:
                artifact_id = (
                    str(ref.get("artifact_id"))
                    if isinstance(ref, Mapping) and ref.get("artifact_id")
                    else str(ref)
                )
                if not artifact_id.strip():
                    raise ValueError("artifact_refs cannot contain an empty reference")
                artifact = self.connection.execute(
                    """
                    SELECT candidate_commit FROM artifacts
                    WHERE artifact_id = ? AND run_id = ? AND task_id = ? AND attempt_id = ?
                    """,
                    (artifact_id, run_id, task_id, attempt_id),
                ).fetchone()
                if artifact is None:
                    raise ValueError(f"artifact reference is not owned by this attempt: {artifact_id}")
                if candidate_commit and artifact["candidate_commit"] not in {
                    None, candidate_commit
                }:
                    raise ValueError("artifact reference candidate_commit does not match result")
                normalized_refs.append(artifact_id)
            payload["artifact_refs"] = normalized_refs
            # A supervisor may cite child results, but every cited result must
            # belong to a direct child.  This prevents a model from inventing
            # completion on behalf of another Worker.
            child_refs = payload.get("child_result_refs", ())
            if child_refs is not None and not isinstance(child_refs, (list, tuple)):
                raise ValueError("child_result_refs must be an array")
            if child_refs:
                for child_ref in child_refs:
                    child_id = str(child_ref)
                    child = self.connection.execute(
                        """
                        SELECT r.result_id
                        FROM task_results r JOIN tasks child ON child.task_id = r.task_id
                        WHERE r.result_id = ? AND r.run_id = ?
                          AND r.status = 'verified' AND child.parent_task_id = ?
                        """,
                        (child_id, run_id, task_id),
                    ).fetchone()
                    if child is None:
                        raise ValueError(f"child result is not a verified direct child: {child_id}")
                payload["child_result_refs"] = [str(item) for item in child_refs]
            digest = result_digest(payload)
            stored_status = {
                "completed": "submitted",
                "failed": "blocked",
                "needs_input": "needs_input",
                "blocked": "blocked",
            }[payload["status"]]
            existing = self.connection.execute(
                "SELECT * FROM task_results WHERE attempt_id = ? AND result_digest = ?",
                (attempt_id, digest),
            ).fetchone()
            if existing is not None:
                self.connection.commit()
                return dict(existing)
            now = utc_now()
            result_id = f"result-{uuid.uuid4().hex[:16]}"
            result_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            refs_json = json.dumps(normalized_refs, ensure_ascii=False, sort_keys=True)
            self.connection.execute(
                """
                INSERT INTO task_results(
                    result_id, run_id, task_id, attempt_id, status, result_json,
                    result_digest, candidate_commit, artifact_refs_json,
                    verified_by, created_at, verified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result_id, run_id, task_id, attempt_id, stored_status,
                    result_json, digest, candidate_commit, refs_json,
                    None, now, None,
                ),
            )
            self.connection.execute(
                """
                UPDATE attempts SET result_json = ?, candidate_commit = ?,
                    artifact_refs_json = ?, claimed_checks_json = ?,
                    remaining_issues_json = ?, updated_at = ?
                WHERE attempt_id = ?
                """,
                (
                    result_json, candidate_commit, refs_json,
                    json.dumps(payload["claimed_checks"], ensure_ascii=False),
                    json.dumps(payload["remaining_issues"], ensure_ascii=False),
                    now, attempt_id,
                ),
            )
            self._append_event(
                run_id, task_id, attempt_id, "task.result.recorded",
                row["state"], row["state"],
                {
                    "result_id": result_id,
                    "status": stored_status,
                    "result_digest": digest,
                    "verified": False,
                },
            )
            self.connection.commit()
            return dict(
                self.connection.execute(
                    "SELECT * FROM task_results WHERE result_id = ?", (result_id,)
                ).fetchone()
            )
        except BaseException:
            self.connection.rollback()
            raise

    def verify_worker_result(
        self, result_id: str, *, verified_by: str = "hub"
    ) -> dict[str, Any]:
        if not verified_by.strip():
            raise ValueError("verified_by must not be empty")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT * FROM task_results WHERE result_id = ?", (result_id,)
            ).fetchone()
            if row is None:
                raise KeyError(result_id)
            if row["status"] == "verified":
                self.connection.commit()
                return dict(row)
            if row["status"] != "submitted":
                raise ValueError(f"result {result_id} is {row['status']}, not submitted")
            now = utc_now()
            self.connection.execute(
                """
                UPDATE task_results SET status='verified', verified_by=?, verified_at=?
                WHERE result_id=? AND status='submitted'
                """,
                (verified_by, now, result_id),
            )
            self._append_event(
                row["run_id"], row["task_id"], row["attempt_id"],
                "task.result.verified", "submitted", "verified",
                {"result_id": result_id, "verified_by": verified_by},
            )
            self._reconcile_task_graph_tx(row["run_id"], now)
            self.connection.commit()
            return dict(
                self.connection.execute(
                    "SELECT * FROM task_results WHERE result_id = ?", (result_id,)
                ).fetchone()
            )
        except BaseException:
            self.connection.rollback()
            raise

    def aggregate_parent_task(
        self, run_id: str, parent_task_id: str, *, summary_result_id: str | None = None
    ) -> dict[str, Any]:
        """Complete a parent only after every required child and its summary are verified."""
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            parent = self.connection.execute(
                "SELECT * FROM tasks WHERE run_id = ? AND task_id = ?",
                (run_id, parent_task_id),
            ).fetchone()
            if parent is None:
                raise KeyError(parent_task_id)
            children = self.connection.execute(
                "SELECT task_id, required_delivery, state FROM tasks "
                "WHERE run_id = ? AND parent_task_id = ? ORDER BY created_at, task_id",
                (run_id, parent_task_id),
            ).fetchall()
            required = [child for child in children if int(child["required_delivery"])]
            verified: dict[str, sqlite3.Row] = {}
            for child in required:
                result = self.connection.execute(
                    """
                    SELECT * FROM task_results
                    WHERE run_id = ? AND task_id = ? AND status = 'verified'
                    ORDER BY verified_at DESC, created_at DESC LIMIT 1
                    """,
                    (run_id, child["task_id"]),
                ).fetchone()
                if result is not None:
                    verified[str(child["task_id"])] = result
            summary = None
            if summary_result_id:
                summary = self.connection.execute(
                    """SELECT * FROM task_results WHERE result_id = ?
                       AND run_id = ? AND task_id = ? AND status = 'verified'""",
                    (summary_result_id, run_id, parent_task_id),
                ).fetchone()
            child_ids = [str(child["task_id"]) for child in required]
            delivered = len(verified) == len(required)
            summary_refs: set[str] = set()
            if summary is not None:
                summary_payload = json.loads(str(summary["result_json"]))
                summary_refs = {str(ref) for ref in summary_payload.get("child_result_refs", ())}
            required_result_ids = {str(verified[item]["result_id"]) for item in child_ids}
            summary_complete = summary is not None and required_result_ids <= summary_refs
            completed = False
            if delivered and summary_complete and TaskState(parent["state"]) == TaskState.REVIEW:
                self._transition_task_tx(
                    parent_task_id, TaskState.COMPLETED,
                    "verified-child-results-aggregated", utc_now()
                )
                completed = True
            self._append_event(
                run_id, parent_task_id, None, "parent.aggregation.checked",
                parent["state"], parent["state"],
                {
                    "required_children": child_ids,
                    "delivered_children": sorted(verified),
                    "optional_failures_disclosed": [
                        str(child["task_id"])
                        for child in children
                        if not int(child["required_delivery"])
                        and child["state"] in {TaskState.FAILED.value, TaskState.CANCELLED.value}
                    ],
                    "summary_result_id": summary_result_id,
                    "completed": completed,
                },
            )
            self.connection.commit()
            return {
                "run_id": run_id,
                "parent_task_id": parent_task_id,
                "required_children": child_ids,
                "delivered_children": sorted(verified),
                "completed": completed,
                "summary_complete": summary_complete,
            }
        except BaseException:
            self.connection.rollback()
            raise

    def save_supervisor_plan(
        self,
        run_id: str,
        supervisor_task_id: str,
        plan: Any,
        *,
        call_id: str | None = None,
        status: str = "pending_approval",
        format_repair_count: int = 0,
        error: str | None = None,
    ) -> dict[str, Any]:
        """Persist one immutable plan revision and return its preview."""
        if status not in {
            "generating",
            "pending_approval",
            "approved",
            "executing",
            "verifying",
            "delivered",
            "format_failed",
            "needs_revision",
            "cancelled",
        }:
            raise ValueError(f"unsupported supervisor plan status: {status}")
        now = utc_now()
        revision = int(getattr(plan, "revision", 1))
        plan_json = json.dumps(
            _jsonable(plan.to_dict() if hasattr(plan, "to_dict") else plan),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        permissions_json = json.dumps(
            _jsonable(getattr(plan, "permissions", None)),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        budget_json = json.dumps(
            _jsonable(getattr(plan, "budget", None)),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = str(getattr(plan, "digest", ""))
        with self.connection:
            existing = self.connection.execute(
                "SELECT * FROM supervisor_plans WHERE run_id=? AND supervisor_task_id=? AND revision=?",
                (run_id, supervisor_task_id, revision),
            ).fetchone()
            if existing is not None:
                return dict(existing)
            plan_id = f"plan-{uuid.uuid4().hex[:16]}"
            self.connection.execute(
                """
                INSERT INTO supervisor_plans(
                    plan_id, run_id, supervisor_task_id, revision, status,
                    plan_json, plan_digest, permissions_json, budget_json,
                    worker_concurrency, format_repair_count, source_call_id,
                    error_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    run_id,
                    supervisor_task_id,
                    revision,
                    status,
                    plan_json,
                    digest,
                    permissions_json,
                    budget_json,
                    int(getattr(plan, "worker_concurrency", 1)),
                    int(format_repair_count),
                    call_id,
                    json.dumps({"reason": error}, ensure_ascii=False) if error else None,
                    now,
                    now,
                ),
            )
            self._append_event(
                run_id,
                supervisor_task_id,
                None,
                "plan.pending" if status == "pending_approval" else f"plan.{status}",
                TaskState.REVIEW.value,
                TaskState.REVIEW.value,
                {
                    "call_id": call_id,
                    "revision": revision,
                    "plan_digest": digest,
                    "status": status,
                },
            )
            return dict(
                self.connection.execute(
                    "SELECT * FROM supervisor_plans WHERE plan_id=?", (plan_id,)
                ).fetchone()
            )

    def latest_supervisor_plan(
        self, run_id: str, supervisor_task_id: str
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM supervisor_plans WHERE run_id=? AND supervisor_task_id=? "
            "ORDER BY revision DESC LIMIT 1",
            (run_id, supervisor_task_id),
        ).fetchone()
        return None if row is None else dict(row)

    def list_supervisor_plans(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM supervisor_plans WHERE run_id=? ORDER BY revision, plan_id",
            (run_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def approve_supervisor_plan(
        self,
        run_id: str,
        supervisor_task_id: str,
        revision: int,
        plan_digest: str,
        *,
        controller: ControllerToken,
        authority: AuthorityToken,
        permissions: Any = None,
    ) -> dict[str, Any]:
        """Atomically approve a plan revision; repeating the same approval is safe."""
        now = utc_now()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self._ensure_controller_tx(controller, now)
            self._ensure_authority_tx(authority, now)
            row = self.connection.execute(
                "SELECT * FROM supervisor_plans WHERE run_id=? AND supervisor_task_id=? AND revision=?",
                (run_id, supervisor_task_id, int(revision)),
            ).fetchone()
            if row is None:
                raise KeyError(f"plan revision {revision} not found")
            if str(row["plan_digest"]) != str(plan_digest):
                raise ValueError("plan digest does not match the selected revision")
            if permissions is not None:
                expected = json.loads(row["permissions_json"] or "null")
                if _jsonable(permissions) != expected:
                    raise ValueError("plan permissions do not match the selected revision")
            current = str(row["status"])
            if current in {"approved", "executing", "verifying", "delivered"}:
                self.connection.commit()
                return dict(row)
            if current != "pending_approval":
                raise ValueError(f"plan is {current}, must be pending_approval")
            self.connection.execute(
                "UPDATE supervisor_plans SET status='approved', updated_at=? WHERE plan_id=?",
                (now, row["plan_id"]),
            )
            self._append_event(
                run_id,
                supervisor_task_id,
                None,
                "plan.approved",
                TaskState.REVIEW.value,
                TaskState.REVIEW.value,
                {
                    "revision": int(revision),
                    "plan_digest": str(plan_digest),
                    "controller_epoch": controller.epoch,
                    "authority_epoch": authority.epoch,
                },
            )
            updated = self.connection.execute(
                "SELECT * FROM supervisor_plans WHERE plan_id=?", (row["plan_id"],)
            ).fetchone()
            self.connection.commit()
            return dict(updated)
        except BaseException:
            self.connection.rollback()
            raise

    def acquire_run_controller(
        self,
        run_id: str,
        owner_id: str,
        *,
        lease_seconds: int = 60,
        now: str | None = None,
    ) -> ControllerToken | None:
        if not owner_id.strip():
            raise ValueError("owner_id must not be empty")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be at least 1")
        as_of = self._aware_datetime(now)
        expires_at = (as_of + timedelta(seconds=lease_seconds)).isoformat()
        acquired = False
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            run = self.connection.execute(
                "SELECT 1 FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            row = self.connection.execute(
                "SELECT * FROM run_controller_leases WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                epoch = 1
                acquired = True
                self.connection.execute(
                    """
                    INSERT INTO run_controller_leases(
                        run_id, owner_id, epoch, acquired_at,
                        heartbeat_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        owner_id,
                        epoch,
                        as_of.isoformat(),
                        as_of.isoformat(),
                        expires_at,
                    ),
                )
            elif row["expires_at"] > as_of.isoformat():
                if row["owner_id"] != owner_id:
                    self.connection.commit()
                    return None
                epoch = int(row["epoch"])
                self.connection.execute(
                    """
                    UPDATE run_controller_leases
                    SET heartbeat_at = ?, expires_at = ? WHERE run_id = ?
                    """,
                    (as_of.isoformat(), expires_at, run_id),
                )
            else:
                epoch = int(row["epoch"]) + 1
                acquired = True
                self.connection.execute(
                    """
                    UPDATE run_controller_leases
                    SET owner_id = ?, epoch = ?, acquired_at = ?,
                        heartbeat_at = ?, expires_at = ? WHERE run_id = ?
                    """,
                    (
                        owner_id,
                        epoch,
                        as_of.isoformat(),
                        as_of.isoformat(),
                        expires_at,
                        run_id,
                    ),
                )
            if acquired:
                self._append_event(
                    run_id,
                    None,
                    None,
                    "controller.acquired",
                    None,
                    str(epoch),
                    {"owner_id": owner_id, "epoch": epoch, "expires_at": expires_at},
                )
            self.connection.commit()
            return ControllerToken(run_id, owner_id, epoch, expires_at)
        except BaseException:
            self.connection.rollback()
            raise

    def active_run_controller(self, run_id: str) -> dict[str, Any] | None:
        """Return the current controller lease when it is still valid.

        The console uses this only for explicitly supported operator actions
        while the long-running ``serve`` loop owns the lease.  Returning the
        lease as data keeps the fencing token immutable and avoids stealing the
        controller from the scheduler.
        """
        now = self._aware_datetime(None).isoformat()
        row = self.connection.execute(
            """
            SELECT run_id, owner_id, epoch, acquired_at, heartbeat_at, expires_at
            FROM run_controller_leases
            WHERE run_id = ? AND expires_at > ?
            """,
            (run_id, now),
        ).fetchone()
        return None if row is None else dict(row)

    def materialize_supervisor_plan(
        self,
        run_id: str,
        supervisor_task_id: str,
        plan: Any,
        *,
        controller: ControllerToken,
        authority: AuthorityToken,
        worker_cwd: str | None = None,
        max_attempts: int = 2,
        timeout_seconds: float = 120,
    ) -> dict[str, Any]:
        """Atomically turn one validated supervisor plan into worker tasks."""
        if controller.run_id != run_id or authority.run_id != run_id:
            raise FencedControllerError("plan tokens belong to another Run")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        now = utc_now()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self._ensure_controller_tx(controller, now)
            self._ensure_authority_tx(authority, now)
            parent = self.connection.execute(
                """
                SELECT t.run_id, t.state, d.cwd
                FROM tasks t
                JOIN task_dispatch_specs d ON d.task_id = t.task_id
                WHERE t.task_id = ?
                """,
                (supervisor_task_id,),
            ).fetchone()
            if parent is None or parent["run_id"] != run_id:
                raise KeyError(supervisor_task_id)
            existing_event = self.connection.execute(
                """
                SELECT data_json FROM events
                WHERE run_id = ? AND task_id = ? AND kind = 'plan.materialized'
                ORDER BY rowid DESC LIMIT 1
                """,
                (run_id, supervisor_task_id),
            ).fetchone()
            if existing_event is not None:
                data = json.loads(existing_event["data_json"])
                if data.get("plan_digest") == plan.digest:
                    self.connection.commit()
                    return {
                        "status": "already-materialized",
                        "supervisor_task_id": supervisor_task_id,
                        "task_ids": list(data.get("task_ids", ())),
                        "plan_digest": plan.digest,
                    }
                raise ValueError("supervisor task already materialized a different plan")
            if parent["state"] != TaskState.REVIEW.value:
                raise ValueError(
                    f"supervisor task {supervisor_task_id} is {parent['state']}, "
                    "must be REVIEW"
                )

            task_ids = [task.task_id for task in plan.tasks]
            if len(set(task_ids)) != len(task_ids):
                raise ValueError("supervisor plan contains duplicate task_id values")
            existing_ids = {
                str(row["task_id"])
                for row in self.connection.execute(
                    "SELECT task_id FROM tasks WHERE run_id = ?", (run_id,)
                )
            }
            duplicate = set(task_ids) & existing_ids
            if duplicate:
                raise ValueError(f"task already exists: {sorted(duplicate)}")
            cwd = str(worker_cwd or parent["cwd"])
            normalized: list[dict[str, Any]] = []
            for task in plan.tasks:
                scope = tuple(task.write_scope)
                self._validate_task_spec(
                    task_id=task.task_id,
                    access_mode=task.access_mode,
                    max_attempts=max_attempts,
                    required_role_id=task.role_id,
                    required_backend=task.backend,
                    cwd=cwd,
                    timeout_seconds=timeout_seconds,
                    retry_backoff_base_seconds=1,
                    retry_backoff_max_seconds=60,
                )
                if task.access_mode != "write" and scope:
                    raise ValueError(
                        f"read_only task {task.task_id} cannot declare write_scope"
                    )
                task_cwd = cwd
                if self.workspace_policy is not None:
                    task_cwd = self.workspace_policy.validate_cwd(
                        task.access_mode, cwd
                    )
                    if task.access_mode == "write":
                        scope = self.workspace_policy.validate_write_scope(
                            scope, base=task_cwd
                        )
                elif task.access_mode == "write":
                    from orchestrator.workspace.policy import validate_write_scope_static

                    scope = validate_write_scope_static(scope)
                normalized.append(
                    {
                        "task_id": task.task_id,
                        "access_mode": task.access_mode,
                        "write_scope": scope,
                        "max_attempts": max_attempts,
                        "required_role_id": task.role_id,
                        "required_backend": task.backend,
                        "required_model": getattr(task, "required_model", None),
                        "required_provider_id": getattr(task, "required_provider_id", None),
                        "prompt": task.prompt,
                        "cwd": task_cwd,
                        "timeout_seconds": timeout_seconds,
                        "priority": 0,
                        "retry_backoff_base_seconds": 1,
                        "retry_backoff_max_seconds": 60,
                        "parent_task_id": supervisor_task_id,
                        "dispatch_source": "supervisor_plan",
                        "required_delivery": True,
                        "task_kind": getattr(task, "task_kind", "implementation"),
                        "acceptance_criteria": tuple(
                            getattr(task, "acceptance_criteria", ())
                        ),
                        "input_refs": tuple(getattr(task, "input_refs", ())),
                        "output_contract": getattr(task, "output_contract", None),
                        "budget": getattr(task, "budget", None),
                    }
                )

            dependencies = [
                (task.task_id, dependency)
                for task in plan.tasks
                for dependency in task.depends_on
            ]
            task_id_set = set(task_ids)
            if any(dependency not in task_id_set for _, dependency in dependencies):
                raise ValueError("unknown dependency task in supervisor plan")
            graph_nodes = existing_ids | task_id_set
            graph_edges = [
                (str(row["task_id"]), str(row["depends_on_task_id"]))
                for row in self.connection.execute(
                    """
                    SELECT task_id, depends_on_task_id FROM task_dependencies
                    WHERE run_id = ?
                    """,
                    (run_id,),
                )
            ] + dependencies
            remaining = {node: 0 for node in graph_nodes}
            dependents: dict[str, list[str]] = {node: [] for node in graph_nodes}
            for task_id, dependency in graph_edges:
                remaining[task_id] += 1
                dependents[dependency].append(task_id)
            ready = [node for node, count in remaining.items() if count == 0]
            visited = 0
            while ready:
                node = ready.pop()
                visited += 1
                for dependent in dependents[node]:
                    remaining[dependent] -= 1
                    if remaining[dependent] == 0:
                        ready.append(dependent)
            if visited != len(graph_nodes):
                raise ValueError("task dependency graph contains a cycle")

            for spec in normalized:
                self._create_task_tx(run_id=run_id, now=now, **spec)
            for task_id, dependency in dependencies:
                self.connection.execute(
                    """
                    INSERT INTO task_dependencies(
                        run_id, task_id, depends_on_task_id, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (run_id, task_id, dependency, now),
                )
            self._reconcile_task_graph_tx(run_id, now)
            self._append_event(
                run_id,
                supervisor_task_id,
                None,
                "plan.materialized",
                TaskState.REVIEW.value,
                TaskState.REVIEW.value,
                {
                    "plan_digest": plan.digest,
                    "task_ids": task_ids,
                    "controller_epoch": controller.epoch,
                    "authority_epoch": authority.epoch,
                },
            )
            self.connection.execute(
                """
                UPDATE supervisor_plans
                SET status='executing', updated_at=?
                WHERE run_id=? AND supervisor_task_id=? AND revision=?
                """,
                (now, run_id, supervisor_task_id, int(getattr(plan, "revision", 1))),
            )
            self.connection.commit()
            return {
                "status": "materialized",
                "supervisor_task_id": supervisor_task_id,
                "task_ids": task_ids,
                "plan_digest": plan.digest,
            }
        except BaseException:
            self.connection.rollback()
            raise

    def record_supervisor_plan_rejection(
        self,
        run_id: str,
        supervisor_task_id: str,
        *,
        call_id: str,
        reason: str,
        controller: ControllerToken,
        authority: AuthorityToken,
    ) -> bool:
        """Record one rejection for a supervisor result, idempotently."""
        now = utc_now()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self._ensure_controller_tx(controller, now)
            self._ensure_authority_tx(authority, now)
            parent = self.connection.execute(
                "SELECT run_id FROM tasks WHERE task_id = ?",
                (supervisor_task_id,),
            ).fetchone()
            if parent is None or parent["run_id"] != run_id:
                raise KeyError(supervisor_task_id)
            duplicate = self.connection.execute(
                """
                SELECT 1 FROM events
                WHERE run_id = ? AND task_id = ? AND kind = 'plan.rejected'
                  AND json_extract(data_json, '$.call_id') = ?
                LIMIT 1
                """,
                (run_id, supervisor_task_id, call_id),
            ).fetchone()
            if duplicate is not None:
                self.connection.commit()
                return False
            self._append_event(
                run_id,
                supervisor_task_id,
                None,
                "plan.rejected",
                TaskState.REVIEW.value,
                TaskState.REVIEW.value,
                {"call_id": call_id, "reason": reason[:500]},
            )
            self.connection.commit()
            return True
        except BaseException:
            self.connection.rollback()
            raise

    def record_supervisor_plan_needs_input(
        self,
        run_id: str,
        supervisor_task_id: str,
        *,
        call_id: str,
        reason: str,
        controller: ControllerToken,
        authority: AuthorityToken,
    ) -> None:
        """Stop after the single format repair and leave the parent actionable."""
        now = utc_now()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self._ensure_controller_tx(controller, now)
            self._ensure_authority_tx(authority, now)
            self._append_event(
                run_id,
                supervisor_task_id,
                None,
                "plan.needs_input",
                TaskState.REVIEW.value,
                TaskState.REVIEW.value,
                {"call_id": call_id, "reason": reason[:500]},
            )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def renew_run_controller(
        self,
        token: ControllerToken,
        *,
        lease_seconds: int = 60,
        now: str | None = None,
    ) -> ControllerToken:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be at least 1")
        as_of = self._aware_datetime(now)
        expires_at = (as_of + timedelta(seconds=lease_seconds)).isoformat()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self._ensure_controller_tx(token, as_of.isoformat())
            self.connection.execute(
                """
                UPDATE run_controller_leases SET heartbeat_at = ?, expires_at = ?
                WHERE run_id = ? AND owner_id = ? AND epoch = ?
                """,
                (
                    as_of.isoformat(),
                    expires_at,
                    token.run_id,
                    token.owner_id,
                    token.epoch,
                ),
            )
            self.connection.commit()
            return ControllerToken(
                token.run_id, token.owner_id, token.epoch, expires_at
            )
        except BaseException:
            self.connection.rollback()
            raise

    def handoff_run_controller(
        self,
        token: ControllerToken,
        new_owner_id: str,
        *,
        lease_seconds: int = 60,
        now: str | None = None,
    ) -> ControllerToken:
        if not new_owner_id.strip() or new_owner_id == token.owner_id:
            raise ValueError("new controller owner must be different and non-empty")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be at least 1")
        as_of = self._aware_datetime(now)
        expires_at = (as_of + timedelta(seconds=lease_seconds)).isoformat()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self._ensure_controller_tx(token, as_of.isoformat())
            next_epoch = token.epoch + 1
            self.connection.execute(
                """
                UPDATE run_controller_leases
                SET owner_id = ?, epoch = ?, acquired_at = ?,
                    heartbeat_at = ?, expires_at = ? WHERE run_id = ?
                """,
                (
                    new_owner_id,
                    next_epoch,
                    as_of.isoformat(),
                    as_of.isoformat(),
                    expires_at,
                    token.run_id,
                ),
            )
            self._append_event(
                token.run_id,
                None,
                None,
                "controller.handed_off",
                str(token.epoch),
                str(next_epoch),
                {"from_owner": token.owner_id, "to_owner": new_owner_id},
            )
            self.connection.commit()
            return ControllerToken(
                token.run_id, new_owner_id, next_epoch, expires_at
            )
        except BaseException:
            self.connection.rollback()
            raise

    def release_run_controller(
        self, token: ControllerToken, *, now: str | None = None
    ) -> None:
        as_of = self._aware_datetime(now)
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self._ensure_controller_tx(token, as_of.isoformat())
            self.connection.execute(
                """
                UPDATE run_controller_leases SET heartbeat_at = ?, expires_at = ?
                WHERE run_id = ? AND owner_id = ? AND epoch = ?
                """,
                (
                    as_of.isoformat(),
                    as_of.isoformat(),
                    token.run_id,
                    token.owner_id,
                    token.epoch,
                ),
            )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def register_agent(
        self,
        *,
        agent_id: str,
        team_id: str,
        pool_id: str | None,
        backend: str,
        model: str | None,
        capabilities_actual: tuple[str, ...] = (),
        workspace_id: str | None = None,
    ) -> None:
        now = utc_now()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO agent_instances(
                    agent_id, team_id, pool_id, backend, model, status,
                    capabilities_actual_json, workspace_id, authority_epoch,
                    origin, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'RECONCILER', ?, ?)
                """,
                (
                    agent_id,
                    team_id,
                    pool_id,
                    backend,
                    model,
                    AgentState.STARTING.value,
                    json.dumps(capabilities_actual),
                    workspace_id,
                    now,
                    now,
                ),
            )
            run_id = self._latest_run_for_team(team_id)
            if run_id is not None:
                self._append_event(
                    run_id,
                    None,
                    None,
                    "agent.created",
                    None,
                    AgentState.STARTING.value,
                    {"agent_id": agent_id, "pool_id": pool_id, "backend": backend},
                )

    def agent_snapshot(self, agent_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM agent_instances WHERE agent_id = ?", (agent_id,)
        ).fetchone()
        if row is None:
            raise KeyError(agent_id)
        result = dict(row)
        result["capabilities_actual"] = json.loads(
            result.pop("capabilities_actual_json")
        )
        return result

    def transition_agent(
        self, agent_id: str, target: AgentState, *, reason: str
    ) -> None:
        with self.connection:
            row = self.connection.execute(
                "SELECT team_id, status FROM agent_instances WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            if row is None:
                raise KeyError(agent_id)
            current = AgentState(row["status"])
            ensure_agent_transition(current, target)
            now = utc_now()
            self.connection.execute(
                """
                UPDATE agent_instances
                SET status = ?, updated_at = ?,
                    stopped_at = CASE WHEN ? = 'STOPPED' THEN ? ELSE stopped_at END
                WHERE agent_id = ?
                """,
                (target.value, now, target.value, now, agent_id),
            )
            run_id = self._event_run_for_agent(agent_id, row["team_id"])
            if run_id is not None:
                self._append_event(
                    run_id,
                    None,
                    None,
                    "agent.transitioned",
                    current.value,
                    target.value,
                    {"agent_id": agent_id, "reason": reason},
                )

    def bind_role(
        self,
        *,
        binding_id: str,
        run_id: str,
        agent_id: str,
        role_id: str,
        role_version: int,
        task_id: str | None = None,
        primary: bool = True,
    ) -> None:
        with self.connection:
            self._ensure_agent_and_run_share_team(agent_id, run_id)
            if task_id is not None:
                task = self.connection.execute(
                    "SELECT run_id FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
                if task is None:
                    raise KeyError(task_id)
                if task["run_id"] != run_id:
                    raise ValueError("role binding task belongs to a different run")
            now = utc_now()
            self.connection.execute(
                """
                INSERT INTO role_bindings(
                    binding_id, run_id, agent_id, task_id, role_id, role_version,
                    binding_kind, status, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    binding_id,
                    run_id,
                    agent_id,
                    task_id,
                    role_id,
                    role_version,
                    "PRIMARY" if primary else "SECONDARY",
                    RoleBindingState.ACTIVE.value,
                    now,
                ),
            )
            self._append_event(
                run_id,
                task_id,
                None,
                "role.bound",
                None,
                RoleBindingState.ACTIVE.value,
                {
                    "binding_id": binding_id,
                    "agent_id": agent_id,
                    "role_id": role_id,
                    "role_version": role_version,
                    "primary": primary,
                },
            )

    def end_role_binding(self, binding_id: str, *, reason: str) -> None:
        with self.connection:
            row = self.connection.execute(
                "SELECT * FROM role_bindings WHERE binding_id = ?", (binding_id,)
            ).fetchone()
            if row is None:
                raise KeyError(binding_id)
            if RoleBindingState(row["status"]) != RoleBindingState.ACTIVE:
                raise ValueError("role binding is not active")
            now = utc_now()
            self.connection.execute(
                """
                UPDATE role_bindings
                SET status = ?, ended_at = ?, end_reason = ?
                WHERE binding_id = ?
                """,
                (RoleBindingState.ENDED.value, now, reason, binding_id),
            )
            self._append_event(
                row["run_id"],
                row["task_id"],
                None,
                "role.ended",
                RoleBindingState.ACTIVE.value,
                RoleBindingState.ENDED.value,
                {"binding_id": binding_id, "agent_id": row["agent_id"], "reason": reason},
            )

    def role_binding_snapshot(self, binding_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM role_bindings WHERE binding_id = ?", (binding_id,)
        ).fetchone()
        if row is None:
            raise KeyError(binding_id)
        return dict(row)

    def create_backend_session(
        self,
        *,
        session_ref_id: str,
        run_id: str,
        agent_id: str,
        backend: str,
        provider_session_id: str | None,
        cwd: str,
        replacement_for_session_ref_id: str | None = None,
    ) -> None:
        with self.connection:
            self._ensure_agent_and_run_share_team(agent_id, run_id)
            if replacement_for_session_ref_id is not None:
                replaced = self.connection.execute(
                    """
                    SELECT run_id, agent_id FROM backend_sessions
                    WHERE session_ref_id = ?
                    """,
                    (replacement_for_session_ref_id,),
                ).fetchone()
                if replaced is None:
                    raise KeyError(replacement_for_session_ref_id)
                if replaced["run_id"] != run_id or replaced["agent_id"] != agent_id:
                    raise ValueError("replacement session must keep the same owner and run")
            now = utc_now()
            self.connection.execute(
                """
                INSERT INTO backend_sessions(
                    session_ref_id, run_id, agent_id, backend,
                    provider_session_id, state, cwd,
                    replacement_for_session_ref_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_ref_id,
                    run_id,
                    agent_id,
                    backend,
                    provider_session_id,
                    SessionState.OPENING.value,
                    cwd,
                    replacement_for_session_ref_id,
                    now,
                    now,
                ),
            )
            self._append_event(
                run_id,
                None,
                None,
                "session.created",
                None,
                SessionState.OPENING.value,
                {"session_ref_id": session_ref_id, "agent_id": agent_id},
            )

    def transition_backend_session(
        self, session_ref_id: str, target: SessionState, *, reason: str
    ) -> None:
        with self.connection:
            row = self.connection.execute(
                "SELECT * FROM backend_sessions WHERE session_ref_id = ?",
                (session_ref_id,),
            ).fetchone()
            if row is None:
                raise KeyError(session_ref_id)
            current = SessionState(row["state"])
            ensure_session_transition(current, target)
            now = utc_now()
            self.connection.execute(
                """
                UPDATE backend_sessions
                SET state = ?, updated_at = ?,
                    closed_at = CASE WHEN ? = 'CLOSED' THEN ? ELSE closed_at END,
                    close_reason = CASE WHEN ? = 'CLOSED' THEN ? ELSE close_reason END
                WHERE session_ref_id = ?
                """,
                (
                    target.value,
                    now,
                    target.value,
                    now,
                    target.value,
                    reason,
                    session_ref_id,
                ),
            )
            self._append_event(
                row["run_id"],
                None,
                None,
                "session.transitioned",
                current.value,
                target.value,
                {
                    "session_ref_id": session_ref_id,
                    "agent_id": row["agent_id"],
                    "reason": reason,
                },
            )

    def backend_session_snapshot(self, session_ref_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM backend_sessions WHERE session_ref_id = ?",
            (session_ref_id,),
        ).fetchone()
        if row is None:
            raise KeyError(session_ref_id)
        return dict(row)

    def create_backend_call(self, request: AdapterCallRequest) -> None:
        digest = _call_request_digest(request)
        with self.connection:
            existing = self.connection.execute(
                "SELECT request_digest FROM backend_calls WHERE call_id = ?",
                (request.call_id,),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != digest:
                    raise ValueError("call_id already belongs to another request")
                return
            ownership = self.connection.execute(
                """
                SELECT a.generation, a.agent_id, t.run_id,
                       s.agent_id AS session_agent_id, s.run_id AS session_run_id,
                       s.backend AS session_backend
                FROM attempts a
                JOIN tasks t ON t.task_id = a.task_id
                JOIN backend_sessions s ON s.session_ref_id = ?
                WHERE a.attempt_id = ? AND a.task_id = ?
                """,
                (request.session.session_id, request.attempt_id, request.task_id),
            ).fetchone()
            if ownership is None:
                raise ValueError("call references an unknown attempt or session")
            if (
                ownership["generation"] != request.generation
                or ownership["agent_id"] != request.agent_id
                or ownership["run_id"] != request.run_id
                or ownership["session_agent_id"] != request.agent_id
                or ownership["session_run_id"] != request.run_id
                or ownership["session_backend"] != request.session.backend
            ):
                raise ValueError("call ownership or generation does not match")
            self.connection.execute(
                """
                INSERT INTO backend_calls(
                    call_id, run_id, task_id, attempt_id, generation, agent_id,
                    session_ref_id, backend, state, request_digest, requested_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'starting', ?, ?)
                """,
                (
                    request.call_id,
                    request.run_id,
                    request.task_id,
                    request.attempt_id,
                    request.generation,
                    request.agent_id,
                    request.session.session_id,
                    request.session.backend,
                    digest,
                    utc_now(),
                ),
            )
            self._append_event(
                request.run_id,
                request.task_id,
                request.attempt_id,
                "backend.call.starting",
                None,
                "starting",
                {
                    "call_id": request.call_id,
                    "generation": request.generation,
                    "request_digest": digest,
                },
            )

    def backend_call_snapshot(self, call_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM backend_calls WHERE call_id = ?", (call_id,)
        ).fetchone()
        if row is None:
            raise KeyError(call_id)
        return dict(row)

    def authorize_backend_call(
        self,
        call_id: str,
        *,
        controller: ControllerToken | None = None,
        now: str | None = None,
    ) -> None:
        """Fence a persisted call immediately before invoking its backend."""
        now_value = self._aware_datetime(now).isoformat()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT * FROM backend_calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            if row is None:
                raise KeyError(call_id)
            self._ensure_backend_call_controller_tx(row, controller, now_value)
            if row["state"] != "starting" or row["disposition"] is not None:
                raise ValueError("backend call is not awaiting start")
            self.connection.execute(
                """
                UPDATE backend_calls SET backend_may_still_run = 1
                WHERE call_id = ? AND state = 'starting' AND disposition IS NULL
                """,
                (call_id,),
            )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def heartbeat_backend_call_lease(
        self,
        call_id: str,
        *,
        controller: ControllerToken,
        lease_seconds: int,
        now: str | None = None,
    ) -> str:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be at least 1")
        as_of = self._aware_datetime(now)
        now_value = as_of.isoformat()
        expires_at = (as_of + timedelta(seconds=lease_seconds)).isoformat()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                """
                SELECT c.*, a.state AS attempt_state,
                       t.state AS task_state, l.state AS lease_state,
                       l.expires_at AS lease_expires_at
                FROM backend_calls c
                JOIN attempts a ON a.attempt_id = c.attempt_id
                JOIN tasks t ON t.task_id = c.task_id
                JOIN assignment_leases l ON l.attempt_id = c.attempt_id
                WHERE c.call_id = ?
                """,
                (call_id,),
            ).fetchone()
            if row is None:
                raise KeyError(call_id)
            self._ensure_backend_call_controller_tx(row, controller, now_value)
            if (
                row["state"] not in {"starting", "running"}
                or row["attempt_state"]
                not in {AttemptState.ASSIGNED.value, AttemptState.RUNNING.value}
                or row["task_state"] != TaskState.ACTIVE.value
                or row["lease_state"] != "ACTIVE"
                or row["lease_expires_at"] <= now_value
            ):
                raise FencedAttemptError("backend call assignment lease is stale")
            updated = self.connection.execute(
                """
                UPDATE assignment_leases
                SET heartbeat_at = ?, expires_at = ?
                WHERE attempt_id = ? AND generation = ? AND state = 'ACTIVE'
                  AND expires_at > ?
                """,
                (
                    now_value,
                    expires_at,
                    row["attempt_id"],
                    row["generation"],
                    now_value,
                ),
            ).rowcount
            if updated != 1:
                raise FencedAttemptError("backend call assignment lease is stale")
            self.connection.commit()
            return expires_at
        except BaseException:
            self.connection.rollback()
            raise

    def claim_ready_dispatch(
        self,
        run_id: str,
        *,
        controller: ControllerToken,
        authority: AuthorityToken,
        lease_seconds: int = 60,
        now: str | None = None,
    ) -> AdapterCallRequest | None:
        if controller.run_id != run_id:
            raise FencedControllerError("controller token belongs to another Run")
        if authority.run_id != run_id:
            raise FencedAuthorityError("authority token belongs to another Run")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be at least 1")
        as_of = self._aware_datetime(now)
        now_value = as_of.isoformat()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self._ensure_controller_tx(controller, now_value)
            self._ensure_authority_tx(authority, now_value)
            budget = self.budget_status(run_id)
            if budget.get("exceeded"):
                # 硬预算达到上限：零新增调用，停止派发。
                self.connection.commit()
                return None
            run_snapshot_row = self.connection.execute(
                "SELECT team_id, team_snapshot_json FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            workspace_enabled = (
                self.workspace_manager is not None
                and run_snapshot_row is not None
                and bool(run_snapshot_row["team_snapshot_json"])
            )
            candidates = self.connection.execute(
                """
                SELECT t.task_id, t.run_id, t.access_mode, t.write_scope_json,
                       t.max_attempts, t.parent_task_id, t.dispatch_source,
                       t.required_delivery, t.task_kind,
                       t.acceptance_criteria_json, t.input_refs_json,
                       t.output_contract_json, t.budget_json,
                       d.required_role_id, d.required_model,
                       d.required_provider_id,
                       d.instruction_text,
                       d.cwd, d.timeout_seconds,
                       a.agent_id, a.backend, a.model, a.provider_id, b.binding_id,
                       s.session_ref_id, s.provider_session_id
                FROM tasks t
                JOIN task_dispatch_specs d ON d.task_id = t.task_id
                JOIN runs r ON r.run_id = t.run_id
                JOIN role_bindings b
                  ON b.run_id = t.run_id
                 AND b.role_id = d.required_role_id
                 AND b.binding_kind = 'PRIMARY'
                 AND b.status = 'ACTIVE'
                JOIN agent_instances a
                  ON a.agent_id = b.agent_id
                 AND a.status = 'IDLE'
                 AND a.current_task_id IS NULL
                JOIN backend_sessions s
                  ON s.agent_id = a.agent_id
                 AND s.run_id = t.run_id
                 AND s.state = 'IDLE'
                WHERE t.run_id = ? AND t.state = 'READY'
                  AND d.available_at <= ?
                  AND r.control_state = 'RUNNING'
                  AND d.paused = 0
                  AND (d.required_backend IS NULL OR d.required_backend = ''
                       OR d.required_backend = a.backend)
                  AND (d.required_model IS NULL OR d.required_model = ''
                       OR d.required_model = a.model)
                  AND (d.required_provider_id IS NULL OR d.required_provider_id = ''
                       OR d.required_provider_id = a.provider_id)
                ORDER BY d.priority DESC, t.created_at, t.task_id,
                         a.created_at, a.agent_id
                """,
                (run_id, now_value),
            ).fetchall()
            row = None
            for candidate in candidates:
                if self.workspace_policy is not None:
                    # P0-03：派发边界——cwd 不在受管 worktree / 项目根则保守跳过
                    try:
                        self.workspace_policy.validate_cwd(
                            candidate["access_mode"], candidate["cwd"]
                        )
                    except ValueError:
                        continue
                if candidate["access_mode"] == "write":
                    scope = tuple(json.loads(candidate["write_scope_json"]))
                    if scope and self._write_scope_conflicts(
                        run_id,
                        candidate["task_id"],
                        scope,
                        now_value,
                    ):
                        continue
                row = candidate
                break
            if row is None:
                self.connection.commit()
                return None
            attempt_number = self.connection.execute(
                "SELECT COUNT(*) + 1 FROM attempts WHERE task_id = ?",
                (row["task_id"],),
            ).fetchone()[0]
            generation = self.connection.execute(
                "SELECT COALESCE(MAX(generation), 0) + 1 FROM attempts WHERE task_id = ?",
                (row["task_id"],),
            ).fetchone()[0]
            attempt_id = f"attempt-{uuid.uuid4().hex[:16]}"
            call_id = f"call-{attempt_id}"
            lease_id = f"lease-{uuid.uuid4().hex}"
            expires_at = (as_of + timedelta(seconds=lease_seconds)).isoformat()
            workspace_path = str(row["cwd"])
            scratch_path: str | None = None
            base_commit: str | None = None
            if workspace_enabled:
                manifest = self.workspace_manager.ensure_run(run_id)
                manifest_path = Path(str(manifest.root)) / "manifest.json"
                manifest_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO run_workspaces(
                        run_id, manifest_path, manifest_digest, lifecycle,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        str(manifest_path),
                        manifest_digest,
                        manifest.lifecycle,
                        now_value,
                        now_value,
                    ),
                )
                allocation = self.workspace_manager.allocate_attempt(
                    run_id,
                    str(row["task_id"]),
                    attempt_id,
                    access_mode=str(row["access_mode"]),
                    agent_id=str(row["agent_id"]),
                )
                scratch_path = str(allocation["scratch_path"])
                base_commit = str(allocation["base_commit"])
                workspace_path = str(allocation["workspace_path"])
            from orchestrator.core.handoff import HandoffPackage, render_handoff_prompt

            plan_revision_row = self.connection.execute(
                """
                SELECT COALESCE(MAX(revision), 1) AS revision
                FROM supervisor_plans
                WHERE run_id = ? AND supervisor_task_id = ?
                """,
                (run_id, row["parent_task_id"] or row["task_id"]),
            ).fetchone()
            dependency_refs = [
                dict(item)
                for item in self.connection.execute(
                    """
                    SELECT d.depends_on_task_id AS task_id,
                           r.result_id, r.candidate_commit, r.artifact_refs_json
                    FROM task_dependencies d
                    JOIN task_results r
                      ON r.task_id = d.depends_on_task_id AND r.status = 'verified'
                    WHERE d.run_id = ? AND d.task_id = ?
                    ORDER BY r.created_at, r.result_id
                    """,
                    (run_id, row["task_id"]),
                ).fetchall()
            ]
            input_refs = json.loads(row["input_refs_json"] or "[]")
            if dependency_refs:
                input_refs = list(input_refs) + [
                    {"kind": "verified_task_result", **ref} for ref in dependency_refs
                ]
            handoff = HandoffPackage(
                contract_version=1,
                project_id=str(run_snapshot_row["team_id"]),
                run_id=run_id,
                task_id=str(row["task_id"]),
                attempt_id=attempt_id,
                plan_revision=int(plan_revision_row["revision"]),
                goal=str(row["instruction_text"]),
                accepted_facts=tuple(
                    {"kind": "verified_dependency", "value": ref}
                    for ref in dependency_refs
                ),
                constraints=(
                    f"access_mode={row['access_mode']}",
                    f"cwd={workspace_path}",
                    f"write_scope={json.dumps(json.loads(row['write_scope_json']), ensure_ascii=False)}",
                ),
                input_refs=tuple(input_refs),
                base_commit=base_commit,
                write_scope=tuple(json.loads(row["write_scope_json"])),
                acceptance_criteria=tuple(
                    json.loads(row["acceptance_criteria_json"] or "[]")
                ),
                budget=json.loads(row["budget_json"])
                if row["budget_json"]
                else None,
                expected_output=json.loads(row["output_contract_json"])
                if row["output_contract_json"]
                else None,
                agent_id=str(row["agent_id"]),
                role_id=str(row["required_role_id"]),
                backend=str(row["backend"]),
                model=row["model"],
                provider_id=row["required_provider_id"] or row["provider_id"],
                session_ref_id=str(row["session_ref_id"]),
            )
            handoff_json = json.dumps(
                handoff.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            request = AdapterCallRequest(
                call_id=call_id,
                run_id=run_id,
                task_id=row["task_id"],
                attempt_id=attempt_id,
                generation=generation,
                agent_id=row["agent_id"],
                session=SessionRef(
                    session_id=row["session_ref_id"],
                    backend=row["backend"],
                    provider_session_id=row["provider_session_id"],
                    model=row["model"],
                    provider_id=row["required_provider_id"] or row["provider_id"],
                ),
                prompt=render_handoff_prompt(row["instruction_text"], handoff),
                policy=AccessPolicy(
                    access_mode=row["access_mode"],
                    cwd=workspace_path,
                    timeout_seconds=float(row["timeout_seconds"]),
                    write_scope=tuple(json.loads(row["write_scope_json"])),
                ),
            )
            self.connection.execute(
                """
                INSERT INTO attempts(
                    attempt_id, task_id, agent_id, state, attempt_number,
                    generation, workspace_path, scratch_path, base_commit,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'ASSIGNED', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    row["task_id"],
                    row["agent_id"],
                    attempt_number,
                    generation,
                    workspace_path,
                    scratch_path,
                    base_commit,
                    now_value,
                    now_value,
                ),
            )
            self.connection.execute(
                """
                UPDATE attempts SET handoff_digest = ? WHERE attempt_id = ?
                """,
                (handoff.digest, attempt_id),
            )
            self.connection.execute(
                """
                INSERT INTO handoffs(
                    handoff_id, run_id, task_id, attempt_id, plan_revision,
                    package_json, package_digest, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"handoff-{uuid.uuid4().hex[:16]}", run_id, row["task_id"],
                    attempt_id, int(plan_revision_row["revision"]), handoff_json,
                    handoff.digest, now_value,
                ),
            )
            self.connection.execute(
                """
                INSERT INTO assignment_leases(
                    lease_id, attempt_id, task_id, owner_agent_id, generation,
                    state, acquired_at, heartbeat_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?)
                """,
                (
                    lease_id,
                    attempt_id,
                    row["task_id"],
                    row["agent_id"],
                    generation,
                    now_value,
                    now_value,
                    expires_at,
                ),
            )
            self.connection.execute(
                """
                INSERT INTO backend_calls(
                    call_id, run_id, task_id, attempt_id, generation, agent_id,
                    session_ref_id, backend, state, request_digest,
                    requested_at, scheduler_owner, controller_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'starting', ?, ?, ?, ?)
                """,
                (
                    call_id,
                    run_id,
                    row["task_id"],
                    attempt_id,
                    generation,
                    row["agent_id"],
                    row["session_ref_id"],
                    row["backend"],
                    _call_request_digest(request),
                    now_value,
                    controller.owner_id,
                    controller.epoch,
                ),
            )
            self.connection.execute(
                """
                INSERT INTO budget_reservations(
                    reservation_id, run_id, task_id, attempt_id, generation,
                    call_id, status, reserved_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'RESERVED', ?)
                """,
                (
                    f"budget-{uuid.uuid4().hex[:16]}",
                    run_id,
                    row["task_id"],
                    attempt_id,
                    generation,
                    call_id,
                    now_value,
                ),
            )
            task_updated = self.connection.execute(
                """
                UPDATE tasks SET state = 'ACTIVE', version = version + 1,
                    updated_at = ? WHERE task_id = ? AND state = 'READY'
                """,
                (now_value, row["task_id"]),
            ).rowcount
            agent_updated = self.connection.execute(
                """
                UPDATE agent_instances SET status = 'BUSY', current_task_id = ?,
                    updated_at = ? WHERE agent_id = ? AND status = 'IDLE'
                    AND current_task_id IS NULL
                """,
                (row["task_id"], now_value, row["agent_id"]),
            ).rowcount
            session_updated = self.connection.execute(
                """
                UPDATE backend_sessions SET state = 'ACTIVE', updated_at = ?
                WHERE session_ref_id = ? AND state = 'IDLE'
                """,
                (now_value, row["session_ref_id"]),
            ).rowcount
            if (task_updated, agent_updated, session_updated) != (1, 1, 1):
                raise FencedAttemptError("dispatch claim lost a state race")
            self._append_event(
                run_id,
                row["task_id"],
                attempt_id,
                "dispatch.claimed",
                TaskState.READY.value,
                TaskState.ACTIVE.value,
                {
                    "agent_id": row["agent_id"],
                    "session_ref_id": row["session_ref_id"],
                    "call_id": call_id,
                    "generation": generation,
                    "scheduler_owner": controller.owner_id,
                    "controller_epoch": controller.epoch,
                    "workspace_path": workspace_path,
                    "scratch_path": scratch_path,
                    "base_commit": base_commit,
                    "handoff_digest": handoff.digest,
                    "dispatch_source": row["dispatch_source"],
                },
            )
            self.connection.commit()
            return request
        except BaseException:
            self.connection.rollback()
            raise

    def pause_run(
        self, run_id: str, controller: ControllerToken, *, reason: str
    ) -> None:
        self._set_run_control_state(run_id, "PAUSED", controller, reason, "run.paused")

    def resume_run(
        self, run_id: str, controller: ControllerToken, *, reason: str
    ) -> None:
        self._set_run_control_state(
            run_id, "RUNNING", controller, reason, "run.resumed"
        )

    def _set_run_control_state(
        self,
        run_id: str,
        target: str,
        controller: ControllerToken,
        reason: str,
        event_kind: str,
    ) -> None:
        if not reason.strip():
            raise ValueError("reason must not be empty")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self._ensure_controller_tx(
                controller, self._aware_datetime(None).isoformat()
            )
            if controller.run_id != run_id:
                raise FencedControllerError("controller token belongs to another Run")
            row = self.connection.execute(
                "SELECT control_state FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            current = str(row["control_state"])
            if current == target:
                self.connection.commit()
                return
            now = utc_now()
            self.connection.execute(
                "UPDATE runs SET control_state = ? WHERE run_id = ?",
                (target, run_id),
            )
            self._append_event(
                run_id, None, None, event_kind, current, target, {"reason": reason}
            )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def pause_task(
        self, task_id: str, controller: ControllerToken, *, reason: str
    ) -> None:
        self._set_task_paused(task_id, 1, controller, reason, "task.paused")

    def resume_task(
        self, task_id: str, controller: ControllerToken, *, reason: str
    ) -> None:
        self._set_task_paused(task_id, 0, controller, reason, "task.resumed")

    def _set_task_paused(
        self,
        task_id: str,
        paused: int,
        controller: ControllerToken,
        reason: str,
        event_kind: str,
    ) -> None:
        if not reason.strip():
            raise ValueError("reason must not be empty")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self._ensure_controller_tx(
                controller, self._aware_datetime(None).isoformat()
            )
            row = self.connection.execute(
                """
                SELECT t.run_id, d.paused
                FROM tasks t
                JOIN task_dispatch_specs d ON d.task_id = t.task_id
                WHERE t.task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row["run_id"] != controller.run_id:
                raise FencedControllerError("task belongs to another Run")
            current = int(row["paused"])
            if current == paused:
                self.connection.commit()
                return
            now = utc_now()
            self.connection.execute(
                "UPDATE task_dispatch_specs SET paused = ? WHERE task_id = ?",
                (paused, task_id),
            )
            self._append_event(
                row["run_id"],
                task_id,
                None,
                event_kind,
                str(current),
                str(paused),
                {"reason": reason},
            )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def request_cancel_task(
        self, task_id: str, controller: ControllerToken, *, reason: str
    ) -> str:
        if not reason.strip():
            raise ValueError("reason must not be empty")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            now = self._aware_datetime(None).isoformat()
            self._ensure_controller_tx(controller, now)
            row = self.connection.execute(
                "SELECT run_id, state FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row["run_id"] != controller.run_id:
                raise FencedControllerError("task belongs to another Run")
            current = TaskState(row["state"])
            run_id = str(row["run_id"])
            if current in {
                TaskState.COMPLETED,
                TaskState.FAILED,
                TaskState.CANCELLED,
            }:
                self.connection.commit()
                return "noop"
            if current == TaskState.CANCEL_REQUESTED:
                self._transition_task_tx(
                    task_id, TaskState.CANCELLED, reason, now
                )
                self._cancel_descendants_tx(run_id, task_id, reason, now)
                self._reconcile_task_graph_tx(run_id, now)
                self.connection.commit()
                return "cancelled"
            if current in {TaskState.PENDING, TaskState.READY}:
                self._transition_task_tx(
                    task_id, TaskState.CANCEL_REQUESTED, reason, now
                )
                self._transition_task_tx(task_id, TaskState.CANCELLED, reason, now)
                self._cancel_descendants_tx(run_id, task_id, reason, now)
                self._reconcile_task_graph_tx(run_id, now)
                self.connection.commit()
                return "cancelled"
            if current == TaskState.REVIEW:
                self._transition_task_tx(
                    task_id, TaskState.CANCEL_REQUESTED, reason, now
                )
                self._transition_task_tx(task_id, TaskState.CANCELLED, reason, now)
                self._cancel_descendants_tx(run_id, task_id, reason, now)
                self._reconcile_task_graph_tx(run_id, now)
                self.connection.commit()
                return "cancelled"
            if current == TaskState.ACTIVE:
                disposition = self._request_cancel_active_task_tx(
                    run_id, task_id, reason, now
                )
                self._cancel_descendants_tx(run_id, task_id, reason, now)
                self.connection.commit()
                return disposition
            self.connection.commit()
            return "noop"
        except BaseException:
            self.connection.rollback()
            raise

    def _cancel_descendants_tx(
        self, run_id: str, parent_task_id: str, reason: str, now: str
    ) -> None:
        rows = self.connection.execute(
            """
            WITH RECURSIVE descendants(task_id) AS (
                SELECT task_id FROM tasks
                WHERE run_id = ? AND parent_task_id = ?
                UNION ALL
                SELECT t.task_id FROM tasks t
                JOIN descendants d ON d.task_id = t.parent_task_id
                WHERE t.run_id = ?
            )
            SELECT task_id FROM descendants ORDER BY task_id
            """,
            (run_id, parent_task_id, run_id),
        ).fetchall()
        for row in rows:
            self._cancel_task_in_tx(run_id, str(row["task_id"]), reason, now)

    def request_cancel_run(
        self, run_id: str, controller: ControllerToken, *, reason: str
    ) -> dict[str, Any]:
        if not reason.strip():
            raise ValueError("reason must not be empty")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            now = self._aware_datetime(None).isoformat()
            self._ensure_controller_tx(controller, now)
            if controller.run_id != run_id:
                raise FencedControllerError("controller token belongs to another Run")
            rows = self.connection.execute(
                """
                SELECT task_id FROM tasks
                WHERE run_id = ? AND state NOT IN (
                    'COMPLETED', 'FAILED', 'CANCELLED'
                )
                ORDER BY created_at, task_id
                """,
                (run_id,),
            ).fetchall()
            dispositions: dict[str, str] = {}
            for row in rows:
                task_id = str(row["task_id"])
                dispositions[task_id] = self._cancel_task_in_tx(
                    run_id, task_id, reason, now
                )
            self._reconcile_task_graph_tx(run_id, now)
            self.connection.commit()
            return {"dispositions": dispositions}
        except BaseException:
            self.connection.rollback()
            raise

    def _cancel_task_in_tx(
        self, run_id: str, task_id: str, reason: str, now: str
    ) -> str:
        row = self.connection.execute(
            "SELECT state FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        current = TaskState(row["state"])
        if current in {
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.CANCELLED,
        }:
            return "noop"
        if current == TaskState.CANCEL_REQUESTED:
            self._transition_task_tx(task_id, TaskState.CANCELLED, reason, now)
            return "cancelled"
        if current in {TaskState.PENDING, TaskState.READY, TaskState.REVIEW}:
            self._transition_task_tx(task_id, TaskState.CANCEL_REQUESTED, reason, now)
            self._transition_task_tx(task_id, TaskState.CANCELLED, reason, now)
            return "cancelled"
        if current == TaskState.ACTIVE:
            return self._request_cancel_active_task_tx(
                run_id, task_id, reason, now
            )
        return "noop"

    def _request_cancel_active_task_tx(
        self, run_id: str, task_id: str, reason: str, now: str
    ) -> str:
        self._transition_task_tx(task_id, TaskState.CANCEL_REQUESTED, reason, now)
        attempts = self.connection.execute(
            """
            SELECT attempt_id, generation FROM attempts
            WHERE task_id = ? AND state IN ('ASSIGNED', 'RUNNING')
            ORDER BY attempt_number DESC
            """,
            (task_id,),
        ).fetchall()
        any_running_call = False
        for attempt in attempts:
            call = self.connection.execute(
                """
                SELECT call_id, state, agent_id, session_ref_id FROM backend_calls
                WHERE attempt_id = ? AND state IN (
                    'starting', 'running', 'cancel_requested'
                )
                """,
                (attempt["attempt_id"],),
            ).fetchone()
            if call is None:
                continue
            terminate_message = MessageEnvelope(
                message_id=f"msg-terminate-{call['call_id']}",
                team_id=str(
                    self.connection.execute(
                        "SELECT team_id FROM runs WHERE run_id = ?", (run_id,)
                    ).fetchone()["team_id"]
                ),
                run_id=run_id,
                task_id=task_id,
                sender_agent_id="hub",
                recipients=(Recipient("agent", str(call["agent_id"])),),
                kind="terminate",
                message_type="terminate",
                source="hub",
                payload={"call_id": str(call["call_id"]), "reason": reason[:500]},
                correlation_id=str(call["call_id"]),
                idempotency_key=f"terminate:{call['call_id']}",
                target_agent_id=str(call["agent_id"]),
                attempt_id=str(attempt["attempt_id"]),
                expires_at=(self._aware_datetime(now) + timedelta(seconds=30)).isoformat(),
            )
            self._append_message_tx(terminate_message)
            if call["state"] == "starting":
                self.connection.execute(
                    """
                    UPDATE backend_calls
                    SET state = 'cancelled', finished_at = ?,
                        disposition = 'cancelled', settled_at = ?
                    WHERE call_id = ? AND state = 'starting'
                    """,
                    (now, now, call["call_id"]),
                )
                self._transition_attempt_tx(
                    attempt["attempt_id"],
                    AttemptState.CANCEL_REQUESTED,
                    reason,
                    now,
                )
                self._transition_attempt_tx(
                    attempt["attempt_id"], AttemptState.CANCELLED, reason, now
                )
                self._release_attempt_resources_tx(
                    attempt["attempt_id"],
                    int(attempt["generation"]),
                    task_id,
                    reason,
                    now,
                )
            elif call["state"] == "running":
                self.connection.execute(
                    """
                    UPDATE backend_calls
                    SET state = 'cancel_requested', cancel_requested_at = ?
                    WHERE call_id = ? AND state = 'running'
                    """,
                    (now, call["call_id"]),
                )
                self._transition_attempt_tx(
                    attempt["attempt_id"],
                    AttemptState.CANCEL_REQUESTED,
                    reason,
                    now,
                )
                any_running_call = True
        if not any_running_call:
            self._transition_task_tx(task_id, TaskState.CANCELLED, reason, now)
            self._reconcile_task_graph_tx(run_id, now)
            return "cancelled"
        return "cancel_requested"

    def _release_attempt_resources_tx(
        self,
        attempt_id: str,
        generation: int,
        task_id: str,
        reason: str,
        now: str,
    ) -> None:
        call = self.connection.execute(
            """
            SELECT agent_id, session_ref_id FROM backend_calls
            WHERE attempt_id = ?
            ORDER BY requested_at DESC LIMIT 1
            """,
            (attempt_id,),
        ).fetchone()
        self.connection.execute(
            """
            UPDATE assignment_leases
            SET state = 'RELEASED', closed_at = ?, close_reason = ?
            WHERE attempt_id = ? AND generation = ? AND state = 'ACTIVE'
            """,
            (now, reason, attempt_id, generation),
        )
        if call is not None:
            self.connection.execute(
                """
                UPDATE agent_instances
                SET status = CASE WHEN status = 'BUSY' THEN 'IDLE' ELSE status END,
                    current_task_id = NULL, updated_at = ?
                WHERE agent_id = ? AND current_task_id = ?
                  AND status IN ('BUSY', 'DRAINING')
                """,
                (now, call["agent_id"], task_id),
            )
            self.connection.execute(
                """
                UPDATE backend_sessions SET state = 'IDLE', updated_at = ?
                WHERE session_ref_id = ? AND state = 'ACTIVE'
                """,
                (now, call["session_ref_id"]),
            )

    def _transition_task_tx(
        self, task_id: str, target: TaskState, reason: str, now: str
    ) -> None:
        row = self.connection.execute(
            "SELECT run_id, state FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise KeyError(task_id)
        current = TaskState(row["state"])
        ensure_task_transition(current, target)
        self.connection.execute(
            """
            UPDATE tasks
            SET state = ?, terminal_reason = ?, version = version + 1, updated_at = ?
            WHERE task_id = ?
            """,
            (
                target.value,
                reason if target == TaskState.CANCELLED else None,
                now,
                task_id,
            ),
        )
        self._append_event(
            row["run_id"],
            task_id,
            None,
            "task.transitioned",
            current.value,
            target.value,
            {"reason": reason},
        )

    def _transition_attempt_tx(
        self, attempt_id: str, target: AttemptState, reason: str, now: str
    ) -> None:
        row = self.connection.execute(
            """
            SELECT a.state, a.task_id, t.run_id
            FROM attempts a JOIN tasks t ON t.task_id = a.task_id
            WHERE a.attempt_id = ?
            """,
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise KeyError(attempt_id)
        current = AttemptState(row["state"])
        ensure_attempt_transition(current, target)
        terminal = target in {
            AttemptState.CANCELLED,
            AttemptState.FAILED,
            AttemptState.STALE,
        }
        self.connection.execute(
            """
            UPDATE attempts
            SET state = ?, finished_at = ?, terminal_reason = ?, updated_at = ?
            WHERE attempt_id = ?
            """,
            (
                target.value,
                now if terminal else None,
                reason if terminal else None,
                now,
                attempt_id,
            ),
        )
        self._append_event(
            row["run_id"],
            row["task_id"],
            attempt_id,
            "attempt.transitioned",
            current.value,
            target.value,
            {"reason": reason},
        )

    def backend_call_cancel_requested(self, call_id: str) -> bool:
        row = self.connection.execute(
            "SELECT state FROM backend_calls WHERE call_id = ?", (call_id,)
        ).fetchone()
        return row is not None and row["state"] == "cancel_requested"

    def _ack_terminate_messages_tx(
        self,
        run_id: str,
        task_id: str,
        attempt_id: str,
        call_id: str,
        now: str,
    ) -> None:
        """Mark the cancellation signal acknowledged when the call reaches a terminal state."""
        rows = self.connection.execute(
            """
            SELECT d.delivery_id
            FROM message_deliveries d
            JOIN messages m ON m.message_id = d.message_id
            WHERE d.run_id=? AND d.task_id=?
              AND d.status IN ('QUEUED', 'DELIVERED')
              AND json_extract(m.envelope_json, '$.attempt_id') = ?
              AND json_extract(m.envelope_json, '$.message_type') = 'terminate'
              AND json_extract(m.envelope_json, '$.payload.call_id') = ?
            """,
            (run_id, task_id, attempt_id, call_id),
        ).fetchall()
        for row in rows:
            self.connection.execute(
                """
                UPDATE message_deliveries
                SET status='ACKNOWLEDGED', acknowledged_at=?,
                    consumer_id='backend', lease_until=NULL
                WHERE delivery_id=? AND status IN ('QUEUED', 'DELIVERED')
                """,
                (now, row["delivery_id"]),
            )
            self._append_event(
                run_id, task_id, attempt_id, "message.acknowledged",
                "DELIVERED", "ACKNOWLEDGED",
                {"delivery_id": row["delivery_id"], "call_id": call_id, "source": "backend"},
            )

    def confirm_backend_stopped(
        self,
        call_id: str,
        *,
        controller: ControllerToken,
        reason: str = "backend-stop-confirmed",
    ) -> str:
        """Release a resource only after a previously unconfirmed cancel is confirmed."""
        if not reason.strip():
            raise ValueError("reason must not be empty")
        now = utc_now()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self._ensure_controller_tx(controller, now)
            row = self.connection.execute(
                """
                SELECT c.*, a.generation AS attempt_generation
                FROM backend_calls c JOIN attempts a ON a.attempt_id=c.attempt_id
                WHERE c.call_id=?
                """,
                (call_id,),
            ).fetchone()
            if row is None:
                raise KeyError(call_id)
            if row["run_id"] != controller.run_id:
                raise FencedControllerError("backend call belongs to another Run")
            if not bool(row["backend_may_still_run"]):
                self.connection.commit()
                return "already_confirmed"
            self.connection.execute(
                "UPDATE backend_calls SET backend_may_still_run=0 WHERE call_id=?",
                (call_id,),
            )
            self._ack_terminate_messages_tx(
                row["run_id"], row["task_id"], row["attempt_id"], call_id, now
            )
            self._release_attempt_resources_tx(
                str(row["attempt_id"]), int(row["attempt_generation"]),
                str(row["task_id"]), reason, now,
            )
            self._append_event(
                row["run_id"], row["task_id"], row["attempt_id"],
                "backend.stop.confirmed", row["state"], row["state"],
                {"call_id": call_id, "reason": reason},
            )
            self.connection.commit()
            return "confirmed"
        except BaseException:
            self.connection.rollback()
            raise

    def _write_scope_conflicts(
        self,
        run_id: str,
        task_id: str,
        scope: tuple[str, ...],
        now_value: str,
    ) -> bool:
        if not scope:
            return False
        rows = self.connection.execute(
            """
            SELECT write_scope_json FROM tasks
            WHERE run_id = ? AND state = 'ACTIVE'
              AND access_mode = 'write' AND task_id != ?
            """,
            (run_id, task_id),
        ).fetchall()
        if self.workspace_policy is not None:
            # P0-03：规范化 + 大小写不敏感 + 目录包含的冲突检测
            for row in rows:
                other = tuple(json.loads(row["write_scope_json"]))
                if self.workspace_policy.scopes_conflict(scope, other):
                    return True
            return False
        target = set(scope)
        for row in rows:
            other = set(json.loads(row["write_scope_json"]))
            if other & target:
                return True
        return False

    def enqueue_merge(
        self,
        run_id: str,
        task_id: str,
        attempt_id: str,
        result_commit: str,
        base_commit: str,
        controller: ControllerToken,
        *,
        authority: AuthorityToken,
        reason: str,
    ) -> str:
        if not result_commit.strip() or not base_commit.strip():
            raise ValueError("result_commit and base_commit must not be empty")
        if not reason.strip():
            raise ValueError("reason must not be empty")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            now = self._aware_datetime(None).isoformat()
            self._ensure_controller_tx(controller, now)
            self._ensure_authority_tx(authority, now)
            if controller.run_id != run_id:
                raise FencedControllerError("controller token belongs to another Run")
            if authority.run_id != run_id:
                raise FencedAuthorityError("authority token belongs to another Run")
            # P0-02：入队前原子验证 Task 处于 REVIEW、Attempt 存在且非终态
            task_row = self.connection.execute(
                "SELECT state FROM tasks WHERE task_id = ? AND run_id = ?",
                (task_id, run_id),
            ).fetchone()
            if task_row is None:
                raise ValueError(f"task {task_id} does not exist")
            if task_row["state"] != TaskState.REVIEW.value:
                raise ValueError(
                    f"task {task_id} is {task_row['state']}, must be REVIEW to enqueue merge"
                )
            attempt_row = self.connection.execute(
                "SELECT state FROM attempts WHERE attempt_id = ? AND task_id = ?",
                (attempt_id, task_id),
            ).fetchone()
            if attempt_row is None:
                raise ValueError(f"attempt {attempt_id} does not exist for task {task_id}")
            if attempt_row["state"] in {
                AttemptState.CANCELLED.value,
                AttemptState.FAILED.value,
                AttemptState.STALE.value,
            }:
                raise ValueError(
                    f"attempt {attempt_id} is terminal ({attempt_row['state']}), cannot merge"
                )
            run_snapshot = self.connection.execute(
                "SELECT team_snapshot_json FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run_snapshot is not None and run_snapshot["team_snapshot_json"]:
                gate = self.verification_gate(run_id, task_id, attempt_id, result_commit)
                if not gate["ready"]:
                    raise ValueError(
                        "candidate cannot enter merge queue before PASS verification "
                        "and an evidence-bound review decision"
                    )
            merge_id = f"merge-{uuid.uuid4().hex[:16]}"
            idempotency_key = f"{run_id}:{task_id}:{attempt_id}"
            self.connection.execute(
                """
                INSERT INTO merge_queue(
                    merge_id, run_id, task_id, attempt_id, result_commit,
                    base_commit, status, idempotency_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)
                """,
                (
                    merge_id,
                    run_id,
                    task_id,
                    attempt_id,
                    result_commit,
                    base_commit,
                    idempotency_key,
                    now,
                ),
            )
            self._append_event(
                run_id,
                task_id,
                attempt_id,
                "merge.enqueued",
                "PENDING",
                "PENDING",
                {
                    "reason": reason,
                    "merge_id": merge_id,
                    "result_commit": result_commit,
                    "base_commit": base_commit,
                },
            )
            self.connection.commit()
            return merge_id
        except BaseException:
            self.connection.rollback()
            raise

    def claim_merge_queue(
        self, run_id: str, controller: ControllerToken, *, authority: AuthorityToken
    ) -> dict[str, Any] | None:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            now = self._aware_datetime(None).isoformat()
            self._ensure_controller_tx(controller, now)
            self._ensure_authority_tx(authority, now)
            if controller.run_id != run_id:
                raise FencedControllerError("controller token belongs to another Run")
            if authority.run_id != run_id:
                raise FencedAuthorityError("authority token belongs to another Run")
            row = self.connection.execute(
                """
                SELECT merge_id, task_id, attempt_id, result_commit, base_commit
                FROM merge_queue
                WHERE run_id = ? AND status = 'PENDING'
                ORDER BY created_at, task_id, merge_id
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                self.connection.commit()
                return None
            self.connection.execute(
                """
                UPDATE merge_queue
                SET status = 'APPLYING', claim_owner = ?, claimed_at = ?
                WHERE merge_id = ? AND status = 'PENDING'
                """,
                (controller.owner_id, now, row["merge_id"]),
            )
            self._append_event(
                run_id,
                row["task_id"],
                row["attempt_id"],
                "merge.claimed",
                "PENDING",
                "APPLYING",
                {"merge_id": row["merge_id"]},
            )
            self.connection.commit()
            return {
                "merge_id": row["merge_id"],
                "task_id": row["task_id"],
                "attempt_id": row["attempt_id"],
                "result_commit": row["result_commit"],
                "base_commit": row["base_commit"],
            }
        except BaseException:
            self.connection.rollback()
            raise

    def finish_merge(
        self,
        merge_id: str,
        status: str,
        controller: ControllerToken,
        *,
        authority: AuthorityToken,
        result_commit: str | None = None,
        is_integrated: Any | None = None,
        outbox_payload: dict[str, Any] | None = None,
        issue_kind: str | None = None,
        issue_detail: dict[str, Any] | None = None,
    ) -> None:
        if status not in {"applied", "conflict", "failed"}:
            raise ValueError(f"unsupported merge status: {status}")
        if status == "applied":
            if result_commit is None:
                raise ValueError("applied merge requires result_commit")
            if is_integrated is None:
                raise ValueError(
                    "applied merge requires is_integrated(result_commit) proof "
                    "that the commit landed on the integration branch"
                )
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            now = self._aware_datetime(None).isoformat()
            self._ensure_controller_tx(controller, now)
            self._ensure_authority_tx(authority, now)
            row = self.connection.execute(
                """
                SELECT m.run_id, m.task_id, m.attempt_id, m.status, m.claim_owner,
                       m.result_commit,
                       t.state AS task_state
                FROM merge_queue m JOIN tasks t ON t.task_id = m.task_id
                WHERE m.merge_id = ?
                """,
                (merge_id,),
            ).fetchone()
            if row is None:
                raise KeyError(merge_id)
            if row["run_id"] != controller.run_id:
                raise FencedControllerError("controller token belongs to another Run")
            if row["claim_owner"] != controller.owner_id:
                raise FencedControllerError(
                    "merge was claimed by a different controller owner"
                )
            # P0-02：严格 APPLYING 前置——重复调用（已 APPLIED/CONFLICT/FAILED）零副作用
            if row["status"] != "APPLYING":
                raise RuntimeError(
                    f"merge {merge_id} is {row['status']}, not APPLYING; refusing to re-settle"
                )
            run_id = str(row["run_id"])
            task_id = str(row["task_id"])
            attempt_id = str(row["attempt_id"])
            if status == "applied":
                # P0-02：Git 对账证明——result commit 必须已落集成分支
                stored_commit = str(row["result_commit"] or "")
                proof_commit = result_commit or stored_commit
                try:
                    landed = bool(is_integrated(proof_commit))
                except Exception:
                    landed = False
                if not landed:
                    raise ValueError(
                        f"result commit {proof_commit} is not integrated on the branch; "
                        "refusing to mark merge APPLIED"
                    )
            target = {
                "applied": "APPLIED",
                "conflict": "CONFLICT",
                "failed": "FAILED",
            }[status]
            updated = self.connection.execute(
                """
                UPDATE merge_queue
                SET status = ?, settled_at = ?
                WHERE merge_id = ? AND claim_owner = ? AND status = 'APPLYING'
                """,
                (target, now, merge_id, controller.owner_id),
            ).rowcount
            if updated == 0:
                raise RuntimeError(
                    f"merge {merge_id} was not in APPLYING state; refusing to re-settle"
                )
            if status == "applied":
                if outbox_payload is not None:
                    # P0-02：merge 业务状态与 Outbox intent 同一事务
                    outbox_id = f"outbox-{uuid.uuid4().hex[:16]}"
                    self.connection.execute(
                        """
                        INSERT INTO outbox(
                            outbox_id, run_id, aggregate_type, aggregate_id,
                            event_type, payload_json, status, available_at, created_at
                        ) VALUES (?, ?, 'merge', ?, 'merge.applied', ?, 'PENDING', ?, ?)
                        """,
                        (
                            outbox_id,
                            run_id,
                            merge_id,
                            json.dumps(outbox_payload),
                            now,
                            now,
                        ),
                    )
                if row["task_state"] == TaskState.REVIEW.value:
                    self._transition_task_tx(
                        task_id, TaskState.INTEGRATION, "merge-applied", now
                    )
                    self._transition_task_tx(
                        task_id, TaskState.COMPLETED, "merge-applied", now
                    )
                elif row["task_state"] == TaskState.INTEGRATION.value:
                    self._transition_task_tx(
                        task_id, TaskState.COMPLETED, "merge-applied", now
                    )
            elif issue_kind:
                issue_id = f"issue-{uuid.uuid4().hex[:16]}"
                self.connection.execute(
                    """
                    INSERT INTO integration_issues(
                        issue_id, run_id, task_id, attempt_id, kind, detail_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        issue_id,
                        run_id,
                        task_id,
                        attempt_id,
                        issue_kind,
                        json.dumps(issue_detail or {}),
                        now,
                    ),
                )
                self._append_event(
                    run_id,
                    task_id,
                    attempt_id,
                    "integration.issue",
                    row["status"],
                    target,
                    {"merge_id": merge_id, "kind": issue_kind, "detail": issue_detail or {}},
                )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def reconcile_merge_queue(
        self, run_id: str, controller: ControllerToken
    ) -> dict[str, Any]:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            now = self._aware_datetime(None).isoformat()
            self._ensure_controller_tx(controller, now)
            if controller.run_id != run_id:
                raise FencedControllerError("controller token belongs to another Run")
            # 重启后遗留的 APPLYING 且非当前 owner 的记录：没有可对账的
            # commit trailer 时保守重置回 PENDING（可安全重试一次，重复由
            # idempotency_key 与 commit 对账兜底）；已 APPLIED 绝不重放。
            rows = self.connection.execute(
                """
                SELECT merge_id, task_id, attempt_id FROM merge_queue
                WHERE run_id = ? AND status = 'APPLYING'
                  AND claim_owner != ?
                """,
                (run_id, controller.owner_id),
            ).fetchall()
            requeued: list[str] = []
            for row in rows:
                self.connection.execute(
                    """
                    UPDATE merge_queue
                    SET status = 'PENDING', claim_owner = NULL, claimed_at = NULL
                    WHERE merge_id = ? AND status = 'APPLYING' AND claim_owner != ?
                    """,
                    (row["merge_id"], controller.owner_id),
                )
                requeued.append(str(row["merge_id"]))
            self.connection.commit()
            return {"requeued": requeued, "reapplied": []}
        except BaseException:
            self.connection.rollback()
            raise

    def reconcile_merge_with_git(
        self,
        run_id: str,
        controller: ControllerToken,
        is_applied: Any,
        *,
        authority: AuthorityToken | None = None,
    ) -> dict[str, Any]:
        """Reconcile APPLYING merges against the real integration branch.

        ``is_applied(result_commit)`` decides whether the result commit already
        landed on the integration branch (e.g. via commit trailer / ancestor
        check). Already-applied merges are marked APPLIED and never re-applied;
        the rest are safely requeued as PENDING for one retry.

        ``authority`` is optional for backward compatibility during migration;
        when provided it is atomically fenced before any merge state changes.
        """
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            now = self._aware_datetime(None).isoformat()
            self._ensure_controller_tx(controller, now)
            if controller.run_id != run_id:
                raise FencedControllerError("controller token belongs to another Run")
            if authority is not None:
                self._ensure_authority_tx(authority, now)
            rows = self.connection.execute(
                """
                SELECT merge_id, task_id, attempt_id, result_commit
                FROM merge_queue
                WHERE run_id = ? AND status = 'APPLYING'
                """,
                (run_id,),
            ).fetchall()
            marked_applied: list[str] = []
            requeued: list[str] = []
            for row in rows:
                merge_id = str(row["merge_id"])
                result_commit = str(row["result_commit"])
                try:
                    landed = bool(is_applied(result_commit))
                except Exception:
                    landed = False
                if landed:
                    self.connection.execute(
                        """
                        UPDATE merge_queue
                        SET status = 'APPLIED', settled_at = ?
                        WHERE merge_id = ? AND status = 'APPLYING'
                        """,
                        (now, merge_id),
                    )
                    marked_applied.append(merge_id)
                else:
                    self.connection.execute(
                        """
                        UPDATE merge_queue
                        SET status = 'PENDING', claim_owner = NULL, claimed_at = NULL
                        WHERE merge_id = ? AND status = 'APPLYING'
                        """,
                        (merge_id,),
                    )
                    requeued.append(merge_id)
            self.connection.commit()
            return {
                "marked_applied": marked_applied,
                "requeued": requeued,
                "reapplied": [],
            }
        except BaseException:
            self.connection.rollback()
            raise

    # ---- Business Authority (Supervisor) ----

    def acquire_authority(
        self,
        run_id: str,
        owner_agent_id: str,
        role_id: str,
        *,
        lease_seconds: int = 300,
        scope: str = "supervisor",
    ) -> AuthorityToken:
        if not owner_agent_id.strip() or not role_id.strip():
            raise ValueError("owner_agent_id and role_id must not be empty")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            as_of = self._aware_datetime(None)
            now = as_of.isoformat()
            expires_at = (as_of + timedelta(seconds=lease_seconds)).isoformat()
            row = self.connection.execute(
                "SELECT * FROM authority_leases WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                epoch = 1
                self.connection.execute(
                    """
                    INSERT INTO authority_leases(
                        lease_id, run_id, owner_agent_id, role_id, scope, epoch,
                        state, acquired_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)
                    """,
                    (
                        f"authority-{uuid.uuid4().hex[:16]}",
                        run_id,
                        owner_agent_id,
                        role_id,
                        scope,
                        epoch,
                        now,
                        expires_at,
                    ),
                )
            else:
                # 每 Run 一行：已有 ACTIVE 且未过期时不得被普通 acquire 无条件覆盖
                # （强制接管必须走 force_takeover_authority 的人工审批流程）；
                # 只有旧租约已过期才允许在此接管恢复。
                if row["state"] == "ACTIVE" and str(row["expires_at"]) > now:
                    raise FencedAuthorityError(
                        "active authority is held by another supervisor; "
                        "use force_takeover_authority with human approval"
                    )
                # 接管：更新同一行并递增 epoch，旧 epoch 立即失效。
                epoch = int(row["epoch"]) + 1
                self.connection.execute(
                    """
                    UPDATE authority_leases
                    SET owner_agent_id = ?, role_id = ?, scope = ?, epoch = ?,
                        state = 'ACTIVE', acquired_at = ?, expires_at = ?,
                        handoff_state = NULL, handoff_target_agent_id = NULL,
                        ended_at = NULL, end_reason = NULL
                    WHERE run_id = ?
                    """,
                    (
                        owner_agent_id,
                        role_id,
                        scope,
                        epoch,
                        now,
                        expires_at,
                        run_id,
                    ),
                )
            self._append_event(
                run_id,
                None,
                None,
                "authority.acquired",
                None,
                "ACTIVE",
                {
                    "owner_agent_id": owner_agent_id,
                    "role_id": role_id,
                    "epoch": epoch,
                },
            )
            self.connection.commit()
            return AuthorityToken(
                run_id=run_id,
                owner_agent_id=owner_agent_id,
                role_id=role_id,
                epoch=epoch,
                expires_at=expires_at,
            )
        except BaseException:
            self.connection.rollback()
            raise

    def renew_authority(
        self, token: AuthorityToken, *, lease_seconds: int = 300
    ) -> AuthorityToken:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            as_of = self._aware_datetime(None)
            now = as_of.isoformat()
            self._ensure_authority_tx(token, now)
            expires_at = (as_of + timedelta(seconds=lease_seconds)).isoformat()
            self.connection.execute(
                """
                UPDATE authority_leases SET expires_at = ?
                WHERE run_id = ? AND owner_agent_id = ? AND epoch = ?
                  AND state = 'ACTIVE'
                """,
                (
                    expires_at,
                    token.run_id,
                    token.owner_agent_id,
                    token.epoch,
                ),
            )
            self.connection.commit()
            return AuthorityToken(
                run_id=token.run_id,
                owner_agent_id=token.owner_agent_id,
                role_id=token.role_id,
                epoch=token.epoch,
                expires_at=expires_at,
            )
        except BaseException:
            self.connection.rollback()
            raise

    def active_authority(self, run_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT lease_id, run_id, owner_agent_id, role_id, scope, epoch,
                   state, handoff_state, handoff_target_agent_id, acquired_at,
                   expires_at
            FROM authority_leases
            WHERE run_id = ? AND state = 'ACTIVE'
            """,
            (run_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def _ensure_authority_tx(
        self, token: AuthorityToken, now: str
    ) -> None:
        row = self.connection.execute(
            """
            SELECT owner_agent_id, epoch, expires_at FROM authority_leases
            WHERE run_id = ? AND state = 'ACTIVE'
            """,
            (token.run_id,),
        ).fetchone()
        if row is None:
            raise FencedAuthorityError("no active authority lease")
        if (
            row["owner_agent_id"] != token.owner_agent_id
            or int(row["epoch"]) != token.epoch
        ):
            raise FencedAuthorityError("authority token is stale (old epoch)")
        if row["expires_at"] <= now:
            raise FencedAuthorityError("authority lease expired")

    def request_authority_handoff(
        self,
        run_id: str,
        token: AuthorityToken,
        target_agent_id: str,
        *,
        reason: str,
    ) -> dict[str, Any]:
        if not target_agent_id.strip() or not reason.strip():
            raise ValueError("target_agent_id and reason must not be empty")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            now = self._aware_datetime(None).isoformat()
            self._ensure_authority_tx(token, now)
            if token.run_id != run_id:
                raise FencedAuthorityError("authority token belongs to another Run")
            active_merge = self.connection.execute(
                """
                SELECT COUNT(*) FROM merge_queue
                WHERE run_id = ? AND status = 'APPLYING'
                """,
                (run_id,),
            ).fetchone()[0]
            if active_merge > 0:
                raise RuntimeError(
                    "handoff rejected: an integration merge is in progress"
                )
            request_id = f"handoff-{uuid.uuid4().hex[:16]}"
            self.connection.execute(
                """
                UPDATE authority_leases
                SET handoff_state = 'REQUESTED', handoff_target_agent_id = ?,
                    requested_at = ?
                WHERE run_id = ? AND state = 'ACTIVE'
                """,
                (target_agent_id, now, run_id),
            )
            self._append_event(
                run_id,
                None,
                None,
                "authority.handoff_requested",
                "ACTIVE",
                "REQUESTED",
                {"request_id": request_id, "target_agent_id": target_agent_id, "reason": reason},
            )
            self.connection.commit()
            return {
                "request_id": request_id,
                "run_id": run_id,
                "target_agent_id": target_agent_id,
            }
        except BaseException:
            self.connection.rollback()
            raise

    def accept_authority_handoff(
        self, run_id: str, request_id: str, target_agent_id: str
    ) -> None:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            now = self._aware_datetime(None).isoformat()
            updated = self.connection.execute(
                """
                UPDATE authority_leases
                SET handoff_state = 'ACCEPTED'
                WHERE run_id = ? AND state = 'ACTIVE'
                  AND handoff_state = 'REQUESTED'
                  AND handoff_target_agent_id = ?
                """,
                (run_id, target_agent_id),
            ).rowcount
            if updated == 0:
                raise RuntimeError("no matching pending handoff request")
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def commit_authority_handoff(
        self,
        run_id: str,
        request_id: str,
        target_agent_id: str,
        *,
        lease_seconds: int = 300,
    ) -> AuthorityToken:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            as_of = self._aware_datetime(None)
            now = as_of.isoformat()
            expires_at = (as_of + timedelta(seconds=lease_seconds)).isoformat()
            row = self.connection.execute(
                """
                SELECT * FROM authority_leases
                WHERE run_id = ? AND state = 'ACTIVE'
                  AND handoff_state = 'ACCEPTED'
                  AND handoff_target_agent_id = ?
                """,
                (run_id, target_agent_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("no accepted handoff to commit")
            epoch = int(row["epoch"]) + 1
            role_id = str(row["role_id"])
            scope = str(row["scope"])
            self.connection.execute(
                """
                UPDATE authority_leases
                SET owner_agent_id = ?, role_id = ?, scope = ?, epoch = ?,
                    state = 'ACTIVE', acquired_at = ?, expires_at = ?,
                    handoff_state = NULL, handoff_target_agent_id = NULL,
                    ended_at = NULL, end_reason = NULL
                WHERE run_id = ?
                """,
                (
                    target_agent_id,
                    role_id,
                    scope,
                    epoch,
                    now,
                    expires_at,
                    run_id,
                ),
            )
            self._append_event(
                run_id,
                None,
                None,
                "authority.handoff_committed",
                str(row["epoch"]),
                str(epoch),
                {"owner_agent_id": target_agent_id, "role_id": role_id},
            )
            self.connection.commit()
            return AuthorityToken(
                run_id=run_id,
                owner_agent_id=target_agent_id,
                role_id=role_id,
                epoch=epoch,
                expires_at=expires_at,
            )
        except BaseException:
            self.connection.rollback()
            raise

    def force_takeover_authority(
        self,
        run_id: str,
        owner_agent_id: str,
        role_id: str,
        *,
        requested_by: str,
        approval_request_id: str,
        lease_seconds: int = 300,
        scope: str = "supervisor",
    ) -> AuthorityToken:
        """人工审批的强制接管。

        仅在提供已批准（APPROVED）、作用域匹配、单次使用的审批单时允许
        无条件接管 ACTIVE authority；审批单被原子消费为 USED，并记录
        authority.takeover_forced 审计事件。旧 epoch 立即失效。
        """
        if not owner_agent_id.strip() or not role_id.strip():
            raise ValueError("owner_agent_id and role_id must not be empty")
        if not requested_by.strip() or not approval_request_id.strip():
            raise ValueError("requested_by and approval_request_id must not be empty")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            as_of = self._aware_datetime(None)
            now = as_of.isoformat()
            expires_at = (as_of + timedelta(seconds=lease_seconds)).isoformat()
            approval = self.connection.execute(
                """
                SELECT run_id, scope, single_use, status, expires_at
                FROM approval_requests WHERE request_id = ?
                """,
                (approval_request_id,),
            ).fetchone()
            if approval is None:
                raise RuntimeError("approval request does not exist")
            if str(approval["run_id"]) != run_id:
                raise RuntimeError("approval request belongs to another Run")
            if approval["status"] != "APPROVED":
                raise RuntimeError("approval request is not APPROVED")
            if approval["scope"] != scope:
                raise RuntimeError(
                    f"approval scope {approval['scope']!r} does not match takeover scope {scope!r}"
                )
            if approval["expires_at"] is not None and str(
                approval["expires_at"]
            ) <= now:
                raise RuntimeError("approval request has expired")
            # 原子消费审批：APPROVED → USED（单次使用语义）
            self.connection.execute(
                """
                UPDATE approval_requests SET status = 'USED'
                WHERE request_id = ? AND status = 'APPROVED'
                """,
                (approval_request_id,),
            )
            row = self.connection.execute(
                "SELECT epoch FROM authority_leases WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                epoch = 1
                self.connection.execute(
                    """
                    INSERT INTO authority_leases(
                        lease_id, run_id, owner_agent_id, role_id, scope, epoch,
                        state, acquired_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)
                    """,
                    (
                        f"authority-{uuid.uuid4().hex[:16]}",
                        run_id,
                        owner_agent_id,
                        role_id,
                        scope,
                        epoch,
                        now,
                        expires_at,
                    ),
                )
            else:
                epoch = int(row["epoch"]) + 1
                self.connection.execute(
                    """
                    UPDATE authority_leases
                    SET owner_agent_id = ?, role_id = ?, scope = ?, epoch = ?,
                        state = 'ACTIVE', acquired_at = ?, expires_at = ?,
                        handoff_state = NULL, handoff_target_agent_id = NULL,
                        ended_at = NULL, end_reason = 'force-takeover'
                    WHERE run_id = ?
                    """,
                    (
                        owner_agent_id,
                        role_id,
                        scope,
                        epoch,
                        now,
                        expires_at,
                        run_id,
                    ),
                )
            self._append_event(
                run_id,
                None,
                None,
                "authority.takeover_forced",
                None,
                "ACTIVE",
                {
                    "owner_agent_id": owner_agent_id,
                    "role_id": role_id,
                    "epoch": epoch,
                    "requested_by": requested_by,
                    "approval_request_id": approval_request_id,
                },
            )
            self.connection.commit()
            return AuthorityToken(
                run_id=run_id,
                owner_agent_id=owner_agent_id,
                role_id=role_id,
                epoch=epoch,
                expires_at=expires_at,
            )
        except BaseException:
            self.connection.rollback()
            raise

    # ---- Human Approval ----

    def create_approval_request(
        self,
        run_id: str,
        *,
        task_id: str,
        action_summary: str,
        params: dict[str, Any],
        requested_by: str,
        scope: str,
        single_use: bool = False,
        expires_at: str | None = None,
    ) -> str:
        if not action_summary.strip() or not requested_by.strip():
            raise ValueError("action_summary and requested_by must not be empty")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            now = self._aware_datetime(None).isoformat()
            params_hash = hashlib.sha256(
                json.dumps(params, sort_keys=True).encode("utf-8")
            ).hexdigest()
            request_id = f"approval-{uuid.uuid4().hex[:16]}"
            self.connection.execute(
                """
                INSERT INTO approval_requests(
                    request_id, run_id, task_id, action_summary, params_hash,
                    requested_by, scope, single_use, status, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)
                """,
                (
                    request_id,
                    run_id,
                    task_id,
                    action_summary,
                    params_hash,
                    requested_by,
                    scope,
                    1 if single_use else 0,
                    expires_at,
                    now,
                ),
            )
            self._append_event(
                run_id,
                task_id,
                None,
                "approval.requested",
                "PENDING",
                "PENDING",
                {"request_id": request_id, "action_summary": action_summary, "scope": scope},
            )
            self.connection.commit()
            return request_id
        except BaseException:
            self.connection.rollback()
            raise

    def decide_approval(
        self,
        request_id: str,
        decision: str,
        *,
        decided_by: str,
        comment: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> None:
        if decision not in {"APPROVED", "REJECTED"}:
            raise ValueError(f"unsupported decision: {decision}")
        if not decided_by.strip():
            raise ValueError("decided_by must not be empty")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            now = self._aware_datetime(None).isoformat()
            row = self.connection.execute(
                "SELECT run_id, status, expires_at, params_hash "
                "FROM approval_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                raise KeyError(request_id)
            if row["status"] != "PENDING":
                raise RuntimeError("approval request was already decided")
            # B2（P1-03）：过期审批不可再决定（原子检查）
            if row["expires_at"] is not None and str(row["expires_at"]) <= now:
                raise ValueError("approval request has expired")
            # B2（P1-03）：params 原子消费——决定必须对应申请时的参数
            if params is not None:
                actual_hash = hashlib.sha256(
                    json.dumps(params, sort_keys=True).encode("utf-8")
                ).hexdigest()
                if actual_hash != row["params_hash"]:
                    raise ValueError("params do not match the approval request")
            self.connection.execute(
                """
                UPDATE approval_requests
                SET status = ?, decided_at = ?, decided_by = ?
                WHERE request_id = ? AND status = 'PENDING'
                """,
                (decision, now, decided_by, request_id),
            )
            decision_id = f"decision-{uuid.uuid4().hex[:16]}"
            self.connection.execute(
                """
                INSERT INTO approval_decisions(
                    decision_id, request_id, run_id, decision, decided_by,
                    comment, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (decision_id, request_id, row["run_id"], decision, decided_by, comment, now),
            )
            self._append_event(
                row["run_id"],
                None,
                None,
                "approval.decided",
                "PENDING",
                decision,
                {"request_id": request_id, "decided_by": decided_by, "comment": comment},
            )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def list_approval_requests(
        self, run_id: str, *, status: str | None = None
    ) -> list[dict[str, Any]]:
        if status is not None:
            rows = self.connection.execute(
                """
                SELECT request_id, run_id, task_id, action_summary, scope,
                       status, single_use, expires_at, created_at
                FROM approval_requests
                WHERE run_id = ? AND status = ?
                ORDER BY created_at, request_id
                """,
                (run_id, status),
            ).fetchall()
        else:
            rows = self.connection.execute(
                """
                SELECT request_id, run_id, task_id, action_summary, scope,
                       status, single_use, expires_at, created_at
                FROM approval_requests
                WHERE run_id = ?
                ORDER BY created_at, request_id
                """,
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def reassign_task(
        self,
        run_id: str,
        task_id: str,
        controller: ControllerToken,
        authority: AuthorityToken,
        *,
        reason: str = "manual-reassign",
    ) -> None:
        """B2（P1-03）：把处于 REVIEW / FAILED 的任务重新进入 READY 重新派发。

        原子消费：需要有效 controller + authority；清理未终态 attempt；
        重置任务 READY 并记录 task.reassigned 事件。
        """
        if not reason.strip():
            raise ValueError("reason must not be empty")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            now = self._aware_datetime(None).isoformat()
            self._ensure_controller_tx(controller, now)
            self._ensure_authority_tx(authority, now)
            row = self.connection.execute(
                "SELECT run_id, state FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            if str(row["run_id"]) != run_id:
                raise FencedControllerError("task belongs to another Run")
            state = TaskState(row["state"])
            if state not in {TaskState.REVIEW, TaskState.FAILED}:
                raise ValueError(f"task in state {state.value} cannot be reassigned")
            rework_count = self.connection.execute(
                """
                SELECT COUNT(*) FROM events
                WHERE run_id = ? AND task_id = ? AND kind = 'task.reassigned'
                """,
                (run_id, task_id),
            ).fetchone()[0]
            if int(rework_count) >= 2:
                raise ValueError("task rework limit reached (maximum two rounds)")
            # 清理进行中的 attempt（标记 CANCELLED，晚到结果被隔离）
            self.connection.execute(
                """
                UPDATE attempts SET state = 'CANCELLED'
                WHERE task_id = ? AND state NOT IN
                ('SUBMITTED', 'CANCELLED', 'FAILED', 'STALE')
                """,
                (task_id,),
            )
            self.connection.execute(
                "UPDATE tasks SET state = 'READY' WHERE task_id = ?", (task_id,)
            )
            self._append_event(
                run_id,
                task_id,
                None,
                "task.reassigned",
                state.value,
                "READY",
                {"reason": reason},
            )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def record_review_decision(
        self,
        run_id: str,
        task_id: str,
        *,
        attempt_id: str | None,
        layer: str,
        decision: str,
        decided_by: str,
        detail: dict[str, Any],
        authority: AuthorityToken,
    ) -> str:
        if layer not in {"deterministic", "model", "human"}:
            raise ValueError(f"unsupported review layer: {layer}")
        if decision not in {"PASS", "REWORK", "BLOCKED", "APPROVED", "REJECTED"}:
            raise ValueError(f"unsupported review decision: {decision}")
        if not decided_by.strip():
            raise ValueError("decided_by must not be empty")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            now = self._aware_datetime(None).isoformat()
            self._ensure_authority_tx(authority, now)
            if authority.run_id != run_id:
                raise FencedAuthorityError("authority token belongs to another Run")
            snapshot = self.connection.execute(
                "SELECT team_snapshot_json FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if snapshot is not None and snapshot["team_snapshot_json"] and decision in {
                "PASS", "APPROVED"
            }:
                if not attempt_id:
                    raise ValueError("approved review requires an attempt_id")
                candidate_commit = str(detail.get("candidate_commit") or "").strip()
                evidence_refs = detail.get("evidence_refs")
                if not candidate_commit or not isinstance(evidence_refs, (list, tuple)) or not evidence_refs:
                    raise ValueError(
                        "approved review requires candidate_commit and evidence_refs"
                    )
                placeholders = ",".join("?" for _ in evidence_refs)
                evidence_rows = self.connection.execute(
                    f"""
                    SELECT evidence_id FROM verification_evidence
                    WHERE run_id = ? AND task_id = ? AND attempt_id = ?
                      AND candidate_commit = ? AND status = 'PASS'
                      AND evidence_id IN ({placeholders})
                    """,
                    (run_id, task_id, attempt_id, candidate_commit, *map(str, evidence_refs)),
                ).fetchall()
                if len(evidence_rows) != len(set(map(str, evidence_refs))):
                    raise ValueError(
                        "approved review references missing or mismatched verification evidence"
                    )
            decision_id = f"review-{uuid.uuid4().hex[:16]}"
            self.connection.execute(
                """
                INSERT INTO review_decisions(
                    decision_id, run_id, task_id, attempt_id, layer, decision,
                    detail_json, decided_by, authority_epoch, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    run_id,
                    task_id,
                    attempt_id,
                    layer,
                    decision,
                    json.dumps(detail),
                    decided_by,
                    authority.epoch,
                    now,
                ),
            )
            self._append_event(
                run_id,
                task_id,
                attempt_id,
                "review.decision",
                "PENDING",
                decision,
                {"layer": layer, "decided_by": decided_by, "authority_epoch": authority.epoch},
            )
            self.connection.commit()
            return decision_id
        except BaseException:
            self.connection.rollback()
            raise

    # ---- Hard Budget ----

    def record_budget(
        self,
        run_id: str,
        *,
        max_run_seconds: int | None = None,
        max_calls: int | None = None,
        max_turns: int | None = None,
        max_tasks: int | None = None,
        max_cost_decimal: str | None = None,
    ) -> None:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            now = self._aware_datetime(None).isoformat()
            row = self.connection.execute(
                "SELECT budget_id FROM budgets WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                self.connection.execute(
                    """
                    INSERT INTO budgets(
                        budget_id, run_id, max_run_seconds, max_calls, max_turns,
                        max_tasks, max_cost_decimal, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"budget-{uuid.uuid4().hex[:16]}",
                        run_id,
                        max_run_seconds,
                        max_calls,
                        max_turns,
                        max_tasks,
                        max_cost_decimal,
                        now,
                        now,
                    ),
                )
            else:
                assignments = []
                parameters: list[Any] = []
                for column, value in (
                    ("max_run_seconds", max_run_seconds),
                    ("max_calls", max_calls),
                    ("max_turns", max_turns),
                    ("max_tasks", max_tasks),
                    ("max_cost_decimal", max_cost_decimal),
                ):
                    if value is not None:
                        assignments.append(f"{column} = ?")
                        parameters.append(value)
                parameters.append(now)
                parameters.append(run_id)
                self.connection.execute(
                    f"UPDATE budgets SET {', '.join(assignments)}, updated_at = ? "
                    "WHERE run_id = ?",
                    parameters,
                )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def budget_status(self, run_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            """
            SELECT b.max_run_seconds, b.max_calls, b.max_turns, b.max_tasks,
                   b.max_cost_decimal, r.created_at AS run_created_at
            FROM budgets b JOIN runs r ON r.run_id = b.run_id
            WHERE b.run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            return {
                "run_id": run_id,
                "exceeded": False,
                "calls": None,
                "tasks": None,
                "run_seconds": None,
                "turns": 0,
                "tokens": 0,
                "cost": 0.0,
            }
        calls = self.connection.execute(
            "SELECT COUNT(*) FROM backend_calls WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
        tasks = self.connection.execute(
            "SELECT COUNT(*) FROM tasks WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
        # B1（P1-02）：从权威 usage_json 聚合 turn / token / 金额用量
        usage_row = self.connection.execute(
            """
            SELECT
              COALESCE(SUM(json_extract(usage_json, '$.turns')), 0) AS turns,
              COALESCE(SUM(CAST(json_extract(usage_json, '$.cost_decimal') AS REAL)), 0)
                AS cost,
              COALESCE(SUM(json_extract(usage_json, '$.input_tokens')), 0)
                + COALESCE(SUM(json_extract(usage_json, '$.output_tokens')), 0)
                AS tokens
            FROM backend_calls
            WHERE run_id = ? AND usage_json IS NOT NULL
            """,
            (run_id,),
        ).fetchone()
        turns = int(usage_row["turns"] or 0)
        cost = float(usage_row["cost"] or 0)
        tokens = int(usage_row["tokens"] or 0)
        try:
            started = datetime.fromisoformat(row["run_created_at"])
            run_seconds = max(
                0, int((self._aware_datetime(None) - started).total_seconds())
            )
        except (ValueError, TypeError):
            run_seconds = 0
        exceeded = False
        for usage, limit in (
            (calls, row["max_calls"]),
            (tasks, row["max_tasks"]),
            (run_seconds, row["max_run_seconds"]),
            (turns, row["max_turns"]),
            (cost, row["max_cost_decimal"]),
        ):
            if limit is None:
                continue
            try:
                numeric_limit = float(limit)
            except (TypeError, ValueError):
                continue
            if usage >= numeric_limit:
                exceeded = True
        return {
            "run_id": run_id,
            "exceeded": exceeded,
            "calls": int(calls),
            "tasks": int(tasks),
            "run_seconds": run_seconds,
            "turns": turns,
            "tokens": tokens,
            "cost": round(cost, 6),
            "max_calls": row["max_calls"],
            "max_tasks": row["max_tasks"],
            "max_run_seconds": row["max_run_seconds"],
            "max_turns": row["max_turns"],
            "max_cost_decimal": row["max_cost_decimal"],
        }

    def list_budget_reservations(
        self, run_id: str, *, status: str | None = None
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM budget_reservations WHERE run_id=?"
        params: list[Any] = [run_id]
        if status is not None:
            query += " AND status=?"
            params.append(status)
        query += " ORDER BY reserved_at, reservation_id"
        return [dict(row) for row in self.connection.execute(query, params).fetchall()]

    def record_outbox_intent(
        self,
        run_id: str,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        payload: dict[str, Any],
        controller: ControllerToken,
    ) -> str:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            now = self._aware_datetime(None).isoformat()
            self._ensure_controller_tx(controller, now)
            if controller.run_id != run_id:
                raise FencedControllerError("controller token belongs to another Run")
            outbox_id = f"outbox-{uuid.uuid4().hex[:16]}"
            self.connection.execute(
                """
                INSERT INTO outbox(
                    outbox_id, run_id, aggregate_type, aggregate_id, event_type,
                    payload_json, status, available_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)
                """,
                (
                    outbox_id,
                    run_id,
                    aggregate_type,
                    aggregate_id,
                    event_type,
                    json.dumps(payload),
                    now,
                    now,
                ),
            )
            self._append_event(
                run_id,
                aggregate_id,
                None,
                "outbox.recorded",
                "PENDING",
                "PENDING",
                {"outbox_id": outbox_id, "event_type": event_type},
            )
            self.connection.commit()
            return outbox_id
        except BaseException:
            self.connection.rollback()
            raise

    def claim_outbox(
        self, run_id: str, controller: ControllerToken
    ) -> dict[str, Any] | None:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            now = self._aware_datetime(None).isoformat()
            self._ensure_controller_tx(controller, now)
            if controller.run_id != run_id:
                raise FencedControllerError("controller token belongs to another Run")
            row = self.connection.execute(
                """
                SELECT outbox_id, event_type, payload_json, attempts
                FROM outbox
                WHERE run_id = ? AND status = 'PENDING' AND available_at <= ?
                ORDER BY created_at, outbox_id
                LIMIT 1
                """,
                (run_id, now),
            ).fetchone()
            if row is None:
                self.connection.commit()
                return None
            self.connection.execute(
                """
                UPDATE outbox SET attempts = attempts + 1 WHERE outbox_id = ?
                """,
                (row["outbox_id"],),
            )
            self.connection.commit()
            return {
                "outbox_id": row["outbox_id"],
                "event_type": row["event_type"],
                "payload": json.loads(row["payload_json"]),
                "attempts": int(row["attempts"]) + 1,
            }
        except BaseException:
            self.connection.rollback()
            raise

    def finish_outbox(
        self, outbox_id: str, status: str, controller: ControllerToken
    ) -> None:
        if status not in {"sent", "failed"}:
            raise ValueError(f"unsupported outbox status: {status}")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            now = self._aware_datetime(None).isoformat()
            self._ensure_controller_tx(controller, now)
            row = self.connection.execute(
                "SELECT run_id, attempts FROM outbox WHERE outbox_id = ?",
                (outbox_id,),
            ).fetchone()
            if row is None:
                raise KeyError(outbox_id)
            if row["run_id"] != controller.run_id:
                raise FencedControllerError("controller token belongs to another Run")
            if status == "sent":
                self.connection.execute(
                    """
                    UPDATE outbox
                    SET status = 'SENT', sent_at = ?
                    WHERE outbox_id = ? AND status = 'PENDING'
                    """,
                    (now, outbox_id),
                )
                event_kind = "outbox.delivered"
            else:
                # B3（P1-06）：退避重试 / 死信。
                # attempts 已在 claim 时 +1；未达上限则保持 PENDING 并安排重试，
                # 达到上限则进入 FAILED（死信），不再投递。
                attempts = int(row["attempts"] or 1)
                max_attempts = self.MAX_OUTBOX_ATTEMPTS
                if attempts < max_attempts:
                    from datetime import timedelta as _timedelta

                    backoff_seconds = min(120, 2 ** (attempts - 1))
                    next_at = (
                        self._aware_datetime(None)
                        + _timedelta(seconds=backoff_seconds)
                    ).isoformat()
                    self.connection.execute(
                        """
                        UPDATE outbox
                        SET available_at = ?
                        WHERE outbox_id = ? AND status = 'PENDING'
                        """,
                        (next_at, outbox_id),
                    )
                    event_kind = "outbox.retry_scheduled"
                else:
                    self.connection.execute(
                        """
                        UPDATE outbox
                        SET status = 'FAILED'
                        WHERE outbox_id = ? AND status = 'PENDING'
                        """,
                        (outbox_id,),
                    )
                    event_kind = "outbox.dead_letter"
            self._append_event(
                row["run_id"],
                outbox_id,
                None,
                event_kind,
                "PENDING",
                "SENT" if status == "sent" else "FAILED",
                {"outbox_id": outbox_id, "attempts": int(row["attempts"] or 1)},
            )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def mark_backend_call_running(
        self,
        call_id: str,
        snapshot: CallSnapshot,
        *,
        reason: str,
        controller: ControllerToken | None = None,
    ) -> None:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                """
                SELECT c.*, a.state AS attempt_state,
                       a.generation AS attempt_generation,
                       t.state AS task_state, l.state AS lease_state,
                       l.expires_at, ai.status AS agent_status,
                       ai.current_task_id, s.state AS session_state
                FROM backend_calls c
                JOIN attempts a ON a.attempt_id = c.attempt_id
                JOIN tasks t ON t.task_id = c.task_id
                JOIN assignment_leases l ON l.attempt_id = c.attempt_id
                JOIN agent_instances ai ON ai.agent_id = c.agent_id
                JOIN backend_sessions s ON s.session_ref_id = c.session_ref_id
                WHERE c.call_id = ?
                """,
                (call_id,),
            ).fetchone()
            if row is None:
                raise KeyError(call_id)
            now = utc_now()
            self._ensure_backend_call_controller_tx(row, controller, now)
            if row["state"] != "starting" or snapshot.state != CallState.RUNNING:
                raise ValueError("backend call is not starting")
            if not (
                row["generation"] == row["attempt_generation"]
                and row["task_state"] == TaskState.ACTIVE.value
                and row["lease_state"] == "ACTIVE"
                and row["expires_at"] > now
                and row["agent_status"] == AgentState.BUSY.value
                and row["current_task_id"] == row["task_id"]
                and row["session_state"] in {
                    SessionState.IDLE.value,
                    SessionState.ACTIVE.value,
                }
            ):
                raise FencedAttemptError("backend start confirmation is stale")
            current_attempt = AttemptState(row["attempt_state"])
            ensure_attempt_transition(current_attempt, AttemptState.RUNNING)
            self.connection.execute(
                """
                UPDATE backend_calls SET state = 'running', provider_call_id = ?,
                    backend_invoked = ?, started_at = ? WHERE call_id = ?
                """,
                (
                    snapshot.ref.provider_call_id,
                    int(snapshot.backend_invoked),
                    snapshot.started_at or now,
                    call_id,
                ),
            )
            self.connection.execute(
                "UPDATE attempts SET state = 'RUNNING', updated_at = ? WHERE attempt_id = ?",
                (now, row["attempt_id"]),
            )
            self._append_event(
                row["run_id"],
                row["task_id"],
                row["attempt_id"],
                "backend.call.running",
                "starting",
                "running",
                {"call_id": call_id, "reason": reason},
            )
            self._append_event(
                row["run_id"],
                row["task_id"],
                row["attempt_id"],
                "attempt.transitioned",
                current_attempt.value,
                AttemptState.RUNNING.value,
                {"reason": reason, "call_id": call_id},
            )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def finish_backend_call(
        self,
        call_id: str,
        snapshot: CallSnapshot,
        *,
        reason: str,
        controller: ControllerToken | None = None,
    ) -> str:
        if not snapshot.state.is_terminal:
            raise ValueError("backend call snapshot is not terminal")
        result_json = json.dumps(
            {
                "text": snapshot.text,
                "structured": _jsonable(snapshot.structured),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        failure_json = (
            None
            if snapshot.failure is None
            else json.dumps(_jsonable(snapshot.failure), ensure_ascii=False, sort_keys=True)
        )
        usage_json = (
            None
            if snapshot.usage is None
            else json.dumps(_jsonable(snapshot.usage), ensure_ascii=False, sort_keys=True)
        )
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            now = utc_now()
            row = self.connection.execute(
                """
                SELECT c.*, a.state AS attempt_state, a.generation AS attempt_generation,
                       t.state AS task_state, t.max_attempts,
                       l.state AS lease_state, l.expires_at,
                       COALESCE(d.retry_backoff_base_seconds, 1)
                           AS retry_backoff_base_seconds,
                       COALESCE(d.retry_backoff_max_seconds, 60)
                           AS retry_backoff_max_seconds,
                       (SELECT COUNT(*) FROM attempts x WHERE x.task_id = c.task_id)
                           AS attempt_count
                FROM backend_calls c
                JOIN attempts a ON a.attempt_id = c.attempt_id
                JOIN tasks t ON t.task_id = c.task_id
                JOIN assignment_leases l ON l.attempt_id = c.attempt_id
                LEFT JOIN task_dispatch_specs d ON d.task_id = c.task_id
                WHERE c.call_id = ?
                """,
                (call_id,),
            ).fetchone()
            if row is None:
                raise KeyError(call_id)
            self._ensure_backend_call_controller_tx(row, controller, now)
            if row["disposition"] is not None:
                disposition = str(row["disposition"])
                self.connection.commit()
                return disposition
            current_dispatch = (
                row["generation"] == row["attempt_generation"]
                and row["attempt_state"]
                in {AttemptState.ASSIGNED.value, AttemptState.RUNNING.value}
                and row["task_state"] == TaskState.ACTIVE.value
                and row["lease_state"] == "ACTIVE"
                and row["expires_at"] > now
            )
            late_result = snapshot.state == CallState.SUCCEEDED and not current_dispatch
            task_cancel_requested = (
                row["task_state"] == TaskState.CANCEL_REQUESTED.value
            )
            attempt_cancel_requested = (
                row["attempt_state"] == AttemptState.CANCEL_REQUESTED.value
            )
            cancel_convergence = task_cancel_requested and attempt_cancel_requested
            attempt_target: AttemptState | None = None
            task_target: TaskState | None = None
            retry_delay: int | None = None
            retry_available_at: str | None = None
            if cancel_convergence:
                disposition = "cancelled"
                late_result = snapshot.state == CallState.SUCCEEDED
                attempt_target = AttemptState.CANCELLED
                task_target = TaskState.CANCELLED
            elif late_result:
                disposition = "late"
            elif snapshot.state == CallState.SUCCEEDED and current_dispatch:
                disposition = "submitted"
                attempt_target = AttemptState.SUBMITTED
                task_target = TaskState.REVIEW
            elif current_dispatch and snapshot.state in {
                CallState.FAILED,
                CallState.TIMED_OUT,
            }:
                retryable = snapshot.failure is None or snapshot.failure.retryable
                can_retry = retryable and row["attempt_count"] < row["max_attempts"]
                disposition = "requeued" if can_retry else "failed"
                attempt_target = AttemptState.FAILED
                task_target = TaskState.READY if can_retry else TaskState.FAILED
                if can_retry:
                    retry_delay, retry_available_at = self._retry_schedule(
                        attempt_count=int(row["attempt_count"]),
                        base_seconds=int(row["retry_backoff_base_seconds"]),
                        max_seconds=int(row["retry_backoff_max_seconds"]),
                        now=now,
                    )
            elif current_dispatch and snapshot.state == CallState.BLOCKED:
                disposition = "failed"
                attempt_target = AttemptState.FAILED
                task_target = TaskState.FAILED
            elif current_dispatch and snapshot.state == CallState.CANCELLED:
                disposition = "cancelled"
                attempt_target = AttemptState.CANCELLED
                task_target = TaskState.CANCELLED
            else:
                disposition = "recorded"
            self.connection.execute(
                """
                UPDATE backend_calls
                SET state = ?, provider_call_id = ?, backend_invoked = ?,
                    backend_may_still_run = ?, finished_at = ?, result_json = ?,
                    failure_json = ?, usage_json = ?, late_result = ?,
                    disposition = ?, settled_at = ?
                WHERE call_id = ?
                """,
                (
                    snapshot.state.value,
                    snapshot.ref.provider_call_id,
                    int(snapshot.backend_invoked),
                    int(snapshot.backend_may_still_run),
                    snapshot.finished_at or now,
                    result_json,
                    failure_json,
                    usage_json,
                    int(late_result),
                    disposition,
                    now,
                    call_id,
                ),
            )
            self.connection.execute(
                """
                UPDATE budget_reservations
                SET status='SETTLED', settled_at=?
                WHERE call_id=? AND status='RESERVED'
                """,
                (now, call_id),
            )
            if cancel_convergence and not snapshot.backend_may_still_run:
                self._ack_terminate_messages_tx(
                    row["run_id"], row["task_id"], row["attempt_id"], call_id, now
                )
            if attempt_target is not None and task_target is not None:
                resource_release_allowed = not snapshot.backend_may_still_run
                if resource_release_allowed:
                    self.connection.execute(
                        """
                        UPDATE assignment_leases SET state = 'RELEASED', closed_at = ?,
                            close_reason = ?
                        WHERE attempt_id = ? AND generation = ? AND state = 'ACTIVE'
                        """,
                        (
                            now,
                            f"backend-{snapshot.state.value}",
                            row["attempt_id"],
                            row["generation"],
                        ),
                    )
                if retry_available_at is not None:
                    self.connection.execute(
                        """
                        UPDATE task_dispatch_specs SET available_at = ?
                        WHERE task_id = ?
                        """,
                        (retry_available_at, row["task_id"]),
                    )
                self.connection.execute(
                    """
                    UPDATE attempts SET state = ?, finished_at = ?,
                        terminal_reason = ?, updated_at = ? WHERE attempt_id = ?
                    """,
                    (
                        attempt_target.value,
                        now,
                        None if attempt_target == AttemptState.SUBMITTED else reason,
                        now,
                        row["attempt_id"],
                    ),
                )
                self.connection.execute(
                    """
                    UPDATE tasks SET state = ?, terminal_reason = ?,
                        version = version + 1, updated_at = ? WHERE task_id = ?
                    """,
                    (
                        task_target.value,
                        reason if task_target == TaskState.FAILED else None,
                        now,
                        row["task_id"],
                    ),
                )
                if resource_release_allowed:
                    self.connection.execute(
                        """
                        UPDATE agent_instances
                        SET status = CASE WHEN status = 'BUSY' THEN 'IDLE' ELSE status END,
                            current_task_id = NULL, updated_at = ?
                        WHERE agent_id = ? AND current_task_id = ?
                          AND status IN ('BUSY', 'DRAINING')
                        """,
                        (now, row["agent_id"], row["task_id"]),
                    )
                    self.connection.execute(
                        """
                        UPDATE backend_sessions SET state = 'IDLE', updated_at = ?
                        WHERE session_ref_id = ? AND state = 'ACTIVE'
                        """,
                        (now, row["session_ref_id"]),
                    )
                else:
                    self._append_event(
                        row["run_id"], row["task_id"], row["attempt_id"],
                        "backend.resource.held", row["state"], row["state"],
                        {"call_id": call_id, "reason": "backend_may_still_run"},
                    )
            event_kind = "backend.call.late_result" if late_result else "backend.call.finished"
            self._append_event(
                row["run_id"],
                row["task_id"],
                row["attempt_id"],
                event_kind,
                row["state"],
                snapshot.state.value,
                {"call_id": call_id, "reason": reason, "late_result": late_result},
            )
            if attempt_target is not None and task_target is not None:
                attempt_from = row["attempt_state"]
                task_from = row["task_state"]
                self._append_event(
                    row["run_id"],
                    row["task_id"],
                    row["attempt_id"],
                    "attempt.transitioned",
                    attempt_from,
                    attempt_target.value,
                    {"reason": reason, "call_id": call_id},
                )
                self._append_event(
                    row["run_id"],
                    row["task_id"],
                    row["attempt_id"],
                    "task.transitioned",
                    task_from,
                    task_target.value,
                    {
                        "reason": reason,
                        "call_id": call_id,
                        "retry_delay_seconds": retry_delay,
                        "available_at": retry_available_at,
                        "attempt_count": row["attempt_count"],
                    },
                )
                if task_target in {TaskState.FAILED, TaskState.CANCELLED}:
                    self._reconcile_task_graph_tx(row["run_id"], now)
            self.connection.commit()
            return disposition
        except BaseException:
            self.connection.rollback()
            raise

    def starting_backend_calls(self, *, run_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT call_id, run_id, task_id, attempt_id, generation, state,
                   backend_invoked, scheduler_owner, controller_epoch
            FROM backend_calls
            WHERE run_id = ? AND state IN ('starting', 'running', 'cancel_requested')
            ORDER BY requested_at, call_id
            """,
            (run_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def recover_backend_call(
        self,
        call_id: str,
        *,
        controller: ControllerToken,
        reason: str,
        allow_current_epoch: bool = False,
    ) -> str:
        """Atomically fence one abandoned controller-owned call and its resources."""
        now = utc_now()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self._ensure_controller_tx(controller, now)
            row = self.connection.execute(
                """
                SELECT c.*, a.state AS attempt_state,
                       a.generation AS attempt_generation,
                       a.recovery_outcome, t.state AS task_state,
                       t.max_attempts, l.state AS lease_state,
                       COALESCE(d.retry_backoff_base_seconds, 1)
                           AS retry_backoff_base_seconds,
                       COALESCE(d.retry_backoff_max_seconds, 60)
                           AS retry_backoff_max_seconds,
                       (SELECT COUNT(*) FROM attempts x WHERE x.task_id = c.task_id)
                           AS attempt_count
                FROM backend_calls c
                JOIN attempts a ON a.attempt_id = c.attempt_id
                JOIN tasks t ON t.task_id = c.task_id
                JOIN assignment_leases l ON l.attempt_id = c.attempt_id
                LEFT JOIN task_dispatch_specs d ON d.task_id = c.task_id
                WHERE c.call_id = ?
                """,
                (call_id,),
            ).fetchone()
            if row is None:
                raise KeyError(call_id)
            if row["run_id"] != controller.run_id:
                raise FencedControllerError("backend call belongs to another Run")
            call_epoch = row["controller_epoch"]
            if call_epoch is not None:
                call_epoch = int(call_epoch)
                if call_epoch > controller.epoch or (
                    call_epoch == controller.epoch and not allow_current_epoch
                ):
                    raise FencedControllerError(
                        "controller cannot recover this backend call epoch"
                    )
            if row["state"] not in {"starting", "running", "cancel_requested"}:
                disposition = str(row["disposition"] or "recorded")
                self.connection.commit()
                return disposition

            previous_call_state = str(row["state"])
            isolate_session = (
                previous_call_state != "starting"
                or bool(row["backend_invoked"])
                or bool(row["backend_may_still_run"])
                or row["controller_epoch"] is None
            )
            self.connection.execute(
                """
                UPDATE backend_calls SET state = 'orphaned', finished_at = ?,
                    late_result = 1, disposition = 'orphaned', settled_at = ?,
                    backend_may_still_run = CASE
                        WHEN state = 'starting' AND backend_invoked = 0
                             AND backend_may_still_run = 0 THEN 0 ELSE 1 END
                WHERE call_id = ?
                  AND state IN ('starting', 'running', 'cancel_requested')
                """,
                (now, now, call_id),
            )
            self.connection.execute(
                """
                UPDATE budget_reservations
                SET status='SETTLED', settled_at=?
                WHERE call_id=? AND status='RESERVED'
                """,
                (now, call_id),
            )

            outcome = "orphaned"
            recoverable_attempt = (
                row["generation"] == row["attempt_generation"]
                and row["attempt_state"]
                in {AttemptState.ASSIGNED.value, AttemptState.RUNNING.value}
                and row["task_state"] == TaskState.ACTIVE.value
                and row["lease_state"] == "ACTIVE"
                and row["recovery_outcome"] is None
            )
            if recoverable_attempt:
                outcome = (
                    "requeued"
                    if row["attempt_count"] < row["max_attempts"]
                    else "failed"
                )
                task_target = (
                    TaskState.READY if outcome == "requeued" else TaskState.FAILED
                )
                retry_delay: int | None = None
                retry_available_at: str | None = None
                if outcome == "requeued":
                    retry_delay, retry_available_at = self._retry_schedule(
                        attempt_count=int(row["attempt_count"]),
                        base_seconds=int(row["retry_backoff_base_seconds"]),
                        max_seconds=int(row["retry_backoff_max_seconds"]),
                        now=now,
                    )
                released = self.connection.execute(
                    """
                    UPDATE assignment_leases
                    SET state = 'EXPIRED', closed_at = ?, close_reason = ?
                    WHERE attempt_id = ? AND generation = ? AND state = 'ACTIVE'
                    """,
                    (now, reason, row["attempt_id"], row["generation"]),
                ).rowcount
                if released != 1:
                    raise FencedAttemptError("attempt lease was renewed or closed")
                self.connection.execute(
                    """
                    UPDATE attempts SET state = 'STALE', finished_at = ?,
                        terminal_reason = ?, recovery_outcome = ?, updated_at = ?
                    WHERE attempt_id = ?
                    """,
                    (now, reason, outcome, now, row["attempt_id"]),
                )
                self.connection.execute(
                    """
                    UPDATE tasks SET state = ?, terminal_reason = ?,
                        version = version + 1, updated_at = ? WHERE task_id = ?
                    """,
                    (
                        task_target.value,
                        reason if task_target == TaskState.FAILED else None,
                        now,
                        row["task_id"],
                    ),
                )
                if retry_available_at is not None:
                    self.connection.execute(
                        "UPDATE task_dispatch_specs SET available_at = ? WHERE task_id = ?",
                        (retry_available_at, row["task_id"]),
                    )
                self._append_event(
                    row["run_id"],
                    row["task_id"],
                    row["attempt_id"],
                    "attempt.transitioned",
                    row["attempt_state"],
                    AttemptState.STALE.value,
                    {"reason": reason, "generation": row["generation"]},
                )
                self._append_event(
                    row["run_id"],
                    row["task_id"],
                    row["attempt_id"],
                    "task.transitioned",
                    row["task_state"],
                    task_target.value,
                    {
                        "reason": reason,
                        "recovery_outcome": outcome,
                        "retry_delay_seconds": retry_delay,
                        "available_at": retry_available_at,
                        "attempt_count": row["attempt_count"],
                    },
                )
                if task_target == TaskState.FAILED:
                    self._reconcile_task_graph_tx(row["run_id"], now)

            if isolate_session:
                self.connection.execute(
                    """
                    UPDATE agent_instances SET status = 'DRAINING',
                        current_task_id = NULL, updated_at = ?
                    WHERE agent_id = ? AND current_task_id = ? AND status = 'BUSY'
                    """,
                    (now, row["agent_id"], row["task_id"]),
                )
                self.connection.execute(
                    """
                    UPDATE backend_sessions SET state = 'FAILED', updated_at = ?,
                        close_reason = ?
                    WHERE session_ref_id = ? AND state = 'ACTIVE'
                    """,
                    (now, reason, row["session_ref_id"]),
                )
            else:
                self.connection.execute(
                    """
                    UPDATE agent_instances
                    SET status = CASE WHEN status = 'BUSY' THEN 'IDLE' ELSE status END,
                        current_task_id = NULL, updated_at = ?
                    WHERE agent_id = ? AND current_task_id = ?
                      AND status IN ('BUSY', 'DRAINING')
                    """,
                    (now, row["agent_id"], row["task_id"]),
                )
                self.connection.execute(
                    """
                    UPDATE backend_sessions SET state = 'IDLE', updated_at = ?
                    WHERE session_ref_id = ? AND state = 'ACTIVE'
                    """,
                    (now, row["session_ref_id"]),
                )

            self._append_event(
                row["run_id"],
                row["task_id"],
                row["attempt_id"],
                "backend.call.orphaned",
                previous_call_state,
                "orphaned",
                {
                    "call_id": call_id,
                    "reason": reason,
                    "recovering_controller_epoch": controller.epoch,
                },
            )
            self._append_event(
                row["run_id"],
                row["task_id"],
                row["attempt_id"],
                "dispatch.resources_released",
                None,
                None,
                {
                    "call_id": call_id,
                    "reason": reason,
                    "session_isolated": isolate_session,
                },
            )
            self.connection.commit()
            return outcome
        except BaseException:
            self.connection.rollback()
            raise

    def mark_backend_call_orphaned(self, call_id: str, *, reason: str) -> None:
        with self.connection:
            row = self.connection.execute(
                "SELECT * FROM backend_calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            if row is None:
                raise KeyError(call_id)
            if row["state"] not in {"starting", "running", "cancel_requested"}:
                return
            previous_state = row["state"]
            self.connection.execute(
                """
                UPDATE backend_calls SET state = 'orphaned', finished_at = ?,
                    late_result = 1, disposition = 'orphaned', settled_at = ?,
                    backend_may_still_run = CASE
                        WHEN state = 'starting' AND backend_invoked = 0
                             AND backend_may_still_run = 0 THEN 0 ELSE 1 END
                WHERE call_id = ?
                  AND state IN ('starting', 'running', 'cancel_requested')
                """,
                (utc_now(), utc_now(), call_id),
            )
            self.connection.execute(
                """
                UPDATE budget_reservations
                SET status='SETTLED', settled_at=?
                WHERE call_id=? AND status='RESERVED'
                """,
                (utc_now(), call_id),
            )
            self._append_event(
                row["run_id"],
                row["task_id"],
                row["attempt_id"],
                "backend.call.orphaned",
                previous_state,
                "orphaned",
                {"call_id": call_id, "reason": reason},
            )

    def release_dispatch_resources(
        self,
        call_id: str,
        *,
        reason: str,
        isolate_session: bool = False,
    ) -> None:
        with self.connection:
            row = self.connection.execute(
                "SELECT * FROM backend_calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            if row is None:
                raise KeyError(call_id)
            now = utc_now()
            if isolate_session:
                self.connection.execute(
                    """
                    UPDATE agent_instances SET status = 'DRAINING',
                        current_task_id = NULL, updated_at = ?
                    WHERE agent_id = ? AND current_task_id = ?
                      AND status = 'BUSY'
                    """,
                    (now, row["agent_id"], row["task_id"]),
                )
                self.connection.execute(
                    """
                    UPDATE backend_sessions SET state = 'FAILED', updated_at = ?,
                        close_reason = ?
                    WHERE session_ref_id = ? AND state = 'ACTIVE'
                    """,
                    (now, reason, row["session_ref_id"]),
                )
            else:
                self.connection.execute(
                    """
                    UPDATE agent_instances
                    SET status = CASE WHEN status = 'BUSY' THEN 'IDLE' ELSE status END,
                        current_task_id = NULL, updated_at = ?
                    WHERE agent_id = ? AND current_task_id = ?
                      AND status IN ('BUSY', 'DRAINING')
                    """,
                    (now, row["agent_id"], row["task_id"]),
                )
                self.connection.execute(
                    """
                    UPDATE backend_sessions SET state = 'IDLE', updated_at = ?
                    WHERE session_ref_id = ? AND state = 'ACTIVE'
                    """,
                    (now, row["session_ref_id"]),
                )
            self._append_event(
                row["run_id"],
                row["task_id"],
                row["attempt_id"],
                "dispatch.resources_released",
                None,
                None,
                {
                    "call_id": call_id,
                    "reason": reason,
                    "session_isolated": isolate_session,
                },
            )

    def pool_agent_snapshots(
        self, run_id: str, pool_id: str
    ) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT a.*,
                   b.binding_id, b.role_id, b.role_version,
                   s.session_ref_id, s.state AS session_state
            FROM agent_instances a
            JOIN role_bindings b ON b.agent_id = a.agent_id
            LEFT JOIN backend_sessions s
              ON s.agent_id = a.agent_id AND s.run_id = b.run_id
             AND s.state IN ('OPENING', 'IDLE', 'ACTIVE')
            WHERE b.run_id = ? AND b.status = 'ACTIVE'
              AND b.binding_kind = 'PRIMARY'
              AND a.pool_id = ? AND a.origin = 'RECONCILER'
            ORDER BY a.created_at, a.agent_id
            """,
            (run_id, pool_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def acquire_pool_reconcile_lock(
        self,
        run_id: str,
        pool_id: str,
        *,
        lease_seconds: int = 30,
    ) -> str | None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be at least 1")
        owner = f"pool-lock-{uuid.uuid4().hex}"
        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(seconds=lease_seconds)).isoformat()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                """
                DELETE FROM pool_reconcile_locks
                WHERE run_id = ? AND pool_id = ? AND expires_at <= ?
                """,
                (run_id, pool_id, now.isoformat()),
            )
            try:
                self.connection.execute(
                    """
                    INSERT INTO pool_reconcile_locks(
                        run_id, pool_id, owner, acquired_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (run_id, pool_id, owner, now.isoformat(), expires_at),
                )
            except sqlite3.IntegrityError:
                self.connection.rollback()
                return None
            self.connection.commit()
            return owner
        except BaseException:
            self.connection.rollback()
            raise

    def release_pool_reconcile_lock(
        self, run_id: str, pool_id: str, owner: str
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                DELETE FROM pool_reconcile_locks
                WHERE run_id = ? AND pool_id = ? AND owner = ?
                """,
                (run_id, pool_id, owner),
            )

    def provision_fake_pool_agent(
        self,
        *,
        run_id: str,
        pool_id: str,
        backend: str,
        model: str | None,
        provider_id: str | None = None,
        role_id: str,
        role_version: int = 1,
    ) -> dict[str, str]:
        agent_id = f"agent-{pool_id}-{uuid.uuid4().hex[:12]}"
        binding_id = f"binding-{uuid.uuid4().hex[:12]}"
        session_ref_id = f"session-{uuid.uuid4().hex[:12]}"
        provider_session_id = f"fake-provider-{uuid.uuid4().hex[:12]}"
        now = utc_now()
        with self.connection:
            run = self.connection.execute(
                "SELECT team_id FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            self.connection.execute(
                """
                INSERT INTO agent_instances(
                    agent_id, team_id, pool_id, backend, model, provider_id, status,
                    capabilities_actual_json, authority_epoch, origin,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'IDLE', '[]', 0, 'RECONCILER', ?, ?)
                """,
                (
                    agent_id,
                    run["team_id"],
                    pool_id,
                    backend,
                    model,
                    provider_id,
                    now,
                    now,
                ),
            )
            self.connection.execute(
                """
                INSERT INTO role_bindings(
                    binding_id, run_id, agent_id, role_id, role_version,
                    binding_kind, status, started_at
                ) VALUES (?, ?, ?, ?, ?, 'PRIMARY', 'ACTIVE', ?)
                """,
                (binding_id, run_id, agent_id, role_id, role_version, now),
            )
            self.connection.execute(
                """
                INSERT INTO backend_sessions(
                    session_ref_id, run_id, agent_id, backend,
                    provider_session_id, state, cwd, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'IDLE', '', ?, ?)
                """,
                (
                    session_ref_id,
                    run_id,
                    agent_id,
                    backend,
                    provider_session_id,
                    now,
                    now,
                ),
            )
            self._append_event(
                run_id,
                None,
                None,
                "pool.agent.provisioned",
                None,
                AgentState.IDLE.value,
                {
                    "agent_id": agent_id,
                    "binding_id": binding_id,
                    "session_ref_id": session_ref_id,
                    "pool_id": pool_id,
                },
            )
        return {
            "agent_id": agent_id,
            "binding_id": binding_id,
            "session_ref_id": session_ref_id,
        }

    def assign_agent_task(self, agent_id: str, task_id: str, *, reason: str) -> None:
        with self.connection:
            row = self.connection.execute(
                """
                SELECT a.status, t.run_id
                FROM agent_instances a
                JOIN tasks t ON t.task_id = ?
                JOIN role_bindings b
                  ON b.agent_id = a.agent_id AND b.run_id = t.run_id
                 AND b.status = 'ACTIVE' AND b.binding_kind = 'PRIMARY'
                WHERE a.agent_id = ?
                """,
                (task_id, agent_id),
            ).fetchone()
            if row is None:
                raise ValueError("agent has no active primary binding for task run")
            current = AgentState(row["status"])
            ensure_agent_transition(current, AgentState.BUSY)
            self.connection.execute(
                """
                UPDATE agent_instances SET status = 'BUSY', current_task_id = ?,
                    updated_at = ? WHERE agent_id = ?
                """,
                (task_id, utc_now(), agent_id),
            )
            self._append_event(
                row["run_id"],
                task_id,
                None,
                "agent.task_assigned",
                current.value,
                AgentState.BUSY.value,
                {"agent_id": agent_id, "reason": reason},
            )

    def release_agent_task(self, agent_id: str, *, reason: str) -> None:
        with self.connection:
            row = self.connection.execute(
                """
                SELECT a.status, a.current_task_id, a.team_id
                FROM agent_instances a WHERE a.agent_id = ?
                """,
                (agent_id,),
            ).fetchone()
            if row is None:
                raise KeyError(agent_id)
            current = AgentState(row["status"])
            if current not in {AgentState.BUSY, AgentState.DRAINING}:
                raise ValueError("agent is not running assigned work")
            target = AgentState.IDLE if current == AgentState.BUSY else current
            if target != current:
                ensure_agent_transition(current, target)
            self.connection.execute(
                """
                UPDATE agent_instances SET status = ?, current_task_id = NULL,
                    updated_at = ? WHERE agent_id = ?
                """,
                (target.value, utc_now(), agent_id),
            )
            run_id = self._event_run_for_agent(agent_id, row["team_id"])
            if run_id is not None:
                self._append_event(
                    run_id,
                    row["current_task_id"],
                    None,
                    "agent.task_released",
                    current.value,
                    target.value,
                    {"agent_id": agent_id, "reason": reason},
                )

    def mark_agent_draining(self, agent_id: str, *, run_id: str, reason: str) -> None:
        with self.connection:
            row = self.connection.execute(
                "SELECT status FROM agent_instances WHERE agent_id = ?", (agent_id,)
            ).fetchone()
            if row is None:
                raise KeyError(agent_id)
            current = AgentState(row["status"])
            ensure_agent_transition(current, AgentState.DRAINING)
            self.connection.execute(
                "UPDATE agent_instances SET status = 'DRAINING', updated_at = ? WHERE agent_id = ?",
                (utc_now(), agent_id),
            )
            self._append_event(
                run_id,
                None,
                None,
                "pool.agent.draining",
                current.value,
                AgentState.DRAINING.value,
                {"agent_id": agent_id, "reason": reason},
            )

    def finalize_drained_agent(self, agent_id: str, *, run_id: str) -> None:
        with self.connection:
            agent = self.connection.execute(
                "SELECT status, current_task_id FROM agent_instances WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            if agent is None:
                raise KeyError(agent_id)
            if AgentState(agent["status"]) != AgentState.DRAINING:
                raise ValueError("agent is not draining")
            if agent["current_task_id"] is not None:
                raise ValueError("draining agent still owns a task")
            active_leases = self.connection.execute(
                """
                SELECT COUNT(*) FROM assignment_leases
                WHERE owner_agent_id = ? AND state = 'ACTIVE'
                """,
                (agent_id,),
            ).fetchone()[0]
            if active_leases:
                raise ValueError("draining agent still owns an active lease")
            active_session = self.connection.execute(
                """
                SELECT session_ref_id, state FROM backend_sessions
                WHERE agent_id = ? AND run_id = ?
                  AND state IN ('OPENING', 'IDLE', 'ACTIVE')
                """,
                (agent_id, run_id),
            ).fetchone()
            if active_session is not None and active_session["state"] == "ACTIVE":
                raise ValueError("draining agent session still has an active turn")
            now = utc_now()
            if active_session is not None:
                self.connection.execute(
                    """
                    UPDATE backend_sessions SET state = 'CLOSED', updated_at = ?,
                        closed_at = ?, close_reason = 'pool-scale-down'
                    WHERE session_ref_id = ?
                    """,
                    (now, now, active_session["session_ref_id"]),
                )
            self.connection.execute(
                """
                UPDATE role_bindings SET status = 'ENDED', ended_at = ?,
                    end_reason = 'pool-scale-down'
                WHERE run_id = ? AND agent_id = ? AND status = 'ACTIVE'
                """,
                (now, run_id, agent_id),
            )
            self.connection.execute(
                """
                UPDATE agent_instances SET status = 'STOPPED', stopped_at = ?,
                    updated_at = ? WHERE agent_id = ?
                """,
                (now, now, agent_id),
            )
            self._append_event(
                run_id,
                None,
                None,
                "pool.agent.stopped",
                AgentState.DRAINING.value,
                AgentState.STOPPED.value,
                {"agent_id": agent_id},
            )

    def create_task(
        self,
        run_id: str,
        task_id: str,
        *,
        access_mode: str = "read_only",
        write_scope: tuple[str, ...] = (),
        max_attempts: int = 2,
        required_role_id: str = "worker",
        required_backend: str | None = None,
        required_model: str | None = None,
        required_provider_id: str | None = None,
        prompt: str | None = None,
        cwd: str = ".",
        timeout_seconds: float = 60,
        priority: int = 0,
        retry_backoff_base_seconds: int = 1,
        retry_backoff_max_seconds: int = 60,
        parent_task_id: str | None = None,
        dispatch_source: str = "user",
        required_delivery: bool = True,
        task_kind: str = "implementation",
        acceptance_criteria: tuple[str, ...] = (),
        input_refs: tuple[Any, ...] = (),
        output_contract: Any = None,
        budget: Any = None,
    ) -> None:
        self._validate_task_spec(
            task_id=task_id,
            access_mode=access_mode,
            max_attempts=max_attempts,
            required_role_id=required_role_id,
            required_backend=required_backend,
            required_model=required_model,
            required_provider_id=required_provider_id,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            retry_backoff_base_seconds=retry_backoff_base_seconds,
            retry_backoff_max_seconds=retry_backoff_max_seconds,
        )
        # 只读任务不能声明 write_scope（无论是否注入 policy 都强制执行）
        if access_mode != "write" and write_scope:
            raise ValueError("read_only task cannot declare write_scope")
        if self.workspace_policy is not None:
            # P0-03：Task 创建边界——canonical cwd + write_scope 规范化
            cwd = self.workspace_policy.validate_cwd(access_mode, cwd)
            if access_mode == "write":
                write_scope = self.workspace_policy.validate_write_scope(
                    write_scope, base=cwd
                )
        elif access_mode == "write":
            # 无 policy 时仍做 write_scope 基础校验（非空/相对/无 ../.git）
            from orchestrator.workspace.policy import validate_write_scope_static

            write_scope = validate_write_scope_static(write_scope)
        now = utc_now()
        with self.connection:
            self._create_task_tx(
                run_id=run_id,
                task_id=task_id,
                access_mode=access_mode,
                write_scope=write_scope,
                max_attempts=max_attempts,
                required_role_id=required_role_id,
                required_backend=required_backend,
                required_model=required_model,
                required_provider_id=required_provider_id,
                prompt=prompt,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                priority=priority,
                retry_backoff_base_seconds=retry_backoff_base_seconds,
                retry_backoff_max_seconds=retry_backoff_max_seconds,
                parent_task_id=parent_task_id,
                dispatch_source=dispatch_source,
                required_delivery=required_delivery,
                task_kind=task_kind,
                acceptance_criteria=acceptance_criteria,
                input_refs=input_refs,
                output_contract=output_contract,
                budget=budget,
                now=now,
            )

    def create_task_graph(
        self,
        run_id: str,
        task_specs: list[Mapping[str, Any]],
        dependencies: list[tuple[str, str]],
    ) -> None:
        if not task_specs:
            raise ValueError("task graph must contain at least one task")
        allowed_keys = {
            "task_id",
            "access_mode",
            "write_scope",
            "max_attempts",
            "required_role_id",
            "required_backend",
            "required_model",
            "required_provider_id",
            "prompt",
            "cwd",
            "timeout_seconds",
            "priority",
            "retry_backoff_base_seconds",
            "retry_backoff_max_seconds",
            "parent_task_id",
            "dispatch_source",
            "required_delivery",
            "task_kind",
            "acceptance_criteria",
            "input_refs",
            "output_contract",
            "budget",
        }
        normalized: list[dict[str, Any]] = []
        task_ids: set[str] = set()
        for raw in task_specs:
            unknown = set(raw) - allowed_keys
            if unknown:
                raise ValueError(f"unknown task graph fields: {sorted(unknown)}")
            task_id = str(raw.get("task_id", ""))
            if task_id in task_ids:
                raise ValueError(f"duplicate task_id in graph: {task_id}")
            task_ids.add(task_id)
            spec = {
                "task_id": task_id,
                "access_mode": str(raw.get("access_mode", "read_only")),
                "write_scope": tuple(raw.get("write_scope", ())),
                "max_attempts": int(raw.get("max_attempts", 2)),
                "required_role_id": str(raw.get("required_role_id", "worker")),
                "required_backend": raw.get("required_backend"),
                "required_model": raw.get("required_model"),
                "required_provider_id": raw.get("required_provider_id"),
                "prompt": raw.get("prompt"),
                "cwd": str(raw.get("cwd", ".")),
                "timeout_seconds": float(raw.get("timeout_seconds", 60)),
                "priority": int(raw.get("priority", 0)),
                "retry_backoff_base_seconds": int(
                    raw.get("retry_backoff_base_seconds", 1)
                ),
                "retry_backoff_max_seconds": int(
                    raw.get("retry_backoff_max_seconds", 60)
                ),
                "parent_task_id": raw.get("parent_task_id"),
                "dispatch_source": str(raw.get("dispatch_source", "user")),
                "required_delivery": bool(raw.get("required_delivery", True)),
                "task_kind": str(raw.get("task_kind", "implementation")),
                "acceptance_criteria": tuple(raw.get("acceptance_criteria", ())),
                "input_refs": tuple(raw.get("input_refs", ())),
                "output_contract": raw.get("output_contract"),
                "budget": raw.get("budget"),
            }
            self._validate_task_spec(
                task_id=spec["task_id"],
                access_mode=spec["access_mode"],
                max_attempts=spec["max_attempts"],
                required_role_id=spec["required_role_id"],
                required_backend=spec["required_backend"],
                required_model=spec["required_model"],
                required_provider_id=spec["required_provider_id"],
                cwd=spec["cwd"],
                timeout_seconds=spec["timeout_seconds"],
                retry_backoff_base_seconds=spec["retry_backoff_base_seconds"],
                retry_backoff_max_seconds=spec["retry_backoff_max_seconds"],
            )
            # 只读任务不能声明 write_scope（无论是否注入 policy 都强制执行）
            if spec["access_mode"] != "write" and spec["write_scope"]:
                raise ValueError(
                    f"read_only task {spec['task_id']} cannot declare write_scope"
                )
            if self.workspace_policy is not None:
                # P0-03：graph 内每个 task 同样做 cwd/write_scope 边界校验
                spec["cwd"] = self.workspace_policy.validate_cwd(
                    spec["access_mode"], spec["cwd"]
                )
                if spec["access_mode"] == "write":
                    spec["write_scope"] = self.workspace_policy.validate_write_scope(
                        spec["write_scope"], base=spec["cwd"]
                    )
            elif spec["access_mode"] == "write":
                from orchestrator.workspace.policy import validate_write_scope_static

                spec["write_scope"] = validate_write_scope_static(
                    spec["write_scope"]
                )
            normalized.append(spec)

        edges = list(dict.fromkeys(dependencies))
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            if self.connection.execute(
                "SELECT 1 FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone() is None:
                raise KeyError(run_id)
            existing_ids = {
                str(row["task_id"])
                for row in self.connection.execute(
                    "SELECT task_id FROM tasks WHERE run_id = ?", (run_id,)
                )
            }
            duplicate = task_ids & existing_ids
            if duplicate:
                raise ValueError(f"task already exists: {sorted(duplicate)}")
            for task_id, depends_on in edges:
                if task_id not in task_ids:
                    raise ValueError("dependency target must be created in this graph")
                if task_id == depends_on:
                    raise ValueError("task cannot depend on itself")
                if depends_on not in task_ids and depends_on not in existing_ids:
                    other_run = self.connection.execute(
                        "SELECT run_id FROM tasks WHERE task_id = ?", (depends_on,)
                    ).fetchone()
                    if other_run is not None:
                        raise ValueError("dependency belongs to another Run")
                    raise ValueError(f"unknown dependency task: {depends_on}")

            graph_nodes = existing_ids | task_ids
            graph_edges = [
                (str(row["task_id"]), str(row["depends_on_task_id"]))
                for row in self.connection.execute(
                    """
                    SELECT task_id, depends_on_task_id FROM task_dependencies
                    WHERE run_id = ?
                    """,
                    (run_id,),
                )
            ] + edges
            remaining_dependencies = {node: 0 for node in graph_nodes}
            dependents: dict[str, list[str]] = {node: [] for node in graph_nodes}
            for task_id, depends_on in graph_edges:
                remaining_dependencies[task_id] += 1
                dependents[depends_on].append(task_id)
            ready = [node for node, count in remaining_dependencies.items() if count == 0]
            visited = 0
            while ready:
                node = ready.pop()
                visited += 1
                for dependent in dependents[node]:
                    remaining_dependencies[dependent] -= 1
                    if remaining_dependencies[dependent] == 0:
                        ready.append(dependent)
            if visited != len(graph_nodes):
                raise ValueError("task dependency graph contains a cycle")

            now = utc_now()
            for spec in normalized:
                self._create_task_tx(run_id=run_id, now=now, **spec)
            for task_id, depends_on in edges:
                self.connection.execute(
                    """
                    INSERT INTO task_dependencies(
                        run_id, task_id, depends_on_task_id, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (run_id, task_id, depends_on, now),
                )
            self._reconcile_task_graph_tx(run_id, now)
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    @staticmethod
    def _validate_task_spec(
        *,
        task_id: str,
        access_mode: str,
        max_attempts: int,
        required_role_id: str,
        cwd: str,
        timeout_seconds: float,
        retry_backoff_base_seconds: int,
        retry_backoff_max_seconds: int,
        required_backend: str | None = None,
        required_model: str | None = None,
        required_provider_id: str | None = None,
    ) -> None:
        if not task_id.strip():
            raise ValueError("task_id must not be empty")
        if access_mode not in {"read_only", "write"}:
            raise ValueError("access_mode must be read_only or write")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if not required_role_id.strip():
            raise ValueError("required_role_id must not be empty")
        if required_backend is not None and not required_backend.strip():
            raise ValueError("required_backend must not be empty when provided")
        if required_model is not None and not str(required_model).strip():
            raise ValueError("required_model must not be empty when provided")
        if required_provider_id is not None and not str(required_provider_id).strip():
            raise ValueError("required_provider_id must not be empty when provided")
        if not cwd.strip():
            raise ValueError("cwd must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if retry_backoff_base_seconds < 1:
            raise ValueError("retry_backoff_base_seconds must be at least 1")
        if retry_backoff_max_seconds < retry_backoff_base_seconds:
            raise ValueError(
                "retry_backoff_max_seconds must be at least the base delay"
            )

    def _create_task_tx(
        self,
        *,
        run_id: str,
        task_id: str,
        access_mode: str,
        write_scope: tuple[str, ...],
        max_attempts: int,
        required_role_id: str,
        required_backend: str | None,
        required_model: str | None,
        required_provider_id: str | None = None,
        prompt: str | None,
        cwd: str,
        timeout_seconds: float,
        priority: int,
        retry_backoff_base_seconds: int,
        retry_backoff_max_seconds: int,
        now: str,
        parent_task_id: str | None = None,
        dispatch_source: str = "user",
        required_delivery: bool = True,
        task_kind: str = "implementation",
        acceptance_criteria: tuple[str, ...] = (),
        input_refs: tuple[Any, ...] = (),
        output_contract: Any = None,
        budget: Any = None,
    ) -> None:
        if not dispatch_source.strip():
            raise ValueError("dispatch_source must not be empty")
        if not task_kind.strip():
            raise ValueError("task_kind must not be empty")
        if any(not isinstance(item, str) or not item.strip() for item in acceptance_criteria):
            raise ValueError("acceptance_criteria must contain non-empty strings")
        if parent_task_id is not None:
            parent = self.connection.execute(
                "SELECT run_id FROM tasks WHERE task_id = ?", (parent_task_id,)
            ).fetchone()
            if parent is None or str(parent["run_id"]) != run_id:
                raise ValueError("parent_task_id must belong to the same Run")
        self.connection.execute(
            """
            INSERT INTO tasks(
                task_id, run_id, state, access_mode, write_scope_json,
                max_attempts, parent_task_id, dispatch_source, required_delivery,
                task_kind, acceptance_criteria_json, input_refs_json,
                output_contract_json, budget_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                run_id,
                TaskState.PENDING.value,
                access_mode,
                json.dumps(write_scope),
                max_attempts,
                parent_task_id,
                dispatch_source,
                int(required_delivery),
                task_kind,
                json.dumps(list(acceptance_criteria), ensure_ascii=False),
                json.dumps(list(input_refs), ensure_ascii=False, sort_keys=True),
                None if output_contract is None else json.dumps(
                    output_contract, ensure_ascii=False, sort_keys=True
                ),
                None if budget is None else json.dumps(
                    budget, ensure_ascii=False, sort_keys=True
                ),
                now,
                now,
            ),
        )
        self.connection.execute(
            """
            INSERT INTO task_dispatch_specs(
                task_id, required_role_id, required_backend, required_model,
                required_provider_id,
                instruction_text, cwd,
                timeout_seconds, priority, available_at,
                retry_backoff_base_seconds, retry_backoff_max_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                required_role_id,
                required_backend,
                required_model,
                required_provider_id,
                prompt if prompt is not None else task_id,
                cwd,
                timeout_seconds,
                priority,
                now,
                retry_backoff_base_seconds,
                retry_backoff_max_seconds,
            ),
        )
        self._append_event(
            run_id,
            task_id,
            None,
            "task.created",
            None,
            TaskState.PENDING.value,
            {
                "access_mode": access_mode,
                "write_scope": write_scope,
                "max_attempts": max_attempts,
                "required_role_id": required_role_id,
                "parent_task_id": parent_task_id,
                "dispatch_source": dispatch_source,
            },
        )

    def task_state(self, task_id: str) -> TaskState:
        row = self.connection.execute(
            "SELECT state FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return TaskState(row["state"])

    def reconcile_task_graph(
        self,
        run_id: str,
        *,
        controller: ControllerToken,
        authority: AuthorityToken,
        now: str | None = None,
    ) -> dict[str, int]:
        now_value = self._aware_datetime(now).isoformat()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self._ensure_controller_tx(controller, now_value)
            self._ensure_authority_tx(authority, now_value)
            if controller.run_id != run_id:
                raise FencedControllerError("controller token belongs to another Run")
            result = self._reconcile_task_graph_tx(run_id, now_value)
            self.connection.commit()
            return result
        except BaseException:
            self.connection.rollback()
            raise

    def _reconcile_task_graph_tx(self, run_id: str, now: str) -> dict[str, int]:
        readied = 0
        cancelled = 0
        while True:
            changed = False
            blocked = self.connection.execute(
                """
                SELECT t.task_id, MIN(upstream.task_id) AS upstream_task_id
                FROM tasks t
                JOIN task_dependencies d
                  ON d.run_id = t.run_id AND d.task_id = t.task_id
                JOIN tasks upstream ON upstream.task_id = d.depends_on_task_id
                WHERE t.run_id = ? AND t.state = 'PENDING'
                  AND upstream.state IN ('FAILED', 'CANCELLED')
                GROUP BY t.task_id
                ORDER BY t.created_at, t.task_id
                """,
                (run_id,),
            ).fetchall()
            for row in blocked:
                reason = f"upstream-terminal:{row['upstream_task_id']}"
                requested = self.connection.execute(
                    """
                    UPDATE tasks SET state = 'CANCEL_REQUESTED',
                        terminal_reason = ?, version = version + 1, updated_at = ?
                    WHERE task_id = ? AND run_id = ? AND state = 'PENDING'
                    """,
                    (reason, now, row["task_id"], run_id),
                ).rowcount
                if requested != 1:
                    continue
                self._append_event(
                    run_id,
                    row["task_id"],
                    None,
                    "task.transitioned",
                    TaskState.PENDING.value,
                    TaskState.CANCEL_REQUESTED.value,
                    {"reason": reason},
                )
                self.connection.execute(
                    """
                    UPDATE tasks SET state = 'CANCELLED',
                        version = version + 1, updated_at = ?
                    WHERE task_id = ? AND run_id = ? AND state = 'CANCEL_REQUESTED'
                    """,
                    (now, row["task_id"], run_id),
                )
                self._append_event(
                    run_id,
                    row["task_id"],
                    None,
                    "task.transitioned",
                    TaskState.CANCEL_REQUESTED.value,
                    TaskState.CANCELLED.value,
                    {"reason": reason},
                )
                cancelled += 1
                changed = True

            ready_rows = self.connection.execute(
                """
                SELECT t.task_id
                FROM tasks t
                WHERE t.run_id = ? AND t.state = 'PENDING'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM task_dependencies d
                      JOIN tasks upstream ON upstream.task_id = d.depends_on_task_id
                      WHERE d.run_id = t.run_id AND d.task_id = t.task_id
                        AND upstream.state <> 'COMPLETED'
                        AND NOT EXISTS (
                            SELECT 1 FROM task_results verified
                            WHERE verified.task_id = upstream.task_id
                              AND verified.status = 'verified'
                        )
                  )
                ORDER BY t.created_at, t.task_id
                """,
                (run_id,),
            ).fetchall()
            for row in ready_rows:
                updated = self.connection.execute(
                    """
                    UPDATE tasks SET state = 'READY', version = version + 1,
                        updated_at = ?
                    WHERE task_id = ? AND run_id = ? AND state = 'PENDING'
                    """,
                    (now, row["task_id"], run_id),
                ).rowcount
                if updated != 1:
                    continue
                self._append_event(
                    run_id,
                    row["task_id"],
                    None,
                    "task.transitioned",
                    TaskState.PENDING.value,
                    TaskState.READY.value,
                    {"reason": "dependencies-completed"},
                )
                readied += 1
                changed = True
            if not changed:
                return {"readied": readied, "cancelled": cancelled}

    def transition_task(
        self,
        task_id: str,
        target: TaskState,
        *,
        reason: str,
    ) -> None:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT run_id, state FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            current = TaskState(row["state"])
            ensure_task_transition(current, target)
            if current == TaskState.PENDING and target == TaskState.READY:
                blocking = self.connection.execute(
                    """
                    SELECT upstream.task_id
                    FROM task_dependencies d
                    JOIN tasks upstream ON upstream.task_id = d.depends_on_task_id
                    WHERE d.run_id = ? AND d.task_id = ?
                      AND upstream.state <> 'COMPLETED'
                      AND NOT EXISTS (
                          SELECT 1 FROM task_results verified
                          WHERE verified.task_id = upstream.task_id
                            AND verified.status = 'verified'
                      )
                    LIMIT 1
                    """,
                    (row["run_id"], task_id),
                ).fetchone()
                if blocking is not None:
                    raise ValueError(
                        f"task dependency is incomplete: {blocking['task_id']}"
                    )
            now = utc_now()
            self.connection.execute(
                """
                UPDATE tasks
                SET state = ?, version = version + 1, updated_at = ?
                WHERE task_id = ?
                """,
                (target.value, now, task_id),
            )
            self._append_event(
                row["run_id"],
                task_id,
                None,
                "task.transitioned",
                current.value,
                target.value,
                {"reason": reason},
            )
            if target in {
                TaskState.COMPLETED,
                TaskState.FAILED,
                TaskState.CANCELLED,
            }:
                self._reconcile_task_graph_tx(row["run_id"], now)
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def create_attempt(self, task_id: str, attempt_id: str, agent_id: str) -> int:
        return self.create_attempt_with_lease(task_id, attempt_id, agent_id)[
            "attempt_number"
        ]

    def create_attempt_with_lease(
        self,
        task_id: str,
        attempt_id: str,
        agent_id: str,
        *,
        lease_seconds: int = 60,
    ) -> dict[str, Any]:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be at least 1")
        with self.connection:
            task = self.connection.execute(
                "SELECT run_id, state FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise KeyError(task_id)
            current = TaskState(task["state"])
            ensure_task_transition(current, TaskState.ACTIVE)
            number = self.connection.execute(
                "SELECT COUNT(*) AS count FROM attempts WHERE task_id = ?", (task_id,)
            ).fetchone()["count"] + 1
            generation = self.connection.execute(
                """
                SELECT COALESCE(MAX(generation), 0) + 1 AS generation
                FROM attempts WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()["generation"]
            now = utc_now()
            expires_at = (
                datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
            ).isoformat()
            lease_id = f"lease-{uuid.uuid4()}"
            self.connection.execute(
                """
                INSERT INTO attempts(
                    attempt_id, task_id, agent_id, state, attempt_number,
                    generation, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    task_id,
                    agent_id,
                    AttemptState.ASSIGNED.value,
                    number,
                    generation,
                    now,
                    now,
                ),
            )
            self.connection.execute(
                """
                INSERT INTO assignment_leases(
                    lease_id, attempt_id, task_id, owner_agent_id, generation,
                    state, acquired_at, heartbeat_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?)
                """,
                (
                    lease_id,
                    attempt_id,
                    task_id,
                    agent_id,
                    generation,
                    now,
                    now,
                    expires_at,
                ),
            )
            self.connection.execute(
                """
                UPDATE tasks
                SET state = ?, version = version + 1, updated_at = ?
                WHERE task_id = ?
                """,
                (TaskState.ACTIVE.value, now, task_id),
            )
            self._append_event(
                task["run_id"],
                task_id,
                attempt_id,
                "attempt.created",
                None,
                AttemptState.ASSIGNED.value,
                {
                    "agent_id": agent_id,
                    "attempt_number": number,
                    "generation": generation,
                    "lease_id": lease_id,
                },
            )
            self._append_event(
                task["run_id"],
                task_id,
                attempt_id,
                "task.transitioned",
                current.value,
                TaskState.ACTIVE.value,
                {"reason": "attempt-created"},
            )
            return {
                "attempt_number": number,
                "generation": generation,
                "lease_id": lease_id,
                "expires_at": expires_at,
            }

    def attempt_state(self, attempt_id: str) -> AttemptState:
        row = self.connection.execute(
            "SELECT state FROM attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise KeyError(attempt_id)
        return AttemptState(row["state"])

    def transition_attempt(
        self,
        attempt_id: str,
        target: AttemptState,
        *,
        reason: str,
    ) -> None:
        if target == AttemptState.SUBMITTED:
            raise ValueError("use submit_attempt so the lease fencing check is enforced")
        with self.connection:
            row = self.connection.execute(
                """
                SELECT a.state, a.task_id, t.run_id
                FROM attempts a JOIN tasks t ON t.task_id = a.task_id
                WHERE a.attempt_id = ?
                """,
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise KeyError(attempt_id)
            current = AttemptState(row["state"])
            ensure_attempt_transition(current, target)
            self.connection.execute(
                "UPDATE attempts SET state = ?, updated_at = ? WHERE attempt_id = ?",
                (target.value, utc_now(), attempt_id),
            )
            self._append_event(
                row["run_id"],
                row["task_id"],
                attempt_id,
                "attempt.transitioned",
                current.value,
                target.value,
                {"reason": reason},
            )

    def heartbeat_attempt(
        self,
        attempt_id: str,
        generation: int,
        *,
        lease_seconds: int = 60,
        now: str | None = None,
    ) -> str:
        as_of = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)
        if as_of.tzinfo is None:
            raise ValueError("now must include a timezone")
        now_value = as_of.isoformat()
        expires_at = (as_of + timedelta(seconds=lease_seconds)).isoformat()
        with self.connection:
            updated = self.connection.execute(
                """
                UPDATE assignment_leases
                SET heartbeat_at = ?, expires_at = ?
                WHERE attempt_id = ? AND generation = ? AND state = 'ACTIVE'
                  AND expires_at > ?
                """,
                (
                    now_value,
                    expires_at,
                    attempt_id,
                    generation,
                    now_value,
                ),
            ).rowcount
            if updated != 1:
                raise FencedAttemptError("attempt lease is stale or generation mismatched")
        return expires_at

    def record_progress_heartbeat(
        self,
        attempt_id: str,
        generation: int,
        *,
        phase: str,
        progress: Mapping[str, Any] | None = None,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Persist a small progress checkpoint under the attempt lease fence."""
        if not phase.strip():
            raise ValueError("phase must not be empty")
        as_of = self._aware_datetime(now)
        now_value = as_of.isoformat()
        payload = dict(progress or {})
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                """
                SELECT a.*, t.run_id, t.state AS task_state,
                       l.state AS lease_state, l.expires_at
                FROM attempts a
                JOIN tasks t ON t.task_id=a.task_id
                JOIN assignment_leases l ON l.attempt_id=a.attempt_id
                WHERE a.attempt_id=?
                """,
                (attempt_id,),
            ).fetchone()
            if (
                row is None
                or int(row["generation"]) != generation
                or row["lease_state"] != "ACTIVE"
                or row["expires_at"] <= now_value
                or row["state"] not in {
                    AttemptState.ASSIGNED.value,
                    AttemptState.RUNNING.value,
                    AttemptState.CANCEL_REQUESTED.value,
                }
            ):
                raise FencedAttemptError("attempt lease is stale or generation mismatched")
            heartbeat_id = f"heartbeat-{uuid.uuid4().hex[:16]}"
            self.connection.execute(
                """
                INSERT INTO progress_heartbeats(
                    heartbeat_id, run_id, task_id, attempt_id, generation,
                    phase, progress_json, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    heartbeat_id,
                    row["run_id"],
                    row["task_id"],
                    attempt_id,
                    generation,
                    phase[:100],
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    now_value,
                ),
            )
            self._append_event(
                row["run_id"], row["task_id"], attempt_id,
                "attempt.progress", row["state"], row["state"],
                {"heartbeat_id": heartbeat_id, "phase": phase[:100], "progress": payload},
            )
            self.connection.commit()
            return {
                "heartbeat_id": heartbeat_id,
                "run_id": row["run_id"],
                "task_id": row["task_id"],
                "attempt_id": attempt_id,
                "generation": generation,
                "phase": phase[:100],
                "progress": payload,
                "observed_at": now_value,
            }
        except BaseException:
            self.connection.rollback()
            raise

    def list_inactive_attempts(
        self,
        run_id: str,
        *,
        idle_seconds: int,
        now: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return active attempts whose latest progress/lease heartbeat is old."""
        if idle_seconds < 1:
            raise ValueError("idle_seconds must be at least 1")
        cutoff = (
            self._aware_datetime(now) - timedelta(seconds=idle_seconds)
        ).isoformat()
        rows = self.connection.execute(
            """
            SELECT a.attempt_id, a.task_id, a.agent_id, a.generation,
                   a.state, l.heartbeat_at AS lease_heartbeat_at,
                   MAX(p.observed_at) AS progress_at,
                   MAX(COALESCE(p.observed_at, l.heartbeat_at)) AS last_activity
            FROM attempts a
            JOIN tasks t ON t.task_id=a.task_id
            JOIN assignment_leases l ON l.attempt_id=a.attempt_id
            LEFT JOIN progress_heartbeats p ON p.attempt_id=a.attempt_id
            WHERE t.run_id=? AND a.state IN ('ASSIGNED', 'RUNNING', 'CANCEL_REQUESTED')
              AND l.state='ACTIVE'
            GROUP BY a.attempt_id, a.task_id, a.agent_id, a.generation,
                     a.state, l.heartbeat_at
            HAVING last_activity <= ?
            ORDER BY last_activity, a.attempt_id
            """,
            (run_id, cutoff),
        ).fetchall()
        return [dict(row) for row in rows]

    def submit_attempt(
        self,
        attempt_id: str,
        generation: int,
        *,
        reason: str,
    ) -> None:
        with self.connection:
            now = utc_now()
            row = self.connection.execute(
                """
                SELECT a.state, a.task_id, a.generation, t.run_id, t.state AS task_state,
                       l.state AS lease_state, l.expires_at
                FROM attempts a
                JOIN tasks t ON t.task_id = a.task_id
                JOIN assignment_leases l ON l.attempt_id = a.attempt_id
                WHERE a.attempt_id = ?
                """,
                (attempt_id,),
            ).fetchone()
            if (
                row is None
                or row["generation"] != generation
                or row["lease_state"] != "ACTIVE"
                or row["expires_at"] <= now
            ):
                raise FencedAttemptError("attempt lease is stale or generation mismatched")
            current = AttemptState(row["state"])
            ensure_attempt_transition(current, AttemptState.SUBMITTED)
            if TaskState(row["task_state"]) != TaskState.ACTIVE:
                raise FencedAttemptError("task no longer accepts this attempt")
            released = self.connection.execute(
                """
                UPDATE assignment_leases
                SET state = 'RELEASED', closed_at = ?, close_reason = ?
                WHERE attempt_id = ? AND generation = ? AND state = 'ACTIVE'
                  AND expires_at > ?
                """,
                (now, "submitted", attempt_id, generation, now),
            ).rowcount
            if released != 1:
                raise FencedAttemptError("attempt lease expired before submission")
            self.connection.execute(
                """
                UPDATE attempts
                SET state = ?, finished_at = ?, updated_at = ?
                WHERE attempt_id = ?
                """,
                (AttemptState.SUBMITTED.value, now, now, attempt_id),
            )
            self.connection.execute(
                """
                UPDATE tasks
                SET state = ?, version = version + 1, updated_at = ?
                WHERE task_id = ?
                """,
                (TaskState.REVIEW.value, now, row["task_id"]),
            )
            self._append_event(
                row["run_id"],
                row["task_id"],
                attempt_id,
                "attempt.transitioned",
                current.value,
                AttemptState.SUBMITTED.value,
                {"reason": reason, "generation": generation},
            )
            self._append_event(
                row["run_id"],
                row["task_id"],
                attempt_id,
                "task.transitioned",
                TaskState.ACTIVE.value,
                TaskState.REVIEW.value,
                {"reason": reason},
            )

    def recover_lost_attempt(
        self,
        attempt_id: str,
        generation: int,
        *,
        reason: str,
        expired_at_or_before: str | None = None,
    ) -> str:
        with self.connection:
            row = self.connection.execute(
                """
                SELECT a.state, a.task_id, a.generation, a.recovery_outcome,
                       t.run_id, t.state AS task_state, t.max_attempts,
                       COALESCE(d.retry_backoff_base_seconds, 1)
                           AS retry_backoff_base_seconds,
                       COALESCE(d.retry_backoff_max_seconds, 60)
                           AS retry_backoff_max_seconds,
                       (SELECT COUNT(*) FROM attempts x WHERE x.task_id = a.task_id)
                           AS attempt_count,
                       l.state AS lease_state, l.expires_at
                FROM attempts a
                JOIN tasks t ON t.task_id = a.task_id
                JOIN assignment_leases l ON l.attempt_id = a.attempt_id
                LEFT JOIN task_dispatch_specs d ON d.task_id = a.task_id
                WHERE a.attempt_id = ?
                """,
                (attempt_id,),
            ).fetchone()
            if row is None or row["generation"] != generation:
                raise FencedAttemptError("attempt generation mismatched")
            if row["recovery_outcome"]:
                return str(row["recovery_outcome"])
            if row["lease_state"] != "ACTIVE" or AttemptState(row["state"]) not in {
                AttemptState.ASSIGNED,
                AttemptState.RUNNING,
            }:
                raise FencedAttemptError("attempt is not recoverable")
            if TaskState(row["task_state"]) != TaskState.ACTIVE:
                raise FencedAttemptError("task is not active")
            outcome = (
                "requeued"
                if row["attempt_count"] < row["max_attempts"]
                else "failed"
            )
            target = TaskState.READY if outcome == "requeued" else TaskState.FAILED
            now = utc_now()
            retry_delay: int | None = None
            retry_available_at: str | None = None
            if outcome == "requeued":
                retry_delay, retry_available_at = self._retry_schedule(
                    attempt_count=int(row["attempt_count"]),
                    base_seconds=int(row["retry_backoff_base_seconds"]),
                    max_seconds=int(row["retry_backoff_max_seconds"]),
                    now=now,
                )
            if expired_at_or_before is None:
                lease_updated = self.connection.execute(
                    """
                    UPDATE assignment_leases
                    SET state = 'EXPIRED', closed_at = ?, close_reason = ?
                    WHERE attempt_id = ? AND generation = ? AND state = 'ACTIVE'
                    """,
                    (now, reason, attempt_id, generation),
                ).rowcount
            else:
                lease_updated = self.connection.execute(
                    """
                    UPDATE assignment_leases
                    SET state = 'EXPIRED', closed_at = ?, close_reason = ?
                    WHERE attempt_id = ? AND generation = ? AND state = 'ACTIVE'
                      AND expires_at <= ?
                    """,
                    (
                        now,
                        reason,
                        attempt_id,
                        generation,
                        expired_at_or_before,
                    ),
                ).rowcount
            if lease_updated != 1:
                raise FencedAttemptError("attempt lease was renewed or closed")
            self.connection.execute(
                """
                UPDATE attempts
                SET state = ?, finished_at = ?, terminal_reason = ?,
                    recovery_outcome = ?, updated_at = ?
                WHERE attempt_id = ?
                """,
                (
                    AttemptState.STALE.value,
                    now,
                    reason,
                    outcome,
                    now,
                    attempt_id,
                ),
            )
            if retry_available_at is not None:
                self.connection.execute(
                    "UPDATE task_dispatch_specs SET available_at = ? WHERE task_id = ?",
                    (retry_available_at, row["task_id"]),
                )
            self.connection.execute(
                """
                UPDATE tasks
                SET state = ?, terminal_reason = ?, version = version + 1,
                    updated_at = ? WHERE task_id = ?
                """,
                (
                    target.value,
                    reason if target == TaskState.FAILED else None,
                    now,
                    row["task_id"],
                ),
            )
            self._append_event(
                row["run_id"],
                row["task_id"],
                attempt_id,
                "attempt.transitioned",
                row["state"],
                AttemptState.STALE.value,
                {"reason": reason, "generation": generation},
            )
            self._append_event(
                row["run_id"],
                row["task_id"],
                attempt_id,
                "task.transitioned",
                TaskState.ACTIVE.value,
                target.value,
                {
                    "reason": reason,
                    "recovery_outcome": outcome,
                    "retry_delay_seconds": retry_delay,
                    "available_at": retry_available_at,
                    "attempt_count": row["attempt_count"],
                },
            )
            if target == TaskState.FAILED:
                self._reconcile_task_graph_tx(row["run_id"], now)
            return outcome

    def attempt_generation(self, attempt_id: str) -> int:
        row = self.connection.execute(
            "SELECT generation FROM attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise KeyError(attempt_id)
        return int(row["generation"])

    def task_terminal_reason(self, task_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT terminal_reason FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return row["terminal_reason"]

    def delete_task(
        self,
        run_id: str,
        task_id: str,
        controller: ControllerToken,
        authority: AuthorityToken,
        *,
        reason: str = "console-delete",
    ) -> dict[str, str]:
        """Remove a task and its execution records without stealing a lease.

        Only non-running work may be deleted. Events remain as an audit trail;
        child records are removed in FK-safe order so the task row can be
        removed without leaving an executable dispatch behind.
        """
        if controller.run_id != run_id or authority.run_id != run_id:
            raise FencedControllerError("task delete token belongs to another Run")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            now = self._aware_datetime(None).isoformat()
            self._ensure_controller_tx(controller, now)
            self._ensure_authority_tx(authority, now)
            task = self.connection.execute(
                "SELECT state FROM tasks WHERE task_id = ? AND run_id = ?",
                (task_id, run_id),
            ).fetchone()
            if task is None:
                raise KeyError(task_id)
            state = str(task["state"])
            if state in {"ACTIVE", "INTEGRATION", "CANCEL_REQUESTED"}:
                raise ValueError(
                    f"task {task_id} is {state}; cancel and wait before deleting"
                )
            active_call = self.connection.execute(
                """
                SELECT call_id FROM backend_calls
                WHERE task_id = ? AND state IN ('starting', 'running', 'cancel_requested')
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            if active_call is not None:
                raise ValueError(
                    f"task {task_id} still has an active backend call "
                    f"{active_call['call_id']}"
                )

            self._append_event(
                run_id,
                task_id,
                None,
                "task.deleted",
                state,
                None,
                {"reason": reason[:200], "controller_epoch": controller.epoch},
            )
            self.connection.execute(
                "DELETE FROM approval_decisions WHERE request_id IN "
                "(SELECT request_id FROM approval_requests WHERE task_id = ?)",
                (task_id,),
            )
            for table in (
                "approval_requests",
                "assignment_leases",
                "backend_calls",
                "budget_reservations",
                "message_deliveries",
                "messages",
                "review_decisions",
                "merge_queue",
                "integration_issues",
                "role_bindings",
            ):
                self.connection.execute(
                    f"DELETE FROM {table} WHERE task_id = ?", (task_id,)
                )
            self.connection.execute(
                "DELETE FROM task_dependencies "
                "WHERE task_id = ? OR depends_on_task_id = ?",
                (task_id, task_id),
            )
            self.connection.execute(
                "DELETE FROM task_dispatch_specs WHERE task_id = ?", (task_id,)
            )
            self.connection.execute("DELETE FROM attempts WHERE task_id = ?", (task_id,))
            self.connection.execute(
                "DELETE FROM tasks WHERE task_id = ? AND run_id = ?",
                (task_id, run_id),
            )
            self.connection.commit()
            return {"run_id": run_id, "task_id": task_id, "state": state}
        except BaseException:
            self.connection.rollback()
            raise

    def expired_active_attempts(
        self,
        *,
        now: str | None = None,
        limit: int = 100,
        run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        rows = self.connection.execute(
            """
            SELECT a.attempt_id, a.generation, a.task_id, t.run_id,
                   l.expires_at
            FROM attempts a
            JOIN tasks t ON t.task_id = a.task_id
            JOIN assignment_leases l ON l.attempt_id = a.attempt_id
            WHERE a.state IN (?, ?)
              AND t.state = ?
              AND l.state = 'ACTIVE'
              AND l.expires_at <= ?
              AND (? IS NULL OR t.run_id = ?)
            ORDER BY l.expires_at, a.attempt_id
            LIMIT ?
            """,
            (
                AttemptState.ASSIGNED.value,
                AttemptState.RUNNING.value,
                TaskState.ACTIVE.value,
                now or utc_now(),
                run_id,
                run_id,
                limit,
            ),
        ).fetchall()
        return [dict(row) for row in rows]

    def _append_message_tx(self, message: MessageEnvelope) -> None:
        """Insert a message and its recipient deliveries in the caller's tx."""
        kind = message.message_type or message.kind
        # 旧版本允许扩展 kind（例如 artifact.submitted）；R5 只为标准
        # kind 提供语义，不破坏已有审计消息。
        if not kind.strip():
            raise ValueError("message kind must not be empty")
        if message.target_agent_id:
            recipients = tuple(
                recipient
                for recipient in message.recipients
                if recipient.id == message.target_agent_id
            )
            if not recipients:
                raise ValueError("target_agent_id must match a recipient")
        else:
            recipients = message.recipients
        envelope = message.to_dict()
        envelope["message_type"] = kind
        self.connection.execute(
            """
            INSERT INTO messages(
                message_id, idempotency_key, run_id, task_id,
                envelope_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                message.message_id,
                message.idempotency_key,
                message.run_id,
                message.task_id,
                json.dumps(envelope, ensure_ascii=False, sort_keys=True),
                message.created_at,
            ),
        )
        for recipient in recipients:
            self.connection.execute(
                """
                INSERT INTO message_deliveries(
                    delivery_id, message_id, run_id, task_id,
                    recipient_type, recipient_id, status, attempts,
                    available_at, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'QUEUED', 0, ?, ?, ?)
                """,
                (
                    f"delivery-{uuid.uuid4().hex[:16]}",
                    message.message_id,
                    message.run_id,
                    message.task_id,
                    recipient.type,
                    recipient.id,
                    message.created_at,
                    message.expires_at,
                    message.created_at,
                ),
            )
        self._append_event(
            message.run_id,
            message.task_id,
            message.attempt_id,
            "message.persisted",
            None,
            "QUEUED",
            {
                "message_id": message.message_id,
                "kind": kind,
                "sequence": message.sequence,
                "source": message.source,
                "attempt_id": message.attempt_id,
            },
        )

    def append_message(self, message: MessageEnvelope) -> None:
        """Persist a protocol intent and durable per-recipient deliveries."""
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self._append_message_tx(message)
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def list_message_deliveries(
        self,
        run_id: str,
        *,
        task_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT d.*, m.envelope_json, m.created_at AS message_created_at
            FROM message_deliveries d
            JOIN messages m ON m.message_id = d.message_id
            WHERE d.run_id = ?
        """
        params: list[Any] = [run_id]
        if task_id is not None:
            query += " AND d.task_id = ?"
            params.append(task_id)
        if status is not None:
            query += " AND d.status = ?"
            params.append(status)
        query += " ORDER BY d.created_at, d.delivery_id"
        return [dict(row) for row in self.connection.execute(query, params).fetchall()]

    def _message_delivery_is_stale_tx(self, envelope: Mapping[str, Any]) -> str | None:
        """Return a deterministic reason when guidance targets a closed attempt."""
        if str(envelope.get("message_type") or envelope.get("kind") or "") != "user_guidance":
            return None
        attempt_id = envelope.get("attempt_id")
        if not attempt_id:
            return "guidance_missing_attempt"
        row = self.connection.execute(
            "SELECT state FROM attempts WHERE attempt_id = ?", (str(attempt_id),)
        ).fetchone()
        if row is None or str(row["state"]) not in {
            AttemptState.ASSIGNED.value,
            AttemptState.RUNNING.value,
            AttemptState.CANCEL_REQUESTED.value,
        }:
            return "stale_attempt"
        return None

    def expire_message_deliveries(
        self,
        run_id: str,
        *,
        controller: ControllerToken,
        now: str | None = None,
    ) -> int:
        """Expire queued/in-flight messages without bypassing controller fencing."""
        now_value = self._aware_datetime(now).isoformat()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self._ensure_controller_tx(controller, now_value)
            if controller.run_id != run_id:
                raise FencedControllerError("message run does not match controller")
            rows = self.connection.execute(
                """
                SELECT delivery_id, task_id FROM message_deliveries
                WHERE run_id = ? AND status IN ('QUEUED', 'DELIVERED')
                  AND expires_at IS NOT NULL AND expires_at <= ?
                """,
                (run_id, now_value),
            ).fetchall()
            for row in rows:
                self.connection.execute(
                    """
                    UPDATE message_deliveries
                    SET status='EXPIRED', failed_at=?, lease_until=NULL,
                        last_error='message expired'
                    WHERE delivery_id=? AND status IN ('QUEUED', 'DELIVERED')
                    """,
                    (now_value, row["delivery_id"]),
                )
                self._append_event(
                    run_id, row["task_id"], None, "message.expired",
                    "DELIVERED", "EXPIRED", {"delivery_id": row["delivery_id"]},
                )
            self.connection.commit()
            return len(rows)
        except BaseException:
            self.connection.rollback()
            raise

    def claim_message_deliveries(
        self,
        run_id: str,
        *,
        controller: ControllerToken,
        consumer_id: str,
        limit: int = 100,
        lease_seconds: int = 60,
        now: str | None = None,
    ) -> list[dict[str, Any]]:
        """Claim queued deliveries; expired delivery leases are safely redelivered."""
        if not consumer_id.strip():
            raise ValueError("consumer_id must not be empty")
        if limit < 1 or lease_seconds < 1:
            raise ValueError("limit and lease_seconds must be positive")
        as_of = self._aware_datetime(now)
        now_value = as_of.isoformat()
        lease_until = (as_of + timedelta(seconds=lease_seconds)).isoformat()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self._ensure_controller_tx(controller, now_value)
            if controller.run_id != run_id:
                raise FencedControllerError("message run does not match controller")
            self.connection.execute(
                """
                UPDATE message_deliveries
                SET status='QUEUED', consumer_id=NULL, lease_until=NULL
                WHERE run_id=? AND status='DELIVERED' AND lease_until IS NOT NULL
                  AND lease_until <= ?
                """,
                (run_id, now_value),
            )
            rows = self.connection.execute(
                """
                SELECT d.*, m.envelope_json
                FROM message_deliveries d
                JOIN messages m ON m.message_id = d.message_id
                WHERE d.run_id=? AND d.status='QUEUED' AND d.available_at <= ?
                  AND (d.expires_at IS NULL OR d.expires_at > ?)
                ORDER BY d.created_at, d.delivery_id LIMIT ?
                """,
                (run_id, now_value, now_value, limit),
            ).fetchall()
            claimed: list[dict[str, Any]] = []
            for row in rows:
                envelope = json.loads(str(row["envelope_json"]))
                stale_reason = self._message_delivery_is_stale_tx(envelope)
                if stale_reason is not None:
                    self.connection.execute(
                        """
                        UPDATE message_deliveries
                        SET status='FAILED', failed_at=?, last_error=?, lease_until=NULL
                        WHERE delivery_id=? AND status='QUEUED'
                        """,
                        (now_value, stale_reason, row["delivery_id"]),
                    )
                    self._append_event(
                        run_id, row["task_id"], envelope.get("attempt_id"),
                        "message.failed", "QUEUED", "FAILED",
                        {"delivery_id": row["delivery_id"], "reason": stale_reason},
                    )
                    continue
                updated = self.connection.execute(
                    """
                    UPDATE message_deliveries
                    SET status='DELIVERED', attempts=attempts+1,
                        consumer_id=?, lease_until=?, delivered_at=?
                    WHERE delivery_id=? AND status='QUEUED'
                    """,
                    (consumer_id, lease_until, now_value, row["delivery_id"]),
                ).rowcount
                if updated != 1:
                    continue
                delivery = dict(row)
                delivery.update(
                    {
                        "status": "DELIVERED",
                        "attempts": int(row["attempts"] or 0) + 1,
                        "consumer_id": consumer_id,
                        "lease_until": lease_until,
                        "envelope": envelope,
                    }
                )
                claimed.append(delivery)
                self._append_event(
                    run_id, row["task_id"], envelope.get("attempt_id"),
                    "message.delivered", "QUEUED", "DELIVERED",
                    {"delivery_id": row["delivery_id"], "consumer_id": consumer_id},
                )
            self.connection.commit()
            return claimed
        except BaseException:
            self.connection.rollback()
            raise

    def acknowledge_message(
        self,
        delivery_id: str,
        *,
        controller: ControllerToken,
        consumer_id: str,
    ) -> str:
        """Acknowledge one claimed delivery; duplicate ack is idempotent."""
        if not consumer_id.strip():
            raise ValueError("consumer_id must not be empty")
        now = utc_now()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self._ensure_controller_tx(controller, now)
            row = self.connection.execute(
                "SELECT * FROM message_deliveries WHERE delivery_id=?", (delivery_id,)
            ).fetchone()
            if row is None:
                raise KeyError(delivery_id)
            if row["run_id"] != controller.run_id:
                raise FencedControllerError("message delivery belongs to another Run")
            if row["status"] == "ACKNOWLEDGED":
                self.connection.commit()
                return "acknowledged"
            if row["status"] != "DELIVERED" or row["consumer_id"] != consumer_id:
                raise FencedAttemptError("message delivery is not owned by consumer")
            self.connection.execute(
                """
                UPDATE message_deliveries
                SET status='ACKNOWLEDGED', acknowledged_at=?, lease_until=NULL
                WHERE delivery_id=? AND status='DELIVERED' AND consumer_id=?
                """,
                (now, delivery_id, consumer_id),
            )
            self._append_event(
                row["run_id"], row["task_id"], None, "message.acknowledged",
                "DELIVERED", "ACKNOWLEDGED", {"delivery_id": delivery_id, "consumer_id": consumer_id},
            )
            self.connection.commit()
            return "acknowledged"
        except BaseException:
            self.connection.rollback()
            raise

    def fail_message_delivery(
        self,
        delivery_id: str,
        *,
        controller: ControllerToken,
        consumer_id: str,
        reason: str,
        retry_delay_seconds: int = 1,
    ) -> str:
        """Return a failed delivery to the queue, or move it to dead letter."""
        if not reason.strip():
            raise ValueError("reason must not be empty")
        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds must not be negative")
        now_dt = self._aware_datetime(None)
        now = now_dt.isoformat()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self._ensure_controller_tx(controller, now)
            row = self.connection.execute(
                "SELECT * FROM message_deliveries WHERE delivery_id=?", (delivery_id,)
            ).fetchone()
            if row is None:
                raise KeyError(delivery_id)
            if row["run_id"] != controller.run_id:
                raise FencedControllerError("message delivery belongs to another Run")
            if row["status"] == "ACKNOWLEDGED":
                self.connection.commit()
                return "acknowledged"
            if row["status"] != "DELIVERED" or row["consumer_id"] != consumer_id:
                raise FencedAttemptError("message delivery is not owned by consumer")
            attempts = int(row["attempts"] or 0)
            terminal = attempts >= self.MAX_MESSAGE_ATTEMPTS
            if terminal:
                status, available = "FAILED", now
            else:
                status = "QUEUED"
                available = (now_dt + timedelta(seconds=retry_delay_seconds)).isoformat()
            self.connection.execute(
                """
                UPDATE message_deliveries
                SET status=?, available_at=?, failed_at=?, consumer_id=NULL,
                    lease_until=NULL, last_error=?
                WHERE delivery_id=? AND status='DELIVERED' AND consumer_id=?
                """,
                (status, available, now, reason[:500], delivery_id, consumer_id),
            )
            self._append_event(
                row["run_id"], row["task_id"], None,
                "message.dead_letter" if terminal else "message.retry_scheduled",
                "DELIVERED", status,
                {"delivery_id": delivery_id, "attempts": attempts, "reason": reason[:500]},
            )
            self.connection.commit()
            return status.lower()
        except BaseException:
            self.connection.rollback()
            raise

    def events(self, *, task_id: str | None = None) -> list[dict[str, Any]]:
        if task_id is None:
            rows = self.connection.execute(
                "SELECT * FROM events ORDER BY rowid"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM events WHERE task_id = ? ORDER BY rowid", (task_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def summary(self, *, run_id: str | None = None) -> dict[str, Any]:
        if run_id is None:
            runs = self.connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            messages = self.connection.execute(
                "SELECT COUNT(*) FROM messages"
            ).fetchone()[0]
            task_rows = self.connection.execute(
                "SELECT state, COUNT(*) AS count FROM tasks GROUP BY state ORDER BY state"
            ).fetchall()
            attempt_rows = self.connection.execute(
                """
                SELECT state, COUNT(*) AS count
                FROM attempts GROUP BY state ORDER BY state
                """
            ).fetchall()
            events = self.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        else:
            runs = self.connection.execute(
                "SELECT COUNT(*) FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            messages = self.connection.execute(
                "SELECT COUNT(*) FROM messages WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            task_rows = self.connection.execute(
                """
                SELECT state, COUNT(*) AS count
                FROM tasks WHERE run_id = ? GROUP BY state ORDER BY state
                """,
                (run_id,),
            ).fetchall()
            attempt_rows = self.connection.execute(
                """
                SELECT a.state, COUNT(*) AS count
                FROM attempts a
                JOIN tasks t ON t.task_id = a.task_id
                WHERE t.run_id = ?
                GROUP BY a.state ORDER BY a.state
                """,
                (run_id,),
            ).fetchall()
            events = self.connection.execute(
                "SELECT COUNT(*) FROM events WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
        return {
            "runs": runs,
            "tasks": {row["state"]: row["count"] for row in task_rows},
            "attempts": {row["state"]: row["count"] for row in attempt_rows},
            "messages": messages,
            "events": events,
        }

    def _migrate_schema(self) -> None:
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version > CURRENT_SCHEMA_VERSION:
            raise RuntimeError(f"database schema version {version} is newer than supported")
        if version < 2:
            with self.connection:
                task_columns = {
                    row["name"]
                    for row in self.connection.execute("PRAGMA table_info(tasks)")
                }
                if "max_attempts" not in task_columns:
                    self.connection.execute(
                        "ALTER TABLE tasks ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 2"
                    )
                if "terminal_reason" not in task_columns:
                    self.connection.execute(
                        "ALTER TABLE tasks ADD COLUMN terminal_reason TEXT"
                    )
                attempt_columns = {
                    row["name"]
                    for row in self.connection.execute("PRAGMA table_info(attempts)")
                }
                for name, declaration in (
                    ("generation", "INTEGER NOT NULL DEFAULT 1"),
                    ("finished_at", "TEXT"),
                    ("terminal_reason", "TEXT"),
                    ("recovery_outcome", "TEXT"),
                ):
                    if name not in attempt_columns:
                        self.connection.execute(
                            f"ALTER TABLE attempts ADD COLUMN {name} {declaration}"
                        )
                self.connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS assignment_leases (
                        lease_id TEXT PRIMARY KEY,
                        attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(attempt_id),
                        task_id TEXT NOT NULL REFERENCES tasks(task_id),
                        owner_agent_id TEXT NOT NULL,
                        generation INTEGER NOT NULL,
                        state TEXT NOT NULL CHECK(state IN ('ACTIVE', 'EXPIRED', 'RELEASED')),
                        acquired_at TEXT NOT NULL,
                        heartbeat_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        closed_at TEXT,
                        close_reason TEXT,
                        UNIQUE(task_id, generation)
                    );
                    PRAGMA user_version=2;
                    """
                )
        if version < 3:
            self._migrate_to_v3()
        if version < 4:
            self._migrate_to_v4()
        if version < 5:
            self._migrate_to_v5()
        if version < 6:
            self._migrate_to_v6()
        if version < 7:
            self._migrate_to_v7()
        if version < 8:
            self._migrate_to_v8()
        if version < 9:
            self._migrate_to_v9()
        if version < 10:
            self._migrate_to_v10()
        if version < 11:
            self._migrate_to_v11()
        if version < 12:
            self._migrate_to_v12()
        if version < 13:
            self._migrate_to_v13()
        if version < 14:
            self._migrate_to_v14()
        if version < 15:
            self._migrate_to_v15()
        if version < 16:
            self._migrate_to_v16()
        if version < 17:
            self._migrate_to_v17()
        if version < 18:
            self._migrate_to_v18()
        if version < 19:
            self._migrate_to_v19()
        if version < 20:
            self._migrate_to_v20()

    def _migrate_to_v3(self) -> None:
        with self.connection:
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_instances (
                    agent_id TEXT PRIMARY KEY,
                    team_id TEXT NOT NULL,
                    pool_id TEXT,
                    backend TEXT NOT NULL,
                    model TEXT,
                    status TEXT NOT NULL CHECK(status IN (
                        'STARTING', 'IDLE', 'BUSY', 'DRAINING', 'STOPPED', 'FAILED'
                    )),
                    capabilities_actual_json TEXT NOT NULL,
                    current_task_id TEXT,
                    workspace_id TEXT,
                    last_heartbeat_at TEXT,
                    authority_epoch INTEGER NOT NULL DEFAULT 0,
                    origin TEXT NOT NULL CHECK(origin IN ('RECONCILER', 'MIGRATED')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    stopped_at TEXT
                )
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS role_bindings (
                    binding_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id),
                    task_id TEXT REFERENCES tasks(task_id),
                    role_id TEXT NOT NULL,
                    role_version INTEGER NOT NULL,
                    binding_kind TEXT NOT NULL CHECK(binding_kind IN ('PRIMARY', 'SECONDARY')),
                    status TEXT NOT NULL CHECK(status IN ('ACTIVE', 'ENDED')),
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    end_reason TEXT
                )
                """
            )
            self.connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_primary_role_per_agent_run
                ON role_bindings(agent_id, run_id)
                WHERE binding_kind = 'PRIMARY' AND status = 'ACTIVE'
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS backend_sessions (
                    session_ref_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id),
                    backend TEXT NOT NULL,
                    provider_session_id TEXT,
                    state TEXT NOT NULL CHECK(state IN (
                        'OPENING', 'IDLE', 'ACTIVE', 'CLOSED', 'FAILED'
                    )),
                    cwd TEXT NOT NULL,
                    replacement_for_session_ref_id TEXT REFERENCES backend_sessions(session_ref_id),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    closed_at TEXT,
                    close_reason TEXT
                )
                """
            )
            self.connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS one_provider_session_per_backend
                ON backend_sessions(backend, provider_session_id)
                WHERE provider_session_id IS NOT NULL
                """
            )
            ambiguous = self.connection.execute(
                """
                SELECT a.agent_id
                FROM attempts a
                JOIN tasks t ON t.task_id = a.task_id
                JOIN runs r ON r.run_id = t.run_id
                GROUP BY a.agent_id
                HAVING COUNT(DISTINCT r.team_id) > 1
                LIMIT 1
                """
            ).fetchone()
            if ambiguous is not None:
                raise RuntimeError(
                    f"historical agent belongs to multiple teams: {ambiguous['agent_id']}"
                )
            now = utc_now()
            historical_agents = self.connection.execute(
                """
                SELECT a.agent_id, MIN(r.team_id) AS team_id,
                       SUM(CASE WHEN a.state IN ('ASSIGNED', 'RUNNING') THEN 1 ELSE 0 END)
                           AS active_attempts,
                       MAX(CASE WHEN a.state IN ('ASSIGNED', 'RUNNING') THEN a.task_id END)
                           AS current_task_id
                FROM attempts a
                JOIN tasks t ON t.task_id = a.task_id
                JOIN runs r ON r.run_id = t.run_id
                GROUP BY a.agent_id
                """
            ).fetchall()
            for agent in historical_agents:
                if agent["active_attempts"] > 1:
                    raise RuntimeError(
                        f"historical agent has multiple active attempts: {agent['agent_id']}"
                    )
                status = (
                    AgentState.BUSY.value
                    if agent["active_attempts"] == 1
                    else AgentState.STOPPED.value
                )
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO agent_instances(
                        agent_id, team_id, backend, status,
                        capabilities_actual_json, current_task_id,
                        authority_epoch, origin, created_at, updated_at, stopped_at
                    ) VALUES (?, ?, 'unknown', ?, '[]', ?, 0, 'MIGRATED', ?, ?, ?)
                    """,
                    (
                        agent["agent_id"],
                        agent["team_id"],
                        status,
                        agent["current_task_id"],
                        now,
                        now,
                        now if status == AgentState.STOPPED.value else None,
                    ),
                )
            violations = self.connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError("foreign key check failed during schema v3 migration")
            self.connection.execute("PRAGMA user_version=3")

    def _migrate_to_v4(self) -> None:
        with self.connection:
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS backend_calls (
                    call_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
                    generation INTEGER NOT NULL,
                    agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id),
                    session_ref_id TEXT NOT NULL REFERENCES backend_sessions(session_ref_id),
                    backend TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN (
                        'starting', 'running', 'cancel_requested', 'succeeded',
                        'failed', 'timed_out', 'cancelled', 'blocked', 'orphaned'
                    )),
                    request_digest TEXT NOT NULL,
                    provider_call_id TEXT,
                    backend_invoked INTEGER NOT NULL DEFAULT 0,
                    backend_may_still_run INTEGER NOT NULL DEFAULT 0,
                    requested_at TEXT NOT NULL,
                    started_at TEXT,
                    cancel_requested_at TEXT,
                    finished_at TEXT,
                    result_json TEXT,
                    failure_json TEXT,
                    usage_json TEXT,
                    late_result INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self.connection.execute("PRAGMA user_version=4")

    def _migrate_to_v5(self) -> None:
        with self.connection:
            self.connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS one_open_session_per_agent_run
                ON backend_sessions(agent_id, run_id)
                WHERE state IN ('OPENING', 'IDLE', 'ACTIVE')
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS pool_reconcile_locks (
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    pool_id TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    PRIMARY KEY(run_id, pool_id)
                )
                """
            )
            self.connection.execute("PRAGMA user_version=5")

    def _migrate_to_v6(self) -> None:
        with self.connection:
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS task_dispatch_specs (
                    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
                    required_role_id TEXT NOT NULL,
                    instruction_text TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    timeout_seconds REAL NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 0,
                    available_at TEXT NOT NULL
                )
                """
            )
            call_columns = {
                row["name"]
                for row in self.connection.execute("PRAGMA table_info(backend_calls)")
            }
            for name, declaration in (
                ("disposition", "TEXT"),
                ("settled_at", "TEXT"),
                ("scheduler_owner", "TEXT"),
            ):
                if name not in call_columns:
                    self.connection.execute(
                        f"ALTER TABLE backend_calls ADD COLUMN {name} {declaration}"
                    )
            self.connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS one_call_per_attempt_generation
                ON backend_calls(attempt_id, generation)
                """
            )
            self.connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_lease_per_agent
                ON assignment_leases(owner_agent_id)
                WHERE state = 'ACTIVE'
                """
            )
            self.connection.execute("PRAGMA user_version=6")

    def _migrate_to_v7(self) -> None:
        with self.connection:
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS run_controller_leases (
                    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
                    owner_id TEXT NOT NULL,
                    epoch INTEGER NOT NULL CHECK(epoch > 0),
                    acquired_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )
            call_columns = {
                row["name"]
                for row in self.connection.execute("PRAGMA table_info(backend_calls)")
            }
            if "controller_epoch" not in call_columns:
                self.connection.execute(
                    "ALTER TABLE backend_calls ADD COLUMN controller_epoch INTEGER"
                )
            self.connection.execute("PRAGMA user_version=7")

    def _migrate_to_v8(self) -> None:
        with self.connection:
            self.connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS tasks_run_task_key
                ON tasks(run_id, task_id)
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS task_dependencies (
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    depends_on_task_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(run_id, task_id, depends_on_task_id),
                    CHECK(task_id <> depends_on_task_id),
                    FOREIGN KEY(run_id, task_id)
                        REFERENCES tasks(run_id, task_id),
                    FOREIGN KEY(run_id, depends_on_task_id)
                        REFERENCES tasks(run_id, task_id)
                )
                """
            )
            self.connection.execute(
                """
                CREATE INDEX IF NOT EXISTS task_dependents_lookup
                ON task_dependencies(run_id, depends_on_task_id, task_id)
                """
            )
            dispatch_columns = {
                row["name"]
                for row in self.connection.execute(
                    "PRAGMA table_info(task_dispatch_specs)"
                )
            }
            for name, declaration in (
                (
                    "retry_backoff_base_seconds",
                    "INTEGER NOT NULL DEFAULT 1",
                ),
                (
                    "retry_backoff_max_seconds",
                    "INTEGER NOT NULL DEFAULT 60",
                ),
            ):
                if name not in dispatch_columns:
                    self.connection.execute(
                        f"ALTER TABLE task_dispatch_specs ADD COLUMN {name} {declaration}"
                    )
            self.connection.execute("PRAGMA user_version=8")

    def _migrate_to_v9(self) -> None:
        with self.connection:
            run_columns = {
                row["name"]
                for row in self.connection.execute("PRAGMA table_info(runs)")
            }
            if "control_state" not in run_columns:
                self.connection.execute(
                    "ALTER TABLE runs ADD COLUMN control_state "
                    "TEXT NOT NULL DEFAULT 'RUNNING'"
                )
            dispatch_columns = {
                row["name"]
                for row in self.connection.execute(
                    "PRAGMA table_info(task_dispatch_specs)"
                )
            }
            if "paused" not in dispatch_columns:
                self.connection.execute(
                    "ALTER TABLE task_dispatch_specs ADD COLUMN paused "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            self.connection.execute("PRAGMA user_version=9")

    def _migrate_to_v10(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS merge_queue (
                    merge_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    attempt_id TEXT NOT NULL,
                    result_commit TEXT NOT NULL,
                    base_commit TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'PENDING', 'APPLYING', 'APPLIED', 'CONFLICT', 'FAILED'
                    )),
                    claim_owner TEXT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    claimed_at TEXT,
                    settled_at TEXT
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    outbox_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    aggregate_type TEXT NOT NULL,
                    aggregate_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'PENDING', 'SENT', 'FAILED'
                    )),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    sent_at TEXT
                );
                CREATE TABLE IF NOT EXISTS integration_issues (
                    issue_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    attempt_id TEXT,
                    kind TEXT NOT NULL CHECK(kind IN (
                        'write_scope_overlap', 'content_conflict', 'unexpected'
                    )),
                    detail_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );
                PRAGMA user_version=10;
                """
            )

    def _migrate_to_v11(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS authority_leases (
                    lease_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
                    owner_agent_id TEXT NOT NULL,
                    role_id TEXT NOT NULL,
                    scope TEXT NOT NULL DEFAULT 'supervisor',
                    epoch INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('ACTIVE', 'ENDED')),
                    handoff_state TEXT CHECK(
                        handoff_state IS NULL OR
                        handoff_state IN ('REQUESTED', 'ACCEPTED')
                    ),
                    handoff_target_agent_id TEXT,
                    requested_at TEXT,
                    acquired_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    ended_at TEXT,
                    end_reason TEXT
                );
                CREATE TABLE IF NOT EXISTS approval_requests (
                    request_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    attempt_id TEXT,
                    action_summary TEXT NOT NULL,
                    params_hash TEXT NOT NULL,
                    requested_by TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    single_use INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL CHECK(status IN (
                        'PENDING', 'APPROVED', 'REJECTED', 'EXPIRED', 'USED'
                    )),
                    expires_at TEXT,
                    created_at TEXT NOT NULL,
                    decided_at TEXT,
                    decided_by TEXT
                );
                CREATE TABLE IF NOT EXISTS approval_decisions (
                    decision_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL REFERENCES approval_requests(request_id),
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    decision TEXT NOT NULL CHECK(decision IN ('APPROVED', 'REJECTED')),
                    decided_by TEXT NOT NULL,
                    comment TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS budgets (
                    budget_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
                    max_run_seconds INTEGER,
                    max_calls INTEGER,
                    max_turns INTEGER,
                    max_tasks INTEGER,
                    max_cost_decimal TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                PRAGMA user_version=11;
                """
            )

    def _migrate_to_v12(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS review_decisions (
                    decision_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    attempt_id TEXT,
                    layer TEXT NOT NULL CHECK(layer IN (
                        'deterministic', 'model', 'human'
                    )),
                    decision TEXT NOT NULL CHECK(decision IN (
                        'PASS', 'REWORK', 'BLOCKED', 'APPROVED', 'REJECTED'
                    )),
                    detail_json TEXT NOT NULL,
                    decided_by TEXT NOT NULL,
                    authority_epoch INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                PRAGMA user_version=12;
                """
            )

    def _migrate_to_v13(self) -> None:
        # 任务按厂商路由：任务可声明 required_backend（codex/codebuddy/fake），
        # 派发时只绑定该后端的 agent；NULL/空表示任意后端（向后兼容）。
        with self.connection:
            cols = {
                row["name"]
                for row in self.connection.execute("PRAGMA table_info(task_dispatch_specs)")
            }
            if "required_backend" not in cols:
                self.connection.execute(
                    "ALTER TABLE task_dispatch_specs "
                    "ADD COLUMN required_backend TEXT"
                )
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_dispatch_backend "
                "ON task_dispatch_specs(required_backend)"
            )
            self.connection.execute("PRAGMA user_version=13")

    def _migrate_to_v14(self) -> None:
        # 任务可按具体模型路由；NULL/空表示沿用后端池中任意模型。
        with self.connection:
            cols = {
                row["name"]
                for row in self.connection.execute(
                    "PRAGMA table_info(task_dispatch_specs)"
                )
            }
            if "required_model" not in cols:
                self.connection.execute(
                    "ALTER TABLE task_dispatch_specs "
                    "ADD COLUMN required_model TEXT"
                )
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_dispatch_backend_model "
                "ON task_dispatch_specs(required_backend, required_model)"
            )
            self.connection.execute("PRAGMA user_version=14")

    def _migrate_to_v15(self) -> None:
        # 任务和 Agent 池可绑定 CodeBuddy 的具体提供方（例如火山 CodingPlan）。
        with self.connection:
            agent_columns = {
                row["name"]
                for row in self.connection.execute(
                    "PRAGMA table_info(agent_instances)"
                )
            }
            if "provider_id" not in agent_columns:
                self.connection.execute(
                    "ALTER TABLE agent_instances ADD COLUMN provider_id TEXT"
                )
            dispatch_columns = {
                row["name"]
                for row in self.connection.execute(
                    "PRAGMA table_info(task_dispatch_specs)"
                )
            }
            if "required_provider_id" not in dispatch_columns:
                self.connection.execute(
                    "ALTER TABLE task_dispatch_specs "
                    "ADD COLUMN required_provider_id TEXT"
                )
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_dispatch_provider "
                "ON task_dispatch_specs(required_provider_id)"
            )
            self.connection.execute("PRAGMA user_version=15")

    def _migrate_to_v16(self) -> None:
        """Freeze Run team snapshots and persist supervisor plan revisions."""
        with self.connection:
            run_columns = {
                row["name"] for row in self.connection.execute("PRAGMA table_info(runs)")
            }
            for name, declaration in (
                ("team_snapshot_json", "TEXT"),
                ("team_snapshot_digest", "TEXT"),
                ("approval_mode", "TEXT NOT NULL DEFAULT 'auto'"),
            ):
                if name not in run_columns:
                    self.connection.execute(
                        f"ALTER TABLE runs ADD COLUMN {name} {declaration}"
                    )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS supervisor_plans (
                    plan_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    supervisor_task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    revision INTEGER NOT NULL CHECK(revision > 0),
                    status TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    plan_digest TEXT NOT NULL,
                    permissions_json TEXT,
                    budget_json TEXT,
                    worker_concurrency INTEGER NOT NULL CHECK(worker_concurrency > 0),
                    format_repair_count INTEGER NOT NULL DEFAULT 0,
                    source_call_id TEXT,
                    error_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(run_id, supervisor_task_id, revision)
                )
                """
            )
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_supervisor_plans_run "
                "ON supervisor_plans(run_id, supervisor_task_id, revision)"
            )
            self.connection.execute("PRAGMA user_version=16")

    def _migrate_to_v17(self) -> None:
        """Record attempt-level workspace allocation and published artifacts."""
        with self.connection:
            attempt_columns = {
                row["name"] for row in self.connection.execute("PRAGMA table_info(attempts)")
            }
            for name, declaration in (
                ("workspace_path", "TEXT"),
                ("scratch_path", "TEXT"),
                ("base_commit", "TEXT"),
            ):
                if name not in attempt_columns:
                    self.connection.execute(
                        f"ALTER TABLE attempts ADD COLUMN {name} {declaration}"
                    )
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS run_workspaces (
                    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
                    manifest_path TEXT NOT NULL,
                    manifest_digest TEXT NOT NULL,
                    lifecycle TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
                    path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    candidate_commit TEXT,
                    producer TEXT NOT NULL,
                    version INTEGER NOT NULL CHECK(version > 0),
                    published_at TEXT NOT NULL,
                    UNIQUE(run_id, task_id, attempt_id, path, version)
                );
                CREATE INDEX IF NOT EXISTS idx_artifacts_task
                    ON artifacts(run_id, task_id, attempt_id, version);
                PRAGMA user_version=17;
                """
            )

    def _migrate_to_v18(self) -> None:
        """Persist Hub-owned handoffs, structured results, and task lineage."""
        with self.connection:
            task_columns = {
                row["name"] for row in self.connection.execute("PRAGMA table_info(tasks)")
            }
            for name, declaration in (
                ("parent_task_id", "TEXT REFERENCES tasks(task_id)"),
                ("dispatch_source", "TEXT NOT NULL DEFAULT 'user'"),
                ("required_delivery", "INTEGER NOT NULL DEFAULT 1"),
                ("task_kind", "TEXT NOT NULL DEFAULT 'implementation'"),
                ("acceptance_criteria_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("input_refs_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("output_contract_json", "TEXT"),
                ("budget_json", "TEXT"),
            ):
                if name not in task_columns:
                    self.connection.execute(
                        f"ALTER TABLE tasks ADD COLUMN {name} {declaration}"
                    )
            attempt_columns = {
                row["name"]
                for row in self.connection.execute("PRAGMA table_info(attempts)")
            }
            for name, declaration in (
                ("handoff_digest", "TEXT"),
                ("result_json", "TEXT"),
                ("candidate_commit", "TEXT"),
                ("artifact_refs_json", "TEXT"),
                ("claimed_checks_json", "TEXT"),
                ("remaining_issues_json", "TEXT"),
            ):
                if name not in attempt_columns:
                    self.connection.execute(
                        f"ALTER TABLE attempts ADD COLUMN {name} {declaration}"
                    )
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS handoffs (
                    handoff_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
                    plan_revision INTEGER NOT NULL,
                    package_json TEXT NOT NULL,
                    package_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(task_id, attempt_id)
                );
                CREATE INDEX IF NOT EXISTS idx_handoffs_run
                    ON handoffs(run_id, task_id, created_at);
                CREATE TABLE IF NOT EXISTS task_results (
                    result_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
                    status TEXT NOT NULL CHECK(status IN (
                        'submitted', 'verified', 'blocked', 'needs_input', 'rejected'
                    )),
                    result_json TEXT NOT NULL,
                    result_digest TEXT NOT NULL,
                    candidate_commit TEXT,
                    artifact_refs_json TEXT NOT NULL,
                    verified_by TEXT,
                    created_at TEXT NOT NULL,
                    verified_at TEXT,
                    UNIQUE(attempt_id, result_digest)
                );
                CREATE INDEX IF NOT EXISTS idx_task_results_task
                    ON task_results(run_id, task_id, created_at);
                PRAGMA user_version=18;
                """
            )

    def _migrate_to_v19(self) -> None:
        """Persist independent, candidate-bound verification evidence."""
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS verification_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
                    candidate_commit TEXT NOT NULL,
                    check_name TEXT NOT NULL,
                    check_definition_digest TEXT NOT NULL,
                    command_json TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    exit_code INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('PASS', 'FAIL', 'BLOCKED')),
                    output_sha256 TEXT NOT NULL,
                    output_summary TEXT NOT NULL,
                    environment_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(attempt_id, candidate_commit, check_name,
                           check_definition_digest)
                );
                CREATE INDEX IF NOT EXISTS idx_verification_evidence_task
                    ON verification_evidence(run_id, task_id, attempt_id, candidate_commit);
                PRAGMA user_version=19;
                """
            )

    def _migrate_to_v20(self) -> None:
        """Add durable per-recipient delivery state for protocol messages."""
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS message_deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL REFERENCES messages(message_id),
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    recipient_type TEXT NOT NULL,
                    recipient_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'QUEUED', 'DELIVERED', 'ACKNOWLEDGED', 'FAILED', 'EXPIRED'
                    )),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    consumer_id TEXT,
                    available_at TEXT NOT NULL,
                    lease_until TEXT,
                    delivered_at TEXT,
                    acknowledged_at TEXT,
                    failed_at TEXT,
                    expires_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(message_id, recipient_type, recipient_id)
                );
                CREATE INDEX IF NOT EXISTS idx_message_deliveries_claim
                    ON message_deliveries(run_id, status, available_at, created_at);
                CREATE INDEX IF NOT EXISTS idx_message_deliveries_message
                    ON message_deliveries(message_id, recipient_id);
                CREATE TABLE IF NOT EXISTS progress_heartbeats (
                    heartbeat_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
                    generation INTEGER NOT NULL,
                    phase TEXT NOT NULL,
                    progress_json TEXT NOT NULL,
                    observed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_progress_heartbeats_attempt
                    ON progress_heartbeats(attempt_id, observed_at);
                CREATE TABLE IF NOT EXISTS budget_reservations (
                    reservation_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
                    generation INTEGER NOT NULL,
                    call_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL CHECK(status IN ('RESERVED', 'SETTLED', 'RELEASED')),
                    reserved_at TEXT NOT NULL,
                    settled_at TEXT,
                    released_at TEXT,
                    UNIQUE(attempt_id, generation)
                );
                CREATE INDEX IF NOT EXISTS idx_budget_reservations_run
                    ON budget_reservations(run_id, status, reserved_at);
                PRAGMA user_version=20;
                """
            )
            # 迁移旧 messages 时补建投递行，避免升级后历史协议消息变成
            # 只有审计、没有可恢复状态的“黑洞”。无法解析的旧扩展消息仍
            # 保留在 messages，不阻塞整个数据库升级。
            for row in self.connection.execute(
                "SELECT message_id, run_id, task_id, envelope_json, created_at "
                "FROM messages"
            ).fetchall():
                try:
                    envelope = json.loads(str(row["envelope_json"]))
                    recipients = envelope.get("recipients") or []
                    for recipient in recipients:
                        self.connection.execute(
                            """
                            INSERT OR IGNORE INTO message_deliveries(
                                delivery_id, message_id, run_id, task_id,
                                recipient_type, recipient_id, status, attempts,
                                available_at, expires_at, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, 'QUEUED', 0, ?, ?, ?)
                            """,
                            (
                                f"delivery-{uuid.uuid4().hex[:16]}",
                                row["message_id"],
                                row["run_id"],
                                row["task_id"],
                                str(recipient.get("type") or "agent"),
                                str(recipient["id"]),
                                row["created_at"],
                                envelope.get("expires_at"),
                                row["created_at"],
                            ),
                        )
                except (TypeError, KeyError, ValueError, json.JSONDecodeError):
                    continue

    def _latest_run_for_team(self, team_id: str) -> str | None:
        row = self.connection.execute(
            """
            SELECT run_id FROM runs WHERE team_id = ?
            ORDER BY created_at DESC, rowid DESC LIMIT 1
            """,
            (team_id,),
        ).fetchone()
        return None if row is None else str(row["run_id"])

    @staticmethod
    def _aware_datetime(value: str | None) -> datetime:
        parsed = datetime.fromisoformat(value) if value else datetime.now(timezone.utc)
        if parsed.tzinfo is None:
            raise ValueError("now must include a timezone")
        return parsed.astimezone(timezone.utc)

    def _retry_schedule(
        self,
        *,
        attempt_count: int,
        base_seconds: int,
        max_seconds: int,
        now: str,
    ) -> tuple[int, str]:
        delay = base_seconds
        for _ in range(max(0, attempt_count - 1)):
            if delay >= max_seconds:
                delay = max_seconds
                break
            delay = min(max_seconds, delay * 2)
        available_at = (
            self._aware_datetime(now) + timedelta(seconds=delay)
        ).isoformat()
        return delay, available_at

    def _ensure_controller_tx(self, token: ControllerToken, now: str) -> None:
        row = self.connection.execute(
            """
            SELECT owner_id, epoch, expires_at FROM run_controller_leases
            WHERE run_id = ?
            """,
            (token.run_id,),
        ).fetchone()
        if (
            row is None
            or row["owner_id"] != token.owner_id
            or int(row["epoch"]) != token.epoch
            or row["expires_at"] <= now
        ):
            raise FencedControllerError("Run controller token is stale or expired")

    def _ensure_backend_call_controller_tx(
        self,
        call: sqlite3.Row,
        token: ControllerToken | None,
        now: str,
    ) -> None:
        call_epoch = call["controller_epoch"]
        if call_epoch is None:
            return
        if (
            token is None
            or token.run_id != call["run_id"]
            or token.owner_id != call["scheduler_owner"]
            or token.epoch != int(call_epoch)
        ):
            raise FencedControllerError(
                "backend call belongs to another controller epoch"
            )
        self._ensure_controller_tx(token, now)

    def _event_run_for_agent(self, agent_id: str, team_id: str) -> str | None:
        row = self.connection.execute(
            """
            SELECT run_id FROM role_bindings
            WHERE agent_id = ? AND status = 'ACTIVE'
            ORDER BY started_at DESC, rowid DESC LIMIT 1
            """,
            (agent_id,),
        ).fetchone()
        return str(row["run_id"]) if row is not None else self._latest_run_for_team(team_id)

    def _ensure_agent_and_run_share_team(self, agent_id: str, run_id: str) -> None:
        row = self.connection.execute(
            """
            SELECT a.team_id AS agent_team_id, r.team_id AS run_team_id
            FROM agent_instances a CROSS JOIN runs r
            WHERE a.agent_id = ? AND r.run_id = ?
            """,
            (agent_id, run_id),
        ).fetchone()
        if row is None:
            agent_exists = self.connection.execute(
                "SELECT 1 FROM agent_instances WHERE agent_id = ?", (agent_id,)
            ).fetchone()
            if agent_exists is None:
                raise KeyError(agent_id)
            raise KeyError(run_id)
        if row["agent_team_id"] != row["run_team_id"]:
            raise ValueError("agent and run belong to different teams")

    def _append_event(
        self,
        run_id: str,
        task_id: str | None,
        attempt_id: str | None,
        kind: str,
        from_state: str | None,
        to_state: str | None,
        data: dict[str, Any],
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO events(
                event_id, run_id, task_id, attempt_id, kind, from_state,
                to_state, data_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"evt-{uuid.uuid4()}",
                run_id,
                task_id,
                attempt_id,
                kind,
                from_state,
                to_state,
                json.dumps(data, ensure_ascii=False, sort_keys=True),
                utc_now(),
            ),
        )
