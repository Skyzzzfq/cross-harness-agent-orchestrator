"""Independent, candidate-bound verification for the personal edition.

Only named checks in ``CHECK_DEFINITIONS`` can run.  A model may request a
check name, but it cannot provide an arbitrary shell command or shell string.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from orchestrator.storage.sqlite_store import SQLiteStateStore


CHECK_DEFINITIONS: dict[str, tuple[str, ...]] = {
    "commit_exists": ("git", "cat-file", "-e", "{candidate}^{{commit}}"),
    "diff_check": ("git", "diff", "--check", "{base}..{candidate}"),
    "python_compile": ("{python}", "-m", "compileall", "-q", "orchestrator"),
    "unit_tests": ("{python}", "-m", "unittest", "discover", "-s", "tests"),
}


@dataclass(frozen=True)
class VerificationResult:
    evidence_id: str
    check_name: str
    status: str
    exit_code: int
    candidate_commit: str


class VerificationService:
    """Run fixed checks in the attempt worktree and persist their evidence."""

    def __init__(self, store: SQLiteStateStore, *, timeout_seconds: float = 120.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.store = store
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def check_definition_digest(check_name: str) -> str:
        try:
            definition = CHECK_DEFINITIONS[check_name]
        except KeyError as exc:
            raise ValueError(f"unknown verification check: {check_name}") from exc
        return hashlib.sha256(
            json.dumps(
                {"version": 1, "name": check_name, "argv": definition},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def verify_candidate(
        self,
        run_id: str,
        task_id: str,
        attempt_id: str,
        candidate_commit: str,
        *,
        checks: tuple[str, ...] = ("commit_exists", "diff_check"),
    ) -> list[VerificationResult]:
        if not candidate_commit.strip():
            raise ValueError("candidate_commit must not be empty")
        if not checks:
            raise ValueError("at least one verification check is required")
        if len(set(checks)) != len(checks):
            raise ValueError("verification checks must be unique")
        for check_name in checks:
            if check_name not in CHECK_DEFINITIONS:
                raise ValueError(f"unknown verification check: {check_name}")
        row = self.store.connection.execute(
            """
            SELECT t.run_id, t.task_id, a.attempt_id, a.workspace_path,
                   a.base_commit, r.team_snapshot_json
            FROM tasks t JOIN attempts a ON a.task_id=t.task_id
            JOIN runs r ON r.run_id=t.run_id
            WHERE t.run_id=? AND t.task_id=? AND a.attempt_id=?
            """,
            (run_id, task_id, attempt_id),
        ).fetchone()
        if row is None:
            raise ValueError("verification references an unknown task attempt")
        workspace = Path(str(row["workspace_path"] or ""))
        if not workspace.is_dir():
            raise ValueError("attempt worktree is unavailable for verification")
        base_commit = str(row["base_commit"] or "")
        if "diff_check" in checks and not base_commit:
            raise ValueError("diff_check requires a Hub-recorded base_commit")
        results: list[VerificationResult] = []
        for check_name in checks:
            argv_template = CHECK_DEFINITIONS[check_name]
            argv = tuple(
                item.format(
                    candidate=candidate_commit,
                    base=base_commit,
                    python=sys.executable,
                )
                for item in argv_template
            )
            status = "PASS"
            exit_code = 0
            output = ""
            try:
                environment = os.environ.copy()
                environment["GIT_TERMINAL_PROMPT"] = "0"
                completed = subprocess.run(
                    argv,
                    cwd=workspace,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    shell=False,
                    check=False,
                )
                exit_code = int(completed.returncode)
                output = (completed.stdout or "") + (completed.stderr or "")
                if exit_code != 0:
                    status = "FAIL"
            except subprocess.TimeoutExpired as exc:
                status = "BLOCKED"
                exit_code = -1
                output = f"verification timed out after {self.timeout_seconds}s: {exc}"
            except OSError as exc:
                status = "BLOCKED"
                exit_code = -1
                output = f"verification could not start: {type(exc).__name__}"
            output = output[:4000]
            evidence = self.store.record_verification_evidence(
                run_id,
                task_id,
                attempt_id,
                candidate_commit=candidate_commit,
                check_name=check_name,
                check_definition_digest=self.check_definition_digest(check_name),
                command=argv,
                cwd=str(workspace),
                exit_code=exit_code,
                status=status,
                output_sha256=hashlib.sha256(output.encode("utf-8")).hexdigest(),
                output_summary=output,
                environment={"service": "verification-v1", "python": sys.version.split()[0]},
            )
            results.append(
                VerificationResult(
                    evidence_id=str(evidence["evidence_id"]),
                    check_name=check_name,
                    status=status,
                    exit_code=exit_code,
                    candidate_commit=candidate_commit,
                )
            )
        return results
